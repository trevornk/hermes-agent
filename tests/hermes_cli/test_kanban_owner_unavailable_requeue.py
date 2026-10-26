"""An attempt that never reached its profile owner is requeued unspent, fenced to its own run.

Incident (2026-10-24 15:11 and 15:19, board quant-general): the shared owner stalled for >10 s
twice; four dispatcher submitters could not open a socket to it, exited ``1`` with
"Kanban owner refused: Gateway connection failed; no local fallback", were booked ``crashed``,
charged ``consecutive_failures`` and auto-blocked after the second stall — four cards parked for
one host condition. Nothing about any card had run.

These tests pin the typed path: only the connect phase is transport (an owner's refusal and an
answer lost after submission stay the card's own exit ``1``); the submitter books the verdict as
a ``worker_result`` receipt fenced to its exact run/claim/pid; the sweep requeues from that
receipt alone (a log-tail trailer is never enough), without the counter, the same-error breaker
or the protocol-violation streak; the respawn guard holds the card under a bounded exponential
backoff across ticks; and terminal provider exits, review-lane routing and cards that are not
running are untouched.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as client
from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, GatewayUnavailableError
from hermes_cli.kanban_owner_recovery import (
    KANBAN_OWNER_UNAVAILABLE_EXIT_CODE,
    OWNER_BACKOFF_BASE_SECONDS,
    OWNER_BACKOFF_MAX_SECONDS,
    managed_run_exit_code,
    owner_backoff_seconds,
)
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

CONNECT_FAILED = "Gateway connection failed; no local fallback"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)   # every recorded worker is dead
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return home


def _claimed(conn, tid: str, *, lane: str = "ready") -> dict:
    """Claim ``tid`` for a submitter that is THIS process (so its receipt pid matches)."""
    host = kb._claimer_id().split(":", 1)[0]
    claim = kb.claim_review_task if lane == "review" else kb.claim_task
    task = claim(conn, tid, claimer=f"{host}:w{os.getpid()}")
    assert task is not None
    conn.execute("UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
                 (os.getpid(), int(time.time()) - 120, tid))
    conn.commit()
    return {"board": "default", "task_id": tid, "run_id": task.current_run_id, "claim_lock": task.claim_lock}


def _receipt(params: dict, exc=None) -> bool:
    return client.record_owner_unavailable(
        str(kb.kanban_db_path()), params, exc or client.OwnerTransportUnavailable(GatewayClientError(CONNECT_FAILED)))


def _runs(conn, tid: str):
    return conn.execute("SELECT outcome, metadata, ended_at FROM task_runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()


def _events(conn, tid: str) -> list[str]:
    return [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def _age_runs(conn, tid: str, seconds: int) -> None:
    conn.execute("UPDATE task_runs SET ended_at = ended_at - ? WHERE task_id=? AND ended_at IS NOT NULL", (seconds, tid))
    conn.commit()


# --- the submitter's verdict is typed -------------------------------------------------------

@pytest.mark.parametrize("exc, submitted, transport", [
    (GatewayClientError(CONNECT_FAILED), False, True),             # the incident's error
    (GatewayUnavailableError("Gateway exited: runtime_exited", "exited"), False, True),
    (ConnectionRefusedError(61, "refused"), False, True),
    (TimeoutError(), False, True),
    (GatewayRPCError("permission_denied"), False, False),           # the owner ANSWERED
    (GatewayRPCError("stale_kanban_claim"), False, False),
    (GatewayClientError("Kanban execution unknown; retained by owner"), True, False),  # after submit: uncertain
    (ConnectionResetError(), True, False),
    (ValueError("not a transport error"), False, False),
])
def test_only_a_connect_phase_transport_failure_is_transient(exc, submitted, transport):
    assert client.is_owner_transport_failure(exc, submitted=submitted) is transport


class _Client:
    def __init__(self, error):
        import asyncio
        self.events, self.error = asyncio.Queue(), error

    async def rpc(self, method, **params):
        raise self.error


def _connect_that(monkeypatch, *, raises=None, yields=None):
    from hermes_cli import gateway_client

    @contextlib.asynccontextmanager
    async def connect_gateway():
        if raises is not None:
            raise raises
        yield yields
    monkeypatch.setattr(gateway_client, "connect_gateway", connect_gateway)


PARAMS = {"board": "default", "task_id": "t1", "run_id": 1, "claim_lock": "host:1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    GatewayClientError(CONNECT_FAILED),
    GatewayUnavailableError("Gateway exited: runtime_exited", "exited"),
    GatewayUnavailableError("Gateway starting: not_ready", "starting"),   # transient state, wait exhausted
    OSError(61, "refused"),
])
async def test_run_raises_the_typed_failure_only_before_a_socket_exists(monkeypatch, tmp_path, error):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    monkeypatch.setattr(client, "OWNER_WAIT_S", 0.0)
    _connect_that(monkeypatch, raises=error)
    with pytest.raises(client.OwnerTransportUnavailable) as caught:
        await client.run(PARAMS, str(tmp_path / "kanban.db"))
    assert caught.value.cause is error


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    GatewayRPCError("stale_kanban_claim"),
    GatewayClientError("Kanban execution unknown; retained by owner"),
    ConnectionResetError(),
])
async def test_run_keeps_a_failure_after_the_socket_is_up_untyped(monkeypatch, tmp_path, error):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    _connect_that(monkeypatch, yields=_Client(error))
    with pytest.raises(type(error)) as caught:
        await client.run(PARAMS, str(tmp_path / "kanban.db"))
    assert caught.value is error


# --- the verdict is booked on the board, fenced to the run --------------------------------

def _submitter_env(monkeypatch, params: dict) -> None:
    monkeypatch.setenv("HERMES_KANBAN_BOARD", params["board"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", params["task_id"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(params["run_id"]))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", params["claim_lock"])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))


def test_submit_books_a_fenced_receipt_and_exits_the_reserved_code(kanban_home, monkeypatch, capsys):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        params = _claimed(conn, tid)
    _submitter_env(monkeypatch, params)

    async def no_socket(_params, _db):
        raise client.OwnerTransportUnavailable(GatewayClientError(CONNECT_FAILED))
    monkeypatch.setattr(client, "run", no_socket)

    assert client.main() == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
    err = capsys.readouterr().err
    assert f"Kanban owner unavailable (GatewayClientError): {CONNECT_FAILED}" in err
    assert f"{KANBAN_WORKER_EXIT_TRAILER}{KANBAN_OWNER_UNAVAILABLE_EXIT_CODE}" in err
    with kbc.connect() as conn:
        receipt = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result'", (tid,)).fetchone()["payload"])
        assert receipt["exit_code"] == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
        assert (receipt["pid"], receipt["claim_lock"], receipt["infrastructure"]) == (os.getpid(), params["claim_lock"], True)
        assert (receipt["error_type"], receipt["error"]) == ("GatewayClientError", CONNECT_FAILED)
        assert managed_run_exit_code(conn, tid, os.getpid(), params["claim_lock"]) == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE


def test_an_owners_refusal_is_not_booked_as_transport(kanban_home, monkeypatch, capsys):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        params = _claimed(conn, tid)
    _submitter_env(monkeypatch, params)

    async def refused(_params, _db):
        raise GatewayRPCError("permission_denied")
    monkeypatch.setattr(client, "run", refused)

    assert client._submit() == 1
    assert "Kanban owner refused (GatewayRPCError): permission_denied" in capsys.readouterr().err
    with kbc.connect() as conn:
        assert "worker_result" not in _events(conn, tid)


def test_the_receipt_is_written_only_for_the_live_run_under_the_same_claim(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        params = _claimed(conn, tid)
    assert _receipt(dict(params, claim_lock="other-host:999")) is False     # another claim's attempt
    assert _receipt(dict(params, run_id=params["run_id"] + 1)) is False     # not this run
    with kbc.connect() as conn:
        assert "worker_result" not in _events(conn, tid)
    assert _receipt(params) is True
    with kbc.connect() as conn:
        kb._end_run(conn, tid, outcome="completed")                          # the run is over now
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.commit()
    assert _receipt(params) is False                                         # a closed run takes nothing


# --- the sweep requeues unspent, the guard spaces the retries ------------------------------

def test_four_submitters_failing_to_dial_one_owner_spend_nothing(kanban_home, all_assignees_spawnable):
    """The incident, replayed through ``dispatch_once``: four cards, one stalled owner."""
    spawned: list[str] = []
    with kbc.connect() as conn:
        tids = [kb.create_task(conn, title=f"t{i}", assignee="a") for i in range(4)]
        conn.execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?", (tids[0],))  # an earlier real failure
        conn.commit()
        for tid in tids:
            assert _receipt(_claimed(conn, tid)) is True

        res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: spawned.append(task.id) or os.getpid(),
                                failure_limit=2)

        assert sorted(res.owner_unavailable) == sorted(tids)
        assert (res.crashed, res.auto_blocked, spawned) == ([], [], [])
        assert sorted(res.respawn_guarded) == sorted((tid, "owner_backoff") for tid in tids)
        assert "owner_unavailable=4" in kbd.describe_suppression([res]) and "owner_backoff=4" in kbd.describe_suppression([res])
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert (task.status, task.claim_lock, task.worker_pid) == ("ready", None, None)
            assert task.consecutive_failures == (1 if tid == tids[0] else 0)          # preserved, not spent
            assert "could not reach the profile owner" in task.last_failure_error
            [(outcome, metadata, _)] = _runs(conn, tid)
            assert outcome == "owner_unavailable"
            assert kb._json_dict(metadata)["infrastructure"] is True
            assert kb._json_dict(metadata)["exit_code"] == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
            kinds = _events(conn, tid)
            assert "owner_unavailable" in kinds and "gave_up" not in kinds and "crashed" not in kinds
            assert kbd._protocol_violation_streak(conn, tid) == 0


def test_backoff_doubles_per_consecutive_owner_unavailable_run_across_ticks(kanban_home, all_assignees_spawnable):
    spawned: list[str] = []

    def spawn(task, ws, board=None):
        spawned.append(task.id)
        return os.getpid()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        assert _receipt(_claimed(conn, tid)) is True
        kbd.dispatch_once(conn, spawn_fn=spawn)                     # tick 1: books the run, holds the card
        assert spawned == [] and kbd.check_respawn_guard(conn, tid) == "owner_backoff"
        _age_runs(conn, tid, owner_backoff_seconds(1) - 5)
        assert kbd.dispatch_once(conn, spawn_fn=spawn).respawn_guarded == [(tid, "owner_backoff")]
        _age_runs(conn, tid, 10)                                    # first hold elapsed: respawned
        assert kbd.dispatch_once(conn, spawn_fn=spawn).spawned[0][0] == tid
        assert spawned == [tid]

        # The respawned attempt fails to dial the owner too: the hold doubles.
        task = kb.get_task(conn, tid)
        conn.execute("UPDATE tasks SET worker_started_at=NULL, started_at=? WHERE id=?", (int(time.time()) - 120, tid))
        conn.commit()
        assert _receipt({"board": "default", "task_id": tid, "run_id": task.current_run_id, "claim_lock": task.claim_lock})
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert res.owner_unavailable == [tid] and kbd._owner_unavailable_streak(conn, tid) == 2
        _age_runs(conn, tid, owner_backoff_seconds(1) + 5)      # past the FIRST hold, inside the second
        assert kbd.check_respawn_guard(conn, tid) == "owner_backoff"
        _age_runs(conn, tid, owner_backoff_seconds(2) - owner_backoff_seconds(1))
        assert kbd.check_respawn_guard(conn, tid) is None
        assert kb.get_task(conn, tid).consecutive_failures == 0


def test_backoff_is_exponential_and_bounded():
    base, cap = OWNER_BACKOFF_BASE_SECONDS, OWNER_BACKOFF_MAX_SECONDS
    assert [owner_backoff_seconds(n) for n in (0, 1, 2, 3, 4)] == [base, base, base * 2, base * 4, base * 8]
    assert owner_backoff_seconds(40) == cap and owner_backoff_seconds(10_000) == cap


def test_a_review_lane_attempt_returns_to_review(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        assert _receipt(_claimed(conn, tid, lane="review")) is True
        assert kbd.detect_crashed_workers(conn) == []
        assert kb.get_task(conn, tid).status == "review"
        assert kb.get_task(conn, tid).consecutive_failures == 0


# --- fencing and the sentinels that must keep their meaning ---------------------------------

def test_a_trailer_alone_never_earns_the_unspent_requeue(kanban_home):
    """A per-tick dispatcher reads the log tail; ``rc=69`` there may belong to an earlier attempt."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        _claimed(conn, tid)
        log = kb.worker_log_path(tid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"older attempt\n{KANBAN_WORKER_EXIT_TRAILER}{KANBAN_OWNER_UNAVAILABLE_EXIT_CODE}\n")

        assert kbd.detect_crashed_workers(conn) == [tid]
        task = kb.get_task(conn, tid)
        assert (task.status, task.consecutive_failures) == ("ready", 1)
        [(outcome, _, _)] = _runs(conn, tid)
        assert outcome == "crashed"


