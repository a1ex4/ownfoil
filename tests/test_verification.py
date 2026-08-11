"""Behavioural tests for file integrity verification.

nstools is stubbed throughout: there are no sample game files in the repo, and these
assert the *contract* — how the three phases map onto the stored verdicts, when the
pipeline asks for verification, and what a failed verdict stops downstream.
"""
import os
import types
from contextlib import contextmanager

import pytest

from containers import verification
import tasks
from db import db, Files, Libraries, reset_file_verification, verification_status

from app import create_app

from test_compression import _settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    """App + DB + a library dir on disk, with a helper to seed files."""
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    lib_dir = tmp_path / "games"
    lib_dir.mkdir()
    ctx = app.app_context()
    ctx.push()
    db.create_all()
    library = Libraries(path=str(lib_dir))
    db.session.add(library)
    db.session.commit()

    monkeypatch.setattr(tasks, "get_settings", lambda: _settings(verify=True))

    def seed(name="Game.nsp", **columns):
        path = lib_dir / name
        path.write_bytes(b"RAWDATA")
        f = Files(filepath=str(path), library_id=library.id, folder=str(lib_dir),
                  filename=name, extension=name.rsplit(".", 1)[-1], size=7,
                  mtime=os.path.getmtime(path), identified=True, **columns)
        db.session.add(f)
        db.session.commit()
        return f

    yield types.SimpleNamespace(app=app, lib_dir=lib_dir, seed=seed, monkeypatch=monkeypatch)
    ctx.pop()


def _fake_container(raises=None):
    """Stand in for containers.open_container; lifecycle itself is tested in test_containers."""
    @contextmanager
    def opener(filepath, meta_only=False):
        if raises is not None:
            raise raises
        yield types.SimpleNamespace()
    return opener


def _stub_phases(monkeypatch, *, decrypt=True, signature=True, hashed=True, messages=None):
    """Replace the three nstools phases, recording which ones ran."""
    ran = []
    msgs = messages or []
    monkeypatch.setattr(verification, "open_container", _fake_container())
    monkeypatch.setattr(verification.Verify, "verify_decrypt",
                        lambda c, m: (ran.append("decrypt"), (decrypt, msgs))[1])
    monkeypatch.setattr(verification.Verify, "verify_sig",
                        lambda c, m: (ran.append("sig"), (signature, [], msgs))[1])
    monkeypatch.setattr(verification.Verify, "verify_hash",
                        lambda c, h, m: (ran.append("hash"), (hashed, msgs))[1])
    return ran


@pytest.fixture(autouse=True)
def _keys_loaded(monkeypatch):
    """Every test here assumes valid keys; the absent-keys case is tested explicitly."""
    monkeypatch.setattr(tasks.titles_lib.Keys, "keys_loaded", True, raising=False)
    import settings as settings_mod
    monkeypatch.setattr(settings_mod, "ensure_keys", lambda action: None)


# --- verify() ------------------------------------------------------------------------------

def test_signature_depth_skips_the_hash_phase(monkeypatch, tmp_path):
    ran = _stub_phases(monkeypatch)
    verdict = verification.verify(str(tmp_path / "Game.nsp"), verification.DEPTH_SIGNATURE)
    assert verdict == (True, None, None, None)
    assert "hash" not in ran


def test_hash_depth_runs_every_phase(monkeypatch, tmp_path):
    ran = _stub_phases(monkeypatch)
    assert verification.verify(str(tmp_path / "Game.nsp"),
                               verification.DEPTH_HASH) == (True, True, False, None)
    assert ran[:3] == ["decrypt", "sig", "hash"]


def test_decrypt_failure_counts_against_the_signature_verdict(monkeypatch, tmp_path):
    """The structural test - missing NCAs, missing tickets, a wrong title key - has no
    column of its own; it is folded in, because it is a prerequisite for the signature."""
    _stub_phases(monkeypatch, decrypt=False,
                 messages=["> abc.nca\t -> is MISSING <<<-"])
    sig, _, _, error = verification.verify(str(tmp_path / "Game.nsp"),
                                           verification.DEPTH_SIGNATURE)
    assert sig is False
    assert error == "> abc.nca\t -> is MISSING <<<-"


