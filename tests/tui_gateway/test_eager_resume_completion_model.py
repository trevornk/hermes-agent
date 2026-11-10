"""A queued completion can run as soon as eager resume starts session services."""
import json
from types import SimpleNamespace

from tui_gateway import server
from tests.tui_gateway.test_session_resume_db_ownership import (
    _RecordingDB, _resume, profile_dbs,
)


def test_eager_resume_completion_keeps_stored_model_before_rpc_returns(profile_dbs, monkeypatch):
    def database(db_path=None, **kwargs):
        db = _RecordingDB(db_path=db_path, **kwargs)
        db.rows['owner'] = {'id': 'owner', 'cwd': '', 'source': 'desktop',
                            'model': 'gpt-6-astra', 'model_config': json.dumps({
                                'model': 'gpt-6-astra', 'provider': 'openai-codex',
                                'reasoning_config': {'enabled': True, 'effort': 'high'}})}
        profile_dbs.append(db)
        return db

    monkeypatch.setattr('hermes_state_registry.acquire', database)
    monkeypatch.setattr(server, '_config_model_target', lambda: ('profile-default', 'other-provider'))
    monkeypatch.setattr(server, '_load_cfg', lambda: {'model': {'default': 'profile-default', 'provider': 'other-provider'}})
    monkeypatch.setattr(server, '_make_agent', lambda *a, **kw: SimpleNamespace(
        model=kw['model_override']['model'], provider=kw['provider_override']))
    monkeypatch.setattr(server, '_hydrate_session_cwd', lambda *a, **kw: None)
    monkeypatch.setattr(server, '_register_session_cwd', lambda *a: None)
    monkeypatch.setattr(server, '_session_todo_state', lambda *a: None)
    monkeypatch.setattr(server, '_wire_session_agent', lambda *a: None)
    monkeypatch.setattr(server, '_schedule_mcp_late_refresh', lambda *a: None)
    monkeypatch.setattr(server, '_emit', lambda *a: None)
    monkeypatch.setattr(server, '_session_info', lambda agent, *a: {'model': agent.model})
    monkeypatch.setattr(server, '_set_session_context', lambda *a, **kw: [])
    monkeypatch.setattr(server, '_clear_session_context', lambda *a: None)
    switches, observed = [], []

    def switch(sid, session, value, **kwargs):
        switches.append(value)
        session['agent'].model = 'profile-default'

    def services(sid, key, session):
        # Deterministic ordering of the real race: the notification turn's normal
        # model sync runs before eager resume has returned from _init_session.
        server._sync_agent_model_with_config(sid, session)
        observed.append(session['agent'].model)

    monkeypatch.setattr(server, '_apply_model_switch', switch)
    monkeypatch.setattr(server, '_start_session_services', services)
    response = _resume(session_id='owner', profile='work', eager_build=True, omit_messages=True)
    assert 'error' not in response, response
    assert observed == ['gpt-6-astra']
    assert switches == []
    session = server._sessions[response['result']['session_id']]
    assert session['model_override']['model'] == 'gpt-6-astra'
