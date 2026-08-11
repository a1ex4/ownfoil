"""Generic realtime pub/sub over a single multiplexed WebSocket.

Knows nothing about what is being published. Producers register a topic and either
push events as they happen, or expose a `poll` callable the broker ticks for them —
needed when the state lives in another process and only surfaces through the database.

Every subscriber gets a snapshot of each topic on connect, so a client never needs a
separate seed query and a reconnect self-heals.
"""
import json
import logging
import queue
import threading
from collections import namedtuple

logger = logging.getLogger('main')

# Ticks per second the polled topics are re-read at. This interval *is* the update
# throttle: state cannot reach a client faster than the broker reads it.
POLL_INTERVAL = 0.25

# How long a client thread blocks before looking up from the queue to notice a socket
# that went away without a close frame.
IDLE_TIMEOUT = 1.0

# Events buffered per client before it is considered too slow to keep in sync.
QUEUE_SIZE = 100

# `access` is a role name understood by User.has_access, or None for public.
# `snapshot()` returns the topic's full current state; `poll()` returns an iterable of
# (event_type, data) pairs, or None for push-only topics.
Topic = namedtuple('Topic', 'name access snapshot poll')

TOPICS = {}

# Queued at shutdown to wake a client thread that is idling on its queue. The flag is
# what actually ends the loop; this only saves it the wait.
_SHUTDOWN = object()

_app = None
_subscribers = set()
_lock = threading.Lock()
_poller = None
_stop = threading.Event()
_shutdown = threading.Event()


class Subscription:
    """One client's event queue, scoped to the topics it is allowed to see."""

    def __init__(self, topics):
        self.topics = set(topics)
        self.queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.stale = set()  # topics awaiting a snapshot; their events are dropped


def init_app(app):
    """Bind the Flask app whose context polled sources run in."""
    global _app
    _app = app


def register_topic(name, access=None, snapshot=None, poll=None):
    """Register a topic clients can subscribe to."""
    TOPICS[name] = Topic(name, access, snapshot, poll)


def publish(topic, event_type, data):
    """Fan an event out to every subscriber of a topic."""
    event = {'topic': topic, 'type': event_type, 'data': data}
    with _lock:
        targets = [s for s in _subscribers if topic in s.topics]
    for sub in targets:
        _offer(sub, event)


def _offer(sub, event):
    """Queue an event for one client, asking it to resync if it fell too far behind."""
    if event['topic'] in sub.stale:
        # Already owed a snapshot, which supersedes anything queued before it.
        return
    try:
        sub.queue.put_nowait(event)
    except queue.Full:
        # A client this far behind would receive a stream with holes in it. Drop the
        # backlog and have it rebuild from a snapshot instead of drifting silently.
        # The whole queue goes, so every topic it watches has to be rebuilt.
        while True:
            try:
                sub.queue.get_nowait()
            except queue.Empty:
                break
        sub.stale |= sub.topics
        for topic in sub.topics:
            sub.queue.put_nowait({'topic': topic, 'type': 'resync', 'data': None})


def snapshot_event(topic):
    """Build a topic's snapshot event, or None if it has nothing to seed from."""
    source = TOPICS.get(topic)
    if source is None or source.snapshot is None:
        return None
    return {'topic': topic, 'type': 'snapshot', 'data': source.snapshot()}


def subscribe(topics):
    """Register a client for the given topics and start polling if needed."""
    sub = Subscription(topics)
    with _lock:
        _subscribers.add(sub)
        _ensure_poller()
    return sub


def unsubscribe(sub):
    with _lock:
        _subscribers.discard(sub)


def allowed_topics(requested):
    """Filter requested topic names down to registered ones the caller may read."""
    from auth import admin_account_created
    from flask_login import current_user

    known = [name for name in requested if name in TOPICS]
    if not admin_account_created():
        return known  # auth disabled entirely, same as access_required
    if not current_user.is_authenticated:
        return []
    return [name for name in known
            if TOPICS[name].access is None or current_user.has_access(TOPICS[name].access)]


# --- Polling ---

def _polled_topics_locked():
    """Registered topics that are both pollable and currently being watched. Lock held."""
    watched = set()
    for sub in _subscribers:
        watched |= sub.topics
    return [t for name, t in TOPICS.items() if t.poll is not None and name in watched]


def _ensure_poller():
    """Start the poller thread if a polled topic just gained its first subscriber. Lock held."""
    global _poller
    if _poller is not None or not _polled_topics_locked():
        return
    _stop.clear()
    _poller = threading.Thread(target=_poll_loop, name='realtime-poller', daemon=True)
    _poller.start()


def _poll_loop():
    """Tick every watched polled topic until the last of their subscribers leaves."""
    global _poller
    try:
        while not _stop.is_set():
            with _lock:
                topics = _polled_topics_locked()
                if not topics:
                    # Clearing the handle under the same lock that registers subscribers
                    # is what makes the exit safe: a client arriving now either registers
                    # before this check and keeps the thread alive, or sees no poller and
                    # starts one.
                    _poller = None
                    return
            with _app.app_context():
                for topic in topics:
                    try:
                        for event_type, data in topic.poll() or ():
                            publish(topic.name, event_type, data)
                    except Exception as e:
                        logger.error(f"Realtime topic '{topic.name}' poll failed: {e}")
            _stop.wait(POLL_INTERVAL)
    finally:
        # Also covers the stop() path, so a shutdown never leaves a dead handle behind
        # that would stop the next subscriber from starting a poller.
        with _lock:
            if _poller is threading.current_thread():
                _poller = None


def stop():
    """Stop the poller thread and release every client socket, for shutdown."""
    _stop.set()
    _shutdown.set()
    poller = _poller
    if poller is not None:
        poller.join(timeout=POLL_INTERVAL * 8)
    # A client thread left blocked here holds a server thread the interpreter joins on
    # its way out, so the process would hang until the client itself went away.
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        try:
            sub.queue.put_nowait(_SHUTDOWN)
        except queue.Full:
            pass  # it is already draining events, and sees the flag on the next lap


# --- WebSocket endpoint ---

def serve(ws, requested):
    """Drive one client socket: seed it with snapshots, then stream events until it goes."""
    topics = allowed_topics(requested)
    if not topics:
        ws.close()
        return

    sub = subscribe(topics)
    try:
        for topic in topics:
            event = snapshot_event(topic)
            if event:
                ws.send(json.dumps(event))
        while ws.connected and not _shutdown.is_set():
            try:
                event = sub.queue.get(timeout=IDLE_TIMEOUT)
            except queue.Empty:
                continue
            if event is _SHUTDOWN:
                break
            if event['type'] == 'resync':
                # Cleared before the snapshot is built, so a change landing mid-build is
                # queued behind it rather than dropped.
                sub.stale.discard(event['topic'])
                event = snapshot_event(event['topic']) or event
            ws.send(json.dumps(event))
    except Exception:
        pass  # client vanished mid-send; the finally below is the whole cleanup
    finally:
        unsubscribe(sub)
