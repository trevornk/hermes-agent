"""Preflight compression progress is judged on anchor-free estimates on BOTH sides.

``_compress_context`` clears the usage anchor, so the pre-pass figure used to be the provider's real
usage (~208,909) while the post-pass figure was the rough estimate (~148,677): a pass that returned
the same rows unchanged scored as a 29% cut, was committed, and the next request was still ~215k.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.turn_context_compaction import CompactionOutcome, _run_preflight_passes

ANCHORED = 208_909
ROUGH = 148_677


def _rows(tokens_each):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"row {i}", "t": tokens_each}
        for i in range(10)
    ]


def _rough(messages):
    return sum(m["t"] for m in messages)


def _run(compress):
    compressor = SimpleNamespace(
        protect_first_n=1, protect_last_n=1, threshold_tokens=150_000, context_length=1_000_000,
        summary_target_ratio=0.5, should_compress=lambda tokens: tokens >= 150_000,
    )
    agent = SimpleNamespace(
        context_compressor=compressor, session_id="s1", model="m", max_compression_attempts=3,
        _emit_status=MagicMock(), _compression_blocked_transient=None, tools=None,
        _usage_anchor={"anchored": True},
    )

    def _compress(msgs, system, **kw):
        agent._usage_anchor = None  # what _compress_context does via set_usage_anchor(agent, None)
        return compress(msgs), system

    agent._compress_context = _compress
    out = CompactionOutcome(
        messages=_rows(ROUGH // 10), active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=0,
    )
    committed = []
    with patch(
        "agent.turn_context.anchored_context_tokens",
        side_effect=lambda msgs, anchor: ANCHORED if anchor else None,
    ), patch(
        "agent.turn_context.estimate_request_tokens_rough", side_effect=lambda m, **kw: _rough(m)
    ), patch(
        "agent.turn_context_compaction.automatic_compaction_status_message", return_value=""
    ), patch(
        "agent.turn_context_compaction.conversation_history_after_compression",
        side_effect=lambda a, m, h: committed.append(m) or h,
    ):
        _run_preflight_passes(agent, out, compressor, ANCHORED, "sys", "t")
    return out, committed


def test_noop_pass_with_anchored_pre_estimate_is_not_progress():
    out, committed = _run(lambda msgs: [dict(m) for m in msgs])  # new list, identical rows
    assert out.blocked is True
    assert committed == []


def test_real_shrink_still_counts_as_progress_while_anchor_active():
    out, committed = _run(lambda msgs: [dict(m, t=m["t"] // 2) for m in msgs])  # same rows, half the tokens
    assert len(committed) == 1
    assert out.blocked is not True
