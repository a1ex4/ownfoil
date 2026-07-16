"""nsz compression/decompression wrapper.

Owns all nsz interaction. Compression writes into a caller-provided working
directory and verifies the result before returning; the caller is responsible
for atomically moving the verified file into place and updating the database.
"""
import logging
import threading
from pathlib import Path
from multiprocessing import cpu_count

import nsz
import enlighten as _enlighten
from nsz.nut import Keys, Print as _nsz_print
from nsz.NszDecompressor import VerificationException

from constants import COMPRESS_EXT, DECOMPRESS_EXT

logger = logging.getLogger('main')

# nsz surfaces progress two ways: an enlighten terminal bar, or a shared statusReport dict.
# The bar writes cursor-control escapes that corrupt the app log (worse with parallel workers),
# so we suppress it and poll the statusReport instead — feeding progress to the task row.
POLL_INTERVAL = 1.0


class _NoBar:
    """No-op stand-in for enlighten.Counter: progress is reported via statusReport, not a bar."""
    def __init__(self, *a, **k): pass
    def update(self, *a, **k): pass
    def refresh(self, *a, **k): pass
    def close(self, *a, **k): pass


_enlighten.Counter = _NoBar          # kills every bar, incl. blockCompress (no statusReport hook)
_nsz_print.enableInfo = False        # silence nsz's [OPEN]/[ADDING]/[VERIFIED]/... chatter (errors stay)


def _with_progress(run, progress, base, span):
    """Run one nsz phase, mapping its statusReport to base..base+span percent via a poller thread.

    `run` takes a statusReportInfo (report_dict, key) tuple (or None) and calls the nsz function
    with it. While it blocks, a daemon thread reads report[key] = [current, _, total, phase] and
    reports monotonically increasing integer percent through `progress`. No thread when progress
    is None. The phase-end percent is forced on success so the row always reaches base+span.
    """
    if progress is None:
        return run(None)
    report, key = {}, 0
    stop = threading.Event()

    def poll():
        last = -1
        while not stop.is_set():
            entry = report.get(key)
            if entry and entry[2]:
                pct = int(base + span * min(1.0, entry[0] / entry[2]))
                if pct > last:  # ignore nsz's per-NCA resets; keep the bar monotonic
                    last = pct
                    progress(pct)
            stop.wait(POLL_INTERVAL)

    t = threading.Thread(target=poll, name='nsz-progress', daemon=True)
    t.start()
    try:
        result = run((report, key))
    finally:
        stop.set()
        t.join(timeout=2)
    progress(int(base + span))
    return result


def _ensure_keys():
    """nsz reads decryption keys from the module-global Keys state; reuse the
    app's own loader instead of nsz's ~/.switch default search."""
    if not Keys.keys_loaded:
        from settings import load_keys
        load_keys()
    if not Keys.keys_loaded:
        raise RuntimeError('Cannot compress: no valid keys loaded.')


def compressed_path(source):
    """Final path a compressed source will occupy (same dir, mapped extension)."""
    source = Path(source)
    return source.with_suffix('.' + COMPRESS_EXT[source.suffix.lstrip('.').lower()])


def decompressed_path(source):
    source = Path(source)
    return source.with_suffix('.' + DECOMPRESS_EXT[source.suffix.lstrip('.').lower()])


def _use_block(ext, opts):
    """Solid vs block selection, mirroring nsz's own default (block for XCI)."""
    mode = opts.get('solid', 'auto')
    if mode == 'block':
        return True
    if mode == 'solid':
        return False
    return ext == 'xci'


def compress_to(source, out_dir, opts, progress=None):
    """Compress source into out_dir, verify bit-identical against the source, and
    return the compressed file path.

    Raises VerificationException (or RuntimeError) on failure, leaving source
    untouched. Compression uses keep=True so the compressed file is bit-identically
    restorable; verification then fully reconstructs it and SHA256-compares against
    the still-present original (nsz's full --verify), catching any divergence from
    the actual source bytes rather than just re-checking each NCA's own hash header.

    `progress(pct)` is called with overall percent: compress fills 0-50, verify 50-100.
    """
    _ensure_keys()
    source = Path(source).resolve()
    out_dir = Path(out_dir)
    ext = source.suffix.lstrip('.').lower()
    level = int(opts.get('level', 18))
    long_mode = bool(opts.get('long_distance', False))
    threads = int(opts.get('threads', 0)) or -1
    use_block = _use_block(ext, opts)

    def _compress(sri):
        if use_block:
            # blockCompress has no statusReport hook: no live percent, but its bar is suppressed.
            bs = int(opts.get('block_size_exponent', 20))
            block_threads = threads if threads > 0 else cpu_count()
            return nsz.blockCompress(source, level, True, False, long_mode, bs, out_dir, block_threads)
        report, key = sri if sri else ({}, 0)
        solid_threads = threads if threads > 0 else 3
        return nsz.solidCompress(source, level, True, False, long_mode, out_dir, solid_threads, report, key, None)

    out = Path(_with_progress(_compress, progress, 0, 50))
    if not out.is_file():
        raise RuntimeError(f'Compression produced no output for {source.name}')

    logger.info(f'Verifying compressed file (bit-identical): {out.name}')
    _with_progress(lambda sri: nsz.verify(out, False, True, True, source, sri, None), progress, 50, 50)
    return out


def decompress_to(source, out_dir, progress=None):
    """Decompress source into out_dir and return the decompressed file path."""
    _ensure_keys()
    source = Path(source).resolve()
    out_dir = Path(out_dir)
    _with_progress(lambda sri: nsz.decompress(source, out_dir, False, sri), progress, 0, 100)
    out = out_dir / decompressed_path(source).name
    if not out.is_file():
        raise RuntimeError(f'Decompression produced no output for {source.name}')
    return out
