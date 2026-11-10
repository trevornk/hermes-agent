"""Regression tests: an unauthorized/dropped inbound message must never fire the
platform-adapter processing-lifecycle hooks (``on_processing_start`` / ``on_processing_complete``).

Before this fix, ``BasePlatformAdapter.handle_message`` spawned ``_process_message_background``
unconditionally, and only the RUNNER's ``_hm_admit_event`` (called from inside the spawned
background task, via the installed ``_message_handler``) rejected an unauthorized sender —
AFTER ``on_processing_start`` had already fired and put a 👀 reaction on the message. The handler
then returned ``None``, which ``_process_message_background`` classifies as
``ProcessingOutcome.SUCCESS`` (empty response == "nothing to say", not "rejected"), so
``on_processing_complete`` swapped the 👀 for a ✅ — reporting a dropped, unauthorized message as
a successfully completed turn.

The fix adds ``BasePlatformAdapter._precheck_hook_suppression``, called from ``handle_message``
before the background task spawns: it peeks at the SAME authorization callback the runner wires
via ``set_authorization_check`` and, on a confirmed rejection, stamps ``event._hooks_suppressed``.
``_run_processing_hook`` (the single call site both hooks go through) then no-ops for that event.
"""

from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


class _HookRecordingAdapter(BasePlatformAdapter):
    """Base-loop adapter (real ``handle_message`` / ``_process_message_background``) that
    records every processing-lifecycle hook invocation it actually receives."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.MATRIX)
        self.hooks: list = []
        self.handler_calls: list = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="msg-1")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.hooks.append(("start", event.message_id))

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        self.hooks.append(("complete", event.message_id, outcome))


def _make_event(user_id: str = "@stranger:example.org", message_id: str = "m1") -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.MATRIX, chat_id="!room:example.org", chat_type="dm", user_id=user_id,
        ),
        message_id=message_id,
    )


async def _drain(adapter: _HookRecordingAdapter, session_key: str) -> None:
    """Await the background task ``handle_message`` spawned for ``session_key``."""
    task = adapter._session_tasks.get(session_key)
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_unauthorized_sender_gets_no_reactions_at_all():
    """A confirmed-unauthorized sender must fire NEITHER on_processing_start NOR
    on_processing_complete — the reaction pair the WINTOPPERS incident leaked."""
    adapter = _HookRecordingAdapter()

    async def rejecting_handler(_event):
        adapter.handler_calls.append(_event.message_id)

    adapter.set_message_handler(rejecting_handler)
    adapter.set_authorization_check(lambda user_id, chat_type=None, chat_id=None, **_: False)

    event = _make_event()
    from gateway.session import build_session_key
    session_key = build_session_key(event.source)

    await adapter.handle_message(event)
    await _drain(adapter, session_key)

    assert adapter.hooks == [], (
        "an unauthorized sender must produce zero processing-lifecycle hook calls, "
        f"got {adapter.hooks!r}"
    )
    # The handler itself still runs (the real gateway's own admission gate is the actual
    # authority; the adapter-level peek only suppresses the cosmetic reaction hooks).
    assert adapter.handler_calls == ["m1"]


@pytest.mark.asyncio
async def test_authorized_sender_still_gets_start_then_success():
    """Sanity/non-regression: an authorized sender's normal 👀 -> ✅ pair must still fire."""
    adapter = _HookRecordingAdapter()

    async def accepting_handler(_event):
        adapter.handler_calls.append(_event.message_id)
        return "hi there"

    adapter.set_message_handler(accepting_handler)
    adapter.set_authorization_check(lambda user_id, chat_type=None, chat_id=None, **_: True)

    event = _make_event(user_id="@trevor:sppun.com")
    from gateway.session import build_session_key
    session_key = build_session_key(event.source)

    await adapter.handle_message(event)
    await _drain(adapter, session_key)

    assert [h[0] for h in adapter.hooks] == ["start", "complete"]
    assert adapter.hooks[1][2] == ProcessingOutcome.SUCCESS


@pytest.mark.asyncio
async def test_unknown_authorization_result_does_not_suppress():
    """No authorization check wired at all (bare adapters, many existing tests) must behave
    exactly as before the fix: hooks fire normally. ``None`` ("unknown") must never be treated
    as a rejection."""
    adapter = _HookRecordingAdapter()

    async def accepting_handler(_event):
        return "hi there"

    adapter.set_message_handler(accepting_handler)
    # No set_authorization_check call at all -> _authorization_check stays None -> "unknown".

    event = _make_event()
    from gateway.session import build_session_key
    session_key = build_session_key(event.source)

    await adapter.handle_message(event)
    await _drain(adapter, session_key)

    assert [h[0] for h in adapter.hooks] == ["start", "complete"]
