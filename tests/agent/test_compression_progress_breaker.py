"""Real-token no-progress circuit breaker (incident 2026-11-10: 188 compactions, 176 of them N->N).

Compaction that does not lower the REAL input tokens of the next provider call must stop being retried, whatever
the cause. The breaker is host-side and engine-agnostic, built on ``agent.compression_suppression``.
"""

import logging
from types import SimpleNamespace

import pytest

from agent import compression_suppression as sup
from agent.compression_progress_breaker import MARGIN

TRIGGER = 150_000
REAL = 200_000


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"compression": {"fits_window_blocking_seconds": 5, "context_timeout_seconds": 60,
                                 "context_total_ceiling_seconds": 30}},
    )
    from run_agent import AIAgent

    a = AIAgent(api_key="k", base_url="https://inference-api.nousresearch.com/v1", provider="nous",
                api_mode="chat_completions", model="anthropic/claude-fable-5.1", session_id="sess-1", platform="cli",
                quiet_mode=True, skip_context_files=True, skip_memory=True, save_trajectories=False,
                enabled_toolsets=["file"])
    a.context_compressor.context_length = 1_000_000
    a.context_compressor.threshold_tokens = TRIGGER
    a._current_turn_id = "turn-1"
    yield a
    a.close()


class FakeCompress:
    """Stands in for ``compress_context``: records calls; ``shrink`` is the real-token effect of a pass."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, agent, messages, system_message, **kw):
        self.calls.append(kw)
        agent._compression_skipped_due_to_lock = agent._compression_blocked_transient = None  # as compress_context does
        return messages, system_message  # N->N: the very same transcript


@pytest.fixture
def engine(monkeypatch):
    fake = FakeCompress()
    monkeypatch.setattr("agent.conversation_compression.compress_context", fake)
    return fake


def _usage(prompt: int) -> SimpleNamespace:
    # Anthropic shape: uncached input + cache read + cache write all count toward the real input.
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=5, total_tokens=prompt + 5,
                           prompt_tokens_details=SimpleNamespace(cached_tokens=prompt // 2, cache_write_tokens=100),
                           completion_tokens_details=None)


def _api_call(agent, msgs, real: int) -> None:
    from agent import turn_usage

    turn_usage.record_response_usage(
        agent, SimpleNamespace(usage=_usage(real), id=None, model=agent.model), messages=msgs, api_call_count=1,
        api_duration=0.1, compression_attempts=0, max_compression_attempts=3)


def _turn(agent, msgs, real_for_next_call, last_real) -> None:
    """One pre-API iteration: compress when the last real input is over the trigger, then call the provider."""
    if last_real > TRIGGER:
        agent._compress_context(msgs, "sys", approx_tokens=last_real, trigger="pre_api")
    _api_call(agent, msgs, real_for_next_call)


def _run_unchanged(agent, msgs, calls: int, real: int = REAL) -> None:
    _api_call(agent, msgs, real)  # the call whose usage the first compaction is judged against
    for _ in range(calls):
        _turn(agent, msgs, real, real)


def test_repeated_noop_compaction_on_unchanged_context_is_cut_off_after_two_attempts(agent, engine, caplog):
    msgs = [{"role": "user", "content": "x"}]
    with caplog.at_level(logging.WARNING):
        _run_unchanged(agent, msgs, 10)
    assert len(engine.calls) == 2
    assert sup.is_suppressed(agent) == sup.REASON_NO_REAL_PROGRESS
    warn = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "no_real_progress" in r.getMessage()]
    assert warn, "arming must log at WARNING"
    assert any("sess-1" in m and str(REAL) in m.replace(",", "") for m in warn)


def test_growth_by_one_leaf_chunk_lifts_the_breaker_for_exactly_one_new_attempt(agent, engine, caplog):
    msgs = [{"role": "user", "content": "x"}]
    _run_unchanged(agent, msgs, 6)
    assert len(engine.calls) == 2
    # Growth below a chunk: still suppressed.
    _turn(agent, msgs, REAL + 19_000, REAL)
    assert sup.is_suppressed(agent) == sup.REASON_NO_REAL_PROGRESS
    with caplog.at_level(logging.WARNING):
        _turn(agent, msgs, REAL + 20_000, REAL + 19_000)  # lifts on this usage
    assert sup.is_suppressed(agent) is None
    assert any(r.levelno == logging.WARNING and "lifted" in r.getMessage() for r in caplog.records)
    before = len(engine.calls)
    last = REAL + 20_000
    for _ in range(6):
        _turn(agent, msgs, last, last)
    assert len(engine.calls) - before == 2  # a fresh pass that fails twice re-arms
    assert sup.is_suppressed(agent) == sup.REASON_NO_REAL_PROGRESS


def test_engine_leaf_chunk_tokens_sets_the_growth_needed_to_lift(agent, engine):
    agent.context_compressor.leaf_chunk_tokens = 5_000
    msgs = [{"role": "user", "content": "x"}]
    _run_unchanged(agent, msgs, 4)
    assert sup.is_suppressed(agent)
    _api_call(agent, msgs, REAL + 5_000)
    assert sup.is_suppressed(agent) is None


def test_new_summary_node_lifts_the_breaker(agent, engine):
    agent.context_compressor.summary_node_count = 3
    msgs = [{"role": "user", "content": "x"}]
    _run_unchanged(agent, msgs, 4)
    assert sup.is_suppressed(agent)
    _api_call(agent, msgs, REAL)  # no growth, nodes unchanged
    assert sup.is_suppressed(agent)
    agent.context_compressor.summary_node_count = 4
    _api_call(agent, msgs, REAL)
    assert sup.is_suppressed(agent) is None


def test_genuine_progress_never_arms(agent, engine):
    msgs = [{"role": "user", "content": "x"}]
    _api_call(agent, msgs, REAL)
    for _ in range(8):
        agent._compress_context(msgs, "sys", approx_tokens=REAL, trigger="pre_api")
        _api_call(agent, msgs, REAL - 80_000)  # compaction cut the real input by 40%
        _api_call(agent, msgs, REAL)  # the conversation regrows; no compaction pending, no verdict
    assert sup.is_suppressed(agent) is None
    assert len(engine.calls) == 8


def test_strike_streak_resets_on_a_good_pass(agent, engine):
    msgs = [{"role": "user", "content": "x"}]
    _api_call(agent, msgs, REAL)
    agent._compress_context(msgs, "sys", approx_tokens=REAL)
    _api_call(agent, msgs, REAL)  # strike 1
    agent._compress_context(msgs, "sys", approx_tokens=REAL)
    _api_call(agent, msgs, REAL // 2)  # good pass: streak resets
    agent._compress_context(msgs, "sys", approx_tokens=REAL // 2)
    _api_call(agent, msgs, REAL // 2)  # strike 1 again, not 2
    assert sup.is_suppressed(agent) is None


@pytest.mark.parametrize(
    "call_kwargs",
    [{"approx_tokens": 2_000_000}, {"approx_tokens": None}, {"approx_tokens": REAL, "force": True},
     {"approx_tokens": REAL, "bypass_cooldown": True}],
    ids=["over_window", "unknown_window", "force", "bypass"],
)
def test_armed_breaker_never_blocks_exempt_requests(agent, engine, call_kwargs):
    msgs = [{"role": "user", "content": "x"}]
    _run_unchanged(agent, msgs, 4)
    assert sup.is_suppressed(agent) == sup.REASON_NO_REAL_PROGRESS
    before = len(engine.calls)
    agent._compress_context(msgs, "sys", **call_kwargs)
    assert len(engine.calls) == before + 1


def test_forced_and_bypass_passes_do_not_count_as_strikes(agent, engine):
    msgs = [{"role": "user", "content": "x"}]
    _api_call(agent, msgs, REAL)
    for kw in ({"force": True}, {"bypass_cooldown": True}) * 3:
        agent._compress_context(msgs, "sys", approx_tokens=REAL, **kw)
        _api_call(agent, msgs, REAL)
    assert sup.is_suppressed(agent) is None


def test_new_session_resets_the_breaker_but_rotation_does_not(agent, engine):
    msgs = [{"role": "user", "content": "x"}]
    _run_unchanged(agent, msgs, 4)
    assert sup.is_suppressed(agent)
    agent.session_id = "sess-1-child"  # compression-driven rotation: same conversation
    assert sup.is_suppressed(agent) == sup.REASON_NO_REAL_PROGRESS
    agent.reset_session_state()  # /new, /resume, /branch
    assert sup.is_suppressed(agent) is None
    before = len(engine.calls)
    _run_unchanged(agent, msgs, 4)
    assert len(engine.calls) - before == 2  # starts from a clean slate
