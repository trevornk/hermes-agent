"""Completion-notice mailbox invariants: no_agent cron output is a durable Bot Chat
row, never a live-owner turn. Real disk, no mocks of the mailbox itself."""
import pytest


def _make_bot_chat(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        session_id = "bot"
        db.create_session(session_id, "cli")
        db.set_session_title(session_id, "Bot Chat")
    finally:
        db.close()
    return session_id


def test_completion_notice_binds_only_to_the_canonical_bot_chat_session(tmp_path):
    from tools import bot_live_delivery as mailbox

    bot = _make_bot_chat(tmp_path)
    queued = mailbox.deliver_completion_notice(
        tmp_path, "Cronjob Response: scout completed", delivery_id="c" * 32)
    assert queued["kind"] == "completion_notice"
    assert queued["target"] == {"profile_home": str(tmp_path.resolve()), "session_id": bot}
    # Same delivery_id + same payload is idempotent, not a duplicate queue entry.
    assert mailbox.deliver_completion_notice(
        tmp_path, "Cronjob Response: scout completed", delivery_id="c" * 32) == queued
    with pytest.raises(ValueError):
        mailbox.deliver_completion_notice(tmp_path, "different text", delivery_id="c" * 32)
    # A session that is not Bot Chat's lineage cannot claim it.
    assert mailbox.claim_pending_completion_notice(tmp_path, "some-other-session") is None
    claimed = mailbox.claim_pending_completion_notice(tmp_path, bot)
    assert claimed["status"] == "claimed" and claimed["message"] == queued["message"]


def test_completion_notice_claim_recovers_after_consumer_crash_without_duplicate_transcript_row(tmp_path):
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    bot = _make_bot_chat(tmp_path)
    queued = mailbox.deliver_completion_notice(tmp_path, "Cronjob Response: scout finished", delivery_id="f" * 32)

    first = mailbox.claim_pending_completion_notice(tmp_path, bot)
    assert first is not None
    # Consumer "crashes" here without ever calling complete_completion_notice.
    # Unlike an agent-turn delivery, this claim must be reclaimable: the state.db
    # append below is what makes it safe (idempotent by delivery_id).
    retried = mailbox.claim_pending_completion_notice(tmp_path, bot)
    assert retried is not None and retried["claim_token"] != first["claim_token"]

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        first_row = db.append_completion_notice(bot, queued["message"], queued["delivery_id"])
        second_row = db.append_completion_notice(bot, queued["message"], queued["delivery_id"])
    finally:
        db.close()
    assert first_row == second_row  # idempotent append: no duplicate transcript row

    persisted = mailbox.complete_completion_notice(tmp_path, queued["delivery_id"], retried["claim_token"])
    assert persisted["status"] == "persisted"
    # The stale first claim_token can no longer complete it (already terminal, but
    # a fresh, never-completed delivery_id with a stale token must be rejected).
    other = mailbox.deliver_completion_notice(tmp_path, "second", delivery_id="d" * 32)
    mailbox.claim_pending_completion_notice(tmp_path, bot)
    with pytest.raises(RuntimeError):
        mailbox.complete_completion_notice(tmp_path, other["delivery_id"], "stale-token")


def test_completion_notice_never_creates_bot_chat(tmp_path):
    from tools import bot_live_delivery as mailbox

    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    with pytest.raises(RuntimeError):
        mailbox.deliver_completion_notice(tmp_path, "no bot chat yet", delivery_id="e" * 32)
