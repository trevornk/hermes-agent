"""Read-only proof for a needs_input continuation of an existing PR."""
from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Any, Optional
import time


def _proof_epoch(value: Any) -> Optional[int]:
    """Never truncate fractional stamps or guess the timezone of a legacy stamp."""
    if type(value) is int:
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    if value.isascii() and value.isdigit():
        return int(value) or None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is not None and not stamp.microsecond:
            return int(stamp.timestamp())
    except (ValueError, OverflowError, OSError):
        return None
    return None


def is_needs_input_continuation(conn: sqlite3.Connection, task_id: str, since: int) -> bool:
    """The latest attempt's own typed block followed by an explicit unblock.

    Run/event ids establish arrival order; timestamps independently establish
    strict causality. A new claim consumes this proof, even if spawning fails.
    Task fields alone cannot prove the kind: unblock deliberately retains them.
    """
    from hermes_cli import kanban_db as kb

    run = conn.execute(
        "SELECT r.*, t.assignee, t.status AS task_status, t.current_run_id "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.task_id = ? ORDER BY r.id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if (run is None or run["outcome"] != "blocked" or run["task_status"] != "ready"
            or run["current_run_id"] is not None or not run["profile"]
            or run["profile"] != run["assignee"]):
        return False
    started, ended = _proof_epoch(run["started_at"]), _proof_epoch(run["ended_at"])
    if started is None or ended is None or started > ended:
        return False
    block = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1", (task_id, run["id"]),
    ).fetchone()
    if block is None or kb._json_dict(block["payload"]).get("kind") != "needs_input":
        return False
    blocked_at = _proof_epoch(block["created_at"])
    if blocked_at is None or blocked_at < ended:
        return False
    if not _pr_stamps_readable(conn, task_id):
        return False
    return _explicit_unblock_after(conn, task_id, block["id"], max(since, ended, blocked_at), run, blocked_at)


def _explicit_unblock_after(
    conn: sqlite3.Connection, task_id: str, block_id: int, after: int, run: sqlite3.Row, blocked_at: int,
) -> bool:
    """Only observations may accompany the unblock; every other transition invalidates it."""
    unblocked = False
    for event in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
        (task_id, block_id),
    ):
        if event["kind"] in {"commented", "respawn_guarded", "heartbeat"}:
            continue
        if event["kind"] == "worker_result":
            if not _blocked_run_result(conn, task_id, run, event, block_id, blocked_at):
                return False
            continue
        # Unknown/new lifecycle events fail closed too. Do not skip malformed
        # or backdated transitions by selecting events with a timestamp range.
        if event["kind"] != "unblocked" or unblocked:
            return False
        stamp = _proof_epoch(event["created_at"])
        if stamp is None or not after < stamp <= int(time.time()):
            return False
        unblocked = True
    return unblocked


def _pr_stamps_readable(conn: sqlite3.Connection, task_id: str) -> bool:
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE
    from hermes_cli import kanban_db as kb

    for comment in conn.execute("SELECT body, created_at FROM task_comments WHERE task_id = ?", (task_id,)):
        if (_RESPAWN_GUARD_PR_URL_RE.search(kb._lossy_text(comment["body"]) or "")
                and _proof_epoch(comment["created_at"]) is None):
            return False
    return True



def _blocked_run_result(
    conn: sqlite3.Connection, task_id: str, run: sqlite3.Row, event: sqlite3.Row,
    block_id: int, blocked_at: int,
) -> bool:
    """A successful managed-worker receipt observes its own closed run; it grants nothing.

    Match the immutable worker_bound identity, not run.worker_pid (the submitter,
    which can differ from the interpreter). Never read last_output as authority.
    """
    from hermes_cli import kanban_db as kb

    data = kb._json_dict(event["payload"])
    if (event["run_id"] != run["id"] or type(data.get("exit_code")) is not int
            or data["exit_code"] != 0 or not run["claim_lock"]
            or data.get("claim_lock") != run["claim_lock"]):
        return False
    stamp = _proof_epoch(event["created_at"])
    if stamp is None or not blocked_at <= stamp <= int(time.time()):
        return False
    bound = conn.execute(
        "SELECT id, payload, created_at FROM task_events WHERE task_id=? AND run_id=? "
        "AND kind='worker_bound' ORDER BY id DESC LIMIT 1", (task_id, run["id"]),
    ).fetchone()
    if bound is None or bound["id"] >= block_id:
        return False
    identity = kb._json_dict(bound["payload"])
    bound_at, started = _proof_epoch(bound["created_at"]), _proof_epoch(run["started_at"])
    if bound_at is None or started is None or not started <= bound_at <= blocked_at:
        return False
    pid = data.get("pid")
    return (type(pid) is int and pid > 0
            and (pid, data["claim_lock"]) == (identity.get("pid"), identity.get("claim_lock")))
