import threading
import time
import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask
from croniter import croniter, CroniterBadCronError

logger = logging.getLogger('main')

class JobScheduler:
    def __init__(self, app: Flask, max_workers: int = 4):
        self.app = app
        self._lock = threading.RLock()
        self.scheduled_jobs: Dict[str, Dict[str, Any]] = {}
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._running = True
        self._sleep_time = 1  # seconds
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Job scheduler initialized.")

    def _run_loop(self):
        with self.app.app_context():
            logger.debug("Scheduler loop started.")
            try:
                self._check_jobs()
            except Exception as e:
                logger.error(f"Initial scheduler check failed: {e}")
            while self._running:
                try:
                    self._check_jobs()
                except Exception as e:
                    logger.error(f"Scheduler loop error: {str(e)}")
                time.sleep(self._sleep_time)
            logger.info("Scheduler loop exited.")

    def _check_jobs(self):
        now = datetime.now()
        with self._lock:
            for job_id, job in list(self.scheduled_jobs.items()):
                if job['next_run'] <= now:
                    self._execute_job(job)
                    self._reschedule(job)

    def _execute_job(self, job: Dict[str, Any]):
        def job_wrapper():
            with self.app.app_context():
                try:
                    logger.info(f"Starting job {job['id']}")
                    job['func'](*job.get('args', []), **job.get('kwargs', {}))
                    with self._lock:
                        job['last_run'] = datetime.now()
                        job['last_error'] = None
                    schedule_info = f" Next run at {job['next_run']}" if not job.get('run_once') else ""
                    logger.info(f"Completed job {job['id']}.{schedule_info}")
                except Exception as e:
                    with self._lock:
                        job['last_error'] = str(e)
                    schedule_info = f" Next run at {job['next_run']}" if not job.get('run_once') else ""
                    logger.error(f"Job {job['id']} failed: {e}.{schedule_info}")

        self.executor.submit(job_wrapper)

    def _reschedule(self, job: Dict[str, Any]):
        if job.get('run_once'):
            with self._lock:
                del self.scheduled_jobs[job['id']]
                logger.info(f"Job {job['id']} completed and removed (one-off).")
        else:
            now = datetime.now()
            if job['interval']:
                job['next_run'] = now + job['interval']
            elif job['cron']:
                job['next_run'] = self._next_cron(job['cron'])

    def _next_cron(self, cron_expr: str) -> datetime:
        try:
            base = datetime.now()
            return croniter(cron_expr, base).get_next(datetime)
        except CroniterBadCronError:
            raise ValueError(f"Invalid cron expression: {cron_expr}")

    def add_job(
        self,
        job_id: str,
        func: Callable,
        cron: Optional[str] = None,
        interval: Optional[timedelta] = None,
        args: tuple = (),
        kwargs: Optional[Dict[str, Any]] = None,
        run_once: bool = False,
        run_first: bool = False # New parameter
    ):
        with self._lock:
            if job_id in self.scheduled_jobs:
                raise ValueError(f"Job {job_id} already exists.")

            if not (cron or interval or run_once):
                raise ValueError("Must provide either cron, interval, or run_once=True.")

            if run_once or run_first: # If run_once or run_first is True, execute immediately
                next_run = datetime.now()
            elif cron:
                next_run = self._next_cron(cron)
            elif interval:
                next_run = datetime.now() + interval # Run after the first interval
            else:
                # This case should ideally not be reached due to the initial check
                next_run = datetime.now() 

            self.scheduled_jobs[job_id] = {
                'id': job_id,
                'func': func,
                'cron': cron,
                'interval': interval,
                'args': args,
                'kwargs': kwargs or {},
                'next_run': next_run,
                'run_once': run_once,
                'last_run': None,
                'last_error': None
            }

            schedule_info = f"cron: {cron}" if cron else f"interval: {interval}" if interval else "one-off"
            logger.info(f"Added job {job_id} with schedule: {schedule_info}, first run at {next_run}")

    def remove_job(self, job_id: str):
        with self._lock:
            if job_id in self.scheduled_jobs:
                del self.scheduled_jobs[job_id]
                logger.info(f"Removed job {job_id}.")

    def shutdown(self):
        self._running = False
        self.executor.shutdown(wait=False)
        logger.debug("Job scheduler shutdown.")

def init_scheduler(app: Flask):
    app.scheduler = JobScheduler(app)

# Generic parallel runner
def run_task_parallel(
    inputs: List[Any],
    func: Callable[[Any], Any],
    max_threads: int = 4,
    app: Optional[Flask] = None
):
    """
    Run a task in parallel across a list of inputs.
    Each thread will call `func(item)`.

    - If `app` is provided, runs each task inside `app.app_context()`.
    - Logs errors individually.
    """
    results = []
    def wrapper(input_item):
        try:
            if app:
                with app.app_context():
                    return func(input_item)
            else:
                return func(input_item)
        except Exception as e:
            logger.error(f"Error processing {input_item}: {e}")
            return None

    with ThreadPoolExecutor(max_threads) as executor:
        futures = [executor.submit(wrapper, item) for item in inputs]
        for f in as_completed(futures):
            results.append(f.result())

    return results
