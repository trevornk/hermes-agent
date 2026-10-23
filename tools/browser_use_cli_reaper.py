"""Browser Use harness daemon lifecycle: owner claim, idle/orphan reaping, exit teardown.

``browser_harness`` starts its daemon with ``start_new_session=True`` and expects it to
OUTLIVE the CLI invocation that spawned it — that detachment is what makes a session
persist across ``browser_exec`` calls. So the daemon is reparented to init on purpose;
``ppid=1`` is the design, not the leak, and a parent-death signal (pipe EOF, process
group, ``PR_SET_PDEATHSIG``) would kill the browser after every single tool call.

The leak is that nothing ever reclaimed those daemons. ``browser_tool_lifecycle`` owns
the same three mechanisms for the ``agent-browser`` lane — an owner-pid claim, an idle
grace, and teardown at exit — and the harness lane had none of them, so every distinct
``BU_NAME`` left a daemon holding a CDP connection until reboot.

Stopping is IPC-first: ``meta: shutdown`` is the only path that lets the daemon stop a
billable Browser Use cloud browser before it dies, and answering ``{"pong": true}`` on
its own socket is itself proof of identity. A signal is the fallback for a daemon whose
socket is already gone, and is sent only to a PID that is ours, still alive, and running
``browser_harness.daemon``. Chrome is NOT a child of this daemon (Hermes always exports a
``BU_CDP_*`` endpoint, so the harness never launches its own browser) — the agent-browser
lane reaps the browser, this one reaps the daemon holding the socket to it.
"""

import contextlib
import math
import os
import stat
import signal
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, Iterator, List, Optional, Set, Tuple

if TYPE_CHECKING:
    import psutil

from tools.browser_tool_origin import origin as _bt

# Runtime dirs we have claimed a session in, so the janitor sweeps the same dirs the CLI
# actually used even when an operator points a profile at a custom ``BH_RUNTIME_DIR``.
_claimed_runtime_dirs: set[str] = set()
_claim_lock = threading.Lock()
_owned_claims: dict[tuple[str, str], tuple[int, int, int, int]] = {}

# A daemon answering ping gets this long to run its cloud-stop and exit before the signal
# fallback; a wedged one must not hold the janitor thread.
_SHUTDOWN_IPC_TIMEOUT_S = 5.0
_SHUTDOWN_EXIT_WAIT_S = 10.0


def _truthy_env(env: dict[str, str], key: str) -> bool:
    return str(env.get(key, "")).strip() == "1"


def harness_runtime_dir(env: Optional[dict[str, str]] = None) -> Optional[Path]:
    """Resolve the dir holding ``bu-*.sock``/``.pid``, mirroring ``browser_harness.paths``.

    Precedence (``_ipc.py``): ``BH_RUNTIME_DIR`` → ``BH_TMP_DIR`` → ``BH_HOME`` /
    ``BROWSER_HARNESS_HOME`` / ``XDG_CONFIG_HOME``-derived home + ``runtime``. Resolved from
    the env dict handed to the CLI so a profile-scoped override lands in the same dir the
    subprocess will use.
    """
    src = os.environ if env is None else env
    for key in ("BH_RUNTIME_DIR", "BH_TMP_DIR"):
        raw = src.get(key)
        if raw:
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                return Path(raw).expanduser().resolve()
            return None

    home_raw = src.get("BH_HOME") or src.get("BROWSER_HARNESS_HOME")
    try:
        if home_raw:
            return Path(home_raw).expanduser().resolve() / "runtime"
        xdg = src.get("XDG_CONFIG_HOME")
        if xdg:
            return (Path(xdg).expanduser() / "browser-harness").resolve() / "runtime"
        return (Path.home() / ".config" / "browser-harness").resolve() / "runtime"
    except (OSError, RuntimeError, ValueError) as exc:
        _bt.logger.debug("Could not resolve browser-harness runtime dir: %s", exc)
        return None


