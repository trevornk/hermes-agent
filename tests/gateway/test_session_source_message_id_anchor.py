"""Regression: the resolved session source must carry the CURRENT event's
``message_id``, never a stale one left over from session resolution.

``_hmwa_resolve_session`` can return a ``source`` built from a persisted/cached
session origin (Telegram topic recovery, async-delegation route recovery,
restored synthetic-event sources) that carries a DIFFERENT, older
``message_id`` than the event actually being handled right now. If that stale
id survives into the turn, everything keyed off ``source.message_id`` --
reply-to anchors, the ``HERMES_SESSION_MESSAGE_ID`` env var threaded into
tools, deterministic per-turn ownership ids -- silently points at the wrong
message. On self-hosted Matrix (no Discord fallback) this is how a
context-limit warning, or any other reply, can land threaded onto an
unrelated older message instead of the one that triggered it.

The fix in ``gateway/run_turn.py::_handle_message_with_agent`` re-stamps the
resolved source with the inbound event's own ``message_id`` right after
resolution, before anything downstream reads it.
"""

import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource

SESSION_KEY = "agent:main:telegram:group:-1001:12345"


def _bootstrap(monkeypatch, tmp_path):
    """Minimal GatewayRunner setup (pattern from test_internal_notification_marker)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._is_user_authorized = lambda _source: True
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _gen: True
    runner._begin_session_run_generation = lambda _key: 1
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=SESSION_KEY,
        session_id="sess-msgid",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )
    return runner


def _stale_source():
    """A source with a message_id left over from a PRIOR event (e.g. a persisted
    origin, or the id async-delegation route recovery re-used from the batch
    completion that spawned this run)."""
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
        message_id="msg-STALE-OLD",
    )


def _event():
    return MessageEvent(
        text="hello world",
        source=_stale_source(),
        message_id="msg-CURRENT",
        internal=False,
    )


@pytest.mark.asyncio
async def test_resolved_source_gets_current_event_message_id_not_stale_one(
    monkeypatch, tmp_path,
):
    runner = _bootstrap(monkeypatch, tmp_path)
    # _hmwa_resolve_session hands back a source still carrying the STALE id
    # (as it would after topic recovery / a restored persisted origin).
    runner._hmwa_resolve_session = AsyncMock(
        return_value=(_stale_source(), runner.session_store.get_or_create_session.return_value, SESSION_KEY)
    )

    captured = {}

    async def _fake_prepare_turn(event, source, session_entry, session_key, quick_key, run_generation):
        captured["source"] = source
        return None, []

    runner._hmwa_prepare_turn = _fake_prepare_turn

    await runner._handle_message_with_agent(_event(), _stale_source(), SESSION_KEY, 1)

    assert "source" in captured, "prepare_turn was never reached"
    assert captured["source"].message_id == "msg-CURRENT"
    assert captured["source"].message_id != "msg-STALE-OLD"


@pytest.mark.asyncio
async def test_resolved_source_message_id_flows_into_session_env(monkeypatch, tmp_path):
    """End-to-end through _set_session_env: the env var used by tools/anchors
    must reflect the current event, not whatever _hmwa_resolve_session echoed
    back."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner._hmwa_resolve_session = AsyncMock(
        return_value=(_stale_source(), runner.session_store.get_or_create_session.return_value, SESSION_KEY)
    )

    captured_env = {}
    real_set_session_env = runner._set_session_env

    def _spy_set_session_env(context):
        captured_env["message_id"] = context.source.message_id
        return real_set_session_env(context)

    runner._set_session_env = _spy_set_session_env
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ack",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    await runner._handle_message_with_agent(_event(), _stale_source(), SESSION_KEY, 1)

    assert captured_env.get("message_id") == "msg-CURRENT"
