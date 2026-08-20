"""Local media storage for artwork ownfoil produces itself, rather than links to.

An icon extracted from a file has no URL to point at, so it gets one here. Lives under the
derived-data directory next to titledb: losing it costs a re-extraction, nothing more.
"""
import hashlib
import os

from constants import ICONS_DIR

ICON_URL_PREFIX = '/api/media/icons'


def icon_filename(title_id):
    return f'{title_id.upper()}.jpg'


def save_icon(title_id, data):
    """Store a title's icon and return the URL to read it back from."""
    os.makedirs(ICONS_DIR, exist_ok=True)
    path = os.path.join(ICONS_DIR, icon_filename(title_id))
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    # The path is stable across re-extractions, so only a content-keyed query string can stop
    # a browser serving the icon of the language the user just switched away from.
    return f'{ICON_URL_PREFIX}/{icon_filename(title_id)}?v={hashlib.sha256(data).hexdigest()[:8]}'
