"""nstools verification wrapper.

Owns all nstools interaction. Verification is cumulative: the signature test needs the
container decrypted, and the hash test needs the header list the signature test builds
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

# One label for the verdicts `verify` returns. Derived on read, never stored.
STATUS_UNVERIFIED = 'UNVERIFIED'
STATUS_VALID = 'VALID'
STATUS_REPACK = 'REPACK'
STATUS_MODIFIED = 'MODIFIED'
STATUS_CORRUPT = 'CORRUPT'
STATUS_SIGNATURE_OK = 'SIGNATURE_OK'
STATUS_SIGNATURE_FAILED = 'SIGNATURE_FAILED'

# Matches any value including NULL, for the rules where a column does not discriminate.
STATUS_ANY = object()

# (status, signature_valid, hash_valid, hash_modified), first match wins. Order is load
# bearing only between MODIFIED and CORRUPT: CORRUPT is the catch-all for a failed hash,
# so a row written before hash_modified existed keeps the verdict it was given.
STATUS_RULES = (
    (STATUS_MODIFIED,         STATUS_ANY, False, True),
    (STATUS_CORRUPT,          STATUS_ANY, False, STATUS_ANY),
    (STATUS_VALID,            True,       True,  STATUS_ANY),
    (STATUS_REPACK,           False,      True,  STATUS_ANY),
    (STATUS_SIGNATURE_OK,     True,       None,  STATUS_ANY),
    (STATUS_SIGNATURE_FAILED, False,      None,  STATUS_ANY),
    (STATUS_UNVERIFIED,       None,       None,  STATUS_ANY),
)


def status_of(signature_valid, hash_valid, hash_modified):
    """The status label for one file's verdict columns.

    Takes the raw column values: a row read through the ORM carries booleans, one read
    through raw SQL carries 0/1, and both have to land on the same label.
    """
    verdicts = [None if v is None else bool(v)
                for v in (signature_valid, hash_valid, hash_modified)]
    for status, *wanted in STATUS_RULES:
        if all(want is STATUS_ANY or want is got for want, got in zip(wanted, verdicts)):
            return status
    # Only reachable if a verdict were written without its signature column, which the
    # verify task never does - hash depth always records both.
    return STATUS_UNVERIFIED

# Lines nstools emits for a content that failed.
_FAILURES = ('<<<-', 'MODIFIED', 'FILE IS CORRUPT')
_MAX_ERROR = 2000

# nstools decides CORRECT / MODIFIED / CORRUPT per content.
_MODIFIED_VERDICT = 'FILE WAS MODIFIED'

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


def _modified_from(messages):
    """Whether the hash test failed because the file was repacked rather than damaged."""
    return any(line.startswith('VERDICT:') and _MODIFIED_VERDICT in line
               for msg in messages for line in msg.splitlines())


def verify(filepath, depth, progress=None):
    """Verify one container. Returns (signature_valid, hash_valid, hash_modified, error).

    hash_valid and hash_modified are None when the depth did not ask for the hash test.
    hash_modified splits a failed hash test in two: True means the failing contents are
    still filed under the names the container's own CNMT records, so they were rewritten
    in place rather than damaged or swapped.
    """
    from settings import ensure_keys
    ensure_keys('verify')

    messages = []
    try:
        with open_container(filepath) as container:
            ok_decrypt, messages = Verify.verify_decrypt(container, messages)
            ok_signature, headers, messages = Verify.verify_sig(container, messages)
            signature_valid = bool(ok_decrypt) and bool(ok_signature)
            hash_valid = hash_modified = None
            if depth == DEPTH_HASH:
                with _progress_bars(progress, filepath):
                    ok_hash, messages = Verify.verify_hash(container, headers, messages)
                hash_valid = bool(ok_hash)
                hash_modified = _modified_from(messages)
    except Exception as e:
        logger.warning(f'Verification of {os.path.basename(filepath)} failed: {e}')
        # A raised phase is a verdict, not an absent result. At hash depth both hash columns
        # have to be written: a null hash_modified next to a failed hash is how the verify
        # stage recognises a row from before that column existed and schedules a re-check,
        # so leaving it null here would re-verify an unreadable file on every sweep.
        failed = False if depth == DEPTH_HASH else None
        return False, failed, failed, str(e)

    return signature_valid, hash_valid, hash_modified, _error_from(messages)
