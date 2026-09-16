"""The OPTIONS handshake: server identity and per-caller capabilities (API spec section 3).

Not one of the shop client protocols: it answers on its own, ahead of any client being
identified, so these tests pin both its access rules and that independence.
"""
import base64

import pytest

from constants import API_PROTOCOL_VERSION, APP_VERSION
from discovery import discovery_payload
from settings import get_server_uid, set_shop_settings

# What a caller reaching the handshake can do today: browse, and resume its downloads. The
# rest stay false until the dump upload, save backup and tus endpoints exist.
FEATURES = {
    "shop": True,
    "resumable_download": True,
    "dumps_upload": False,
    "save_backup": False,
    "resumable_upload": False,
}

# The fixture accounts, by what they may do: admin and shopper have shop access, noshop does not.
PASSWORDS = {"admin": "adminpass1", "shopper": "shoppass1", "noshop": "noshoppass1"}

# label, public shop, user, password (None: the account's own), expected status
ACCESS = [
    ("private, no credentials", False, None, None, 401),
    ("private, wrong password", False, "shopper", "wrongpass1", 401),
    ("private, unknown user", False, "ghost", "ghostpass1", 401),
    ("private, no shop access", False, "noshop", None, 403),
    ("private, shop access", False, "shopper", None, 200),
    ("private, admin", False, "admin", None, 200),
    ("public, no credentials", True, None, None, 200),
    ("public, shop access", True, "shopper", None, 200),
    ("public, no shop access", True, "noshop", None, 200),
]


def handshake(shop, user=None, password=None):
    headers = {}
    if user:
        credentials = f"{user}:{password or PASSWORDS[user]}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
    return shop.client.open("/", method="OPTIONS", headers=headers)


@pytest.mark.parametrize("public, user, password, status",
                         [case[1:] for case in ACCESS], ids=[case[0] for case in ACCESS])
def test_shop_access_gating(shop_app, public, user, password, status):
    """The handshake follows the shop's own public/private setting, like browsing does."""
    set_shop_settings({"public": public})
    response = handshake(shop_app, user, password)

    assert response.status_code == status
    if status == 200:
        assert response.json["public"] is public
        # An anonymous caller has no permissions, an authenticated one none that are served
        # yet: both come out of the same computation.
        assert response.json["features"] == FEATURES
    else:
        assert response.json["error"]


def test_identity_comes_from_settings(shop_app):
    set_shop_settings({"public": True, "name": "My Public Shop", "motd": "Welcome!", "host": "shop.example.com"})

    assert handshake(shop_app).json == {
        "uid": get_server_uid(),
        "name": "My Public Shop",
        "version": APP_VERSION,
        "protocol_version": API_PROTOCOL_VERSION,
        "motd": "Welcome!",
        "public": True,
        "remote": "shop.example.com",
        "features": FEATURES,
    }


def test_uid_is_the_one_discovery_answers_with(shop_app):
    """Reached by either route, a client must learn the same id or it saves the shop twice."""
    set_shop_settings({"public": True})

    assert handshake(shop_app).json["uid"] == discovery_payload()["uid"]


def test_refusal_without_credentials_challenges(shop_app):
    set_shop_settings({"public": False})

    assert handshake(shop_app).headers["WWW-Authenticate"].startswith("Basic ")


def test_handshake_does_not_depend_on_the_legacy_clients(shop_app):
    """No shop client speaks this handshake, so disabling every one of them cannot take it down."""
    set_shop_settings({"public": True,
                       "clients": {name: {"enabled": False}
                                   for name in ("tinfoil", "cyberfoil", "sphaira")}})

    assert handshake(shop_app).json["features"] == FEATURES


def test_handshake_is_root_only(shop_app):
    """Section 5 reserves OPTIONS on other paths (tus discovery); only the root handshakes."""
    set_shop_settings({"public": True})
    response = shop_app.client.open("/base", method="OPTIONS")

    assert response.get_json(silent=True) is None
