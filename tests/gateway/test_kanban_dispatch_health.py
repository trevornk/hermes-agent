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
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    res = kbd.DispatchResult(respawn_guarded=[("held", "active_pr")] if hold else [])
    res.spawn_failed = ["failed"] if failed else []
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (dict, kb, {}))
    monkeypatch.setattr(watchers, "_resolve_dispatcher_settings", lambda *a: SimpleNamespace(interval=1))
    dispatcher = SimpleNamespace(tick_once=lambda: [("default", res)], ready_nonempty=lambda: True)
    monkeypatch.setattr(watchers, "_KanbanDispatcher", lambda *a: dispatcher)
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(watchers, "_resolve_auto_decompose_settings", lambda *a: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", list)
    monkeypatch.setattr(runner, "_release_kanban_dispatcher_lock", lambda: None)
    ticks = []

    async def between(interval):
        ticks.append(1)
        if len(ticks) == 6:
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
    warning = caplog.text
    assert "dispatcher stuck" in warning
    if hold:
        assert "active_pr=1" in warning and "PR handoff and block/unblock history" in warning
    if failed:
        assert "spawn_failed=1" in warning and "Spawn failures recorded" in warning
    else:
        assert "Spawn failures recorded" not in warning
