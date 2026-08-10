"""Everything that reads or writes a game container (NSP/NSZ/XCI/XCZ).

Only `open_container` is re-exported: `compression` and `verification` pull in nstools and
nsz.Decompressor, which the identify path has no use for. Import those as submodules.
"""
from .container import open_container

__all__ = ["open_container"]