def test_a_previous_runs_receipt_does_not_speak_for_the_current_run(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        stale = _claimed(conn, tid)
        assert _receipt(stale) is True
        assert kbd.detect_crashed_workers(conn) == []                     # run 1: unspent
        current = _claimed(conn, tid)                                     # run 2 under a NEW claim
        assert current["run_id"] != stale["run_id"]
        assert kbd.detect_crashed_workers(conn) == [tid]                  # no receipt for run 2: a crash
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        assert [r["outcome"] for r in _runs(conn, tid)] == ["owner_unavailable", "crashed"]


def test_cards_that_are_not_running_are_untouched(kanban_home):
    with kbc.connect() as conn:
        done = kb.create_task(conn, title="done", assignee="a")
        params = _claimed(conn, done)
        kb._append_event(conn, done, "worker_result", {"pid": os.getpid(), "claim_lock": params["claim_lock"],
                         "exit_code": KANBAN_OWNER_UNAVAILABLE_EXIT_CODE}, run_id=params["run_id"])
        kb.complete_task(conn, done, summary="finished")
        review = kb.create_task(conn, title="review", assignee="a")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review,))
        conn.commit()
        before = {tid: dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) for tid in (done, review)}

        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)

        assert res.owner_unavailable == [] and res.crashed == []
        for tid in (done, review):
            assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before[tid]


def test_a_terminal_provider_receipt_still_parks_the_card_sticky(kanban_home):
    """The reserved transport code leaves the provider-terminal verdict exactly as it was."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="a")
        params = _claimed(conn, tid)
        kb._append_event(conn, tid, "worker_result", {"pid": os.getpid(), "claim_lock": params["claim_lock"],
                         "exit_code": kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE, "last_output": "401 invalid key"},
                         run_id=params["run_id"])
        conn.commit()
        assert kbd.detect_crashed_workers(conn) == [tid]
        assert kbd.detect_crashed_workers._last_auto_blocked == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        gave_up = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()["payload"])
        assert gave_up["terminal_provider"] is True and gave_up["sticky"] is True
        assert kb.recompute_ready(conn, failure_limit=10) == 0
