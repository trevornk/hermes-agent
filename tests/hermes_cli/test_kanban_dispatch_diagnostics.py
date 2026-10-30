"""Read-only task diagnostics use the same live guard as dispatch."""
import contextlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.mark.parametrize("scenario", ["active_pr", "approved", "spawn_failed"])
def test_task_diagnostics_distinguish_hold_continuation_and_failure(tmp_path, monkeypatch, capsys, scenario):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    clock = [int(time.time()) - 100]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"failure_limit": 2}})
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Resume the existing PR", assignee="dev")
        kb.claim_task(conn, tid)
        if scenario == "spawn_failed":
            kbd._record_task_failure(conn, tid, error="missing worker executable", outcome="spawn_failed",
                                     release_claim=True, end_run=True, failure_limit=2)
            kb.claim_task(conn, tid)
            kbd._record_task_failure(conn, tid, error="missing worker executable", outcome="spawn_failed",
                                     release_claim=True, end_run=True, failure_limit=2)
        else:
            clock[0] += 1
            kb.add_comment(conn, tid, author="dev", body="https://github.com/example/repo/pull/8")
            clock[0] += 5
            assert kb.block_task(conn, tid, kind="needs_input", reason="May I continue?")
            clock[0] += 5
            assert kb.unblock_task(conn, tid)
            if scenario == "active_pr":
                # A later attempt crashed without a handoff; its PR stays guarded.
                assert kb.claim_task(conn, tid) is not None
                kbd._record_task_failure(conn, tid, error="worker exited 1", outcome="crashed",
                                         release_claim=True, end_run=True)
                clock[0] += 1
                kb.add_comment(conn, tid, author="desktop", body="Approved https://github.com/example/repo/pull/8")
        # A historical event must not be mistaken for the current predicate.
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
        conn.execute("PRAGMA query_only = ON")
        monkeypatch.setattr(kbc, "connect_closing", lambda: contextlib.nullcontext(conn))
        assert cli._cmd_diagnostics(SimpleNamespace(task=tid, json=True, severity=None)) == 0
        rows = json.loads(capsys.readouterr().out)
        diags = next(row["diagnostics"] for row in rows if row["task_id"] == tid)
        from plugins.kanban.dashboard.plugin_api import _compute_task_diagnostics
        dashboard = _compute_task_diagnostics(conn, task_ids=[tid]).get(tid, [])
        assert dashboard == diags
        holds = [d for d in diags if d["kind"] == "dispatch_guarded"]
        failures = [d for d in diags if d["kind"] == "repeated_failures"]
        if scenario == "active_pr":
            assert len(holds) == 1 and holds[0]["data"]["reason"] == "active_pr"
            assert not failures
        elif scenario == "approved":
            assert not holds and not failures
        else:
            assert not holds
            assert len(failures) == 1
            assert failures[0]["data"]["most_recent_outcome"] == "spawn_failed"
