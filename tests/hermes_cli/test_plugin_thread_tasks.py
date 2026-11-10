"""Synchronous plugin registration owns a supervised, profile-bound event loop."""

import asyncio
import inspect
import threading
from concurrent.futures import CancelledError, Future
from pathlib import Path

import pytest

from agent import secret_scope
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_constants import get_hermes_home


@pytest.mark.parametrize('multiplex', [True, False])
@pytest.mark.parametrize('teardown', ['unload', 'reload'])
def test_register_without_loop_scopes_and_stops_watcher(tmp_path, monkeypatch, multiplex, teardown):
    from hermes_cli import plugins

    launch, owner, other = [tmp_path / name for name in ('launch', 'owner', 'other')]
    if not multiplex:
        owner = launch
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(launch))
    monkeypatch.setenv('TASK_PROBE_TOKEN', 'ambient')
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', multiplex)
    for home in {launch, owner, other}:
        home.mkdir()
    (owner / '.env').write_text('TASK_PROBE_TOKEN=owner\n')
    (other / '.env').write_text('TASK_PROBE_TOKEN=other\nTASK_OTHER_TOKEN=foreign\n')
    plugin = owner / 'plugins' / 'thread_probe'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text('name: thread_probe\nversion: 0.1\n')
    (owner / 'config.yaml').write_text('plugins:\n  enabled: [thread_probe]\n')
    (plugin / '__init__.py').write_text('''
import asyncio
import threading
from agent.secret_scope import get_secret
from hermes_constants import get_hermes_home
started = threading.Event()
cleaned = threading.Event()
observed = []
async def watch():
    try:
        observed.append((str(get_hermes_home()), get_secret('TASK_PROBE_TOKEN'),
                         get_secret('TASK_OTHER_TOKEN')))
        started.set()
        await asyncio.Event().wait()
    finally:
        cleaned.set()
def register(ctx):
    global task
    task = ctx.spawn_task(watch(), name='thread-probe')
''')
    monkeypatch.setattr(plugins, 'get_bundled_plugins_dir', lambda: tmp_path / 'empty')
    monkeypatch.setattr(PluginManager, '_scan_entry_points', lambda self: [])
    manager = PluginManager(scope_key=str(owner))
    manager.discover_and_load()
    module = manager._plugins['thread_probe'].module
    task = module.task
    try:
        assert isinstance(task, Future)
        assert task.thread.daemon
        assert module.started.wait(10)
        assert module.observed == [(str(owner), 'owner', None)]
        assert get_hermes_home() == launch
        if multiplex:
            with pytest.raises(secret_scope.UnscopedSecretError):
                secret_scope.get_secret('TASK_PROBE_TOKEN')
        assert not task.done()
        if teardown == 'reload':
            manager.discover_and_load(force=True)
        else:
            assert manager.unload('thread_probe')
        task.thread.join(10)
        assert not task.thread.is_alive()
        assert module.cleaned.is_set()
        assert task.done() and task.cancelled()
        with pytest.raises(CancelledError):
            task.result()
    finally:
        current = manager._plugins.get('thread_probe')
        manager.unload('thread_probe')
        if current is not None:
            current.module.task.thread.join(10)
        task.cancel()
        task.thread.join(10)


def test_no_loop_failure_logged_and_completion_untracked(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', False)
    manager = PluginManager(scope_key=str(tmp_path))
    ctx = PluginContext(PluginManifest(name='thread_error', source='user'), manager)

    async def fail():
        raise ValueError('synthetic watcher failure')

    task = ctx.spawn_task(fail())
    task.thread.join(10)
    assert not task.thread.is_alive()
    assert task.done() and not task.cancelled()
    assert isinstance(task.exception(), ValueError)
    assert 'synthetic watcher failure' in caplog.text
    assert 'thread_error' not in manager._ownership_ledger


def test_no_loop_cancel_during_scope_load_closes_coroutine(tmp_path, monkeypatch):
    from gateway import run

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(secret_scope, '_MULTIPLEX_ACTIVE', True)
    loading, release = threading.Event(), threading.Event()

    def load(home):
        loading.set()
        assert release.wait(10)
        return {}

    monkeypatch.setattr(run, '_load_profile_secret_scope', load)
    manager = PluginManager(scope_key=str(tmp_path))
    ctx = PluginContext(PluginManifest(name='thread_cancel', source='user'), manager)

    async def probe():
        raise AssertionError('cancelled coroutine must not start')

    coro = probe()
    task = ctx.spawn_task(coro)
    try:
        assert loading.wait(10)
        manager.unload('thread_cancel')
    finally:
        release.set()
        task.thread.join(10)
    assert not task.thread.is_alive()
    assert task.cancelled()
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
