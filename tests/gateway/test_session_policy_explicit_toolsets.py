"""An explicit ``toolsets`` pick on ``session.create`` is the session's exact allowlist.

``hermes chat -t web,vision -q ...`` runs through the gateway, which resolved the pick with the
saved-profile resolver and so inherited its augmentations: the active context engine's runtime
tools and every enabled MCP server. A restricted reader worker could then call ``lcm_*`` recall
tools over the profile's past sessions. Without a gateway the same ``-t`` was exact.
"""
import pytest

from gateway.session_policy import build_policy

_LCM = {'context': {'engine': 'lcm'}, 'platform_toolsets': {'cli': ['terminal', 'web', 'vision']}}


def _servers(tmp_path, *names):
    return {name: {'command': str(tmp_path / 'absent')} for name in names}


@pytest.mark.parametrize('source', ['cli', 'oneshot', 'tui'])
def test_an_explicit_pick_excludes_the_context_engine(tmp_path, source):
    policy = build_policy({'source': source, 'cwd': str(tmp_path), 'toolsets': ['web', 'vision']}, dict(_LCM))
    assert set(policy.toolsets) == {'web', 'vision'}, policy.toolsets


def test_naming_the_context_engine_keeps_it(tmp_path):
    policy = build_policy({'source': 'cli', 'cwd': str(tmp_path), 'toolsets': ['web', 'context_engine']},
                          dict(_LCM))
    assert set(policy.toolsets) == {'web', 'context_engine'}, policy.toolsets


def test_an_explicit_pick_excludes_unnamed_mcp_servers(tmp_path):
    cfg = {**_LCM, 'mcp_servers': _servers(tmp_path, 'srv')}
    policy = build_policy({'source': 'cli', 'cwd': str(tmp_path), 'toolsets': ['web']}, cfg)
    assert set(policy.toolsets) == {'web'}, policy.toolsets


@pytest.mark.parametrize('name', ['srv', 'mcp-srv'])
def test_a_named_mcp_server_stays_selected_alone(tmp_path, name):
    cfg = {**_LCM, 'mcp_servers': _servers(tmp_path, 'srv', 'other')}
    policy = build_policy({'source': 'cli', 'cwd': str(tmp_path), 'toolsets': ['web', name]}, cfg)
    assert set(policy.toolsets) == {'web', 'srv'}, policy.toolsets


def test_without_a_pick_the_profile_default_keeps_its_augmentations(tmp_path):
    cfg = {**_LCM, 'mcp_servers': _servers(tmp_path, 'srv')}
    policy = build_policy({'source': 'cli', 'cwd': str(tmp_path)}, cfg)
    assert {'terminal', 'web', 'vision', 'context_engine', 'srv'} <= set(policy.toolsets), policy.toolsets