def test_corrupt_content_reports_only_the_failing_lines(monkeypatch, tmp_path):
    """A whole nstools log is far too long for a DB column, and mostly narration."""
    _stub_phases(monkeypatch, hashed=False, messages=[
        "> FILE: abc.nca\n> SHA256: dead\n> FILE IS CORRUPT",
        "\nVERDICT: NSP FILE IS SAFE",
    ])
    sig, hashed, modified, error = verification.verify(str(tmp_path / "Game.nsp"),
                                                       verification.DEPTH_HASH)
    assert (sig, hashed, modified) == (True, False, False)
    assert error == "> FILE IS CORRUPT"


def test_a_repack_is_told_apart_from_damage(monkeypatch, tmp_path):
    """nstools reports MODIFIED and CORRUPT as the same failed verdict. The extra column
    is the only thing that keeps a repacked file from being called damaged."""
    _stub_phases(monkeypatch, hashed=False, messages=[
        "> FILE WAS MODIFIED", "\nVERDICT: XCI FILE WAS MODIFIED"])
    sig, hashed, modified, error = verification.verify(str(tmp_path / "Game.xci"),
                                                       verification.DEPTH_HASH)
    assert (sig, hashed, modified) == (True, False, True)
    assert error == "> FILE WAS MODIFIED; VERDICT: XCI FILE WAS MODIFIED"


def test_a_resigned_header_alone_is_not_a_modified_verdict(monkeypatch, tmp_path):
    """verify_sig writes a per-content '-> was MODIFIED' for every re-signed header, and
    that is the common case - a repack whose contents still hash correctly. Only the
    file-level VERDICT line from the hash phase decides the column."""
    _stub_phases(monkeypatch, signature=False, messages=[
        "> abc.nca\t\t -> was MODIFIED", "\nVERDICT: NSP FILE COULD'VE BEEN TAMPERED WITH",
        "\nVERDICT: NSP FILE IS CORRECT"])
    assert verification.verify(str(tmp_path / "Game.nsp"),
                               verification.DEPTH_HASH)[:3] == (False, True, False)


def test_unreadable_file_is_not_vouched_for(monkeypatch, tmp_path):
    """A phase that raises is a failure, not an absent result: we cannot vouch for a file
    we could not read, and leaving the verdict null would retry it on every sweep."""
    _stub_phases(monkeypatch)
    monkeypatch.setattr(verification.Verify, "verify_sig",
                        lambda c, m: (_ for _ in ()).throw(OSError("master_key_0a missing")))
    assert verification.verify(str(tmp_path / "Game.nsp"), verification.DEPTH_HASH) == (
        False, False, None, "master_key_0a missing")


def test_unopenable_file_reports_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(verification, "open_container",
                        _fake_container(raises=ValueError("not a PFS0")))
    assert verification.verify(str(tmp_path / "Game.nsp"), verification.DEPTH_SIGNATURE) == (
        False, None, None, "not a PFS0")


# --- the verdict line nstools actually emits ---------------------------------------------
#
# _modified_from reads nstools' log, so it is pinned to wording. These cases are copied
# verbatim from Verify.py rather than paraphrased, and the last one is the trap: the same
# two words appear in the signature phase, about headers rather than contents.

MODIFIED_LINES = [
    ("\nVERDICT: XCI FILE WAS MODIFIED", True),
    ("\nVERDICT: NSP FILE WAS MODIFIED", True),
    ("\nVERDICT: NSP FILE IS CORRECT", False),
    ("\nVERDICT: NSP FILE IS CORRUPT", False),
    ("\nVERDICT: NSP FILE COULD'VE BEEN TAMPERED WITH", False),
    ("> abc.nca\t\t -> was MODIFIED", False),
    ("> FILE WAS MODIFIED", False),          # per content, not the file's verdict
]


@pytest.mark.parametrize("line,expected", MODIFIED_LINES,
                         ids=[line.strip()[:38] for line, _ in MODIFIED_LINES])
def test_only_the_hash_phase_verdict_line_means_modified(line, expected):
    assert verification._modified_from([line]) is expected


def test_progress_shim_reports_monotonic_whole_file_percent(monkeypatch, tmp_path):
    """nstools builds a fresh bar per NCA, counting MiB within that content only, so
    whole-file percent needs the finished bars accumulated."""
    source = tmp_path / "Game.nsp"
    source.write_bytes(b"x" * (100 << 20))     # 100 MiB
    seen = []

    with verification._progress_bars(seen.append, str(source)):
        Bar = verification.Verify.pb_Counter
        first = Bar(total=50)
        for mib in (10, 20, 50):
            first.count = mib
            first.refresh()
        first.close()
        second = Bar(total=50)
        second.count = 25
        second.refresh()

    assert seen == [10, 20, 50, 75]
    assert seen == sorted(seen)


