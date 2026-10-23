"""``kanban.worker_model`` pins the model for ordinary dispatched workers.

Without it every worker inherits the assigned profile's main model, so a profile whose main slot is
the expensive planner model also runs every well-specified kanban card on it. The knob routes plain
cards to a cheaper worker model while a per-card ``model`` override and a decomposition root (the
card that judges its fanned-out children) keep their own routing. Workers run inside the owning
gateway (``gateway/session_kanban.build_kanban_policy``), so the knob is applied there.
"""
from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest


@pytest.fixture
def claim(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from gateway.session_contract import Principal
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_DELEGATED_CHILD', raising=False)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env',
                        lambda name: str(tmp_path) if name == 'default' else str(tmp_path / name))
    kb.create_board('owned')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()

    def _claim(*, decomposed=False, **task_kw):
        with closing(connect(board='owned')) as conn:
            task_id = kb.create_task(conn, title='card', body='acceptance: tests pass', assignee='default',
                                     workspace_kind='dir', workspace_path=str(workspace), **task_kw)
            if decomposed:
                kb._append_event(conn, task_id, 'decomposed', {'child_ids': ['x'], 'root_assignee': 'default'})
                conn.commit()
            kb.recompute_ready(conn)
            task = kb.claim_task(conn, task_id)
        actor = Principal('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
        connection = SimpleNamespace(authority=SimpleNamespace(profile_id=str(tmp_path)), actor=actor, native_owner=True)
        return connection, dict(board='owned', task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)

    return _claim


def _config(**kanban):
    return {'model': {'default': 'planner-model', 'provider': 'custom'},
            'platform_toolsets': {'cli': ['terminal', 'file']}, 'kanban': kanban}


def _policy(claim, config, **task_kw):
    from gateway.session_kanban import build_kanban_policy
    connection, params = claim(**task_kw)
    policy, _secrets = build_kanban_policy(connection, params, config)
    return policy


def test_unset_worker_model_keeps_profile_default(claim):
    policy = _policy(claim, _config())
    assert policy.model == 'planner-model' and policy.provider == 'custom'


def test_worker_model_applies_to_plain_card(claim):
    policy = _policy(claim, _config(worker_model='worker-model', worker_provider='anthropic'))
    assert policy.model == 'worker-model' and policy.provider == 'anthropic'


def test_worker_model_without_provider_keeps_profile_provider_resolution(claim):
    policy = _policy(claim, _config(worker_model='worker-model'))
    assert policy.model == 'worker-model'


def test_card_override_beats_worker_model(claim):
    policy = _policy(claim, _config(worker_model='worker-model', worker_provider='anthropic'),
                     model_override='card-model', provider_override='openai-codex')
    assert policy.model == 'card-model' and policy.provider == 'openai-codex'


def test_decomposition_root_keeps_profile_default(claim):
    policy = _policy(claim, _config(worker_model='worker-model', worker_provider='anthropic'), decomposed=True)
    assert policy.model == 'planner-model' and policy.provider == 'custom'


def test_reasoning_effort_still_independent(claim):
    from hermes_constants import parse_reasoning_effort
    policy = _policy(claim, _config(worker_model='worker-model', worker_provider='anthropic'), reasoning_effort='high')
    assert policy.model == 'worker-model' and policy.reasoning_config == parse_reasoning_effort('high')
