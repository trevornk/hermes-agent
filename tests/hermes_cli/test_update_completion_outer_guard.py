"""The outer safe-update's pre-swap launch guard ends with the first process of the new tree.

The hop onto a release is driven by the staged merge-base tree, whose launcher would otherwise
finish "its" update unattributed (2026-10-12 incident). The outer updater suppresses that with the
lazy-install bridge var plus a guard marker; the completion child must lift both so the update's
own explicit work (tool preparation, Node dependencies) is unchanged, and must leave a bridge var
that was set by someone else (Docker image, test harness) alone.
"""
import os

from hermes_cli import update_completion


def test_guard_lifts_bridge_var_only_when_the_outer_updater_set_it():
    environ = {"HERMES_UPDATE_PRE_SWAP_LAUNCH_GUARD": "1", "HERMES_DISABLE_LAZY_INSTALLS": "1", "KEEP": "x"}
    assert update_completion.lift_pre_swap_launch_guard(environ) is True
    assert environ == {"KEEP": "x"}


def test_foreign_bridge_var_is_kept_without_the_guard():
    environ = {"HERMES_DISABLE_LAZY_INSTALLS": "1"}
    assert update_completion.lift_pre_swap_launch_guard(environ) is False
    assert environ == {"HERMES_DISABLE_LAZY_INSTALLS": "1"}


def test_main_lifts_the_guard_before_reading_the_request(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_UPDATE_PRE_SWAP_LAUNCH_GUARD", "1")
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    seen = {}

    def fake_read_text(self, *args, **kwargs):
        seen["env"] = dict(os.environ)
        raise RuntimeError("stop here")

    monkeypatch.setattr(update_completion.Path, "read_text", fake_read_text)
    monkeypatch.setattr(update_completion.sys, "argv", ["update_completion.py", str(tmp_path / "req"), str(tmp_path / "res")])
    try:
        update_completion.main()
    except RuntimeError as exc:
        assert str(exc) == "stop here"
    assert "HERMES_UPDATE_PRE_SWAP_LAUNCH_GUARD" not in seen["env"]
    assert "HERMES_DISABLE_LAZY_INSTALLS" not in seen["env"]
    assert update_completion.PRE_SWAP_LAUNCH_GUARD == "HERMES_UPDATE_PRE_SWAP_LAUNCH_GUARD"
