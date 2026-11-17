"""A context-engine plugin whose startup load failed is re-armed by the engine lookup.

Plugins load at process start under the per-plugin deadline. When a context-engine plugin's
import + ``register()`` overruns it (I/O contention during a restart storm) or raises, the failed
load disposes its registrations and the manager's engine slot stays empty for the life of the
process: every agent built afterwards logs "Context engine 'X' not found" and silently runs the
built-in compressor. Platform plugins already heal from the reconnect watcher (#126356); these
tests pin the same contract for context engines: the lookup that needs the engine retries the
failed load (bounded by the deadline, backed off while it keeps failing, per profile).
"""

from __future__ import annotations

import logging
import sys
import threading
import types
from pathlib import Path
from textwrap import dedent

import pytest
import hermes_yaml as yaml

from hermes_cli import plugins_loader

ENGINE_NAME = "ctx-rearm"
PLUGIN_NAME = "engine-plugin"  # like hermes-lcm vs ``lcm``: the plugin dir is not the engine name

# Bodies are spliced into the template below at register()'s indentation (dedent runs after the splice).
_MODES = {
    "ok": "pass",
    "hang_once": "if first:\n" + " " * 16 + "_rearm_probe.release.wait()",
    "raise_once": "if first:\n" + " " * 16 + "raise RuntimeError('startup I/O contention')",
    "raise_always": "raise RuntimeError('permanently broken')",
}


