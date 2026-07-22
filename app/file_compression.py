"""nsz compression/decompression wrapper.

Owns all nsz interaction. Compression writes into a caller-provided working
directory and verifies the result before returning its path; the caller
finalizes (updates the database and removes the source).
"""
import logging
import threading
from hashlib import sha256 as _sha256
from pathlib import Path
from multiprocessing import cpu_count

import nsz
from nsz import Decompressor as _nsz_decompressor
from nsz.Fs import Nsp as _Nsp, Xci as _Xci
from nsz.nut import Keys, Print as _nsz_print
from nsz.Decompressor import VerificationException

# nsz's in-memory NCZ->NCA reconstruction (returns the reconstructed NCA's sha256).
# Module-level dunder name, reached by getattr. Reused for verification below; the
# re-encryption keys live in the NCZSECTN header, so it needs neither disk nor keys.txt.
_decompress_ncz = getattr(_nsz_decompressor, '__decompressNcz')

from constants import COMPRESS_EXT, DECOMPRESS_EXT

logger = logging.getLogger('main')

POLL_INTERVAL = 2.0   # seconds between statusReport polls
COMPRESS_SPAN = 90    # compress fills 0..COMPRESS_SPAN, verify the rest (empirically ~90/10 of wall time)

# suppress nsz's logs and progress bars
_nsz_print.enableInfo = False
_nsz_print.minimalOutput = True
_nsz_print.progress = lambda *a, **k: None


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
                if pct > last:  # ignore nsz's per-NCA resets; keep it monotonic
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
    mode = opts.get('mode', 'auto')
    if mode == 'block':
        return True
    if mode == 'solid':
        return False
    return ext == 'xci'


def _nca_content_hashes(path, sri=None):
    """Map each NCA's identity (name minus .nca/.ncz) to the sha256 of its *content*.

    Compressed .ncz members are decompressed in memory (no temp file, no keys) and
    the reconstructed NCA is hashed; plain .nca members are hashed as-is. A source and
    a faithful compression of it yield identical maps — independent of container padding
    and of whether an NCA matches its own name-hash.
    """
    path = Path(path)
    is_xci = path.suffix.lower() in ('.xci', '.xcz')
    container = (_Xci.Xci() if is_xci else _Nsp.Nsp())
    container.open(str(path), 'rb')
    members = (f for part in container.hfs0 for f in part) if is_xci else iter(container)
    hashes = {}
    try:
        for f in members:
            if f._path.endswith('.ncz'):
                _, hexhash = _decompress_ncz(f, None, sri, None)
            elif f._path.endswith('.nca'):
                h = _sha256()
                f.seek(0)
                while not f.eof():
                    h.update(f.read(0x100000))
                hexhash = h.hexdigest()
            else:
                continue
            hashes[f._path[:-4]] = hexhash
    finally:
        container.close()
    return hashes


def _verify_roundtrip(source, out, progress):
    """Confirm compression didn't corrupt content: every NCA must decompress back to
    the exact bytes of its source counterpart. Raises VerificationException otherwise."""
    src = _nca_content_hashes(source)
    got = _with_progress(lambda sri: _nca_content_hashes(out, sri), progress, COMPRESS_SPAN, 100 - COMPRESS_SPAN)
    corrupted = sorted(k for k in src if k in got and got[k] != src[k])
    missing = sorted(k for k in src if k not in got)
    unexpected = sorted(k for k in got if k not in src)
    if corrupted or missing or unexpected:
        raise VerificationException(
            f'Round-trip verification failed for {source.name}: '
            f'{len(corrupted)} corrupted, {len(missing)} missing, {len(unexpected)} unexpected NCA(s)')


def compress_to(source, out_dir, opts, progress=None):
    """Compress source into out_dir, verify it round-trips to the source, and return
    the compressed file path.

    Raises VerificationException (or RuntimeError) on failure, leaving source untouched.
    Compression uses keep=True so every NCA is bit-identically restorable; verification
    then decompresses each NCA and SHA256-compares its content against the matching
    source NCA (_verify_roundtrip). This proves compression didn't corrupt anything
    without requiring whole-container byte-identity, which nsz can't reproduce for some
    valid containers (padding normalisation), nor that source NCAs match their own names.

    `progress(pct)` is called with overall percent: compress fills 0..COMPRESS_SPAN, verify the rest.
    """
    _ensure_keys()
    source = Path(source).resolve()
    out_dir = Path(out_dir)
    ext = source.suffix.lstrip('.').lower()
    level = int(opts.get('level', 18))
    long_mode = bool(opts.get('long_distance', False))
    threads = int(opts.get('threads', 0)) or -1
    use_block = _use_block(ext, opts)

    def _solid(sri):
        report, key = sri if sri else ({}, 0)
        solid_threads = threads if threads > 0 else 3
        return nsz.solidCompress(source, level, True, False, long_mode, out_dir, solid_threads, report, key, None)

    def _block(sri):
        report, key = sri if sri else ({}, 0)
        bs = int(opts.get('block_size_exponent', 20))
        block_threads = threads if threads > 0 else cpu_count()
        return nsz.blockCompress(source, level, True, False, long_mode, bs, out_dir, block_threads, report, key)

    out = Path(_with_progress(_block if use_block else _solid, progress, 0, COMPRESS_SPAN))
    if not out.is_file():
        raise RuntimeError(f'Compression produced no output for {source.name}')

    try:
        logger.info(f'Verifying compressed file (NCA round-trip): {out.name}')
        _verify_roundtrip(source, out, progress)
    except BaseException:
        out.unlink(missing_ok=True)  # never leave an unverified/partial output behind
        raise
    return out


def decompress_to(source, out_dir, progress=None):
    """Decompress source into out_dir and return the decompressed file path."""
    _ensure_keys()
    source = Path(source).resolve()
    out_dir = Path(out_dir)
    out = out_dir / decompressed_path(source).name
    try:
        _with_progress(lambda sri: nsz.decompress(source, out_dir, False, sri), progress, 0, 100)
    except BaseException:
        out.unlink(missing_ok=True)  # never leave a partial output behind
        raise
    if not out.is_file():
        raise RuntimeError(f'Decompression produced no output for {source.name}')
    return out
