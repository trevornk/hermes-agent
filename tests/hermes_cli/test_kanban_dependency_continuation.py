"""A run-bound dependency park resumes its PR only after a causally later promotion."""

import json
import os
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


def _park(board, *, kind="dependency", bound=False):
    conn, clock = board
    parent = kb.create_task(conn, title="Gate parent")
    tid = kb.create_task(conn, title="Finish existing PR", assignee="dev")
    run = kb.claim_task(conn, tid)
    if bound:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_bound", {"pid": os.getpid(), "claim_lock": run.claim_lock},
                             run_id=run.current_run_id)
    clock[0] += 1
    kb.add_comment(conn, tid, author="dev", body=f"Continue {PR} on the existing branch")
    clock[0] += 1
    kb.link_tasks(conn, parent, tid, expected_child_run_id=run.current_run_id)
    clock[0] += 5
    if kind == "crash":
        kbd._record_task_failure(conn, tid, error="worker exited 1", outcome="crashed",
                                 release_claim=True, end_run=True)
    else:
        assert kb.block_task(conn, tid, reason="Waiting for gate parent", kind=kind,
                             expected_run_id=run.current_run_id)
    clock[0] += 1
    kb.add_comment(conn, tid, author="dev", body=f"Existing PR remains {PR}")
    return tid, parent, run


def _promote(board, parent):
    conn, clock = board
    clock[0] += 10
    assert kb.complete_task(conn, parent, result="Gate finished")
    clock[0] += 1
    kb.recompute_ready(conn)
    clock[0] += 1


def test_dependency_release_dispatches_same_task_with_prior_pr_context(board, monkeypatch):
    conn, clock = board
    tid, parent, run = _park(board)
    assert kb.get_task(conn, tid).status == "todo"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    # Links and observations after parking do not themselves release the gate.
    clock[0] += 1
    kb.link_tasks(conn, parent, tid)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "heartbeat")
    _promote(board, parent)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
    assert kbd.check_respawn_guard(conn, tid) is None
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    monkeypatch.setattr("hermes_cli.kanban_db_workspace.resolve_workspace",
                        lambda *a, **kw: str(Path.cwd()))
    contexts = []

    def spawn(task, workspace, **kwargs):
        assert task.id == tid and task.assignee == "dev"
        assert task.current_run_id != run.current_run_id
        contexts.append(kb.build_worker_context(conn, task.id))

    result = kbd.dispatch_once(conn, spawn_fn=spawn)
    assert [s[0] for s in result.spawned] == [tid]
    assert PR in contexts[0] and "Waiting for gate parent" in contexts[0]
    assert len(kb.list_tasks(conn)) == 2


@pytest.mark.parametrize("kind", ["crash", None])
def test_promotion_without_dependency_park_cannot_resume(board, kind):
    conn, _ = board
    tid, parent, _ = _park(board, kind=kind)
    if kind is None:
        assert kb.unblock_task(conn, tid)
    _promote(board, parent)
    assert kb.get_task(conn, tid).status == "ready"
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("mutation", [
    "unfinished_parent", "different_profile", "empty_profile", "no_parent", "no_promotion",
    "unbound_wait", "wrong_run_wait", "missing_kind", "bad_payload", "blocked_kind",
    "block_before_end", "started_after_end", "future_promotion", "new_pr",
])
def test_dependency_proof_fails_closed(board, mutation):
    conn, clock = board
    tid, parent, _ = _park(board)
    if mutation == "unfinished_parent":
        other = kb.create_task(conn, title="Still open")
        kb.link_tasks(conn, other, tid)
        _promote(board, parent)
        assert kb.get_task(conn, tid).status == "todo"
        # A stale ready state and promotion cannot bypass a live second parent.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "promoted")
    else:
        _promote(board, parent)
    if mutation == "new_pr":
        kb.add_comment(conn, tid, author="dev", body=PR)
    if mutation == "wrong_run_wait":
        other = kb.create_task(conn, title="Other run", assignee="dev")
        other_run = kb.claim_task(conn, other)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET run_id=? WHERE task_id=? AND kind='dependency_wait'",
                         (other_run.current_run_id, tid))
    with kb.write_txn(conn):
        if mutation in {"different_profile", "empty_profile"}:
            conn.execute("UPDATE tasks SET assignee=? WHERE id=?",
                         ("other" if mutation == "different_profile" else "", tid))
        if mutation == "no_parent":
            conn.execute("DELETE FROM task_links WHERE child_id=?", (tid,))
        if mutation == "no_promotion":
            conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='promoted'", (tid,))
        if mutation == "unbound_wait":
            conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='dependency_wait'", (tid,))
        if mutation in {"missing_kind", "bad_payload"}:
            payload = json.dumps({"reason": "parent_not_done"}) if mutation == "missing_kind" else "{bad"
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='dependency_wait'", (payload, tid))
        if mutation == "blocked_kind":
            conn.execute("UPDATE task_events SET kind='blocked' WHERE task_id=? AND kind='dependency_wait'", (tid,))
        if mutation == "block_before_end":
            conn.execute("UPDATE task_runs SET ended_at=ended_at+1 WHERE task_id=?", (tid,))
        if mutation == "started_after_end":
            conn.execute("UPDATE task_runs SET started_at=ended_at+1 WHERE task_id=?", (tid,))
        if mutation == "future_promotion":
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='promoted'", (clock[0]+1, tid))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("kind", ["status", "reclaimed", "unblocked", "assigned", "blocked", "claimed",
                                  "promoted", "unknown"])
