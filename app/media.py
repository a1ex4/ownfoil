"""Local store for title artwork, whatever its source.

Artwork reaches ownfoil three ways - hotlink URLs in the titledb dump, a user-authored
entry, or an icon read out of a file's own Control NCA - and all three end up here as a
file on disk plus a `media` row naming which title slot it fills.

Files are content-addressed: the stored filename is the source URL's basename, which
titledb already makes a content hash, or a hash of the bytes when there is no URL. Two
titles sharing artwork therefore share one file, a locale switch writes new files beside
the old ones rather than over them, and a stored filename is proof the image is already
retrieved. It also makes every URL this module mints immutable, so they are served with a
year of cache and no revalidation.

Lives under the derived-data directory next to titledb: losing it costs a re-download.
"""
import hashlib
import io
import os
import re
import threading
import time
from urllib.parse import urlparse

import requests
from PIL import Image

from constants import MEDIA_DIR
from titledb.schema import SOURCE_EXTRACT

URL_PREFIX = '/api/media'
TIMEOUT = (10, 60)

ICON = 'icon'
BANNER = 'banner'
SCREENSHOT = 'screenshot'
BOXART = 'boxart'
KINDS = (ICON, BANNER, SCREENSHOT, BOXART)

ORIGINAL = 'original'
CLIENT = 'client'
SIZES = (ORIGINAL, CLIENT)

# Every rendition but the original is a re-encode fitted to a box, as (the box for icons,
# the box for everything else, JPEG quality). Icons are square; everything else is
# landscape store art, which titledb ships at the Switch's own 1280x720 - half of that is
# still sharp on a phone card or a half-width Switch browser at a quarter of the bytes.
RENDITIONS = {
    CLIENT: ((256, 256), (640, 360), 90),
}

# A re-encode needs its encoder spelled out: Pillow defaults to quality 75 and 4:2:0 chroma,
# and halving the chroma planes smears exactly the saturated logo edges store artwork is
# made of. Full chroma keeps a downscale looking like a downscale.
_JPEG_OPTIONS = {'subsampling': 0, 'optimize': True}

# How long a stored file is left alone regardless of what names it. The bytes land before the
# row naming them is committed, so without this a sweep running in between would collect an
# image that is about to be referenced, costing a re-download.
COLLECT_GRACE = 3600

_SAFE_FILENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_LOCAL_SLOT = re.compile(URL_PREFIX + r'/[^/]+/([^/]+)/[^/]+/[^/]+/([A-Za-z0-9][A-Za-z0-9._-]*)')


def media_dir(kind, size):
    """Directory holding one rendition of one kind. Raises on anything unknown."""
    if kind not in KINDS:
        raise ValueError(f'Unknown media kind: {kind}')
    if size not in SIZES:
        raise ValueError(f'Unknown media size: {size}')
    return os.path.join(MEDIA_DIR, kind, size)


def media_path(kind, size, filename):
    return os.path.join(media_dir(kind, size), filename)


def box(kind, size):
    """The box one rendition of one kind of artwork is fitted into."""
    icon, other, _quality = RENDITIONS[size]
    return icon if kind == ICON else other


def fit(dimensions, box):
    """The size an image of `dimensions` is stored at in `box`: the aspect ratio kept, and
    never enlarged, so an original already inside the box keeps its own size."""
    width, height = dimensions
    scale = min(box[0] / width, box[1] / height)
    if scale >= 1:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))


def url_for(title_id, kind, position, size, filename):
    """URL a client reads one rendition back from.

    The title and slot are in the path for legibility in logs and network panels, and to
    leave room for a per-title access check later; the file is found by kind/size/filename
    alone, so serving stays a static send with no database read.
    """
    return f'{URL_PREFIX}/{title_id.upper()}/{kind}/{position}/{size}/{filename}'


def filename_from_url(url):
    """The basename to store a URL's image under, or a hash of the URL if it has none usable."""
    name = os.path.basename(urlparse(url).path)
    if _SAFE_FILENAME.match(name) and '.' in name:
        return name
    ext = os.path.splitext(name)[1]
    if not _SAFE_FILENAME.match(ext.lstrip('.') or 'x'):
        ext = ''
    return hashlib.sha256(url.encode()).hexdigest()[:32] + (ext or '.jpg')


def is_local(url):
    return bool(url) and url.startswith(URL_PREFIX + '/')


def slots_in(text):
    """Every (kind, filename) a local media URL in this text names.

    An override holds its images as URLs, one of which can point back into this store - an
    extracted icon - and as a JSON-encoded list at that, so this reads the text rather than
    trying to tell which column is a URL.
    """
    return set(_LOCAL_SLOT.findall(text)) if isinstance(text, str) else set()


