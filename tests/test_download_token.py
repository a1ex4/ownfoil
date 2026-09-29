"""Token downloads: /api/download/<token>, the URL the catalogue hands out.

The point of the token is that it is not the primary key: an id can be walked from 1
upwards, and on a public shop that needs no credentials at all.
"""
import pytest

from db import Files, db
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
    assert FILENAME.rsplit(".", 1)[0] not in token
    assert len(token) >= 16
    with shop.app.app_context():
        row = Files.query.get(id_)
        library_id = row.library_id
        db.session.delete(row)
        db.session.flush()
        db.session.add(Files(id=id_, library_id=library_id, filepath=filepath, filename=FILENAME))
        db.session.commit()
    assert file_row(shop)[1] != token


def test_the_token_route_serves_the_file(shop):
    _, token, filepath = file_row(shop)
    response = shop.client.get(f"/api/download/{token}")
    assert response.status_code == 200
    with open(filepath, "rb") as f:
        assert response.data == f.read()


def test_an_unknown_token_is_404(shop):
    assert shop.client.get("/api/download/not-a-real-token").status_code == 404


def test_a_range_request_is_served(shop):
    """Clients resume and probe files with a Range, which send_from_directory honours."""
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
