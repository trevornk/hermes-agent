"""Detached Desktop sessions must retain their sole Kanban delivery poller (#138836)."""

from __future__ import annotations

import contextlib
import importlib
import sqlite3
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


@pytest.fixture()
def server(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with patch.dict("sys.modules", {"hermes_cli.env_loader": MagicMock(), "hermes_cli.banner": MagicMock()}):
        mod = importlib.import_module("tui_gateway.server")
        yield mod
        mod._sessions.clear()
        mod._pending_ws_reaps.clear()


class _Timer:
    created: list = []

    def __init__(self, delay, callback):
        self.delay, self.callback = delay, callback
        _Timer.created.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture()
def detached(server, monkeypatch):
    _Timer.created = []
    monkeypatch.setattr(server.threading, "Timer", _Timer)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 20)
    monkeypatch.setattr(server, "_session_has_active_delegations", lambda *a: False)
    popped = []
    monkeypatch.setattr(server, "_teardown_popped_session", lambda s, **kw: popped.append(s))
    sid = "sid-detached-kanban"
    session = {
        "session_key": "desktop-kanban-session",
        "transport": server._detached_ws_transport,
        "running": False,
        "history_lock": threading.Lock(),
        "last_active": 0.0,
        "created_at": 0.0,
    }
    server._sessions[sid] = session
    return sid, session, popped


def _subscribe(session, *, status="running", chat_id=None, platform="tui", board=None):
    with contextlib.closing(kbc.connect(board=board)) as conn:
        tid = kb.create_task(conn, title="detached delivery", assignee="worker", board=board)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        kbn.add_notify_sub(conn, task_id=tid, platform=platform,
                           chat_id=chat_id or session["session_key"], thread_id="thread-1")
    return tid


def _complete(tid):
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="shipped the card")


def _fire_orphan_reap(server, sid):
    server._schedule_ws_orphan_reap(sid)
    _Timer.created[-1].callback()


def test_detached_idle_session_with_running_subscribed_card_is_not_reaped(server, detached):
    sid, session, popped = detached
    _subscribe(session)
    _fire_orphan_reap(server, sid)
    assert server._sessions.get(sid) is session and not popped
    assert server._pending_ws_reaps[sid] is _Timer.created[-1]
    assert len(_Timer.created) == 2


@pytest.mark.parametrize("status", ["todo", "ready"])
def test_waiting_subscribed_card_keeps_detached_session(server, detached, status):
    sid, session, popped = detached
    _subscribe(session, status=status)
    _fire_orphan_reap(server, sid)
    assert server._sessions.get(sid) is session and not popped


@pytest.mark.parametrize("status", ["blocked", "scheduled", "triage", "review", "done", "archived"])
def test_settled_card_without_unclaimed_events_releases_session(server, detached, status):
    sid, session, popped = detached
    tid = _subscribe(session)
    assert not server._session_is_reapable(sid, session)
    if status == "done":
        _complete(tid)
        assert len(server._collect_kanban_notifications(session)) == 1
    else:
        with contextlib.closing(kbc.connect()) as conn, kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    session.pop("_kanban_retention_cache", None)
    assert server._session_is_reapable(sid, session)
    _fire_orphan_reap(server, sid)
    assert sid not in server._sessions and popped == [session]


def test_unclaimed_completion_keeps_session_until_claimed_and_cache_expires(server, detached, monkeypatch):
    sid, session, _ = detached
    tid = _subscribe(session)
    _complete(tid)
    clock = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    assert not server._session_is_reapable(sid, session)
    assert len(server._collect_kanban_notifications(session)) == 1
    assert not server._session_is_reapable(sid, session)  # cached True is bounded
    clock[0] += server._KANBAN_RETENTION_CACHE_SECONDS + 1
    assert server._session_is_reapable(sid, session)


def test_completion_delivered_exactly_once_after_detached_retention(server, detached):
    sid, session, popped = detached
    tid = _subscribe(session)
    _fire_orphan_reap(server, sid)
    assert server._sessions.get(sid) is session and not popped
    _complete(tid)
    texts = server._collect_kanban_notifications(session)
    assert len(texts) == 1 and tid in texts[0] and "shipped the card" in texts[0]
    assert server._collect_kanban_notifications(session) == []
    session.pop("_kanban_retention_cache", None)
    assert server._session_is_reapable(sid, session)