@pytest.mark.parametrize("after_promotion", [False, True])
def test_lifecycle_changes_invalidate_dependency_release(board, kind, after_promotion):
    conn, _ = board
    tid, parent, _ = _park(board)
    if after_promotion:
        _promote(board, parent)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, {"status": "ready"})
    if not after_promotion:
        _promote(board, parent)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("target", ["dependency_wait", "pr", "ended_at"])
def test_promotion_must_be_strictly_after_block_run_and_pr(board, target):
    conn, _ = board
    tid, parent, _ = _park(board)
    _promote(board, parent)
    with kb.write_txn(conn):
        stamp = conn.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='promoted'", (tid,)).fetchone()[0]
        if target == "pr":
            conn.execute("UPDATE task_comments SET created_at=? WHERE task_id=?", (stamp, tid))
        elif target == "ended_at":
            conn.execute("UPDATE task_runs SET ended_at=? WHERE task_id=?", (stamp, tid))
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='dependency_wait'", (stamp, tid))
        else:
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?", (stamp, tid, target))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("table,column,where", [
    ("task_runs", "started_at", ""),
    ("task_runs", "ended_at", ""),
    ("task_events", "created_at", " AND kind='dependency_wait'"),
    ("task_events", "created_at", " AND kind='promoted'"),
    ("task_comments", "created_at", " AND id=(SELECT MIN(id) FROM task_comments)"),
])
@pytest.mark.parametrize("stamp", ["not-a-stamp", 1793284600.5, "2026-10-29T12:00:00"])
def test_malformed_proof_stamps_never_authorize(board, table, column, where, stamp):
    conn, _ = board
    tid, parent, _ = _park(board)
    _promote(board, parent)
    # Keep the later PR comment recent: even an older malformed PR stamp
    # must invalidate the proof, rather than merely age out of the guard.
    with kb.write_txn(conn):
        conn.execute(f"UPDATE {table} SET {column}=? WHERE task_id=?{where}", (stamp, tid))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_new_claim_consumes_dependency_proof(board):
    conn, clock = board
    tid, parent, _ = _park(board)
    _promote(board, parent)
    assert kbd.check_respawn_guard(conn, tid) is None
    assert kb.claim_task(conn, tid) is not None
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"
    clock[0] += 1
    kbd._record_task_failure(conn, tid, error="worker exited 1", outcome="crashed",
                             release_claim=True, end_run=True)
    kb.recompute_ready(conn)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("after_promotion", [False, True])
@pytest.mark.parametrize("tamper", [None, "run", "claim", "pid", "code", "payload", "time", "binding"])
def test_worker_result_must_observe_exact_dependency_run(board, after_promotion, tamper):
    from gateway.session_kanban import _record_worker_result

    conn, clock = board
    tid, parent, run = _park(board, bound=True)
    if after_promotion:
        _promote(board, parent)
    clock[0] += 1
    _record_worker_result({"db": str(kb.kanban_db_path()), "task_id": tid,
                           "run_id": run.current_run_id, "claim_lock": run.claim_lock}, 0, "Waiting for gate")
    if not after_promotion:
        _promote(board, parent)
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
    assert kbd.check_respawn_guard(conn, tid) == (None if tamper is None else "active_pr")
