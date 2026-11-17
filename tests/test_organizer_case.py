"""The organizer on a case-insensitive library (#388): a folder or name differing from the template only
in case is the file's own, not a collision, and the watcher events of the move are all recognised as ours.

The filesystem is emulated over a real directory: every path resolves to its on-disk spelling, and a
case-only rename either applies (SMB, NTFS, APFS) or is silently ignored (Linux vfat/exFAT).
"""
import os
import shutil
import sys
import types

import pytest

import library
import tasks
from constants import APP_TYPE_BASE
from db import db, Files, Libraries, IgnoredEvent
from app import create_app

_exists, _isdir, _listdir, _samefile = os.path.exists, os.path.isdir, os.listdir, os.path.samefile
_rename, _makedirs = os.rename, os.makedirs

TITLE_ID = '0100FBD013AB6800'


class CaseInsensitiveFS:
    def __init__(self, root, case_rename):
        self.root, self.case_rename, self.renames = root, case_rename, []

    def real(self, path):
        """The on-disk spelling of path; parts that don't exist yet are kept as given."""
        path = os.fspath(path)
        if not path.startswith(self.root + os.sep):
            return path
        cur = self.root
        for part in os.path.relpath(path, self.root).split(os.sep):
            entries = _listdir(cur) if _isdir(cur) else []
            cur = os.path.join(cur, next((e for e in entries if e.casefold() == part.casefold()), part))
        return cur

    def rename(self, src, dst):
        src, target = self.real(src), os.path.join(self.real(os.path.dirname(dst)), os.path.basename(dst))
        if _exists(target) and _samefile(src, target) and not self.case_rename:
            return
        _rename(src, target)
        self.renames.append((src, target))

    def install(self, monkeypatch):
        monkeypatch.setattr(os.path, 'exists', lambda p: _exists(self.real(p)))
        monkeypatch.setattr(os.path, 'isdir', lambda p: _isdir(self.real(p)))
        monkeypatch.setattr(os.path, 'samefile', lambda a, b: _samefile(self.real(a), self.real(b)))
        monkeypatch.setattr(os, 'listdir', lambda p: _listdir(self.real(p)))
        monkeypatch.setattr(os, 'makedirs', lambda p, exist_ok=False: _makedirs(self.real(p), exist_ok=exist_ok))
        monkeypatch.setattr(os, 'rename', self.rename)
        monkeypatch.setattr(shutil, 'move', self.rename)


class CaseSensitiveFS(CaseInsensitiveFS):
    def real(self, path):
        return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    lib_dir = str(tmp_path / 'games')
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    lib = Libraries(path=lib_dir)
    db.session.add(lib)
    db.session.commit()
    monkeypatch.setattr(library.titles_lib, 'get_game_info', lambda _id: {'name': 'SnowRunner'})
    yield types.SimpleNamespace(app=app, lib_dir=lib_dir, lib=lib, monkeypatch=monkeypatch)
    ctx.pop()


def _seed(env, files):
    files = [os.path.join(*rel.split('/')) for rel in files]
    for rel in files:
        path = os.path.join(env.lib_dir, rel)
        _makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'wb').close()
    f = Files(filepath=os.path.join(env.lib_dir, files[0]), library_id=env.lib.id,
              folder=os.path.dirname(os.path.join(env.lib_dir, files[0])),
              filename=os.path.basename(files[0]), extension='nsp', size=0, mtime=0)
    db.session.add(f)
    db.session.commit()
    app_ = types.SimpleNamespace(app_type=APP_TYPE_BASE, app_id=TITLE_ID, app_version=0,
                                 title=types.SimpleNamespace(title_id=TITLE_ID))
    return types.SimpleNamespace(filepath=f.filepath, filename=f.filename, extension='nsp',
                                 library_id=env.lib.id, multicontent=False, apps=[app_])


def _on_disk(root):
    return sorted(_rel(os.path.join(d, n), root) for d, _, names in os.walk(root) for n in names)


