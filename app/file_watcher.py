from constants import *
import time, os
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
import threading
from functools import wraps
import logging

# Retrieve main logger
logger = logging.getLogger('main')


def is_dict_in_list(dict_list, dictionary):
    for item in dict_list:
        if item == dictionary:
            return True
    return False

def debounce(wait):
    """Decorator that postpones a function's execution until after `wait` seconds
    have elapsed since the last time it was invoked."""
    def decorator(fn):
        @wraps(fn)
        def debounced(*args, **kwargs):
            def call_it():
                fn(*args, **kwargs)
            if hasattr(debounced, '_timer'):
                debounced._timer.cancel()
            debounced._timer = threading.Timer(wait, call_it)
            debounced._timer.start()
        return debounced
    return decorator

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
    def __init__(self, callback, debounce_time=5):
        self._raw_callback = callback  # The actual callback passed to the handler
        self.directories = []
        self.debounce_time = debounce_time
        self.events_to_process = {
            'modified': [],
            'created': [],
            'deleted': [],
            'moved': []
        }
        self.debounced_process_events = self.debounce_callback(self._process_collected_events, debounce_time)

    def add_directory(self, directory):
        if directory not in self.directories:
            self.directories.append(directory)

    def debounce_callback(self, callback, wait):
        @debounce(wait)
        def debounced_callback():
            callback()
        return debounced_callback

    def _process_collected_events(self):
        if any(self.events_to_process.values()):  # Check if any list has events
            self._raw_callback(self.events_to_process)
            # Reset the events_to_process dictionary
            self.events_to_process = {
                'modified': [],
                'created': [],
                'deleted': [],
                'moved': []
            }

    def collect_event(self, event, directory):
        if event.is_directory:
            return

        if event.event_type in ['deleted', 'moved', 'created']:
            file_extension = os.path.splitext(event.src_path)[1][1:]
            if file_extension not in ALLOWED_EXTENSIONS:
                return

            event_slim = {
                'directory': directory,
                'dest_path': event.dest_path,
                'src_path': event.src_path
            }
            if not is_dict_in_list(self.events_to_process[event.event_type], event_slim):
                self.events_to_process[event.event_type].append(event_slim)
                self.debounced_process_events()  # Trigger the debounce mechanism

    def on_modified(self, event):
        for directory in self.directories:
            if event.src_path.startswith(directory):
                self.collect_event(event, directory)

    def on_created(self, event):
        for directory in self.directories:
            if event.src_path.startswith(directory):
                self.collect_event(event, directory)

    def on_deleted(self, event):
        for directory in self.directories:
            if event.src_path.startswith(directory):
                self.collect_event(event, directory)

    def on_moved(self, event):
        for directory in self.directories:
            if event.src_path.startswith(directory):
                self.collect_event(event, directory)