@pytest.mark.parametrize("flag", ["_turn_cancel_requested", "_finalized"])
def test_stop_or_finalized_session_is_not_pinned(server, detached, flag):
    _, session, _ = detached
    _subscribe(session)
    assert server._session_owns_pending_kanban_notification(session)
    session["_kanban_pending"] = ["already claimed"]
    session[flag] = True
    assert not server._session_owns_pending_kanban_notification(session)


def test_buffered_text_pins_only_a_session_with_a_key(server, detached):
    _, session, _ = detached
    session["_kanban_pending"] = ["already claimed"]
    assert server._session_owns_pending_kanban_notification(session)
    session.pop("session_key")
    assert not server._session_owns_pending_kanban_notification(session)


@pytest.mark.parametrize("failure", ["corrupt", "connect", "missing_table", "board_listing"])
def test_unreadable_board_fails_open(server, detached, monkeypatch, failure):
    _, session, _ = detached
    path = kb.kanban_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if failure == "corrupt":
        path.write_text("not a SQLite database")
    elif failure == "missing_table":
        sqlite3.connect(path).close()
    else:
        sqlite3.connect(path).close()
        def fail(*a, **kw):
            raise sqlite3.OperationalError("unreadable board")
        if failure == "board_listing":
            monkeypatch.setattr(kb, "list_boards", fail)
        else:
            monkeypatch.setattr(sqlite3, "connect", fail)
    assert not server._session_owns_pending_kanban_notification(session)


def test_missing_board_does_not_pin_or_create_database(server, detached):
    _, session, _ = detached
    path = kb.kanban_db_path()
    assert not path.exists()
    assert not server._session_owns_pending_kanban_notification(session)
    assert not path.exists()


def test_foreign_chat_subscription_does_not_pin(server, detached):
    _, session, _ = detached
    _subscribe(session, chat_id="someone-else")
    assert not server._session_owns_pending_kanban_notification(session)


def test_cached_stray_keys_are_checked_without_state_lookup(server, detached, monkeypatch):
    _, session, _ = detached
    _subscribe(session, chat_id="delegate-key", platform="TuI")
    monkeypatch.setattr(server, "_session_db", lambda *_: pytest.fail("retention must not open state.db"))
    assert not server._session_owns_pending_kanban_notification(session)
    session["_kanban_stray_keys"] = ("delegate-key",)
    assert server._session_owns_pending_kanban_notification(session)


def test_board_probe_is_cached_deduplicated_and_read_only(server, detached, monkeypatch):
    _, session, _ = detached
    _subscribe(session)
    meta = kb.read_board_metadata()
    monkeypatch.setattr(kb, "list_boards", lambda **kw: [meta, dict(meta, slug="alias")])
    with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
        assert server._session_owns_pending_kanban_notification(session)
        assert server._session_owns_pending_kanban_notification(session)
    assert connect.call_count == 1
    assert connect.call_args.args[0].endswith("?mode=ro")


def test_archived_board_does_not_pin(server, detached):
    _, session, _ = detached
    kb.create_board("retired")
    _subscribe(session, board="retired")
    kb.write_board_metadata("retired", archived=True)
    assert not server._session_owns_pending_kanban_notification(session)


def test_retention_reads_boards_in_owning_profile_scope(server, detached, tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    _, session, _ = detached
    session["profile_home"] = str(tmp_path / "other-profile")
    with server._session_profile_runtime_scope(session, hydrate_secrets=False):
        _subscribe(session)
    list_boards = kb.list_boards

    def scoped_boards(**kwargs):
        assert get_hermes_home() == Path(session["profile_home"])
        return list_boards(**kwargs)

    monkeypatch.setattr(kb, "list_boards", scoped_boards)
    assert server._session_owns_pending_kanban_notification(session)


def test_cap_spares_kanban_poller_but_idle_session_allows_memory_trim(server, detached, monkeypatch):
    sid, session, popped = detached
    _subscribe(session)
    monkeypatch.setattr(server, "_max_live_sessions", lambda: 1)
    server._sessions["busy"] = {"running": True}
    server._enforce_session_cap()
    assert server._sessions.get(sid) is session and not popped
    assert server._session_is_lru_evictable(sid, session)
    server._sessions.pop("busy")
    assert server._sessions_quiescent()
