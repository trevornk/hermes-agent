"""``active_pr`` must not strand the dispatcher's own iteration-budget retry.

A worker that runs out of iterations is booked ``timed_out`` with
``budget_used``/``budget_max`` and re-queued to ``ready`` so the same profile
can continue. When that worker had already posted its PR URL, the duplicate-PR
guard refused the very retry the dispatcher had just scheduled, for the whole
24h window. The continuation is the implementer finishing its own PR; a
wall-clock max_runtime kill and a crash stay guarded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

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


def test_newer_pr_comment_after_the_budget_retry_guards_again(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET ended_at = ended_at - 30 WHERE id = ?", (run_id,),
            )
        kb.add_comment(conn, tid, author="dev", body="Opened https://github.com/example/repo/pull/9")
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_same_second_tie_stays_guarded(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_comments SET created_at = "
                "(SELECT ended_at FROM task_runs WHERE id = ?) WHERE task_id = ?",
                (run_id, tid),
            )
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


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


def test_budget_run_by_a_different_profile_does_not_lift(kanban_home: Path) -> None:
    """The budget run must be by the profile now on the card. (A real reassign
    after the PR comment is a handoff and lifts the guard on its own; this is
    the raw run-profile check, exercised by rewriting the assignee directly.)"""
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee = 'other' WHERE id = ?", (tid,))
        assert kbd.check_respawn_guard(conn, tid) == "active_pr"


def test_dispatch_spawns_the_budget_retry(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kbc.connect() as conn:
        tid, run_id = _armed_running(conn)
        _budget_exhausted(conn, tid, run_id)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert tid in [s[0] for s in res.spawned]
        assert tid not in dict(res.respawn_guarded)


def test_operator_same_profile_lift_is_request_review_then_reopen(kanban_home: Path) -> None:
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
