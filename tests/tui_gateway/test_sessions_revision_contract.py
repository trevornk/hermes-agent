"""Session projection tracking must avoid row reads for unrelated commits."""
import sqlite3

import pytest

from hermes_state import SessionDB
from tui_gateway import server


@pytest.mark.parametrize('sql', (
    'UPDATE unrelated SET value = 2',
    'UPDATE gateway_heartbeats SET last_heartbeat = 2',
    "UPDATE sessions SET last_activity_at = 2, last_activity_description = 'busy'",
))
def test_unrelated_commits_do_not_read_session_rows(tmp_path, monkeypatch, sql):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('s', 'cli', 1)")
    conn.execute("INSERT INTO gateway_heartbeats(backend_id, pid, started_at, last_heartbeat) VALUES ('b', 1, 1, 1)")
    conn.execute('CREATE TABLE unrelated(value INTEGER)')
    conn.execute('INSERT INTO unrelated VALUES (1)')
    conn.commit()
    reads = []
    import hermes_state
    connect = hermes_state._connect_tracked_db

    def traced(*args, **kwargs):
        reader = connect(*args, **kwargs)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == 'sessions':
                reads.append(column)
            return sqlite3.SQLITE_OK
        reader.set_authorizer(authorize)
        return reader

    monkeypatch.setattr(hermes_state, '_connect_tracked_db', traced)
    monkeypatch.setattr(server, '_settled_mtime', lambda _: object())
    baseline = server._session_db_content_sig(path)
    reads.clear()
    conn.execute(sql)
    conn.commit()
    assert server._session_db_content_sig(path) == baseline
    assert reads == [], 'unrelated commits must not read session rows'
    conn.execute("UPDATE sessions SET title = 'changed'")
    conn.commit()
    assert server._session_db_content_sig(path) != baseline
    assert reads == [], 'relevant commits must not read session rows either'
    conn.close()


@pytest.fixture
def tracked_store(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.close()
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('s', 'cli', 1)")
    conn.commit()
    monkeypatch.setattr(server, '_settled_mtime', lambda _: object())
    yield path, conn
    conn.close()


@pytest.mark.parametrize('field', (
    'id', 'source', 'session_key', 'display_name', 'model', 'parent_session_id',
    'started_at', 'ended_at', 'end_reason', 'message_count', 'tool_call_count',
    'cwd', 'git_branch', 'git_repo_root', 'title', 'title_source', 'profile_name',
    'archived', 'pinned', 'hidden', 'last_read_at', 'handoff_state',
))
def test_every_projection_field_is_tracked(tracked_store, field):
    path, conn = tracked_store
    before = server._session_db_content_sig(path)
    # Foreign keys are off for this direct SQL writer; numeric affinity is preserved.
    value = 42 if field in {'started_at', 'ended_at', 'message_count', 'tool_call_count',
                           'archived', 'pinned', 'hidden', 'last_read_at'} else 'changed'
    conn.execute(f'UPDATE sessions SET {field} = ?', (value,))
    conn.commit()
    after = server._session_db_content_sig(path)
    assert after != before
    conn.execute(f'UPDATE sessions SET {field} = ?', (value,))
    conn.commit()
    assert server._session_db_content_sig(path) == after
    if field not in {'id', 'source', 'started_at', 'message_count', 'tool_call_count',
                     'archived', 'pinned', 'hidden'}:
        conn.execute(f'UPDATE sessions SET {field} = NULL')
        conn.commit()
        assert server._session_db_content_sig(path) != after


def test_transactions_and_direct_insert_delete(tracked_store):
    path, conn = tracked_store
    before = server._session_db_content_sig(path)
    conn.execute("UPDATE sessions SET title = 'uncommitted'")
    assert server._session_db_content_sig(path) == before
    conn.rollback()
    assert server._session_db_content_sig(path) == before
    conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('other', 'cli', 2)")
    conn.commit()
    inserted = server._session_db_content_sig(path)
    assert inserted != before
    conn.execute("DELETE FROM sessions WHERE id = 'other'")
    conn.commit()
    assert server._session_db_content_sig(path) != inserted


def test_failed_reader_retries_without_signature_flap(tracked_store, monkeypatch):
    import hermes_state
    path, conn = tracked_store
    before = server._session_db_content_sig(path)
    connect = hermes_state._connect_tracked_db
    def busy(*args, **kwargs):
        raise sqlite3.OperationalError('busy')
    monkeypatch.setattr(hermes_state, '_connect_tracked_db', busy)
    conn.execute("UPDATE sessions SET title = 'new'")
    conn.commit()
    assert server._session_db_content_sig(path) == before
    monkeypatch.setattr(hermes_state, '_connect_tracked_db', connect)
    assert server._session_db_content_sig(path) != before


def test_revision_probe_rechecks_racy_timestamps(tracked_store, monkeypatch):
    import time
    path, conn = tracked_store
    frozen = time.time_ns()
    monkeypatch.setattr(server, '_newest_mtime_ns', lambda _: frozen)
    from tui_gateway import change_watcher
    monkeypatch.setattr(server, '_settled_mtime', change_watcher._settled_mtime)
    monkeypatch.setattr(server, '_files_sig', lambda _: ())
    before = server._session_db_content_sig(path)
    conn.execute("UPDATE sessions SET title = 'racy'")
    conn.commit()
    assert server._session_db_content_sig(path) != before



def test_foreign_named_triggers_cannot_hide_session_changes(tmp_path, monkeypatch):
    from hermes_state_common import SESSIONS_LIST_REVISION_TRIGGERS
    path = tmp_path / 'foreign.db'
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT);"
                       "CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT);"
                       "INSERT INTO sessions VALUES('s', 'before');")
    for name in SESSIONS_LIST_REVISION_TRIGGERS:
        conn.execute(f'CREATE TRIGGER {name} AFTER INSERT ON sessions BEGIN SELECT 1; END')
    conn.commit()
    monkeypatch.setattr(server, '_settled_mtime', lambda _: object())
    before = server._session_db_content_sig(path)
    conn.execute("UPDATE sessions SET title='after'")
    conn.commit()
    assert server._session_db_content_sig(path) != before
    conn.close()


