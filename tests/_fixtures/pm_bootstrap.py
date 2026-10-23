"""Run incidental entry-point bootstrap against per-test installation state.

Importing a business module can reach hermes_bootstrap after the home-I/O
tripwire is armed. The source checkout is not a test installation: its linked
Git recovery markers and adjacent payload metadata belong to the operator.
Redirect only that checkout at PM's explicit project-root boundary. Execute
the real functions on a temporary installation; fixture-owned roots supplied
by bootstrap/PM tests pass through unchanged. No filesystem guard is relaxed.
"""
from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Callable, Any

import pytest


def redirect_checkout(function: Callable[..., Any], checkout: Path, sandbox: Path) -> Callable[..., Any]:
    @wraps(function)
    def isolated(project_root: Path | None = None, *args: Any, **kwargs: Any) -> Any:
        root = checkout if project_root is None else Path(project_root)
        if root.resolve() == checkout:
            root = sandbox
        return function(root, *args, **kwargs)

    return isolated


@pytest.fixture(autouse=True)
def _isolate_pm_bootstrap(_hermetic_environment, tmp_path, monkeypatch):
    from hermes_cli import _early_recovery, venv_sync
    from pm import environments

    checkout = Path(__file__).resolve().parents[2]
    sandbox = tmp_path / 'bootstrap-install' / 'repo'
    sandbox.mkdir(parents=True)
    for module, name in (
        (_early_recovery, 'restore_interrupted_pull'),
        (_early_recovery, 'recover_if_needed'),
        (venv_sync, 'prepare_launch'),
        (environments, 'activate_dependencies'),
        # Child process PATH construction probes sealed-payload metadata too.
        (environments, '_payload_manifest'),
    ):
        monkeypatch.setattr(module, name, redirect_checkout(getattr(module, name), checkout, sandbox))
    return sandbox
