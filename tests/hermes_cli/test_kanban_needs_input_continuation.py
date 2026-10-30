"""Only a run-bound needs_input block and a later explicit unblock resume an existing PR."""

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR = "https://github.com/example/repo/pull/8"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    clock = [int(time.time()) - 100]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    kb.init_db()
    with kbc.connect() as conn:
        yield conn, clock


def _blocked(board, kind="needs_input", *, bound=False):
    conn, clock = board
    tid = kb.create_task(conn, title="Finish existing PR", assignee="dev")
    run = kb.claim_task(conn, tid)
    if bound:
        import os
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound", {"pid": os.getpid(), "claim_lock": run.claim_lock},
                             run_id=run.current_run_id)
    clock[0] += 1
    kb.add_comment(conn, tid, author="dev", body=f"Continue {PR} on the existing branch")
    clock[0] += 5
    assert kb.block_task(conn, tid, reason="Need a decision", kind=kind,
                         expected_run_id=run.current_run_id)
    return tid, run.current_run_id


def _unblock(board, tid):
    conn, clock = board
    clock[0] += 10
    assert kb.unblock_task(conn, tid)
    clock[0] += 1


def test_real_block_unblock_dispatches_same_task_with_prior_pr_context(board, monkeypatch):
    conn, clock = board
    tid, run_id = _blocked(board)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    clock[0] += 1
    kb.add_comment(conn, tid, author="desktop", body="Approved; finish the existing PR")
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    _unblock(board, tid)
    # Harmless repeated holds are observations, never new authorization.
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr("hermes_cli.kanban_db_workspace.resolve_workspace",
                        lambda *a, **kw: str(Path.cwd()))
    contexts = []

    def spawn(task, workspace, **kwargs):
        assert task.id == tid and task.assignee == "dev"
        assert task.current_run_id != run_id
        contexts.append(kb.build_worker_context(conn, task.id))

    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert [s[0] for s in result.spawned] == [tid]
    assert PR in contexts[0] and "Need a decision" in contexts[0]
    assert "Approved; finish the existing PR" in contexts[0]
    assert len(kb.list_tasks(conn)) == 1


@pytest.mark.parametrize("kind", [None, "capability", "transient"])
def test_other_block_kinds_cannot_authorize_continuation(board, kind):
    conn, _ = board
    tid, _ = _blocked(board, kind)
    if kb.get_task(conn, tid).status == "blocked":
        _unblock(board, tid)
    else:
        kb.recompute_ready(conn)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("mutation", [
    "no_unblock", "same_assign", "arbitrary_ready", "unbound_block", "wrong_run_block",
    "missing_kind", "bad_payload", "new_pr", "pr_tie", "block_tie", "run_tie",
    "bad_pr_time", "bad_block_time", "bad_unblock_time", "bad_run_time",
    "status_after", "status_between", "reclaim_after", "reblock_after", "newer_crash",
    "newer_live", "different_profile", "backdated_transition", "bad_transition_time",
])
def test_continuation_fails_closed(board, mutation):
    conn, clock = board
    tid, _run_id = _blocked(board)
    if mutation not in {"no_unblock", "same_assign", "arbitrary_ready"}:
        _unblock(board, tid)
    if mutation == "same_assign":
        assert kb.assign_task(conn, tid, "dev")
    if mutation == "arbitrary_ready":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            kb._append_event(conn, tid, "status", {"status": "ready"})
    if mutation == "new_pr":
        kb.add_comment(conn, tid, author="desktop", body=f"Approved {PR}")
    if mutation in {"newer_crash", "newer_live"}:
        run = kb.claim_task(conn, tid)
        assert run is not None
        if mutation == "newer_crash":
            clock[0] += 1
            kbd._record_task_failure(conn, tid, error="worker exited 1", outcome="crashed",
                                     release_claim=True, end_run=True)
    if mutation == "wrong_run_block":
        other = kb.create_task(conn, title="Other", assignee="dev")
        other_run = kb.claim_task(conn, other)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET run_id = ? WHERE task_id = ? AND kind = 'blocked'",
                         (other_run.current_run_id, tid))
    _tamper_block(conn, tid, mutation)
    _tamper_timestamp(conn, tid, mutation)
    _append_transition(conn, clock, tid, mutation)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"



def _tamper_block(conn, tid, mutation):
    with kb.write_txn(conn):
        if mutation == "unbound_block":
            conn.execute("UPDATE task_events SET run_id = NULL WHERE task_id = ? AND kind = 'blocked'", (tid,))
        if mutation in {"missing_kind", "bad_payload"}:
            payload = json.dumps({"reason": "needs_input"}) if mutation == "missing_kind" else "{bad"
            conn.execute("UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'blocked'", (payload, tid))
        if mutation == "different_profile":
            conn.execute("UPDATE tasks SET assignee = 'other' WHERE id = ?", (tid,))


