"""In-place compaction must supersede the originals of a carried tail whatever engine produced it.

``archive_and_compact`` re-inserts the carried-forward tail as new rows (same ``message_uid``). The built-in
compressor tags those dicts ``_compaction_tail`` so the commit flags the originals rewind-only
(``active=0, compacted=0``). A plugin context engine returns the same verbatim tail untagged: ``tail_count``
is 0, the originals stay ``compacted=1`` beside their live twins, and message-level search (which includes
``compacted=1`` rows without dedupe) returns each carried message once per compaction.

These drive the real ``compress_context`` -> ``_commit_compaction`` -> ``SessionDB.archive_and_compact`` path
with a stub engine, so only the tagging differs between the tagged and untagged cases.
"""

import copy
import os
from unittest.mock import patch

import pytest

from agent.conversation_compression import compress_context
from hermes_state import SessionDB

TAIL = 4


@pytest.fixture()
def db(tmp_path):
    state = SessionDB(db_path=tmp_path / "state.db")
    yield state
    state.close()


def _make_agent(db, sid, fake_compress):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
            quiet_mode=True, session_db=db, session_id=sid, skip_context_files=True, skip_memory=True,
        )
    agent.compression_in_place = True
    agent.context_compressor.compress = fake_compress
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    agent.context_compressor.compression_count = 1
    return agent


def _seed(db, sid, n=10):
    db.create_session(sid, "cli", model="test/model")
    for i in range(n):
        db.append_message(session_id=sid, role="user" if i % 2 == 0 else "assistant", content=f"turn {i}")


def _engine(*, tag: bool, mutate=None):
    """Stub engine: ``[summary] + the last TAIL held dicts`` as copies (a tail the engine did not rewrite)."""

    def compress(messages, current_tokens=None, focus_topic=None, force=False):
        tail = [copy.deepcopy(m) for m in messages[-TAIL:]]
        for m in tail:
            if tag:
                m["_compaction_tail"] = True
        if mutate is not None:
            mutate(tail)
        return [{"role": "user", "content": "[CONTEXT COMPACTION] summary of earlier turns"}, *tail]

    return compress


def _compact(db, sid, engine):
    held = db.get_messages_as_conversation(sid, include_row_ids=True)
    agent = _make_agent(db, sid, engine)
    compress_context(agent, held, approx_tokens=100_000, system_message="sys")
    return agent


def _rows(db, sid):
    return [dict(r) for r in db._conn.execute(
        "SELECT id, role, content, active, compacted, message_uid FROM messages WHERE session_id = ? ORDER BY id",
        (sid,)).fetchall()]


def _state(db, sid):
    """Normalised row state: (role, content, active, compacted) in insertion order, ids/uids dropped."""
    return [(r["role"], r["content"], r["active"], r["compacted"]) for r in _rows(db, sid)]


def _archived_beside_live_twin(db, sid):
    """Archived-searchable (compacted=1) rows that share a message_uid with a live row."""
    rows = _rows(db, sid)
    live = {r["message_uid"] for r in rows if r["active"] == 1}
    return [r for r in rows if r["active"] == 0 and r["compacted"] == 1 and r["message_uid"] in live]


def _hits(db, query):
    """Message-level search hits for the phrase (archived-searchable and live rows both count)."""
    return db.search_messages(f'"{query}"')


class TestUntaggedTailHelper:
    def test_selects_only_dicts_carried_from_held_by_uid(self):
        from agent.conversation_compression_archive import untagged_carried_tail

        held = [{"role": "user", "content": "a", "message_uid": "u1"}, {"role": "assistant", "content": "b", "message_uid": "u2"}]
        carried = {"role": "assistant", "content": "b", "message_uid": "u2"}
        summary = {"role": "user", "content": "[summary]"}
        foreign = {"role": "user", "content": "x", "message_uid": "u9"}
        assert untagged_carried_tail(held, [summary, carried, foreign, "not a dict"]) == [carried]

    def test_no_uid_on_either_side_selects_nothing(self):
        from agent.conversation_compression_archive import untagged_carried_tail

        held = [{"role": "user", "content": "a"}, {"role": "user", "content": "b", "message_uid": ""}]
        compressed = [{"role": "user", "content": "a"}, {"role": "user", "content": "b", "message_uid": ""}]
        assert untagged_carried_tail(held, compressed) == []
        assert untagged_carried_tail([], compressed) == []