def have(kind, filename):
    """The original is on disk, which is what makes a slot already retrieved."""
    return os.path.isfile(media_path(kind, ORIGINAL, filename))


def dimensions(kind, filename):
    """Size of the stored original, as (w, h). Every rendition's size follows from it."""
    with Image.open(media_path(kind, ORIGINAL, filename)) as image:
        return image.size


def usage():
    """What the store occupies, as {kind: {size: {'bytes': n, 'files': n}}}.

    Read off the filesystem rather than the `media` rows: one file backs every title using
    that image, so the rows count copies the disk does not hold - and a file the last row
    naming it has already retired still costs space until the next sweep.
    """
    totals = {}
    for kind in KINDS:
        totals[kind] = {}
        for size in SIZES:
            used = count = 0
            try:
                with os.scandir(media_dir(kind, size)) as entries:
                    for entry in entries:
                        try:
                            used += entry.stat().st_size
                            count += 1
                        except OSError:
                            pass  # removed under us by a sweep
            except OSError:
                pass  # nothing of this kind stored yet
            totals[kind][size] = {'bytes': used, 'files': count}
    return totals


def collect(keep, grace=COLLECT_GRACE):
    """Delete stored files nothing names. `keep` is the (kind, filename) pairs still in use.

    One file backs every title using that image, so only the whole set of rows can retire one
    - which is why this takes the answer rather than working it out per title. A leftover
    `.tmp` from an interrupted write is in no pair and goes the same way.
    """
    cutoff = time.time() - grace
    removed = 0
    for kind in KINDS:
        for size in SIZES:
            directory = media_dir(kind, size)
            if not os.path.isdir(directory):
                continue
            for filename in os.listdir(directory):
                if (kind, filename) in keep:
                    continue
                try:
                    if os.path.getmtime(os.path.join(directory, filename)) <= cutoff:
                        os.remove(os.path.join(directory, filename))
                        removed += 1
                except OSError:
                    pass  # renamed or removed under us, which is the outcome wanted anyway
    return removed


def fetch(url):
    """Download one image. Raises on anything but a successful response."""
    response = requests.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content


def _render(image, kind, size):
    """One rendition of an opened image, encoded in the format its source came in."""
    fitted = image.resize(fit(image.size, box(kind, size)), Image.LANCZOS)
    options = {'quality': RENDITIONS[size][2], **_JPEG_OPTIONS} if image.format == 'JPEG' else {}
    buffer = io.BytesIO()
    fitted.save(buffer, format=image.format, **options)
    return buffer.getvalue()


def store_bytes(kind, data, filename=None):
    """Write every rendition of an image. Returns (filename, (w, h)), the original's size."""
    if filename is None:
        filename = hashlib.sha256(data).hexdigest()[:32] + '.jpg'
    if not _SAFE_FILENAME.match(filename):
        raise ValueError(f'Unsafe media filename: {filename}')

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        size = image.size
        renditions = {rendition: _render(image, kind, rendition) for rendition in RENDITIONS}

    _write(kind, ORIGINAL, filename, data)
    for rendition, rendered in renditions.items():
        _write(kind, rendition, filename, rendered)
    return filename, size


def build(kind, size, filename):
    """Make sure one rendition is on disk, deriving it from the stored original if it isn't.
    False when there is no original, and for ORIGINAL itself."""
    if size not in RENDITIONS or not _SAFE_FILENAME.match(filename):
        return False
    if os.path.isfile(media_path(kind, size, filename)):
        return True
    if not have(kind, filename):
        return False
    with Image.open(media_path(kind, ORIGINAL, filename)) as image:
        image.load()
        rendered = _render(image, kind, size)
    _write(kind, size, filename, rendered)
    return True


def store_extracted(title_id, kind, data):
    """File artwork that came out of a library file rather than a URL, and return its URL.

    Content-addressed like everything else here, so re-reading the same file writes the
    same name and the URL only changes when the image does.
    """
    from db import upsert_media
    filename, size = store_bytes(kind, data)
    upsert_media(title_id, kind, 0, source=SOURCE_EXTRACT, source_url=None,
                 filename=filename, size=size)
    return url_for(title_id, kind, 0, ORIGINAL, filename)


def _write(kind, size, filename, data):
    directory = media_dir(kind, size)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    # Per-thread temp file, so concurrent builds of one rendition don't share it.
    tmp = f'{path}.{threading.get_ident()}.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
