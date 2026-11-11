"""A content-identical compaction (``messages=N->N``) is a no-op: it must not touch state.db.

Incident 2026-11-10: a stuck session held 37,652 message rows for 94 active messages. This runs a no-op pass
through the real host funnel (``_compress_context``) against a real temp SessionDB and asserts that nothing is
persisted, rotated or rewritten, and that the pass is reported as no-progress rather than a success.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_state import SessionDB


def _agent(db: SessionDB, session_id: str):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                        quiet_mode=True, session_db=db, session_id=session_id, skip_context_files=True,
                        skip_memory=True)
    agent._compression_feasibility_checked = True  # skip the one-time aux probe (network)
    return agent


def _same_object(msgs, **_kw):
    return msgs


def _equal_copy(msgs, **_kw):
    return copy.deepcopy(msgs)


@pytest.mark.parametrize("engine", [_same_object, _equal_copy], ids=["same_list", "equal_copy"])
@pytest.mark.parametrize("passes", [1, 3])
def test_noop_compaction_persists_nothing_and_rotates_nothing(tmp_path: Path, caplog, engine, passes) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "NOOP_PASS"
    db.create_session(session_id, source="test")
    agent = _agent(db, session_id)
    messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 200} for i in range(20)]
    try:
        agent._flush_messages_to_session_db(messages, [])
        rows_before = db._conn.execute("SELECT id, session_id, content FROM messages ORDER BY id").fetchall()
        assert len(rows_before) == 20
        transcript_before = copy.deepcopy(messages)
        sp_before = agent._cached_system_prompt if hasattr(agent, "_cached_system_prompt") else None

        with patch.object(type(agent.context_compressor), "compress", side_effect=engine), patch.object(
            db, "archive_and_compact", wraps=db.archive_and_compact
        ) as archive_and_compact, caplog.at_level(logging.INFO):
            for _ in range(passes):
                returned, _sp = agent._compress_context(messages, "sys", approx_tokens=120_000)
                agent._flush_messages_to_session_db(returned, [])  # the turn-end flush that follows in production
                assert returned is messages  # the caller's own transcript object comes back unchanged

        archive_and_compact.assert_not_called()
        assert messages == transcript_before
        assert agent.session_id == session_id  # no rotation / new generation
        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db.get_session(session_id)["end_reason"] is None
        assert db._conn.execute("SELECT id, session_id, content FROM messages ORDER BY id").fetchall() == rows_before
        assert db.message_count() == 20
        text = [r.getMessage() for r in caplog.records]
        assert not any("context compression done" in m for m in text)  # not reported as a success
        prefix = "context compression attempt telemetry: "
        records = [json.loads(m[len(prefix):]) for m in text if m.startswith(prefix)]
        # First pass: judged a no-op and aborted before any commit; the structural backoff it arms then blocks
        # the repeats outright.
        assert records[0]["commit_status"] == "aborted" and records[0].get("failure_class") == "no_progress"
        assert all(r["commit_status"] in {"aborted", "blocked"} for r in records)
        assert sp_before is None or agent._cached_system_prompt == sp_before
    finally:
        agent.close()
