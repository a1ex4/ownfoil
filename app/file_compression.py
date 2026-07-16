"""nsz compression/decompression wrapper.

Owns all nsz interaction. Compression writes into a caller-provided working
directory and verifies the result before returning; the caller is responsible
for atomically moving the verified file into place and updating the database.
"""
import os
import logging
from pathlib import Path
from multiprocessing import cpu_count

import nsz
from nsz.nut import Keys
from nsz.NszDecompressor import VerificationException

from constants import COMPRESS_EXT, DECOMPRESS_EXT

logger = logging.getLogger('main')


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


def compress_to(source, out_dir, opts):
    """Compress source into out_dir, verify, and return the compressed file path.

    Raises VerificationException (or RuntimeError) on failure, leaving source
    untouched. Verification is NCA-hash based (nsz downgrades full --verify to
    this whenever --keep is off, since dropped NDV0 fragments otherwise read as
    corruption); it proves the content is intact and installable.
    """
    _ensure_keys()
    source = Path(source).resolve()
    out_dir = Path(out_dir)
    ext = source.suffix.lstrip('.').lower()
    level = int(opts.get('level', 18))
    long_mode = bool(opts.get('long_distance', False))
    threads = int(opts.get('threads', 0)) or -1

    if _use_block(ext, opts):
        bs = int(opts.get('block_size_exponent', 20))
        block_threads = threads if threads > 0 else cpu_count()
        out = nsz.blockCompress(source, level, False, False, long_mode, bs, out_dir, block_threads)
    else:
        solid_threads = threads if threads > 0 else 3
        out = nsz.solidCompress(source, level, False, False, long_mode, out_dir, solid_threads, {}, 0, None)

    out = Path(out)
    if not out.is_file():
        raise RuntimeError(f'Compression produced no output for {source.name}')

    logger.info(f'Verifying compressed file: {out.name}')
    nsz.verify(out, False, True, False, None)
    return out


def decompress_to(source, out_dir):
    """Decompress source into out_dir and return the decompressed file path."""
    _ensure_keys()
    source = Path(source).resolve()
    out_dir = Path(out_dir)
    nsz.decompress(source, out_dir, False)
    out = out_dir / decompressed_path(source).name
    if not out.is_file():
        raise RuntimeError(f'Decompression produced no output for {source.name}')
    return out
