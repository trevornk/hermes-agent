"""Blocking compression is capped when the request already fits the model window.

Incident: a ~249k-token request in a 1M window waited the full 300 s idle / 600 s ceiling per attempt
for a compaction it did not need to send. A fitting request now waits at most
``compression.fits_window_blocking_seconds`` and is then sent uncompressed; the worker is NOT cancelled, so
its commit can still land for a later call. Over-window and unknown-window requests keep the old budgets.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from agent.conversation_compression import CompressionCommitFence, run_compress_context_with_progress_timeout
from agent.conversation_compression_deadline import resolve_fits_window_blocking_seconds


def _stalled_worker(release, committed, *, fenced=True):
    def worker(fence):
        if fenced:
            fence.mark_commit_watermark_fenced()  # the worker holds the lease + start watermark
        release.wait(10)
        if fence.begin_commit():
            try:
                committed.append(fence)
            finally:
                fence.finish_commit()
        return [{"role": "user", "content": "compacted"}], "new-prompt"
    return worker


@pytest.fixture
def stalled():
    release, committed = threading.Event(), []
    yield release, committed, _stalled_worker(release, committed)
    release.set()


def test_fitting_request_returns_uncompressed_at_the_cap_and_leaves_the_worker_running(stalled):
    release, committed, worker = stalled
    msgs = [{"role": "user", "content": "x"}]
    fence = CompressionCommitFence()
    started = time.monotonic()
    result = run_compress_context_with_progress_timeout(
        worker=worker, messages=msgs, system_prompt_fallback="sys", idle_timeout_seconds=30.0,
        total_ceiling_seconds=60.0, fence=fence, fits_window_blocking_seconds=0.2,
    )
    assert time.monotonic() - started < 5.0
    assert result[0] is msgs and result[1] == "sys"
    # Not cancelled: the worker may still commit for the next call.
    assert fence.is_cancelled is False
    release.set()
    deadline = time.monotonic() + 5
    while not committed and time.monotonic() < deadline:
        threading.Event().wait(0.02)
    assert committed == [fence]


def test_cap_waits_for_the_watermark_fence_before_detaching():
    release, committed = threading.Event(), []
    worker = _stalled_worker(release, committed, fenced=False)
    started = time.monotonic()
    try:
        run_compress_context_with_progress_timeout(
            worker=worker, messages=[{"role": "user", "content": "x"}], system_prompt_fallback="sys",
            idle_timeout_seconds=1.0, total_ceiling_seconds=2.0, stall_fallback=False,
            fits_window_blocking_seconds=0.1,
        )
    finally:
        release.set()
    # An unfenced late commit could clobber newer rows: the old budget (one idle window) applies.
    assert time.monotonic() - started >= 0.9


def test_cap_applies_as_soon_as_a_slow_starting_worker_holds_the_fence():
    release = threading.Event()

    def slow_start(fence):
        threading.Event().wait(0.6)  # lease acquisition took longer than the cap
        fence.mark_commit_watermark_fenced()
        release.wait(10)
        return [], ""

    started = time.monotonic()
    try:
        result = run_compress_context_with_progress_timeout(
            worker=slow_start, messages=[{"role": "user", "content": "x"}], system_prompt_fallback="sys",
            idle_timeout_seconds=30.0, total_ceiling_seconds=60.0, fits_window_blocking_seconds=0.1,
        )
        assert result[1] == "sys"
        assert time.monotonic() - started < 20.0  # detached right after the fence, not after the 30s idle budget
    finally:
        release.set()


def test_over_window_request_ignores_the_fits_window_cap(stalled):
    _release, _committed, worker = stalled
    started = time.monotonic()
    result = run_compress_context_with_progress_timeout(
        worker=worker, messages=[{"role": "user", "content": "x"}], system_prompt_fallback="sys",
        idle_timeout_seconds=1.0, total_ceiling_seconds=2.0, request_exceeds_window=True, stall_fallback=False,
        fits_window_blocking_seconds=0.1,
    )
    assert time.monotonic() - started >= 0.9  # one idle budget, not the 0.1s fits-window cap
    assert result[1] == "sys"



def test_no_cap_keeps_the_old_idle_budget(stalled):
    _release, _committed, worker = stalled
    started = time.monotonic()
    run_compress_context_with_progress_timeout(
        worker=worker, messages=[{"role": "user", "content": "x"}], system_prompt_fallback="sys",
        idle_timeout_seconds=1.0, total_ceiling_seconds=2.0, stall_fallback=False,
    )
    assert time.monotonic() - started >= 0.9


def test_progress_and_deadline_extension_do_not_lift_the_cap():
    release = threading.Event()

    def trickle(fence):
        fence.mark_commit_watermark_fenced()
        end = time.monotonic() + 5
        while time.monotonic() < end and not release.is_set():
            fence.touch_progress()
            fence.extend_deadline(5.0)  # prompt-reading style deadline push
            threading.Event().wait(0.02)
        return [], ""

    started = time.monotonic()
    try:
        run_compress_context_with_progress_timeout(
            worker=trickle, messages=[{"role": "user", "content": "x"}], system_prompt_fallback="sys",
            idle_timeout_seconds=30.0, total_ceiling_seconds=60.0, fits_window_blocking_seconds=0.3,
        )
        assert time.monotonic() - started < 3.0
    finally:
        release.set()


def test_resolver_default_override_and_invalid_values():
    assert resolve_fits_window_blocking_seconds({}) == 45.0
    assert resolve_fits_window_blocking_seconds({"fits_window_blocking_seconds": 12}) == 12.0
    for bad in (0, -5, "abc", True, float("nan"), float("inf")):
        assert resolve_fits_window_blocking_seconds({"fits_window_blocking_seconds": bad}) is None, bad


def test_default_config_declares_the_key():
    from hermes_cli.config import DEFAULT_CONFIG
    assert resolve_fits_window_blocking_seconds(DEFAULT_CONFIG["compression"]) is not None
    assert "fits_window_blocking_seconds" in DEFAULT_CONFIG["compression"]


@pytest.mark.parametrize(
    "approx_tokens,in_place,expect_fast", [(10, True, True), (10, False, False), (5_000, True, False), (None, True, False)]
)
def test_facade_applies_the_cap_only_to_fitting_in_place_requests(
    monkeypatch, stalled, approx_tokens, in_place, expect_fast
):
    from agent.compression_facade import _CommitFenceRegistration, _run_under_progress_timeout

    _release, _committed, worker = stalled
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"compression": {"fits_window_blocking_seconds": 0.2}}
    )
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(context_length=1000), _cached_system_prompt="sys",
        compression_in_place=in_place,
    )
    fence = CompressionCommitFence()
    started = time.monotonic()
    _run_under_progress_timeout(
        agent, lambda f, target_messages=None, same_turn_fallback_recovery=False: worker(f),
        [{"role": "user", "content": "x"}], "sys", active_fence=fence,
        registration=_CommitFenceRegistration(fence), fence_registration_lock=threading.RLock(),
        idle_timeout=1.0, total_ceiling=2.0, approx_tokens=approx_tokens,
    )
    elapsed = time.monotonic() - started
    if expect_fast:
        assert elapsed < 0.9
    else:
        assert elapsed >= 0.9
