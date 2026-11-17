"""Scoped safe-updater handoff; absent outside an explicitly armed outer transaction.

Only the external snapshot's pinned stdlib producer is loaded. The plan is durable
before mutation and its lock spans publication; recovery uses that same lock.
This is process handoff metadata, not a user configuration option.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Iterator


def active() -> bool:
    return bool(os.environ.get("HERMES_UPDATE_SNAPSHOT"))


@contextmanager
def publication(target: Path, staged: Path | None, *, replace_symlink: bool = False) -> Iterator[None]:
    """Set replace_symlink only for directory-entry replacement, never a write-through."""
    snapshot = os.environ.get("HERMES_UPDATE_SNAPSHOT")
    if not snapshot:
        yield
        return
    from pm.paths import repo_root
    from pm.environments import dependency_home_root

    destination = Path(snapshot)
    manifest = json.loads((destination / "manifest.json").read_bytes())
    helper = destination / "producer.py"
    if (manifest.get("transaction_id") != os.environ.get("HERMES_UPDATE_CORRELATION_ID")
            or manifest.get("producer_sha256") != hashlib.sha256(helper.read_bytes()).hexdigest()
            or manifest.get("repo") != str(repo_root().resolve())
            or manifest.get("home") != str(dependency_home_root().resolve())):
        raise RuntimeError("outer publication handoff identity mismatch")
    spec = importlib.util.spec_from_file_location("_hermes_outer_producer", helper)
    if spec is None or spec.loader is None:
        raise RuntimeError("outer publication helper is unavailable")
    producer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    options = {"replace_symlink": True} if replace_symlink else {}
    with producer.producer_publication(destination, repo_root(), dependency_home_root(),
                                       Path(target).absolute(), staged, **options):
        yield


def unlink(path: Path) -> None:
    with publication(path, None):
        path.unlink(missing_ok=True)


def refuse_plugin_swap() -> None:
    if active():
        raise RuntimeError("outer update does not own whole plugin directories; "
                           "publish separately inventoried code, not mutable plugin runtime data")
