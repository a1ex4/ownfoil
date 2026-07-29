"""Tests for `is_network_path`, which decides whether a library path is watched natively
(inotify / ReadDirectoryChangesW) or polled. The Windows branch resolves the drive type via
GetDriveTypeW; the POSIX branch reads the mount table. Both are exercised here on any host:
the Windows path logic uses `ntpath` explicitly and the WinAPI call is stubbed out."""

import pytest

import utils


def _windows(monkeypatch, drive_type=3):
    """Pretend we're on Windows, with GetDriveTypeW returning `drive_type` (None = failure)."""
    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils, "get_windows_drive_type", lambda drive: drive_type)


DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE = 0, 1, 2
DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = 3, 4, 5, 6


@pytest.mark.parametrize("drive_type,expected", [
    (DRIVE_FIXED, False),
    (DRIVE_REMOVABLE, False),
    (DRIVE_CDROM, False),
    (DRIVE_RAMDISK, False),
    (DRIVE_REMOTE, True),        # mapped network drive
    (DRIVE_UNKNOWN, True),       # undeterminable: poll rather than miss events
    (DRIVE_NO_ROOT_DIR, True),
    (None, True),                # GetDriveTypeW unavailable/failed
])
def test_windows_drive_types(monkeypatch, drive_type, expected):
    _windows(monkeypatch, drive_type)
    assert utils.is_network_path(r"C:\games\switch") is expected


@pytest.mark.parametrize("path", [
    r"\\nas\media\switch",
    "//nas/media/switch",
])
def test_windows_unc_is_network(monkeypatch, path):
    """A UNC share is remote regardless of what GetDriveTypeW would say (it has no drive root)."""
    _windows(monkeypatch, DRIVE_FIXED)
    assert utils.is_network_path(path) is True


def test_windows_queries_the_drive_root(monkeypatch):
    """GetDriveTypeW must be given a drive *root* ('C:\\'), not the full path."""
    seen = []
    monkeypatch.setattr(utils.sys, "platform", "win32")
    monkeypatch.setattr(utils, "get_windows_drive_type", lambda drive: seen.append(drive) or DRIVE_FIXED)

    utils.is_network_path(r"D:\library\nsp")
    assert seen == ["D:\\"]


def test_posix_uses_mount_table(monkeypatch):
    """Off Windows the fstype decides, and an undeterminable mount is polled."""
    monkeypatch.setattr(utils.sys, "platform", "linux")

    monkeypatch.setattr(utils, "get_path_fstype", lambda p: "ext4")
    assert utils.is_network_path("/games") is False

    monkeypatch.setattr(utils, "get_path_fstype", lambda p: "nfs4")
    assert utils.is_network_path("/games") is True

    monkeypatch.setattr(utils, "get_path_fstype", lambda p: None)
    assert utils.is_network_path("/games") is True
