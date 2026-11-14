"""A gateway's first partial status write must not revive the previous life's drain."""

import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from gateway import status
from hermes_cli.gateway import _runtime_health_lines
from hermes_cli import gateway_multiplex_mode as multiplex


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "owns_gateway_runtime_lock", lambda: True)
    monkeypatch.setattr(sys, "argv", ["hermes", "send"])
    return tmp_path / "gateway_state.json", status._build_pid_record()


def seed_drain(path, identity):
    payload = {
        **identity,
        "gateway_state": "draining",
        "exit_reason": "signal",
        "restart_requested": True,
        "active_agents": 2,
        "active_work": [{"kind": "agent", "session_id": "old-session"}],
        "updated_at": (datetime.now(UTC) - timedelta(minutes=8)).isoformat(),
        "platforms": {"telegram": {"state": "connected"}},
    }
    path.write_text(json.dumps(payload))
    return payload


def assert_starting():
    payload = status.read_runtime_status()
    assert payload["gateway_state"] == "starting"
    assert payload["active_agents"] == 0
    assert payload["active_work"] is None
    assert payload["restart_requested"] is False
    assert payload["exit_reason"] is None
    assert not any("draining" in line for line in _runtime_health_lines())
    return payload


def test_first_multiplex_write_resets_previous_life(runtime, monkeypatch):
    path, current = runtime
    previous_pid = current["pid"] + 1000000
    seed_drain(path, {**current, "pid": previous_pid})
    monkeypatch.setattr(status, "_pid_exists", lambda pid: pid == current["pid"])
    assert any("Stale gateway_state.json" in line for line in _runtime_health_lines())
    monkeypatch.setattr(multiplex, "_pending_decision", [multiplex.MultiplexDecision(True, "default", "unset")])

    multiplex.publish_pending_multiplex_decision()

    # Read back the persisted snapshot, as a separate status command would.
    monkeypatch.setattr(status, "_runtime_status_state", None)
    payload = assert_starting()
    assert payload["pid"] == current["pid"]
    assert payload["start_time"] == current["start_time"]
    assert payload["platforms"] == {"telegram": {"state": "connected"}}
    assert payload["multiplex_standalone_reason"] is None


@pytest.mark.parametrize("identity_change", ["reused_pid", "missing_pid"])
def test_new_identity_resets_drain(runtime, identity_change):
    path, current = runtime
    previous = dict(current)
    if identity_change == "reused_pid":
        assert current["start_time"] is not None
        previous["start_time"] = current["start_time"] - 60
    else:
        previous.pop("pid")
    seed_drain(path, previous)

    status.write_runtime_status(parked_profiles={"other": "unavailable"})

    assert_starting()


def test_same_life_partial_write_preserves_genuine_drain(runtime):
    status.write_runtime_status(gateway_state="draining", active_agents=2)

    status.write_runtime_status(platform="telegram", platform_state="connected")

    payload = status.read_runtime_status()
    assert payload["gateway_state"] == "draining"
    assert payload["active_agents"] == 2
    assert any("Gateway draining for shutdown (2 active agent(s))" in line for line in _runtime_health_lines())


def test_slow_startup_partial_writes_stay_starting(runtime):
    path, current = runtime
    seed_drain(path, {**current, "pid": current["pid"] + 1})
    status.write_runtime_status(gateway_state="starting")
    assert_starting()

    for fields in (
        {"platform": "telegram", "platform_state": "connecting"},
        {"multiplex_standalone_reason": None},
        {"parked_profiles": {"other": "unavailable"}},
    ):
        status.write_runtime_status(**fields)
        assert_starting()


def test_non_owner_preserves_live_owner_drain(runtime, monkeypatch):
    path, current = runtime
    owner = {**current, "pid": current["pid"] + 1, "start_time": current["start_time"] - 60,
             "argv": ["hermes", "gateway", "run"]}
    previous = seed_drain(path, owner)
    monkeypatch.setattr(status, "owns_gateway_runtime_lock", lambda: False)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: pid == owner["pid"])
    monkeypatch.setattr(status, "_get_process_start_time", lambda pid: owner["start_time"])
    assert status.runtime_status_pid_is_live(previous)

    status.write_runtime_status(platform="matrix", platform_state="connected")

    payload = status.read_runtime_status()
    for field in ("pid", "start_time", "argv", "hermes_home", "gateway_state", "active_agents",
                  "active_work", "restart_requested", "exit_reason"):
        assert payload[field] == previous[field]
    assert payload["platforms"]["matrix"]["state"] == "connected"


def test_explicit_lifecycle_fields_win_on_new_life(runtime):
    path, current = runtime
    seed_drain(path, {**current, "pid": current["pid"] + 1})

    status.write_runtime_status(gateway_state="startup_failed", exit_reason="x")

    payload = status.read_runtime_status()
    assert payload["gateway_state"] == "startup_failed"
    assert payload["exit_reason"] == "x"
    assert payload["active_agents"] == 0
