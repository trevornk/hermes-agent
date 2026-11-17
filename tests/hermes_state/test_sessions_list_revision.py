"""The store keeps one revision for what a session list renders, whoever writes the row.

The Desktop change watcher used to hash every session row on each state.db write (heartbeats and
cron runs included) to decide whether to broadcast ``sessions.changed``, so its cost grew with the
whole history (#146643). It now reads this trigger-kept counter instead.
"""
import sqlite3
from contextlib import closing

import pytest

from hermes_state import SessionDB
from hermes_state_common import SESSIONS_LIST_REVISION_KEY, SESSIONS_LIST_REVISION_TRIGGERS


def _revision(path):
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (SESSIONS_LIST_REVISION_KEY,)).fetchone()
    return row and row[0]


def _write(path, sql, *params):
    """A bare writer, as an older build or another process would issue it."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(sql, params)
        conn.commit()


def test_revision_moves_for_list_changes_and_holds_for_activity_restamps(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session("s1", source="cli")
        created = _revision(path)
        assert created is not None

        _write(path, "UPDATE sessions SET last_activity_at = 123456 WHERE id = 's1'")
        _write(path, "UPDATE sessions SET title = title, pinned = pinned WHERE id = 's1'")
        assert _revision(path) == created

        _write(path, "UPDATE sessions SET title = 'Renamed' WHERE id = 's1'")
        renamed = _revision(path)
        assert renamed != created

        _write(path, "UPDATE sessions SET title = NULL WHERE id = 's1'")
        cleared = _revision(path)
        assert cleared != renamed

        _write(path, "DELETE FROM sessions WHERE id = 's1'")
        assert _revision(path) != cleared
    finally:
        db.close()


class TestReconcileColumnsErrorHandling:
    """_reconcile_columns must not bury migration failures (#79531/#80037).

    A locked ALTER used to be swallowed at DEBUG: startup "succeeded" with a
    half-reconciled schema and every session-list read then 500ed with
    "no such column" until an unrelated writable open. The contract now:
    duplicate-column races stay quiet, lock/busy propagates (so the open-time
    lock patience retries the whole init), everything else warns.
    """

    class _FailingAlterCursor:
        """Pass through to a real cursor, failing ALTER TABLE with ``exc``."""

        def __init__(self, real_cursor, exc):
            self._real = real_cursor
            self._exc = exc

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith("ALTER TABLE"):
                raise self._exc
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _db_missing_column(self, tmp_path):
        """A store whose sessions table lacks last_read_at."""
        db_path = tmp_path / "state.db"
        seed = SessionDB(db_path=db_path)
        seed.close()
        conn = sqlite3.connect(str(db_path))
        try:
            # A store that lacks the column predates the triggers naming it, too.
            for trigger in SESSIONS_LIST_REVISION_TRIGGERS:
                conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute("ALTER TABLE sessions DROP COLUMN last_read_at")
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_locked_alter_propagates(self, tmp_path):
        """database-is-locked must escape _reconcile_columns, not vanish.

        Propagation is what lets _connect_and_init_with_lock_patience retry
        the whole init with jittered backoff instead of serving a store
        that is silently behind SCHEMA_SQL.
        """
        db_path = self._db_missing_column(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            stale = SessionDB.__new__(SessionDB)
            stale._conn = conn
            cursor = self._FailingAlterCursor(
                conn.cursor(),
                sqlite3.OperationalError("database is locked"),
            )
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                stale._reconcile_columns(cursor)
        finally:
            conn.close()

    def test_duplicate_column_race_stays_quiet(self, tmp_path, caplog):
        """A duplicate-column race is expected and must not warn or raise."""
        import logging

        db_path = self._db_missing_column(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            stale = SessionDB.__new__(SessionDB)
            stale._conn = conn
            cursor = self._FailingAlterCursor(
                conn.cursor(),
                sqlite3.OperationalError(
                    "duplicate column name: last_read_at"
                ),
            )
            with caplog.at_level(logging.WARNING, logger="hermes_state"):
                stale._reconcile_columns(cursor)
        finally:
            conn.close()
        assert not [
            r for r in caplog.records if "reconcile" in r.getMessage()
        ]

    def test_other_alter_failures_warn(self, tmp_path, caplog):
        """Schema mistakes (e.g. un-ADDable NOT NULL) log at WARNING."""
        import logging

        db_path = self._db_missing_column(tmp_path)
        conn = sqlite3.connect(str(db_path))
        try:
            stale = SessionDB.__new__(SessionDB)
            stale._conn = conn
            cursor = self._FailingAlterCursor(
                conn.cursor(),
                sqlite3.OperationalError(
                    "Cannot add a NOT NULL column with default value NULL"
                ),
            )
            with caplog.at_level(logging.WARNING, logger="hermes_state"):
                stale._reconcile_columns(cursor)
        finally:
            conn.close()
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING
            and "reconcile" in r.getMessage()
        ]
        assert warnings, "un-ADDable column failure must be logged at WARNING+"

    def test_locked_alter_is_retried_by_open_lock_patience(self, tmp_path, monkeypatch):
        """End-to-end: a transiently locked ALTER heals on open retry.

        The lock-patience wrapper retries on OperationalError raised out of
        _connect_and_init; before this fix _reconcile_columns caught the
        error internally so the retry never saw it and the store stayed
        stale forever.
        """
        db_path = self._db_missing_column(tmp_path)

        original = SessionDB._reconcile_columns
        calls = {"n": 0}

        def flaky_reconcile(self, cursor):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(self, cursor)

        monkeypatch.setattr(SessionDB, "_reconcile_columns", flaky_reconcile)
        # Keep the retry fast — patience budget is 20s by default.
        monkeypatch.setattr(SessionDB, "_WRITE_RETRY_SLOW_MIN_S", 0.001)
        monkeypatch.setattr(SessionDB, "_WRITE_RETRY_SLOW_MAX_S", 0.005)

        healed = SessionDB(db_path=db_path)
        try:
            cols = {
                r[1]
                for r in healed._conn.execute(
                    'PRAGMA table_info("sessions")'
                ).fetchall()
            }
        finally:
            healed.close()
        assert calls["n"] >= 2, "lock patience must retry the init"
        assert "last_read_at" in cols