def _runtime_stem(env: dict[str, str], session: str) -> str:
    """``bu`` when a caller-supplied runtime dir isolates one daemon, else ``bu-<name>``
    (``browser_harness._ipc._runtime_stem``)."""
    isolated = bool(env.get("BH_RUNTIME_DIR") or env.get("BH_TMP_DIR"))
    shared = _truthy_env(env, "BH_RUNTIME_DIR_SHARED") or _truthy_env(env, "BH_TMP_DIR_SHARED")
    return "bu" if isolated and not shared else f"bu-{session or 'default'}"


def _owner_path(runtime_dir: Path, stem: str) -> Path:
    return runtime_dir / f"{stem}.owner_pid"


def claim_session(env: dict[str, str], session: str) -> None:
    """Record this process as the owner of ``session``'s daemon, before the CLI spawns it.

    Rewritten on EVERY call, so the file's mtime doubles as a restart-proof activity marker
    (the socket's mtime never moves, and the daemon log is only appended on notable events).
    Best-effort: an unwritable runtime dir just leaves the daemon on the legacy
    idle-only path rather than failing the tool call.
    """
    runtime_dir = harness_runtime_dir(env)
    if runtime_dir is None:
        return
    stem = _runtime_stem(env, session)
    try:
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _session_guard(runtime_dir, stem, blocking=True) as locked:
            if not locked:
                return
            claim = _owner_path(runtime_dir, stem)
            claim.write_text(str(os.getpid()), encoding="utf-8")
            snapshot = _claim_snapshot(claim)
            if snapshot is not None:
                with _claim_lock:
                    _owned_claims[(str(runtime_dir), stem)] = (os.getpid(), *snapshot)
    except OSError as exc:
        _bt.logger.debug("Could not claim harness session %s: %s", stem, exc)
        return
    with _claim_lock:
        _claimed_runtime_dirs.add(str(runtime_dir))


