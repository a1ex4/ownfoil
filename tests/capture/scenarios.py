"""The shop states to capture, and what the operator does to the client in each.

Each scenario is a shop configuration plus one instruction. The server resets the shop to a
known baseline before applying the scenario, so they are independent and any single one can
be recaptured on its own.

Only states that change what the *client* sends are worth an operator's time. Variations
that only change what the server answers - the other content filters, host verification over
HTTPS, an unknown file id - are synthesized in the tests from the headers captured here.
"""
from dataclasses import dataclass, field

from fixture import PASSWORDS, UNKNOWN_USER, WRONG_PASSWORD

ALL_CLIENTS = ("tinfoil", "cyberfoil", "sphaira")

# Applied before every scenario so none of them inherits the previous one's state.
BASELINE = {
    "host": "",
    "public": False,
    "motd": "Ownfoil capture fixture",
    "clients": {
        "cyberfoil": {"enabled": True, "hauth": {}},
        "tinfoil": {"enabled": True, "encrypt": True, "hauth": {}},
        "sphaira": {"enabled": True},
    },
}


@dataclass
class Scenario:
    name: str
    title: str
    instruction: str
    expect: str
    shop: dict = field(default_factory=dict)
    credentials: tuple = None          # (user, password) to configure in the client
    path: str = ""                     # appended to the shop url
    disable_client: bool = False       # disables whichever client is being captured
    clients: tuple = ALL_CLIENTS


SCENARIOS = [
    Scenario(
        name="public-browse",
        title="Public shop, no credentials",
        shop={"public": True},
        instruction="Clear any credentials from the shop entry, then open the shop.",
        expect="Full file listing, no authentication involved.",
    ),
    Scenario(
        name="private-no-credentials",
        title="Private shop, no credentials",
        instruction="Keep the credentials cleared, then open the shop.",
        expect="Refused: shop requires authentication, no credentials provided.",
    ),
    Scenario(
        name="unknown-user",
        title="Private shop, user that does not exist",
        credentials=(UNKNOWN_USER, WRONG_PASSWORD),
        instruction="Set the credentials above, then open the shop.",
        expect="Refused: unknown user.",
    ),
    Scenario(
        name="wrong-password",
        title="Private shop, wrong password",
        credentials=("shopper", WRONG_PASSWORD),
        instruction="Set the credentials above, then open the shop.",
        expect="Refused: incorrect password.",
    ),
    Scenario(
        name="authenticated-browse",
        title="Private shop, valid user with shop access",
        credentials=("shopper", PASSWORDS["shopper"]),
        instruction="Set the credentials above, then open the shop.",
        expect="Full file listing.",
    ),
    Scenario(
        name="no-shop-access",
        title="Private shop, valid user without shop access",
        credentials=("noshop", PASSWORDS["noshop"]),
        instruction="Set the credentials above, then open the shop.",
        expect="Refused: user has no shop access. Distinct from a bad password.",
    ),
    Scenario(
        name="admin-browse",
        title="Private shop, admin account",
        credentials=("admin", PASSWORDS["admin"]),
        instruction="Set the credentials above, then open the shop.",
        expect="Full listing. Captured because only an admin registers an unknown Hauth.",
    ),
    Scenario(
        name="content-filter",
        title="Base games only",
        credentials=("shopper", PASSWORDS["shopper"]),
        path="base",
        instruction=("Point the client at the /base url above (Sphaira: open the shop and "
                     "navigate into 'base'), then open it."),
        expect="Only identified base games; the update, DLC, multi and unidentified files drop out.",
    ),
    Scenario(
        name="download",
        title="Download a file",
        credentials=("shopper", PASSWORDS["shopper"]),
        instruction=("Point the client back at the shop url above - CyberFoil resolves a "
                     "download against it, so a leftover /base url downloads nothing - then "
                     "download Test Game and let it finish."),
        expect="File served; download_count goes to 1. Sphaira HEADs before it GETs.",
    ),
    Scenario(
        name="download-repeat",
        title="Download the same file again, immediately",
        credentials=("shopper", PASSWORDS["shopper"]),
        instruction=("Download Test Game a second time, within a minute of the first. "
                     "Run the 'download' scenario first."),
        expect="File served again; download_count unchanged - the 60s throttle window.",
    ),
    Scenario(
        name="client-disabled",
        title="This client disabled in the shop settings",
        credentials=("shopper", PASSWORDS["shopper"]),
        disable_client=True,
        instruction="Open the shop.",
        expect="Refused before authentication: shop access from this client is disabled.",
    ),
    Scenario(
        name="tinfoil-plaintext",
        title="Tinfoil with shop encryption off",
        credentials=("shopper", PASSWORDS["shopper"]),
        shop={"clients": {"tinfoil": {"encrypt": False}}},
        clients=("tinfoil",),
        instruction="Open the shop.",
        expect="Plain JSON shop instead of the TINFOIL container.",
    ),
    Scenario(
        name="free-browsing",
        title="Anything else the client does on its own",
        credentials=("shopper", PASSWORDS["shopper"]),
        instruction=("Browse freely for a minute: open the shop, move around, cancel a "
                     "download, let it sit idle. Press Enter when done."),
        expect=("Catch-all. This is where requests nobody wrote a handler for turn up - "
                "POST, OPTIONS, repeated polling, retries after a refusal."),
    ),
]

BY_NAME = {s.name: s for s in SCENARIOS}


def for_client(client):
    return [s for s in SCENARIOS if client in s.clients]
