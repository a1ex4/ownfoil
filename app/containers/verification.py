"""nstools verification wrapper.

Owns all nstools interaction. Verification is cumulative: the signature test needs the
container decrypted, and the hash test needs the header list the signature test builds
(it is what tells a re-signed-but-intact file apart from a corrupt one). So a deeper
depth always runs the shallower ones, and `signature_valid` covers the structural
decrypt test - missing NCAs, missing tickets, wrong title keys - as well as the RSA-PSS
header signature.

nstools' own `Verify.verify()` is not used: it leaks the container handle when a phase
raises, and its vlevel guard silently promotes anything outside {1, 2} to a full hash.
"""
import logging
import os
from contextlib import contextmanager

from nstools import Verify

from constants import COMPRESS_EXT, DECOMPRESS_EXT

from .container import open_container

logger = logging.getLogger('main')

DEPTH_SIGNATURE = 'signature'
DEPTH_HASH = 'hash'
DEPTHS = (DEPTH_SIGNATURE, DEPTH_HASH)

# The container formats nstools can open. Same set the compressor works on.
VERIFY_EXT = frozenset(COMPRESS_EXT) | frozenset(DECOMPRESS_EXT)

# One label for the pair of verdicts `verify` returns. Derived on read, never stored: a
# status column would be a second copy of what signature_valid and hash_valid already
# say, free to drift from them.
STATUS_UNVERIFIED = 'UNVERIFIED'
STATUS_VALID = 'VALID'
STATUS_REPACK = 'REPACK'
STATUS_CORRUPT = 'CORRUPT'
STATUS_SIGNATURE_OK = 'SIGNATURE_OK'
STATUS_SIGNATURE_FAILED = 'SIGNATURE_FAILED'

# Matches either verdict but never NULL, for the rule where one column does not
# discriminate.
STATUS_ANY = object()

# (status, signature_valid, hash_valid), first match wins. The rules are disjoint, so
# the order is for reading rather than for precedence: a bad content hash outranks
# whatever the signature said, which is why CORRUPT comes first.
STATUS_RULES = (
    (STATUS_CORRUPT,          STATUS_ANY, False),
    (STATUS_VALID,            True,       True),
    (STATUS_REPACK,           False,      True),
    (STATUS_SIGNATURE_OK,     True,       None),
    (STATUS_SIGNATURE_FAILED, False,      None),
    (STATUS_UNVERIFIED,       None,       None),
)


def status_of(signature_valid, hash_valid):
    """The status label for one file's two verdict columns.

    Takes the raw column values: a row read through the ORM carries booleans, one read
    through raw SQL carries 0/1, and both have to land on the same label.
    """
    signature_valid = None if signature_valid is None else bool(signature_valid)
    hash_valid = None if hash_valid is None else bool(hash_valid)
    for status, want_signature, want_hash in STATUS_RULES:
        if want_hash is hash_valid and want_signature in (STATUS_ANY, signature_valid):
            return status
    # Only reachable if a verdict were written without its signature column, which the
    # verify task never does - hash depth always records both.
    return STATUS_UNVERIFIED

# Lines nstools emits for a content that failed. Everything else it prints is progress
# narration, and a whole run's log is far too long to put in a DB column. 'MODIFIED'
# is matched bare because the two phases disagree on case: verify_sig writes
# '-> was MODIFIED', verify_hash '> FILE WAS MODIFIED'.
_FAILURES = ('<<<-', 'MODIFIED', 'FILE IS CORRUPT')
_MAX_ERROR = 2000

# nstools prints its whole log with bare print(), which nsz's Print.silent does not reach.
# Shadowing the module's builtin is targeted; redirect_stdout would swap sys.stdout for
# every thread in the worker. Nothing is lost - the same text comes back in the vmsg list.
Verify.print = lambda *args, **kwargs: None


@contextmanager
def _progress_bars(progress, filepath):
    """Report hashing progress through nstools' hardcoded per-NCA enlighten bar.

    Verify.pb_Counter is a module-level import, so swapping it is the only hook available.
    The bar counts MiB within one NCA and a fresh one is built per content, so whole-file
    percent needs the finished ones accumulated. For an NSZ the counter walks the
    *decompressed* bytes, which overshoot the file size, hence the clamp.
    """
    if progress is None:
        yield
        return
    total = max(1, os.path.getsize(filepath) >> 20)
    state = {'done': 0, 'pct': -1}

    class Bar:
        def __init__(self, total=0, **kwargs):
            self.count = 0

        def refresh(self):
            # Called every 64 KiB; only a percent change is worth a database write.
            pct = min(99, int(100 * (state['done'] + self.count) / total))
            if pct != state['pct']:
                state['pct'] = pct
                progress(pct)

        def close(self):
            state['done'] += self.count

    original = Verify.pb_Counter
    Verify.pb_Counter = Bar
    try:
        yield
    finally:
        Verify.pb_Counter = original


def _error_from(messages):
    """The failing lines of an nstools run, or None when it found nothing wrong."""
    failed = [line.strip() for msg in messages for line in msg.splitlines()
              if any(marker in line for marker in _FAILURES)]
    return '; '.join(failed)[:_MAX_ERROR] if failed else None


def verify(filepath, depth, progress=None):
    """Verify one container. Returns (signature_valid, hash_valid, error).

    hash_valid is None when the depth did not ask for it. A phase that raises rather than
    returning a verdict counts as invalid, with the exception text as the error: a file we
    cannot read is not a file we can vouch for.
    """
    from settings import ensure_keys
    ensure_keys('verify')

    messages = []
    try:
        with open_container(filepath) as container:
            ok_decrypt, messages = Verify.verify_decrypt(container, messages)
            ok_signature, headers, messages = Verify.verify_sig(container, messages)
            signature_valid = bool(ok_decrypt) and bool(ok_signature)
            hash_valid = None
            if depth == DEPTH_HASH:
                with _progress_bars(progress, filepath):
                    ok_hash, messages = Verify.verify_hash(container, headers, messages)
                hash_valid = bool(ok_hash)
    except Exception as e:
        logger.warning(f'Verification of {os.path.basename(filepath)} failed: {e}')
        return False, (False if depth == DEPTH_HASH else None), str(e)

    return signature_valid, hash_valid, _error_from(messages)