def test_progress_shim_clamps_and_is_restored(monkeypatch, tmp_path):
    """An NSZ's counter walks decompressed bytes, which overshoot the file size."""
    source = tmp_path / "Game.nsz"
    source.write_bytes(b"x" * (1 << 20))
    seen = []
    original = verification.Verify.pb_Counter

    with verification._progress_bars(seen.append, str(source)):
        bar = verification.Verify.pb_Counter(total=99)
        bar.count = 500
        bar.refresh()

    assert seen == [99]                                   # clamped, never 100
    assert verification.Verify.pb_Counter is original     # nstools left as we found it


def test_nstools_chatter_is_silenced():
    """43 bare print() calls that nsz's Print.silent does not reach."""
    assert verification.Verify.print("anything") is None


# --- the verify stage ------------------------------------------------------------------------

@pytest.mark.parametrize("depth,columns,expected", [
    ("signature", {}, True),
    ("signature", {"signature_valid": True}, False),
    ("signature", {"signature_valid": False}, False),      # a verdict is a verdict
    ("hash", {"signature_valid": True}, True),             # deeper level not yet attempted
    ("hash", {"hash_valid": False, "hash_modified": False}, False),
    ("hash", {"hash_valid": True}, False),
    # A failure with no hash_modified came from a phase that raised - an unreadable file
    # rather than a verdict - so it is re-checked. A pass has nothing left to learn.
    ("hash", {"hash_valid": False}, True),
])
def test_needs_verify_tracks_the_configured_depth(env, depth, columns, expected):
    f = env.seed(**columns)
    mgmt = _settings(verify=True, depth=depth)["library"]["management"]
    assert tasks._needs_verify(f, mgmt) is expected


def test_needs_verify_is_off_without_keys(env):
    """nstools cannot open a container without them, so the stage must not be offered."""
    env.monkeypatch.setattr(tasks.titles_lib.Keys, "keys_loaded", False, raising=False)
    f = env.seed()
    assert tasks._needs_verify(f, _settings(verify=True)["library"]["management"]) is False


@pytest.mark.parametrize("name", ["Game.kip", "Game.txt"])
def test_needs_verify_skips_non_containers(env, name):
    f = env.seed(name)
    assert tasks._needs_verify(f, _settings(verify=True)["library"]["management"]) is False


def test_needs_verify_is_off_when_disabled(env):
    f = env.seed()
    assert tasks._needs_verify(f, _settings(verify=False)["library"]["management"]) is False


def test_verify_runs_after_organize_and_before_compress(env):
    """A file is verified where it will live, and never compressed before it is vouched for."""
    enqueued = []
    env.monkeypatch.setattr(tasks, "get_settings",
                            lambda: _settings(verify=True, organizer=True))
    env.monkeypatch.setattr(tasks, "organize_file", lambda *a, **k: True)
    env.monkeypatch.setattr(tasks, "enqueue_task",
                            lambda name, data=None, **k: enqueued.append(name))
    f = env.seed()

    tasks.process_file_task(file_id=f.id)

    assert db.session.get(Files, f.id).organized is True
    assert enqueued == ["library_maintenance", "verify_file"]


# (filename, verdict columns, the status they derive to). One file per status a real
# library produces, so the compression expectation can be read off the status rather than
# restated per case - CORRUPT is the only one that blocks.
VERDICT_CASES = [
    ("A.nsp", {}, verification.STATUS_UNVERIFIED),
    ("B.nsp", {"signature_valid": True}, verification.STATUS_SIGNATURE_OK),
    ("C.nsp", {"signature_valid": False}, verification.STATUS_SIGNATURE_FAILED),
    ("D.nsp", {"signature_valid": True, "hash_valid": True}, verification.STATUS_VALID),
    ("E.nsp", {"signature_valid": False, "hash_valid": True}, verification.STATUS_REPACK),
    ("F.nsp", {"hash_valid": False, "hash_modified": True}, verification.STATUS_MODIFIED),
    ("G.nsp", {"hash_valid": False}, verification.STATUS_CORRUPT),
]


def test_verification_status_takes_a_row_or_an_id(env):
    """The row-level reader every caller shares, so the columns are folded in one place."""
    for filename, verdicts, status in VERDICT_CASES:
        f = env.seed(filename, **verdicts)
        assert verification_status(f) == status
        assert verification_status(f.id) == status


