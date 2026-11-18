"""The embedded dispatcher keeps starvation visible with an accurate cause."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers as watchers
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.mark.parametrize("hold,failed", [(True, False), (False, True), (True, True), (False, False)])
def test_gateway_warns_with_actionable_cause(monkeypatch, caplog, hold, failed):
    res = kbd.DispatchResult(respawn_guarded=[("held", "active_pr")] if hold else [])
    res.spawn_failed = ["failed"] if failed else []
    _run_ticks(monkeypatch, caplog, [[("default", res)]] * 6)
    warning = caplog.text
    assert "dispatcher stuck" in warning
    if hold:
        assert "active_pr=1" in warning and "last run ended without a recorded handoff" in warning
    if failed:
        assert "spawn_failed=1" in warning and "Spawn failures recorded" in warning
    else:
        assert "Spawn failures recorded" not in warning


def _run_ticks(monkeypatch, caplog, ticks, ready=lambda: True):
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (dict, kb, {}))
    monkeypatch.setattr(watchers, "_resolve_dispatcher_settings", lambda *a: SimpleNamespace(interval=1))
    sequence = iter(ticks)
    dispatcher = SimpleNamespace(tick_once=lambda: next(sequence), ready_nonempty=ready)
    monkeypatch.setattr(watchers, "_KanbanDispatcher", lambda *a: dispatcher)
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(watchers, "_resolve_auto_decompose_settings", lambda *a: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", list)
    monkeypatch.setattr(runner, "_release_kanban_dispatcher_lock", lambda: None)
    completed = []

    async def between(interval):
        completed.append(1)
        if len(completed) == len(ticks):
            runner._running = False

    async def immediate(*a, **kw):
        return None

    async def thread(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(runner, "_sleep_between_ticks", between, raising=False)
    monkeypatch.setattr(watchers.asyncio, "sleep", immediate)
    monkeypatch.setattr(watchers, "_to_thread_process_service", thread)
    with caplog.at_level(logging.WARNING):
        asyncio.run(runner._kanban_dispatcher_watcher())


@pytest.mark.parametrize("kind", ["host", "board", "profile", "human"])
def test_expected_queueing_stays_quiet(monkeypatch, caplog, kind):
    res = kbd.DispatchResult()
    if kind in {"host", "board"}:
        res.capacity_held, res.capacity_running, res.capacity_limit = kind, 5, 5
    elif kind == "profile":
        res.skipped_per_profile_capped = [("card", "dev", 1)]
    _run_ticks(monkeypatch, caplog, [[("default", res)]] * 12, ready=lambda: kind != "human")
    assert "dispatcher stuck" not in caplog.text


@pytest.mark.parametrize("same_board", [True, False])
def test_capacity_cannot_hide_active_pr(monkeypatch, caplog, same_board):
    capped = kbd.DispatchResult(capacity_held="host", capacity_running=5, capacity_limit=5)
    capped.skipped_per_profile_capped = [("card", "dev", 1)]
    guarded = kbd.DispatchResult(respawn_guarded=[("held", "active_pr")])
    if same_board:
        capped.respawn_guarded = guarded.respawn_guarded
        results = [("a", capped)]
    else:
        results = [("a", capped), ("b", guarded)]
    _run_ticks(monkeypatch, caplog, [results] * 6)
    assert "dispatcher stuck" in caplog.text
    assert "active_pr=1" in caplog.text
    assert "capacity_held=host(5/5)" in caplog.text
    assert "per_profile_capped=1" in caplog.text


def test_capacity_release_starts_fresh_failure_window(monkeypatch, caplog):
    empty = [("a", kbd.DispatchResult())]
    capped = [("a", kbd.DispatchResult(capacity_held="host"))]
    spawned = [("a", kbd.DispatchResult(spawned=[("card", "dev", "")]))]
    _run_ticks(monkeypatch, caplog, [empty] * 5 + [capped] * 12 + [spawned] + [empty] * 6)
    warnings = [r.message for r in caplog.records if "dispatcher stuck" in r.message]
    assert len(warnings) == 1
    assert "6 consecutive ticks" in warnings[0]
