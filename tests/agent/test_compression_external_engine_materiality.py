"""An external-engine pass that does not materially shrink the request must not rewrite history.

Production evidence (session 20261108_134714_387e30): 72 active rows, but 36,223 superseded rows holding only
1,828 distinct message uids (the top uids 58-59 times each). Every committed in-place compaction superseded and
re-inserted the carried tail. Those passes were ``messages=N->N`` with a tiny or zero real reduction: the old
external-engine guard only refused ``candidate_tokens >= source_tokens``, so a 1-3% trim was committed.
The shared materiality rule (``compression_made_progress``: fewer rows, or a >5% token cut) now decides, priced
like-for-like with the system prompt and tools on both sides. Built-in engine behaviour is unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from agent.context_engine import ContextEngine
from hermes_state import SessionDB

N = 20
BODY = 400


class _ExternalEngine(ContextEngine):
    """A plugin-shaped engine (not a ContextCompressor) whose compress() is supplied per test."""

    name = "external-stub"  # type: ignore[assignment]

    def __init__(self, context_length: int, compress_fn) -> None:
        self.context_length = context_length
        self.threshold_tokens = int(context_length * 0.5)
        self._compress_fn = compress_fn
        self.calls = 0

    def update_from_response(self, usage: dict[str, Any]) -> None:
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        return True

    def compress(self, messages, current_tokens=None, focus_topic=None, force=False, memory_context=""):
        self.calls += 1
        return self._compress_fn(messages)


def _agent(db: SessionDB, session_id: str, compress_fn):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                        quiet_mode=True, session_db=db, session_id=session_id, skip_context_files=True,
                        skip_memory=True)
    agent._compression_feasibility_checked = True  # skip the one-time aux probe (network)
    agent.context_compressor = _ExternalEngine(agent.context_compressor.context_length, compress_fn)
    return agent


def _transcript() -> list[dict[str, Any]]:
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * BODY} for i in range(N)]


def _trim_one(percent: float):
    """Same row count; the leading messages lose ``percent`` of the transcript's characters between them."""
    def fn(msgs):
        out = copy.deepcopy(msgs)
        to_cut = int(sum(len(m["content"]) for m in out) * percent)
        for m in out:
            cut = min(to_cut, len(m["content"]) - 1)
            m["content"] = m["content"][: len(m["content"]) - cut]
            to_cut -= cut
            if to_cut <= 0:
                break
        return out
    return fn


def _drop_rows(count: int):
    def fn(msgs):
        return copy.deepcopy(msgs[count:])
    return fn


def _rows(db: SessionDB):
    return db._conn.execute("SELECT id, session_id, content, active, compacted FROM messages ORDER BY id").fetchall()


_BIG_TOOLS = [{"type": "function", "function": {"name": "t", "description": "d" * 80_000, "parameters": {}}}]


def _run(tmp_path: Path, caplog, compress_fn, tools=()):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "MATERIALITY"
    db.create_session(session_id, source="test")
    agent = _agent(db, session_id, compress_fn)
    # Explicit: the request price must not depend on the default toolset / system prompt.
    agent.tools, agent._cached_system_prompt = list(tools), "sys"
    messages = _transcript()
    agent._flush_messages_to_session_db(messages, [])
    rows_before = _rows(db)
    assert len(rows_before) == N
    before = copy.deepcopy(messages)
    with patch.object(db, "archive_and_compact", wraps=db.archive_and_compact) as archive, \
            caplog.at_level(logging.INFO):
        returned, _sp = agent._compress_context(messages, "sys", approx_tokens=120_000)
    records = [json.loads(r.getMessage()[len("context compression attempt telemetry: "):]) for r in caplog.records
               if r.getMessage().startswith("context compression attempt telemetry: ")]
    return db, agent, messages, before, returned, rows_before, archive, records


@pytest.mark.parametrize("percent", [0.01, 0.03])
def test_same_rows_with_a_tiny_trim_is_refused_before_any_persistence(tmp_path: Path, caplog, percent) -> None:
    db, agent, messages, before, returned, rows_before, archive, records = _run(
        tmp_path, caplog, _trim_one(percent))
    try:
        archive.assert_not_called()
        assert returned is messages and messages == before  # caller's transcript untouched
        assert _rows(db) == rows_before  # no supersede, no re-insert
        assert agent.session_id == "MATERIALITY"  # no rotation
        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.get_session("MATERIALITY")["end_reason"] is None
        assert records and records[0]["commit_status"] == "aborted"
        assert records[0].get("failure_class") == "no_material_reduction"
        assert not any("context compression done" in r.getMessage() for r in caplog.records)
    finally:
        agent.close()


def test_material_token_cut_with_the_same_rows_is_still_committed(tmp_path: Path, caplog) -> None:
    db, agent, messages, before, returned, rows_before, archive, records = _run(tmp_path, caplog, _trim_one(0.30))
    try:
        assert returned is not before and len(returned) == N
        assert records and records[0]["commit_status"] == "committed"
        assert any("context compression done" in r.getMessage() for r in caplog.records)
    finally:
        agent.close()


def test_fewer_rows_is_committed_even_with_a_tiny_token_cut(tmp_path: Path, caplog) -> None:
    db, agent, messages, before, returned, rows_before, archive, records = _run(tmp_path, caplog, _drop_rows(1))
    try:
        assert len(returned) == N - 1
        assert records and records[0]["commit_status"] == "committed"
    finally:
        agent.close()


def test_tool_schemas_are_priced_on_both_sides(tmp_path: Path, caplog) -> None:
    """A 30% transcript trim is material on its own but a ~1% wobble once ~20k tokens of tool schemas the
    provider also receives are counted, so the pass is refused."""
    db, agent, messages, before, returned, rows_before, archive, records = _run(
        tmp_path, caplog, _trim_one(0.30), tools=_BIG_TOOLS)
    try:
        archive.assert_not_called()
        assert _rows(db) == rows_before and messages == before
        assert records[0].get("failure_class") == "no_material_reduction"
    finally:
        agent.close()
