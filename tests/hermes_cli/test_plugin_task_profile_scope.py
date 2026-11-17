"""Background plugin work belongs to its manager, even when spawned from another turn."""

import asyncio
import threading
from pathlib import Path

import pytest

from agent import secret_scope
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize('multiplex,routed', [(True, False), (False, True), (False, False)])
def test_plugin_task_reads_only_owner_secrets(tmp_path, monkeypatch, multiplex, routed):
    launch = tmp_path / 'launch'
    owner = tmp_path / 'owner' if routed or multiplex else launch
    other = tmp_path / 'other'
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setenv('TASK_PROBE_TOKEN', 'ambient')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', multiplex)
    for home in {launch, owner, other}:
        home.mkdir()
    (owner / '.env').write_text('TASK_PROBE_TOKEN=owner\n')
    (other / '.env').write_text('TASK_PROBE_TOKEN=other\nTASK_OTHER_TOKEN=foreign\n')
    plugin = owner / 'plugins' / 'task_probe'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: task_probe\nversion: 0.1\n')
    (owner / 'config.yaml').write_text('plugins:\n  enabled: [task_probe]\n')
    (plugin / '__init__.py').write_text('''
from agent.secret_scope import get_secret
from hermes_constants import get_hermes_home
async def probe():
    return str(get_hermes_home()), get_secret('TASK_PROBE_TOKEN'), get_secret('TASK_OTHER_TOKEN')
def register(ctx):
    global context
    context = ctx
''')
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, 'get_bundled_plugins_dir', lambda: tmp_path / 'empty')
    monkeypatch.setattr(PluginManager, '_scan_entry_points', lambda self: [])
    manager = PluginManager(scope_key=str(owner))

    async def scenario():
        # Load normally, then spawn again while a different turn's secrets are bound.
        manager.discover_and_load()
        module = manager._plugins['task_probe'].module
        monkeypatch.setenv('TASK_PROBE_TOKEN', 'ambient')
        assert await module.context.spawn_task(module.probe()) == (str(owner), 'owner', None)
        token = secret_scope.set_secret_scope({'TASK_PROBE_TOKEN': 'caller'}, profile_home=str(other))
        home_token = set_hermes_home_override(str(other))
        try:
            assert await module.context.spawn_task(module.probe()) == (str(owner), 'owner', None)
            assert get_hermes_home() == other
            assert secret_scope.get_secret('TASK_PROBE_TOKEN') == 'caller'
        finally:
            reset_hermes_home_override(home_token)
            secret_scope.reset_secret_scope(token)
        manager.unload('task_probe')

    asyncio.run(scenario())


def test_task_scope_loads_off_loop_and_unwinds_on_error(tmp_path, monkeypatch):
    from gateway import run
    from hermes_cli.plugins_tasks import run_profile_task

    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    loop_thread = threading.get_ident()
    home = tmp_path / 'owner'
    def load(profile_home):
        assert threading.get_ident() != loop_thread
        assert get_hermes_home() == profile_home == home
        return {'TASK_PROBE_TOKEN': 'owner'}
    monkeypatch.setattr(run, '_load_profile_secret_scope', load)

    async def scenario():
        before = get_hermes_home()
        async def fail():
            assert secret_scope.get_secret('TASK_PROBE_TOKEN') == 'owner'
            raise ValueError('probe')
        with pytest.raises(ValueError, match='probe'):
            await run_profile_task(fail(), str(home))
        assert get_hermes_home() == before
        with pytest.raises(secret_scope.UnscopedSecretError):
            secret_scope.get_secret('TASK_PROBE_TOKEN')
    asyncio.run(scenario())


