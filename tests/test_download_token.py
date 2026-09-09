"""Token downloads: /api/download/<token>, the URL the catalogue hands out.

The point of the token is that it is not the primary key: /api/get_game/<id> can be
walked from 1 upwards, and on a public shop that needs no credentials at all. Everything
else - the bytes, the gate, the counting - has to stay identical to the old route.
"""
import pytest

from db import Files
from settings import set_shop_settings

FILENAME = "Test Game [0100000000010000][v0].nsp"


@pytest.fixture
def shop(shop_app):
    set_shop_settings({"public": True})
    return shop_app


def file_row(shop, filename=FILENAME):
    with shop.app.app_context():
        row = Files.query.filter_by(filename=filename).first()
        return row.id, row.download_token, row.filepath


def test_every_file_gets_a_token(shop):
    with shop.app.app_context():
        tokens = [f.download_token for f in Files.query.all()]
    assert all(tokens) and len(set(tokens)) == len(tokens)


def test_a_token_is_not_derived_from_the_row(shop):
    """A token built from the id or the path would be as guessable as the id it replaces."""
    id_, token, filepath = file_row(shop)
    assert str(id_) not in token
    assert FILENAME.rsplit(".", 1)[0] not in token
    assert len(token) >= 16


def test_the_token_route_serves_the_same_bytes(shop):
    id_, token, _ = file_row(shop)
    by_token = shop.client.get(f"/api/download/{token}")
    by_id = shop.client.get(f"/api/get_game/{id_}")
    assert by_token.status_code == 200
    assert by_token.data == by_id.data


def test_an_unknown_token_is_404(shop):
    assert shop.client.get("/api/download/not-a-real-token").status_code == 404


def test_a_range_request_is_served(shop):
    """send_from_directory carries Range support over from the id route."""
    _, token, _ = file_row(shop)
    response = shop.client.get(f"/api/download/{token}", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert len(response.data) == 4


def test_a_download_is_counted(shop):
    _, token, _ = file_row(shop)
    assert shop.client.get(f"/api/download/{token}").status_code == 200
    with shop.app.app_context():
        assert Files.query.filter_by(filename=FILENAME).first().download_count == 1


def test_a_private_shop_still_needs_credentials(shop):
    """The token is not a bearer credential: it opens nothing the id route would refuse."""
    _, token, _ = file_row(shop)
    set_shop_settings({"public": False})
    assert shop.client.get(f"/api/download/{token}").status_code == 401
