"""Real-token no-progress circuit breaker for automatic compression.

carry-marker: compression-real-progress-breaker

Why: a session sat above its compaction trigger while every pass was a no-op (``messages=N->N``), so each API
call compacted again (188 passes, 30-70 s each). Whatever the cause, the host can see the symptom: the REAL
input tokens of the next provider call did not drop. Two consecutive such passes arm
``compression_suppression`` (reason ``no_real_progress``, session scope); it is lifted when the real input has
grown by one leaf chunk above the arming baseline, or the engine reports more summary nodes.

Real input = ``prompt_tokens`` of the last provider response (input + cache read + cache write), the figure
``turn_usage.record_response_usage`` feeds the usage anchor and ``context_compressor.last_prompt_tokens``
(stored as ``agent._last_prompt_size_tokens``). Engine hooks are duck-typed and optional:
``summary_node_count`` (int or callable) and ``leaf_chunk_tokens`` (on the engine or its ``config``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from agent import compression_suppression as suppression

logger = logging.getLogger(__name__)

MARGIN = 0.10
STRIKES_TO_ARM = 2
DEFAULT_LEAF_CHUNK_TOKENS = 20_000
_STATE_ATTR = "_compression_progress_breaker"


@dataclass
class _State:
    pending_before: Optional[int] = None  # real input of the call before an un-judged automatic pass
    strikes: int = 0
    baseline: Optional[int] = None  # real input when armed; None while not armed
    nodes_at_arm: Optional[int] = None


def _state(agent: Any) -> _State:
    return vars(agent).setdefault(_STATE_ATTR, _State())


def _positive_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _summary_nodes(agent: Any) -> Optional[int]:
    value = getattr(getattr(agent, "context_compressor", None), "summary_node_count", None)
    if callable(value):
        value = value()
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _leaf_chunk_tokens(agent: Any) -> int:
    engine = getattr(agent, "context_compressor", None)
    for holder in (engine, getattr(engine, "config", None)):
        chunk = _positive_int(getattr(holder, "leaf_chunk_tokens", None))
        if chunk:
            return chunk
    return DEFAULT_LEAF_CHUNK_TOKENS


def note_automatic_compaction(agent: Any, *, force: bool, bypass_cooldown: bool) -> None:
    """Called by the compression funnel after a pass that actually ran: remember the real input it must lower."""
    from agent.conversation_compression import (
        compression_blocked_transiently, compression_skipped_due_to_lock, context_compression_timed_out,
    )

    if force or bypass_cooldown or compression_blocked_transiently(agent) or compression_skipped_due_to_lock(agent):
        return
    if context_compression_timed_out(agent):
        return
    state = _state(agent)
    before = _positive_int(getattr(agent, "_last_prompt_size_tokens", None))
    if state.pending_before is None and before is not None:
        state.pending_before = before  # back-to-back passes with no call between: judge against the older call


def observe_real_usage(agent: Any, real_tokens: int) -> None:
    """Called with the real input tokens of each provider response: judge a pending pass, or test the lift."""
    state = _state(agent)
    session = getattr(agent, "session_id", None) or "none"
    if state.baseline is not None:
        nodes = _summary_nodes(agent)
        grew = real_tokens >= state.baseline + _leaf_chunk_tokens(agent)
        new_node = nodes is not None and state.nodes_at_arm is not None and nodes > state.nodes_at_arm
        state.pending_before = None
        if grew or new_node:
            logger.warning(
                "Compression no-progress breaker lifted (session=%s, real_input=%d, baseline=%d, summary_nodes=%s->%s)",
                session, real_tokens, state.baseline, state.nodes_at_arm, nodes,
            )
            suppression.clear(agent, suppression.REASON_NO_REAL_PROGRESS)
            state.baseline = state.nodes_at_arm = None
            state.strikes = 0
        return
    before, state.pending_before = state.pending_before, None
    if before is None:
        return
    if real_tokens <= before * (1 - MARGIN):
        state.strikes = 0
        return
    state.strikes += 1
    if state.strikes >= STRIKES_TO_ARM:
        state.baseline, state.nodes_at_arm = real_tokens, _summary_nodes(agent)
        logger.warning(
            "Compression is not lowering real input tokens (session=%s, real_before=%d, real_after=%d, strikes=%d): "
            "suppressing automatic compression (no_real_progress) until input grows by a leaf chunk",
            session, before, real_tokens, state.strikes,
        )
        suppression.arm(agent, suppression.REASON_NO_REAL_PROGRESS, scope="session")


def reset(agent: Any) -> None:
    """A real new session (``/new``, ``/resume``, ``/branch``): forget strikes and lift the suppression."""
    if vars(agent).pop(_STATE_ATTR, None) is not None:
        suppression.clear(agent, suppression.REASON_NO_REAL_PROGRESS)
