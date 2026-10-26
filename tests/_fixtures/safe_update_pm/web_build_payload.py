import sys
from pathlib import Path
import hermes_cli.main as hm


def _resolve(mod, name):
    try:
        return getattr(mod, name)
    except AttributeError:
        return None


def _real(fn):
    # hermes_cli.main resolves retired updater names through a frozen shim (main.py __getattr__ ->
    # _old_updater.stop_for_relaunch) that runs a whole "historical takeover" update and exits 0
    # instead of answering. Looked up through hm first, this check became a second 3-4 minute
    # updater pass inside the stopped window (2026-10-15). Only a real builder counts.
    return None if fn is None or getattr(fn, "__module__", "") == "hermes_cli._old_updater" else fn


try:
    import hermes_cli.main_web_build as hwb
except ImportError:
    hwb = None
build_fn = _real(_resolve(hwb, "_build_web_ui")) if hwb is not None else None
build_source = "main_web_build"
if build_fn is None:
    build_fn = _real(_resolve(hm, "_build_web_ui"))
    build_source = "main"
if build_fn is None:
    print("web UI bundle: BUILD FAILED (compat: _build_web_ui not found in "
          "hermes_cli.main_web_build or hermes_cli.main)")
    sys.exit(1)

needed_fn = _real(_resolve(hwb, "_web_ui_build_needed")) if hwb is not None else None
needed_source = "main_web_build"
if needed_fn is None:
    needed_fn = _real(_resolve(hm, "_web_ui_build_needed"))
    needed_source = "main"
if needed_fn is None:
    # Fail closed: report "needed" rather than fabricating "current" when we
    # cannot determine staleness. build_fn's own result still gates pass/fail.
    needed_fn = lambda _wd: True
    needed_source = "unresolved-fail-closed"

needed = needed_fn(Path("web"))
ok = build_fn(Path("web"), fatal=True)
print("web UI bundle:", ("rebuilt" if needed else "current") if ok else "BUILD FAILED",
      f"(needed_source={needed_source}, build_source={build_source})")
sys.exit(0 if ok else 1)
