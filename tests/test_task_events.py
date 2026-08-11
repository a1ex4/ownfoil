"""Tests for the task/worker realtime topics.

The contract is the event stream a client sees: a snapshot to start from, then one
add/update/remove per thing that actually changed. Assertions are on those events, not
on how the diff is computed.
"""
import pytest

import db as db_mod
import task_events
from app import create_app
from db import Task, db, init_db


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """An app with an empty tasks table, and the topic state reset around each test."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(db_mod, "TITLES_DB_FILE", str(config / "titles.db"))

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    init_db(app)

    task_events._tasks_state = {}
    task_events._workers_state = {}
    with app.app_context():
        yield app


def add_task(**kwargs):
    """Insert a task row, defaulting the columns a caller does not care about."""
    fields = {"task_name": "scan_library", "status": "pending", "completion_pct": 0,
              "input_json": '{"library_path": "/games"}', "input_hash": "h"}
    fields.update(kwargs)
    task = Task(**fields)
    db.session.add(task)
    db.session.commit()
    return task


def by_type(events):
    """Events grouped as {type: [task ids]}, which is what callers actually react to."""
    grouped = {}
    for event_type, data in events:
        grouped.setdefault(event_type, []).append(data["id"])
    return grouped


# --- Diffing ---

def test_the_first_poll_reports_everything_as_new(queue):
    task = add_task()
    assert by_type(task_events.tasks_poll()) == {"add": [task.id]}


def test_an_unchanged_queue_produces_no_events(queue):
    add_task()
    task_events.tasks_poll()
    assert task_events.tasks_poll() == []


CHANGES = [
    ("progress moves", {"completion_pct": 42}),
    ("a worker claims it", {"status": "running", "worker_id": 3}),
    ("it fails", {"status": "failed", "error_message": "boom"}),
]


@pytest.mark.parametrize("label,change", CHANGES, ids=[c[0] for c in CHANGES])
def test_a_changed_row_is_reported_once(queue, label, change):
    task = add_task()
    task_events.tasks_poll()

    for field, value in change.items():
        setattr(task, field, value)
    db.session.commit()

    assert by_type(task_events.tasks_poll()) == {"update": [task.id]}
    assert task_events.tasks_poll() == []  # and not again


def test_a_deleted_row_is_reported_as_removed(queue):
    task = add_task()
    task_events.tasks_poll()
    task_id = task.id

    db.session.delete(task)
    db.session.commit()

    assert by_type(task_events.tasks_poll()) == {"remove": [task_id]}


def test_only_the_rows_that_changed_are_reported(queue):
    quiet = add_task()
    busy = add_task(input_hash="other")
    task_events.tasks_poll()

    busy.completion_pct = 10
    db.session.commit()

    events = by_type(task_events.tasks_poll())
    assert events == {"update": [busy.id]}
    assert quiet.id not in events.get("update", [])


# --- Payload ---

def test_a_snapshot_carries_the_queue(queue):
    add_task()
    add_task(input_hash="other")
    assert len(task_events.tasks_snapshot()) == 2


def test_a_long_queue_is_capped(queue, monkeypatch):
    """A library scan queues one task per file; a client is sent a window, not all of it."""
    monkeypatch.setattr(task_events, "MAX_TASKS", 5)
    for n in range(12):
        add_task(input_hash="h%d" % n)

    snapshot = task_events.tasks_snapshot()
    assert len(snapshot) == 5


def test_the_window_holds_the_oldest_tasks(queue, monkeypatch):
    """Tasks are claimed oldest-first, so the window is what runs now and what is next."""
    monkeypatch.setattr(task_events, "MAX_TASKS", 3)
    ids = [add_task(input_hash="h%d" % n).id for n in range(8)]

    assert [t["id"] for t in task_events.tasks_snapshot()] == ids[:3]


def test_work_completing_pulls_the_next_tasks_into_the_window(queue, monkeypatch):
    monkeypatch.setattr(task_events, "MAX_TASKS", 3)
    ids = [add_task(input_hash="h%d" % n).id for n in range(6)]
    task_events.tasks_poll()

    db.session.query(Task).filter(Task.id == ids[0]).delete()
    db.session.commit()

    assert by_type(task_events.tasks_poll()) == {"remove": [ids[0]], "add": [ids[3]]}


def test_a_snapshot_does_not_swallow_events_owed_to_other_clients(queue):
    """A client joining mid-tick must not absorb changes the poller has yet to emit."""
    task = add_task()
    task_events.tasks_poll()

    task.completion_pct = 50
    db.session.commit()
    task_events.tasks_snapshot()  # a second client connects here

    assert by_type(task_events.tasks_poll()) == {"update": [task.id]}


def test_tasks_carry_their_display_name_and_parentage(queue):
    parent = add_task(task_name="scan_libraries", input_json="{}")
    child = add_task(parent_id=parent.id, input_hash="child")

    payload = {t["id"]: t for t in task_events.tasks_snapshot()}
    assert payload[parent.id]["displayName"] == "Scan all libraries"
    assert payload[child.id]["displayName"] == "Scan /games"
    assert payload[child.id]["parentId"] == parent.id


def test_timestamps_are_marked_as_utc(queue):
    """Naive UTC in the database would otherwise be read as local time by a browser."""
    add_task()
    created = task_events.tasks_snapshot()[0]["createdAt"]
    assert created.endswith("Z") and "T" in created


def test_a_file_task_is_labelled_with_its_filename(queue):
    """Resolved in the same query as the tasks, so a busy queue is not a query per task."""
    from db import Files, Libraries

    library = Libraries(path="/games")
    db.session.add(library)
    db.session.flush()
    file_obj = Files(library_id=library.id, filepath="/games/Some Game.nsp",
                     folder="/games", filename="Some Game.nsp", extension="nsp")
    db.session.add(file_obj)
    db.session.commit()

    add_task(task_name="compress_file", input_json='{"file_id": %d}' % file_obj.id)
    assert task_events.tasks_snapshot()[0]["displayName"] == "Compress Some Game.nsp"


def test_a_file_task_whose_file_is_gone_still_gets_a_label(queue):
    add_task(task_name="verify_file", input_json='{"file_id": 999}')
    assert task_events.tasks_snapshot()[0]["displayName"] == "Verify file #999"


def test_listing_tasks_does_not_query_per_task(queue):
    """A scan queues one task per file. Resolving each label with its own query made a
    3000-task snapshot take 1.8s - well past the poll interval it has to fit inside."""
    for n in range(30):
        add_task(task_name="process_file", input_json='{"file_id": %d}' % n,
                 input_hash="h%d" % n)

    from sqlalchemy import event

    queries = []

    def record(conn, cursor, statement, *args):
        queries.append(statement)

    # The queue itself is read through a raw DBAPI connection, which bypasses this hook -
    # so what it catches is exactly the per-task ORM lookups that must not happen.
    engine = db.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        task_events.tasks_snapshot()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert queries == [], f"{len(queries)} per-task queries for 30 tasks"


def test_a_task_whose_input_no_longer_fits_still_gets_a_label(queue):
    add_task(task_name="scan_library", input_json='{"gone": true}')
    assert task_events.tasks_snapshot()[0]["displayName"] == "Scan library"


# --- Workers ---

class FakeProcess:
    def __init__(self, pid, alive=True):
        self.pid = pid
        self._alive = alive

    def is_alive(self):
        return self._alive


@pytest.fixture
def pool(monkeypatch):
    import app as app_mod

    fake = type("Pool", (), {"workers": {1: (FakeProcess(101), None),
                                         2: (FakeProcess(102, alive=False), None)}})()
    monkeypatch.setattr(app_mod, "pool", fake)
    return fake


def test_workers_report_liveness_and_pid(queue, pool):
    workers = {w["id"]: w for w in task_events.workers_snapshot()}
    assert (workers[1]["pid"], workers[1]["alive"]) == (101, True)
    assert workers[2]["alive"] is False


def test_a_worker_is_linked_to_the_task_it_is_running(queue, pool):
    task = add_task(status="running", worker_id=1)
    workers = {w["id"]: w for w in task_events.workers_snapshot()}
    assert workers[1]["taskId"] == task.id
    assert workers[2]["taskId"] is None


def test_a_worker_picking_up_a_task_is_an_update(queue, pool):
    task_events.workers_poll()
    add_task(status="running", worker_id=1)
    assert by_type(task_events.workers_poll()) == {"update": [1]}


def test_workers_are_empty_without_a_pool(queue, monkeypatch):
    import app as app_mod
    monkeypatch.setattr(app_mod, "pool", None)
    assert task_events.workers_snapshot() == []
