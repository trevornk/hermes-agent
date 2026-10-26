"""A managed worker's owner link failing is typed transport, never the card's own failure.

Incident shape (2026-10-24, quant-general): the profile owner stalled for >10 s twice; every
managed interpreter's persistence call ended ``worker_receipt_timeout``, the session turn lease
could not be renewed and the turn was hard-interrupted. The log line said only
``worker_receipt_timeout`` (no method, no budget, no owner) and the dispatcher booked an ordinary
exit, charging the card's breaker for a host condition. These tests pin the typed verdict:
``WorkerReceiptTimeout`` names what it waited for, classifies as ``owner_transport`` apart from
an owner's error reply, and only that evidence turns a kanban worker's exit into
``KANBAN_OWNER_UNAVAILABLE_EXIT_CODE``. A provider-terminal verdict and a completed turn win.
"""
from __future__ import annotations

import queue
import types

import pytest

from agent.runtime_session_store import RECEIPT_BUDGET_S, WorkerPersistenceError, WorkerReceiptTimeout, _OwnerLink
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE, KANBAN_TERMINAL_PROVIDER_EXIT_CODE
from hermes_cli.kanban_owner_recovery import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE
from hermes_cli.turn_exit import owner_transport_failed, turn_exit_code
from hermes_state_errors import PERSISTENCE_ERROR_CAUSES, classify_persistence_error


def _idle_link() -> _OwnerLink:
    """A link with no reader thread: its reply queue simply never fills."""
    link = _OwnerLink.__new__(_OwnerLink)
    link.replies = queue.SimpleQueue()
    link.awaiting = None
    return link


def test_receipt_timeout_names_the_call_its_budget_and_the_owner():
    with pytest.raises(WorkerReceiptTimeout) as caught:
        _idle_link().receive(0.01, method="session.append", endpoint="ws://127.0.0.1:4242")
    exc = caught.value
    assert isinstance(exc, TimeoutError)                      # the type callers already handle
    assert str(exc).startswith("worker_receipt_timeout")       # the bounded reason code, intact
    assert exc.reason == "worker_receipt_timeout"
    assert (exc.method, exc.timeout, exc.endpoint) == ("session.append", 0.01, "ws://127.0.0.1:4242")
    assert "session.append" in str(exc) and "ws://127.0.0.1:4242" in str(exc)
    assert RECEIPT_BUDGET_S == 20.0                            # the budget itself is not inflated here


@pytest.mark.parametrize("error, cause", [
    (WorkerReceiptTimeout("session.append", 20.0), "owner_transport"),   # no receipt at all
    (WorkerPersistenceError("owner_unavailable"), "owner_transport"),    # no owner to dial
    (WorkerPersistenceError("persistence_failed"), "unknown"),           # the owner ANSWERED: application
    (WorkerPersistenceError("permission_denied"), "unknown"),
    ("database is locked", "locked"),                                    # storage, as before
])
def test_transport_is_classified_apart_from_an_owners_reply(error, cause):
    assert classify_persistence_error(error) == cause
    assert cause in PERSISTENCE_ERROR_CAUSES


def test_new_cause_falls_back_to_the_default_explanation():
    from agent.turn_explainers import _persistence_explanation_key
    assert _persistence_explanation_key("owner_transport") == "explainer.persistence.default"


@pytest.mark.parametrize("result, flags, code", [
    # Startup failure with the owner link down: infrastructure, not a bare 1.
    (None, {"owner_unavailable": True}, KANBAN_OWNER_UNAVAILABLE_EXIT_CODE),
    # A lease the owner never renewed interrupts the turn: still infrastructure.
    ({"interrupted": True}, {"owner_unavailable": True}, KANBAN_OWNER_UNAVAILABLE_EXIT_CODE),
    ({"interrupted": True}, {}, 130),
    # The persistence-failed turn result (finalizer stamps the classified cause).
    ({"failed": True, "failure_reason": "session_persistence_failed:owner_transport"},
     {"owner_unavailable": True}, KANBAN_OWNER_UNAVAILABLE_EXIT_CODE),
    ({"failed": True, "failure_reason": "session_persistence_failed:owner_transport"}, {}, 1),
    # Terminal provider verdicts and credential flags are never softened into transport.
    ({"failed": True, "failure_reason": "auth"}, {"owner_unavailable": True}, KANBAN_TERMINAL_PROVIDER_EXIT_CODE),
    (None, {"owner_unavailable": True, "credentials_terminal": True}, KANBAN_TERMINAL_PROVIDER_EXIT_CODE),
    (None, {"owner_unavailable": True, "credentials_rate_limited": True}, KANBAN_RATE_LIMIT_EXIT_CODE),
    # A completed turn is a completed turn.
    ({"final_response": "done"}, {"owner_unavailable": True}, 0),
])
def test_owner_unavailable_exit_policy(result, flags, code):
    assert turn_exit_code(result, kanban_worker=True, **flags) == code


def test_only_kanban_workers_use_the_reserved_code():
    assert turn_exit_code(None, kanban_worker=False, owner_unavailable=True) == 1
    assert turn_exit_code({"interrupted": True}, kanban_worker=False, owner_unavailable=True) == 130


def test_owner_transport_failed_reads_typed_evidence_only():
    agent = types.SimpleNamespace(_last_persistence_error_cause=None, _lease_refresh_failure_cause=None)
    assert owner_transport_failed(agent) is False
    agent._lease_refresh_failure_cause = "owner_transport"
    assert owner_transport_failed(agent) is True
    agent = types.SimpleNamespace(_last_persistence_error_cause="locked", _lease_refresh_failure_cause=None)
    assert owner_transport_failed(agent) is False                     # a busy store is not the owner link
    wrapped = RuntimeError("agent construction failed")
    wrapped.__cause__ = WorkerReceiptTimeout("worker.register", 20.0)
    assert owner_transport_failed(None, wrapped) is True
    assert owner_transport_failed(None, WorkerPersistenceError("permission_denied")) is False
    assert owner_transport_failed(None, RuntimeError("worker_receipt_timeout mentioned in prose")) is True


def test_lease_refresh_failure_records_its_cause_on_the_agent():
    """The renewal the owner never answered is the evidence the exit policy reads."""
    from agent import turn_facade_lease

    class _Db:
        def refresh_session_turn_lease(self, *args, **kwargs):
            raise WorkerReceiptTimeout("session.refresh_turn_lease", 20.0, "ws://127.0.0.1:1")

    agent = types.SimpleNamespace(_session_turn_lease_refresh_interval=60.0, session_id="s1")
    lease = turn_facade_lease.DurableTurnLease(agent, _Db(), "s1", "holder")
    interrupts: list[str] = []
    lease._interrupt_turn = interrupts.append
    assert lease.refresh_tick() is False
    assert agent._lease_refresh_failure_cause == "owner_transport"
    assert interrupts and "could not be refreshed" in interrupts[0]
    assert owner_transport_failed(agent) is True
