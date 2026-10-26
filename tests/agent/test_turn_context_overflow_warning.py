"""Tests for the silent-context-overflow warning (the fix for the bug where a
session crosses the compression threshold but compression is blocked — by the
summary-LLM cooldown (#11529) or anti-thrashing (#40803) — and the model then
silently stops answering because nothing tells the user why.

The fix surfaces a deduped ``_emit_warning`` from ``build_turn_context`` and
exposes ``ContextCompressor.should_compress_info`` (a ``(bool, reason)`` tuple)
so callers can tell *why* compression was skipped while still over threshold.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.status_output import StatusOutputMixin
from agent.turn_context import build_turn_context
from agent.usage_anchor import capture_usage_anchor
from tests.agent.test_turn_context import _FakeAgent


# ---------------------------------------------------------------------------
# Unit tests for ContextCompressor.should_compress_info
# ---------------------------------------------------------------------------

def _make_compressor(**kwargs) -> ContextCompressor:
    defaults = dict(
        model="test-model",
        threshold_percent=0.65,
        protect_first_n=2,
        protect_last_n=3,
        quiet_mode=True,
    )
    defaults.update(kwargs)
    # 96K context -> small-context floor raises threshold_percent to 0.75,
    # so threshold_tokens = 72_000. 73_000 is "over threshold".
    with patch("agent.context_compressor.get_model_context_length", return_value=96000):
        comp = ContextCompressor(**defaults)
        # Resolve while the mock is active (lazy init, #32221).
        _ = comp.context_length
        return comp


class TestShouldCompressInfo:


    def test_cooldown_reports_reason(self):
        comp = _make_compressor()
        comp.last_prompt_tokens = comp.last_real_prompt_tokens = 73_000
        comp._summary_failure_cooldown_until = time.monotonic() + 60
        should, reason = comp.should_compress_info(73_000)
        assert should is False
        assert reason is not None
        assert reason.startswith("cooldown:")



    def test_should_compress_bool_shim_unchanged(self):
        """should_compress() must still return a bare bool for existing
        callers in conversation_loop.py (and/or chains)."""
        comp = _make_compressor()
        comp.last_prompt_tokens = comp.last_real_prompt_tokens = 73_000
        comp._summary_failure_cooldown_until = time.monotonic() + 60
        result = comp.should_compress(73_000)
        assert result is False
        assert not isinstance(result, tuple)


# ---------------------------------------------------------------------------
# Integration tests: build_turn_context surfaces the warning
# ---------------------------------------------------------------------------

class _WarnAgent(_FakeAgent):
    """_FakeAgent already covers the prologue; we just enable compression and
    record _emit_warning calls (the base class now has a MagicMock for it)."""

    def __init__(self):
        super().__init__()
        self.compression_enabled = True
        self._warnings = []
        self._compress_calls = 0
        # Replace the MagicMock with a recorder so we can assert contents.
        self._emit_warning = lambda message: self._warnings.append(message)

    def _compress_context(self, messages, *a, **k):
        self._compress_calls += 1
        return messages, "SYSTEM"


def _build_warn_agent(compressor: ContextCompressor) -> _WarnAgent:
    agent = _WarnAgent()
    agent.context_compressor = compressor
    return agent


def _run_build(agent):
    """Run build_turn_context with the prologue-side effects stubbed.

    The estimate is over the 72k threshold but under the 96k model window: the compression
    branch must run without tripping the over-window fail-closed (a no-progress pass on a request
    above the window ends the turn; that path is covered by
    ``tests/agent/test_over_window_compression_fail_closed.py``)."""
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None), \
         patch("agent.turn_context._should_run_preflight_estimate", return_value=True), \
         patch("agent.turn_context.estimate_request_tokens_rough", return_value=80_000):
        return build_turn_context(
            agent=agent,
            user_message="hello",
            system_message=None,
            conversation_history=None,
            task_id=None,
            stream_callback=None,
            persist_user_message=None,
            restore_or_build_system_prompt=lambda *a, **k: None,
            install_safe_stdio=lambda: None,
            sanitize_surrogates=lambda s: s,
            summarize_user_message_for_log=lambda s: s,
            set_session_context=lambda _sid: None,
            set_current_write_origin=lambda _o: None,
            ra=lambda: type("R", (), {"_set_interrupt": lambda *a, **k: None})(),
        )


class TestTurnContextOverflowWarning:
    def test_warns_on_cooldown_block(self):
        comp = _make_compressor()
        comp.last_prompt_tokens = comp.last_real_prompt_tokens = 73_000
        comp._summary_failure_cooldown_until = time.monotonic() + 30
        agent = _build_warn_agent(comp)
        _run_build(agent)
        assert len(agent._warnings) == 1
        assert "cooldown" in agent._warnings[0]






    def test_noop_compression_does_not_rearm_while_still_over_threshold(self):
        """A blocker clearing only permits an attempt; a no-op attempt does
        not end the existing pressure episode or make the same warning noisy.
        """
        comp = _make_compressor()
        comp.last_prompt_tokens = comp.last_real_prompt_tokens = 73_000
        comp._summary_failure_cooldown_until = time.monotonic() + 30
        agent = _build_warn_agent(comp)
        # Turn 1: over threshold + cooldown -> warn.
        _run_build(agent)
        assert len(agent._warnings) == 1
        # Turn 2: cooldown expires while STILL over threshold -> a no-op
        # compression attempt runs.
        comp._summary_failure_cooldown_until = 0.0
        _run_build(agent)
        assert agent._compress_calls > 0
        assert agent._last_ctx_overflow_warn == ("ctx_overflow_blocked", "cooldown")
        # Turn 3: cooldown re-arms; the same episode remains deduped.
        comp._summary_failure_cooldown_until = time.monotonic() + 30
        _run_build(agent)
        assert len(agent._warnings) == 1

    def test_repeated_compression_noops_do_not_rearm_blocked_warning(self):
        """Starting a compression pass is not recovery: if it no-ops while the
        same blocker remains, the existing warning episode must stay deduped.
        Only an explicit recovered edge re-arms it for a later episode.
        """
        emitted = []
        agent = _StatusAgent(lambda kind, message, category=None: emitted.append((kind, message, category)))

        agent._warn_context_overflow_blocked("cooldown:30", 73_000, 72_000)
        agent._clear_context_overflow_warn()  # a no-op compression attempt
        agent._warn_context_overflow_blocked("cooldown:29", 73_100, 72_000)

        assert len(emitted) == 1

        agent._clear_context_overflow_warn(recovered=True)
        agent._warn_context_overflow_blocked("cooldown:30", 73_200, 72_000)

        assert [(kind, category) for kind, _message, category in emitted] == [
            ("warn", "context_pressure"),
            ("context_pressure_recovered", "context_pressure"),
            ("warn", "context_pressure"),
        ]
        assert "not the model context limit" in emitted[0][1]

    def test_same_blocker_escalates_from_maintenance_to_context_cap_risk(self):
        """A cooldown card just over the maintenance threshold must be replaced
        when the same episode reaches the resolved context cap.  The hard-risk
        copy must not claim that a request at the cap can safely continue.
        """
        emitted = []
        agent = _StatusAgent(
            lambda kind, message, category=None: emitted.append((kind, message, category)),
            context_length=128_000,
        )

        agent._warn_context_overflow_blocked("cooldown:30", 72_001, 72_000)
        agent._warn_context_overflow_blocked("cooldown:29", 128_000, 72_000)
        agent._warn_context_overflow_blocked("cooldown:28", 128_100, 72_000)
        agent._warn_context_overflow_blocked("cooldown:27", 100_000, 72_000)

        assert [kind for kind, _message, _category in emitted] == ["warn", "warn"]
        assert "you can continue" in emitted[0][1]
        assert "model context limit" in emitted[1][1]
        assert "you can continue" not in emitted[1][1]

    def test_legacy_status_consumer_gets_one_plaintext_warning_per_episode(self):
        """Chat gateways (Matrix, Telegram, ...) consume ``status_callback(kind, text)``
        and post the text. They must see exactly the base behaviour: one ``warn`` post
        per episode, one more on escalation, never a recovery post and never a
        duplicate for the same episode."""
        posts = []
        agent = _StatusAgent(lambda kind, message: posts.append((kind, message)), context_length=128_000)

        agent._warn_context_overflow_blocked("cooldown:30", 73_000, 72_000)
        agent._warn_context_overflow_blocked("cooldown:29", 73_100, 72_000)
        agent._clear_context_overflow_warn()  # attempt, not recovery
        agent._warn_context_overflow_blocked("cooldown:28", 73_200, 72_000)
        assert [kind for kind, _ in posts] == ["warn"]

        agent._clear_context_overflow_warn(recovered=True)
        assert len(posts) == 1, "recovery is never an outgoing chat post"

        agent._warn_context_overflow_blocked("cooldown:30", 73_200, 72_000)
        assert len(posts) == 2, "a new episode after measured recovery warns again"

        agent._warn_context_overflow_blocked("cooldown:29", 128_000, 72_000)
        assert len(posts) == 3 and "model context limit" in posts[2][1]
        agent._warn_context_overflow_blocked("cooldown:28", 100_000, 72_000)
        assert len(posts) == 3, "severity never downgrades within an episode"
        assert {kind for kind, _ in posts} == {"warn"}

    def test_typed_status_consumer_keeps_generic_warnings_uncategorized(self):
        """The TUI gateway bridge accepts ``category``: context pressure rides on the
        legacy ``warn`` kind with its typed category, generic warnings carry none, and
        recovery is delivered only when a pressure episode is open."""
        events = []
        agent = _StatusAgent(lambda kind, message, category=None: events.append((kind, message, category)))

        agent._clear_context_overflow_warn(recovered=True)  # nothing open: silent
        agent._warn_context_overflow_blocked("cooldown:30", 73_000, 72_000)
        agent._emit_warning("⚠ The session lease expired.")
        agent._clear_context_overflow_warn(recovered=True)
        agent._clear_context_overflow_warn(recovered=True)  # already recovered: silent

        assert [(kind, category) for kind, _message, category in events] == [
            ("warn", "context_pressure"),
            ("warn", None),
            ("context_pressure_recovered", "context_pressure"),
        ]

    def test_no_warning_below_threshold_with_persisted_cooldown(self):
        """A live cooldown with the context BELOW threshold must not warn —
        there is no overflow to warn about (the cooldown branch is reached
        via the cheap preflight pre-check, which is not a threshold
        guarantee). An unanchored rough estimate is not recovery evidence,
        so an open episode stays armed."""
        comp = _make_compressor()
        comp.last_prompt_tokens = 10_000
        comp._summary_failure_cooldown_until = time.monotonic() + 30
        agent = _build_warn_agent(comp)
        agent._last_ctx_overflow_warn = ("ctx_overflow_blocked", "cooldown")
        with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None), \
             patch("agent.turn_context._should_run_preflight_estimate", return_value=True), \
             patch("agent.turn_context.estimate_request_tokens_rough", return_value=10_000):
            _build_turn(agent, conversation_history=None)
        assert agent._warnings == []
        assert agent._last_ctx_overflow_warn == ("ctx_overflow_blocked", "cooldown")

    def test_anchored_sub_threshold_measurement_recovers_turn_start_episode(self):
        """A provider usage anchor covering the transcript is a real measurement:
        below threshold it ends the open episode and re-arms the next one."""
        comp = _make_compressor()
        comp.last_prompt_tokens = 10_000
        comp._summary_failure_cooldown_until = time.monotonic() + 30
        agent = _build_warn_agent(comp)
        agent._last_ctx_overflow_warn = ("ctx_overflow_blocked", "cooldown")
        history = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        agent._usage_anchor = capture_usage_anchor(10_000, 20, history)
        with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None), \
             patch("agent.turn_context._should_run_preflight_estimate", return_value=True), \
             patch("agent.turn_context.estimate_request_tokens_rough", return_value=999_999):
            _build_turn(agent, conversation_history=history)
        assert agent._warnings == []
        assert agent._last_ctx_overflow_warn is None


def _build_turn(agent, *, conversation_history):
    return build_turn_context(
        agent=agent,
        user_message="hello",
        system_message=None,
        conversation_history=conversation_history,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: type("R", (), {"_set_interrupt": lambda *a, **k: None})(),
    )


class _StatusAgent(StatusOutputMixin):
    """The status spine alone, over a bare object: no AIAgent construction."""

    def __init__(self, status_callback, *, context_length=0):
        self.status_callback = status_callback
        self._last_ctx_overflow_warn = None
        self.suppress_status_output = True
        self.log_prefix = ""
        self._print_fn = None
        self.context_compressor = SimpleNamespace(context_length=context_length)

    def _touch_activity(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# Plugin context engines: backward-compatible should_compress_info default
# ---------------------------------------------------------------------------

class TestPluginEngineDefault:
    def test_abc_default_returns_tuple(self):
        """Engines that only implement should_compress() get the tuple shape
        for free from the ContextEngine base class — the call sites in
        turn_context.py / conversation_loop.py must not raise
        AttributeError on plugin engines (sweeper review, #62625)."""
        from tests.agent.test_plugin_context_engine_init import _StubEngine

        engine = _StubEngine()
        result = engine.should_compress_info(123_456)
        assert result == (False, None)

    def test_abc_default_delegates_to_should_compress(self):
        from agent.context_engine import ContextEngine

        class _TrueEngine(ContextEngine):
            @property
            def name(self):
                return "true-stub"

            def update_from_response(self, usage):
                pass

            def should_compress(self, prompt_tokens=None):
                return True

            def compress(self, messages, current_tokens=None, focus_topic=None):
                return messages

        assert _TrueEngine().should_compress_info(1) == (True, None)


# ---------------------------------------------------------------------------
# Gateway noise filter: the warning is FAILURE-CLASS and must survive
# ---------------------------------------------------------------------------

class TestWarningSurvivesNoiseFilter:
    """The blocked-overflow warning is a deliberate carve-out from
    routine-compression silence (#16775 class). The gateway noise regex
    (#69550 just widened it) must NOT swallow it, or the fix is dead on
    every chat platform. Executes the REAL compiled regex — never eyeball
    a regex (noise-regex salvage rule).
    """

    def _emitted_warning(self, reason: str) -> str:
        from agent.conversation_compression import (
            CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE,
        )

        return CONTEXT_OVERFLOW_BLOCKED_WARNING_TEMPLATE.format(
            tokens=85_000, threshold=72_000, reason=reason
        )

    def test_cooldown_warning_not_matched_by_noise_regex(self):
        from gateway.run import _TELEGRAM_NOISY_STATUS_RE

        assert not _TELEGRAM_NOISY_STATUS_RE.search(
            self._emitted_warning("cooldown:30")
        )


    def test_warning_delivered_on_chat_platform(self):
        """End-to-end through the fail-closed gateway status preparer."""
        from gateway.config import Platform
        from gateway.run import _prepare_gateway_status_message

        message = self._emitted_warning("cooldown:30")
        assert (
            _prepare_gateway_status_message(Platform.TELEGRAM, "warn", message)
            == message
        )
