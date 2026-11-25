"""Which library files `delete_files_under_dir` removes when a folder disappears."""

import os

import pytest

from app import create_app
from db import db, Files, Libraries, delete_files_under_dir

FILES = ['Zelda/a.nsp', 'Zelda/DLC/b.nsp', 'ZELDA/c.nsp', 'Zelda II/d.nsp', 'Zeldas/e.nsp',
         '100%_Orange/f.nsp', '100XYOrange/g.nsp', 'under_score/h.nsp', 'underXscore/i.nsp']

CASES = {
    'direct and nested files': ('Zelda', ['Zelda/a.nsp', 'Zelda/DLC/b.nsp']),
    'trailing host separator': ('Zelda' + os.sep, ['Zelda/a.nsp', 'Zelda/DLC/b.nsp']),
    'trailing slash': ('Zelda/', ['Zelda/a.nsp', 'Zelda/DLC/b.nsp']),
    'case-only sibling kept (#406)': ('ZELDA', ['ZELDA/c.nsp']),
    'percent and underscore are literal': ('100%_Orange', ['100%_Orange/f.nsp']),
    'underscore is literal': ('under_score', ['under_score/h.nsp']),
}


@pytest.fixture
def lib_dir(tmp_path):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    with app.app_context():
        db.create_all()
        lib_dir = str(tmp_path / 'games')
        lib = Libraries(path=lib_dir)
        db.session.add(lib)
        db.session.commit()
        for rel in FILES:
            path = os.path.join(lib_dir, *rel.split('/'))
            db.session.add(Files(filepath=path, library_id=lib.id, folder=os.path.dirname(path),
                                 filename=os.path.basename(path), extension='nsp'))
        db.session.commit()
        yield lib_dir


@pytest.mark.parametrize('folder,expected', CASES.values(), ids=CASES.keys())
def test_delete_files_under_dir(lib_dir, folder, expected):
    assert delete_files_under_dir(os.path.join(lib_dir, folder)) == len(expected)
    remaining = {os.path.relpath(f.filepath, lib_dir).replace(os.sep, '/') for f in Files.query}
    assert remaining == set(FILES) - set(expected)
