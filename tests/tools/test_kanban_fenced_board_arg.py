"""A fenced worker's explicit ``board=`` must never be silently ignored.

Dispatched workers (``HERMES_KANBAN_TASK``) and delegated descendants resolve
every board through the dispatcher's ``HERMES_KANBAN_DB`` pin — that is the
"workers physically cannot see other boards" isolation (5ec6baa / 387c78c8f7).
Before this fix, ``kanban_create(board="default")`` from a worker pinned to
another board quietly wrote the card to the worker's OWN board and reported
success, and ``kanban_show(board="default", task_id=<a default-board card>)``
answered "not found". The isolation stays; the silent misroute becomes a loud,
actionable refusal (tool layer and ``hermes kanban --board``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def fenced_worker(monkeypatch, tmp_path):
    """A worker dispatched from board ``own`` while the ``default`` board holds
    a separate card — the exact quant-general -> default shape."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME",
                "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()

    kb.init_db()  # default board -> <root>/kanban.db
    with kbc.connect_closing(board="default") as conn:
        default_tid = kb.create_task(conn, title="lives-on-default", assignee="someone")

    kb.create_board("own")
    own_db = kb.kanban_db_path(board="own")
    with kbc.connect_closing(board="own") as conn:
        worker_tid = kb.create_task(conn, title="worker-card", assignee="test-worker")
        kb.claim_task(conn, worker_tid)
        run_id = kb._current_run_id(conn, worker_tid)

    monkeypatch.setenv("HERMES_KANBAN_DB", str(own_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "own")
    monkeypatch.setenv("HERMES_KANBAN_TASK", worker_tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"own_db": own_db, "default_tid": default_tid, "worker_tid": worker_tid,
            "default_db": home / "kanban.db"}


def _titles(db: Path) -> list[str]:
    import sqlite3
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute("SELECT title FROM tasks")]


def test_create_with_foreign_board_is_refused_not_misrouted(fenced_worker):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create(
        {"title": "meant-for-default", "assignee": "default", "board": "default"}))
    assert out.get("error"), out
    assert "default" in out["error"] and "own" in out["error"]
    # Nothing landed anywhere: not silently on the worker's own board ...
    assert "meant-for-default" not in _titles(fenced_worker["own_db"])
    # ... and the fence still holds for the foreign board.
    assert "meant-for-default" not in _titles(fenced_worker["default_db"])


def test_show_with_foreign_board_is_refused_not_reported_missing(fenced_worker):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_show(
        {"task_id": fenced_worker["default_tid"], "board": "default"}))
    assert out.get("error"), out
    assert "not found" not in out["error"], out


@pytest.mark.parametrize("board", ["own", "OWN", None])
def test_own_board_or_omitted_board_still_works(fenced_worker, board):
    from tools import kanban_tools as kt
    args = {"title": f"own-{board}", "assignee": "test-worker"}
    if board is not None:
        args["board"] = board
    out = json.loads(kt._handle_create(args))
    assert out.get("ok"), out
    assert f"own-{board}" in _titles(fenced_worker["own_db"])


def test_unfenced_caller_explicit_board_still_wins(fenced_worker, monkeypatch):
    """Outside the fence (e.g. a gateway/routing layer carrying the pin but no
    HERMES_KANBAN_TASK) an explicit board keeps overriding the pin."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_show(
        {"task_id": fenced_worker["default_tid"], "board": "default"}))
    assert out.get("task", {}).get("title") == "lives-on-default", out


def test_cli_board_flag_from_fenced_worker_is_refused(fenced_worker, capsys):
    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", "--board", "default", "create", "cli-cross"])
    rc = kc.kanban_command(args)
    assert rc != 0
    assert "cli-cross" not in _titles(fenced_worker["own_db"])
    assert "cli-cross" not in _titles(fenced_worker["default_db"])
    assert "own" in capsys.readouterr().err
