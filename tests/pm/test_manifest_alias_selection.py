"""PM selections follow runtime manifest identities without cross-home alias leakage."""
from pathlib import Path

import pytest
import hermes_yaml as yaml
from hermes_cli.plugins_discovery import scan_directory, resolve_manifest_winners, gate_manifest
from pm import workspace as ws


@pytest.fixture
def roots(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    other = home / 'profiles/work'
    bundled = tmp_path / 'repo/plugins'
    for p in (home, other, bundled):
        p.mkdir(parents=True)
    monkeypatch.setattr('pm.plugins_state.dependency_homes', lambda: [home, other])
    monkeypatch.setattr(ws.paths, 'repo_root', lambda: bundled.parent)
    return home, other, bundled


def config(home, enabled, disabled=()):
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'enabled': enabled, 'disabled': list(disabled)}}))


def plugin(root, directory, name):
    p = root / directory
    p.mkdir(parents=True)
    (p / 'plugin.yaml').write_text(yaml.safe_dump({'name': name, 'python_dependencies': ['aiohttp>=3.14.3,<4']}))
    return p


def loaded(home, bundled, enabled, disabled=()):
    winners = resolve_manifest_winners(scan_directory(bundled, 'bundled') + scan_directory(home / 'plugins', 'user'))
    return {Path(m.path) for m in winners.values() if gate_manifest(m, set(disabled), set(enabled)).action == 'load'}


def test_flat_manifest_alias_not_directory_key(roots):
    home, _, bundled = roots
    p = plugin(home / 'plugins', 'telnyx_sms', 'telnyx-sms-platform')
    config(home, ['telnyx-sms-platform'])
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['telnyx-sms-platform']) == {p}
    config(home, ['telnyx_sms'])
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['telnyx_sms']) == set()


def test_flat_alias_collision_uses_discovery_winner(roots):
    home, _, bundled = roots
    plugin(home / 'plugins', 'aaa', 'same')
    winner = plugin(home / 'plugins', 'zzz', 'same')
    config(home, ['same'])
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['same']) == {winner}


@pytest.mark.parametrize('disabled', [[], ['shared'], ['category/one'], ['one']])
def test_category_alias_collision_and_disable_parity(roots, disabled):
    home, _, bundled = roots
    a = plugin(home / 'plugins', 'category/one', 'shared')
    b = plugin(home / 'plugins', 'other/two', 'shared')
    config(home, ['category/one', 'shared'], disabled)
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['category/one', 'shared'], disabled)
    if not disabled:
        assert set(ws.enabled_member_dirs()) == {a, b}


def test_bundled_impostor_and_legitimate_override(roots):
    home, _, bundled = roots
    original = plugin(bundled, 'builtin', 'builtin')
    plugin(home / 'plugins', 'aaa-impostor', 'builtin')
    config(home, ['builtin'])
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['builtin']) == {original}
    override = plugin(home / 'plugins', 'builtin', 'builtin')
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['builtin']) == {override}


def test_aliases_are_resolved_per_profile(roots):
    home, other, _bundled = roots
    a = plugin(home / 'plugins', 'sms_default', 'shared')
    b = plugin(other / 'plugins', 'sms_work', 'shared')
    config(home, ['shared'])
    config(other, ['shared'])
    assert set(ws.enabled_member_dirs()) == {a, b}
    config(home, [])
    assert ws.enabled_member_dirs() == [b]
    config(other, ['shared'], ['shared'])
    assert ws.enabled_member_dirs() == []


def test_proposed_alias_disable_does_not_write_config(roots):
    home, _, _ = roots
    p = plugin(home / 'plugins', 'category/sms', 'sms-platform')
    config(home, ['category/sms'])
    before = (home / 'config.yaml').read_bytes()
    assert ws.enabled_member_dirs() == [p]
    assert ws.enabled_member_dirs(proposed_home=home, enabled=['category/sms'], disabled=['sms-platform']) == []
    assert (home / 'config.yaml').read_bytes() == before


def test_manifestless_package_and_staged_destination_survive(roots):
    home, _, _ = roots
    p = home / 'plugins/legacy'
    p.mkdir(parents=True)
    (p / 'pyproject.toml').write_text('[project]\nname="legacy"\nversion="1.0"\n')
    config(home, ['legacy'])
    assert ws.enabled_member_dirs() == [p]
    staging = home / 'plugins/incoming'
    assert ws.enabled_plugin_dirs(proposed_home=home, enabled=['incoming'], installing=staging) == [staging]


def test_unselected_package_metadata_does_not_enter_alias_scan(roots):
    home, _, _ = roots
    p = plugin(home / 'plugins', 'selected_dir', 'selected')
    bad = plugin(home / 'plugins', 'unselected', 'unselected')
    (bad / 'pyproject.toml').write_text('[broken TOML')
    config(home, ['selected'])
    assert ws.enabled_member_dirs() == [p]


def test_three_way_collision_follows_existing_runtime_order(roots):
    home, _, bundled = roots
    plugin(bundled, 'builtin', 'builtin')
    plugin(home / 'plugins', 'builtin', 'builtin')
    last = plugin(home / 'plugins', 'zzz', 'builtin')
    config(home, ['builtin'])
    # Runtime's last-writer rule applies after the first legitimate user
    # override. PM must not silently choose a different dependency provider.
    assert set(ws.enabled_member_dirs()) == loaded(home, bundled, ['builtin']) == {last}


def test_update_staging_copy_never_becomes_a_member(roots):
    """plugins_transaction stages an update under ``<plugins_dir>/.update-*/plugin`` while the installed
    tree stays the member; a same-named staged manifest must not enter the union (its relative
    ``[tool.uv.sources]`` would resolve from the staging dir)."""
    home, _, _ = roots
    installed = plugin(home / 'plugins', 'transactional', 'transactional')
    plugin(home / 'plugins', '.update-abc123/plugin', 'transactional')
    config(home, ['transactional'])
    assert ws.enabled_member_dirs() == [installed]
