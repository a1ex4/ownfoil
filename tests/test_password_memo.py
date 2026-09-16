"""Memoized password verification (auth.verify_password).

scrypt is deliberately ~135ms of CPU, and it used to run on every authenticated
request. Successful verifications are now memoized, which is only safe if the memo
answers exactly what a fresh hash check would: these tests pin that equivalence,
not the speed-up. They count hash invocations rather than measuring time, since
"scrypt did not run again" is the actual contract.
"""
import pytest
from werkzeug.security import generate_password_hash

import auth

PASSWORD = "correct horse"
WRONG = "incorrect horse"

# Verifying is the point of the exercise, so keep the work parameters cheap: the memo
# behaves the same whichever cost the stored hash was generated with.
CHEAP = "scrypt:1024:8:1"


@pytest.fixture
def hashes(monkeypatch):
    """A clean memo, and a counter for how often the real hash check runs."""
    auth._verify_cache.clear()
    calls = []
    real = auth.check_password_hash

    def counted(pwhash, password):
        calls.append(pwhash)
        return real(pwhash, password)

    monkeypatch.setattr(auth, "check_password_hash", counted)
    yield calls
    auth._verify_cache.clear()


def test_a_repeat_verification_does_not_rerun_the_hash(hashes):
    pwhash = generate_password_hash(PASSWORD, method=CHEAP)

    assert [auth.verify_password(pwhash, PASSWORD) for _ in range(3)] == [True] * 3
    assert len(hashes) == 1


def test_a_wrong_password_is_rejected_every_time(hashes):
    """Failures are never memoized, so guessing stays as expensive as it ever was."""
    pwhash = generate_password_hash(PASSWORD, method=CHEAP)

    assert [auth.verify_password(pwhash, WRONG) for _ in range(3)] == [False] * 3
    assert len(hashes) == 3


def test_a_changed_password_invalidates_the_old_one(hashes):
    """The stored hash is part of the key, so a new hash cannot hit the old entry."""
    old = generate_password_hash(PASSWORD, method=CHEAP)
    assert auth.verify_password(old, PASSWORD)

    new = generate_password_hash("brand new", method=CHEAP)

    assert not auth.verify_password(new, PASSWORD)
    assert auth.verify_password(new, "brand new")


def test_two_accounts_sharing_a_password_are_verified_separately(hashes):
    """Distinct salts mean distinct hashes, so one account's hit cannot answer for
    another's - the memo says 'this password matches this hash', nothing wider."""
    first = generate_password_hash(PASSWORD, method=CHEAP)
    second = generate_password_hash(PASSWORD, method=CHEAP)

    assert auth.verify_password(first, PASSWORD)
    assert auth.verify_password(second, PASSWORD)
    assert len(hashes) == 2


def test_an_entry_is_reverified_once_it_expires(hashes, monkeypatch):
    monkeypatch.setattr(auth, "VERIFY_CACHE_TTL", 0)
    pwhash = generate_password_hash(PASSWORD, method=CHEAP)

    assert auth.verify_password(pwhash, PASSWORD)
    assert auth.verify_password(pwhash, PASSWORD)
    assert len(hashes) == 2


def test_the_memo_stays_bounded_and_correct(hashes, monkeypatch):
    """Many distinct credentials must not grow the cache without limit."""
    monkeypatch.setattr(auth, "VERIFY_CACHE_MAX", 8)
    pwhashes = [generate_password_hash(f"pw{i}", method=CHEAP) for i in range(20)]

    for i, pwhash in enumerate(pwhashes):
        assert auth.verify_password(pwhash, f"pw{i}")

    assert len(auth._verify_cache) <= 8
    assert auth.verify_password(pwhashes[0], "pw0")
    assert not auth.verify_password(pwhashes[0], "pw1")
