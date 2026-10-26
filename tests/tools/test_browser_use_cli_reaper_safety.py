"""Safety regressions using inert processes/sockets only; never send OS signals."""
from __future__ import annotations

import os
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from tools import browser_use_cli_reaper as reaper

pytestmark = pytest.mark.platforms("posix")


@pytest.mark.parametrize("argv", [
    ["python", "-c", "pass", "-m", "browser_harness.daemon"],
    ["python", "-c", "pass", "/owned/browser_harness/daemon.py"],
    ["python", "other.py", "-m", "browser_harness.daemon"],
    ["echo", "-m", "browser_harness.daemon"],
    ["python", "-m", "other", "browser_harness/daemon.py"],
    ["python", "-", "-m", "browser_harness.daemon"],
    ["python", "--", "-m", "browser_harness.daemon"],
    ["python", "-W", "-m", "browser_harness.daemon"],
    ["python", "--unknown", "-m", "browser_harness.daemon"],
    ["python", "-V", "-m", "browser_harness.daemon"],
    ["python", "-m", "browser_harness.daemonish"],
    ["python", "-m"], ["python", "-X"], ["python"], [], None,
    ["python", None, "-m", "browser_harness.daemon"],
    "python -m browser_harness.daemon",
])
def test_rejects_payloads_and_malformed_invocations(argv):
    assert reaper._is_harness_daemon_cmdline(argv) is False


@pytest.mark.parametrize("argv", [
    ["python", "-m", "browser_harness.daemon"],
    ["/venv/bin/python3.14", "-u", "-m", "browser_harness.daemon", "--name", "x"],
    ["python3", "-IB", "-W", "ignore", "-X", "dev", "-m", "browser_harness.daemon"],
    ["python", "-Wignore", "-Xdev", "-m", "browser_harness.daemon"],
    ["python", "--check-hash-based-pycs", "always", "-m", "browser_harness.daemon"],
    ["python", "-m", "browser_harness.daemon", "-c", "payload"],
    ["python", "browser_harness/daemon.py"],
    ["python", "--", "/owned/browser_harness/daemon.py", "argument"],
    ["python", "-u", "/owned/browser_harness/daemon.py", "argument"],
])
def test_preserves_supported_python_invocations(argv):
    assert reaper._is_harness_daemon_cmdline(argv) is True


@pytest.fixture
def inert(monkeypatch, tmp_path):
    state = SimpleNamespace(birth=100.0, uid=os.getuid(), argv=["python", "-m", "browser_harness.daemon"],
                            alive=True, signals=[], runtime=tmp_path, unreadable=False)

    class Process:
        def __init__(self, pid):
            self.pid = pid
            self.birth = state.birth
        def create_time(self):
            if state.unreadable:
                raise psutil.AccessDenied(self.pid)
            return self.birth
        def cmdline(self):
            return state.argv
        def uids(self):
            return SimpleNamespace(real=state.uid)
        def environ(self):
            return {"BH_RUNTIME_DIR": str(state.runtime)}
        def is_running(self):
            return state.alive and self.birth == state.birth
        def send_signal(self, sig):
            if not self.is_running():
                raise psutil.NoSuchProcess(self.pid)
            state.signals.append(sig)
        def terminate(self):
            self.send_signal(signal.SIGTERM)
        def kill(self):
            self.send_signal(signal.SIGKILL)

    monkeypatch.setattr(psutil, "Process", Process)
    monkeypatch.setattr(psutil, "process_iter", lambda *a: [])
    monkeypatch.setattr(reaper, "_pid_alive", lambda pid: state.alive)
    monkeypatch.setattr(reaper.os, "kill", lambda pid, sig: state.signals.append(sig))
    monkeypatch.setattr(reaper, "_SHUTDOWN_EXIT_WAIT_S", 0)
    monkeypatch.setattr(reaper, "_claimed_runtime_dirs", set())
    return state


def test_reused_pid_is_not_killed_after_term(inert, monkeypatch):
    def sent(pid, sig):
        inert.signals.append(sig)
        inert.birth = 200.0
    original = psutil.Process.send_signal
    def process_sent(self, sig):
        original(self, sig)
        inert.birth = 200.0
    monkeypatch.setattr(reaper.os, "kill", sent)
    monkeypatch.setattr(psutil.Process, "send_signal", process_sent)
    reaper._signal_daemon(123)
    assert inert.signals == [signal.SIGTERM]


def test_unreadable_birth_refuses_all_signals(inert):
    inert.unreadable = True
    assert reaper._signal_daemon(123) is False
    assert inert.signals == []


