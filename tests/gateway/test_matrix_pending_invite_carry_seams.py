"""Behavioral seams carried by the original pending-invite production patch."""
from unittest.mock import Mock

import pytest
from tests.gateway.test_matrix_pending_invite_carried import (
    _make_adapter, _member_invite_event, _invite_sync_data, _drain_invite_tasks,
)

@pytest.mark.asyncio
@pytest.mark.parametrize('recipient', ['@HERMES:EXAMPLE.ORG', '  @hermes:example.org  ', '\t@Hermes:Example.Org\n'])
async def test_normalized_recipient_joins(recipient):
    adapter = _make_adapter()
    adapter._schedule_pending_invite_joins(_invite_sync_data(invite_state={
        'events': [_member_invite_event(state_key=recipient)]}))
    await _drain_invite_tasks(adapter)
    adapter._join_room_by_id.assert_awaited_once_with('!pending_room:example.org')
    adapter._record_dm_room.assert_awaited_once_with('!pending_room:example.org', '@alice:example.org')

@pytest.mark.parametrize('sender', ['', None])
def test_allow_all_missing_inviter_never_schedules_dm(monkeypatch, sender):
    monkeypatch.setenv('GATEWAY_ALLOW_ALL_USERS', 'true')
    adapter = _make_adapter()
    adapter._schedule_invite_join = Mock()
    adapter._schedule_pending_invite_joins(_invite_sync_data(invite_state={
        'events': [_member_invite_event(sender=sender)]}))
    adapter._schedule_invite_join.assert_called_once_with(
        '!pending_room:example.org', is_direct=False, inviter='')

@pytest.mark.asyncio
async def test_default_allow_all_does_not_authorize_secondary(monkeypatch):
    from agent.secret_scope import set_secret_scope, reset_secret_scope, set_multiplex_active
    monkeypatch.setenv('GATEWAY_ALLOW_ALL_USERS', 'true')
    adapter = _make_adapter()
    adapter._allowed_user_ids = set()
    token = set_secret_scope({})
    set_multiplex_active(True)
    try:
        adapter._schedule_pending_invite_joins(_invite_sync_data(invite_state={
            'events': [_member_invite_event(sender='@mallory:example.org')]}))
        await _drain_invite_tasks(adapter)
        adapter._join_room_by_id.assert_not_awaited()
        adapter._record_dm_room.assert_not_awaited()
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)
