"""nsz container access — the one place that turns a path into an open Nsp/Xci.

nsz's factory dispatches on the exact-case suffix and hands back an unopened instance, so
every caller had grown its own way past that. Owning the open here also owns the nsz output
globals: they are module state, so they were already process-wide by import order.
"""
import logging
from contextlib import contextmanager
from pathlib import Path

from nsz.Fs import Nsp as _Nsp, Xci as _Xci, Pfs0 as _Pfs0, factory as _factory
from nsz.nut import Print as _nsz_print

logger = logging.getLogger('main')

_nsz_print.enableInfo = False
_nsz_print.minimalOutput = True
_Pfs0.Print.silent = True


@contextmanager
def open_container(filepath, meta_only=False):
    """Open an NSP/NSZ/XCI/XCZ for reading, closed however the caller leaves."""
    path = Path(filepath)
    container = _factory(path.with_suffix(path.suffix.lower()))
    if type(container) not in (_Nsp.Nsp, _Xci.Xci):
        raise ValueError(f'Unsupported container extension: {path.suffix}')
    try:
        container.open(str(filepath), 'rb', meta_only=meta_only)
        yield container
    finally:
        container.flush()
        container.close()
