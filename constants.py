"""Compatibility shim for imports.

Some modules import `constants` as a top-level module (when running via
`python app/app.py`, the `app/` directory is on `sys.path`). Unit tests and
other entrypoints import `app.*` from the repo root, where `constants` would
otherwise not resolve.
"""

from app.constants import *  # noqa: F401,F403
