"""PR holds follow the latest run outcome, independent of release-event proofs."""

import ast
import re

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_outcomes as kb_outcomes

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
    assert kbd.check_respawn_guard(conn, tid) is None
    clock[0] += 1
    kb.add_comment(conn, tid, author="desktop", body="Approved; finish the existing PR")
    assert kbd.check_respawn_guard(conn, tid) is None
    _unblock(board, tid)
    # Historical observations do not change the latest run outcome.
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
def test_every_block_kind_records_a_worker_handoff(board, kind):
    conn, _ = board
    tid, _ = _blocked(board, kind)
    if kb.get_task(conn, tid).status == "blocked":
        _unblock(board, tid)
    else:
        kb.recompute_ready(conn)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("mutation", [
    "no_unblock", "same_assign", "arbitrary_ready", "unbound_block", "wrong_run_block",
    "missing_kind", "bad_payload", "new_pr", "pr_tie", "block_tie", "run_tie",
    "bad_pr_time", "bad_block_time", "bad_unblock_time", "bad_run_time",
    "status_after", "status_between", "reclaim_after", "reblock_after", "newer_crash",
    "newer_live", "different_profile", "backdated_transition", "bad_transition_time",
])
def test_event_chain_details_do_not_override_the_latest_run(board, mutation):
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
    expected = "active_pr" if mutation in {"newer_crash", "newer_live"} else None
    assert kbd.check_respawn_guard(conn, tid) == expected



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

def test_later_untyped_block_is_also_a_worker_handoff(board):
    conn, clock = board
    tid, _ = _blocked(board)
    _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) is None
    assert kb.claim_task(conn, tid) is not None
    clock[0] += 5
    assert kb.block_task(conn, tid, reason="Stop again")
    _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("stage", ["workspace", "spawn"])
def test_failed_continuation_launch_is_reported_and_holds(board, monkeypatch, stage):
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


def test_classifying_a_crash_in_place_does_not_change_its_outcome(board):
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
@pytest.mark.parametrize("stamp_kind", ["unreadable", "fractional", "iso"])
def test_event_and_run_timestamps_do_not_override_worker_handoff(board, table, column, where, stamp_kind):
    from datetime import UTC, datetime

    now = int(time.time())
    stamp = {"unreadable": "not-a-stamp", "fractional": now - 10.5,
             "iso": datetime.fromtimestamp(now - 10, UTC).isoformat()}[stamp_kind]
    conn, _ = board
    tid, _ = _blocked(board)
    _unblock(board, tid)
    with kb.write_txn(conn):
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE task_id = ?{where}", (stamp, tid))
    assert kbd.check_respawn_guard(conn, tid) is None


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
        assert kbd.check_respawn_guard(conn, tid) is None
        _unblock(board, tid)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("tamper", ["run", "claim", "pid", "code", "payload", "time", "binding"])
def test_worker_result_binding_details_do_not_override_worker_handoff(board, tamper):
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
    assert kbd.check_respawn_guard(conn, tid) is None


PR_COMMENT = "Handoff (iteration budget exhausted): draft PR https://github.com/example/repo/pull/8"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _armed_running(conn, assignee: str = "dev") -> tuple[str, int]:
    """A claimed card whose worker posted its PR URL 60s ago."""
    tid = kb.create_task(conn, title="impl", assignee=assignee)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    kb.add_comment(conn, tid, author=assignee, body=PR_COMMENT)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_comments SET created_at = created_at - 60 WHERE task_id = ?", (tid,),
        )
    return tid, claimed.current_run_id


def _budget_exhausted(conn, tid: str, run_id: int) -> None:
    """Exactly what agent/turn_finalizer._record_kanban_budget_exhausted books."""
    kbd._record_task_failure(
        conn, tid,
        error="Iteration budget exhausted (60/60) — task could not complete within the allowed iterations",
        outcome="timed_out", release_claim=True, end_run=True,
        event_payload_extra={"budget_used": 60, "budget_max": 60},
        expected_run_id=run_id,
    )