def _rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, '/')


# How each observer reports the renames: inotify one event per rename (a rename to a name the
# watcher ignores reads as a delete), polling the net move, inotify into a new folder delete + create.
def _inotify(renames):
    return [types.SimpleNamespace(type='moved', src_path=s, dest_path=d) if library.is_library_file(d)
            else types.SimpleNamespace(type='deleted', src_path=s, dest_path='') for s, d in renames]

def _polling(renames):
    return [types.SimpleNamespace(type='moved', src_path=renames[0][0], dest_path=renames[-1][1])] if renames else []

def _new_folder(renames):
    return [e for s, d in renames for e in (types.SimpleNamespace(type='deleted', src_path=s, dest_path=''),
                                            types.SimpleNamespace(type='created', src_path=d, dest_path=''))]


SMB = (CaseInsensitiveFS, True, [_inotify, _polling])
VFAT = (CaseInsensitiveFS, False, [_inotify])
EXT4 = (CaseSensitiveFS, True, [_inotify, _polling, _new_folder])

CASES = [
    # id, fs, files on disk (the first is organized), expected files on disk
    ('folder-case-smb', SMB, ['Snowrunner/SnowRunner.nsp'], ['Snowrunner/SnowRunner.nsp']),
    ('folder-case-vfat', VFAT, ['Snowrunner/SnowRunner.nsp'], ['Snowrunner/SnowRunner.nsp']),
    ('name-case-smb', SMB, ['Snowrunner/snowrunner.nsp'], ['Snowrunner/SnowRunner.nsp']),
    ('name-case-vfat', VFAT, ['Snowrunner/snowrunner.nsp'], ['Snowrunner/SnowRunner.nsp']),
    ('other-file-collides', SMB, ['Other.nsp', 'Snowrunner/snowrunner.nsp'],
     ['Snowrunner/SnowRunner(2).nsp', 'Snowrunner/snowrunner.nsp']),
    ('suffix-released', SMB, ['Snowrunner/SnowRunner(2).nsp'], ['Snowrunner/SnowRunner.nsp']),
    ('case-sensitive-new-folder', EXT4, ['Snowrunner/SnowRunner.nsp'], ['SnowRunner/SnowRunner.nsp']),
]


@pytest.mark.parametrize('case,fs,files,expected', CASES, ids=[c[0] for c in CASES])
def test_organize_on_case_insensitive_library(env, case, fs, files, expected):
    fs_cls, case_rename, observers = fs
    if fs_cls is CaseSensitiveFS and sys.platform == 'win32':
        pytest.skip('NTFS folds case itself')
    file_obj = _seed(env, files)
    disk = fs_cls(env.lib_dir, case_rename)
    disk.install(env.monkeypatch)

    assert library.organize_file(file_obj, env.lib_dir, {'templates': {'base': '{titleName}/{titleName}'}})

    assert _on_disk(env.lib_dir) == expected
    organized = Files.query.one().filepath
    assert _rel(organized, env.lib_dir) == expected[0]
    target = library.organized_path(file_obj, env.lib_dir, {'templates': {'base': '{titleName}/{titleName}'}})
    assert os.path.dirname(target) == os.path.dirname(organized)  # what maintenance compares with the DB

    import app as app_mod
    env.monkeypatch.setattr(app_mod, 'app', env.app)
    marks = [(m.src_path, m.dest_path) for m in IgnoredEvent.query.all()]
    for observer in observers:
        IgnoredEvent.query.delete()
        db.session.add_all(IgnoredEvent(src_path=s, dest_path=d) for s, d in marks)
        db.session.commit()
        enqueued = []
        env.monkeypatch.setattr(tasks, 'enqueue_task', lambda name, data=None: enqueued.append(name))
        app_mod.on_library_change([types.SimpleNamespace(directory=env.lib_dir, **vars(e))
                                   for e in observer(disk.renames)])
        assert (observer.__name__, enqueued, IgnoredEvent.query.count()) == (observer.__name__, [], 0)
