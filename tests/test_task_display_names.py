"""Every registered task needs a label, since it is what logs and the Tasks page show.

A task added without one would silently fall back to its raw name, which is the kind of
gap nobody notices until it is in front of a user.
"""
import pytest

import tasks as tasks_mod

# Representative input per task, standing in for what the queue actually stores.
INPUTS = {
    'scan_library': {'library_path': '/games'},
    'add_file': {'library_path': '/games', 'filepath': '/games/Game.nsp'},
    'process_file': {'file_id': 1, 'filepath': '/games/Game.nsp'},
    'library_maintenance': {'library_path': '/games'},
    'add_missing_apps_for_title': {'title_id': '0100000000AAAAA0'},
    'update_titles_for_title': {'title_id': '0100000000AAAAA0'},
    'verify_file': {'file_id': 1, 'filepath': '/games/Game.nsp'},
    'compress_file': {'file_id': 1, 'filepath': '/games/Game.nsp'},
    'decompress_file': {'file_id': 1, 'filepath': '/games/Game.nsz'},
    'remove_library': {'library_path': '/games'},
    'handle_file_added': {'library_path': '/games', 'filepath': '/games/Game.nsp'},
    'handle_file_moved': {'library_path': '/games', 'src_path': '/games/A.nsp',
                          'dest_path': '/games/B.nsp'},
    'handle_file_deleted': {'filepath': '/games/Game.nsp'},
    'handle_dir_deleted': {'dirpath': '/games/Folder'},
}


def test_every_registered_task_has_a_display_name():
    assert set(tasks_mod.TASK_REGISTRY) - set(tasks_mod.TASK_DISPLAY) == set()


@pytest.mark.parametrize('task_name', sorted(tasks_mod.TASK_REGISTRY))
def test_a_task_builds_a_non_empty_label_from_its_input(task_name):
    label = tasks_mod.task_display_name(task_name, INPUTS.get(task_name, {}))
    assert label and label.strip() == label


LABELS = [
    ('scan_library', {'library_path': '/games'}, 'Scan /games'),
    ('compress_file', {'file_id': 1, 'filepath': '/games/Game.nsp'}, 'Compress Game.nsp'),
    ('handle_file_deleted', {'filepath': '/games/Game.nsp'}, 'Deleted Game.nsp'),
    ('update_titledb', {}, 'Update TitleDB'),
]


@pytest.mark.parametrize('task_name,input_data,expected', LABELS,
                         ids=[c[0] for c in LABELS])
def test_labels_read_the_way_a_user_would_expect(task_name, input_data, expected):
    assert tasks_mod.task_display_name(task_name, input_data) == expected


def test_input_that_no_longer_matches_falls_back_to_the_task_name():
    """input_json is persisted, so it can outlive a change to a task's arguments."""
    assert tasks_mod.task_display_name('scan_library', {'obsolete': 1}) == 'Scan library'


def test_an_unregistered_task_still_gets_a_label():
    assert tasks_mod.task_display_name('some_new_task', {}) == 'Some new task'
