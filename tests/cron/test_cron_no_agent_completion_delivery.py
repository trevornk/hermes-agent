"""no_agent cron jobs deliver a display-only Bot Chat completion notice, never a
CLI-fallback or live-owner agent turn — that would re-run finished work as a prompt."""
from pathlib import Path
from unittest.mock import Mock

from cron import scheduler_delivery as delivery
from tools import bot_live_delivery as mailbox


def _make_bot_chat(home):
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session("bot", source="cli")
        db.set_session_title("bot", "Bot Chat")
    finally:
        db.close()
    return "bot"


def test_no_agent_bot_chat_completion_is_queued_without_a_live_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_bot_chat(tmp_path)
    # v0.22.4 retired the CLI-fallback lane; the agent-turn machinery left is the live-owner
    # admission and the deferred record, and a no_agent completion must reach neither.
    import cron.bot_chat_legacy as legacy
    deferred = Mock(side_effect=AssertionError("no_agent completion must not be deferred as an agent turn"))
    monkeypatch.setattr(legacy, "defer", deferred)
    discovery = Mock(side_effect=AssertionError("no_agent completion must not look up a live owner"))
    monkeypatch.setattr(mailbox, "find_canonical_live_owner", discovery)

    job = dict(id="scout", name="Scout", execution_id="run-1", no_agent=True)
    outcome = delivery._deliver_to_bot_chat(job, "finished", "")
    assert outcome is not None and "queued" in outcome

    records = list((tmp_path / "runtime/bot_live_delivery").glob("*.json"))
    assert len(records) == 1
    receipt = mailbox.read_delivery_result(tmp_path, records[0].stem)
    assert receipt["kind"] == "completion_notice"
    assert receipt["message"] == "Cronjob Response: Scout\n\nfinished"
    deferred.assert_not_called()
    discovery.assert_not_called()


def test_no_agent_bot_chat_result_records_queued_not_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_bot_chat(tmp_path)

    job = dict(id="scout", name="Scout", execution_id="run-2", no_agent=True)
    first = delivery._deliver_to_bot_chat(dict(job), "finished", "")
    assert first is not None and "queued" in first
    # Same execution_id + payload replays the receipt, not a second notice.
    second = delivery._deliver_to_bot_chat(dict(job), "finished", "")
    assert second == first
    assert len(list((tmp_path / "runtime/bot_live_delivery").glob("*.json"))) == 1


def test_no_agent_bot_chat_persisted_receipt_is_complete_not_renderer_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_bot_chat(tmp_path)

    job = dict(id="scout", name="Scout", execution_id="run-3", no_agent=True)
    pending = delivery._deliver_to_bot_chat(dict(job), "finished", "")
    assert pending is not None and "queued" in pending

    claimed = mailbox.claim_pending_completion_notice(tmp_path, "bot")
    assert claimed is not None
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.append_completion_notice("bot", claimed["message"], claimed["delivery_id"])
    finally:
        db.close()
    mailbox.complete_completion_notice(tmp_path, claimed["delivery_id"], claimed["claim_token"])

    settled = delivery._deliver_to_bot_chat(dict(job), "finished", "")
    assert settled is None


def test_no_agent_completion_notice_masks_secrets_in_output_and_job_name(tmp_path, monkeypatch):
    # The notice is delivered directly (never through bot_chat_message), so it must scrub the
    # job name and the output itself with the same fail-closed policy as every other outward lane.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_bot_chat(tmp_path)
    secret = "sk-" + "A" * 32

    job = dict(id="scout", name=f"Scout {secret}", execution_id="run-4", no_agent=True)
    outcome = delivery._deliver_to_bot_chat(job, f"finished {secret}", "")
    assert outcome is not None and "queued" in outcome

    records = list((tmp_path / "runtime/bot_live_delivery").glob("*.json"))
    assert len(records) == 1
    message = mailbox.read_delivery_result(tmp_path, records[0].stem)["message"]
    assert message.startswith("Cronjob Response: Scout ")
    assert secret not in message
    assert "finished" in message
