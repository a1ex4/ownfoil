"""Access rules on /api/media (API spec section 4.2).

Artwork the catalogue points at is part of the catalogue: it used to be gated on a
Flask-Login session alone, so a browser could load it and no API client could - on a
public shop either, leaving every local Image broken for a Switch client. These tests
pin that it now follows the shop's own public/private setting and takes Basic Auth,
without losing the browser session path the web UI uses.
"""
import base64
import io
import os

import pytest
from PIL import Image

import media
from capture.fixture import (PASSWORDS, UNKNOWN_USER, WRONG_PASSWORD,
                             seed_admin_without_shop_access)
from settings import set_shop_settings

TITLE_ID = "0100000000010000"
FILENAME = "icon.jpg"
URL = f"/api/media/{TITLE_ID}/{media.ICON}/0/{media.CLIENT}/{FILENAME}"

# label, public shop, user, password (None: the account's own), expected status
ACCESS = [
    ("private, no credentials", False, None, None, 401),
    ("private, unknown user", False, UNKNOWN_USER, "ghostpass1", 401),
    ("private, wrong password", False, "shopper", WRONG_PASSWORD, 401),
    ("private, no shop access", False, "noshop", None, 403),
    ("private, shop access", False, "shopper", None, 200),
    ("private, admin", False, "admin", None, 200),
    ("public, no credentials", True, None, None, 200),
    ("public, no shop access", True, "noshop", None, 200),
    ("public, shop access", True, "shopper", None, 200),
]

# label, the account whose session cookie is held, expected status
SESSION_ACCESS = [
    ("shop access", "shopper", 200),
    ("admin", "admin", 200),
    ("no shop access", "noshop", 403),
]


@pytest.fixture
def stored(shop_app, tmp_path, monkeypatch):
    """The fixture shop with one stored icon, and the bytes it should hand back."""
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path / "media"))
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), "red").save(buffer, format="JPEG")

    directory = media.media_dir(media.ICON, media.CLIENT)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, FILENAME), "wb") as f:
        f.write(buffer.getvalue())

    shop_app.icon = buffer.getvalue()
    return shop_app


def basic(user, password=None):
    credentials = f"{user}:{password or PASSWORDS[user]}".encode()
    return {"Authorization": "Basic " + base64.b64encode(credentials).decode()}


def logged_in_client(shop, user):
    """A fresh client holding `user`'s session cookie - the way the web UI arrives."""
    client = shop.app.test_client()
    client.post("/login", data={"user": user, "password": PASSWORDS[user]})
    return client


@pytest.mark.parametrize("public, user, password, status",
                         [case[1:] for case in ACCESS], ids=[case[0] for case in ACCESS])
def test_shop_access_gating(stored, public, user, password, status):
    """Same rules as browsing: anonymous on a public shop, shop access otherwise."""
    set_shop_settings({"public": public})
    response = stored.client.get(URL, headers=basic(user, password) if user else {})

    assert response.status_code == status
    if status == 200:
        assert response.data == stored.icon


@pytest.mark.parametrize("user, status",
                         [case[1:] for case in SESSION_ACCESS],
                         ids=[case[0] for case in SESSION_ACCESS])
def test_a_session_cookie_is_still_a_way_in(stored, user, status):
    """The web UI loads these URLs from img tags, which carry a cookie and nothing else."""
    response = logged_in_client(stored, user).get(URL)

    assert response.status_code == status


def test_an_admin_without_shop_access_still_gets_in(stored):
    """The route named both accesses before this gate existed, and the admin pages that
    show artwork still expect the pair."""
    user, password = seed_admin_without_shop_access(stored.app)

    response = stored.client.get(URL, headers=basic(user, password))

    assert response.status_code == 200


def test_an_anonymous_caller_is_told_how_to_authenticate(stored):
    """A client that did not send credentials gets the challenge that says to."""
    response = stored.client.get(URL)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="Ownfoil"'


# public shop, the directive the response carries, the one it must not, the Vary it needs.
# `Cookie` is Flask-Login's own doing on both; what a private shop adds is `Authorization`,
# without which a cache could answer one caller's credentials with another's response.
CACHING = [
    ("private shop", False, "private", "public", "Authorization, Cookie"),
    ("public shop", True, "public", "private", "Cookie"),
]


@pytest.mark.parametrize("public, present, absent, vary",
                         [case[1:] for case in CACHING], ids=[case[0] for case in CACHING])
def test_caching_is_shareable_only_on_a_public_shop(stored, public, present, absent, vary):
    """Filenames are content hashes, so the year of cache holds either way - but a private
    shop's artwork must not sit in a cache anyone else can read."""
    set_shop_settings({"public": public})
    response = stored.client.get(URL, headers=basic("shopper"))

    assert getattr(response.cache_control, present)
    assert not getattr(response.cache_control, absent)
    assert response.cache_control.max_age == 31536000
    assert response.headers.get("Vary") == vary


def test_an_unknown_rendition_is_not_found(stored):
    """A bad kind or size is a 404 and never a path - but only once past the gate, so a
    stranger cannot use the difference to tell what the store holds."""
    url = f"/api/media/{TITLE_ID}/{media.ICON}/0/enormous/{FILENAME}"

    assert stored.client.get(url, headers=basic("shopper")).status_code == 404
    assert stored.client.get(url).status_code == 401


def test_a_rendition_the_store_lacks_is_built_from_its_original(stored):
    """A store written before a rendition existed, or before its box changed, holds only the
    original: the first request builds the rest rather than being turned away."""
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1024), "red").save(buffer, format="JPEG")
    directory = media.media_dir(media.ICON, media.ORIGINAL)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "older.jpg"), "wb") as f:
        f.write(buffer.getvalue())

    url = f"/api/media/{TITLE_ID}/{media.ICON}/0/{media.CLIENT}/older.jpg"
    response = stored.client.get(url, headers=basic("shopper"))

    assert response.status_code == 200
    with Image.open(io.BytesIO(response.data)) as image:
        assert image.size == (256, 256)


def test_a_rendition_with_no_original_is_not_found(stored):
    url = f"/api/media/{TITLE_ID}/{media.ICON}/0/{media.CLIENT}/absent.jpg"

    assert stored.client.get(url, headers=basic("shopper")).status_code == 404
