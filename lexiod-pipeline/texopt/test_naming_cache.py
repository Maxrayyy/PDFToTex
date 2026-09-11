import sqlite3
import time

import pytest

from . import naming_cache


@pytest.mark.parametrize("message", ["database is locked", "database table is locked"])
def test_initialization_retries_transient_wal_lock(tmp_path, monkeypatch, message):
    connect = sqlite3.connect
    failures = []

    class ContendedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            # WAL mode changes can fail immediately despite SQLite's busy timeout.
            if sql == "PRAGMA journal_mode=WAL" and len(failures) < 2:
                failures.append(1)
                raise sqlite3.OperationalError(message)
            return super().execute(sql, *args)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw:
                        connect(*a, factory=ContendedConnection, **kw))
    cache = naming_cache.NamingCache(tmp_path / "names.sqlite3")
    value = {"table": "record"}
    assert cache.get_or_compute("shared", lambda: value) == (value, "llm")
    other = naming_cache.NamingCache(cache.path)
    assert other.get_or_compute("shared", lambda: pytest.fail("Cache was lost")) == (value, "cache")
    assert len(failures) == 2


def test_initialization_stops_waiting_for_persistent_lock(tmp_path, monkeypatch):
    path = tmp_path / "names.sqlite3"
    monkeypatch.setattr(naming_cache, "INITIALIZATION_TIMEOUT", .15, raising=False)
    db = sqlite3.connect(path)
    try:
        db.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            naming_cache.NamingCache(path)
        assert time.monotonic() - started < 2
    finally:
        db.close()
    # An exhausted attempt must release its connections and permit later recovery.
    cache = naming_cache.NamingCache(path)
    assert cache.get_or_compute("shared", lambda: {"ok": True})[1] == "llm"


def test_initialization_does_not_retry_non_lock_errors(tmp_path, monkeypatch):
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        naming_cache.NamingCache(tmp_path / "names.sqlite3")
    assert len(attempts) == 1
