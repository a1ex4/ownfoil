"""A change to where the organizer places files (#392) hands every file back to it, including one whose
move was already under way with the old layout when the change was saved."""
import copy

import pytest

import settings as settings_mod
import tasks
from capture.fixture import PASSWORDS
from db import db, Files


def _organizer(**changes):
    organizer = copy.deepcopy(settings_mod.get_settings()['library']['management']['organizer'])
    for key, value in changes.items():
        if key in organizer['templates']:
            organizer['templates'][key] = value
        else:
            organizer[key] = value
    return organizer


CASES = [
    # id, organizer changes saved, files handed back to the organizer
    ('unchanged', {}, False),
    ('organizer-toggled', {'enabled': True}, False),
    ('empty-folders-toggled', {'remove_empty_folders': True}, False),
    ('base-template', {'base': '{titleName}'}, True),
    ('dlc-template', {'dlc': '{appName}'}, True),
    ('windows-compatible', {'windows_compatible': True}, True),
]


@pytest.mark.parametrize('case,changes,reset', CASES, ids=[c[0] for c in CASES])
def test_save_resets_organized_on_layout_change(shop_app, monkeypatch, case, changes, reset):
    enqueued = []
    monkeypatch.setattr(tasks, 'enqueue_task', lambda name, data=None, **kw: enqueued.append(name))
    with shop_app.app.app_context():
        Files.query.update({Files.organized: True})
        db.session.commit()
        organizer = _organizer(**changes)

    shop_app.client.post('/login', data={'user': 'admin', 'password': PASSWORDS['admin']})
    body = shop_app.client.post('/api/settings/library/management', json={'organizer': organizer}).json

    with shop_app.app.app_context():
        organized = {f.organized for f in Files.query.all()}
    assert (body['success'], organized, enqueued) == (True, {not reset}, ['process_library'])


@pytest.mark.parametrize('saved_during_move', [False, True])
def test_move_racing_a_layout_change_is_redone(shop_app, monkeypatch, saved_during_move):
    moves = []

    def organize_file(file, library_path, organizer):
        moves.append(organizer['templates']['base'])
        if saved_during_move and len(moves) == 1:
            settings_mod.set_library_management_settings({'organizer': _organizer(enabled=True, base='{titleName}')})
        return True

    monkeypatch.setattr(tasks, 'organize_file', organize_file)
    monkeypatch.setattr(tasks, 'request_maintenance', lambda *a: None)
    with shop_app.app.app_context():
        file = Files.query.first()
        mgmt = settings_mod.get_settings()['library']['management']
        mgmt = {**mgmt, 'organizer': {**mgmt['organizer'], 'enabled': True}}
        default = mgmt['organizer']['templates']['base']

        tasks._organize(file, mgmt)

        expected = [default, '{titleName}'] if saved_during_move else [default]
        assert (moves, db.session.get(Files, file.id).organized) == (expected, True)
