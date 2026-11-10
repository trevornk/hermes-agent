"""Regression coverage for host-side external context-engine candidates."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.conversation_compression import _Attempt, _candidate_rejected
from hermes_state import SessionDB
from run_agent import AIAgent


def _agent_with_external_engine(db: SessionDB, engine: object) -> AIAgent:
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id="external-engine-no-progress",
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "system prompt"
    agent._use_prompt_caching = False
    agent.compression_enabled = True
    agent.compression_in_place = True
    agent.context_compressor = engine
    return agent


class _SameSizeExternalEngine:
    """A plugin-shaped engine that rewrites text but provides no token relief."""

    compression_count = 0
    _last_compress_aborted = False
    _last_summary_error = None
    _last_summary_fallback_used = False
    _last_summary_dropped_count = 0

    def __init__(self) -> None:
        self.rejected = 0

    def compress(self, messages, **_kwargs):
        candidate = [dict(message) for message in messages]
        candidate[2]["content"] = "answer"  # Same rough token cost as "reply".
        return candidate

    def record_rejected_compaction(self) -> None:
        self.rejected += 1


def test_host_rejects_external_rewrite_without_material_token_reduction(tmp_path):
    """An external engine cannot commit a semantic rewrite that leaves the host
    context at the same size; the original durable transcript remains authoritative
    and the engine receives one bounded-recovery signal."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("external-engine-no-progress", source="cli")
    engine = _SameSizeExternalEngine()
    agent = _agent_with_external_engine(db, engine)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest request"},
    ]

    returned, _prompt = agent._compress_context(messages, "system prompt", approx_tokens=100)

    # Content-level check only: persistence bookkeeping (timestamp/_row_id/_db_persisted)
    # is attached to messages independently of whether the rewrite was accepted, so a
    # full dict equality would conflate that plumbing with the behavior under test.
    assert [m["content"] for m in returned] == [m["content"] for m in messages]
    assert returned[2]["content"] == "reply"
    assert engine.rejected == 1
    assert db.get_messages("external-engine-no-progress") == []


def test_material_reduction_gate_does_not_override_builtin_candidate_semantics(tmp_path):
    """Only plugins receive the cross-engine token gate; built-in salvage keeps
    its established candidate rules even when rough estimates tie."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("builtin-same-budget", source="cli")
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id="builtin-same-budget",
            skip_context_files=True,
            skip_memory=True,
        )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]
    candidate = [dict(message) for message in messages]
    candidate[2]["content"] = "answer"

    assert _candidate_rejected(
        agent,
        candidate,
        messages,
        [dict(message) for message in messages],
        attempt=_Attempt(snapshot={}, generation=None, started_at=0.0, seed={}),
    ) is False
