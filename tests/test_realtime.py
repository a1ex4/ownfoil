"""The realtime broker, exercised only through fake topics.

Nothing here imports tasks: if this file ever needs to, the generic layer has stopped
being generic.
"""
import queue
import threading

import pytest
from flask import Flask

import realtime


@pytest.fixture
def broker():
    """A clean broker bound to a bare Flask app, restored afterwards."""
    app = Flask(__name__)
    realtime.init_app(app)
    realtime.TOPICS.clear()
    realtime._subscribers.clear()
    yield realtime
    realtime.stop()
    realtime.TOPICS.clear()
    realtime._subscribers.clear()
    realtime._poller = None
    realtime._shutdown.clear()
    _seen.clear()


def drain(sub):
    """Every event queued for a subscriber, without blocking."""
    events = []
    while True:
        try:
            events.append(sub.queue.get_nowait())
        except queue.Empty:
            return events


# --- Fan-out ---

def test_published_events_reach_every_subscriber_of_that_topic(broker):
    broker.register_topic('alpha')
    broker.register_topic('beta')
    one = broker.subscribe(['alpha'])
    two = broker.subscribe(['alpha'])
    other = broker.subscribe(['beta'])

    broker.publish('alpha', 'update', {'n': 1})

    expected = [{'topic': 'alpha', 'type': 'update', 'data': {'n': 1}}]
    assert drain(one) == expected
    assert drain(two) == expected
    assert drain(other) == []


def test_unsubscribed_clients_stop_receiving(broker):
    broker.register_topic('alpha')
    sub = broker.subscribe(['alpha'])
    broker.unsubscribe(sub)

    broker.publish('alpha', 'update', {'n': 1})

    assert drain(sub) == []


def test_a_client_too_slow_to_keep_up_is_asked_to_resync(broker):
    broker.register_topic('alpha', snapshot=lambda: ['fresh'])
    sub = broker.subscribe(['alpha'])

    for n in range(realtime.QUEUE_SIZE + 5):
        broker.publish('alpha', 'update', {'n': n})

    # The backlog is dropped rather than delivered with holes in it.
    assert drain(sub) == [{'topic': 'alpha', 'type': 'resync', 'data': None}]


def test_a_resync_is_answered_with_the_current_snapshot(broker):
    broker.register_topic('alpha', snapshot=lambda: ['fresh'])
    assert broker.snapshot_event('alpha') == {
        'topic': 'alpha', 'type': 'snapshot', 'data': ['fresh']}


def test_a_topic_without_a_snapshot_has_nothing_to_seed(broker):
    broker.register_topic('alpha')
    assert broker.snapshot_event('alpha') is None


# --- Access control ---

ACCESS_CASES = [
    ('admin only, admin caller', 'admin', True, ['secret']),
    ('admin only, shop caller', 'admin', False, []),
    ('public topic, shop caller', None, False, ['secret']),
]


@pytest.mark.parametrize('label,access,can_admin,expected',
                         ACCESS_CASES, ids=[c[0] for c in ACCESS_CASES])
def test_topics_are_filtered_by_the_callers_access(broker, monkeypatch, label, access,
                                                   can_admin, expected):
    broker.register_topic('secret', access=access)

    class FakeUser:
        is_authenticated = True

        def has_access(self, role):
            return can_admin

    monkeypatch.setattr('auth.admin_account_created', lambda: True)
    monkeypatch.setattr('flask_login.current_user', FakeUser())

    assert broker.allowed_topics(['secret']) == expected


def test_unregistered_topics_are_never_served(broker, monkeypatch):
    monkeypatch.setattr('auth.admin_account_created', lambda: False)
    assert broker.allowed_topics(['nope']) == []


def test_every_topic_is_open_while_auth_is_disabled(broker, monkeypatch):
    broker.register_topic('secret', access='admin')
    monkeypatch.setattr('auth.admin_account_created', lambda: False)
    assert broker.allowed_topics(['secret']) == ['secret']


# --- Polling ---

def test_a_polled_topic_publishes_what_its_source_returns(broker):
    ticks = [[('add', {'n': 1})], [('update', {'n': 2})]]
    broker.register_topic('alpha', poll=lambda: ticks.pop(0) if ticks else [])
    sub = broker.subscribe(['alpha'])

    _wait_for(lambda: len(drain_into(sub)) >= 2)
    assert _collected(sub) == [
        {'topic': 'alpha', 'type': 'add', 'data': {'n': 1}},
        {'topic': 'alpha', 'type': 'update', 'data': {'n': 2}},
    ]


def test_push_only_topics_are_never_polled(broker):
    broker.register_topic('alpha')
    broker.subscribe(['alpha'])
    assert realtime._poller is None


def test_the_poller_stops_once_the_last_subscriber_leaves(broker):
    broker.register_topic('alpha', poll=lambda: [])
    sub = broker.subscribe(['alpha'])
    assert realtime._poller is not None

    broker.unsubscribe(sub)
    _wait_for(lambda: realtime._poller is None)
    assert realtime._poller is None


def test_a_failing_source_does_not_take_the_poller_down(broker):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError('source is broken')

    broker.register_topic('alpha', poll=boom)
    broker.subscribe(['alpha'])

    _wait_for(lambda: len(calls) >= 2)
    assert realtime._poller is not None


# --- Shutdown ---

class FakeWS:
    """Just enough socket for serve(): it stays open until someone closes it."""

    def __init__(self):
        self.connected = True
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.connected = False


def test_stopping_the_broker_releases_a_still_open_client(broker, monkeypatch):
    # A client left parked on its queue holds a server thread the interpreter joins on
    # its way out: the process would then hang until the browser tab went away.
    monkeypatch.setattr('auth.admin_account_created', lambda: False)
    broker.register_topic('alpha', snapshot=lambda: ['seed'])
    ws = FakeWS()
    returned = threading.Event()

    def client():
        realtime.serve(ws, ['alpha'])
        returned.set()

    threading.Thread(target=client, daemon=True).start()
    _wait_for(lambda: ws.sent)  # seeded, so it is now parked waiting for events

    broker.stop()

    assert returned.wait(timeout=2), 'client thread never let go of its socket'
    assert realtime._subscribers == set()


# --- helpers ---

_seen = {}


def drain_into(sub):
    _seen.setdefault(id(sub), []).extend(drain(sub))
    return _seen[id(sub)]


def _collected(sub):
    return _seen[id(sub)]


def _wait_for(predicate, timeout=3.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(realtime.POLL_INTERVAL / 4)
    raise AssertionError('condition not reached in time')
