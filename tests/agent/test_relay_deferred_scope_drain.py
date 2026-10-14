"""A logical LLM scope that finishes under another live scope is closed by the turn-close drain.

Title generation runs beside the turn on a thread and shares the session's single scope stack.
When a call finishes while a sibling's scope sits above its own, ``_complete_logical`` cannot pop
it, stores the real result (``defer_scope_output``) and leaves the pop to the drain in
``RelayRuntime._pop_with_drain``. The drain is then reclaiming a call that completed normally, so it:

* closes the scope at the time the call FINISHED (not at drain time, which would stretch the span
  to the whole turn), and
* does not log a WARNING (only a scope that never completed is an orphan).

Both sequences below are what production shows: the title scope sitting above the main call's,
and the main call's own first scope deferred beneath the title scope ("drained 2").
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

import pytest

from agent import relay_llm, relay_runtime

RUNTIME_LOGGER = "agent.relay_runtime"


@pytest.fixture()
def turn_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(), session_id="session-1", platform="cli",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    lease.host.retain_managed_execution("test.relay_deferred_scope_drain")
    try:
        yield lease, turn
    finally:
        lease.host.release_managed_execution("test.relay_deferred_scope_drain")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


def _start(request_id, call_role="primary"):
    """Open a logical call; ``defer_logical_completion`` leaves its scope on the stack."""
    relay_llm.execute(
        {"model": "test-model", "messages": []},
        lambda _request: {"content": "ok"},
        session_id="session-1",
        name="test-provider",
        model_name="test-model",
        metadata={"api_mode": "custom", "api_request_id": request_id, "call_role": call_role},
        defer_logical_completion=True,
    )


def _record_pops(monkeypatch, relay):
    """Wrap ``scope.pop`` and return the list of (handle, kwargs) it receives."""
    pops: list[tuple[object, dict]] = []
    real_pop = relay.scope.pop

    def recording_pop(handle, **kwargs):
        pops.append((handle, kwargs))
        return real_pop(handle, **kwargs)

    monkeypatch.setattr(relay.scope, "pop", recording_pop)
    return pops


def _title_under_main(turn):
    """Title call whose scope outlives the main call's: it finishes under the main scope."""
    _start("title-req", call_role="auxiliary")
    _start("main-req")
    handles = {"title": turn.logical_llm_calls["title-req"], "main": turn.logical_llm_calls["main-req"]}
    t_before = datetime.now(UTC)
    relay_llm.complete_logical_call("title-req", outcome="success")  # main scope on top -> deferred
    t_after = datetime.now(UTC)
    relay_llm.complete_logical_call("main-req", outcome="success")  # on top -> popped normally
    return handles, (t_before, t_after)


def _drain_pop(pops, handle):
    matches = [kw for h, kw in pops if relay_runtime._same_handle(h, handle) and kw.get("output")]
    assert len(matches) == 1, f"expected exactly one close of the scope, got {matches}"
    return matches[0]


def test_deferred_completed_scope_drain_is_not_a_warning(turn_ctx, caplog):
    _lease, turn = turn_ctx
    _title_under_main(turn)

    with caplog.at_level(logging.DEBUG, logger=RUNTIME_LOGGER):
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")

    assert not [r for r in caplog.records if r.name == RUNTIME_LOGGER and r.levelno >= logging.WARNING]
    drained = [r for r in caplog.records if r.name == RUNTIME_LOGGER and "deferred scope" in r.getMessage()]
    assert [r.levelno for r in drained] == [logging.DEBUG], "the reclaim stays visible at DEBUG"
    assert drained[0].args[0] == 1


def test_deferred_scope_closes_at_its_own_completion_time(turn_ctx, monkeypatch):
    lease, turn = turn_ctx
    pops = _record_pops(monkeypatch, lease.host.relay)
    handles, (t_before, t_after) = _title_under_main(turn)

    relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")

    kwargs = _drain_pop(pops, handles["title"])
    assert kwargs["output"] == {"outcome": "success"}, "the real result is still what the span carries"
    ended_at = kwargs.get("timestamp")
    assert isinstance(ended_at, datetime) and ended_at.tzinfo is not None, kwargs
    assert t_before <= ended_at <= t_after, "end time is when the call finished, not when the drain ran"


def test_primary_call_deferred_under_aux_scope_is_also_reclaimed_quietly(turn_ctx, monkeypatch, caplog):
    """Production's usual "drained 2": the title scope lands just above main call #1, so call #1
    finishes under it (deferred); the title finishes under call #2 (deferred); call #2 pops. The
    turn close then reclaims BOTH, and both are completed calls with their own end times."""
    lease, turn = turn_ctx
    pops = _record_pops(monkeypatch, lease.host.relay)
    _start("main-1")
    _start("title-req", call_role="auxiliary")
    handles = {"main-1": turn.logical_llm_calls["main-1"], "title": turn.logical_llm_calls["title-req"]}
    relay_llm.complete_logical_call("main-1", outcome="success")  # title scope on top -> deferred
    _start("main-2")
    relay_llm.complete_logical_call("title-req", outcome="success")  # main-2 on top -> deferred
    relay_llm.complete_logical_call("main-2", outcome="success")

    with caplog.at_level(logging.DEBUG, logger=RUNTIME_LOGGER):
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")

    assert not [r for r in caplog.records if r.name == RUNTIME_LOGGER and r.levelno >= logging.WARNING]
    drained = [r for r in caplog.records if "deferred scope" in r.getMessage()]
    assert [r.args[0] for r in drained] == [2]
    for name in ("main-1", "title"):
        ended_at = _drain_pop(pops, handles[name]).get("timestamp")
        assert isinstance(ended_at, datetime) and ended_at.tzinfo is not None, name


