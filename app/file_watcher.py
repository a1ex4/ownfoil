from constants import *
from utils import *
import time, os, threading
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
from types import SimpleNamespace
import logging

# Retrieve main logger
logger = logging.getLogger('main')

# Force-walk a newly created subdirectory for this long before assuming its inotify watch is
# reliable; after the window a media-less folder graduates to per-file events (stops re-walking).
NEW_DIR_GRACE_SECONDS = 300


class Watcher:
    def __init__(self, callback):
        self.directories = set()  # Use a set to store directories
        self.callback = callback
        self.event_handler = Handler(self.callback)
        # Local paths (and the config file) share one native observer (inotify/FSEvents);
        # each network path gets a dedicated PollingObserver with its own interval.
        self.native = Observer()
        self.scheduler_map = {}  # directory -> (observer, watch)
        self._lock = threading.RLock()  # guards scheduler_map/directories/_watcher_config
        self._started = False
        self._watcher_config = self._snapshot_config()  # last-seen global watcher config

    def run(self):
        self._started = True
        # Start the native observer plus any dedicated pollers already scheduled before startup.
        for obs in {self.native, *(obs for obs, _ in self.scheduler_map.values())}:
            if not obs.is_alive():
                obs.start()
        logger.debug('Successfully started observers.')

    def stop(self):
        logger.debug('Stopping observers...')
        observers = {self.native, *(obs for obs, _ in self.scheduler_map.values())}
        for obs in observers:
            obs.stop()
        for obs in observers:
            obs.join()
        logger.debug('Successfully stopped observers.')

    def add_directory(self, directory):
        from settings import get_watcher_config
        with self._lock:
            if directory in self.directories:
                return False
            if not os.path.exists(directory):
                logger.warning(f'Directory {directory} does not exist, not added to watchdog.')
                return False
            config = get_watcher_config()
            if not config['enabled']:
                logger.info(f'File watcher disabled for {directory}, not monitoring.')
                return False
            network = is_network_path(directory)
            if network:
                observer = PollingObserver(timeout=config['polling_interval'])
                if self._started:
                    observer.start()
                logger.info(f'Watching {directory} via polling every {config["polling_interval"]}s (network filesystem).')
            else:
                observer = self.native
                logger.info(f'Watching {directory} via native observer (local filesystem).')
            watch = observer.schedule(self.event_handler, directory, recursive=True)
            self.scheduler_map[directory] = (observer, watch)
            self.directories.add(directory)
            self.event_handler.add_directory(directory)
            return True

    def add_file_callback(self, filepath, callback):
        """Watch a single file via its parent directory; invoke callback() on change."""
        parent = os.path.dirname(os.path.abspath(filepath)) or '.'
        handler = _FileCallbackHandler(filepath, callback)
        self.native.schedule(handler, parent, recursive=False)
        logger.debug(f'Watching {filepath} for changes.')

    def remove_directory(self, directory):
        logger.debug(f'Removing {directory} from watchdog monitoring...')
        with self._lock:
            if directory not in self.directories:
                logger.debug(f'{directory} not in watchdog, nothing to do.')
                return False
            observer, watch = self.scheduler_map.pop(directory)
            observer.unschedule(watch)
            if observer is not self.native:
                observer.stop()
                observer.join()
            self.directories.remove(directory)
            self.event_handler.remove_directory(directory)
            logger.info(f'Removed {directory} from watchdog monitoring.')
            return True

    def _snapshot_config(self):
        """Current global watcher config (enabled/interval)."""
        from settings import get_watcher_config
        return get_watcher_config()

    def reconcile(self):
        """Re-apply a global watcher config change (enable/disable, interval) to every library path.
        Path add/remove is handled by the library API, not here.
        Must run off the observer dispatch thread (schedule/unschedule take the observer lock)."""
        from settings import get_library_paths
        with self._lock:
            current = self._snapshot_config()
            if current == self._watcher_config:
                return
            logger.info('Watcher config changed, re-applying to all library paths.')
            for path in get_library_paths():
                if path in self.directories:
                    self.remove_directory(path)
                if current['enabled']:
                    self.add_directory(path)
            self._watcher_config = current

class _FileCallbackHandler(FileSystemEventHandler):
    def __init__(self, filepath, callback):
        self.filepath = os.path.abspath(filepath)
        self.callback = callback

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Match src or dest: an atomic save (os.replace of a temp file onto the target)
        # arrives as a moved event whose dest_path — not src_path — is the watched file.
        paths = [event.src_path, getattr(event, 'dest_path', '')]
        if self.filepath not in [os.path.abspath(p) for p in paths if p]:
            return
        try:
            self.callback()
        except Exception as e:
            logger.error(f'File callback error for {self.filepath}: {e}')


