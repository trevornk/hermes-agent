"""Timer lanes trim without a full collection (#139763).

The gateway housekeeping tick and the TUI idle reaper call ``trim_memory`` on a fixed cooldown. A
full ``gc.collect()`` there walks the whole tracked heap with the GIL held: 1-9.7 s every ~60 s on a
macOS gateway, freezing the kanban owner loop past the worker WebSocket open timeout. Those lanes
pass ``collect=False``; the pressure lanes keep collect-then-trim.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from hermes_cli import mem_trim


@pytest.fixture(autouse=True)
def _reset_trim_state(monkeypatch):
    monkeypatch.setattr(mem_trim, "_last_trim_monotonic", 0.0)
    monkeypatch.setattr(mem_trim, "_probe_done", True)
    monkeypatch.setattr(mem_trim, "_malloc_trim", None)
    monkeypatch.setattr(mem_trim, "_trim_call_count", 0)
    monkeypatch.setattr(mem_trim, "_config_settings", lambda: (True, 60.0, 1, 0.0))
    monkeypatch.setattr(mem_trim.time, "monotonic", lambda: 100.0)


def test_timer_lane_trims_without_collecting_where_pages_can_be_released(monkeypatch):
    collect = Mock(return_value=0)
    trim = Mock(return_value=1)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)
    monkeypatch.setattr(mem_trim, "_malloc_trim", trim)

    assert mem_trim.trim_memory(reason="messaging gateway housekeeping", collect=False) is True
    collect.assert_not_called()
    trim.assert_called_once_with(0)
    assert mem_trim._last_trim_monotonic == 100.0   # the cooldown still governs a real trim


def test_timer_lane_is_a_no_op_without_a_page_release_primitive(monkeypatch):
    """macOS/musl: nothing to do, so neither a collection nor a claimed cooldown."""
    collect = Mock(return_value=0)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)

    assert mem_trim.trim_memory(reason="idle reaper periodic trim", collect=False) is False
    collect.assert_not_called()
    assert mem_trim._last_trim_monotonic == 0.0


def test_timer_lane_does_not_starve_a_pressure_lane_collection(monkeypatch):
    """A no-op tick must not hold the shared cooldown against the next pressure-lane collection."""
    collect = Mock(return_value=3)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)

    mem_trim.trim_memory(reason="messaging gateway housekeeping", collect=False)
    mem_trim.trim_memory(reason="large tool result")
    collect.assert_called_once_with()


def test_pressure_lanes_still_collect_before_trimming(monkeypatch):
    collect = Mock(return_value=3)
    trim = Mock(return_value=1)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)
    monkeypatch.setattr(mem_trim, "_malloc_trim", trim)

    assert mem_trim.trim_memory(reason="post-compression") is True
    collect.assert_called_once_with()
    trim.assert_called_once_with(0)
