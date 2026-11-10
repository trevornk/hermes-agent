"""A fresh heartbeat must not preserve a stale active-work count."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import run, status


@pytest.mark.parametrize("live_count", [0, 2])
def test_heartbeat_refreshes_stale_active_agents(tmp_path, monkeypatch, live_count):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    writer = status._RuntimeStatusWriter()
    monkeypatch.setattr(status, "_runtime_status_writer", writer)
    status.publish_runtime_status(gateway_state="running", active_agents=7)
    assert writer.flush(timeout=2)
    assert json.loads((tmp_path / "gateway_state.json").read_text())["active_agents"] == 7

    count = Mock(return_value=live_count)
    runner = SimpleNamespace(_active_work_count=count)
    stop = threading.Event()

    def heartbeat_only(label, fn):
        if label == "Runtime heartbeat":
            fn()
            stop.set()

    # Run the actual scheduled heartbeat, without unrelated housekeeping side effects.
    monkeypatch.setattr(run, "_housekeeping_chore", heartbeat_only)
    try:
        run._start_gateway_housekeeping(stop, interval=0, runner=runner)
        assert writer.flush(timeout=2)
        record = json.loads((tmp_path / "gateway_state.json").read_text())
        assert record["active_agents"] == live_count
        assert record["gateway_state"] == "running"
        count.assert_called_once_with()
    finally:
        writer.flush(timeout=2)


def test_status_publication_remains_best_effort(monkeypatch):
    publish = Mock(side_effect=OSError("status unavailable"))
    monkeypatch.setattr(status, "publish_runtime_status", publish)
    run._write_runtime_status_quiet()
    publish.assert_called_once_with()


def test_heartbeat_count_failure_remains_best_effort(monkeypatch):
    publish = Mock()
    monkeypatch.setattr(status, "publish_runtime_status", publish)
    runner = SimpleNamespace(_active_work_count=Mock(side_effect=RuntimeError("unavailable")))
    run._write_runtime_status_quiet(runner=runner)
    publish.assert_not_called()
