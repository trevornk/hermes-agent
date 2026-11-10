"""Tests for Matrix DM room recording on invite (issue #44679).

When the bot's Matrix account has no ``m.direct`` account data (common for
accounts created solely for Hermes), DM rooms are silently treated as groups.
This tests the fix that records DM rooms in ``m.direct`` when the invite
event carries ``is_direct: true``.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig


def _make_adapter(tmp_path=None):
    """Create a MatrixAdapter with mocked config."""
    from plugins.platforms.matrix.adapter import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@hermes:example.org",
        },
    )
    adapter = MatrixAdapter(config)
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    adapter._startup_ts = time.time() - 10
    # Authorize the inviter used throughout this module so the invite-auth
    # gate in _on_invite (rejects auto-joins from non-allow-listed users)
    # lets the join through and the DM-recording side effects are exercised.
    adapter._allowed_user_ids = {"@alice:example.org"}
    return adapter


def _make_invite_event(
    room_id="!dm_room:example.org",
    sender="@alice:example.org",
    is_direct=True,
    state_key=None,
):
    """Create a fake membership invite event.

    ``state_key`` on an ``m.room.member`` event is the *invitee*, not the
    sender. It therefore defaults to the bot's own MXID (the account
    ``_make_adapter`` configures), matching the shape of a real invite
    directed at us. Tests covering the bridge fan-out case pass an explicit
    foreign ``state_key``.
    """
    content = SimpleNamespace(is_direct=is_direct)
    return SimpleNamespace(
        room_id=room_id,
        sender=sender,
        state_key=state_key if state_key is not None else "@hermes:example.org",
        content=content,
    )


# ---------------------------------------------------------------------------
# _schedule_pending_invite_joins DM recording
# ---------------------------------------------------------------------------


def _member_invite_event(
    state_key="@hermes:example.org",
    sender="@alice:example.org",
    is_direct=True,
    membership="invite",
):
    """Create a stripped m.room.member event as found in invite_state."""
    return {
        "type": "m.room.member",
        "state_key": state_key,
        "sender": sender,
        "content": {"membership": membership, "is_direct": is_direct},
    }


def _invite_sync_data(room_id="!dm_room:example.org", invite_state=None):
    """Create a sync payload with one pending invite room."""
    room = {} if invite_state is None else {"invite_state": invite_state}
    return {"rooms": {"invite": {room_id: room}}, "next_batch": "s1"}


class TestPendingInviteReconciliationRecordsDM:
    """_schedule_pending_invite_joins threads the DM signal from invite_state.

    After a gateway restart a direct invite is reconciled from sync's
    ``rooms.invite`` rather than a live invite event. The stripped
    ``m.room.member`` event in ``invite_state`` still carries ``is_direct``
    and the inviter; without threading them through, the room is never
    recorded in ``m.direct`` and is misclassified as a group (surfaced by
    the triage of #62493).
    """

    @staticmethod
    async def _drain_invite_tasks(adapter):
        """Await any tasks _schedule_invite_join spawned."""
        for task in list(adapter._invite_join_tasks.values()):
            await task

    @pytest.mark.asyncio
    async def test_reconciled_direct_invite_records_dm(self):
        adapter = _make_adapter()
        adapter._join_room_by_id = AsyncMock(return_value=True)
        adapter._record_dm_room = AsyncMock()

        sync_data = _invite_sync_data(
            invite_state={"events": [_member_invite_event(is_direct=True)]}
        )
        adapter._schedule_pending_invite_joins(sync_data)
        await self._drain_invite_tasks(adapter)

        adapter._join_room_by_id.assert_awaited_once_with("!dm_room:example.org")
        adapter._record_dm_room.assert_awaited_once_with(
            "!dm_room:example.org", "@alice:example.org"
        )

    @pytest.mark.asyncio
    async def test_reconciled_direct_invite_ends_up_classified_as_dm(self):
        """End to end: the reconciled room lands in _dm_rooms as a DM."""
        adapter = _make_adapter()
        adapter._join_room_by_id = AsyncMock(return_value=True)
        adapter._client = MagicMock()
        adapter._client.get_account_data = AsyncMock(
            side_effect=Exception("M_NOT_FOUND")
        )
        adapter._client.set_account_data = AsyncMock()

        sync_data = _invite_sync_data(
            invite_state={"events": [_member_invite_event(is_direct=True)]}
        )
        adapter._schedule_pending_invite_joins(sync_data)
        await self._drain_invite_tasks(adapter)

        adapter._client.set_account_data.assert_awaited_once_with(
            "m.direct", {"@alice:example.org": ["!dm_room:example.org"]}
        )
        assert adapter._dm_rooms.get("!dm_room:example.org") is True

    @pytest.mark.asyncio
    async def test_reconciled_non_direct_invite_does_not_record(self):
        """A pending invite without the is_direct flag joins (the inviter
        is allow-listed) but records nothing in m.direct. Invites whose
        inviter cannot be read from the stripped state at all are covered
        by test_matrix_pending_invite_auth.py: they are rejected outright
        by the inviter allowlist gate, so no join happens either."""
        adapter = _make_adapter()
        adapter._join_room_by_id = AsyncMock(return_value=True)
        adapter._record_dm_room = AsyncMock()

        sync_data = _invite_sync_data(
            invite_state={"events": [_member_invite_event(is_direct=False)]}
        )
        adapter._schedule_pending_invite_joins(sync_data)
        await self._drain_invite_tasks(adapter)

        adapter._join_room_by_id.assert_awaited_once_with("!dm_room:example.org")
        adapter._record_dm_room.assert_not_awaited()


