"""Basic Auth on /api/graphql (API spec section 2).

The endpoint used to take a session cookie only, while every shop endpoint authenticates
per request with Basic Auth. Both now work, and these tests pin the property that makes
that safe: the answer a caller gets depends on their permissions, never on how they
proved who they are - including the ETag that decides whether they get a cached one.
"""
import base64

import pytest

from capture.fixture import PASSWORDS, UNKNOWN_USER, WRONG_PASSWORD

# Asks for one figure every caller may see and one only admins may: the same document
# tells the two roles apart.
QUERY = "query Stats { stats { totalApps totalFiles } }"

# label, user, password (None: the account's own), expected status
ACCESS = [
    ("no credentials", None, None, 401),
    ("unknown user", UNKNOWN_USER, "ghostpass1", 401),
    ("wrong password", "shopper", WRONG_PASSWORD, 401),
    ("no shop access", "noshop", None, 403),
    ("shop access", "shopper", None, 200),
    ("admin", "admin", None, 200),
]


def basic(user, password=None):
    credentials = f"{user}:{password or PASSWORDS[user]}".encode()
    return {"Authorization": "Basic " + base64.b64encode(credentials).decode()}


def query(client, headers=None):
    return client.post("/api/graphql", json={"query": QUERY}, headers=headers or {})


def logged_in_client(shop, user):
    """A fresh client holding `user`'s session cookie - the other way into the endpoint."""
    client = shop.app.test_client()
    client.post("/login", data={"user": user, "password": PASSWORDS[user]})
    return client


@pytest.mark.parametrize("user, password, status",
                         [case[1:] for case in ACCESS], ids=[case[0] for case in ACCESS])
def test_basic_auth_gating(shop_app, user, password, status):
    """Basic Auth is a way in, gated exactly like the cookie path was: shop or admin access."""
    response = query(shop_app.client, basic(user, password) if user else None)

    assert response.status_code == status


@pytest.mark.parametrize("user", ["admin", "shopper"])
def test_permissions_do_not_depend_on_the_auth_method(shop_app, user):
    """Same account, both doors: identical data and identical ETag."""
    with_basic = query(shop_app.app.test_client(), basic(user))
    with_cookie = query(logged_in_client(shop_app, user))

    assert with_basic.json == with_cookie.json
    assert with_basic.headers["ETag"] == with_cookie.headers["ETag"]


def test_admin_only_fields_stay_admin_only(shop_app):
    """The two roles really are served different documents, so the ETag has to separate them."""
    as_admin = query(shop_app.app.test_client(), basic("admin")).json["data"]["stats"]
    as_shopper = query(shop_app.app.test_client(), basic("shopper")).json["data"]["stats"]

    assert as_admin["totalApps"] == as_shopper["totalApps"]
    assert as_admin["totalFiles"] > 0
    assert as_shopper["totalFiles"] == 0


def test_cached_responses_do_not_cross_roles(shop_app):
    """A shopper replaying an admin's ETag is answered afresh, not handed the admin's 304."""
    admin_etag = query(shop_app.app.test_client(), basic("admin")).headers["ETag"]
    shopper = shop_app.app.test_client()
    shopper_etag = query(shopper, basic("shopper")).headers["ETag"]

    assert shopper_etag != admin_etag

    stolen = shopper.post("/api/graphql", json={"query": QUERY},
                          headers={**basic("shopper"), "If-None-Match": admin_etag})
    assert stolen.status_code == 200
    assert stolen.json["data"]["stats"]["totalFiles"] == 0

    own = shopper.post("/api/graphql", json={"query": QUERY},
                       headers={**basic("shopper"), "If-None-Match": shopper_etag})
    assert own.status_code == 304