def test_compress_stage_refuses_only_a_corrupt_file(env):
    """Damaged bytes would fail nsz's own round-trip check. Nothing else is grounds to
    refuse: a re-signed or un-renamed repack is commonplace and its content still compresses."""
    mgmt = _settings(verify=True)["library"]["management"]
    for filename, verdicts, status in VERDICT_CASES:
        assert tasks._needs_compress(env.seed(filename, **verdicts), mgmt) is (
            status != verification.STATUS_CORRUPT), status


# --- verify_file task --------------------------------------------------------------------

def test_verify_file_stores_the_verdicts_and_redrives(env):
    enqueued = []
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(verify=True, depth="hash"))
    env.monkeypatch.setattr(tasks, "enqueue_task",
                            lambda name, data=None, **k: enqueued.append((name, data)))
    env.monkeypatch.setattr(tasks.verification_lib, "verify",
                            lambda fp, depth, progress=None: (True, False, True, "> FILE IS CORRUPT"))
    f = env.seed()
    fid = f.id

    tasks.verify_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    assert f.signature_valid is True and f.hash_valid is False and f.hash_modified is True
    assert f.verification_error == "> FILE IS CORRUPT"
    assert f.verified_at is not None
    assert enqueued == [("process_file", {"file_id": fid})]


def test_verify_file_at_signature_depth_leaves_hash_untouched(env):
    """A shallow run must not overwrite a hash verdict a deeper run already produced."""
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda *a, **k: None)
    env.monkeypatch.setattr(tasks.verification_lib, "verify",
                            lambda fp, depth, progress=None: (True, None, None, None))
    f = env.seed(hash_valid=True)
    fid = f.id

    tasks.verify_file_task(file_id=fid)

    assert db.session.get(Files, fid).hash_valid is True


def test_verify_file_missing_source_noop(env):
    env.monkeypatch.setattr(tasks.verification_lib, "verify", lambda *a, **k: 1 / 0)
    f = env.seed()
    os.remove(f.filepath)
    tasks.verify_file_task(file_id=f.id)   # returns cleanly, does not call nstools


def test_verify_stage_converges(env):
    """The re-drive must not loop: once a verdict is stored the stage stops applying."""
    env.monkeypatch.setattr(tasks, "get_settings", lambda: _settings(verify=True, depth="hash"))
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda *a, **k: None)
    env.monkeypatch.setattr(tasks.verification_lib, "verify",
                            lambda fp, depth, progress=None: (False, False, False, "bad"))
    f = env.seed()
    fid = f.id

    tasks.verify_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    mgmt = _settings(verify=True, depth="hash")["library"]["management"]
    assert tasks._needs_verify(f, mgmt) is False


# --- invalidation ----------------------------------------------------------------------------

def test_content_change_clears_the_verdicts(env):
    """The verdicts describe the bytes that were there; new bytes need a new verdict."""
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda *a, **k: None)
    f = env.seed(signature_valid=True, hash_valid=True)
    fid, path = f.id, f.filepath
    with open(path, "wb") as fh:
        fh.write(b"DIFFERENT PAYLOAD")

    tasks.handle_file_added_task(library_path=str(env.lib_dir), filepath=path)

    f = db.session.get(Files, fid)
    assert f.signature_valid is None and f.hash_valid is None and f.hash_modified is None
    assert f.verification_error is None and f.verified_at is None


def test_compression_preserves_the_verdicts(env):
    """nsz round-trip-verifies NCA content hashes itself, so an NSP that verified good is
    still good as an NSZ - re-reading every byte again would buy nothing."""
    from containers import compression
    from test_compression import _stub_produce
    env.monkeypatch.setattr(tasks, "enqueue_task", lambda *a, **k: None)
    env.monkeypatch.setattr(compression, "compress_to", _stub_produce())
    f = env.seed(signature_valid=True, hash_valid=True)
    fid = f.id

    tasks.compress_file_task(file_id=fid)

    f = db.session.get(Files, fid)
    assert f.compressed is True
    assert f.signature_valid is True and f.hash_valid is True


def test_reset_file_verification_clears_every_column(env):
    f = env.seed(signature_valid=True, hash_valid=False, hash_modified=True)
    f.verification_error = "boom"
    reset_file_verification(f)
    db.session.commit()
    assert (f.signature_valid, f.hash_valid, f.hash_modified,
            f.verification_error, f.verified_at) == (None, None, None, None, None)