def test_ipc_wait_cannot_authorize_a_recycled_pid(inert, monkeypatch, tmp_path):
    def ipc(*args):
        inert.birth = 200.0
        return True, 123
    monkeypatch.setattr(reaper, "_ipc_shutdown", ipc)
    reaper._stop_daemon(tmp_path, "bu-test", 123)
    assert inert.signals == []


def test_claim_replaced_during_decision_is_not_stopped(inert, monkeypatch, tmp_path):
    env = {"BH_RUNTIME_DIR": str(tmp_path)}
    reaper.claim_session(env, "test")
    (tmp_path / "bu.pid").write_text("123")
    monkeypatch.setattr(reaper, "_sweep_dirs", lambda: [tmp_path])
    def decide(*args, **kwargs):
        (tmp_path / "bu.owner_pid").write_text("999")
        return True, "test decision"
    monkeypatch.setattr(reaper, "_should_reap", decide)
    stop = Mock(return_value=True)
    monkeypatch.setattr(reaper, "_stop_daemon", stop)
    reaper.shutdown_owned_harness_daemons()
    stop.assert_not_called()


def test_stale_pid_claim_does_not_belong_to_new_process(inert, monkeypatch, tmp_path):
    (tmp_path / "bu.owner_pid").write_text(str(os.getpid()))
    (tmp_path / "bu.pid").write_text("123")
    monkeypatch.setattr(reaper, "_sweep_dirs", lambda: [tmp_path])
    stop = Mock(return_value=True)
    monkeypatch.setattr(reaper, "_stop_daemon", stop)
    reaper.shutdown_owned_harness_daemons()
    stop.assert_not_called()


def test_unprotected_socket_is_not_an_identity_proof(inert, monkeypatch, tmp_path):
    import socket
    (tmp_path / "bu.sock").touch()
    connections = []
    class Socket:
        def settimeout(self, timeout): pass
        def connect(self, path): connections.append(path)
        def sendall(self, payload): pass
        def recv(self, size): return b'{"pong":true,"pid":123,"ok":true}\n'
        def close(self): pass
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())
    assert reaper._ipc_shutdown(tmp_path, "bu") == (False, None)
    assert connections == []


@pytest.mark.parametrize("mutation", ["birth", "unreadable", "uid", "argv", "runtime"])
def test_escalation_revalidates_identity_and_scope(inert, monkeypatch, mutation):
    original = psutil.Process.send_signal
    def send(self, sig):
        original(self, sig)
        setattr(inert, mutation, {"birth": 200.0, "unreadable": True, "uid": -1,
                                  "argv": ["python", "other.py"], "runtime": inert.runtime / "foreign"}[mutation])
    monkeypatch.setattr(psutil.Process, "send_signal", send)
    reaper._signal_daemon(123, runtime_dir=inert.runtime)
    assert inert.signals == [signal.SIGTERM]


def test_stable_wedged_daemon_can_be_escalated(inert, monkeypatch):
    original = psutil.Process.send_signal
    def send(self, sig):
        original(self, sig)
        if sig == signal.SIGKILL:
            inert.alive = False
    monkeypatch.setattr(psutil.Process, "send_signal", send)
    assert reaper._signal_daemon(123, runtime_dir=inert.runtime)
    assert inert.signals == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.parametrize("birth", [None, True, 0, -1, float("nan"), float("inf")])
def test_invalid_birth_is_fail_closed(inert, birth):
    inert.birth = birth
    assert reaper._signal_daemon(123) is False
    assert inert.signals == []


@pytest.mark.parametrize("pid", [True, 0, -1, 2**31])
def test_invalid_signal_pid_is_fail_closed(inert, pid):
    assert reaper._signal_daemon(pid) is False
    assert inert.signals == []


@pytest.mark.parametrize("reply_pid", [999, None])
def test_pong_never_redirects_fallback(inert, monkeypatch, reply_pid):
    monkeypatch.setattr(reaper, "_ipc_shutdown", lambda *a: (False, reply_pid))
    reaper._stop_daemon(inert.runtime, "bu", 123)
    if reply_pid == 999:
        assert inert.signals == []
    else:
        assert inert.signals == [signal.SIGTERM, signal.SIGKILL]


def test_other_runtime_indexed_pid_is_not_signalled(inert, monkeypatch, tmp_path):
    inert.runtime = tmp_path / "other"
    monkeypatch.setattr(reaper, "_ipc_shutdown", lambda *a: (False, None))
    assert not reaper._stop_daemon(tmp_path, "bu", 123)
    assert inert.signals == []


