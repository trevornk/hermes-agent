"""Standalone health warnings distinguish PR holds from failed spawns without hiding starvation."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops as ops


@pytest.mark.parametrize("hold,failed", [(True, False), (False, True), (True, True), (False, False)])
def test_standalone_warns_with_actionable_cause(monkeypatch, capsys, hold, failed):
    res = kbd.DispatchResult(respawn_guarded=[("held", "active_pr")] if hold else [])
    res.spawn_failed = ["failed"] if failed else []
    monkeypatch.setattr(ops.kb, "init_db", lambda: None)
    monkeypatch.setattr(ops.kbc, "connect_closing", lambda: _ReadyConnection())
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)

    def daemon(**kwargs):
        for _ in range(6):
            kwargs["on_tick"](res)

    monkeypatch.setattr(kbd, "run_daemon", daemon)
    assert ops._cmd_daemon(SimpleNamespace(force=True, interval=1, verbose=False,
                                           pidfile=None, max=None, failure_limit=2)) == 0
    warning = capsys.readouterr().err
    assert "WARN dispatcher stuck" in warning
    if hold:
        assert "active_pr=1" in warning and "last run ended without a recorded handoff" in warning
    if failed:
        assert "spawn_failed=1" in warning and "Spawn failures recorded" in warning
    else:
        assert "Spawn failures recorded" not in warning


class _ReadyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