class TestWithUntaggedTail:
    HELD = [{"role": "user", "content": "a", "message_uid": "u1"}]
    CARRY = {"role": "user", "content": "a", "message_uid": "u1"}

    def test_tagged_tail_returns_the_callers_carried_messages_untouched(self):
        from agent.conversation_compression_archive import with_untagged_tail

        anchor = [{"role": "assistant", "content": "reply"}]
        assert with_untagged_tail(anchor, self.HELD, [self.CARRY], True) is anchor
        assert with_untagged_tail(None, self.HELD, [self.CARRY], True) is None

    def test_untagged_tail_extends_the_callers_list_and_keeps_none_when_nothing_is_carried(self):
        from agent.conversation_compression_archive import with_untagged_tail

        anchor = {"role": "assistant", "content": "reply"}
        assert with_untagged_tail([anchor], self.HELD, [self.CARRY], False) == [anchor, self.CARRY]
        assert with_untagged_tail(None, self.HELD, [self.CARRY], False) == [self.CARRY]
        assert with_untagged_tail(None, self.HELD, [{"role": "user", "content": "x"}], False) is None


class TestUntaggedEngineTail:
    def test_originals_of_an_untagged_tail_become_rewind_only(self, db):
        sid = "untagged-tail"
        _seed(db, sid)
        _compact(db, sid, _engine(tag=False))

        assert _archived_beside_live_twin(db, sid) == []
        rows = _rows(db, sid)
        originals, twins = rows[:10], rows[11:]
        # The six turns the summary replaced stay archived-searchable; the four carried originals are superseded.
        assert [(r["content"], r["active"], r["compacted"]) for r in originals] == (
            [(f"turn {i}", 0, 1) for i in range(6)] + [(f"turn {i}", 0, 0) for i in range(6, 10)])
        assert [(r["content"], r["active"], r["compacted"]) for r in twins] == [(f"turn {i}", 1, 0) for i in range(6, 10)]
        # Live context unchanged: summary + the four carried turns, one row each.
        live = db.get_messages_as_conversation(sid)
        assert [m["content"] for m in live] == ["[CONTEXT COMPACTION] summary of earlier turns", *[f"turn {i}" for i in range(6, 10)]]
        assert db.get_session(sid)["message_count"] == 1 + TAIL

    def test_search_returns_each_carried_message_once(self, db):
        sid = "untagged-search"
        _seed(db, sid)
        _compact(db, sid, _engine(tag=False))
        assert len(_hits(db, "turn 8")) == 1  # carried tail: the live twin only
        assert len(_hits(db, "turn 2")) == 1  # summarized away: still discoverable once

    def test_untagged_end_state_equals_tagged_end_state(self, db, tmp_path):
        """The two engines hand the commit the same transcript; the stored result must not depend on tagging."""
        _seed(db, "u")
        _compact(db, "u", _engine(tag=False))
        other = SessionDB(db_path=tmp_path / "tagged.db")
        try:
            _seed(other, "t")
            _compact(other, "t", _engine(tag=True))
            assert _state(db, "u") == _state(other, "t")
        finally:
            other.close()

    def test_second_compaction_leaves_no_archived_twin_of_a_live_row(self, db):
        """The tail is carried again when fewer than TAIL new turns arrived: uid multiplicity of 3."""
        sid = "untagged-twice"
        _seed(db, sid)
        _compact(db, sid, _engine(tag=False))
        db.append_message(session_id=sid, role="user", content="turn 10")
        _compact(db, sid, _engine(tag=False))
        assert _archived_beside_live_twin(db, sid) == []
        live = db.get_messages_as_conversation(sid)
        assert len(live) == 1 + TAIL
        assert live[-1]["content"] == "turn 10"
        assert len(_hits(db, "turn 9")) == 1

    def test_tool_rows_in_the_tail_are_superseded_too(self, db):
        sid = "untagged-tools"
        db.create_session(sid, "cli", model="test/model")
        for i in range(4):
            db.append_message(session_id=sid, role="user" if i % 2 == 0 else "assistant", content=f"early {i}")
        db.append_message(session_id=sid, role="user", content="run it")
        db.append_message(
            session_id=sid, role="assistant", content=None,
            tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
        db.append_message(session_id=sid, role="tool", content="tool output", tool_call_id="call_1")
        db.append_message(session_id=sid, role="assistant", content="done")
        _compact(db, sid, _engine(tag=False))
        assert _archived_beside_live_twin(db, sid) == []
        carried = [r for r in _rows(db, sid)[:8] if r["role"] in ("tool",) or r["content"] in ("run it", "done")]
        assert carried and all((r["active"], r["compacted"]) == (0, 0) for r in carried)

    def test_rewind_still_works_after_an_untagged_compaction(self, db):
        sid = "untagged-rewind"
        _seed(db, sid)
        _compact(db, sid, _engine(tag=False))
        twin = next(r for r in _rows(db, sid) if r["content"] == "turn 8" and r["active"] == 1)
        result = db.rewind_to_message(sid, twin["id"])
        assert result["rewound_count"] == 2  # turn 8 and turn 9
        live = [m["content"] for m in db.get_messages_as_conversation(sid)]
        assert live == ["[CONTEXT COMPACTION] summary of earlier turns", "turn 6", "turn 7"]
        # Undone turns are hidden from search; the rewound originals did not come back.
        assert _hits(db, "turn 8") == [] and _hits(db, "turn 9") == []
        assert len(_hits(db, "turn 7")) == 1


class TestUntaggedTailNeverOverReaches:
    def test_rewritten_carry_keeps_its_original_archived(self, db):
        """Same uid and _row_id but different content is not a verbatim carry: its original is summarized history."""

        def rewrite(tail):
            tail[1]["content"] = "turn 7 (pruned by the engine)"

        sid = "untagged-rewritten"
        _seed(db, sid)
        _compact(db, sid, _engine(tag=False, mutate=rewrite))
        archived = {r["content"]: (r["active"], r["compacted"]) for r in _rows(db, sid)[:10]}
        assert archived["turn 7"] == (0, 1)  # original of the rewritten row: still discoverable
        assert {archived[f"turn {i}"] for i in (6, 8, 9)} == {(0, 0)}  # verbatim carries are superseded

    def test_summary_row_and_dropped_rows_are_not_flagged(self, db):
        sid = "untagged-dropped"
        _seed(db, sid)
        _compact(db, sid, _engine(tag=False))
        rows = _rows(db, sid)
        summary = next(r for r in rows if r["content"].startswith("[CONTEXT COMPACTION]"))
        assert (summary["active"], summary["compacted"]) == (1, 0)
        assert all((r["active"], r["compacted"]) == (0, 1) for r in rows[:6])  # rows the engine dropped

    def test_helper_is_not_consulted_when_the_engine_tagged_its_tail(self, db):
        sid = "tagged-unchanged"
        _seed(db, sid)
        with patch("agent.conversation_compression_archive.untagged_carried_tail", create=True,
                   side_effect=AssertionError("tagged tail must not take the fallback")):
            _compact(db, sid, _engine(tag=True))
        rows = _rows(db, sid)
        assert [(r["active"], r["compacted"]) for r in rows[:10]] == [(0, 1)] * 6 + [(0, 0)] * 4
        assert _archived_beside_live_twin(db, sid) == []

    def test_fallback_names_exactly_the_verbatim_carries_for_the_commit(self, db):
        """The commit is handed the four carried tail dicts, and not the summary row."""
        sid = "untagged-extends"
        _seed(db, sid)
        seen = {}
        real = db.archive_and_compact

        def spy(*args, **kwargs):
            seen["carried"] = kwargs.get("carried_messages")
            return real(*args, **kwargs)

        with patch.object(db, "archive_and_compact", side_effect=spy):
            _compact(db, sid, _engine(tag=False))
        assert [m["content"] for m in seen["carried"]] == [f"turn {i}" for i in range(6, 10)]

    def test_reply_anchor_carry_is_extended_not_replaced(self, db):
        """The core reinserts the dropped last reply as ``carried_messages=[reply]``; that reply has no uid, so
        only the caller's list names it. The untagged tail the engine kept is added to it, not substituted."""
        import agent.conversation_compression_reply_anchor as anchor

        def engine(messages, current_tokens=None, focus_topic=None, force=False):
            return [
                {"role": "user", "content": "[CONTEXT COMPACTION] summary of earlier turns"},
                *[copy.deepcopy(m) for m in messages[-3:-1]],  # turn 7, turn 8; the engine dropped turn 9
            ]

        def reinsert_reply(original_messages, compressed, *, session_id=None):
            reply = {k: v for k, v in copy.deepcopy(original_messages[-1]).items() if k != "message_uid"}
            compressed.append(reply)
            return reply

        sid = "untagged-anchor"
        _seed(db, sid)
        seen = {}
        real = db.archive_and_compact

        def spy(*args, **kwargs):
            seen["carried"] = [m["content"] for m in kwargs.get("carried_messages") or ()]
            return real(*args, **kwargs)

        with patch.object(anchor, "_ensure_compressed_keeps_last_assistant_reply", reinsert_reply), \
                patch.object(db, "archive_and_compact", side_effect=spy):
            _compact(db, sid, engine)
        assert sorted(seen["carried"]) == ["turn 7", "turn 8", "turn 9"]
        rows = _rows(db, sid)[:10]
        assert [(r["active"], r["compacted"]) for r in rows[7:]] == [(0, 0)] * 3  # turn 7, 8 and the anchored 9
        assert _archived_beside_live_twin(db, sid) == []

    def test_a_row_with_a_uid_the_engine_minted_is_not_named_as_carried(self, db):
        """Only a uid the compressor was HANDED marks a carry: a synthesized row (the summary, or an engine copy
        under a fresh uid) is not named, even when it duplicates a held message verbatim."""

        def engine(messages, current_tokens=None, focus_topic=None, force=False):
            replay_of_turn_2 = {**copy.deepcopy(messages[2]), "message_uid": "engine-minted-uid"}
            return [
                {"role": "user", "content": "[CONTEXT COMPACTION] summary", "message_uid": "engine-summary-uid"},
                replay_of_turn_2,
                *[copy.deepcopy(m) for m in messages[-2:]],
            ]

        sid = "untagged-foreign-uid"
        _seed(db, sid)
        seen = {}
        real = db.archive_and_compact

        def spy(*args, **kwargs):
            seen["carried"] = [m["content"] for m in kwargs.get("carried_messages") or ()]
            return real(*args, **kwargs)

        with patch.object(db, "archive_and_compact", side_effect=spy):
            _compact(db, sid, engine)
        assert seen["carried"] == ["turn 8", "turn 9"]
        rows = _rows(db, sid)[:10]
        assert (rows[2]["active"], rows[2]["compacted"]) == (0, 1)  # turn 2's original: summarized, not superseded
        assert [(r["active"], r["compacted"]) for r in rows[8:]] == [(0, 0)] * 2
