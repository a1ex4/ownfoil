"""Filename sanitization and Windows path length enforcement for the organizer."""
import os
import pytest

from constants import (
    COLLISION_SUFFIX_RESERVE,
    MAX_DIR_PATH_WINDOWS,
    MAX_PART_WINDOWS,
    MAX_PATH_WINDOWS,
    MAX_NAME_WINDOWS,
    MIN_PART_WINDOWS,
    TEMPLATE_NAME_KEYS,
    TRUNCATION_MARKER,
)
import utils
from library import prepare_template_names
from utils import sanitize_filename, sanitized_path_parts, truncate_path_parts

SANITIZE_CASES = [
    # name, windows, expected
    ('Pokémon: Let\'s Go', True, 'Pokémon： Let\'s Go'),
    ('Pokémon: Let\'s Go', False, 'Pokémon: Let\'s Go'),
    ('A/B', True, 'A／B'),
    ('A/B', False, 'A／B'),
    ('<t> "x"|y?*\\z', True, '＜t＞ ＂x＂｜y？＊＼z'),
    ('<t> "x"|y?*\\z', False, '<t> "x"|y?*\\z'),
    ('  spaced  ', True, 'spaced'),
    ('  spaced  ', False, 'spaced'),
    ('Game. ', True, 'Game．'),
    ('Game. ', False, 'Game.'),
    ('Mr. Driller', True, 'Mr. Driller'),
    ('CON', True, '_CON'),
    ('con', False, 'con'),
    ('bell\x07', True, 'bell␇'),
    ('nul\x00', False, 'nul␀'),
    ('void* tRrLM2(); //Void Terrarium 2', True, 'void＊ tRrLM2(); ／／Void Terrarium 2'),
    ('void* tRrLM2(); //Void Terrarium 2', False, 'void* tRrLM2(); ／／Void Terrarium 2'),
]


@pytest.mark.parametrize('name, windows, expected', SANITIZE_CASES)
def test_sanitize_filename(name, windows, expected, monkeypatch):
    # sanitize_filename ORs the flag with `sys.platform == 'win32'`, so the host would decide
    # the outcome of the windows_compatible=False rows. Pin it to isolate the flag itself.
    monkeypatch.setattr(utils.sys, 'platform', 'linux')
    assert sanitize_filename(name, windows_compatible=windows) == expected


def test_windows_host_sanitizes_without_the_flag(monkeypatch):
    """Running on Windows applies the Windows ruleset even when the flag is off, since the
    filenames are headed for a Windows filesystem either way."""
    monkeypatch.setattr(utils.sys, 'platform', 'win32')
    assert sanitize_filename("Pokémon: Let's Go", windows_compatible=False) == "Pokémon： Let's Go"
    assert sanitize_filename('CON', windows_compatible=False) == '_CON'


def _full_len(parts, prefix_len):
    return prefix_len + sum(len(p) + 1 for p in parts)


MON_YU = ('MON-YU： DEFEAT MONSTERS AND GAIN STRONG WEAPONS AND ARMOR. YOU MAY BE DEFEATED, BUT DON’T '
          'GIVE UP. BECOME STRONGER. I BELIEVE THERE WILL BE A DAY WHEN THE HEROES DEFEAT THE DEVIL KING')

TRUNCATE_CASES = [
    # id, prefix_len, parts
    ('short path untouched', 10, ['Title', 'Title [0100].nsp']),
    ('long real title', 6, [MON_YU, f'{MON_YU} [0100E7500BF84000][v0].nsp']),
    ('long real title, long prefix', 43, [MON_YU, f'{MON_YU} [0100E7500BF84000][v0].nsp']),
    ('long filename', 10, ['Title', 'T' * 400 + '.nsp']),
    ('long directory', 10, ['D' * 400, 'Title [0100].nsp']),
    ('several long directories', 10, ['D' * 200, 'E' * 200, 'F' * 200, 'Title.nsp']),
    ('long prefix', 200, ['Some Long Title Name', 'Some Long Title Name [0100].nsp']),
    ('no directory', 10, ['T' * 400 + '.nsp']),
    ('no extension', 10, ['Title', 'T' * 400]),
]


@pytest.mark.parametrize('case_id, prefix_len, parts', TRUNCATE_CASES, ids=[c[0] for c in TRUNCATE_CASES])
def test_truncate_path_parts(case_id, prefix_len, parts):
    result = truncate_path_parts(list(parts), prefix_len)

    assert len(result) == len(parts)
    # A collision suffix must still fit within the limit
    assert _full_len(result, prefix_len) + COLLISION_SUFFIX_RESERVE <= MAX_PATH_WINDOWS
    assert prefix_len + sum(len(p) + 1 for p in result[:-1]) <= MAX_DIR_PATH_WINDOWS
    assert all(len(p) <= MAX_PART_WINDOWS for p in result)
    # Nothing is shortened more than necessary, and each part stays usable
    assert all(len(new) <= len(old) for new, old in zip(result, parts))
    assert all(len(p) >= min(MIN_PART_WINDOWS, len(old)) for p, old in zip(result, parts))
    # Truncation never leaves a name Windows would reject
    assert all(p == p.rstrip() and not p.endswith('.') for p in result)
    # The extension is preserved
    assert os.path.splitext(result[-1])[1] == os.path.splitext(parts[-1])[1]
    # Shortened names, and only those, are marked with the ellipsis
    for new, old in zip(result, parts):
        assert (TRUNCATION_MARKER in new) == (len(new) < len(old))


