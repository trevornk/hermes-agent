"""The pinned stall-fallback retry only runs for engines that consume the summary-route pin.

``pin_summary_route`` is read by the built-in compressor (``take_pinned_summary_route``); an engine that never
reads it (the LCM plugin) re-runs the same route on retry and re-pays the whole prep for nothing. Engines
declare ``honors_summary_route_pin``; the base default is False and the built-in compressor says True.
"""

import logging
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor, take_pinned_summary_route
from agent.context_engine import ContextEngine
from agent.conversation_compression import run_compress_context_with_progress_timeout

ROUTE = {"label": "fallback_chain[0](custom)", "provider": "custom", "model": "backup", "timeout": 1.0}


class _Worker:
    """First attempt stalls (no progress); a later attempt commits."""

    def __init__(self):
        self.routes = []
        self.release = threading.Event()

    def __call__(self, fence):
        self.routes.append(take_pinned_summary_route())
        if len(self.routes) == 1:
            self.release.wait(10)
            return [{"role": "assistant", "content": "late"}], "late"
        if not fence.begin_commit():
            return [{"role": "assistant", "content": "cancelled"}], "cancelled"
        try:
            return [{"role": "assistant", "content": "compressed"}], "summarized"
        finally:
            fence.finish_commit()


def _run(engine, *, request_exceeds_window=False, with_agent=True):
    worker, msgs = _Worker(), [{"role": "user", "content": "x"}]
    agent = SimpleNamespace(context_compressor=engine) if with_agent else None
    try:
        with patch("agent.conversation_compression.resolve_compression_fallback_route", return_value=dict(ROUTE)):
            result = run_compress_context_with_progress_timeout(
                worker=worker, messages=msgs, system_prompt_fallback="degraded", idle_timeout_seconds=0.5,
                total_ceiling_seconds=3.0, telemetry_agent=agent, request_exceeds_window=request_exceeds_window,
            )
    finally:
        worker.release.set()
    return worker, msgs, result


def test_capability_defaults():
    assert ContextEngine.honors_summary_route_pin is False
    assert ContextCompressor.honors_summary_route_pin is True


def test_engine_that_ignores_the_pin_gets_no_pinned_retry(caplog):
    caplog.set_level(logging.INFO)
    worker, msgs, result = _run(SimpleNamespace())  # third-party engine without the attribute
    assert len(worker.routes) == 1
    assert result[0] is msgs
    assert any("does not honor summary-route pins" in r.getMessage() for r in caplog.records)


def test_engine_declaring_false_gets_no_pinned_retry():
    worker, msgs, result = _run(SimpleNamespace(honors_summary_route_pin=False))
    assert len(worker.routes) == 1 and result[0] is msgs


def test_deterministic_route_is_also_skipped_for_a_non_honoring_engine():
    # Over-window escalates to the deterministic sentinel pin, which only compress() consumes.
    worker, msgs, result = _run(SimpleNamespace(), request_exceeds_window=True)
    assert len(worker.routes) == 1 and result[0] is msgs


@pytest.mark.parametrize("engine", [SimpleNamespace(honors_summary_route_pin=True), None], ids=["honors", "no_agent"])
def test_honoring_engine_or_unknown_engine_still_retries(engine):
    worker, _msgs, result = _run(engine, with_agent=engine is not None)
    assert len(worker.routes) == 2 and worker.routes[1] is not None
    assert result[1] == "summarized"
