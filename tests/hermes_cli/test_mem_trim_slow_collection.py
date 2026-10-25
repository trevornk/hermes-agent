"""Slow full collections are observable on every host, bounded, without changing when they run.

``trim_memory`` logged timing only after a successful ``malloc_trim``, so a macOS gateway whose
housekeeping ``gc.collect`` held the GIL for seconds (py-spy: 553/868 samples in the collector;
11.5 s and 5.2 s stretches observed) left no trace in its own log. Collection timing is now
recorded for the collection itself; a slow one is a rate-limited WARNING carrying what it
suppressed. Nothing here alters the cooldown, the floor or the collector.
"""
from __future__ import annotations

import logging
from itertools import count
from unittest.mock import Mock

import pytest

from hermes_cli import mem_trim


@pytest.fixture(autouse=True)
def _reset_trim_state(monkeypatch):
    monkeypatch.setattr(mem_trim, "_last_trim_monotonic", 0.0)
    monkeypatch.setattr(mem_trim, "_probe_done", True)
    monkeypatch.setattr(mem_trim, "_malloc_trim", None)   # no page-release primitive: macOS, musl
    monkeypatch.setattr(mem_trim, "_trim_call_count", 0)
    monkeypatch.setattr(mem_trim, "_last_slow_gc_log_monotonic", 0.0)
    monkeypatch.setattr(mem_trim, "_slow_gc_suppressed", 0)
    monkeypatch.setattr(mem_trim, "_config_settings", lambda: (True, 0.0, 1, 0.0))
    mem_trim.last_collection.clear()


def _collections_taking(monkeypatch, seconds: float, collected: int = 7):
    """Every ``gc.collect`` appears to take ``seconds`` on the perf clock (three reads per trim:
    start, the collection stamp, the trim total); returns the mocked collector."""
    collect = Mock(return_value=collected)
    monkeypatch.setattr(mem_trim.gc, "collect", collect)
    reads = count()
    monkeypatch.setattr(mem_trim.time, "perf_counter",
                        lambda: (0.0, seconds, seconds + 0.01)[next(reads) % 3])
    return collect


def _slow_records(caplog):
    return [r for r in caplog.records if r.name == mem_trim.logger.name and r.levelno == logging.WARNING
            and "slow gc.collect" in r.getMessage()]


def test_a_slow_collection_is_logged_without_a_page_release_primitive(monkeypatch, caplog):
    collect = _collections_taking(monkeypatch, 2.5)
    monkeypatch.setattr(mem_trim.time, "monotonic", lambda: 100.0)
    caplog.set_level(logging.DEBUG, logger=mem_trim.logger.name)

    assert mem_trim.trim_memory(force=True, reason="messaging gateway housekeeping") is False
    collect.assert_called_once()
    [record] = _slow_records(caplog)
    message = record.getMessage()
    assert "reason=messaging gateway housekeeping" in message
    assert "duration_ms=2500.0" in message and "collected=7" in message and "suppressed_since_last=0" in message
    assert mem_trim.last_collection["gc_ms"] == pytest.approx(2500.0)
    assert mem_trim.last_collection["reason"] == "messaging gateway housekeeping"


def test_a_fast_collection_stays_at_debug(monkeypatch, caplog):
    _collections_taking(monkeypatch, 0.05)
    monkeypatch.setattr(mem_trim.time, "monotonic", lambda: 100.0)
    caplog.set_level(logging.DEBUG, logger=mem_trim.logger.name)

    mem_trim.trim_memory(force=True, reason="tui turn completion")
    assert _slow_records(caplog) == []
    assert any(r.levelno == logging.DEBUG and r.getMessage().startswith("gc.collect:")
               for r in caplog.records if r.name == mem_trim.logger.name)
    assert mem_trim.last_collection["gc_ms"] == pytest.approx(50.0)


def test_slow_warnings_are_rate_limited_and_count_what_they_suppressed(monkeypatch, caplog):
    _collections_taking(monkeypatch, 2.0)
    caplog.set_level(logging.WARNING, logger=mem_trim.logger.name)
    clock = {"now": 100.0}
    monkeypatch.setattr(mem_trim.time, "monotonic", lambda: clock["now"])

    for now in (100.0, 110.0, 120.0):      # past the 5 s force floor, inside the 60 s log floor
        clock["now"] = now
        mem_trim.trim_memory(force=True, reason="agent close")
    assert len(_slow_records(caplog)) == 1
    assert mem_trim._slow_gc_suppressed == 2

    clock["now"] = 200.0                   # past the log floor: one line, carrying the two it held back
    mem_trim.trim_memory(force=True, reason="agent close")
    records = _slow_records(caplog)
    assert len(records) == 2
    assert "suppressed_since_last=2" in records[-1].getMessage()
    assert mem_trim._slow_gc_suppressed == 0


def test_collection_timing_never_changes_the_trim_verdict(monkeypatch):
    """Observability only: the page-release result and the cooldown stay exactly as before."""
    _collections_taking(monkeypatch, 3.0)
    monkeypatch.setattr(mem_trim, "_malloc_trim", Mock(return_value=1))
    monkeypatch.setattr(mem_trim.time, "monotonic", lambda: 100.0)
    assert mem_trim.trim_memory(reason="turn", cooldown_seconds=60) is True
    assert mem_trim.trim_memory(reason="turn", cooldown_seconds=60) is False   # cooldown, unchanged
