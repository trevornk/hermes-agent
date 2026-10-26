"""Dashboard session routes must run SessionDB open + work off the event loop.

Regression for #60747: /search, /stats, /{id} (and bulk delete) ran FTS and
SQLite inline on the loop thread, so a large state.db froze every other
dashboard request (rename too). These drive the real route handlers and record which
thread every SessionDB call lands on.
"""

import asyncio
import threading

import pytest

import hermes_cli.web_models as _web_models
import hermes_cli.web_routers.sessions as _rt_sessions
import hermes_cli.web_server_sessions as _web_server_sessions


def test_bulk_delete_sessiondb_work_runs_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    db_threads: list[int] = []
    db_modes: list[bool] = []

    class _DB:
        def delete_sessions(self, ids, **kwargs):
            db_threads.append(threading.get_ident())
            assert ids == ["one", "two"]
            return 2

        def close(self):
            db_threads.append(threading.get_ident())

    def _open_db(profile=None, *, read_only):
        assert profile is None
        db_modes.append(read_only)
        return _DB()

    monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile", _open_db)

    result = asyncio.run(
        _rt_sessions.bulk_delete_sessions_endpoint(
            _web_models.BulkDeleteSessions(ids=["one", "two"])
        )
    )

    assert result == {"ok": True, "deleted": 2, "skipped_active": []}
    assert db_modes == [False]
    assert db_threads
    assert all(thread_id != loop_thread for thread_id in db_threads)


class _ReadDB:
    """Read-only stand-in that records which thread every SessionDB call runs on."""

    def __init__(self, threads: list[int]):
        self._threads = threads

    def _hit(self, value=None):
        self._threads.append(threading.get_ident())
        return value

    def session_count(self, **_kw):
        return self._hit(3)

    def message_count(self):
        return self._hit(7)

    def session_count_by_source(self, **_kw):
        return self._hit({"cli": 3})

    def get_session(self, sid):
        return self._hit({"id": sid, "title": "t"})

    def get_session_rich_row(self, sid):
        return self._hit(None)

    def get_compression_tip(self, root_id):
        return self._hit(None)

    def search_sessions_by_id(self, q, **_kw):
        return self._hit([{"id": "sess-1", "preview": "hello", "started_at": 1.0}])

    def search_messages(self, **_kw):
        return self._hit([])

    def close(self):
        self._hit()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: _rt_sessions.search_sessions(q="hello"), id="search"),
        pytest.param(lambda: _rt_sessions.get_session_stats(), id="stats"),
        pytest.param(lambda: _rt_sessions.get_session_detail("sess-1"), id="detail"),
    ],
)
def test_session_read_handlers_run_sessiondb_work_off_event_loop(monkeypatch, call):
    """Regression for #60747: /search, /stats and /{id} ran FTS + SQLite inline on the
    loop thread, so a large state.db froze every other dashboard request."""
    loop_thread = threading.get_ident()
    db_threads: list[int] = []

    def _open_db(profile=None, *, read_only):
        assert read_only is True
        return _ReadDB(db_threads)

    monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile", _open_db)
    monkeypatch.setattr(_rt_sessions, "_resolve_session_id", lambda db, sid: sid)

    asyncio.run(call())

    assert db_threads, "handler never touched the SessionDB"
    assert all(thread_id != loop_thread for thread_id in db_threads)


def test_session_rename_runs_writer_open_and_update_off_event_loop(monkeypatch, tmp_path):
    """Main's rename fix (1769024ca3b) ported to the unified runtime: the PATCH still opens no
    second SessionDB writer (the owning authority applies the ``sidebar`` mutation), and that
    authority's SQLite transaction runs off the event loop thread."""
    from types import SimpleNamespace

    import gateway.session_mutations as _mutations
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import Principal
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    def _open_db(*_args, **_kwargs):
        raise AssertionError("rename must not open its own SessionDB writer")

    write_threads: list[int] = []
    commit = _mutations.mutate_runtime_session

    def _recording_commit(*args, **kwargs):
        write_threads.append(threading.get_ident())
        return commit(*args, **kwargs)

    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("sess-1", source="cli")
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id="p", instance_id="owner",
                                     db=db, epoch=begin_runtime_epoch(db, instance_id="owner"))
        authority.sessions["sess-1"] = LiveSession(None, "route")
        actor = Principal("owner", "p", frozenset({"session:control"}), "http")
        monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile", _open_db)
        monkeypatch.setattr(_web_server_sessions, "_session_mutation_context", lambda request, profile: (authority, actor))
        monkeypatch.setattr(_mutations, "mutate_runtime_session", _recording_commit)

        async def _rename():
            body = _web_models.SessionRename(title="renamed", request_id="req-1", expected_revision=0)
            return threading.get_ident(), await _rt_sessions.rename_session_endpoint("sess-1", body, request=object())

        loop_thread, result = asyncio.run(_rename())
        assert result["ok"] is True and result["title"] == "renamed"
        assert db.get_session("sess-1")["title"] == "renamed"
    assert write_threads and all(thread_id != loop_thread for thread_id in write_threads)