class Handler(FileSystemEventHandler):
    def __init__(self, callback, stability_duration=5):
        self._raw_callback = callback  # Callback to invoke for stable files
        self.directories = []
        self.stability_duration = stability_duration  # Stability duration in seconds
        self.tracked_files = {}  # Tracks files being copied
        self.new_dirs = {}  # newly created subdir -> grace expiry; force-walked during the race window
        self.pending_dir_walks = {}  # subdirectory -> watched root, awaiting a settle-then-walk
        self.dir_walk_lock = threading.Lock()
        self.debounced_check_final = self._debounce(self._check_file_stability, stability_duration, f'stability-{id(self)}')
        self.debounced_dir_walk = self._debounce(self._flush_dir_walks, stability_duration, f'dirwalk-{id(self)}')

    def add_directory(self, directory):
        if directory not in self.directories:
            self.directories.append(directory)

    def remove_directory(self, directory):
        if directory in self.directories:
            self.directories.remove(directory)

    def _debounce(self, func, wait, key):
        """Debounce decorator for the stability/dir-walk checks."""
        @debounce(wait, key=key)
        def debounced():
            func()
        return debounced

    def _track_file(self, event):
        """Start or update tracking for a file."""
        if event.type == 'moved':
            file_path = event.dest_path
        else:
            file_path = event.src_path
        try:
            current_size = os.path.getsize(file_path)
        except OSError:
            # The file vanished between its event and this call (e.g. a conversion's
            # transient output). Ignore it rather than let the observer thread die.
            return
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
            if current_size != file_data.size:
                # Still growing (e.g. an ongoing copy with no further events): reset the window.
                file_data.size = current_size
                file_data.timestamp = now
            elif (now - file_data.timestamp) >= self.stability_duration:
                stable_files.append(file_data)
                del self.tracked_files[file_path]  # Stop tracking stable file

        # Trigger the callback for all stable files
        if stable_files:
            self._raw_callback(stable_files)

        # Re-arm while files remain: walk-discovered files get no further events to drive re-checks.
        if self.tracked_files:
            self.debounced_check_final()

    def _track_directory_files(self, dirpath, directory):
        """Track every allowed file under a newly appeared directory, so a folder moved in
        goes through the same stability check as individual files (mid-copy safe). Returns count."""
        tracked = 0
        for root, _, files in os.walk(dirpath):
            for name in files:
                if not any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
                    continue
                path = os.path.join(root, name)
                if os.path.exists(path):
                    self._track_file(SimpleNamespace(type='created', directory=directory,
                                                     src_path=path, dest_path=''))
                    tracked += 1
        if tracked:
            self.debounced_check_final()
        return tracked

    def _flush_dir_walks(self):
        """Walk directories that recently appeared to discover files inotify may have missed
        (new-subdirectory watch race, cross-filesystem copies). A directory graduates out of
        'new' only once a walk actually finds files (proof its watch is live and it is populated);
        an empty walk keeps it 'new' so a later modify (files finally arriving) re-walks it."""
        with self.dir_walk_lock:
            pending = dict(self.pending_dir_walks)
            self.pending_dir_walks.clear()
        graduated = []
        for dirpath, root in pending.items():
            if not os.path.isdir(dirpath):
                graduated.append(dirpath)
            elif self._track_directory_files(dirpath, root):
                graduated.append(dirpath)  # files found: watch is live, stop force-walking
        now = time.time()
        with self.dir_walk_lock:
            for d in graduated:
                self.new_dirs.pop(d, None)
            # prune dirs whose grace window elapsed without yielding media, to bound the map
            for d in [k for k, expiry in self.new_dirs.items() if now >= expiry]:
                self.new_dirs.pop(d, None)

    def collect_event(self, source_event, directory):
        """Track file events and trigger the stability check."""
        if source_event.is_directory:
            # A folder moved out emits only this dir event (no per-file deletes), so remove its
            # files by prefix. A folder appearing/being populated (a cross-drive copy fires
            # DirCreated on an empty folder, then per-file events are lost to the inotify
            # subdir-watch race) is walked once activity settles. Established subdirs and renames
            # within the tree emit reliable per-file events, so they need no walk here.
            src = source_event.src_path
            is_root = src.rstrip('/') == directory.rstrip('/')
            if source_event.event_type == 'deleted':
                with self.dir_walk_lock:
                    self.new_dirs.pop(src, None)
                self._raw_callback([SimpleNamespace(type='dir_deleted', directory=directory,
                                                    src_path=src, dest_path='')])
            elif source_event.event_type in ('created', 'modified') and not is_root:
                with self.dir_walk_lock:
                    if source_event.event_type == 'created':
                        self.new_dirs[src] = time.time() + NEW_DIR_GRACE_SECONDS
                    elif src not in self.new_dirs or time.time() >= self.new_dirs[src]:
                        # not new, or its grace window elapsed: watch is reliable, use per-file events
                        self.new_dirs.pop(src, None)
                        return
                    self.pending_dir_walks[src] = directory
                self.debounced_dir_walk()
            return

        # Only content events matter downstream; ignore opened/closed/etc. (native inotify emits
        # these on mere reads and around writes, where they would otherwise mask the real type).
        if source_event.event_type not in ('created', 'modified', 'moved', 'deleted'):
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
