"""Task workers never outlive the process that owns their pool, and a new pool reaps what they left."""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from db import db, Task
from worker_pool import WorkerPool

from app import create_app

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))

# A starter that spawns a worker running the real watchdog, then dies by SIGKILL: no atexit,
# no finally, no worker_exit hook - the case nothing else would clean up.
STARTER = textwrap.dedent("""
    import multiprocessing as mp, os, signal, sys, time
    sys.path.insert(0, {app_dir!r})
    import worker

    def busy(ready):
        worker.exit_with_parent()
        ready.set()
        while True:  # a long task that never looks at stop_event
            time.sleep(1)

    if __name__ == '__main__':
        mp.set_start_method({method!r})
        ready = mp.Event()
        p = mp.Process(target=busy, args=(ready,))
        p.start()
        # A file, not stdout: an orphan would inherit the pipe and keep it open.
        with open(sys.argv[1], 'w') as fh:
            fh.write(f'{{p.pid}} {{ready.wait(30) and p.is_alive()}}')
        os.kill(os.getpid(), signal.SIGKILL)
""")


def _alive(pid):
    try:
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("method", ["forkserver", "spawn"])
def test_a_worker_exits_when_its_owner_is_killed(tmp_path, method):
    starter = tmp_path / "starter.py"
    starter.write_text(STARTER.format(app_dir=APP_DIR, method=method))
    report = tmp_path / "worker.txt"
    subprocess.run([sys.executable, str(starter), str(report)], timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid, running = report.read_text().split()
    child = int(pid)
    assert running == "True"  # the worker got past the watchdog and was working

    deadline = time.time() + 10
    while _alive(child) and time.time() < deadline:
        time.sleep(0.1)
    try:
        assert not _alive(child)
    finally:
        if _alive(child):
            os.kill(child, 9)


# (case, tasks as (status, worker_id), statuses after a new pool starts)
REAP_CASES = [
    ("a task left running is failed", [("running", 1)], ["failed"]),
    ("orphans sharing a worker id are all failed",
     [("running", 1), ("running", 1), ("running", 2)], ["failed", "failed", "failed"]),
    ("queued and finished tasks are left alone",
     [("pending", None), ("failed", 1)], ["pending", "failed"]),
]


@pytest.mark.parametrize("case,rows,expected", REAP_CASES, ids=[c[0] for c in REAP_CASES])
def test_a_new_pool_reaps_tasks_left_running(tmp_path, case, rows, expected):
    app = create_app(f"sqlite:///{tmp_path/'test.db'}")
    with app.app_context():
        db.create_all()
        tasks = [Task(task_name="process_file", status=status, worker_id=worker_id,
                      input_json="{}", input_hash=str(i)) for i, (status, worker_id) in enumerate(rows)]
        db.session.add_all(tasks)
        db.session.commit()
        ids = [t.id for t in tasks]

    WorkerPool(app, initial_count=0)

    with app.app_context():
        assert [db.session.get(Task, i).status for i in ids] == expected