def test_legacy_digest_reads_sequentially_without_primary_key_walk(tmp_path):
    # A TEXT PK autoindex orders by id through table lookups. The fallback must
    # scan rows sequentially even on a store with wide out-of-line session data.
    conn = sqlite3.connect(tmp_path / 'legacy.db')
    conn.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT)')
    conn.executemany('INSERT INTO sessions VALUES(?,?)', [('b', 'two'), ('a', 'one')])
    queries = []
    conn.set_trace_callback(queries.append)
    first = server._session_rows_digest(conn, 1)
    query = next(q for q in queries if q.startswith('SELECT ') and 'FROM sessions' in q)
    conn.set_trace_callback(None)
    plan = conn.execute('EXPLAIN QUERY PLAN ' + query).fetchall()
    assert not any('USING INDEX sqlite_autoindex_sessions' in row[3] for row in plan)
    conn.execute('UPDATE sessions SET title=title')
    assert server._session_rows_digest(conn, 2) == first
    conn.close()


def test_repair_advances_revision_past_writes_missed_by_absent_trigger(tracked_store):
    path, conn = tracked_store
    before = server._session_db_content_sig(path)
    conn.execute('DROP TRIGGER sessions_list_revision_update')
    conn.execute("UPDATE sessions SET title='written during tracking gap'")
    conn.commit()
    db = SessionDB(db_path=path)
    db.close()
    assert server._session_db_content_sig(path) != before


def test_replaced_store_with_same_counter_is_still_a_change(tmp_path, monkeypatch):
    import os
    import time

    paths = [tmp_path / 'state.db', tmp_path / 'replacement.db']
    for path, title in zip(paths, ['first', 'replacement']):
        db = SessionDB(db_path=path)
        db.create_session('s', source='cli')
        db.set_session_title('s', title)
        db.close()
    frozen = time.time_ns() - 10_000_000_000
    monkeypatch.setattr(server, '_newest_mtime_ns', lambda _: frozen)
    monkeypatch.setattr(server, '_settled_mtime', lambda value: value)
    before = server._session_db_content_sig(paths[0])
    os.replace(paths[1], paths[0])
    assert server._session_db_content_sig(paths[0]) != before
