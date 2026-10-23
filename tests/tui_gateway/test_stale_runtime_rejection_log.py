"""A stale-runtime 4001 the gateway itself caused is not a WARNING per poll.

``_sess_nowait`` answers 4001 "session not found" for a runtime id the gateway no
longer holds, and the client heals on exactly that reply (it latches the id and
resumes the stored session). Logging every such reply at WARNING made a laptop
that sleeps and wakes every ~15 minutes — replaying the cached runtime ids of
tabs the 5-minute WS-orphan grace had already reaped — write 60-113 "session-scoped
RPC rejected" warnings a day, ~90% from read-only polls (approval.pending,
process.list, session.control.read) that lose nothing.

The reply must not change (the client contract and the unknown-id rejection are
the security boundary); only the log severity of an *expected* rejection does:

* an id this process closed itself, hit by a background poll / rebind attempt → INFO
  once (with the age), DEBUG for the repeats of the same id;
* anything else — an id this process never held, or a user-intent RPC such as
  ``prompt.submit`` / ``slash.exec`` (the #90428 "message vanished" diagnostic) →
  WARNING exactly as before.
"""

from __future__ import annotations

import logging

import pytest

from tui_gateway import server

QUIET_METHODS = (
    "approval.pending",
    "approval.received",
    "process.list",
    "session.control.read",
    "session.usage",
    "session.activate",
)
LOUD_METHODS = ("prompt.submit", "slash.exec", "session.interrupt")


@pytest.fixture(autouse=True)
def _clean_registry():
    with server._sessions_lock:
        before = dict(server._sessions)
        server._sessions.clear()
    server._recently_closed_runtimes.clear()
    yield
    server._recently_closed_runtimes.clear()
    with server._sessions_lock:
        server._sessions.clear()
        server._sessions.update(before)


def _register(sid: str) -> dict:
    session = {"agent": object(), "session_key": f"stored-{sid}", "running": False}
    with server._sessions_lock:
        server._sessions[sid] = session
    return session


def _reject(sid: str, method: str, caplog) -> tuple[dict | None, dict | None, list[logging.LogRecord]]:
    caplog.clear()
    token = server._current_rpc_method.set(method)
    try:
        with caplog.at_level(logging.DEBUG, logger="tui_gateway.server"):
            s, err = server._sess_nowait({"session_id": sid}, 7)
    finally:
        server._current_rpc_method.reset(token)
    records = [r for r in caplog.records if "session-scoped RPC rejected" in r.getMessage()
               or "stale runtime id" in r.getMessage()]
    return s, err, records


def test_pop_records_the_runtime_id_as_closed_by_this_process():
    _register("rt-closed")
    assert server._pop_session_by_id("rt-closed") is not None
    assert "rt-closed" in server._recently_closed_runtimes


def test_pop_of_unknown_id_records_nothing():
    assert server._pop_session_by_id("rt-never-held") is None
    assert "rt-never-held" not in server._recently_closed_runtimes


@pytest.mark.parametrize("method", QUIET_METHODS)
def test_poll_of_a_runtime_this_process_closed_is_info_not_warning(method, caplog):
    _register("rt-closed")
    server._pop_session_by_id("rt-closed")

    s, err, records = _reject("rt-closed", method, caplog)

    # The reply is the unchanged client contract.
    assert s is None
    assert err["error"]["code"] == 4001
    assert err["error"]["message"] == "session not found"
    assert [r.levelno for r in records] == [logging.INFO], [r.getMessage() for r in records]
    assert "rt-closed" in records[0].getMessage()
    assert method in records[0].getMessage()


def test_repeats_for_the_same_closed_id_drop_to_debug(caplog):
    _register("rt-closed")
    server._pop_session_by_id("rt-closed")

    levels = [
        [r.levelno for r in _reject("rt-closed", "approval.pending", caplog)[2]]
        for _ in range(4)
    ]

    assert levels == [[logging.INFO], [logging.DEBUG], [logging.DEBUG], [logging.DEBUG]]


@pytest.mark.parametrize("method", QUIET_METHODS)
def test_unknown_runtime_id_still_warns(method, caplog):
    """Never held by this process (restart, garbage, another profile's id): unchanged."""
    s, err, records = _reject("rt-never-held", method, caplog)

    assert s is None and err["error"]["code"] == 4001
    assert [r.levelno for r in records] == [logging.WARNING]
    assert "not in memory" in records[0].getMessage()


@pytest.mark.parametrize("method", LOUD_METHODS)
def test_user_intent_rpc_on_a_closed_runtime_still_warns(method, caplog):
    """prompt.submit & co: the #90428 'message vanished' trace must stay a WARNING."""
    _register("rt-closed")
    server._pop_session_by_id("rt-closed")

    s, err, records = _reject("rt-closed", method, caplog)

    assert s is None and err["error"]["code"] == 4001
    assert [r.levelno for r in records] == [logging.WARNING]


def test_live_session_is_untouched(caplog):
    live = _register("rt-live")

    s, err, records = _reject("rt-live", "approval.pending", caplog)

    assert s is live and err is None and records == []


def test_reregistered_id_is_live_again_and_not_treated_as_closed(caplog):
    _register("rt-1")
    server._pop_session_by_id("rt-1")
    live = _register("rt-1")

    s, err, records = _reject("rt-1", "approval.pending", caplog)

    assert s is live and err is None and records == []


def test_closed_registry_is_bounded():
    cap = server._RECENT_CLOSED_RUNTIME_CAP
    for i in range(cap + 25):
        _register(f"rt-{i}")
        server._pop_session_by_id(f"rt-{i}")

    assert len(server._recently_closed_runtimes) == cap
    assert "rt-0" not in server._recently_closed_runtimes  # oldest evicted
    assert f"rt-{cap + 24}" in server._recently_closed_runtimes


def test_approval_pending_rpc_end_to_end_keeps_4001_and_logs_info(caplog):
    """Through the real dispatcher: the wire reply is still a 4001 error."""
    _register("rt-closed")
    server._pop_session_by_id("rt-closed")

    with caplog.at_level(logging.DEBUG, logger="tui_gateway.server"):
        resp = server.handle_request(
            {"jsonrpc": "2.0", "id": 11, "method": "approval.pending", "params": {"session_id": "rt-closed"}})

    assert resp["error"]["code"] == 4001
    rejected = [r for r in caplog.records if "rt-closed" in r.getMessage()]
    assert rejected and all(r.levelno < logging.WARNING for r in rejected)
