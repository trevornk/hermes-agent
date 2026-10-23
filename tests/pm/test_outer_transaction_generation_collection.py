"""Generation collectors yield to an armed outer safe-update transaction.

The outer rollback (hermes_update_transaction.py) refuses to restore when a retained generation
is gone, so nothing running under HERMES_UPDATE_SNAPSHOT may reclaim one: not the sync's own
collector (venv_sync), not ``pm gc`` (runtime_state.collect_generations), not the PM runtime
collector. 2026-10-12: an unselected, unleased, day-old environment was collected mid-update and
the rollback refused.
"""
import json
import os

from pm.environments import install_state_dir, runtime_facts_path, site_packages


def _eligible_generation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    state = install_state_dir(repo)
    for name in ("selected", "superseded"):
        venv = state / "environments" / name / "venv"
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("version = 3.11")
        site_packages(venv).mkdir(parents=True)
        (venv.parent / ".lease-managed").touch()
    runtime_facts_path(repo).write_text(json.dumps(
        {"packages": {"venv": {"environment": str(state / "environments" / "selected" / "venv")}}}))
    return repo, state / "environments" / "superseded"


def test_environment_collector_skips_under_outer_transaction(tmp_path, monkeypatch):
    from hermes_cli.runtime_state import collect_generations

    repo, superseded = _eligible_generation(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_UPDATE_SNAPSHOT", str(tmp_path / "snapshot"))
    assert collect_generations(repo, min_age_seconds=0) == []
    assert superseded.is_dir()
    monkeypatch.delenv("HERMES_UPDATE_SNAPSHOT")
    assert collect_generations(repo, min_age_seconds=0) == [superseded]
    assert not superseded.exists()


def test_runtime_collector_skips_under_outer_transaction(tmp_path, monkeypatch):
    from pm.runtime import collect_runtime_generations

    root = tmp_path / "pm-runtime"
    generations = root / "generations"
    stale = generations / "stale"
    stale.mkdir(parents=True)
    (stale / "pm-runtime.json").write_text("{}")
    (stale / ".lease-managed").touch()
    (root / "selected.json").write_text(json.dumps({"generation": "generations/other"}))
    monkeypatch.setenv("HERMES_UPDATE_SNAPSHOT", str(tmp_path / "snapshot"))
    assert collect_runtime_generations(root) == []
    assert stale.is_dir()
    monkeypatch.delenv("HERMES_UPDATE_SNAPSHOT")
    assert collect_runtime_generations(root) == [stale]
    assert not stale.exists()


def test_held_outer_lease_is_respected_by_the_collector_without_the_env(tmp_path, monkeypatch):
    """The outer updater's lease holder (hermes_update_transaction.py hold-leases) protects a
    generation even for a collector that never saw HERMES_UPDATE_SNAPSHOT (an older tree)."""
    import fcntl

    from hermes_cli.runtime_state import collect_generations

    repo, superseded = _eligible_generation(tmp_path, monkeypatch)
    leases = superseded / ".leases"
    leases.mkdir()
    fd = os.open(leases / "outer-fixture", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert collect_generations(repo, min_age_seconds=0) == []
        assert superseded.is_dir() and (leases / "outer-fixture").is_file()
    finally:
        os.close(fd)
    assert collect_generations(repo, min_age_seconds=0) == [superseded]