def _write_engine_plugin(home: Path, mode: str, name: str = PLUGIN_NAME) -> None:
    """A user plugin that registers a context engine; ``mode`` shapes its first (or every) load."""
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0", "description": "engine plugin"}), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(dedent(f'''
        import _rearm_probe
        from agent.context_engine import ContextEngine
        from hermes_constants import get_hermes_home

        class StubEngine(ContextEngine):
            @property
            def name(self):
                return "{ENGINE_NAME}"
            def update_from_response(self, usage):
                return None
            def should_compress(self, prompt_tokens=None):
                return False
            def compress(self, messages, current_tokens=None):
                return messages

        def register(ctx):
            home = str(get_hermes_home())
            _rearm_probe.calls.append(home)
            first = _rearm_probe.calls.count(home) == 1
            {_MODES[mode]}
            ctx.register_context_engine(StubEngine())
        '''), encoding="utf-8")
    _enable(home, name)


def _write_failing_plugin(home: Path, name: str) -> None:
    """An enabled plugin with no engine whose register() always raises."""
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0", "description": "broken"}), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(
        "import _rearm_probe\n"
        "def register(ctx):\n"
        f"    _rearm_probe.calls.append('{name}')\n"
        "    raise RuntimeError('broken')\n", encoding="utf-8")
    _enable(home, name)


def _enable(home: Path, name: str) -> None:
    config = home / "config.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    data.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    config.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def probe(monkeypatch):
    """Load-attempt ledger the synthetic plugins report into (one entry per register() call)."""
    mod = types.ModuleType("_rearm_probe")
    mod.calls = []
    mod.release = threading.Event()
    monkeypatch.setitem(sys.modules, "_rearm_probe", mod)
    monkeypatch.setattr(plugins_loader, "_ABANDONED_LOADERS", [])
    yield mod
    mod.release.set()
    for worker in list(plugins_loader._ABANDONED_LOADERS):
        worker.join(5)


@pytest.fixture
def clock(monkeypatch):
    """A manual monotonic clock for the loader module only (threading keeps the real one)."""
    now = [1000.0]
    monkeypatch.setattr(plugins_loader, "time", types.SimpleNamespace(monotonic=lambda: now[0]), raising=False)
    return now


def _discover(home: Path):
    import hermes_cli.plugins as plugins_mod
    manager = plugins_mod.get_plugin_manager()
    manager.discover_and_load()
    return manager


def test_timed_out_engine_plugin_is_rearmed_and_the_agent_gets_the_engine(tmp_path, monkeypatch, probe, caplog):
    """The production shape: the load overruns ``plugins.load_timeout_seconds`` at startup. The lookup
    leaves a still-hung worker alone (another import would only leak a thread); once it has died, the
    next agent build re-arms the load and selects the engine instead of warning "not found"."""
    import hermes_cli.plugins as plugins_mod
    from agent.agent_init import _select_context_engine

    home = tmp_path / "home"
    _write_engine_plugin(home, "hang_once")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.2)

    manager = _discover(home)
    loaded = manager._plugins[PLUGIN_NAME]
    assert loaded.enabled is False
    assert loaded.error.startswith("load timed out after 0.2s")
    assert manager._context_engine is None

    assert plugins_mod.get_plugin_context_engine() is None  # its hung worker is still running
    assert probe.calls.count(str(home)) == 1
    probe.release.set()
    for worker in plugins_loader._ABANDONED_LOADERS:
        worker.join(5)
    assert manager._context_engine is None  # the abandoned worker's late registration was refused
    activated = []
    manager.on_plugin_loaded(activated.extend)  # the gateway re-wires handlers from this (#87770)

    with caplog.at_level(logging.INFO):
        engine = _select_context_engine({"context": {"engine": ENGINE_NAME}})

    assert engine is not None and engine.name == ENGINE_NAME
    assert [entry["key"] for entry in activated] == [PLUGIN_NAME]
    assert probe.calls.count(str(home)) == 2
    assert manager._plugins[PLUGIN_NAME].enabled is True
    assert manager._plugins[PLUGIN_NAME].error is None
    assert f"Re-arming failed context-engine plugin load: {PLUGIN_NAME}" in caplog.text
    assert "not found" not in caplog.text
    # Healed: later lookups return the registered engine without another import.
    assert plugins_mod.get_plugin_context_engine() is manager._context_engine
    assert probe.calls.count(str(home)) == 2


def test_persistently_failing_engine_plugin_is_retried_on_a_backoff(tmp_path, monkeypatch, probe, clock):
    """A plugin that keeps failing costs one bounded import per backoff window, not one per agent build:
    immediate first retry, then 30s doubling up to a 10-minute cap."""
    import hermes_cli.plugins as plugins_mod

    home = tmp_path / "home"
    _write_engine_plugin(home, "raise_always")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = _discover(home)
    assert manager._plugins[PLUGIN_NAME].error == "permanently broken"

    def attempts() -> int:
        return probe.calls.count(str(home))

    assert attempts() == 1

    for _ in range(3):
        assert plugins_mod.get_plugin_context_engine() is None
    assert attempts() == 2  # one immediate retry; the other builds in the window imported nothing

    clock[0] += 29.0
    assert plugins_mod.get_plugin_context_engine() is None
    assert attempts() == 2
    clock[0] += 1.0  # 30s after the first retry
    assert plugins_mod.get_plugin_context_engine() is None
    assert attempts() == 3
    clock[0] += 59.0
    assert plugins_mod.get_plugin_context_engine() is None
    assert attempts() == 3
    clock[0] += 1.0  # the wait doubled to 60s
    assert plugins_mod.get_plugin_context_engine() is None
    assert attempts() == 4

    for expected in range(5, 11):  # 120, 240, 480, then capped at 600: one retry per 10-minute window
        clock[0] += 600.0
        assert plugins_mod.get_plugin_context_engine() is None
        assert attempts() == expected
    assert manager._plugins[PLUGIN_NAME].error == "permanently broken"
    assert manager._context_engine_rearm[PLUGIN_NAME] == (9, clock[0] + 600.0)  # 9 retries after the first load


def test_healthy_engine_plugin_is_never_reloaded(tmp_path, monkeypatch, probe, caplog):
    """The healthy path is untouched: one import at discovery, the same engine instance on every lookup,
    no re-arm state and no re-arm log."""
    import hermes_cli.plugins as plugins_mod

    home = tmp_path / "home"
    _write_engine_plugin(home, "ok")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = _discover(home)
    engine = manager._context_engine
    assert engine is not None

    with caplog.at_level(logging.INFO, logger="hermes_cli.plugins"):
        for _ in range(3):
            assert plugins_mod.get_plugin_context_engine() is engine
    assert probe.calls == [str(home)]
    assert "Re-arming" not in caplog.text


def test_lookup_never_retries_a_failed_plugin_that_registers_no_engine(tmp_path, monkeypatch, probe):
    """Only context-engine plugins are candidates: an unrelated broken plugin is not re-imported just
    because the configured engine is missing."""
    import hermes_cli.plugins as plugins_mod

    home = tmp_path / "home"
    _write_failing_plugin(home, "notes")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = _discover(home)
    assert manager._plugins["notes"].error == "broken"

    reads = []
    real = plugins_loader.PluginLoaderMixin._declares_context_engine
    monkeypatch.setattr(plugins_loader.PluginLoaderMixin, "_declares_context_engine",
                        staticmethod(lambda manifest: reads.append(manifest.name) or real(manifest)))
    for _ in range(3):
        assert plugins_mod.get_plugin_context_engine() is None
    assert probe.calls == ["notes"]
    assert reads == ["notes"]  # its source is read once, not on every engine lookup


def test_rearm_is_scoped_to_the_profile_whose_load_failed(tmp_path, probe):
    """One process serves many profiles: profile A's failed engine load neither affects profile B's
    engine nor is retried by B's lookups, and A's own lookup heals only A."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import hermes_cli.plugins as plugins_mod

    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    _write_engine_plugin(home_a, "raise_once")
    _write_engine_plugin(home_b, "ok")

    def under(home, fn):
        token = set_hermes_home_override(str(home))
        try:
            return fn()
        finally:
            reset_hermes_home_override(token)

    manager_a = under(home_a, lambda: _discover(home_a))
    manager_b = under(home_b, lambda: _discover(home_b))
    assert manager_a is not manager_b
    assert manager_a._plugins[PLUGIN_NAME].error == "startup I/O contention"
    assert manager_a._context_engine is None
    engine_b = manager_b._context_engine
    assert engine_b is not None

    assert under(home_b, plugins_mod.get_plugin_context_engine) is engine_b
    assert probe.calls.count(str(home_a)) == 1  # B's lookup did not touch A's failed plugin

    engine_a = under(home_a, plugins_mod.get_plugin_context_engine)
    assert engine_a is not None and engine_a is manager_a._context_engine
    assert engine_a is not engine_b
    assert manager_a._plugins[PLUGIN_NAME].enabled is True
    assert probe.calls.count(str(home_a)) == 2
    assert probe.calls.count(str(home_b)) == 1
    assert manager_b._context_engine is engine_b
