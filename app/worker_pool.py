"""Dynamic pool of task worker subprocesses, shared by the Gunicorn (run.py) and local (local.py) entrypoints."""
import logging
import threading
from multiprocessing import Process, Event as MPEvent

logger = logging.getLogger('main')


class WorkerPool:
    """Manages a dynamic pool of task worker subprocesses."""

    def __init__(self, app, initial_count=1):
        self.app = app  # for app-context when reaping a stopped worker's running task
        self.workers = {}  # worker_id -> (Process, MPEvent)
        self._lock = threading.Lock()
        self._next_id = 1
        self._scale_to(initial_count)

    def _start_worker(self, worker_id=None):
        """Start a single worker process. Reuses worker_id if given, else allocates a new one."""
        from worker import start_worker_process
        if worker_id is None:
            worker_id = self._next_id
            self._next_id += 1
        stop_event = MPEvent()
        proc = Process(target=start_worker_process, args=(stop_event, worker_id))
        proc.start()
        self.workers[worker_id] = (proc, stop_event)
        logger.info(f'Worker-{worker_id} started (pid={proc.pid}).')
        return worker_id

    def _stop_worker(self, worker_id, force=False):
        """Stop a worker by ID. If force, terminate immediately instead of waiting for graceful exit."""
        if worker_id not in self.workers:
            return
        proc, stop_event = self.workers.pop(worker_id)
        if force:
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
        else:
            stop_event.set()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
        logger.info(f'Worker-{worker_id} stopped.')
        # Process is gone: reap any task it was running so its cleanup hook runs).
        from tasks import reap_worker_task
        with self.app.app_context():
            reap_worker_task(worker_id)

    def restart_worker(self, worker_id):
        """Forcefully stop a worker mid-task and start a replacement reusing the same id."""
        with self._lock:
            if worker_id not in self.workers:
                return False
            self._stop_worker(worker_id, force=True)
            self._start_worker(worker_id=worker_id)
            return True

    def _scale_to(self, desired_count):
        """Scale the pool to the desired number of workers."""
        current = len(self.workers)
        if desired_count > current:
            for _ in range(desired_count - current):
                self._start_worker()
        elif desired_count < current:
            # Stop the highest-numbered workers
            ids_to_stop = sorted(self.workers.keys(), reverse=True)[:current - desired_count]
            for wid in ids_to_stop:
                self._stop_worker(wid)

    def scale(self, desired_count):
        """Thread-safe scaling."""
        with self._lock:
            self._scale_to(desired_count)

    def shutdown(self):
        """Stop all workers."""
        with self._lock:
            for wid in list(self.workers.keys()):
                self._stop_worker(wid)

    @property
    def count(self):
        return len(self.workers)