BASE_TEMPLATE = '{titleName}/{titleName} [{appId}][v{appVersion}].{extension}'
DLC_TEMPLATE = '{titleName}/{appName} [{appId}][v{appVersion}].{extension}'

FIT_CASES = [
    # id, template, prefix_len, title_name, app_name
    ('short name untouched', BASE_TEMPLATE, 6, 'Celeste', 'Celeste'),
    ('long name, short prefix', BASE_TEMPLATE, 6, MON_YU, MON_YU),
    ('long name, long prefix', BASE_TEMPLATE, 43, MON_YU, MON_YU),
    ('long dlc names', DLC_TEMPLATE, 43, MON_YU, MON_YU + ' - SEASON PASS BUNDLE'),
    ('long title, short dlc', DLC_TEMPLATE, 43, MON_YU, 'Extra Costumes'),
]


def _organized_parts(template, data, prefix_len):
    parts = sanitized_path_parts(template.format(**prepare_template_names(data, True)), True)
    return truncate_path_parts(parts, prefix_len)


@pytest.mark.parametrize('case_id, template, prefix_len, title_name, app_name',
                         FIT_CASES, ids=[c[0] for c in FIT_CASES])
def test_fit_template_names(case_id, template, prefix_len, title_name, app_name):
    data = {'titleName': title_name, 'appName': app_name, 'appId': '0100E7500BF84000',
            'appVersion': '65536', 'extension': 'nsp'}
    fitted = prepare_template_names(dict(data), True)
    parts = _organized_parts(template, dict(data), prefix_len)

    assert _full_len(parts, prefix_len) + COLLISION_SUFFIX_RESERVE <= MAX_PATH_WINDOWS
    # Only the names are shortened, the rest of the template survives
    assert parts[-1].endswith(f'[{data["appId"]}][v{data["appVersion"]}].nsp')
    assert all(fitted[k] == v for k, v in data.items() if k not in TEMPLATE_NAME_KEYS)
    # A name is only shortened when it exceeds the cap, and always to the same value
    for key in TEMPLATE_NAME_KEYS:
        assert len(fitted[key]) <= MAX_NAME_WINDOWS
        assert (TRUNCATION_MARKER in fitted[key]) == (len(data[key]) > MAX_NAME_WINDOWS)
    assert parts[0] == sanitize_filename(fitted['titleName'], True)


@pytest.mark.parametrize('prefix_len', [6, 43, 62])
def test_title_directory_is_independent_of_the_file(prefix_len):
    """Files of the same title must land in the same folder, whatever their own name."""
    base = {'titleName': MON_YU, 'appId': '0100E7500BF84000', 'appVersion': '0', 'extension': 'nsp'}
    dirs = {
        _organized_parts(DLC_TEMPLATE, {**base, 'appName': app_name}, prefix_len)[0]
        for app_name in ('Extra Costumes', MON_YU, MON_YU + ' - SEASON PASS BUNDLE', 'A' * 300)
    }
    assert len(dirs) == 1


SEPARATOR_CASES = [
    # title_name, app_name, expected title directory
    ('void* tRrLM2(); //Void Terrarium 2', 'DLC/Extra', 'void＊ tRrLM2(); ／／Void Terrarium 2'),
    ('Fate/EXTELLA LINK', 'Fate/EXTELLA Costume', 'Fate／EXTELLA LINK'),
]


@pytest.mark.parametrize('windows', [True, False])
@pytest.mark.parametrize('title_name, app_name, windows_dir', SEPARATOR_CASES)
def test_names_cannot_introduce_path_separators(windows, title_name, app_name, windows_dir):
    """A name containing a slash must stay one path part, not create directories."""
    data = {'titleName': title_name, 'appName': app_name,
            'appId': '0100', 'appVersion': '0', 'extension': 'nsp'}
    prepared = prepare_template_names(dict(data), windows)
    parts = sanitized_path_parts(DLC_TEMPLATE.format(**prepared), windows)

    assert len(parts) == 2
    assert parts[0] == prepared['titleName']
    assert parts[-1] == f'{prepared["appName"]} [0100][v0].nsp'
    # The slashes are replaced, on every platform
    assert not any('/' in p for p in parts)
    assert all(p.count('／') == old.count('/') for p, old in zip(parts, (title_name, app_name)))
    if windows:
        assert parts[0] == windows_dir


def test_fit_template_names_is_independent_of_the_library_path():
    data = {'titleName': MON_YU, 'appName': MON_YU, 'appId': '0100E7500BF84000',
            'appVersion': '0', 'extension': 'nsp'}
    assert _organized_parts(BASE_TEMPLATE, dict(data), 6) == _organized_parts(BASE_TEMPLATE, dict(data), 62)


def test_truncate_keeps_directories_when_filename_can_absorb():
    parts = ['Directory', 'F' * 400 + '.nsp']
    result = truncate_path_parts(list(parts), 10)
    assert result[0] == 'Directory'


def test_truncate_shrinks_directories_before_starving_the_filename():
    parts = ['D' * 300, 'F' * 300 + '.nsp']
    result = truncate_path_parts(list(parts), 10)
    assert len(result[0]) < MAX_PART_WINDOWS
    assert len(os.path.splitext(result[-1])[0]) >= MIN_PART_WINDOWS
