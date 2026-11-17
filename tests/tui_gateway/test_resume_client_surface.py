"""``session.resume`` keeps a Desktop chat on the ``desktop`` surface when the client omits ``source``.

Bug class: a Desktop app connected to a REMOTE/cloud backend (``hermes dashboard`` / ``hermes serve``
with ``HERMES_DESKTOP`` unset) resumes a chat through a path that sends no ``source`` (tile/pane
resume, reconnect after a backend restart).  The live record then fell back to the process env and
became ``tui``: the agent was told "there is no attachment channel, MEDIA: tags are not intercepted"
and refused to attach files, and the next turn carried a ``desktop -> tui`` surface-switch note.

The row remembers the surface the chat was created on.  Precedence on resume:
explicit ``source`` param > stored ``desktop`` row (non-stdio transport) > process env.
Stdio is the terminal UI's own transport (it never sends ``source``), so a TUI that resumes a
desktop chat keeps its own surface.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport

KEY = "20261014_121006_587c68"


class _WsLikeTransport:
    """Any non-stdio transport: a remote websocket client."""

    def write(self, obj) -> bool:
        return True

    def close(self) -> None:
        return None


@pytest.fixture()
def real_db(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _key, _home=None: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_child_run_active", lambda _key, _home=None: False)
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    known = set(server._sessions)
    yield db
    with server._sessions_lock:
        for sid in [s for s in server._sessions if s not in known]:
            server._sessions.pop(sid, None)
    db.close()


@pytest.fixture()
def ws_client():
    token = bind_transport(_WsLikeTransport())
    yield
    reset_transport(token)


def _resume(**params):
    resp = server.handle_request({"id": "1", "method": "session.resume", "params": params})
    assert "error" not in resp, resp
    return server._sessions[resp["result"]["session_id"]]


def _seed(db, source):
    db.create_session(KEY, source=source, model="test-model")
    db.append_message(KEY, "user", "hello")


def test_resume_without_source_keeps_stored_desktop_surface(real_db, ws_client):
    """The regression: remote backend (no HERMES_DESKTOP), client omits ``source`` -> still desktop."""
    _seed(real_db, "desktop")

    live = _resume(session_id=KEY)

    assert live["source"] == "desktop"
    assert server._session_source(live) == "desktop"


def test_resume_without_source_keeps_desktop_on_every_resume_mode(real_db, ws_client):
    _seed(real_db, "desktop")

    for extra in ({"defer_history": True}, {"lazy": True}):
        with server._sessions_lock:
            server._sessions.clear()
        assert _resume(session_id=KEY, **extra)["source"] == "desktop", extra


def test_explicit_source_still_wins_over_stored_row(real_db, ws_client):
    _seed(real_db, "desktop")

    assert _resume(session_id=KEY, source="tui")["source"] == "tui"


def test_stdio_terminal_ui_resuming_a_desktop_chat_keeps_its_own_surface(real_db):
    """``hermes --tui`` never sends ``source`` and rides stdio: it must not be relabeled desktop."""
    _seed(real_db, "desktop")

    assert _resume(session_id=KEY)["source"] == "tui"


def test_stored_non_desktop_row_does_not_override_env_fallback(real_db, ws_client):
    """Only the GUI surface is remembered: a stored ``cli``/``telegram`` row never leaks into the agent platform."""
    _seed(real_db, "telegram")

    assert _resume(session_id=KEY)["source"] == "tui"


def test_stored_tui_row_follows_local_desktop_env(real_db, ws_client, monkeypatch):
    """A TUI-origin chat opened by a local Desktop backend (HERMES_DESKTOP=1) still switches to desktop."""
    _seed(real_db, "tui")
    monkeypatch.setenv("HERMES_DESKTOP", "1")

    assert _resume(session_id=KEY)["source"] == "desktop"
