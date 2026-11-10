"""Who ended a task run: the one classification the ``active_pr`` respawn guard reads.

Every ``task_runs.outcome`` the kanban modules can write must be listed here; a test
scans the writers and fails on an unlisted value, so a new park type added by a Hermes
update surfaces at carry re-port time instead of stalling a card in production.
"""
from __future__ import annotations

from typing import Optional


# Who ended the attempt, independent of how the card is later released.
RUN_OUTCOME_ENDED_BY = {
    # A recorded worker handoff leaves context for the next worker.
    "completed": "worker",
    "blocked": "worker",
    "scheduled": "worker",
    "review_requested": "worker",
    "changes_requested": "worker",
    "dependency_wait": "worker",
    # Interrupted, dead, or unstarted workers may have opened a PR without a handoff.
    "crashed": "supervisor",
    "reclaimed": "supervisor",
    "timed_out": "supervisor",
    "gave_up": "supervisor",
    "spawn_failed": "supervisor",
    "stale": "supervisor",
    "owner_unavailable": "supervisor",
    "rate_limited": "supervisor",
}


def run_ended_by_worker(outcome: Optional[str], metadata: Optional[dict]) -> bool:
    """Whether an attempt recorded its own handoff; unknown outcomes fail closed."""
    if outcome == "timed_out" and (metadata or {}).get("budget_max") is not None:
        # Graceful iteration exhaustion is requeued to continue; a wall-clock kill is not.
        return True
    return RUN_OUTCOME_ENDED_BY.get(outcome) == "worker"
