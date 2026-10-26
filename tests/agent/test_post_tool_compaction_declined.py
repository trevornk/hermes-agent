"""Declined message-aware maintenance is not an exhausted compression budget."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.turn_preflight import compress_after_tool_results


@pytest.mark.parametrize("attempts", [0, 1])
def test_declined_maintenance_does_not_claim_exhausted_budget(attempts):
    compressor = SimpleNamespace(
        last_prompt_tokens=80408, threshold_tokens=64801, context_length=272000,
        should_compress=lambda tokens: True,
        should_compress_request_pressure=Mock(return_value=False),
        should_compress_info=lambda tokens: (True, None),
    )
    agent = SimpleNamespace(
        compression_enabled=True, context_compressor=compressor,
        _compression_feasibility_checked=True, _usage_anchor=None,
        _warn_context_overflow_blocked=Mock(), _clear_context_overflow_warn=Mock(),
    )
    messages = [{"role": "user", "content": "continue"}]
    verdict = compress_after_tool_results(
        agent, messages=messages, system_message="system", user_message="continue",
        active_system_prompt="system", conversation_history=[],
        compression_attempts=attempts, max_compression_attempts=3,
        effective_task_id=None, final_response="", turn_exit_reason=None,
        current_turn_user_idx=0,
    )
    assert verdict.messages is messages
    assert verdict.compression_attempts == attempts
    compressor.should_compress_request_pressure.assert_called_once_with(messages, 80408)
    reasons = [call.args[0] for call in agent._warn_context_overflow_blocked.call_args_list]
    assert all(not reason.startswith("attempts_exhausted") for reason in reasons)


@pytest.mark.parametrize("attempts,reason,expected", [
    (3, None, "attempts_exhausted:3"),
    (0, "cooldown:60", "cooldown:60"),
    (1, "ineffective:2", "ineffective:2"),
])
def test_actual_exhaustion_and_engine_blockers_still_warn(attempts, reason, expected):
    compressor = SimpleNamespace(
        last_prompt_tokens=80408, threshold_tokens=64801, context_length=272000,
        should_compress=lambda tokens: reason is None,
        should_compress_info=lambda tokens: (reason is None, reason),
    )
    agent = SimpleNamespace(
        compression_enabled=True, context_compressor=compressor,
        _compression_feasibility_checked=True, _usage_anchor=None,
        _warn_context_overflow_blocked=Mock(), _clear_context_overflow_warn=Mock(),
    )
    compress_after_tool_results(
        agent, messages=[{"role": "user", "content": "continue"}],
        system_message="system", user_message="continue", active_system_prompt="system",
        conversation_history=[], compression_attempts=attempts, max_compression_attempts=3,
        effective_task_id=None, final_response="", turn_exit_reason=None, current_turn_user_idx=0,
    )
    agent._warn_context_overflow_blocked.assert_called_once_with(expected, 80408, 64801)