def test_scope_that_never_completed_is_still_a_warned_orphan(turn_ctx, monkeypatch, caplog):
    """An abandoned scope above the turn (interrupt mid-call) has no stored result: it keeps the
    WARNING and closes as cancelled, at drain time."""
    lease, turn = turn_ctx
    relay = lease.host.relay
    pops = _record_pops(monkeypatch, relay)
    stray = lease.host.run_in_session(
        lease.session, relay.scope.push, relay_runtime.LOGICAL_LLM_SCOPE, relay.ScopeType.Function,
        handle=None, input={},
    )

    with caplog.at_level(logging.DEBUG, logger=RUNTIME_LOGGER):
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")

    warned = [r for r in caplog.records if r.name == RUNTIME_LOGGER and r.levelno == logging.WARNING]
    assert len(warned) == 1 and "orphaned scope" in warned[0].getMessage() and warned[0].args[0] == 1
    kwargs = _drain_pop(pops, stray)
    assert kwargs["output"] == {"outcome": "cancelled", "hermes.orphan_drain": True}
    assert kwargs.get("timestamp") is None


def test_mixed_drain_warns_only_for_the_true_orphan(turn_ctx, monkeypatch, caplog):
    lease, turn = turn_ctx
    relay = lease.host.relay
    pops = _record_pops(monkeypatch, relay)
    handles, _ = _title_under_main(turn)
    # An abandoned scope pushed above everything: no completion, no stored result.
    stray = lease.host.run_in_session(
        lease.session, relay.scope.push, relay_runtime.LOGICAL_LLM_SCOPE, relay.ScopeType.Function,
        handle=None, input={},
    )

    with caplog.at_level(logging.DEBUG, logger=RUNTIME_LOGGER):
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")

    warned = [r for r in caplog.records if r.name == RUNTIME_LOGGER and r.levelno == logging.WARNING]
    assert len(warned) == 1 and warned[0].args[0] == 1, "only the stray scope is an orphan"
    quiet = [r for r in caplog.records if "deferred scope" in r.getMessage()]
    assert [(r.levelno, r.args[0]) for r in quiet] == [(logging.DEBUG, 1)]
    assert _drain_pop(pops, handles["title"]).get("timestamp") is not None
    assert _drain_pop(pops, stray).get("timestamp") is None


def test_deferred_scope_end_event_is_exported_at_completion_time(tmp_path, monkeypatch):
    """End to end through the real ATOF file sink: the exported ``scope end`` event of the
    deferred logical call is stamped when the call finished, before the turn's own end event."""
    relay_pkg = pytest.importorskip("nemo_relay")
    if getattr(relay_pkg, "_native", None) is None:
        pytest.skip("NeMo Relay native binding is unavailable on this platform")

    hermes_home, atof_dir = tmp_path / "hermes-home", tmp_path / "atof"
    hermes_home.mkdir()
    atof_dir.mkdir()
    config = tmp_path / "plugins.toml"
    config.write_text(
        f"""version = 1

[[components]]
kind = "observability"
enabled = true

[components.config]
version = 4

[components.config.atof]
enabled = true

[[components.config.atof.sinks]]
type = "file"
output_directory = {json.dumps(str(atof_dir))}
filename = "events.jsonl"
mode = "overwrite"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv(relay_runtime.RELAY_PLUGINS_CONFIG_ENV, str(config))
    relay_runtime._reset_for_tests()
    profile_key = relay_runtime.current_profile_key()
    coordinator = relay_runtime.SESSION_COORDINATOR
    try:
        lease = coordinator.acquire_conversation(profile_key=profile_key, session_id="session-1", platform="cli")
        turn = coordinator.begin_turn(lease, turn_id="turn-1", task_id="task-1")
        lease.host.retain_managed_execution("test.relay_deferred_scope_drain")
        try:
            handles, _ = _title_under_main(turn)
            title_uuid = str(handles["title"].uuid)
            # The turn keeps running well after the title call finished.
            time.sleep(0.3)  # health: allow HX013 -- the gap between completion and turn close is the behaviour under test
        finally:
            coordinator.end_turn(turn, outcome="success")
            coordinator.finalize_conversation(profile_key=profile_key, session_id="session-1")
    finally:
        relay_runtime._reset_for_tests()

    events = [
        json.loads(line)
        for line in (atof_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    def end_of(uuid=None, name=None):
        ends = [
            datetime.fromisoformat(e["timestamp"]) for e in events
            if e.get("kind") == "scope" and e.get("scope_category") == "end"
            and (uuid is None or e.get("uuid") == uuid) and (name is None or e.get("name") == name)
        ]
        assert len(ends) == 1, (uuid, name, ends)
        return ends[0]

    title_end, turn_end = end_of(uuid=title_uuid), end_of(name="hermes.turn")
    assert (turn_end - title_end).total_seconds() >= 0.25, (
        f"deferred scope closed {(turn_end - title_end).total_seconds():.3f}s before the turn: "
        "it must keep its completion time, not take the drain time"
    )
