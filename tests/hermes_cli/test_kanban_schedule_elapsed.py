"""``SCHEDULED_UNTIL`` markers on scheduled cards are honored by the dispatcher tick."""

import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _iso(delta_s: int, tz=timezone(timedelta(hours=-6))) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_s)).astimezone(tz).isoformat(timespec="seconds")


def _park(conn, reason, *, parents=()):
    tid = kb.create_task(conn, title="parked", assignee="dev", parents=list(parents))
    if not parents:
        run = kb.claim_task(conn, tid)
        assert kb.schedule_task(conn, tid, reason=reason, expected_run_id=run.current_run_id)
    else:
        assert kb.schedule_task(conn, tid, reason=reason)
    return tid


def _tick(conn):
    return kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=0)


def _status(conn, tid):
    return kb.get_task(conn, tid).status


def _kinds(conn, tid):
    return [e.kind for e in kb.list_events(conn, tid)]


def test_due_marker_releases_with_distinct_event(conn):
    tid = _park(conn, f"update installing SCHEDULED_UNTIL={_iso(-30)}")
    res = _tick(conn)
    assert res.schedule_elapsed == [tid]
    assert _status(conn, tid) == "ready"
    kinds = _kinds(conn, tid)
    assert kinds[-2:] == ["unblocked", "schedule_elapsed"]


def test_utc_z_marker_is_honored(conn):
    past = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tid = _park(conn, f"SCHEDULED_UNTIL={past}.")
    _tick(conn)
    assert _status(conn, tid) == "ready"


def test_future_marker_is_kept(conn):
    tid = _park(conn, f"SCHEDULED_UNTIL={_iso(3600)}")
    assert _tick(conn).schedule_elapsed == []
    assert _status(conn, tid) == "scheduled"


@pytest.mark.parametrize("reason", [
    "", "waiting for Casey", "SCHEDULED_UNTIL=", "SCHEDULED_UNTIL=soon",
    "SCHEDULED_UNTIL=2020-13-45T99:00:00-06:00", "SCHEDULED_UNTIL=2020-01-01",
])
def test_missing_or_malformed_marker_is_kept(conn, reason):
    tid = _park(conn, reason or "park without marker")
    _tick(conn)
    assert _status(conn, tid) == "scheduled"


def test_naive_timestamp_is_kept(conn):
    tid = _park(conn, "SCHEDULED_UNTIL=2020-01-01T00:00:00")
    _tick(conn)
    assert _status(conn, tid) == "scheduled"


def test_repark_uses_newest_marker(conn):
    tid = _park(conn, f"SCHEDULED_UNTIL={_iso(-30)}")
    _tick(conn)
    assert _status(conn, tid) == "ready"
    run = kb.claim_task(conn, tid)
    assert kb.schedule_task(conn, tid, reason=f"SCHEDULED_UNTIL={_iso(3600)}",
                            expected_run_id=run.current_run_id)
    assert _tick(conn).schedule_elapsed == []
    assert _status(conn, tid) == "scheduled"


def test_release_is_idempotent(conn):
    tid = _park(conn, f"SCHEDULED_UNTIL={_iso(-30)}")
    _tick(conn)
    assert _tick(conn).schedule_elapsed == []
    assert _kinds(conn, tid).count("schedule_elapsed") == 1


def test_release_respects_unsatisfied_parents(conn):
    parent = kb.create_task(conn, title="parent", assignee="dev")
    tid = _park(conn, f"SCHEDULED_UNTIL={_iso(-30)}", parents=[parent])
    assert _status(conn, tid) == "scheduled"
    assert _tick(conn).schedule_elapsed == [tid]
    assert _status(conn, tid) == "todo"
    assert "schedule_elapsed" in _kinds(conn, tid)


def test_dry_run_does_not_release(conn):
    tid = _park(conn, f"SCHEDULED_UNTIL={_iso(-30)}")
    kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=True)
    assert _status(conn, tid) == "scheduled"


def test_tool_description_documents_marker():
    from tools.kanban_tools_schemas import KANBAN_SCHEDULE_SCHEMA
    text = KANBAN_SCHEDULE_SCHEMA["description"]
    assert "SCHEDULED_UNTIL" in text and "orchestrator" in text
    assert "does not create a timer" not in text