@contextlib.contextmanager
def _session_guard(runtime_dir: Path, stem: str, *, blocking: bool = False) -> Iterator[bool]:
    """Serialize claim publication with teardown; never unlink the lock inode.

    Janitors skip contention. A tool claim waits for the bounded stop to finish,
    then publishes before its CLI invocation. Unsupported locking fails closed.
    """
    try:
        import fcntl
        fd = os.open(runtime_dir / f"{stem}.owner_lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (ImportError, AttributeError, OSError) as exc:
        _bt.logger.debug("Cannot lock harness claim %s: %s", stem, exc)
        yield False
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            yield False
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _claim_snapshot(path: Path) -> Optional[tuple[int, int, int]]:
    try:
        info = path.stat()
        return info.st_ino, info.st_mtime_ns, info.st_size
    except OSError:
        return None


def _sweep_dirs() -> list[Path]:
    """Runtime dirs to scan: every dir we claimed in, plus this env's default."""
    with _claim_lock:
        dirs = {d for d in _claimed_runtime_dirs}
    default = harness_runtime_dir()
    if default is not None:
        dirs.add(str(default))
    return [Path(d) for d in sorted(dirs)]


def _read_pid(path: Path) -> Optional[int]:
    """Integer PID from ``path``; None when missing, corrupt, or out of range.

    The upper bound matches ``browser_harness._ipc.identify``: a value outside signed
    32-bit ``pid_t`` makes ``os.kill`` raise ``OverflowError``, and 0/negatives address a
    process GROUP rather than one process.
    """
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if 0 < pid < (1 << 31) else None


def _pid_alive(pid: int) -> bool:
    from gateway.status import _pid_exists
    return bool(_pid_exists(pid))


def _idle_seconds(paths: Iterable[Path]) -> Optional[float]:
    """Seconds since the most recent of ``paths`` was written; None when none exist."""
    newest = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return None if newest is None else max(0.0, time.time() - newest)


def _ipc_shutdown(runtime_dir: Path, stem: str) -> tuple[bool, Optional[int]]:
    """Ask the daemon to stop over its own socket. Returns ``(acknowledged, daemon_pid)``.

    Answering ``{"pong": true, "pid": N}`` on a 0600 socket in a 0700 dir IS the identity
    proof, so no separate verification is needed on this path. This is also the ONLY stop
    that runs the daemon's cloud-browser cleanup — a signal here would leave a billable
    browser running.
    """
    sock = runtime_dir / f"{stem}.sock"
    try:
        directory = runtime_dir.lstat()
        endpoint = sock.lstat()
        uid = os.getuid()
        if (not stat.S_ISDIR(directory.st_mode) or not stat.S_ISSOCK(endpoint.st_mode)
                or directory.st_uid != uid or endpoint.st_uid != uid
                or directory.st_mode & 0o077 or endpoint.st_mode & 0o077):
            return False, None
    except (AttributeError, OSError):
        return False, None

    import json
    import socket as _socket

    def _request(payload: dict) -> Optional[dict]:
        try:
            conn = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        except (AttributeError, OSError):
            return None  # no AF_UNIX (Windows): caller falls back to the signal path
        try:
            conn.settimeout(_SHUTDOWN_IPC_TIMEOUT_S)
            conn.connect(str(sock))
            conn.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
            parsed = json.loads(data or b"{}")
            return parsed if isinstance(parsed, dict) else None
        except (OSError, ValueError):
            return None
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    pong = _request({"meta": "ping"})
    if not pong or pong.get("pong") is not True:
        return False, None
    reported = pong.get("pid")
    # ``type(...) is int`` rejects bool: isinstance(True, int) is True, and {"pid": True}
    # would be read as PID 1.
    daemon_pid = reported if type(reported) is int and 0 < reported < (1 << 31) else None

    resp = _request({"meta": "shutdown"})
    return bool(resp and resp.get("ok") is True), daemon_pid


def _is_harness_daemon_cmdline(cmdline: Optional[list[str]]) -> bool:
    """Parse Python's interpreter prefix, never scan program payload arguments.

    Unknown interpreter options fail closed. The executable name is only a syntax
    gate; signalling also requires same-user, runtime and stable process identity.
    """
    import re

    if not isinstance(cmdline, (list, tuple)) or len(cmdline) < 2:
        return False
    if any(not isinstance(arg, str) or not arg or "\0" in arg for arg in cmdline):
        return False
    executable = cmdline[0].replace("\\", "/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:[dt])?(?:\.exe)?", executable, re.IGNORECASE):
        return False
    i = 1
    while i < len(cmdline):
        arg = cmdline[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-"):
            break
        if arg == "-m":
            return i + 1 < len(cmdline) and cmdline[i + 1] == "browser_harness.daemon"
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            if i + 1 >= len(cmdline) or cmdline[i + 1].startswith("-"):
                return False
            if arg == "--check-hash-based-pycs" and cmdline[i + 1] not in ("default", "always", "never"):
                return False
            i += 2
            continue
        if arg.startswith(("-W", "-X")) and len(arg) > 2:
            i += 1
            continue
        if len(arg) > 1 and all(flag in "bBdEiIOPqRsSuvx" for flag in arg[1:]):
            i += 1
            continue
        return False  # -c, stdin, help/version, or an unsupported option
    if i >= len(cmdline):
        return False
    script = cmdline[i].replace("\\", "/")
    return script == "browser_harness/daemon.py" or script.endswith("/browser_harness/daemon.py")


def _daemon_process(pid: int) -> Optional[tuple["psutil.Process", float]]:
    """Capture PID + birth time together with the existing user/argv proof."""
    try:
        import psutil
    except ImportError:
        return None
    if type(pid) is not int or not 0 < pid < (1 << 31) or not hasattr(os, "getuid"):
        return None
    try:
        proc = psutil.Process(pid)
        birth = proc.create_time()
        if not isinstance(birth, (int, float)) or isinstance(birth, bool) or not math.isfinite(birth) or birth <= 0:
            return None
        if proc.uids().real != os.getuid() or not _is_harness_daemon_cmdline(proc.cmdline()):
            return None
        if not proc.is_running():
            return None
        return proc, birth
    except (psutil.Error, OSError, ValueError) as exc:
        _bt.logger.debug("Refusing harness PID %d: identity unreadable (%s)", pid, exc)
        return None


def _verified_daemon_pid(pid: int) -> bool:
    return _daemon_process(pid) is not None


def _signal_daemon(pid: int, *, expected_birth: Optional[float] = None,
                   runtime_dir: Optional[Path] = None) -> bool:
    """Signal one stable identity; revalidate before TERM and before KILL.

    psutil's retained Process adds its own PID-reuse check at send_signal. Never
    use a fresh identity to authorize escalation or walk descendants.
    """
    import psutil

    identity = _daemon_process(pid)
    if identity is None:
        return False
    proc, birth = identity
    if expected_birth is not None and birth != expected_birth:
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        current = _daemon_process(pid)
        if current is None or current[1] != birth:
            return False
        if runtime_dir is not None and _daemon_runtime_dir(current[0]) != runtime_dir:
            return False
        try:
            if not proc.is_running():
                return False
            proc.send_signal(sig)
            deadline = time.monotonic() + _SHUTDOWN_EXIT_WAIT_S
            while time.monotonic() < deadline:
                if not proc.is_running():
                    return True
                time.sleep(0.2)
        except psutil.NoSuchProcess:
            return True  # psutil refused to signal a vanished/recycled identity
        except (psutil.Error, OSError) as exc:
            _bt.logger.warning("Could not signal harness daemon PID %d: %s", pid, exc)
            return False
    return not proc.is_running()


def _clear_endpoint(runtime_dir: Path, stem: str) -> None:
    for suffix in (".sock", ".pid", ".port", ".owner_pid"):
        with contextlib.suppress(OSError):
            (runtime_dir / f"{stem}{suffix}").unlink()


def _stop_daemon(runtime_dir: Path, stem: str, pid: Optional[int]) -> bool:
    """IPC first, but capture fallback identity BEFORE any shutdown/wait gap."""
    identity = _daemon_process(pid) if pid is not None else None
    if identity is not None and _daemon_runtime_dir(identity[0]) != runtime_dir:
        identity = None
    acknowledged, reported_pid = _ipc_shutdown(runtime_dir, stem)
    # A pong cannot redirect a signal to a PID we never captured.
    if reported_pid is not None and reported_pid != pid:
        return False
    if acknowledged and pid is not None:
        deadline = time.monotonic() + _SHUTDOWN_EXIT_WAIT_S
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.2)
    stopped = acknowledged and (pid is None or not _pid_alive(pid))
    if not stopped and identity is not None and pid is not None:
        stopped = _signal_daemon(pid, expected_birth=identity[1], runtime_dir=runtime_dir)
    if stopped or (pid is not None and not _pid_alive(pid)):
        _clear_endpoint(runtime_dir, stem)
    return stopped


def _sessions_in(runtime_dir: Path) -> list[str]:
    """Stems of every daemon with a pid file in ``runtime_dir``."""
    try:
        return sorted(p.name[: -len(".pid")] for p in runtime_dir.glob("bu*.pid"))
    except OSError:
        return []


def _should_reap(runtime_dir: Path, stem: str, *, owner_pid: Optional[int],
                 only_owner: Optional[int]) -> tuple[bool, str]:
    """Reap decision for one daemon: ``(reap, reason)``.

    ``only_owner`` restricts the sweep to daemons claimed by that PID (exit teardown).
    Otherwise: a dead owner means nobody will ever reclaim the daemon — reap now; a live
    owner (this process or another Hermes sharing the runtime dir) keeps its daemon until
    it goes idle past the grace, which is the same escape hatch the agent-browser lane
    uses so a leaked claim cannot make a daemon immortal; a missing/corrupt claim (a
    legacy daemon, or a failed claim write) is idle-gated rather than trusted.
    """
    if only_owner is not None:
        return (owner_pid == only_owner, "owner exiting")

    if owner_pid is not None and not _pid_alive(owner_pid):
        return True, f"owner PID {owner_pid} is gone"

    idle = _idle_seconds([
        _owner_path(runtime_dir, stem),
        runtime_dir / f"{stem}.pid",
        runtime_dir / f"{stem}.sock",
    ])
    grace = _bt.BROWSER_ORPHAN_GRACE_SECONDS
    if idle is None or idle < grace:
        return False, ""  # unknown age or within grace — fail safe
    if owner_pid is None:
        return True, f"unclaimed and idle for {int(idle)}s (grace {grace}s)"
    return True, f"owner PID {owner_pid} is live but the session is idle for {int(idle)}s (grace {grace}s)"


def _pid_file_recent(path: Path) -> bool:
    """True when ``path`` exists and was written within the grace (so an unparseable
    value is more likely a write in flight than a corpse)."""
    idle = _idle_seconds([path])
    return idle is not None and idle < _bt.BROWSER_ORPHAN_GRACE_SECONDS


def _sweep_session(runtime_dir: Path, stem: str, only_owner: Optional[int]) -> bool:
    """Called with the same interprocess lock used by claim_session held."""
    claim = _owner_path(runtime_dir, stem)
    snapshot = _claim_snapshot(claim)
    owner_pid = _read_pid(claim)
    if only_owner is not None:
        with _claim_lock:
            owned = _owned_claims.get((str(runtime_dir), stem))
        if snapshot is None or owned != (only_owner, *snapshot) or owner_pid != only_owner:
            return False
    pid_path = runtime_dir / f"{stem}.pid"
    pid = _read_pid(pid_path)
    if pid is None and _pid_file_recent(pid_path):
        return False
    if pid is None or not _pid_alive(pid):
        _clear_endpoint(runtime_dir, stem)
        return False
    reap, reason = _should_reap(runtime_dir, stem, owner_pid=owner_pid, only_owner=only_owner)
    # Also catch a legacy writer that does not participate in the advisory lock.
    if not reap or _claim_snapshot(claim) != snapshot or _read_pid(claim) != owner_pid:
        return False
    if _read_pid(pid_path) != pid:
        return False
    if _stop_daemon(runtime_dir, stem, pid):
        _bt.logger.info("Stopped browser-harness daemon PID %d (%s): %s", pid, stem, reason)
        return True
    return False


def _sweep(only_owner: Optional[int]) -> int:
    reaped = 0
    for runtime_dir in _sweep_dirs():
        for stem in _sessions_in(runtime_dir):
            try:
                with _session_guard(runtime_dir, stem) as locked:
                    if locked and _sweep_session(runtime_dir, stem, only_owner):
                        reaped += 1
            except Exception as exc:  # one broken endpoint must not abort the sweep
                _bt.logger.warning("Error stopping browser-harness daemon %s: %s", stem, exc)
    return reaped


def _daemon_runtime_dir(proc) -> Optional[Path]:
    """Runtime dir ``proc`` was started with, from its OWN environment; None if unknowable.

    The harness resolves its dir from ``BH_RUNTIME_DIR``/``BH_TMP_DIR``/``BH_HOME``/
    ``XDG_CONFIG_HOME`` at spawn, and the process keeps that environment, so re-running the
    same resolution over it names the dir holding its pid file — without enumerating other
    profiles' dirs. ``environ()`` is unavailable for some processes (macOS hides it for
    others' processes, and for hardened ones), in which case the answer is None and the
    caller must not signal.
    """
    import psutil

    try:
        env = proc.environ()
    except (psutil.Error, OSError):
        return None
    if not env:
        return None
    explicit = any(env.get(k) for k in ("BH_RUNTIME_DIR", "BH_TMP_DIR", "BH_HOME",
                                        "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME"))
    # The ``~/.config`` fallback expands THIS process's HOME; only trust it when the
    # daemon was started with the same one.
    if not explicit and env.get("HOME") != os.environ.get("HOME"):
        return None
    return harness_runtime_dir(env)


def _sweep_unindexed() -> int:
    """Stop harness daemons that no longer have a pid file in a dir we sweep.

    Observed live: a daemon kept running with its ``bu-<name>.sock``/``.pid`` already
    unlinked (a ``--reload``/restart path removes the endpoint, and a daemon that outlives
    that removal is unreachable over IPC and invisible to every file-based scan). Such a
    daemon can never be reused — nothing can find its socket — so it is pure leak, and only
    the process table can see it.

    Fail-closed: only same-user ``browser_harness.daemon`` processes, only when older than
    the grace (so a daemon mid-startup, before it publishes its pid file, is never killed),
    and never one whose PID is still referenced by a pid file we know about. "No pid file
    in OUR dirs" is not "no pid file": another hermes profile with its own runtime dir has
    live daemons we cannot index, so a candidate must also be positively attributable to
    one of our sweep dirs via its own environment. Unattributable → left alone.
    """
    try:
        import psutil
    except ImportError:  # psutil is a hard dep; defensive only
        return 0

    indexed = set()
    ours = set(_sweep_dirs())
    for runtime_dir in ours:
        for stem in _sessions_in(runtime_dir):
            pid = _read_pid(runtime_dir / f"{stem}.pid")
            if pid is not None:
                indexed.add(pid)

    uid = os.getuid() if hasattr(os, "getuid") else None
    grace = _bt.BROWSER_ORPHAN_GRACE_SECONDS
    reaped = 0
    for proc in psutil.process_iter(["pid", "cmdline", "create_time", "uids"]):
        try:
            info = proc.info
            pid = info["pid"]
            if pid in indexed or pid == os.getpid():
                continue
            if not _is_harness_daemon_cmdline(info.get("cmdline")):
                continue
            if uid is not None and info.get("uids") and info["uids"].real != uid:
                continue
            if time.time() - (info.get("create_time") or 0) < grace:
                continue  # may still be publishing its pid file
            runtime_dir = _daemon_runtime_dir(proc)
            if runtime_dir not in ours:
                continue  # another profile's (or unattributable): not ours to reap
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError):
            continue

        birth = info.get("create_time")
        if not isinstance(birth, (int, float)) or isinstance(birth, bool) or not math.isfinite(birth) or birth <= 0:
            continue
        if _signal_daemon(pid, expected_birth=birth, runtime_dir=runtime_dir):
            _bt.logger.info("Stopped browser-harness daemon PID %d: running with no endpoint "
                            "files in this profile's runtime dir (unreachable, cannot be reused)", pid)
            reaped += 1
    return reaped


def reap_orphaned_harness_daemons() -> None:
    """Stop harness daemons whose owning hermes process is gone, or that went idle past the
    grace. Safe from any context; called on the janitor's orphan-reap cycle."""
    reaped = _sweep(only_owner=None) + _sweep_unindexed()
    if reaped:
        _bt.logger.info("Reaped %d orphaned browser-harness daemon(s)", reaped)


def shutdown_owned_harness_daemons() -> None:
    """atexit: stop the daemons THIS process claimed.

    Only our own claims — several hermes processes (gateway multiplex, CLI, kanban worker)
    share one runtime dir, and the last caller of a name owns it. A process that lost a
    claim leaves the daemon to its new owner; ``ensure_daemon`` is idempotent, so a caller
    whose daemon was stopped under it just gets a fresh one on its next call.
    """
    _sweep(only_owner=os.getpid())
