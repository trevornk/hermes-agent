"""Decision briefs preserve card content and obey worker mutation boundaries."""
import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def section(brief):
    return f"{kb.DECISION_BRIEF_START}\n{brief}\n{kb.DECISION_BRIEF_END}"


def test_brief_upserts_at_top_idempotently(worker_env):
    original = "  original\n\todd whitespace  \n\nlast line\n\n\n"
    with kbc.connect() as conn:
        assert kb.edit_task(conn, worker_env, body=original)
        out = json.loads(kt._handle_block({"reason": "Choose", "kind": "needs_input", "brief": "  Decision A  "}))
        assert out.get("brief_set") is True, out
        expected = section("Decision A") + "\n\n" + original
        assert kb.get_task(conn, worker_env).body == expected
        assert kb.set_decision_brief(conn, worker_env, "Decision A")
        assert kb.get_task(conn, worker_env).body == expected
        assert kb.set_decision_brief(conn, worker_env, "Decision B")
        body = kb.get_task(conn, worker_env).body
        assert body == section("Decision B") + "\n\n" + original
        assert body.count(kb.DECISION_BRIEF_START) == 1
        event = kb.list_events(conn, worker_env)[-1]
        assert event.kind == "edited"
        assert event.payload == {"fields": ["body"], "decision_brief": True}


@pytest.mark.parametrize("body", [None, ""])
def test_empty_body_has_no_trailing_blank_lines(worker_env, body):
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, worker_env))
        assert kb.set_decision_brief(conn, worker_env, "Decision")
        assert kb.get_task(conn, worker_env).body == section("Decision")
        assert kb.set_decision_brief(conn, worker_env, "Replacement")
        assert kb.get_task(conn, worker_env).body == section("Replacement")
        assert not kb.set_decision_brief(conn, "unknown", "Decision")


@pytest.mark.parametrize("brief", ["", " \n\t", "x" * 6001, kb.DECISION_BRIEF_START, kb.DECISION_BRIEF_END, None, 42])
def test_invalid_brief_leaves_task_running(worker_env, brief):
    out = json.loads(kt._handle_block({"reason": "Choose", "brief": brief}))
    assert out.get("error"), out
    with kbc.connect() as conn:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert not task.body
        with pytest.raises(ValueError):
            kb.set_decision_brief(conn, worker_env, brief)


def test_other_task_id_is_refused(worker_env):
    with kbc.connect() as conn:
        other = kb.create_task(conn, title="Other", body="Untouched")
        out = json.loads(kt._handle_block({"task_id": other, "reason": "Choose", "brief": "Decision"}))
        assert out.get("error"), out
        assert kb.get_task(conn, other).body == "Untouched"
        assert kb.get_task(conn, worker_env).status == "running"


def test_failed_block_does_not_set_brief(worker_env):
    with kbc.connect() as conn:
        assert kb.block_task(conn, worker_env, kind="needs_input")
        assert kb.edit_task(conn, worker_env, body="Untouched")
        out = json.loads(kt._handle_block({"reason": "Choose", "brief": "Decision"}))
        assert out.get("error"), out
        assert kb.get_task(conn, worker_env).body == "Untouched"


def test_brief_is_redacted_like_reason(worker_env):
    secret = "ghp_" + "A" * 40
    out = json.loads(kt._handle_block({"reason": "Choose", "brief": f"token: {secret}", "kind": "dependency"}))
    assert out.get("brief_set") is True, out
    with kbc.connect() as conn:
        body = kb.get_task(conn, worker_env).body
        assert secret not in body
        assert body == section(kt._redact(f"token: {secret}"))


def test_delegated_child_cli_still_refuses_edit(worker_env):
    from agent.delegation_context import delegated_child_context
    from hermes_cli.kanban import _is_delegated_child_cli_mutation

    with delegated_child_context():
        assert _is_delegated_child_cli_mutation(argparse.Namespace(kanban_action="edit"))
