from constants import *
from utils import *
import time, os
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from types import SimpleNamespace
import logging

# Retrieve main logger
logger = logging.getLogger('main')


class Watcher:
    def __init__(self, callback):
        self.directories = set()  # Use a set to store directories
        self.callback = callback
        self.event_handler = Handler(self.callback)
        self.observer = PollingObserver()
        self.scheduler_map = {}

    def run(self):
        self.observer.start()
        logger.debug('Successfully started observer.')

    def stop(self):
        logger.debug('Stopping observer...')
        self.observer.stop()
        self.observer.join()
        logger.debug('Successfully stopped observer.')

    def add_directory(self, directory):
        if directory not in self.directories:
            if not os.path.exists(directory):
                logger.warning(f'Directory {directory} does not exist, not added to watchdog.')
                return False
            logger.info(f'Adding directory {directory} to watchdog.')
            task = self.observer.schedule(self.event_handler, directory, recursive=True)
            self.scheduler_map[directory] = task
            self.directories.add(directory)
            self.event_handler.add_directory(directory)
            return True
        return False
    
    def remove_directory(self, directory):
        logger.info(f'Removing {directory} from watchdog monitoring...')
        if directory in self.directories:
            if directory in self.scheduler_map:
                self.observer.unschedule(self.scheduler_map[directory])
                del self.scheduler_map[directory]
            self.directories.remove(directory)
            logger.info(f'Removed {directory} from watchdog monitoring.')
            return True
        else:
            logger.info(f'{directory} not in watchdog, nothing to do.')
        return False

class Handler(FileSystemEventHandler):
    def __init__(self, callback, stability_duration=5):
        self._raw_callback = callback  # Callback to invoke for stable files
        self.directories = []
        self.stability_duration = stability_duration  # Stability duration in seconds
        self.tracked_files = {}  # Tracks files being copied
        self.debounced_check_final = self._debounce(self._check_file_stability, stability_duration)

    def add_directory(self, directory):
        if directory not in self.directories:
            self.directories.append(directory)

    def _debounce(self, func, wait):
        """Debounce decorator for the stability check."""
        @debounce(wait)
        def debounced():
            func()
        return debounced

    def _track_file(self, event):
        """Start or update tracking for a file."""
        if event.type == 'moved':
            file_path = event.dest_path
        else:
            file_path = event.src_path
        current_size = os.path.getsize(file_path)
        if file_path not in self.tracked_files:
            event.size = current_size
            event.timestamp = time.time()
            self.tracked_files[file_path] = event
        else:
            self.tracked_files[file_path].size = current_size
            self.tracked_files[file_path].timestamp = time.time()

    def _check_file_stability(self):
        """Check for stable files and invoke the callback."""
        now = time.time()
        stable_files = []

        # Check all tracked files
        for file_path, file_data in list(self.tracked_files.items()):
            if not os.path.exists(file_path):
                # If the file no longer exists, stop tracking it
                del self.tracked_files[file_path]
                continue
            current_size = os.path.getsize(file_path)
            if current_size == file_data.size and (now - file_data.timestamp) >= self.stability_duration:
                stable_files.append(file_data)
                del self.tracked_files[file_path]  # Stop tracking stable file

        # Trigger the callback for all stable files
        if stable_files:
            self._raw_callback(stable_files)

    def collect_event(self, source_event, directory):
        """Track file events and trigger the stability check."""
        if source_event.is_directory:
            return

        if not any(source_event.src_path.endswith(ext) or source_event.dest_path.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            return

        library_event = SimpleNamespace(
            type=source_event.event_type,
            directory=directory,
            src_path=source_event.src_path,
            dest_path=source_event.dest_path,
        )

        if library_event.type == 'moved' and not any(library_event.dest_path.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            library_event.type = 'deleted'

        if library_event.type == 'deleted':
            self._raw_callback([library_event])

        else:
            # Track file on create or modify
            self._track_file(library_event)
            self.debounced_check_final()

        self._check_file_stability()

    def on_any_event(self, event):
        for directory in self.directories:
            if event.src_path.startswith(directory):
                self.collect_event(event, directory)
                break
