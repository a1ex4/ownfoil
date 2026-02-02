import threading
import time
import logging
import re
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask
from croniter import croniter, CroniterBadCronError

logger = logging.getLogger('main')

# Generic interval parsing utilities
def parse_interval_string(interval_str: str) -> Tuple[int, str]:
    """ Parse interval string like '2h', '30m', '1d', '45s' or '0' into (value, unit).:
        Tuple of (interval_value, unit_letter)
        Returns (0, 'h') if interval is '0' or invalid
        
    Examples:
        '2h' -> (2, 'h')
        '30m' -> (30, 'm')
        '0' -> (0, 'h')
    """
    if not interval_str or interval_str == '0':
        return 0, 'h'
    
    match = re.match(r'^(\d+)([smhd])$', str(interval_str))
    if match:
        return int(match.group(1)), match.group(2)
    
    # Invalid format - return disabled
    return 0, 'h'

def validate_interval_string(interval_str: str) -> Tuple[bool, Optional[str]]:
    """
    Validate interval string format.
    
    Args:
        interval_str: Interval string to validate
        
    Returns:
        Tuple of (is_valid, error_message)
        If valid, error_message is None
    """
    if interval_str == '0':
        return True, None
    
    if re.match(r'^\d+[smhd]$', str(interval_str)):
        return True, None
    
    return False, 'Interval must be in format: number+unit (e.g., "2h", "30m", "1d", "45s") or "0" to disable'

def interval_string_to_timedelta(interval_str: str) -> Optional[timedelta]:
    """
    Convert interval string to timedelta object.
    
    Args:
        interval_str: Interval string in format: number + unit (s/m/h/d)
        
    Returns:
        timedelta object or None if interval is '0' or invalid
        
    Examples:
        '2h' -> timedelta(hours=2)
        '30m' -> timedelta(minutes=30)
        '0' -> None
    """
    interval_value, unit = parse_interval_string(interval_str)
    
    if interval_value == 0:
        return None
    
    unit_map = {
        's': 'seconds',
        'm': 'minutes',
        'h': 'hours',
        'd': 'days'
    }
    
    timedelta_unit = unit_map.get(unit, 'hours')
    return timedelta(**{timedelta_unit: interval_value})

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
        now = datetime.now().replace(microsecond=0)
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
                        job['last_run'] = datetime.now().replace(microsecond=0)
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
            now = datetime.now().replace(microsecond=0)
            if job['interval']:
                job['next_run'] = now + job['interval']
            elif job['cron']:
                job['next_run'] = self._next_cron(job['cron'])

    def _next_cron(self, cron_expr: str) -> datetime:
        try:
            base = datetime.now().replace(microsecond=0)
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
        run_first: bool = False,
        start_date: Optional[datetime] = None # for delayed one-off jobs
    ):
        with self._lock:
            if job_id in self.scheduled_jobs:
                raise ValueError(f"Job {job_id} already exists.")

            if not (cron or interval or run_once):
                raise ValueError("Must provide either cron, interval, or run_once=True.")
            
            now = datetime.now().replace(microsecond=0)
            if run_once:
                if start_date:
                    next_run = start_date
                else:
                    next_run = now
            elif run_first: # If run_first is True, execute immediately
                next_run = now
            elif cron:
                next_run = self._next_cron(cron)
            elif interval:
                next_run = now + interval # Run after the first interval
            else:
                # This case should ideally not be reached due to the initial check
                next_run = now

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

    def update_job_interval(self, job_id: str, interval_str: str, func: Callable, run_first: bool = False):
        """
        Update or add a job with an interval string (e.g., '2h', '30m', '0').
        If interval is '0', the job is removed. Otherwise, it's rescheduled.
        
        Args:
            job_id: Unique identifier for the job
            interval_str: Interval string (e.g., '2h', '30m', '1d', '0')
            func: Function to execute
            run_first: Whether to run immediately on first schedule
            
        Returns:
            True if job was scheduled, False if disabled (interval='0')
        """
        # Remove existing job if it exists
        try:
            self.remove_job(job_id)
        except:
            pass
        
        # Parse and validate interval
        interval_delta = interval_string_to_timedelta(interval_str)
        
        if interval_delta is None:
            # Job disabled
            logger.info(f"Job {job_id} disabled (interval set to '0')")
            return False
        
        # Add job with new interval
        self.add_job(
            job_id=job_id,
            func=func,
            interval=interval_delta,
            run_first=run_first
        )
        logger.info(f"Job {job_id} scheduled with interval: {interval_str}")
        return True

    def shutdown(self):
        self._running = False
        self.executor.shutdown(wait=False)
        logger.debug("Job scheduler shutdown.")

def init_scheduler(app: Flask):
    app.scheduler = JobScheduler(app)

# Scheduled job definitions
def update_and_scan_job():
    """Combined job: updates TitleDB then scans library"""
    import app as app_module
    from settings import load_settings
    import titledb
    
    logger.info("Running update job (TitleDB update and library scan)...")
    
    # Update TitleDB with locking
    with app_module.titledb_update_lock:
        app_module.is_titledb_update_running = True
    
    logger.info("Starting TitleDB update...")
    try:
        settings = load_settings()
        titledb.update_titledb(settings)
        logger.info("TitleDB update completed.")
    except Exception as e:
        logger.error(f"Error during TitleDB update: {e}")
    finally:
        with app_module.titledb_update_lock:
            app_module.is_titledb_update_running = False
    
    # Check if update is still running before scanning
    with app_module.titledb_update_lock:
        if app_module.is_titledb_update_running:
            logger.info("Skipping library scan: TitleDB update still in progress.")
            return
    
    # Scan library with locking
    logger.info("Starting library scan...")
    with app_module.scan_lock:
        if app_module.scan_in_progress:
            logger.info('Skipping library scan: scan already in progress.')
            return
        app_module.scan_in_progress = True
    
    try:
        app_module.scan_library()
        app_module.post_library_change()
        logger.info("Library scan completed.")
    except Exception as e:
        logger.error(f"Error during library scan: {e}")
    finally:
        with app_module.scan_lock:
            app_module.scan_in_progress = False
    
    logger.info("Update job completed.")

def schedule_update_and_scan_job(app: Flask, interval_str: str, run_first: bool = True):
    """Schedule or update the update_and_scan job"""
    app.scheduler.update_job_interval(
        job_id='update_db_and_scan',
        interval_str=interval_str,
        func=update_and_scan_job,
        run_first=run_first
    )

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