def _tamper_timestamp(conn, tid, mutation):
    ties = {"pr_tie": ("task_comments", "created_at", ""),
            "block_tie": ("task_events", "created_at", " AND kind = 'blocked'"),
            "run_tie": ("task_runs", "ended_at", "")}
    malformed = {"bad_pr_time": ("task_comments", "created_at", ""),
                 "bad_block_time": ("task_events", "created_at", " AND kind = 'blocked'"),
                 "bad_unblock_time": ("task_events", "created_at", " AND kind = 'unblocked'"),
                 "bad_run_time": ("task_runs", "ended_at", "")}
    with kb.write_txn(conn):
        if mutation in ties:
            table, column, where = ties[mutation]
            conn.execute(f"UPDATE {table} SET {column} = "
                         "(SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'unblocked') "
                         f"WHERE task_id = ?{where}", (tid, tid))
        if mutation in malformed:
            table, column, where = malformed[mutation]
            conn.execute(f"UPDATE {table} SET {column} = 'unreadable' WHERE task_id = ?{where}", (tid,))


def _append_transition(conn, clock, tid, mutation):
    if mutation not in {"status_after", "status_between", "reclaim_after", "reblock_after",
                        "backdated_transition", "bad_transition_time"}:
        return
    with kb.write_txn(conn):
        kind = {"reclaim_after": "reclaimed", "reblock_after": "blocked"}.get(mutation, "status")
        kb._append_event(conn, tid, kind, {"status": "ready"})
        if mutation in {"status_between", "backdated_transition", "bad_transition_time"}:
            stamp = "bad" if mutation == "bad_transition_time" else clock[0] - 5
            conn.execute("UPDATE task_events SET created_at = ? WHERE id = (SELECT MAX(id) FROM task_events)", (stamp,))

def test_a_consumed_continuation_does_not_authorize_a_later_untyped_block(board):
    conn, clock = board
    tid, _ = _blocked(board)
    _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) is None
    assert kb.claim_task(conn, tid) is not None
    clock[0] += 5
    assert kb.block_task(conn, tid, reason="Stop again")
    _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("stage", ["workspace", "spawn"])
def test_failed_continuation_launch_is_reported_and_consumes_proof(board, monkeypatch, stage):
    conn, _ = board
    tid, _ = _blocked(board)
    _unblock(board, tid)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)

    def fail(*args, **kwargs):
        raise RuntimeError("test launch failure")

    monkeypatch.setattr("hermes_cli.kanban_db_workspace.resolve_workspace",
                        fail if stage == "workspace" else lambda *a, **kw: str(Path.cwd()))
    result = kbd.dispatch_once(conn, spawn_fn=fail)
    assert not result.spawned and result.spawn_failed == [tid]
    latest = kb.list_runs(conn, tid)[-1]
    assert latest.outcome == "spawn_failed"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_classifying_a_crash_in_place_does_not_make_it_a_needs_input_run(board):
    conn, _ = board
    tid = kb.create_task(conn, title="Crashed", assignee="dev")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR)
    kbd._record_task_failure(conn, tid, error="worker exited 1", outcome="crashed",
                             release_claim=True, end_run=True, failure_limit=1)
    assert kb.block_task(conn, tid, kind="needs_input", reason="Human help")
    _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("table,column,where", [
    ("task_runs", "started_at", ""),
    ("task_runs", "ended_at", ""),
    ("task_events", "created_at", " AND kind = 'blocked'"),
    ("task_events", "created_at", " AND kind = 'unblocked'"),
    ("task_comments", "created_at", ""),
])
@pytest.mark.parametrize("stamp", ["not-a-stamp", 1793284600.5, "2026-10-29T12:00:00"])
def test_malformed_proof_stamps_never_authorize(board, table, column, where, stamp):
    conn, _ = board
    tid, _ = _blocked(board)
    _unblock(board, tid)
    with kb.write_txn(conn):
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE task_id = ?{where}", (stamp, tid))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("after_unblock", [False, True])
def test_worker_result_is_observational_for_the_same_blocked_run(board, after_unblock):
    from gateway.session_kanban import _record_worker_result

    conn, clock = board
    tid, run_id = _blocked(board, bound=True)
    run = kb.list_runs(conn, tid)[-1]
    if after_unblock:
        _unblock(board, tid)
    clock[0] += 1
    _record_worker_result({"db": str(kb.kanban_db_path()), "task_id": tid,
                           "run_id": run_id, "claim_lock": run.claim_lock}, 0, "blocked for a decision")
    if not after_unblock:
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("tamper", ["run", "claim", "pid", "code", "payload", "time", "binding"])
def test_worker_result_must_match_the_blocked_runs_binding(board, tamper):
    from gateway.session_kanban import _record_worker_result

    conn, clock = board
    tid, run_id = _blocked(board, bound=True)
    run = kb.list_runs(conn, tid)[-1]
    clock[0] += 1
    _record_worker_result({"db": str(kb.kanban_db_path()), "task_id": tid,
                           "run_id": run_id, "claim_lock": run.claim_lock}, 0, "")
    _unblock(board, tid)
    with kb.write_txn(conn):
        if tamper in {"claim", "pid", "code", "payload"}:
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result'", (tid,)).fetchone()
            data = json.loads(row["payload"])
            field, value = {"claim": ("claim_lock", "different"), "pid": ("pid", -1),
                            "code": ("exit_code", 1), "payload": ("exit_code", True)}[tamper]
            data[field] = value
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='worker_result'", (json.dumps(data), tid))
        if tamper == "run":
            conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='worker_result'", (tid,))
        if tamper == "time":
            conn.execute("UPDATE task_events SET created_at='bad' WHERE task_id=? AND kind='worker_result'", (tid,))
        if tamper == "binding":
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='worker_bound'", (tid,))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
