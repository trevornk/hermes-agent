"""Cron completion notices are a durable display-only Bot Chat row: they persist,
dedupe by delivery_id, and never re-enter model replay or the next model prompt."""
from hermes_state import SessionDB


def test_completion_notice_is_idempotent_by_delivery_id(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("bot", source="cli")
    db.append_message("bot", "user", "hi", timestamp=1.0)

    first = db.append_completion_notice("bot", "Cronjob Response: scout finished", "deliv-1")
    second = db.append_completion_notice("bot", "Cronjob Response: scout finished", "deliv-1")
    assert first == second

    _, display = db.get_resume_conversations("bot")
    notice_rows = [m for m in display if m.get("content") == "Cronjob Response: scout finished"]
    assert len(notice_rows) == 1


def test_completion_notice_is_excluded_from_model_replay_but_kept_in_display(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("bot", source="cli")
    db.append_message("bot", "user", "hi", timestamp=1.0)
    db.append_completion_notice("bot", "Cronjob Response: scout finished", "deliv-2")
    db.append_message("bot", "assistant", "ok, noted", timestamp=3.0)

    model_history, display_history = db.get_resume_conversations("bot")
    assert not any(m.get("content") == "Cronjob Response: scout finished" for m in model_history)
    assert any(m.get("content") == "Cronjob Response: scout finished" for m in display_history)

    replay = db.get_messages_as_conversation("bot", repair_alternation=True)
    assert not any(m.get("content") == "Cronjob Response: scout finished" for m in replay)
    full = db.get_messages_as_conversation("bot", repair_alternation=False)
    assert any(m.get("content") == "Cronjob Response: scout finished" for m in full)
