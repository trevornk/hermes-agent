"""The PM fixture changes installation inputs, never the home-I/O policy."""
from pathlib import Path
import sys

import pytest

from tests._fixtures.pm_bootstrap import redirect_checkout


def test_checkout_redirect_runs_real_callable_and_preserves_other_roots(tmp_path):
    checkout = tmp_path / 'operator-checkout'
    sandbox = tmp_path / 'sandbox'
    explicit = tmp_path / 'explicit-install'
    seen = []

    def operation(root, *, value):
        seen.append(root)
        return root, value

    call = redirect_checkout(operation, checkout.resolve(), sandbox)
    assert call(checkout, value=1) == (sandbox, 1)
    assert call(value=2) == (sandbox, 2)
    assert call(explicit, value=3) == (explicit, 3)
    assert seen == [sandbox, sandbox, explicit]


def test_entrypoint_bootstrap_keeps_test_interpreter_and_guards_real_home(_isolate_pm_bootstrap):
    original = sys.prefix
    import hermes_bootstrap
    from hermes_cli import _early_recovery, venv_sync
    from pm import environments

    checkout = Path(__file__).resolve().parents[1]
    assert not _early_recovery.restore_interrupted_pull(checkout)
    assert not _early_recovery.recover_if_needed(checkout)
    assert venv_sync.prepare_launch(checkout, ['chat']) is None
    environments.activate_dependencies(checkout)
    assert sys.prefix == original
    assert _isolate_pm_bootstrap.is_dir()
    # This remains refused before an open: no real config contents are read.
    with pytest.raises(AssertionError, match='REAL hermes home'):
        (Path.home() / '.hermes' / 'config.yaml').read_text()
