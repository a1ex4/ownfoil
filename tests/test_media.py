"""Tests for the local artwork store.

What is asserted is what a caller gets back - the name a file is stored under, that both
renditions exist and how big they are - never how the resize was done.
"""
import io
import os
import time

import pytest
from PIL import Image, ImageDraw

import media


def jpeg(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path / "media"))
    return tmp_path / "media"


# --- naming ---

# (source URL, the filename it is stored under)
URL_CASES = [
    ("https://img-eshop.cdn.nintendo.net/i/abc123.jpg", "abc123.jpg"),
    ("https://example.test/art/banner.png", "banner.png"),
    ("https://example.test/art/banner.png?w=1280", "banner.png"),
]


@pytest.mark.parametrize("url,expected", URL_CASES)
def test_filename_is_kept_from_the_url(url, expected):
    assert media.filename_from_url(url) == expected


# A basename that would escape the store, or name nothing usable, has to be replaced
# rather than sanitized in place - a collision between two such URLs would serve the
# wrong image.
UNUSABLE_URLS = [
    "https://example.test/../../etc/passwd.jpg",
    "https://example.test/art/",
    "https://example.test/%2e%2e/x.jpg",
]


@pytest.mark.parametrize("url", UNUSABLE_URLS)
def test_an_unusable_basename_falls_back_to_a_hash(url):
    name = media.filename_from_url(url)
    assert os.path.basename(name) == name
    assert media._SAFE_FILENAME.match(name)
    assert media.filename_from_url(url) == name


# --- storing ---

# (kind, source size, the client size it is fitted to)
RESIZE_CASES = [
    (media.ICON, (1024, 1024), (256, 256)),
    (media.BANNER, (1280, 720), (640, 360)),
    (media.SCREENSHOT, (1280, 720), (640, 360)),
    (media.BOXART, (600, 900), (240, 360)),
    # Already inside the box: thumbnail() never enlarges, so this is served unchanged.
    (media.ICON, (64, 64), (64, 64)),
]


@pytest.mark.parametrize("kind,source,expected", RESIZE_CASES)
def test_both_renditions_exist_and_the_client_one_fits_its_box(store, kind, source, expected):
    filename, size, client_size = media.store_bytes(kind, jpeg(*source), "art.jpg")

    assert filename == "art.jpg"
    assert size == source
    assert client_size == expected
    for rendition, dims in ((media.ORIGINAL, source), (media.CLIENT, expected)):
        path = store / kind / rendition / "art.jpg"
        with Image.open(path) as image:
            assert image.size == dims


def test_the_client_rendition_only_loses_resolution(store):
    """Store artwork is saturated logo edges, which a default JPEG re-encode smears.

    Asserted against the resize alone, so it holds for whatever encoder settings get it
    there: with Pillow's defaults - quality 75 and 4:2:0 chroma - the worst channel is out
    by 70 of 255 on this source, which is the banding that shows up on a card.
    """
    source = Image.new("RGB", (1024, 1024), "red")
    draw = ImageDraw.Draw(source)
    for x in range(0, 1024, 32):
        draw.rectangle([x, 0, x + 15, 1024], fill="blue")
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", quality=95, subsampling=0)

    media.store_bytes(media.ICON, buffer.getvalue(), "art.jpg")

    expected = source.copy()
    expected.thumbnail((256, 256), Image.LANCZOS, reducing_gap=None)
    with Image.open(store / media.ICON / media.CLIENT / "art.jpg") as client:
        client.load()
        error = [abs(a - b) for a, b in zip(expected.tobytes(), client.tobytes())]
    assert max(error) < 24
    assert sum(error) / len(error) < 4


def test_have_is_true_only_once_both_renditions_are_written(store):
    assert not media.have(media.ICON, "art.jpg")
    media.store_bytes(media.ICON, jpeg(64, 64), "art.jpg")
    assert media.have(media.ICON, "art.jpg")

    os.remove(store / media.ICON / media.CLIENT / "art.jpg")
    assert not media.have(media.ICON, "art.jpg")


def test_store_bytes_refuses_a_filename_that_would_escape_the_store(store):
    with pytest.raises(ValueError):
        media.store_bytes(media.ICON, jpeg(64, 64), "../escaped.jpg")


def test_an_unknown_kind_or_size_has_no_directory():
    for args in ((("nope", media.CLIENT)), ((media.ICON, "nope"))):
        with pytest.raises(ValueError):
            media.media_dir(*args)


# --- collecting ---

def backdate(directory):
    """Age every stored file past the grace, the way a real sweep only ever sees them."""
    old = time.time() - media.COLLECT_GRACE - 60
    for path in directory.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


def test_collect_removes_both_renditions_of_a_file_no_row_names(store):
    media.store_bytes(media.ICON, jpeg(64, 64), "kept.jpg")
    media.store_bytes(media.ICON, jpeg(48, 48), "dropped.jpg")
    # An interrupted write leaves one of these behind and no row ever names it.
    (store / media.ICON / media.ORIGINAL / "kept.jpg.tmp").write_bytes(b"partial")
    backdate(store)

    removed = media.collect({(media.ICON, "kept.jpg")})

    assert removed == 3
    assert media.have(media.ICON, "kept.jpg")
    assert not media.have(media.ICON, "dropped.jpg")
    assert not (store / media.ICON / media.ORIGINAL / "dropped.jpg").exists()
    assert not (store / media.ICON / media.ORIGINAL / "kept.jpg.tmp").exists()


def test_collect_leaves_a_file_the_grace_still_covers(store):
    """The bytes are written before the row naming them is committed."""
    media.store_bytes(media.ICON, jpeg(64, 64), "fresh.jpg")

    assert media.collect(set()) == 0
    assert media.have(media.ICON, "fresh.jpg")


def test_collect_is_a_no_op_on_a_store_that_was_never_written(store):
    assert media.collect(set()) == 0


# --- URLs ---

def test_the_url_carries_the_slot_and_resolves_to_the_stored_file(store):
    url = media.url_for("0100000000010000", media.SCREENSHOT, 2, media.CLIENT, "art.jpg")

    assert url == "/api/media/0100000000010000/screenshot/2/client/art.jpg"
    assert media.is_local(url)
    assert not media.is_local("https://img-eshop.cdn.nintendo.net/i/abc123.jpg")
    # The last two segments are what the route serves from, and nothing else.
    kind, _position, size, name = url.split("/")[4:]
    assert media.media_path(kind, size, name) == str(
        store / media.SCREENSHOT / media.CLIENT / "art.jpg")


def test_extracted_artwork_is_named_by_its_own_bytes(store, monkeypatch):
    recorded = []
    monkeypatch.setattr("db.upsert_media", lambda *a, **kw: recorded.append((a, kw)))

    one = media.store_extracted("0100000000010000", media.ICON, jpeg(64, 64))
    again = media.store_extracted("0100000000010000", media.ICON, jpeg(64, 64))
    other = media.store_extracted("0100000000010000", media.ICON, jpeg(48, 48))

    assert one == again
    assert other != one
    # A stable, content-keyed URL is what lets the route promise a year of caching.
    assert one.startswith("/api/media/0100000000010000/icon/0/original/")
    assert len(recorded) == 3