def test_budget_retry_by_same_profile_spawns(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        _budget_exhausted(conn, tid, run_id)
        assert kb.get_task(conn, tid).status == "ready"
        assert kbd.check_respawn_guard(conn, tid) is None


def test_newer_pr_comment_after_budget_handoff_does_not_rearm(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET ended_at = ended_at - 30 WHERE id = ?", (run_id,),
            )
        kb.add_comment(conn, tid, author="dev", body="Opened https://github.com/example/repo/pull/9")
        assert kbd.check_respawn_guard(conn, tid) is None


def test_budget_handoff_at_same_second_as_comment_lifts(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = "
                "(SELECT ended_at FROM task_runs WHERE id = ?) WHERE task_id = ?",
                (run_id, tid),
            )
        assert kbd.check_respawn_guard(conn, tid) is None


def test_wall_clock_max_runtime_kill_stays_guarded(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, _ = _armed_running(conn)
        # The payload enforce_max_runtime writes: limit_seconds, no budget fields.
        payload = {"pid": 4242, "elapsed_seconds": 3700, "limit_seconds": 3600,
                   "sigkill": False, "retry_status": "ready"}
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL WHERE id = ?", (tid,),
            )
            run_id = kb._end_run(conn, tid, outcome="timed_out", status="timed_out",
                                 error="elapsed 3700s > limit 3600s", metadata=payload)
            kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
        kbd._record_task_failure(
            conn, tid, error="elapsed 3700s > limit 3600s", outcome="timed_out",
            release_claim=False, end_run=False,
            event_payload_extra={"pid": 4242, "sigkill": False, "retry_status": "ready"},
        )
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_crash_stays_guarded(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        kbd._record_task_failure(
            conn, tid, error="worker exited 1", outcome="crashed",
            release_claim=True, end_run=True, expected_run_id=run_id,
        )
        assert kb.get_task(conn, tid).status == "ready"
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_crash_of_the_continuation_rearms_the_guard(kanban_home: Path) -> None:
    """Only the LATEST run can lift it: the budget retry itself crashing is a crash."""
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        retry = kb.claim_task(conn, tid)
        assert retry is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET ended_at = ended_at - 10 WHERE id = ?", (run_id,),
            )
        kbd._record_task_failure(
            conn, tid, error="worker exited 1", outcome="crashed",
            release_claim=True, end_run=True, expected_run_id=retry.current_run_id,
        )
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_budget_handoff_is_independent_of_current_assignee(kanban_home: Path) -> None:
    """A recorded handoff remains a handoff when the card's assignee changes."""
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee = 'other' WHERE id = ?", (tid,))
        assert kbd.check_respawn_guard(conn, tid) is None


def test_dispatch_spawns_the_budget_retry(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert tid in [s[0] for s in res.spawned]
        assert tid not in dict(res.respawn_guarded)


def test_operator_same_profile_lift_is_request_review_then_reopen(board) -> None:
    """The supported operator lever for any other guarded ready card that the
    SAME profile must keep working: ``request-review`` then ``reopen-review``
    records ``review_reopened`` after the PR comment, which lifts the guard
    and restores the implementer. A same-profile ``assign`` does not."""
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        kbd._record_task_failure(
            conn, tid, error="worker exited 1", outcome="crashed",
            release_claim=True, end_run=True, expected_run_id=run_id,
        )
        assert kb.assign_task(conn, tid, "dev") is True
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"
        assert kb.request_review(conn, tid, summary="operator: resume own PR") is True
        assert kb.reopen_review_task(conn, tid) is True
        task = kb.get_task(conn, tid)
        assert task is not None and (task.status, task.assignee) == ("ready", "dev")
        assert kbd.check_respawn_guard(conn, tid) is None


def _ended_card(board, outcome, metadata=None):
    conn, clock = board
    tid = kb.create_task(conn, title="Prior worker opened a PR", assignee="dev")
    kb.claim_task(conn, tid)
    clock[0] += 1
    kb.add_comment(conn, tid, author="dev", body=PR)
    clock[0] += 5
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome=outcome, metadata=metadata)
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    return tid


@pytest.mark.parametrize("outcome", [
    "crashed", "reclaimed", "timed_out", "gave_up", "spawn_failed", "stale", "unknown_future_outcome",
])
def test_supervisor_or_unknown_latest_run_holds(board, outcome):
    conn, _ = board
    tid = _ended_card(board, outcome, {"limit_seconds": 3600} if outcome == "timed_out" else None)
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("outcome", [
    "completed", "blocked", "scheduled", "review_requested", "changes_requested", "dependency_wait",
    "future_worker_handoff",
])
def test_worker_outcomes_lift_without_release_event_proofs(board, monkeypatch, outcome):
    conn, clock = board
    monkeypatch.setitem(kb_outcomes.RUN_OUTCOME_ENDED_BY, "future_worker_handoff", "worker")
    tid = _ended_card(board, outcome)
    # Step 3 is unchanged: a recent completion still holds independently.
    if outcome == "completed":
        clock[0] += 3601
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("metadata,expected", [
    ({"budget_max": 60}, True), ({"budget_max": 0}, True),
    ({"budget_max": None, "limit_seconds": 3600}, False), ({"limit_seconds": 3600}, False),
    ({}, False), (None, False),
])
def test_only_non_null_iteration_budget_marks_timeout_as_worker_handoff(metadata, expected):
    assert kb_outcomes.run_ended_by_worker("timed_out", metadata) is expected
    assert kb_outcomes.run_ended_by_worker("unknown", metadata) is False
    assert kb_outcomes.run_ended_by_worker("crashed", metadata) is False


def test_zero_run_card_pr_link_is_context(board):
    conn, _ = board
    tid = kb.create_task(conn, title="Work on this PR", assignee="dev")
    kb.add_comment(conn, tid, author="operator", body=PR)
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("outcome,expected", [("blocked", None), ("crashed", "active_pr")])
@pytest.mark.parametrize("stamp", ["new", "unreadable"])
def test_external_pr_comment_after_park_or_crash(board, outcome, expected, stamp):
    """Unreadable comments use now, but cannot invalidate a recorded worker handoff."""
    conn, clock = board
    tid = _ended_card(board, outcome)
    clock[0] += 10
    kb.add_comment(conn, tid, author="someone_else", body=f"Please finish {PR}")
    if stamp == "unreadable":
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at='unreadable' WHERE task_id=?", (tid,))
    assert kbd.check_respawn_guard(conn, tid) == expected
    assert "someone_else" in kb.build_worker_context(conn, tid)


def test_expired_pr_comment_does_not_hold_crashed_card(board):
    conn, clock = board
    tid = _ended_card(board, "crashed")
    clock[0] += 86401
    assert kbd.check_respawn_guard(conn, tid) is None


@pytest.mark.parametrize("successor", ["open", "crashed"])
def test_latest_run_id_supersedes_clean_handoff_even_with_backdated_end(board, successor):
    conn, clock = board
    tid, _ = _blocked(board)
    _unblock(board, tid)
    run = kb.claim_task(conn, tid)
    assert run is not None
    if successor == "crashed":
        kbd._record_task_failure(conn, tid, error="worker died", outcome="crashed", release_claim=True, end_run=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET ended_at=? WHERE id=?", (clock[0] - 200, run.current_run_id))
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("assignee,expected", [("dev", "active_pr"), ("closer", None)])
def test_only_different_profile_handoff_lifts_after_crash(board, assignee, expected):
    conn, clock = board
    tid = _ended_card(board, "crashed")
    clock[0] += 10
    assert kb.assign_task(conn, tid, assignee)
    assert kbd.check_respawn_guard(conn, tid) == expected


def test_scheduled_wake_after_workers_own_pr_comment(board):
    conn, clock = board
    tid = kb.create_task(conn, title="Wake to finish PR", assignee="dev")
    run = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR)
    clock[0] += 5
    assert kb.schedule_task(conn, tid, reason="Wait for CI", expected_run_id=run.current_run_id)
    assert kb.list_runs(conn, tid)[-1].outcome == "scheduled"
    clock[0] += 10
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"
    assert kb.list_events(conn, tid)[-1].kind == "unblocked"
    assert kbd.check_respawn_guard(conn, tid) is None


def test_dependency_wait_promoted_after_parent_finishes(board):
    conn, clock = board
    parent = kb.create_task(conn, title="Parent", assignee="dev")
    tid = kb.create_task(conn, title="Child PR", assignee="dev")
    run = kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR)
    kb.link_tasks(conn, parent_id=parent, child_id=tid, expected_child_run_id=run.current_run_id)
    assert kb.block_task(conn, tid, kind="dependency", reason="Wait for parent")
    # This version books dependency parks as blocked, with a dependency_wait event.
    assert kb.list_runs(conn, tid)[-1].outcome == "blocked"
    clock[0] += 5
    assert kb.complete_task(conn, parent, result="Done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "ready"
    assert any(e.kind == "dependency_wait" for e in kb.list_events(conn, tid))
    assert kbd.check_respawn_guard(conn, tid) is None


def test_old_completion_returned_to_ready(board):
    conn, clock = board
    tid = kb.create_task(conn, title="More work on PR", assignee="dev")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR)
    assert kb.complete_task(conn, tid, result="Done")
    clock[0] += 3601
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "status", {"status": "ready"})
    assert kbd.check_respawn_guard(conn, tid) is None


