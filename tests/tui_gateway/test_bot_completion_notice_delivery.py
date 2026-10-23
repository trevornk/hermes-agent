"""Completion notices persist to state.db and repaint without ever claiming a turn."""
import threading

import pytest

from tui_gateway.method_ctx import rebind
from tui_gateway import session_notifications


def test_completion_notice_persists_without_a_turn_when_the_transport_is_detached(tmp_path):
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(tmp_path / "state.db")
    db.create_session("bot", source="cli")
    db.set_session_title("bot", "Bot Chat")
    db.close()

    queued = mailbox.deliver_completion_notice(
        tmp_path, "Cronjob Response: Scout\n\nfinished", delivery_id="a" * 32)

    emitted = []
    poll = rebind(session_notifications._poll_bot_completion_notice_once, {
        "_session_home": lambda session: tmp_path,
        "_emit": lambda event, sid, payload: emitted.append((event, sid, payload)),
        "logger": __import__("logging").getLogger("test"),
    })
    session = {"history_lock": threading.RLock(), "session_key": "bot"}

    assert poll("live", session) is True
    assert mailbox.read_delivery_result(tmp_path, queued["delivery_id"])["status"] == "persisted"
    assert emitted == [("cron.completion", "live",
                        {"text": "Cronjob Response: Scout\n\nfinished", "id": queued["delivery_id"]})]

    db2 = SessionDB(tmp_path / "state.db")
    try:
        _, display = db2.get_resume_conversations("bot")
    finally:
        db2.close()
    assert sum(1 for m in display if m.get("content") == "Cronjob Response: Scout\n\nfinished") == 1

    # A second poll finds nothing left to claim; no duplicate turn, no duplicate row.
    assert poll("live", session) is False
    assert len(emitted) == 1


def test_completion_notice_reopens_without_acquiring_a_turn_lease(tmp_path):
    """Unlike _poll_bot_live_delivery_once, this poller never checks running/lease state:
    the whole point is that reopening Bot Chat surfaces the result before any prompt."""
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox

    db = SessionDB(tmp_path / "state.db")
    db.create_session("bot", source="cli")
    db.set_session_title("bot", "Bot Chat")
    db.close()

    mailbox.deliver_completion_notice(tmp_path, "finished", delivery_id="e" * 32)

    poll = rebind(session_notifications._poll_bot_completion_notice_once, {
        "_session_home": lambda session: tmp_path,
        "_emit": lambda *a, **k: None,
        "logger": __import__("logging").getLogger("test"),
    })
    # No "active_session_lease", no "agent" — a live-owner poll would bail immediately.
    session = {"history_lock": threading.RLock(), "session_key": "bot"}
    assert poll("live", session) is True


def test_completion_notice_poll_skips_a_closing_session(tmp_path):
    from tools import bot_live_delivery as mailbox
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("bot", source="cli")
    db.set_session_title("bot", "Bot Chat")
    db.close()
    mailbox.deliver_completion_notice(tmp_path, "finished", delivery_id="f" * 32)

    poll = rebind(session_notifications._poll_bot_completion_notice_once, {
        "_session_home": lambda session: tmp_path,
        "_emit": lambda *a, **k: None,
        "logger": __import__("logging").getLogger("test"),
    })
    session = {"history_lock": threading.RLock(), "session_key": "bot", "_closing": True}
    assert poll("live", session) is False
    assert mailbox.read_delivery_result(tmp_path, "f" * 32)["status"] == "queued"
