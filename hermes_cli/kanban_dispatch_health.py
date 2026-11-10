"""Shared actionable wording for gateway and standalone dispatch starvation."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db_dispatch import DispatchResult
    from hermes_cli.kanban_diagnostics import Diagnostic


def dispatch_health_detail(results: Iterable[Optional[DispatchResult]]) -> str:
    from hermes_cli.kanban_db_dispatch import describe_suppression

    results = [res for res in results if res is not None]
    held = describe_suppression(results)
    parts = [f" Last tick held back: {held}."] if held else []
    if any(reason == "active_pr" for res in results for _, reason in res.respawn_guarded):
        parts.append(" active_pr is a duplicate-work hold: the last run ended without a recorded handoff (crash, reclaim or timeout); inspect run history, or reassign to hand the PR to another profile.")
    if any(res.spawn_failed for res in results):
        parts.append(" Spawn failures recorded; check worker logs and profile health (venv, PATH, credentials).")
    parts.append(" Inspect `hermes kanban list --status ready` and run history for unresolved dispatch starvation.")
    return "".join(parts)


def dispatch_hold_diagnostics(conn: sqlite3.Connection, task: Any, now: int) -> list[Diagnostic]:
    """Expose the current guard, rather than stale respawn_guarded observations."""
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    from hermes_cli.kanban_diagnostics import Diagnostic, DiagnosticAction, _task_field

    status = _task_field(task, "status")
    task_id = _task_field(task, "id")
    if (status not in {"ready", "review"} or _task_field(task, "claim_lock")
            or not _task_field(task, "assignee")):
        return []
    reason = check_respawn_guard(conn, task_id, lane=status)
    if reason is None:
        return []
    detail = ("Duplicate-work protection is holding this existing PR: the last run ended without a "
              "recorded handoff (crash, reclaim or timeout). Inspect run history, or reassign to hand the PR over."
              if reason == "active_pr" else "The dispatcher is holding this task before claiming it.")
    return [Diagnostic(
        kind="dispatch_guarded", severity="warning", title=f"Dispatch held: {reason}",
        detail=detail, first_seen_at=now, last_seen_at=now,
        data={"reason": reason, "category": "dispatch_hold"},
        actions=[DiagnosticAction(kind="cli_hint", label="Inspect task history", suggested=True,
                                  payload={"command": f"hermes kanban show {task_id}"})],
    )]