def test_review_reopened_after_worker_handoff(board):
    conn, clock = board
    tid = kb.create_task(conn, title="Revisit PR", assignee="dev")
    kb.claim_task(conn, tid)
    kb.add_comment(conn, tid, author="dev", body=PR)
    assert kb.request_review(conn, tid, summary="Review PR")
    assert kb.list_runs(conn, tid)[-1].outcome == "review_requested"
    clock[0] += 5
    assert kb.reopen_review_task(conn, tid)
    assert kbd.check_respawn_guard(conn, tid) is None


def test_all_written_run_outcomes_are_classified():
    """Contract between outcome writers and policy; explicitly requested AST audit."""
    root = Path(__file__).resolve().parents[2]
    paths = sorted(set(root.glob("hermes_cli/kanban*.py"))
                   | set(root.glob("plugins/kanban/**/*.py"))
                   | set(root.glob("gateway/**/*.py"))
                   | {root / "agent/turn_finalizer.py"})
    helpers = {"_end_run", "_end_or_synthesize_run", "_record_task_failure", "_synthesize_ended_run"}
    written = {}
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                if name in helpers:
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    value = keywords.get("outcome", keywords.get("status"))
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        written.setdefault(value.value, []).append(f"{path.relative_to(root)}:{node.lineno}")
            # The crash sweeper's dynamic outcome is selected by this property.
            if isinstance(node, ast.FunctionDef) and node.name == "run_outcome":
                for outcome, line in _returned_outcome_literals(node):
                    written.setdefault(outcome, []).append(f"{path.relative_to(root)}:{line}")
            # Migration/transfer/status cleanup also close runs directly in SQL.
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "UPDATE task_runs" in node.value:
                for outcome in re.findall(r"\boutcome\s*=\s*(?:COALESCE\(outcome,\s*)?'([^']+)'", node.value):
                    written.setdefault(outcome, []).append(f"{path.relative_to(root)}:{node.lineno}")
    assert written, "No outcome writers found"
    missing = {outcome: sites for outcome, sites in written.items() if outcome not in kb_outcomes.RUN_OUTCOME_ENDED_BY}
    assert not missing, f"Unclassified task_runs outcomes: {missing}"



def _returned_outcome_literals(node):
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        for value in ast.walk(child.value):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                yield value.value, value.lineno



@pytest.mark.parametrize("outcome", ["owner_unavailable", "rate_limited"])
def test_transport_failures_are_supervisor_ended(outcome):
    # Earlier cooldown gates retain their own behavior; neither is a worker handoff.
    assert kb_outcomes.run_ended_by_worker(outcome, {}) is False
