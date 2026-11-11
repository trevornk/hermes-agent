"""One blocking compression attempt per turn once a fitting request timed out.

The suppression state is host-side and engine-agnostic: the LCM engine has neither
``record_timeout_failure`` nor ``_consecutive_timeout_failures``, so the engine cooldown never armed and every
API call of the turn re-blocked on a fresh compaction. ``agent.compression_suppression`` is the shared
primitive (other breakers arm it under their own reason); the ``fits_window_timeout`` reason is turn-scoped and
only ever applies to automatic compression of a request that fits the model window.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from agent import compression_suppression as sup
from tests.waiting import wait_until


def test_primitive_arm_query_clear_and_turn_scope():
    agent = SimpleNamespace(_current_turn_id="t1")
    assert sup.is_suppressed(agent) is None
    sup.arm(agent, "fits_window_timeout", scope="turn")
    sup.arm(agent, "no_real_progress", scope="session")
    assert sup.is_suppressed(agent) in {"fits_window_timeout", "no_real_progress"}
    sup.clear(agent, "no_real_progress")
    assert sup.is_suppressed(agent) == "fits_window_timeout"
    agent._current_turn_id = "t2"  # new turn: the turn-scoped reason lapses by itself
    assert sup.is_suppressed(agent) is None
    sup.arm(agent, "no_real_progress", scope="session")
    agent._current_turn_id = "t3"
    assert sup.is_suppressed(agent) == "no_real_progress"  # session scope survives turns
    sup.clear(agent)
    assert sup.is_suppressed(agent) is None


class _Engine:
    """LCM-shaped: no record_timeout_failure, no _consecutive_timeout_failures."""

    context_length = 1000


def _make_agent(monkeypatch, *, cap=0.2):
    from agent.compression_facade import CompressionFacadeMixin

    class _Agent(CompressionFacadeMixin):
        def _conversation_root_id(self):
            return None

    agent = _Agent()
    agent.context_compressor = _Engine()
    agent._cached_system_prompt = "sys"
    agent._current_turn_id = "turn-1"
    agent.session_id = "s"
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"compression": {"fits_window_blocking_seconds": cap, "context_timeout_seconds": 60,
                                 "context_total_ceiling_seconds": 10}},
    )
    return agent


@pytest.fixture
def worker(monkeypatch):
    """Patch compress_context: the first call stalls until released, later calls return at once."""
    release, calls = threading.Event(), type("Calls", (list,), {})()
    calls.release = release

    def fake_compress_context(agent, messages, system_message, **kw):
        calls.append(kw)
        if len(calls) == 1:
            kw["commit_fence"].mark_commit_watermark_fenced()  # lease + start watermark held
            release.wait(10)
        return messages, system_message

    monkeypatch.setattr("agent.conversation_compression.compress_context", fake_compress_context)
    yield calls
    release.set()


def test_timeout_on_a_fitting_request_suppresses_the_rest_of_the_turn_without_engine_support(monkeypatch, worker):
    agent = _make_agent(monkeypatch)
    msgs = [{"role": "user", "content": "x"}]
    started = time.monotonic()
    assert agent._compress_context(msgs, "sys", approx_tokens=500)[0] is msgs
    assert time.monotonic() - started < 30  # the 300s aux-floored idle budget would be far above this
    assert len(worker) == 1
    assert sup.is_suppressed(agent) == "fits_window_timeout"

    started = time.monotonic()
    out = agent._compress_context(msgs, "sys", approx_tokens=500, trigger="pre_api")
    assert out[0] is msgs and time.monotonic() - started < 0.5
    assert len(worker) == 1  # no second worker, no second blocking wait
    from agent.conversation_compression import compression_blocked_transiently, compression_skipped_due_to_lock
    # Callers refund + proceed, like a cooldown defer (a still-running detached worker reports as a lock skip).
    assert compression_blocked_transiently(agent) or compression_skipped_due_to_lock(agent)

    worker.release.set()  # the first attempt finishes (a no-op), so nothing is in flight any more
    agent._current_turn_id = "turn-2"  # a new turn re-enables blocking compression
    wait_until(lambda: agent._compress_context(msgs, "sys", approx_tokens=500) and len(worker) == 2,
               what="a new turn compresses again")


@pytest.mark.parametrize(
    "call_kwargs",
    [
        {"approx_tokens": 5_000},                          # over the 1000-token window
        {"approx_tokens": None},                           # unknown fit
        {"approx_tokens": 500, "force": True},             # manual /compress
        {"approx_tokens": 500, "bypass_cooldown": True},   # provider-proven overflow recovery
    ],
    ids=["over_window", "unknown_window", "force", "overflow_bypass"],
)
def test_suppression_never_applies_to_these_calls(monkeypatch, worker, call_kwargs):
    agent = _make_agent(monkeypatch)
    sup.arm(agent, "fits_window_timeout", scope="turn")
    worker.append({})  # pretend a first call already happened so this one returns at once
    before = len(worker)
    agent._compress_context([{"role": "user", "content": "x"}], "sys", **call_kwargs)
    assert len(worker) == before + 1


def test_armed_suppression_skips_a_fitting_request_with_a_transient_defer(monkeypatch, worker):
    agent = _make_agent(monkeypatch)
    sup.arm(agent, "fits_window_timeout", scope="turn")
    msgs = [{"role": "user", "content": "x"}]
    assert agent._compress_context(msgs, "sys", approx_tokens=500)[0] is msgs
    assert worker == []  # no worker was started
    from agent.conversation_compression import compression_blocked_transiently

    assert compression_blocked_transiently(agent)
