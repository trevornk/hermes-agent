"""Safe-updater invocation contracts against disposable real-PM launcher fixtures.

Uses upstream launcher fixtures but does not rerun their existing tests. No
installer, live launcher, service command, or network operation is executed.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest
from hermes_cli import _launchers
from tests.hermes_cli.test_source_launcher_publication import fixture_tree, select_generation

SCRIPTS = Path(__file__).resolve().parents[1] / '_fixtures/safe_update_pm'
HELPER = SCRIPTS / 'lib/hermes-update-runtime.sh'


def call_runtime(repo, home, mode, *args, stdin=None):
    command = 'set -uo pipefail; source "$1"; AISERVER_HOME="$2"; hermes_runtime_run "$3" "$4" "${@:5}"'
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run(['/bin/bash', '-c', command, 'fixture', str(HELPER), str(home.parent), str(repo), mode, *args],
                          input=stdin, text=True, capture_output=True, env=env, cwd=repo, timeout=20, check=False)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    repo, home, interpreter = fixture_tree(tmp_path, monkeypatch)
    (repo/'pm/lock.json').write_text('{}')
    launcher_dir = repo/'.hermes/bin'
    launcher_dir.mkdir(parents=True)
    _launchers._mint_shell_launcher('hermes', launcher_dir, interpreter,
                                    _launchers._launcher_script('hermes', repo, None))
    select_generation(repo, 1, 'first')
    return repo, home


def test_cli_and_plugin_admission_keep_exact_arguments(installed):
    repo, home = installed
    args=['--profile','quant','plugins','install','hindsight','--enable']
    result=call_runtime(repo,home,'cli',*args,stdin='')
    assert result.returncode == 7, result.stderr
    row=json.loads(result.stdout)
    assert row['argv'] == args
    assert row['value'] == 'first'
    assert row['home'] == str(home)


def test_stdin_preserves_payload_arguments_and_selected_runtime(installed):
    repo,home=installed
    result=call_runtime(repo,home,'stdin','spaces and café', '', stdin="import json,sys,selected_probe; print(json.dumps([sys.argv[1:],selected_probe.VALUE]))\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [['spaces and café',''],'first']


def test_launch_resolves_new_generation_not_cached_python(installed):
    repo,home=installed
    assert json.loads(call_runtime(repo,home,'cli').stdout)['value']=='first'
    select_generation(repo,2,'second')
    assert json.loads(call_runtime(repo,home,'cli').stdout)['value']=='second'


def test_missing_pm_launcher_never_falls_back(installed):
    repo,home=installed
    (repo/'.hermes/bin/hermes').rename(repo/'.hermes/bin/held-hermes')
    (repo/'venv/bin').mkdir(parents=True)
    trap=repo/'venv/bin/python'
    trap.write_text('#!/bin/sh\nprintf legacy-was-invoked\nexit 0\n');trap.chmod(0o755)
    result=call_runtime(repo,home,'cli')
    assert result.returncode != 0
    assert 'launcher missing' in result.stderr
    assert 'legacy-was-invoked' not in result.stdout


def test_compile_module_failure_propagates(installed):
    repo,home=installed
    source=repo/'bad.py';source.write_text('def broken(:\n')
    result=call_runtime(repo,home,'module','py_compile',str(source))
    assert result.returncode != 0
    assert 'SyntaxError' in result.stderr
    source.write_text('VALUE=1\n')
    result=call_runtime(repo,home,'module','py_compile',str(source))
    assert result.returncode == 0, result.stderr


@pytest.mark.live_system_guard_bypass
# Narrow fixture exception: this test only PRINTS the constructed argv. It never
# executes the updater (or any Hermes command); source/target are test-owned.
def test_legacy_initial_handoff_is_distinct(tmp_path):
    repo=tmp_path/'legacy'; home=tmp_path/'.hermes'
    (repo/'venv/bin').mkdir(parents=True);home.mkdir()
    python=repo/'venv/bin/python'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 23\n');python.chmod(0o755)
    command = '''source "$1"; hermes_runtime_command "$2" cli update --yes --no-backup --branch pinned-fixture; printf '%s\\n' "${HERMES_RUNTIME_COMMAND[@]}"'''
    result=subprocess.run(['/bin/bash','-c',command,'fixture',str(HELPER),str(repo)],text=True,capture_output=True,timeout=10,check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()==[str(python),'-m','hermes_cli.main','update','--yes','--no-backup','--branch','pinned-fixture']


def test_existing_checkout_only_rollback_does_not_restore_pm_selection(installed,tmp_path):
    repo,home=installed
    saved=tmp_path/'snapshot';shutil.copytree(repo,saved)
    select_generation(repo,2,'second')
    # Same rename restoration as safe updater; importantly PM state is external.
    repo.rename(tmp_path/'failed-candidate'); saved.rename(repo)
    result=call_runtime(repo,home,'cli')
    assert result.returncode==7, result.stderr
    assert json.loads(result.stdout)['value']=='second'


def test_checkout_snapshot_does_not_restore_published_external_link(tmp_path):
    repo = tmp_path / 'legacy-install'
    old = repo / 'venv/bin/hermes'
    old.parent.mkdir(parents=True)
    old.write_text('old console entry')
    external = tmp_path / 'user-bin/hermes'
    external.parent.mkdir()
    external.symlink_to(old)
    snapshot = tmp_path / 'old-snapshot'
    shutil.copytree(repo, snapshot)
    new = repo / '.hermes/bin/hermes'
    new.parent.mkdir(parents=True)
    new.write_text('published PM entry')
    external.rename(external.with_name('held-old-link'))
    external.symlink_to(new)
    repo.rename(tmp_path / 'failed-install')
    snapshot.rename(repo)
    assert old.exists()
    assert external.is_symlink() and not external.exists()


def test_web_build_payload_uses_selected_generation_and_propagates_failure(installed):
    repo,home=installed
    # The body of the <<'PYWEB' heredoc hermes-safe-update.sh pipes through hermes_runtime_run
    # (stdin mode), kept as its own fixture rather than a copy of the whole updater.
    payload=(SCRIPTS/'web_build_payload.py').read_text()
    main=repo/'hermes_cli/main.py'
    main.write_text('')
    (repo/'hermes_cli/main_web_build.py').write_text(
        "import selected_probe\n"
        "def _web_ui_build_needed(path): return True\n"
        "def _build_web_ui(path, *, fatal):\n"
        "    print('BUILD-RUNTIME='+selected_probe.VALUE)\n"
        "    return selected_probe.VALUE == 'first'\n")
    result=call_runtime(repo,home,'stdin',stdin=payload)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'BUILD-RUNTIME=first' in result.stdout
    select_generation(repo,2,'second')
    result=call_runtime(repo,home,'stdin',stdin=payload)
    assert result.returncode!=0
    assert 'BUILD FAILED' in result.stdout