@pytest.mark.platforms('any')  # A real per-profile child proves its background credential path.
def test_host_background_task_uses_its_own_environment(tmp_path, monkeypatch):
    from tests.hermes_cli.test_plugin_host import _home_with_plugins
    from tools.registry import registry

    body = '''
from agent.secret_scope import get_secret
async def probe():
    return get_secret('TASK_PROBE_TOKEN')
def register(ctx):
    ctx.register_tool(name='task_host_probe', toolset='task_host_probe',
        schema={'name': 'task_host_probe', 'parameters': {'type': 'object', 'properties': {}}},
        handler=lambda args, **kw: ctx.spawn_task(probe()).result(timeout=10))
'''
    home = _home_with_plugins(tmp_path, monkeypatch, {'task_host_probe': body})
    (home / '.env').write_text('TASK_PROBE_TOKEN=host-owner\n')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    monkeypatch.setenv('TASK_PROBE_TOKEN', 'foreign-ambient')
    manager = PluginManager()
    try:
        manager.discover_and_load()
        assert registry.dispatch('task_host_probe', {}, scope=manager.scope_key) == 'host-owner'
    finally:
        manager.unload('task_host_probe')
        if manager._plugin_host_instance is not None:
            manager._plugin_host_instance.shutdown()


def test_task_cancelled_before_start_closes_plugin_coroutine():
    import inspect
    from hermes_cli.plugins import PluginContext, PluginManifest

    async def scenario():
        manager = PluginManager()
        ctx = PluginContext(PluginManifest(name='cancel_probe', source='user'), manager)
        async def probe():
            raise AssertionError('cancelled task must never start')
        coro = probe()
        task = ctx.spawn_task(coro)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
    asyncio.run(scenario())


@pytest.mark.parametrize('threaded', [False, True])
def test_task_survives_multiplex_activation(tmp_path, monkeypatch, threaded):
    from hermes_cli.plugins import PluginContext, PluginManifest

    owner, other = tmp_path / 'owner', tmp_path / 'other'
    for home in (owner, other):
        home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(owner))
    monkeypatch.delenv('TASK_OTHER_TOKEN', raising=False)
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    (owner / '.env').write_text('TASK_PROBE_TOKEN=owner\n')
    (other / '.env').write_text('TASK_OTHER_TOKEN=foreign\n')
    manager = PluginManager(scope_key=str(owner))
    ctx = PluginContext(PluginManifest(name='startup_probe', source='user'), manager)
    started, release = threading.Event(), threading.Event()

    async def probe():
        started.set()
        assert await asyncio.to_thread(release.wait, 10)
        return (get_hermes_home(), secret_scope.get_secret('TASK_PROBE_TOKEN'),
                secret_scope.get_secret('TASK_OTHER_TOKEN'))

    async def scenario():
        task = ctx.spawn_task(probe())
        try:
            assert await asyncio.to_thread(started.wait, 10)
            secret_scope.set_multiplex_active(True)
            monkeypatch.setenv('TASK_OTHER_TOKEN', 'foreign-ambient')
            release.set()
            assert await task == (owner, 'owner', None)
        finally:
            release.set()
            manager.unload('startup_probe')

    if threaded:
        task = ctx.spawn_task(probe())
        try:
            assert started.wait(10)
            secret_scope.set_multiplex_active(True)
            monkeypatch.setenv('TASK_OTHER_TOKEN', 'foreign-ambient')
            release.set()
            assert task.result(timeout=10) == (owner, 'owner', None)
        finally:
            release.set()
            manager.unload('startup_probe')
            task.thread.join(10)
        assert not task.thread.is_alive()
    else:
        asyncio.run(scenario())


def test_single_profile_scope_miss_uses_ambient_credential(tmp_path, monkeypatch):
    from hermes_cli.plugins_tasks import run_profile_task
    from gateway import run

    monkeypatch.setattr(run, '_load_profile_secret_scope', lambda home: {})
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    monkeypatch.setenv('TASK_AMBIENT_TOKEN', 'ambient-only')

    async def probe():
        assert secret_scope.current_secret_scope() == {}
        assert secret_scope.get_secret('TASK_AMBIENT_TOKEN') == 'ambient-only'

    asyncio.run(run_profile_task(probe(), str(tmp_path)))
