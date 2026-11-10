"""Human conversations restart the block-loop count; automated retries do not."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        yield conn, kb.create_task(conn, title="Needs a decision")


def block(conn, tid, kind="transient"):
    assert kb.block_task(conn, tid, kind=kind, reason="Please advise")
    return kb.list_events(conn, tid)[-1]


@pytest.mark.parametrize("author", ["desktop", "dashboard", "trev", "trevor", " Desktop "])
def test_human_reply_between_blocks_restarts_the_count(board, author):
    conn, tid = board
    block(conn, tid)
    kb.add_comment(conn, tid, author, "Use option A")
    assert kb.unblock_task(conn, tid)
    event = block(conn, tid)
    task = kb.get_task(conn, tid)
    assert (task.status, task.block_recurrences) == ("blocked", 1)
    assert event.kind == "blocked"
    assert event.payload["human_reply_reset"] is True
    # A reply resets only the next block, never subsequent bare retries.
    assert kb.unblock_task(conn, tid)
    assert block(conn, tid).kind == "block_loop_detected"
    assert kb.get_task(conn, tid).block_recurrences == 2


@pytest.mark.parametrize("author", [None, "default", "neo-oncall", "worker", "quant", "auto-decomposer", "coder"])
def test_automated_or_bare_unblock_still_escalates(board, author):
    conn, tid = board
    block(conn, tid)
    if author:
        kb.add_comment(conn, tid, author, "UNBLOCK: retry now")
    assert kb.unblock_task(conn, tid)
    event = block(conn, tid)
    assert event.kind == "block_loop_detected"
    assert kb.get_task(conn, tid).status == "triage"
    assert "human_reply_reset" not in event.payload


def test_older_human_comment_does_not_reset(board):
    conn, tid = board
    kb.add_comment(conn, tid, "desktop", "Initial guidance")
    block(conn, tid)
    assert kb.unblock_task(conn, tid)
    assert block(conn, tid).kind == "block_loop_detected"


def test_different_kind_still_restarts_count(board):
    conn, tid = board
    block(conn, tid)
    assert kb.unblock_task(conn, tid)
    assert block(conn, tid, "capability").kind == "blocked"
    assert kb.get_task(conn, tid).block_recurrences == 1
