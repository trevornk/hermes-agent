"""Scratch cleanup ignores archived/empty boards and safely retries terminal tasks."""
import argparse
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return tmp_path


def _sibling(path, *, metadata=None, contents="live", slug="other"):
    directory = kb.boards_root() / slug
    directory.mkdir(parents=True)
    if metadata is not None:
        (directory / "board.json").write_text(json.dumps(metadata), encoding="utf-8")
    db = directory / "kanban.db"
    if contents == "live":
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, workspace_path TEXT)")
            conn.execute("INSERT INTO tasks VALUES ('sibling', 'ready', ?)", (str(path),))
    else:
        db.write_bytes(b"" if contents == "empty" else b"not a sqlite database")
    return directory


@pytest.mark.parametrize(
    "metadata, contents, expected",
    [
        ({"archived": True}, "empty", None),
        ({"archived": False}, "empty", None),
        ({"archived": False}, "live", "shared"),
        ({"archived": False}, "corrupt", "unknown"),
        # Archived boards are deliberately not consulted, even with live rows.
        ({"archived": True}, "live", None),
        ({"archived": "true"}, "live", "shared"),
        ([], "live", "shared"),
    ],
)
def test_sibling_board_cleanup_decision(board, metadata, contents, expected):
    path = kb.workspaces_root() / "leftover"
    _sibling(path, metadata=metadata, contents=contents)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="finished")
        assert kbw._workspace_in_use_by_other(conn, tid, path) == expected


@pytest.mark.parametrize("unreadable", [False, True])
def test_bad_board_metadata_still_scans_live_tasks(board, monkeypatch, unreadable):
    path = kb.workspaces_root() / "leftover"
    directory = _sibling(path)
    metadata_path = directory / "board.json"
    metadata_path.write_text("invalid json", encoding="utf-8")
    if unreadable:
        read_text = Path.read_text

        def read(self, *args, **kwargs):
            if self == metadata_path:
                raise PermissionError("unreadable board metadata")
            return read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="finished")
        assert kbw._workspace_in_use_by_other(conn, tid, path) == "shared"


def test_default_board_is_consulted_even_with_archived_metadata(board):
    path = kb.workspaces_root() / "leftover"
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="live on default")
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(path), tid))
        conn.commit()
    (kb.kanban_home() / "board.json").write_text('{"archived": true}', encoding="utf-8")
    kb.create_board("other")
    with kbc.connect_closing(board="other") as conn:
        tid = kb.create_task(conn, title="finished on named")
        assert kbw._workspace_in_use_by_other(conn, tid, path) == "shared"


def test_archive_storage_is_not_a_board(board):
    path = kb.workspaces_root() / "leftover"
    _sibling(path, contents="corrupt", slug="_archived/old")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="finished")
        assert kbw._workspace_in_use_by_other(conn, tid, path) is None


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "archived"])
def test_gc_removes_terminal_scratch_and_releases_lsp(board, monkeypatch, capsys, status):
    path = kb.workspaces_root() / "leftover"
    path.mkdir(parents=True)
    with kbc.connect_closing() as conn:
        _task(conn, status, path)
    def release_before_removal(value):
        assert value == str(path)
        assert path.is_dir()

    release = Mock(side_effect=release_before_removal)
    monkeypatch.setattr(kbw, "release_lsp_clients", release)
    assert kanban_ops._cmd_gc(_args()) == 0
    assert not path.exists()
    release.assert_called_once_with(str(path))
    assert "1 workspace(s)" in capsys.readouterr().out


def _task(conn, status, path, kind="scratch"):
    tid = kb.create_task(conn, title="workspace owner")
    conn.execute(
        "UPDATE tasks SET status=?, workspace_kind=?, workspace_path=? WHERE id=?",
        (status, kind, str(path), tid),
    )
    conn.commit()
    return tid


def _args():
    return argparse.Namespace(event_retention_days=0, log_retention_days=0)


@pytest.mark.parametrize(
    "guard",
    ["child", "shared", "unknown", "outside",
     pytest.param("symlink", marks=pytest.mark.require_symlinks), "root"],
)
@pytest.mark.parametrize("status", ["done", "archived"])
def test_gc_preserves_guarded_scratch(board, capsys, guard, status):
    path = kb.workspaces_root() / "leftover"
    if guard == "outside":
        path = board / "user-project"
    if guard == "root":
        path = kb.workspaces_root()
    path.mkdir(parents=True)
    (path / "handoff.txt").write_text("still needed", encoding="utf-8")
    target = path
    if guard == "symlink":
        path = kb.workspaces_root() / "link"
        path.symlink_to(target, target_is_directory=True)
    with kbc.connect_closing() as conn:
        tid = _task(conn, status, path)
        if guard == "child":
            child = kb.create_task(conn, title="active child")
            kb.link_tasks(conn, tid, child)
    if guard in ("shared", "unknown"):
        _sibling(path, contents="live" if guard == "shared" else "corrupt")
    assert kanban_ops._cmd_gc(_args()) == 0
    assert (target / "handoff.txt").read_text(encoding="utf-8-sig") == "still needed"
    assert path.is_dir()
    assert "0 workspace(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "status,kind",
    [(s, "scratch") for s in ("running", "ready", "blocked")] + [("done", "worktree")],
)
def test_gc_leaves_nonterminal_tasks_and_done_worktrees(board, monkeypatch, status, kind):
    path = kb.workspaces_root() / "leftover"
    path.mkdir(parents=True)
    with kbc.connect_closing() as conn:
        _task(conn, status, path, kind)
    cleanup = Mock()
    monkeypatch.setattr(kbw, "_cleanup_worktree_workspace", cleanup)
    assert kanban_ops._cmd_gc(_args()) == 0
    assert path.is_dir()
    cleanup.assert_not_called()
