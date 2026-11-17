"""Read-only discovery identities for the PM union (never import plugin code)."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pm.plugin_declarations import native_manifest_file, read_native_manifest
from pm.plugins_state import _is_directory

if TYPE_CHECKING:
    from hermes_cli.plugins_manifest import PluginManifest

# Same category exclusions as collect_directory_manifests. These categories
# have their own provider activation; PM retains their explicit path selection.
BUNDLED_PROVIDER_DIRS = {"memory", "context_engine", "model-providers", "cron_providers", "computer_use"}


def directory_manifests(root: Path, source: str) -> list[PluginManifest]:
    """Use discovery's parser/depth rules, but refuse partial dependency scans.

    Runtime discovery logs unreadable plugins and continues. PM cannot publish a
    smaller environment on that basis, so validate the scan's IO first.
    """
    from hermes_cli.plugins_discovery import scan_directory, _FOREIGN_HARNESS_MANIFEST_DIRS

    skip = BUNDLED_PROVIDER_DIRS if source == "bundled" else set()

    def validate(directory: Path, depth: int = 0) -> None:
        if not _is_directory(directory):
            return
        try:
            children = sorted(directory.iterdir())
        except OSError as exc:
            raise ValueError(f"could not enumerate plugins: {directory}") from exc
        for child in children:
            if (child.name.startswith("__") and child.name.endswith("__")
                    or child.name in _FOREIGN_HARNESS_MANIFEST_DIRS
                    or depth == 0 and child.name in skip):
                continue
            if not _is_directory(child):
                continue
            native = native_manifest_file(child)
            if native is not None:
                read_native_manifest(native)
                continue
            portable = child / "plugin.json"
            try:
                portable.lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ValueError(f"could not inspect plugin manifest: {portable}") from exc
            else:
                from hermes_cli.agent_plugins import read_agent_plugin_manifest
                read_agent_plugin_manifest(child)
                continue
            if depth == 0:
                validate(child, 1)

    validate(root)
    # A plugin update stages its candidate under ``<plugins_dir>/.update-*/plugin`` (plugins_transaction)
    # while the installed tree is still the enabled member; discovery's one-level category recursion
    # reads that staged manifest too, and matching it by bare name would make the half-staged copy a
    # workspace member (its relative ``[tool.uv.sources]`` paths then resolve from the staging dir).
    # Hidden directories are never installed plugins; keep them out of the PM union.
    return [m for m in scan_directory(root, source, skip_names=skip)
            if not any(part.startswith(".") for part in Path(m.path).relative_to(root).parts)]


def winning_manifests(bundled: list[PluginManifest], plugins_dir: Path) -> list[PluginManifest]:
    """Resolve independently for this home; never mix aliases across profiles."""
    from hermes_cli.plugins_discovery import resolve_manifest_winners
    return list(resolve_manifest_winners(bundled + directory_manifests(plugins_dir, "user")).values())
