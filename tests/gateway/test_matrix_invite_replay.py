"""Regression tests: historic m.room.member invite events replayed from a JOINED room's
state/timeline must not be treated as invites to act on.

mautrix's ``MembershipEventDispatcher`` fans out every ``m.room.member`` event whose
``membership`` is ``invite`` as ``InternalEventType.INVITE`` -- regardless of which sync
section carried it. The adapter connects with ``MemorySyncStore``, so every (re)connect is a
full initial sync and each joined room's recent timeline (server default: last 10 events)
is re-dispatched. Any invite event still inside that window -- the bot's own historic
invite, or an invite addressed to someone else -- re-fires ``_on_invite`` on every boot:
"rejecting invite ... from unauthorized user" when the inviter is not allow-listed,
"invited to ... joining" plus a no-op join task when it is.

The only invite the bot can act on is one the homeserver delivers in ``rooms.invite``
(``SyncStream.INVITED_ROOM``). These tests drive ``_on_invite`` with events stamped the way
``Client.dispatch_event`` stamps them (``event.source``).
"""

import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig

try:  # the wider gateway suite may stub mautrix out; the source-stamp tests need the real flag
    from mautrix.client import SyncStream
except ImportError:  # pragma: no cover
    SyncStream = None

needs_syncstream = pytest.mark.skipif(
    SyncStream is None, reason="real mautrix.client.SyncStream not importable"
)

BOT = "@hermes:example.org"
OWNER = "@owner:example.org"
STRANGER = "@stranger:example.org"
ROOM = "!room:example.org"


def _make_adapter(user_id=BOT):
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = MatrixAdapter(
        PlatformConfig(
            enabled=True,
            token="syt_test_token",
            extra={"homeserver": "https://matrix.example.org", "user_id": user_id},
        )
    )
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    adapter._startup_ts = time.time() - 10
    adapter._allowed_user_ids = {OWNER}
    adapter._join_room_by_id = AsyncMock(return_value=True)
    return adapter


def _invite(sender, state_key, source, room_id=ROOM):
    return SimpleNamespace(
        room_id=room_id,
        sender=sender,
        state_key=state_key,
        content=SimpleNamespace(is_direct=False, membership="invite"),
        source=source,
    )


async def _drain(adapter):
    for task in list(adapter._invite_join_tasks.values()):
        await task


def _records(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


REPLAY = (SyncStream.JOINED_ROOM | SyncStream.TIMELINE) if SyncStream else None
LIVE = (SyncStream.INVITED_ROOM | SyncStream.STATE) if SyncStream else None
REPLAY_STATE = (SyncStream.JOINED_ROOM | SyncStream.STATE) if SyncStream else None


@needs_syncstream
class TestReplayedInvitesAreIgnored:
    @pytest.mark.asyncio
    async def test_own_historic_invite_replayed_from_joined_room_is_quiet(self, caplog):
        """The bot's own old invite (unauthorized inviter) re-read on boot: no warning, no join."""
        adapter = _make_adapter()
        adapter._joined_rooms = {ROOM}
        with caplog.at_level(logging.DEBUG):
            await adapter._on_invite(_invite(STRANGER, BOT, REPLAY))
        assert adapter._invite_join_tasks == {}
        adapter._join_room_by_id.assert_not_awaited()
        assert _records(caplog, logging.WARNING) == []

    @pytest.mark.asyncio
    async def test_third_party_invite_replayed_from_joined_room_is_quiet(self, caplog):
        """Someone else's invite (authorized inviter) in the timeline: no 'joining' line, no task."""
        adapter = _make_adapter()
        adapter._joined_rooms = {ROOM}
        with caplog.at_level(logging.DEBUG):
            await adapter._on_invite(_invite(OWNER, "@third:example.org", REPLAY))
        assert adapter._invite_join_tasks == {}
        adapter._join_room_by_id.assert_not_awaited()
        assert _records(caplog, logging.INFO) == []

    @pytest.mark.asyncio
    async def test_source_alone_is_decisive(self, caplog):
        """Not joined, addressed to the bot, authorized inviter -- only the sync-stream stamp says
        this is history. It must still be ignored: a joined-room timeline can never carry a
        live invite for us."""
        adapter = _make_adapter()
        with caplog.at_level(logging.DEBUG):
            await adapter._on_invite(_invite(OWNER, BOT, REPLAY))
        assert adapter._invite_join_tasks == {}
        adapter._join_room_by_id.assert_not_awaited()
        assert _records(caplog, logging.INFO) == []

    @pytest.mark.asyncio
    async def test_state_section_replay_is_quiet_too(self, caplog):
        adapter = _make_adapter()
        adapter._joined_rooms = {ROOM}
        with caplog.at_level(logging.DEBUG):
            await adapter._on_invite(_invite(OWNER, "@third:example.org", REPLAY_STATE))
        assert adapter._invite_join_tasks == {}
        assert _records(caplog, logging.INFO) == []


@needs_syncstream
class TestLiveInvitesStillWork:
    @pytest.mark.asyncio
    async def test_live_invite_from_authorized_user_joins(self):
        adapter = _make_adapter()
        await adapter._on_invite(_invite(OWNER, BOT, LIVE))
        await _drain(adapter)
        adapter._join_room_by_id.assert_awaited_once_with(ROOM)

    @pytest.mark.asyncio
    async def test_live_invite_from_unauthorized_user_is_still_rejected(self, caplog):
        adapter = _make_adapter()
        with caplog.at_level(logging.DEBUG):
            await adapter._on_invite(_invite(STRANGER, BOT, LIVE))
        adapter._join_room_by_id.assert_not_awaited()
        assert any("rejecting invite" in m for m in _records(caplog, logging.WARNING))


class TestNoSourceCompat:
    """Events without a ``source`` stamp (manual dispatch, older callers) keep the old path,
    except that a room we already sit in is never re-joined. The state_key cases live in
    ``test_matrix_invite_state_key.py``."""

    @pytest.mark.asyncio
    async def test_unstamped_invite_to_self_joins(self):
        adapter = _make_adapter()
        await adapter._on_invite(
            SimpleNamespace(
                room_id=ROOM, sender=OWNER, content=SimpleNamespace(is_direct=False)
            )
        )
        await _drain(adapter)
        adapter._join_room_by_id.assert_awaited_once_with(ROOM)

    @pytest.mark.asyncio
    async def test_unstamped_invite_for_joined_room_is_quiet(self, caplog):
        adapter = _make_adapter()
        adapter._joined_rooms = {ROOM}
        with caplog.at_level(logging.DEBUG):
            await adapter._on_invite(
                SimpleNamespace(
                    room_id=ROOM,
                    sender=STRANGER,
                    content=SimpleNamespace(is_direct=False),
                )
            )
        assert adapter._invite_join_tasks == {}
        assert _records(caplog, logging.WARNING) == []