def test_ipc_success_precedes_and_avoids_signals(inert, monkeypatch):
    def ipc(*args):
        assert inert.signals == []
        inert.alive = False
        return True, 123
    monkeypatch.setattr(reaper, "_ipc_shutdown", ipc)
    assert reaper._stop_daemon(inert.runtime, "bu", 123)
    assert inert.signals == []


def test_real_claim_is_owned_but_a_later_claim_is_not(inert, monkeypatch, tmp_path):
    (tmp_path / "bu.pid").write_text("123")
    monkeypatch.setattr(reaper, "_sweep_dirs", lambda: [tmp_path])
    stop = Mock(return_value=True)
    monkeypatch.setattr(reaper, "_stop_daemon", stop)
    reaper.claim_session({"BH_RUNTIME_DIR": str(tmp_path)}, "test")
    reaper.shutdown_owned_harness_daemons()
    stop.assert_called_once_with(tmp_path, "bu", 123)
    stop.reset_mock()
    (tmp_path / "bu.owner_pid").write_text("999")
    reaper.shutdown_owned_harness_daemons()
    stop.assert_not_called()


def test_sweep_skips_a_concurrently_locked_claim(inert, monkeypatch, tmp_path):
    import fcntl
    reaper.claim_session({"BH_RUNTIME_DIR": str(tmp_path)}, "test")
    (tmp_path / "bu.pid").write_text("123")
    monkeypatch.setattr(reaper, "_sweep_dirs", lambda: [tmp_path])
    stop = Mock(return_value=True)
    monkeypatch.setattr(reaper, "_stop_daemon", stop)
    with (tmp_path / "bu.owner_lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        reaper.shutdown_owned_harness_daemons()
        stop.assert_not_called()
    reaper.shutdown_owned_harness_daemons()
    stop.assert_called_once()


@pytest.mark.parametrize("idle, expected", [(0, False), (1000000, True)])
def test_reused_live_owner_pid_still_has_bounded_idle_grace(inert, monkeypatch, idle, expected):
    monkeypatch.setattr(reaper, "_idle_seconds", lambda *a: idle)
    reap, _ = reaper._should_reap(inert.runtime, "bu", owner_pid=999, only_owner=None)
    assert reap is expected


@pytest.mark.parametrize("scope", ["ours", "foreign", "unknown", "newborn", "reused", "missing_birth", "indexed"])
def test_unindexed_scope_and_birth_are_preserved(inert, monkeypatch, scope):
    proc = psutil.Process(123)
    proc.info = {"pid": 123, "cmdline": inert.argv, "uids": proc.uids(), "create_time": inert.birth}
    monkeypatch.setattr(psutil, "process_iter", lambda *a: [proc])
    monkeypatch.setattr(reaper, "_sweep_dirs", lambda: [inert.runtime])
    if scope == "foreign":
        monkeypatch.setattr(proc, "environ", lambda: {"BH_RUNTIME_DIR": str(inert.runtime / "foreign")})
    elif scope == "unknown":
        monkeypatch.setattr(proc, "environ", dict)
    elif scope == "newborn":
        proc.info["create_time"] = reaper.time.time()
    elif scope == "missing_birth":
        proc.info["create_time"] = None
    elif scope == "reused":
        inert.birth = 200.0
    elif scope == "indexed":
        (inert.runtime / "bu.pid").write_text("123")
    reaper._sweep_unindexed()
    assert inert.signals == ([signal.SIGTERM, signal.SIGKILL] if scope == "ours" else [])


@pytest.mark.parametrize("mode,uid,expected", [(0o600, None, True), (0o666, None, False), (0o600, -1, False)])
def test_ipc_enforces_actual_socket_protection(inert, monkeypatch, tmp_path, mode, uid, expected):
    import socket
    import stat
    from pathlib import Path
    sock = tmp_path / "bu.sock"
    real_lstat = Path.lstat
    def lstat(path):
        if path == sock:
            return SimpleNamespace(st_mode=stat.S_IFSOCK | mode, st_uid=os.getuid() if uid is None else uid)
        return real_lstat(path)
    monkeypatch.setattr(Path, "lstat", lstat)
    messages = []
    class Socket:
        def settimeout(self, value): pass
        def connect(self, path): assert path == str(sock)
        def sendall(self, payload): messages.append(payload)
        def recv(self, size): return b'{"pong":true,"pid":123,"ok":true}\n'
        def close(self): pass
    monkeypatch.setattr(socket, "socket", lambda *a: Socket())
    assert reaper._ipc_shutdown(tmp_path, "bu") == ((True, 123) if expected else (False, None))
    assert len(messages) == (2 if expected else 0)
