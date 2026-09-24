"""Which copies deduplication deletes: every file whose apps all have a better copy kept."""
import os
import types

import pytest

from db import db, Apps, Files, Libraries, Titles
from library import remove_outdated_update_files

from app import create_app


@pytest.fixture
def env(tmp_path):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    libraries = []
    for name in ("games", "more"):
        (tmp_path / name).mkdir()
        libraries.append(Libraries(path=str(tmp_path / name)))
    title = Titles(title_id="0100000000010000")
    db.session.add_all(libraries + [title])
    db.session.commit()
    apps = {}

    def seed(name, app_letters, overrides):
        overrides = dict(overrides)
        library = libraries[overrides.pop("library", 0)]
        overrides.pop("pending", None)
        path = os.path.join(library.path, name)
        if not overrides.pop("missing", False):
            with open(path, "wb") as fh:
                fh.write(b"x")
        f = Files(library_id=library.id, filepath=path, filename=name,
                  extension=name.rsplit(".", 1)[-1], identified=True,
                  multicontent=len(app_letters) > 1,
                  **{"identification_type": "cnmt", **overrides})
        db.session.add(f)
        for letter in app_letters:
            if letter not in apps:
                apps[letter] = Apps(title_id=title.id, app_id=letter, app_version="0",
                                    app_type="BASE", owned=True)
                db.session.add(apps[letter])
            apps[letter].files.append(f)
        db.session.commit()
        return f

    yield types.SimpleNamespace(seed=seed, apps=apps)
    ctx.pop()


def test_remove_outdated_update_files_deletes_the_file_and_its_row(env):
    old = env.seed("old.nsp", "U", {})
    env.seed("new.nsp", "V", {})
    for letter, version in (("U", "65536"), ("V", "131072")):
        app = env.apps[letter]
        app.app_id, app.app_version, app.app_type = "0100000000010800", version, "UPDATE"
    db.session.commit()
    path = old.filepath

    remove_outdated_update_files()

    assert not os.path.exists(path)
    assert Files.query.filter_by(filepath=path).first() is None
    assert [f.filename for f in Files.query.all()] == ["new.nsp"]
