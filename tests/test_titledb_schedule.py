"""Tests for how the titledb task keeps itself scheduled.

Periodicity here is a chain, not a scheduler: each run enqueues the next one. So the thing
worth pinning down is that the chain survives a failure — nothing else re-enqueues a failed
task, and a dropped link leaves titledb frozen until the process restarts.
"""
import datetime

import pytest

import db as db_mod
import tasks as tasks_mod
from app import create_app
from db import Task, db, init_db


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """An app with an empty tasks table and a titledb update that does nothing."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)

    monkeypatch.setattr(tasks_mod, "get_settings", lambda: {"scheduler": {"titledb_update_interval": "12h"}})
    monkeypatch.setattr(tasks_mod, "add_missing_apps_to_db", lambda: None)
    monkeypatch.setattr(tasks_mod, "update_titles", lambda: None)
    with app.app_context():
        yield app


def scheduled():
    """The pending update_titledb rows, and how far out each one is."""
    rows = Task.query.filter_by(task_name="update_titledb", status="pending").all()
    now = datetime.datetime.utcnow()
    return [(row.run_after - now).total_seconds() / 3600 for row in rows]


def test_success_schedules_the_next_run_at_the_configured_interval(queue, monkeypatch):
    monkeypatch.setattr(tasks_mod.titledb, "update_titledb", lambda settings: None)

    tasks_mod.update_titledb_task()

    assert len(scheduled()) == 1
    assert scheduled()[0] == pytest.approx(12, abs=0.1)


def test_failure_reschedules_instead_of_dropping_the_chain(queue, monkeypatch):
    def boom(settings):
        raise ConnectionError("release unreachable")

    monkeypatch.setattr(tasks_mod.titledb, "update_titledb", boom)

    with pytest.raises(ConnectionError):
        tasks_mod.update_titledb_task()

    assert len(scheduled()) == 1, "a failed run must leave a follow-up queued"
    assert scheduled()[0] == pytest.approx(1, abs=0.1)


def test_repeated_runs_do_not_pile_up_rows(queue, monkeypatch):
    """enqueue_task would dedup against the pending row and silently keep the stale run_after."""
    monkeypatch.setattr(tasks_mod.titledb, "update_titledb", lambda settings: None)

    tasks_mod.update_titledb_task()
    db.session.query(Task).filter_by(task_name="update_titledb").update(
        {"run_after": datetime.datetime.utcnow() + datetime.timedelta(hours=99)})
    db.session.commit()
    tasks_mod.update_titledb_task()

    assert len(scheduled()) == 1
    assert scheduled()[0] == pytest.approx(12, abs=0.1), "the pending run_after must be moved, not kept"
