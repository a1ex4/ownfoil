"""Rebuild client artwork

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2

The client rendition moved to a 720x405 box at quality 85, the size a title's own page draws
its big image at. Every file already stored was fitted to the old 640x360 box at 90, under
the same URL, so they are deleted here: the next request for each builds it again from the
stored original (`media.build`), and nothing is downloaded. The schema is untouched - the
media table records only the original's size, which is all a rendition's follows from.

The kinds and the directory are spelled out rather than read from `media`, which describes
the store as the code of the day has it, not as it was when this ran.
"""
import os
import shutil

from constants import MEDIA_DIR


revision = 'f8a9b0c1d2e3'
down_revision = 'e7f8a9b0c1d2'
branch_labels = None
depends_on = None


def upgrade():
    for kind in ('icon', 'banner', 'screenshot', 'boxart'):
        shutil.rmtree(os.path.join(MEDIA_DIR, kind, 'client'), ignore_errors=True)


def downgrade():
    # Nothing to restore: the older code rebuilds its own renditions the same way.
    pass