class _MessagesDB:
    """Records one open per DB instance; get_messages blocks on a barrier so a test can force
    two concurrent requests to overlap before either finishes."""

    def __init__(self, open_calls: list, start_gate: threading.Event, release_gate: threading.Event):
        self._open_calls = open_calls
        self._start_gate = start_gate
        self._release_gate = release_gate
        open_calls.append(1)

    def resolve_resume_session_id(self, sid):
        return sid

    def get_messages(self, sid, *, limit, offset, latest, include_compacted, include_ancestors=False):
        self._start_gate.set()
        self._release_gate.wait(timeout=5)
        return [{"id": sid, "offset": offset}]

    def close(self):
        pass


def test_concurrent_identical_message_reads_share_one_db_open(monkeypatch):
    """[carried, dashboard_ws_handshake_offload.patch] A dashboard reconnect storm re-requests
    the same session's transcript with identical params; two concurrent requests for the same
    (profile, session_id, limit, offset, order, include_compacted) must open/scan the SessionDB
    ONCE, not once per request — the second request rides the first's in-flight read instead of
    stacking another full table scan onto the executor."""
    open_calls: list = []
    start_gate = threading.Event()
    release_gate = threading.Event()

    def _open_db(profile=None, *, read_only):
        return _MessagesDB(open_calls, start_gate, release_gate)

    monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile", _open_db)
    monkeypatch.setattr(_rt_sessions, "_resolve_session_id", lambda db, sid: sid)

    async def _scenario():
        call = lambda: _rt_sessions.get_session_messages(
            "sess-1", limit=None, offset=0, order=None, include_compacted=False)
        first = asyncio.ensure_future(call())
        # Wait until the first request is actually inside the (blocked) DB read, so the second
        # request is guaranteed to observe it as in-flight rather than racing ahead of it.
        await asyncio.to_thread(start_gate.wait, 5)
        second = asyncio.ensure_future(call())
        await asyncio.sleep(0.05)  # let `second` reach _coalesced_to_thread before releasing
        release_gate.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(_scenario())

    assert len(open_calls) == 1, f"expected exactly one SessionDB open, got {len(open_calls)}"
    assert results[0] == results[1]


def test_concurrent_different_message_reads_are_not_coalesced(monkeypatch):
    """Requests for different params (here: different offsets) must NOT share a read — coalescing
    is keyed on the full parameter tuple, not just the session id."""
    open_calls: list = []

    def _open_db(profile=None, *, read_only):
        # No blocking needed here: each call gets its own gate pair.
        return _MessagesDB(open_calls, threading.Event(), threading.Event())

    monkeypatch.setattr(_web_server_sessions, "_open_session_db_for_profile", _open_db)
    monkeypatch.setattr(_rt_sessions, "_resolve_session_id", lambda db, sid: sid)

    # Unblock every DB instance's get_messages immediately (no cross-request race to prove here).
    orig_init = _MessagesDB.__init__

    def _init_and_release(self, open_calls_, start_gate, release_gate):
        orig_init(self, open_calls_, start_gate, release_gate)
        release_gate.set()

    monkeypatch.setattr(_MessagesDB, "__init__", _init_and_release)

    async def _scenario():
        return await asyncio.gather(
            _rt_sessions.get_session_messages(
                "sess-1", limit=None, offset=0, order=None, include_compacted=False),
            _rt_sessions.get_session_messages(
                "sess-1", limit=None, offset=50, order=None, include_compacted=False),
        )

    results = asyncio.run(_scenario())

    assert len(open_calls) == 2, f"expected two independent SessionDB opens, got {len(open_calls)}"
    assert results[0] != results[1]
