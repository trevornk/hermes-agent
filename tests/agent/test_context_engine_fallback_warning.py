"""The "Context engine 'X' not found" fallback warning identifies its process, session and cause.

A bare "not found" line could not be tied to a session or to the plugin load failure behind it (the
plugin dir, e.g. ``hermes-lcm``, need not match the engine name, ``lcm``).
"""

from __future__ import annotations

import logging
import os
from textwrap import dedent

import hermes_yaml as yaml

ENGINE_NAME = "ctx-diag"
PLUGIN_NAME = "diag-engine-plugin"


def test_fallback_warning_carries_pid_session_and_plugin_load_error(tmp_path, monkeypatch, caplog):
    import hermes_cli.plugins as plugins_mod
    from agent.agent_init import _select_context_engine
    from hermes_cli import plugins_loader

    home = tmp_path / "home"
    plugin_dir = home / "plugins" / PLUGIN_NAME
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": PLUGIN_NAME, "version": "0.1.0", "description": "broken engine"}),
        encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(dedent('''
        def register(ctx):
            raise RuntimeError("lcm database is locked")
            ctx.register_context_engine(None)
        '''), encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_NAME]}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugins_loader, "_ABANDONED_LOADERS", [])

    plugins_mod.get_plugin_manager().discover_and_load()

    with caplog.at_level(logging.WARNING):
        engine = _select_context_engine({"context": {"engine": ENGINE_NAME}}, "sess-diag-123")

    assert engine is None
    warning = next(r.getMessage() for r in caplog.records
                   if r.getMessage().startswith(f"Context engine '{ENGINE_NAME}' not found"))
    assert f"pid={os.getpid()}" in warning
    assert "sess-diag-123" in warning
    assert "lcm database is locked" in warning
    assert PLUGIN_NAME in warning
