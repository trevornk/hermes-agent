"""A removed worker worktree must not break context rebuilding or change project authority."""

import logging
import subprocess
from types import SimpleNamespace

import pytest

from agent import prompt_builder as pb
from agent.context_file_sources import list_context_file_sources
from agent.runtime_cwd import reset_session_cwd, set_session_cwd
from agent.system_prompt import _context_files_part


@pytest.fixture
def soul_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "SOUL.md").write_text("My isolated soul identity.")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def deleted_cwd(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("Parent project authority must not load.")
    launch = tmp_path / "launch"
    launch.mkdir()
    with monkeypatch.context() as scoped:
        scoped.chdir(launch)
        launch.rmdir()
        yield


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("skip_soul", [False, True])
def test_prompt_skips_deleted_cwd_project_context(soul_home, deleted_cwd, skip_soul, caplog):
    with caplog.at_level(logging.WARNING, logger=pb.__name__):
        prompt = pb.build_context_files_prompt(cwd=None, skip_soul=skip_soul)
    assert ("My isolated soul identity." in prompt) is not skip_soul
    assert "Parent project authority" not in prompt
    assert "AGENTS.md" not in prompt
    if skip_soul:
        assert prompt == ""
    warnings = [r.message for r in caplog.records if r.name == pb.__name__]
    assert len(warnings) == 1
    assert "deleted" in warnings[0] and "project-context" in warnings[0]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("soul_loaded", [False, True])
def test_compression_context_rebuild_after_worktree_removal(tmp_path, monkeypatch, soul_home, soul_loaded):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    git("init")
    (repo / "AGENTS.md").write_text("Main repository authority must not load.")
    git("add", "AGENTS.md")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed")
    worktree = tmp_path / "worker"
    git("worktree", "add", "-b", "worker", str(worktree))
    (worktree / "AGENTS.md").write_text("Removed worktree authority must not load.")
    monkeypatch.setenv("TERMINAL_CWD", str(worktree))
    token = set_session_cwd(str(worktree))
    try:
        with monkeypatch.context() as scoped:
            scoped.chdir(worktree)
            git("worktree", "remove", "--force", str(worktree))
            agent = SimpleNamespace(skip_context_files=False, platform="cli")
            prompt = "\n".join(_context_files_part(agent, ctx_len=None, soul_loaded=soul_loaded))
            assert "Removed worktree authority" not in prompt
            assert "Main repository authority" not in prompt
            assert "AGENTS.md" not in prompt
            assert ("My isolated soul identity." in prompt) is not soul_loaded
    finally:
        reset_session_cwd(token)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("skip_soul", [False, True])
def test_manifest_skips_deleted_cwd_project_sources(soul_home, deleted_cwd, skip_soul):
    sources = list_context_file_sources(cwd=None, skip_soul=skip_soul)
    assert [source["label"] for source in sources] == ([] if skip_soul else ["SOUL.md"])
    if not skip_soul:
        assert sources[0]["loaded"] is True
        assert sources[0]["path"] == str(soul_home / "SOUL.md")


@pytest.mark.parametrize("reader", [pb.build_context_files_prompt, list_context_file_sources])
def test_other_getcwd_errors_propagate(monkeypatch, soul_home, reader):
    error = PermissionError("cwd access denied")

    def denied():
        raise error

    with monkeypatch.context() as scoped:
        scoped.setattr(pb.os, "getcwd", denied)
        with pytest.raises(PermissionError) as caught:
            reader(cwd=None)
    assert caught.value is error


@pytest.mark.parametrize("reader", [pb.build_context_files_prompt, list_context_file_sources])
def test_explicit_cwd_does_not_consult_process_cwd(tmp_path, monkeypatch, soul_home, reader):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("Explicit project authority.")

    def missing():
        raise FileNotFoundError("deleted process cwd")

    with monkeypatch.context() as scoped:
        scoped.setattr(pb.os, "getcwd", missing)
        result = reader(cwd=str(tmp_path), skip_soul=True)
    if isinstance(result, str):
        assert "Explicit project authority." in result
    else:
        assert [(s["label"], s["loaded"]) for s in result] == [("AGENTS.md", True)]
