"""Capacity queueing stays quiet without hiding actionable stranded work."""

from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops as ops
from hermes_cli.kanban_dispatch_health import dispatch_tick_is_starved


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


@pytest.mark.parametrize("kind,local,other,cap", [("host", 2, 3, 5), ("board", 5, 0, 5)])
def test_spawn_budget_records_capacity(board, monkeypatch, kind, local, other, cap):
    for i in range(local):
        tid = kb.create_task(board, title=f"running {i}", assignee="default")
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda _board: other)
    res = kbd.DispatchResult()
    budget = kbd._tick_spawn_budget(
        board, res, max_spawn=cap if kind == "board" else None,
        max_in_progress=cap if kind == "host" else None, board="default",
    )
    assert budget == (False, None)
    assert (res.capacity_held, res.capacity_running, res.capacity_limit) == (kind, cap, cap)
    assert kbd.describe_suppression([None, res]) == f"capacity_held={kind}(5/5)"


@pytest.mark.parametrize("field,value", [
    ("spawn_failed", ["card"]),
    ("respawn_guarded", [("card", "active_pr")]),
    ("respawn_guarded", [("card", "dependency-continuation")]),
    ("respawn_guarded", [("card", "blocker_auth")]),
    ("respawn_guarded", [("card", "recent_success")]),
    ("skipped_unassigned", ["card"]),
    ("skipped_locked", True),
    ("memory_pressure", "critical"),
    ("auto_blocked", ["card"]),
    ("rate_limited", ["card"]),
    ("owner_unavailable", ["card"]),
])
def test_actionable_holds_override_capacity(field, value):
    capped = kbd.DispatchResult(capacity_held="host")
    held = kbd.DispatchResult(**{field: value})
    assert dispatch_tick_is_starved(iter([None, capped, held]))
    setattr(capped, field, value)
    assert dispatch_tick_is_starved([capped])


def test_missing_results_are_unexplained():
    assert dispatch_tick_is_starved([])
    assert dispatch_tick_is_starved([None])
    assert dispatch_tick_is_starved([kbd.DispatchResult()])


def test_suppression_names_profile_and_human_lanes():
    res = kbd.DispatchResult(
        skipped_per_profile_capped=[("a", "dev", 1), ("b", "dev", 1)],
        skipped_nonspawnable=["human"],
    )
    assert kbd.describe_suppression([res]) == "nonspawnable=1, per_profile_capped=2"
    assert not dispatch_tick_is_starved([None, res])


def test_human_only_ready_is_not_spawnable(board):
    tid = kb.create_task(board, title="Human approval", assignee="trev")
    assert kb.get_task(board, tid).status == "ready"
    assert not kbd.has_spawnable_ready(board)
    kb.create_task(board, title="Agent work", assignee="default")
    assert kbd.has_spawnable_ready(board)


def _daemon_ticks(monkeypatch, ticks, *, real_ready=False):
    monkeypatch.setattr(kb, "init_db", lambda: None)
    if not real_ready:
        monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)

    def run(**kwargs):
        for res in ticks:
            kwargs["on_tick"](res)

    monkeypatch.setattr(kbd, "run_daemon", run)
    assert ops._cmd_daemon(Namespace(force=True, interval=1, max=None)) == 0


@pytest.mark.parametrize("kind", ["host", "board", "profile", "human"])
def test_daemon_expected_queueing_is_quiet(board, monkeypatch, capsys, kind):
    res = kbd.DispatchResult()
    if kind in {"host", "board"}:
        res.capacity_held, res.capacity_running, res.capacity_limit = kind, 5, 5
    if kind == "profile":
        res.skipped_per_profile_capped = [("card", "dev", 1)]
    if kind == "human":
        kb.create_task(board, title="Human approval", assignee="trev")
    _daemon_ticks(monkeypatch, [res] * 12, real_ready=kind == "human")
    assert "dispatcher stuck" not in capsys.readouterr().err


@pytest.mark.parametrize("cause", ["spawn_failed", "active_pr", "capped_active_pr", "unexplained"])
def test_daemon_warns_after_fresh_window(board, monkeypatch, capsys, cause):
    held = kbd.DispatchResult(capacity_held="host", capacity_running=5, capacity_limit=5)
    failed = kbd.DispatchResult()
    if cause == "spawn_failed":
        failed.spawn_failed = ["card"]
    if "active_pr" in cause:
        failed.respawn_guarded = [("card", "active_pr")]
    if cause == "capped_active_pr":
        failed.skipped_per_profile_capped = [("other", "dev", 1)]
    spawned = kbd.DispatchResult(spawned=[("card", "dev", "")])
    _daemon_ticks(monkeypatch, [failed] * 5 + [held] * 12 + [spawned] + [failed] * 6)
    warnings = capsys.readouterr().err
    assert warnings.count("dispatcher stuck") == 1
    assert "6 consecutive ticks" in warnings
    if "active_pr" in cause:
        assert "active_pr=1" in warnings and "last run ended without a recorded handoff" in warnings
    if cause == "spawn_failed":
        assert "Spawn failures recorded" in warnings
