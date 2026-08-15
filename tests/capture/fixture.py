"""Fixture accounts and library shared by the capture server and the replay tests.

Both sides have to build the exact same shop, otherwise a capture recorded against one
library can't be replayed against another: file ids end up in Tinfoil's urls and filenames
end up in Sphaira's listings.
"""
import os

from constants import APP_TYPE_BASE, APP_TYPE_UPD, APP_TYPE_DLC
from db import Apps, Files, Libraries, Titles, db

# Fixture accounts. Passwords avoid the characters Tinfoil forbids: @ & / ? # =
USERS = [
    {"user": "admin", "password": "adminpass1", "admin_access": True, "shop_access": True},
    {"user": "shopper", "password": "shoppass1", "admin_access": False, "shop_access": True},
    {"user": "noshop", "password": "noshoppass1", "admin_access": False, "shop_access": False},
]
PASSWORDS = {u["user"]: u["password"] for u in USERS}

UNKNOWN_USER = "ghost"        # never seeded - what the unknown-user scenario types
WRONG_PASSWORD = "wrongpass1"

DUMMY_SIZE = 4096

# The file the clients are asked to download. Dummy bytes like the rest of the library: what
# a download exercises is the transfer and the counting, and the clients take a short file
# as readily as a real one.
DOWNLOAD_TARGET = {
    "relpath": "Test Game/Test Game [0100000000010000][v0].nsp",
    "title": "0100000000010000", "app_id": "0100000000010000", "version": "0",
    "app_type": APP_TYPE_BASE,
}

# A nested tree, because Sphaira serves virtual directories built from these paths, and one
# of each filterable kind so /base, /update, /dlc and /multi all return something distinct.
LIBRARY = [
    DOWNLOAD_TARGET,
    {"relpath": "Test Game/Test Game [0100000000010800][v65536].nsp",
     "title": "0100000000010000", "app_id": "0100000000010800", "version": "65536",
     "app_type": APP_TYPE_UPD},
    {"relpath": "Test Game/Test Game Extra [0100000000011001][v0].nsp",
     "title": "0100000000010000", "app_id": "0100000000011001", "version": "0",
     "app_type": APP_TYPE_DLC},
    {"relpath": "Bundles/Multi Pack [0100000000012000].xci",
     "multicontent": True, "nb_content": 3},
    # Unidentified files are served unfiltered but disappear under every content filter.
    {"relpath": "Unsorted/Mystery File.nsp", "identified": False},
]


def build_library(root):
    """Create the fixture tree under root: realistic names, dummy bytes."""
    for entry in LIBRARY:
        path = os.path.join(root, entry["relpath"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(os.urandom(DUMMY_SIZE))
    return root


def seed_library(root):
    """Insert the Libraries/Files/Titles/Apps rows describing the fixture tree."""
    library = Libraries(path=root)
    db.session.add(library)
    db.session.flush()

    titles = {}
    for entry in LIBRARY:
        path = os.path.join(root, entry["relpath"])
        folder, filename = os.path.split(path)
        file_row = Files(
            library_id=library.id, filepath=path, folder=folder, filename=filename,
            extension=filename.rsplit(".", 1)[-1], size=os.path.getsize(path),
            identified=entry.get("identified", True),
            multicontent=entry.get("multicontent", False),
            nb_content=entry.get("nb_content", 1),
        )
        db.session.add(file_row)
        db.session.flush()

        if not entry.get("app_id"):
            continue
        title_id = entry["title"]
        if title_id not in titles:
            title = Titles(title_id=title_id, have_base=True)
            db.session.add(title)
            db.session.flush()
            titles[title_id] = title
        app = Apps(title_id=titles[title_id].id, app_id=entry["app_id"],
                   app_version=entry["version"], app_type=entry["app_type"], owned=True)
        app.files.append(file_row)
        db.session.add(app)

    db.session.commit()
    return library


def seed_users():
    """Create the three fixture accounts, replacing any left over from an earlier run."""
    from auth import create_or_update_user

    for spec in USERS:
        create_or_update_user(spec["user"], spec["password"],
                              admin_access=spec["admin_access"],
                              shop_access=spec["shop_access"])
