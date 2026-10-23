#!/bin/bash
# hermes-safe-update.sh — update hermes-agent WITHOUT breaking gateway supervision.
#
# Why this exists (incident 2026-06-12):
#  - Gateways/dashboards are supervised by SYSTEM LaunchDaemons
#    (/Library/LaunchDaemons/ai.hermes.gateway-* etc).
#  - Running `hermes gateway install` adds a SECOND (user-level) supervisor and
#    causes an endless --replace kill loop. Never do that.
#  - `hermes update --backup` can leave Discord agents offline for a long time
#    while it builds a multi-GB backup. Agent-triggered updates default to
#    --no-backup; run this script with --backup only during planned maintenance.
#
# Usage: hermes-safe-update.sh [--backup] [--no-backup] [--patch-only | --cua-compat-only | --retry-recovery-only | --cron-completion-delivery-only | --air-m5-cron-completion-only | --air-m5-cron-completion-rollback | --air-m5-retry-only | --air-m5-retry-rollback]
#
# --retry-recovery-only: apply only the approved paired Retry/compression patches
#   and a separately prebuilt, hash-bound Desktop app. It does not resolve a
#   release, install dependencies, or replay ordinary carried patches.
# --cua-compat-only: apply ONLY the pinned five-file CUA compatibility patch
#   to the ai-server hermes-agent checkout. It never resolves a stable release,
#   invokes the inner updater, replays ordinary carried patches, changes dependencies,
#   or touches a plugin checkout. Exact source-tree identity is required before success.
#
# --cron-completion-delivery-only: apply only the approved no-agent Bot Chat completion delivery payload.
# It is a core-only exact-base/hash-gated transaction; it does not resolve a release,
# install dependencies, replay ordinary carries, modify plugins, or build/package clients.
#
# --patch-only: apply ONLY the pinned fix set in lib/hermes-patch-only-fix-set.sh
#   (exact base commits + exact patch SHA256s) to the current installed trees.
#   No stable-release resolution, no inner updater, no carried-patch replay,
#   no dependency/lock/web-build changes. Shares the marker, journal, snapshot,
#   rollback, service pause/restore and guardian contract of the ordinary mode.
set -uo pipefail

# Quant is hosted by the default-profile multiplexer since 2026-09-24.
GATEWAY_DAEMONS=(gateway-default)
# This periodic watchdog is the independent orphan-transaction guardian. Never
# boot it out during an update; include it only in restoration verification.
RECOVERY_DAEMONS=(gateway-health)
# Shared in-process daemons (dashboard, hermes-ui, ...) are the single source of
# truth in lib/hermes-shared-inprocess-daemons.sh, shared with
# gateway-restart.sh so the two scripts can never drift apart on this list
# again (see that file's header for the 2026-07-28/2026-08-02 incidents this
# caused). DASHBOARD_DAEMONS/WEBUI_DAEMONS below are derived from it, kept as
# separate variables only so the existing gateway-count check below (which
# intentionally only counts GATEWAY_DAEMONS) is untouched.
#
# WEBUI_DAEMONS keeps its name but no longer contains `webui`: ai.hermes.webui
# was decommissioned 2026-09-13 in favor of hermes-relay (see the shared list's
# header). The variable now carries hermes-ui alone. The case guard below
# deliberately still ACCEPTS `webui` so that re-adding it to the shared list
# fails on the missing daemon rather than on this guard — but nothing should
# re-add it; stand up hermes-relay instead.
SAFE_UPDATE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/hermes-shared-inprocess-daemons.sh
source "${SAFE_UPDATE_SCRIPT_DIR}/lib/hermes-shared-inprocess-daemons.sh"
source "${SAFE_UPDATE_SCRIPT_DIR}/lib/hermes-update-runtime.sh"
DASHBOARD_DAEMONS=(dashboard)
WEBUI_DAEMONS=(hermes-ui)
# relay-default supersedes the decommissioned webui and is the same long-lived
# in-process agent-code host (see lib/hermes-shared-inprocess-daemons.sh), so it
# gets its own group and is paused/restored/health-gated like the others.
RELAY_DAEMONS=(relay-default)
# Guard against the shared list growing a name neither variable above
# accounts for — fail loudly instead of silently skipping a daemon that
# needed pausing/restoring.
for _shared in "${HERMES_SHARED_INPROCESS_DAEMONS[@]}"; do
  case "$_shared" in
    dashboard|webui|hermes-ui|relay-default) ;;
    *)
      echo "[safe-update] ERROR: lib/hermes-shared-inprocess-daemons.sh lists '$_shared', which this script does not yet pause/restore. Update DASHBOARD_DAEMONS/WEBUI_DAEMONS (or add a new *_DAEMONS group) before proceeding." >&2
      exit 1
      ;;
  esac
done

# Post-restore liveness endpoints, DERIVED from the daemon groups above rather
# than hardcoded.
#
# WHY (2026-09-13 incident): the post-restore health gate polled a hardcoded
# list that still contained http://127.0.0.1:8787/ — ai.hermes.webui — after
# webui was decommissioned earlier that same day. Nothing listened on 8787, so
# curl returned 000, the gate could never go green, and a fully successful
# v2026.9.11 update (32/32 carried patches replayed, tree compiled, bundle
# verified) was rolled back by the guardian at the final step. The daemon list
# had been correctly updated; the parallel URL literal had not. Deriving the
# URLs from the same arrays makes that drift impossible.
#
# health_url_for_daemon: short daemon name -> its local liveness URL, or empty
# for daemons that expose no HTTP surface. webui is retained deliberately so
# that re-adding it to the shared list restores a working gate.
health_url_for_daemon(){
  case "$1" in
    dashboard) echo "http://127.0.0.1:9221/" ;;
    hermes-ui) echo "http://127.0.0.1:8788/" ;;
    webui)     echo "http://127.0.0.1:8787/" ;;
    # Relay's "/" is a WebSocket-upgrade endpoint that 400s on a plain GET, so
    # the liveness probe must target its JSON /health route explicitly.
    relay-default) echo "http://127.0.0.1:8767/health" ;;
    *)         echo "" ;;
  esac
}
HEALTH_URLS=()
for _hd in ${DASHBOARD_DAEMONS[@]+"${DASHBOARD_DAEMONS[@]}"} ${WEBUI_DAEMONS[@]+"${WEBUI_DAEMONS[@]}"} ${RELAY_DAEMONS[@]+"${RELAY_DAEMONS[@]}"}; do
  _hurl="$(health_url_for_daemon "$_hd")"
  if [ -z "$_hurl" ]; then
    echo "[safe-update] ERROR: no liveness URL mapped for daemon '$_hd'; add it to health_url_for_daemon() before proceeding." >&2
    exit 1
  fi
  HEALTH_URLS+=("$_hurl")
done
# The ordinary updater maintains the dedicated Quant Chrome debug LaunchAgent.
# The CUA-only core patch has no Chrome payload and must leave it alone; its
# pause/recovery paths gate this existing group below without changing ordinary
# updater behavior.
CHROME_AGENTS=(quant)
GUI_UID=502   # aiserver
# Hermes profile tool shells may set HOME to ~/.hermes/profiles/<profile>/home.
# Keep this script anchored to the real macOS user home when an agent runs it.
AISERVER_HOME="${AISERVER_HOME:-/Users/aiserver}"
export HOME="$AISERVER_HOME"
EXPECTED_GATEWAYS=1
GATEWAY_PROFILES=(default)
# Patch-only activation is deliberately Neo-only. Ordinary updates retain
# their existing full fleet scope below. Shared holders are derived from
# lib/hermes-shared-inprocess-daemons.sh, never a duplicate list.
PATCH_ONLY_GATEWAY_DAEMONS=(gateway-default)
UPDATE_SERVICE_DAEMONS=()
UPDATE_GATEWAY_PROFILES=()
UPDATE_EXPECTED_GATEWAYS=0
UPDATE_SERVICE_TARGETS=""

BACKUP_FLAG="--no-backup"
DETACHED_RUN="${HERMES_SAFE_UPDATE_DETACHED:-0}"
PATCH_ONLY=0
CUA_COMPAT_ONLY=0
RETRY_RECOVERY_ONLY=0
CRON_COMPLETION_DELIVERY_ONLY=0
AIR_M5_RETRY_ONLY=0
AIR_M5_RETRY_ROLLBACK=0
AIR_M5_CRON_COMPLETION_ONLY=0
AIR_M5_CRON_COMPLETION_ROLLBACK=0
DESKTOP_ARTIFACT_ARG=""
DESKTOP_SHA256_ARG=""
DESKTOP_TARGET_ARG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --backup) BACKUP_FLAG="--backup" ;;
    --no-backup) BACKUP_FLAG="--no-backup" ;;
    --detached-run) DETACHED_RUN=1 ;;
    --patch-only) PATCH_ONLY=1 ;;
    --cua-compat-only) CUA_COMPAT_ONLY=1 ;;
    --retry-recovery-only) RETRY_RECOVERY_ONLY=1 ;;
    --cron-completion-delivery-only) CRON_COMPLETION_DELIVERY_ONLY=1 ;;
    --air-m5-retry-only) AIR_M5_RETRY_ONLY=1 ;;
    --air-m5-retry-rollback) AIR_M5_RETRY_ROLLBACK=1 ;;
    --air-m5-cron-completion-only) AIR_M5_CRON_COMPLETION_ONLY=1 ;;
    --air-m5-cron-completion-rollback) AIR_M5_CRON_COMPLETION_ROLLBACK=1 ;;
    --desktop-artifact) shift; DESKTOP_ARTIFACT_ARG="${1:-}" ;;
    --desktop-sha256) shift; DESKTOP_SHA256_ARG="${1:-}" ;;
    --desktop-target) shift; DESKTOP_TARGET_ARG="${1:-}" ;;
    *) echo "[safe-update] ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ "$((PATCH_ONLY + CUA_COMPAT_ONLY + RETRY_RECOVERY_ONLY + CRON_COMPLETION_DELIVERY_ONLY + AIR_M5_RETRY_ONLY + AIR_M5_RETRY_ROLLBACK + AIR_M5_CRON_COMPLETION_ONLY + AIR_M5_CRON_COMPLETION_ROLLBACK))" -gt 1 ]; then
  echo "[safe-update] ERROR: patch-only/client selectors are mutually exclusive" >&2
  exit 2
fi
# The Air route is client-only and deliberately exits before service scope,
# update markers, launchd, server checkout handling, or any server transaction.
if [ "$AIR_M5_CRON_COMPLETION_ONLY" = "1" ] || [ "$AIR_M5_CRON_COMPLETION_ROLLBACK" = "1" ]; then
  [ -z "$DESKTOP_ARTIFACT_ARG$DESKTOP_SHA256_ARG$DESKTOP_TARGET_ARG" ] || { echo "[safe-update] ERROR: Air M5 route takes no Desktop override arguments" >&2; exit 2; }
  air_mode=activate
  [ "$AIR_M5_CRON_COMPLETION_ROLLBACK" = "1" ] && air_mode=rollback
  exec /bin/bash "$SAFE_UPDATE_SCRIPT_DIR/lib/hermes-air-m5-cron-completion-activation.sh" "$air_mode"
fi
if [ "$AIR_M5_RETRY_ONLY" = "1" ] || [ "$AIR_M5_RETRY_ROLLBACK" = "1" ]; then
  [ -z "$DESKTOP_ARTIFACT_ARG$DESKTOP_SHA256_ARG$DESKTOP_TARGET_ARG" ] || { echo "[safe-update] ERROR: Air M5 route takes no Desktop override arguments" >&2; exit 2; }
  air_mode=activate
  [ "$AIR_M5_RETRY_ROLLBACK" = "1" ] && air_mode=rollback
  exec /bin/bash "$SAFE_UPDATE_SCRIPT_DIR/lib/hermes-air-m5-retry-activation.sh" "$air_mode"
fi
[ "$CUA_COMPAT_ONLY" = "1" ] && PATCH_ONLY=1
[ "$RETRY_RECOVERY_ONLY" = "1" ] && PATCH_ONLY=1
[ "$CRON_COMPLETION_DELIVERY_ONLY" = "1" ] && PATCH_ONLY=1
configure_update_service_scope(){
  local s
  UPDATE_SERVICE_DAEMONS=()
  UPDATE_GATEWAY_PROFILES=()
  if [ "$PATCH_ONLY" = "1" ]; then
    UPDATE_SERVICE_DAEMONS=("${PATCH_ONLY_GATEWAY_DAEMONS[@]}")
    UPDATE_GATEWAY_PROFILES=(default)
    UPDATE_EXPECTED_GATEWAYS=1
  else
    UPDATE_SERVICE_DAEMONS=("${GATEWAY_DAEMONS[@]}")
    UPDATE_GATEWAY_PROFILES=("${GATEWAY_PROFILES[@]}")
    UPDATE_EXPECTED_GATEWAYS=$EXPECTED_GATEWAYS
  fi
  # Shared holders are source-defined and always need a brief cycle after core changes.
  for s in "${HERMES_SHARED_INPROCESS_DAEMONS[@]}"; do
    UPDATE_SERVICE_DAEMONS+=("$s")
  done
  if [ "${#UPDATE_SERVICE_DAEMONS[@]}" -eq 0 ]; then
    printf '%s
' "[safe-update] ERROR: selected update service scope is empty" >&2
    exit 1
  fi
  UPDATE_SERVICE_TARGETS=$(IFS=,; echo "${UPDATE_SERVICE_DAEMONS[*]}")
}
configure_update_service_scope


log(){ echo "[safe-update $(date +%H:%M:%S)] $*"; }

# Free-space preflight. A failed or even a successful update writes ~8-10 GiB
# (rollback clone decay, venv rewrite, .git repack, backups/*), and on this box
# the Data volume has repeatedly been driven to 0 GiB. Refuse to START below the
# floor, before any service is paused or journal written, so a refusal is
# always side-effect free. Fails closed: if free space cannot be determined the
# update does not start. HERMES_SAFE_UPDATE_MIN_FREE_GIB overrides the floor
# (test seam and documented operator escape hatch); HERMES_SAFE_UPDATE_FREE_KB_OVERRIDE
# fakes the measurement for tests.
MIN_FREE_GIB="${HERMES_SAFE_UPDATE_MIN_FREE_GIB:-15}"
FREE_SPACE_VOLUME="${HERMES_SAFE_UPDATE_FREE_VOLUME:-/System/Volumes/Data}"
preflight_free_space(){
  local free_kb min_kb
  case "$MIN_FREE_GIB" in
    ''|*[!0-9]*) log "ERROR: HERMES_SAFE_UPDATE_MIN_FREE_GIB='$MIN_FREE_GIB' is not an integer; refusing to start"; exit 1 ;;
  esac
  if [ -n "${HERMES_SAFE_UPDATE_FREE_KB_OVERRIDE:-}" ]; then
    free_kb="$HERMES_SAFE_UPDATE_FREE_KB_OVERRIDE"
  else
    free_kb="$(df -Pk "$FREE_SPACE_VOLUME" 2>/dev/null | awk 'NR==2 {print $4}')"
  fi
  case "$free_kb" in
    ''|*[!0-9]*) log "ERROR: could not determine free space on $FREE_SPACE_VOLUME; refusing to start (fail closed)"; exit 1 ;;
  esac
  min_kb=$(( MIN_FREE_GIB * 1024 * 1024 ))
  if [ "$free_kb" -lt "$min_kb" ]; then
    log "ERROR: only $(( free_kb / 1024 / 1024 )).$(( (free_kb / 1024 % 1024) * 10 / 1024 )) GiB free on $FREE_SPACE_VOLUME; an update needs at least $MIN_FREE_GIB GiB free (it writes ~8-10 GiB). Free space first (see ~/.hermes/logs/disk-free.log, hermes-rollback-retention.sh --apply) and re-run. Nothing was changed."
    exit 1
  fi
  log "free-space preflight OK: $(( free_kb / 1024 / 1024 )) GiB free on $FREE_SPACE_VOLUME (floor $MIN_FREE_GIB GiB)"
}

submit_detached_if_needed(){
  [ "$DETACHED_RUN" = "1" ] && return 0

  # Always escape the invoking control plane.  An SSH process can look external
  # while its local client is still a child of ai.hermes.dashboard; booting out
  # that dashboard closes SSH and HUPs the remote updater.  A dedicated tmux
  # server is independent of gateways/dashboards and keeps both output and an
  # exit receipt outside the services being cycled.
  local tmux_bin="/opt/homebrew/bin/tmux"
  [ -x "$tmux_bin" ] || tmux_bin="$(command -v tmux 2>/dev/null || true)"
  if [ -z "$tmux_bin" ] || [ ! -x "$tmux_bin" ]; then
    log "ERROR: tmux is required for a kill-independent update control process"
    exit 1
  fi

  mkdir -p "$AISERVER_HOME/.hermes/logs" "$AISERVER_HOME/.hermes/state"
  local stamp session log_file receipt_file receipt_tmp command_q backup_q
  stamp="$(date +%Y%m%d-%H%M%S)"
  session="hermes-safe-update-$stamp-$$"
  log_file="$AISERVER_HOME/.hermes/logs/hermes-safe-update-$stamp.log"
  receipt_file="$AISERVER_HOME/.hermes/state/hermes-safe-update-$stamp.receipt"
  receipt_tmp="${receipt_file}.tmp"
  backup_q="$(printf '%q' "$BACKUP_FLAG")"
  local mode_q=""
  if [ "$CUA_COMPAT_ONLY" = "1" ]; then
    mode_q=" --cua-compat-only"
  elif [ "$RETRY_RECOVERY_ONLY" = "1" ]; then
    if [ -z "$DESKTOP_ARTIFACT_ARG" ] || [ -z "$DESKTOP_SHA256_ARG" ] || [ -z "$DESKTOP_TARGET_ARG" ]; then
      log "ERROR: --retry-recovery-only requires --desktop-artifact, --desktop-sha256 and --desktop-target"
      exit 2
    fi
    mode_q=" --retry-recovery-only --desktop-artifact $(printf '%q' "$DESKTOP_ARTIFACT_ARG") --desktop-sha256 $(printf '%q' "$DESKTOP_SHA256_ARG") --desktop-target $(printf '%q' "$DESKTOP_TARGET_ARG")"
  elif [ "$CRON_COMPLETION_DELIVERY_ONLY" = "1" ]; then
    mode_q=" --cron-completion-delivery-only"
  elif [ "$PATCH_ONLY" = "1" ]; then
    mode_q=" --patch-only"
  fi
  command_q="AISERVER_HOME=/Users/aiserver HERMES_SAFE_UPDATE_DETACHED=1 /bin/bash /Users/aiserver/.hermes/scripts/hermes-safe-update.sh --detached-run $backup_q$mode_q >>$(printf '%q' "$log_file") 2>&1; rc=\$?; printf 'exit_code=%s\\nfinished_at=%s\\n' \"\$rc\" \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >$(printf '%q' "$receipt_tmp"); mv $(printf '%q' "$receipt_tmp") $(printf '%q' "$receipt_file"); exit \"\$rc\""

  if ! "$tmux_bin" -L hermes-safe-update new-session -d -s "$session" "/bin/bash -lc $(printf '%q' "$command_q")"; then
    log "ERROR: failed to create detached tmux update session"
    exit 1
  fi
  if ! "$tmux_bin" -L hermes-safe-update has-session -t "$session" 2>/dev/null; then
    log "ERROR: detached tmux session did not survive startup"
    exit 1
  fi
  log "Submitted detached tmux update session: $session"
  log "Detached update log: $log_file"
  log "Detached exit receipt: $receipt_file"
  log "The dashboard may disconnect briefly while tmux continues the update."
  exit 0
}

preflight_free_space
submit_detached_if_needed
# The detached run re-checks: space can disappear between submission and start.
preflight_free_space

# Production deploys the canonical latest stable release, never the moving
# origin/main branch. Resolve once, fetch the exact tag into a private ref, and
# record the peeled commit before pausing any service. Every later step uses
# this immutable commit so upstream movement cannot change the candidate after
# readiness was proved.
REPO_DIR="$AISERVER_HOME/.hermes/hermes-agent"
# python -m imports from cwd before editable-install mappings. A caller or
# reused tmux server may have a detached staging worktree as cwd; never let
# that worktree capture the inner updater while Git stages the managed tree.
cd "$REPO_DIR" || { log "ERROR: cannot enter managed checkout"; exit 1; }
STABLE_RESOLVER="$AISERVER_HOME/.hermes/scripts/hermes-resolve-stable-release.py"
STABLE_RECEIPT="$AISERVER_HOME/.hermes/state/hermes-stable-release.json"
DEPLOYED_STABLE_RECEIPT="$AISERVER_HOME/.hermes/state/hermes-deployed-stable-release.json"
PATCH_DIR="$AISERVER_HOME/.hermes/scripts/patches"
PLUGIN_PATCH_DIR="$AISERVER_HOME/.hermes/scripts/plugin-patches"

# --- patch-only mode: pinned two-target fix set ----------------------------
# Targets come from lib/hermes-patch-only-fix-set.sh (same trust domain as this
# script). Every check below is read-only and runs BEFORE the marker is claimed
# or any service is paused. A target is accepted only at the exact pinned base
# (patch applies cleanly, marker absent) or in the exact applied state (marker
# present, patch reverse-applies cleanly); any other state fails closed.
PATCH_ONLY_FIX_SET=""
PATCH_ONLY_CORE_STATE=""
PATCH_ONLY_PLUGIN_STATE=""
PATCH_ONLY_PLUGIN_EXPECTED_TREE=""
PATCH_ONLY_CORE_PATCH_PATH=""
PATCH_ONLY_PLUGIN_PATCH_PATH=""
PATCH_ONLY_RECEIPT=""
PATCH_ONLY_DESKTOP=0
DESKTOP_ARTIFACT=""
DESKTOP_EXPECTED_HASH=""
DESKTOP_TARGET=""
DESKTOP_PRE_HASH=""
DESKTOP_ROLLBACK_SNAPSHOT=""
DESKTOP_STAGING_ACTIVE=0
# CUA-only is a single core target. Existing --patch-only keeps its paired plugin target.
PATCH_ONLY_HAS_PLUGIN=1
PATCH_ONLY_CORE_EXPECTED_TREE=""
PLUGIN_DIR=""
GITCMD=(git)
DIRTY_TREE_EXCEPTION='contributors/emails/agent@[Aa]gents-Mac-mini\.local$'

tree_is_dirty(){
  [ -n "$(git -C "$1" status --porcelain 2>/dev/null | grep -v "$DIRTY_TREE_EXCEPTION")" ]
}

patch_only_check_patch_file(){
  local label="$1" path="$2" expected="$3" actual=""
  if [ ! -f "$path" ]; then
    log "ERROR: $label patch file missing: $path"
    return 1
  fi
  actual=$(/usr/bin/python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$path" 2>/dev/null)
  if [ "$actual" != "$expected" ]; then
    log "ERROR: $label patch file SHA256 mismatch: $path (got ${actual:-unreadable}, pinned $expected)"
    return 1
  fi
  return 0
}

# Prints "apply" or "applied" on stdout; logs the reason and returns 1 otherwise.
patch_only_target_state(){
  local label="$1" repo="$2" base="$3" patch="$4" marker_file="$5" marker="$6" expected_tree="${7:-}" head=""
  if [ ! -d "$repo/.git" ] && [ ! -f "$repo/.git" ]; then
    log "ERROR: $label checkout missing or not a git checkout: $repo" >&2
    return 1
  fi
  head=$(git -C "$repo" rev-parse HEAD 2>/dev/null)
  if [ -z "$head" ]; then
    log "ERROR: cannot resolve $label HEAD in $repo" >&2
    return 1
  fi
  if tree_is_dirty "$repo"; then
    log "ERROR: $label checkout is dirty: $repo" >&2
    return 1
  fi
  if ! git -C "$repo" merge-base --is-ancestor "$base" HEAD 2>/dev/null; then
    log "ERROR: $label HEAD $head does not descend from the pinned base $base" >&2
    return 1
  fi
  if [ "$head" = "$base" ]; then
    if grep -Fq "$marker" "$repo/$marker_file" 2>/dev/null; then
      log "ERROR: $label is at the pinned base $base but already contains marker '$marker'" >&2
      return 1
    fi
    if ! git -C "$repo" apply --check --whitespace=nowarn "$patch" >/dev/null 2>&1; then
      log "ERROR: $label patch does not apply cleanly at the pinned base $base: $patch" >&2
      return 1
    fi
    echo apply
    return 0
  fi
  if grep -Fq "$marker" "$repo/$marker_file" 2>/dev/null \
    && git -C "$repo" apply --check --reverse --whitespace=nowarn "$patch" >/dev/null 2>&1; then
    if [ -n "$expected_tree" ] && [ "$(git -C "$repo" rev-parse HEAD^{tree} 2>/dev/null)" != "$expected_tree" ]; then
      log "ERROR: $label has the patch marker but tree identity differs from $expected_tree" >&2
      return 1
    fi
    echo applied
    return 0
  fi
  log "ERROR: $label HEAD $head is neither the pinned base $base nor the exact applied state" >&2
  return 1
}

# Stable recursive content identity for an unpacked .app. This is an integrity
# gate, not a code-signature verdict: the build receipt binds signing/notarization
# separately, while this hashes exactly what is copied and installed.
desktop_tree_sha256(){
  /usr/bin/python3 - "$1" <<'PY'
import hashlib, os, sys
from pathlib import Path
root = Path(sys.argv[1])
if not root.is_dir():
    raise SystemExit(2)
h = hashlib.sha256()
for path in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
    rel = path.relative_to(root).as_posix().encode()
    if path.is_symlink():
        h.update(b'L\0' + rel + b'\0' + os.readlink(path).encode() + b'\0')
    elif path.is_file():
        h.update(b'F\0' + rel + b'\0')
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                h.update(chunk)
    elif path.is_dir():
        h.update(b'D\0' + rel + b'\0')
print(h.hexdigest())
PY
}

desktop_activation_preflight(){
  [ "$PATCH_ONLY_DESKTOP" = "1" ] || return 0
  if [ -z "$DESKTOP_ARTIFACT_ARG" ] || [ -z "$DESKTOP_SHA256_ARG" ] || [ -z "$DESKTOP_TARGET_ARG" ]; then
    log "ERROR: retry-recovery Desktop activation requires --desktop-artifact, --desktop-sha256 and --desktop-target"
    return 1
  fi
  DESKTOP_ARTIFACT="$DESKTOP_ARTIFACT_ARG"
  DESKTOP_EXPECTED_HASH="$DESKTOP_SHA256_ARG"
  DESKTOP_TARGET="$DESKTOP_TARGET_ARG"
  if ! printf '%s' "$DESKTOP_EXPECTED_HASH" | /usr/bin/grep -Eq '^[0-9a-f]{64}$'; then
    log "ERROR: Desktop SHA-256 must be 64 lowercase hex characters"
    return 1
  fi
  if ! /usr/bin/python3 - "$DESKTOP_TARGET" "$AISERVER_HOME" <<'PY'
import sys
from pathlib import Path
target, home = Path(sys.argv[1]), Path(sys.argv[2])
checkout_app = home / ".hermes/hermes-agent/apps/desktop/release/mac-arm64/Hermes.app"
allowed = (Path("/Applications"), home / "Applications")
valid = target.is_absolute() and target.suffix == ".app" and not target.is_symlink()
valid = valid and (target == checkout_app or any(target.parent == root for root in allowed))
valid = valid and target.resolve() == target
sys.exit(0 if valid else 1)
PY
  then
    log "ERROR: Desktop target is outside the approved application paths"
    return 1
  fi
  if [ ! -d "$DESKTOP_ARTIFACT" ] || [ ! -d "$DESKTOP_TARGET" ]; then
    log "ERROR: Desktop artifact and existing target app must both be directories"
    return 1
  fi
  local got
  got=$(desktop_tree_sha256 "$DESKTOP_ARTIFACT") || { log "ERROR: cannot hash Desktop artifact"; return 1; }
  if [ "$got" != "$DESKTOP_EXPECTED_HASH" ]; then
    log "ERROR: Desktop artifact hash mismatch (got $got, pinned $DESKTOP_EXPECTED_HASH)"
    return 1
  fi
  DESKTOP_PRE_HASH=$(desktop_tree_sha256 "$DESKTOP_TARGET") || { log "ERROR: cannot hash existing Desktop app"; return 1; }
  log "Verified: prebuilt Desktop artifact and existing app integrity before service pause."
}

cua_compat_configure(){
  PATCH_ONLY_FIX_SET="cua-compat-20260909"
  PATCH_ONLY_HAS_PLUGIN=0
  PATCH_ONLY_CORE_BASE="bd618b4c61bef01068577f106dbec71639588e42"
  PATCH_ONLY_CORE_PATCH="cua_snapshot_compat_20260909.patch"
  PATCH_ONLY_CORE_SHA256="f946242b12f38f969f5bcdc20d92e97b9c053ef24036e3be3a0e9f49e7971374"
  PATCH_ONLY_CORE_MARKER_FILE="tests/tools/test_computer_use_cua_0_24_schema_contracts.py"
  PATCH_ONLY_CORE_MARKER="test_modern_count_click_preserves_double_right_modifier_under_strict_schema"
  PATCH_ONLY_CORE_EXPECTED_TREE="a9b3c7395aa61adaf50616eb8f7b9a5903827f19"
  PATCH_ONLY_CORE_COMMIT_MSG="fix(computer_use): backport CUA schema compatibility upstream fixes [carried]"
  PATCH_ONLY_PLUGIN_STATE="not-applicable"
}

cron_completion_delivery_configure(){
  PATCH_ONLY_FIX_SET="cron-completion-delivery-20260920"
  PATCH_ONLY_HAS_PLUGIN=0
  PATCH_ONLY_CORE_BASE="1f94a375f86f9978dddfa9ef3ddb97b1b2b89753"
  PATCH_ONLY_CORE_PATCH="cron_completion_delivery_20260920.patch"
  PATCH_ONLY_CORE_SHA256="1ce02604beb4d82051645318b31a083a405299f4bef90371af65f9e165c620ac"
  PATCH_ONLY_CORE_MARKER_FILE="tools/bot_live_delivery.py"
  PATCH_ONLY_CORE_MARKER="def deliver_completion_notice("
  PATCH_ONLY_CORE_EXPECTED_TREE="5e76b1398732b2441348eb604724d95a7628f08c"
  PATCH_ONLY_CORE_COMMIT_MSG="fix(cron): deliver no-agent Bot Chat completions [carried]"
  PATCH_ONLY_PLUGIN_STATE="not-applicable"
}

retry_recovery_configure(){
  local retry_lib="${SAFE_UPDATE_SCRIPT_DIR}/lib/hermes-retry-recovery-fix-set.sh"
  if [ ! -f "$retry_lib" ]; then
    log "ERROR: Retry/recovery fix-set missing: $retry_lib"
    exit 1
  fi
  # shellcheck source=lib/hermes-retry-recovery-fix-set.sh
  source "$retry_lib"
  PATCH_ONLY_DESKTOP=1
}

patch_only_preflight(){
  local fix_set_lib="${SAFE_UPDATE_SCRIPT_DIR}/lib/hermes-patch-only-fix-set.sh" v ok=1
  if [ "$CUA_COMPAT_ONLY" != "1" ] && [ "$RETRY_RECOVERY_ONLY" != "1" ] && [ "$CRON_COMPLETION_DELIVERY_ONLY" != "1" ]; then
    if [ ! -f "$fix_set_lib" ]; then
      log "ERROR: pinned fix set missing: $fix_set_lib"
      exit 1
    fi
    # shellcheck source=lib/hermes-patch-only-fix-set.sh
    source "$fix_set_lib"
  fi
  for v in PATCH_ONLY_FIX_SET PATCH_ONLY_CORE_BASE PATCH_ONLY_CORE_PATCH \
    PATCH_ONLY_CORE_SHA256 PATCH_ONLY_CORE_MARKER_FILE PATCH_ONLY_CORE_MARKER \
    PATCH_ONLY_CORE_COMMIT_MSG; do
    if [ -z "${!v:-}" ]; then
      log "ERROR: pinned fix set does not define $v"
      exit 1
    fi
  done
  if [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
    for v in PATCH_ONLY_PLUGIN_DIR PATCH_ONLY_PLUGIN_BASE PATCH_ONLY_PLUGIN_PATCH \
      PATCH_ONLY_PLUGIN_SHA256 PATCH_ONLY_PLUGIN_MARKER_FILE PATCH_ONLY_PLUGIN_MARKER \
      PATCH_ONLY_PLUGIN_COMMIT_MSG; do
      if [ -z "${!v:-}" ]; then
        log "ERROR: pinned fix set does not define $v"
        exit 1
      fi
    done
    PLUGIN_DIR="$PATCH_ONLY_PLUGIN_DIR"
  fi
  PATCH_ONLY_RECEIPT="$AISERVER_HOME/.hermes/state/hermes-patch-only-${PATCH_ONLY_FIX_SET}.json"
  PATCH_ONLY_CORE_PATCH_PATH="$PATCH_DIR/$PATCH_ONLY_CORE_PATCH"
  PATCH_ONLY_PLUGIN_PATCH_PATH=""
  [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ] && PATCH_ONLY_PLUGIN_PATCH_PATH="$PLUGIN_PATCH_DIR/$PATCH_ONLY_PLUGIN_PATCH"
  if [ "$(id -u)" -eq 0 ]; then GITCMD=(sudo -u aiserver -H git); fi

  log "Patch-only preflight for fix set '$PATCH_ONLY_FIX_SET' (no stable resolution, no inner updater)"
  patch_only_check_patch_file core "$PATCH_ONLY_CORE_PATCH_PATH" "$PATCH_ONLY_CORE_SHA256" || ok=0
  PATCH_ONLY_CORE_STATE=$(patch_only_target_state core "$REPO_DIR" "$PATCH_ONLY_CORE_BASE" \
    "$PATCH_ONLY_CORE_PATCH_PATH" "$PATCH_ONLY_CORE_MARKER_FILE" "$PATCH_ONLY_CORE_MARKER" "$PATCH_ONLY_CORE_EXPECTED_TREE") || ok=0
  if [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
    patch_only_check_patch_file plugin "$PATCH_ONLY_PLUGIN_PATCH_PATH" "$PATCH_ONLY_PLUGIN_SHA256" || ok=0
    PATCH_ONLY_PLUGIN_STATE=$(patch_only_target_state plugin "$PLUGIN_DIR" "$PATCH_ONLY_PLUGIN_BASE" \
      "$PATCH_ONLY_PLUGIN_PATCH_PATH" "$PATCH_ONLY_PLUGIN_MARKER_FILE" "$PATCH_ONLY_PLUGIN_MARKER" "$PATCH_ONLY_PLUGIN_EXPECTED_TREE") || ok=0
  fi
  if [ "$ok" -ne 1 ]; then
    log "ERROR: patch-only preflight failed; nothing was paused or mutated"
    exit 1
  fi
  log "  core:   $PATCH_ONLY_CORE_STATE ($REPO_DIR)"
  [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ] && log "  plugin: $PATCH_ONLY_PLUGIN_STATE ($PLUGIN_DIR)"
  if [ "$PATCH_ONLY_CORE_STATE" = "applied" ] && { [ "$PATCH_ONLY_HAS_PLUGIN" != "1" ] || [ "$PATCH_ONLY_PLUGIN_STATE" = "applied" ]; }; then
    if [ "$PATCH_ONLY_DESKTOP" = "1" ]; then
      desktop_activation_preflight || exit 1
      if [ "$DESKTOP_PRE_HASH" != "$DESKTOP_EXPECTED_HASH" ]; then
        log "ERROR: source fix set is applied but installed Desktop does not match; refusing source-only success"
        exit 1
      fi
    fi
    local core_head plugin_head
    core_head=$(git -C "$REPO_DIR" rev-parse HEAD)
    plugin_head=""
    [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ] && plugin_head=$(git -C "$PLUGIN_DIR" rev-parse HEAD)
    if [ -f "$PATCH_ONLY_RECEIPT" ]; then
      if ! /usr/bin/python3 - "$PATCH_ONLY_RECEIPT" "$core_head" "$plugin_head" <<'PY'
import json, sys
path, core_head, plugin_head = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
ok = data.get("core_head") == core_head and (not plugin_head or data.get("plugin_head") == plugin_head)
if not ok:
    print(f"receipt records core={data.get('core_head')} plugin={data.get('plugin_head')}; "
          f"current core={core_head} plugin={plugin_head or 'not-applicable'}", file=sys.stderr)
sys.exit(0 if ok else 1)
PY
      then
        log "ERROR: fix set is present but the recorded receipt $PATCH_ONLY_RECEIPT does not match the current HEADs"
        exit 1
      fi
      log "Verified: fix set '$PATCH_ONLY_FIX_SET' already applied and matches $PATCH_ONLY_RECEIPT"
    else
      log "Verified: fix set '$PATCH_ONLY_FIX_SET' already applied (no receipt on file)"
    fi
    log "OK: nothing to apply; no service was paused."
    exit 0
  fi
}

# Strict apply at the exact pinned base: no --3way, no fuzz. The result is
# verified by marker, reverse-check and clean tree before it counts.
patch_only_apply(){
  local label="$1" repo="$2" patch="$3" marker_file="$4" marker="$5" commit_msg="$6" expected_tree="${7:-}" pre="" post=""
  pre=$(git -C "$repo" rev-parse HEAD)
  if ! "${GITCMD[@]}" -C "$repo" apply --index --whitespace=nowarn "$patch" 2>/dev/null; then
    "${GITCMD[@]}" -C "$repo" reset --hard HEAD >/dev/null 2>&1 || true
    log "ERROR: $label patch failed to apply at $pre"
    return 1
  fi
  if [ -n "$("${GITCMD[@]}" -C "$repo" ls-files -u 2>/dev/null)" ]; then
    "${GITCMD[@]}" -C "$repo" reset --hard HEAD >/dev/null 2>&1 || true
    log "ERROR: $label patch left unmerged index entries"
    return 1
  fi
  if ! "${GITCMD[@]}" -C "$repo" commit -q -m "$commit_msg" >/dev/null 2>&1; then
    "${GITCMD[@]}" -C "$repo" reset --hard HEAD >/dev/null 2>&1 || true
    log "ERROR: $label patch applied but could not be committed"
    return 1
  fi
  post=$(git -C "$repo" rev-parse HEAD)
  if [ "$post" = "$pre" ] \
    || ! grep -Fq "$marker" "$repo/$marker_file" 2>/dev/null \
    || ! git -C "$repo" apply --check --reverse --whitespace=nowarn "$patch" >/dev/null 2>&1 \
    || { [ -n "$expected_tree" ] && [ "$(git -C "$repo" rev-parse HEAD^{tree} 2>/dev/null)" != "$expected_tree" ]; } \
    || tree_is_dirty "$repo"; then
    log "ERROR: $label post-apply verification failed at $post"
    return 1
  fi
  log "  $label fix applied: $pre -> $post"
  return 0
}

patch_only_apply_fix_set(){
  local patch_py=""
  write_update_journal "patching"
  log "Applying pinned fix set '$PATCH_ONLY_FIX_SET'..."
  if [ "$PATCH_ONLY_CORE_STATE" = "apply" ]; then
    patch_only_apply core "$REPO_DIR" "$PATCH_ONLY_CORE_PATCH_PATH" \
      "$PATCH_ONLY_CORE_MARKER_FILE" "$PATCH_ONLY_CORE_MARKER" "$PATCH_ONLY_CORE_COMMIT_MSG" "$PATCH_ONLY_CORE_EXPECTED_TREE" || exit 1
  else
    log "  core fix already present; not reapplied"
  fi
  if [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
    if [ "$PATCH_ONLY_PLUGIN_STATE" = "apply" ]; then
      patch_only_apply plugin "$PLUGIN_DIR" "$PATCH_ONLY_PLUGIN_PATCH_PATH" \
        "$PATCH_ONLY_PLUGIN_MARKER_FILE" "$PATCH_ONLY_PLUGIN_MARKER" "$PATCH_ONLY_PLUGIN_COMMIT_MSG" "$PATCH_ONLY_PLUGIN_EXPECTED_TREE" || exit 1
    else
      log "  plugin fix already present; not reapplied"
    fi
  fi
  CARRIED_PATCH_CHECKS="${CARRIED_PATCH_CHECKS}${PATCH_ONLY_CORE_MARKER_FILE}|${PATCH_ONLY_CORE_MARKER}|patch-only/core
"
  if [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
    CARRIED_PATCH_CHECKS="${CARRIED_PATCH_CHECKS}${PLUGIN_DIR}/${PATCH_ONLY_PLUGIN_MARKER_FILE}|${PATCH_ONLY_PLUGIN_MARKER}|patch-only/plugin
"
  fi
  # Compile every Python file either patch touches, in the tree it lives in.
  PATCH_ONLY_COMPILE_FILES=""
  for patch_py in $(sed -n 's|^+++ b/\(.*\.py\)$|\1|p' "$PATCH_ONLY_CORE_PATCH_PATH"); do
    PATCH_ONLY_COMPILE_FILES="${PATCH_ONLY_COMPILE_FILES}${REPO_DIR}/${patch_py}
"
  done
  if [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
    for patch_py in $(sed -n 's|^+++ b/\(.*\.py\)$|\1|p' "$PATCH_ONLY_PLUGIN_PATCH_PATH"); do
      PATCH_ONLY_COMPILE_FILES="${PATCH_ONLY_COMPILE_FILES}${PLUGIN_DIR}/${patch_py}
"
    done
    JOURNAL_COMMIT_ID="core=$(git -C "$REPO_DIR" rev-parse HEAD);plugin=$(git -C "$PLUGIN_DIR" rev-parse HEAD)"
  else
    JOURNAL_COMMIT_ID="core=$(git -C "$REPO_DIR" rev-parse HEAD)"
  fi
}

write_patch_only_receipt(){
  /usr/bin/python3 - "$PATCH_ONLY_RECEIPT" "$PATCH_ONLY_FIX_SET" "$JOURNAL_COMMIT_ID" \
    "$(git -C "$REPO_DIR" rev-parse HEAD)" "$PATCH_ONLY_CORE_BASE" \
    "$PATCH_ONLY_CORE_PATCH" "$PATCH_ONLY_CORE_SHA256" "$REPO_DIR" "$PATCH_ONLY_CORE_EXPECTED_TREE" \
    "$PATCH_ONLY_HAS_PLUGIN" "${PLUGIN_DIR:-}" "${PATCH_ONLY_PLUGIN_BASE:-}" \
    "${PATCH_ONLY_PLUGIN_PATCH:-}" "${PATCH_ONLY_PLUGIN_SHA256:-}" <<'PY'
import json, os, sys, time
(path, fix_set, commit_id, core_head, core_base, core_patch, core_sha, core_repo, core_tree,
 has_plugin, plugin_repo, plugin_base, plugin_patch, plugin_sha) = sys.argv[1:]
payload = {
    "commit": commit_id,
    "fix_set": fix_set,
    "core_repo": core_repo,
    "core_base": core_base,
    "core_head": core_head,
    "core_tree": core_tree,
    "core_patch": core_patch,
    "core_patch_sha256": core_sha,
    "recorded_at": time.time(),
}
if has_plugin == "1":
    import subprocess
    plugin_head = subprocess.check_output(["git", "-C", plugin_repo, "rev-parse", "HEAD"], text=True).strip()
    payload.update({
        "plugin_repo": plugin_repo,
        "plugin_base": plugin_base,
        "plugin_head": plugin_head,
        "plugin_patch": plugin_patch,
        "plugin_patch_sha256": plugin_sha,
    })
tmp = f"{path}.tmp.{os.getpid()}"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, path)
dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
PY
}
# --- end patch-only mode definitions ---------------------------------------

if [ "$PATCH_ONLY" = "1" ]; then
STABLE_TAG=""
STABLE_COMMIT=""
STABLE_BASE=""
DEPLOYED_STABLE_COMMIT=""
[ "$CUA_COMPAT_ONLY" = "1" ] && cua_compat_configure
[ "$RETRY_RECOVERY_ONLY" = "1" ] && retry_recovery_configure
[ "$CRON_COMPLETION_DELIVERY_ONLY" = "1" ] && cron_completion_delivery_configure
patch_only_preflight
if ! desktop_activation_preflight; then
  log "ERROR: Desktop preflight failed; nothing was paused or mutated"
  exit 1
fi
else
if [ ! -x "$STABLE_RESOLVER" ]; then
  log "ERROR: stable-release resolver missing or not executable: $STABLE_RESOLVER"
  exit 1
fi
STABLE_JSON=$(python3 "$STABLE_RESOLVER" --repo "$REPO_DIR" --write "$STABLE_RECEIPT") || {
  log "ERROR: could not resolve the canonical stable Hermes release"
  exit 1
}
STABLE_TAG=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["tag"])' "$STABLE_JSON")
STABLE_COMMIT=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["commit"])' "$STABLE_JSON")
STABLE_BASE=$(git -C "$REPO_DIR" merge-base HEAD "$STABLE_COMMIT")
if [ -z "$STABLE_BASE" ]; then
  log "ERROR: current checkout has no common base with $STABLE_TAG ($STABLE_COMMIT)"
  exit 1
fi

# A canonical `latest` response is not permission to downgrade production. Use
# the last successful deployment receipt when available; for the first managed
# run, seed the comparison from the newest stable release tag reachable from the
# current carried HEAD. The target must descend from that deployed base.
DEPLOYED_STABLE_COMMIT=""
if [ -f "$DEPLOYED_STABLE_RECEIPT" ]; then
  DEPLOYED_STABLE_COMMIT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["commit"])' "$DEPLOYED_STABLE_RECEIPT" 2>/dev/null || true)
fi
if [ -z "$DEPLOYED_STABLE_COMMIT" ]; then
  DEPLOYED_STABLE_TAG=$(git -C "$REPO_DIR" describe --tags --abbrev=0 --match 'v20*' HEAD 2>/dev/null || true)
  if [ -n "$DEPLOYED_STABLE_TAG" ]; then
    DEPLOYED_STABLE_COMMIT=$(git -C "$REPO_DIR" rev-parse "${DEPLOYED_STABLE_TAG}^{commit}" 2>/dev/null || true)
  fi
fi
if [ -z "$DEPLOYED_STABLE_COMMIT" ] || ! git -C "$REPO_DIR" cat-file -e "${DEPLOYED_STABLE_COMMIT}^{commit}" 2>/dev/null; then
  log "ERROR: cannot establish the currently deployed stable release commit"
  exit 1
fi
if ! git -C "$REPO_DIR" merge-base --is-ancestor "$DEPLOYED_STABLE_COMMIT" "$STABLE_COMMIT"; then
  log "ERROR: canonical latest $STABLE_TAG would downgrade or diverge from deployed stable $DEPLOYED_STABLE_COMMIT"
  exit 1
fi

if [ -n "$(git -C "$REPO_DIR" status --porcelain | grep -v 'contributors/emails/agent@[Aa]gents-Mac-mini\.local$')" ]; then
  log "ERROR: Hermes checkout is dirty; refusing stable update"
  exit 1
fi
log "Pinned production candidate: $STABLE_TAG at $STABLE_COMMIT"

# Optional immutable-candidate guard: if a staging/review pass deliberately
# recorded an approved-candidate receipt (same {"commit","tag"} shape as
# hermes-resolve-stable-release.py's own --write output), the freshly
# resolved candidate above must match it exactly. This exists because the
# resolver always re-resolves whatever GitHub currently reports as latest
# stable on every run; without this, a later invocation could silently pick
# up an upstream release published after staging approved a specific
# commit, deploying something never reviewed. Absence of the receipt file
# is a no-op -- unattended latest-stable tracking is unaffected -- and this
# guard can only add a STOP, never bypass or weaken the checks above/below.
APPROVED_CANDIDATE_RECEIPT="$AISERVER_HOME/.hermes/state/hermes-approved-stable-candidate.json"
if [ -e "$APPROVED_CANDIDATE_RECEIPT" ] || [ -L "$APPROVED_CANDIDATE_RECEIPT" ]; then
  if ! python3 -c '
import sys
sys.path.insert(0, "'"$AISERVER_HOME"'/.hermes/scripts")
from candidate_pin_compat import check_approved_candidate_pin, ApprovedCandidateMismatch
from pathlib import Path
try:
    check_approved_candidate_pin(sys.argv[1], sys.argv[2], Path(sys.argv[3]))
except ApprovedCandidateMismatch as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    sys.exit(1)
' "$STABLE_COMMIT" "$STABLE_TAG" "$APPROVED_CANDIDATE_RECEIPT"; then
    log "ERROR: resolved candidate does not match the approved-candidate pin; see stderr above"
    exit 1
  fi
  log "Verified: resolved candidate matches the approved-candidate pin at $APPROVED_CANDIDATE_RECEIPT"
fi
fi
# --- end mode-specific preflight -------------------------------------------

# Claim the same cross-entrypoint marker used by CLI, dashboard, desktop, and
# installer updates *before* this wrapper mutates refs or pauses supervision.
# A pre-existing marker is recovered by the independent guardian first; this
# wrapper never steals a dead-owner lock from a failed transaction.
UPDATE_MARKER="$AISERVER_HOME/.hermes-update-in-progress"
if [ -e "$UPDATE_MARKER" ]; then
  if ! /usr/bin/python3 "$AISERVER_HOME/.hermes/scripts/hermes-update-guardian.py"; then
    log "ERROR: an earlier update transaction is active or unrecovered"
    exit 2
  fi
fi
if ! /usr/bin/python3 - "$UPDATE_MARKER" "$$" <<'PY'
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
owner = int(sys.argv[2])
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(f"{owner}\n{time.time()}\n")
PY
then
  log "ERROR: another updater is active; refusing to mutate the production checkout"
  exit 2
fi
release_update_marker(){
  local marker_owner=""
  marker_owner=$(sed -n '1p' "$UPDATE_MARKER" 2>/dev/null || true)
  if [ "$marker_owner" = "$$" ]; then
    rm -f "$UPDATE_MARKER"
  fi
}

UPDATE_STAGING_ACTIVE=0
PRE_UPDATE_HEAD=""
STABLE_UPDATE_BRANCH="hermes-stable-${STABLE_TAG}"
STABLE_STAGING_ROOT=""
ORIGINAL_ORIGIN_URL=""
ORIGIN_URL_SWAPPED=0
UPDATE_JOURNAL="$AISERVER_HOME/.hermes/state/hermes-update-transaction.json"
ROLLBACK_ROOT="$AISERVER_HOME/.hermes/state/hermes-update-rollback-$(date +%Y%m%d-%H%M%S)-$$"
ROLLBACK_SNAPSHOT="$ROLLBACK_ROOT/hermes-agent"
KEEP_MARKER_THROUGH_HEALTH=0
# Second rollback target (patch-only mode only). Journaled as extra_targets so
# hermes-update-guardian.py restores the pair, not just the core checkout.
PLUGIN_ROLLBACK_SNAPSHOT="$ROLLBACK_ROOT/hermes-lcm-plugin"
PLUGIN_STAGING_ACTIVE=0
PLUGIN_PRE_UPDATE_HEAD=""
PLUGIN_JOURNAL_DIR=""
PATCH_ONLY_COMPILE_FILES=""
if [ "$PATCH_ONLY" = "1" ]; then
  JOURNAL_COMMIT_ID=""
  JOURNAL_RECEIPT_PATH="$PATCH_ONLY_RECEIPT"
else
  JOURNAL_COMMIT_ID="$STABLE_COMMIT"
  JOURNAL_RECEIPT_PATH="$DEPLOYED_STABLE_RECEIPT"
fi

write_update_journal(){
  local phase="$1"
  CUA_COMPAT_ONLY="$CUA_COMPAT_ONLY" CRON_COMPLETION_DELIVERY_ONLY="$CRON_COMPLETION_DELIVERY_ONLY" /usr/bin/python3 - "$UPDATE_JOURNAL" "$phase" "$$" "$REPO_DIR" \
    "$ROLLBACK_SNAPSHOT" "$PRE_UPDATE_HEAD" "$ORIGINAL_ORIGIN_URL" \
    "$JOURNAL_COMMIT_ID" "$JOURNAL_RECEIPT_PATH" "$UPDATE_SERVICE_TARGETS" \
    "$PATCH_ONLY" "$PLUGIN_JOURNAL_DIR" "$PLUGIN_ROLLBACK_SNAPSHOT" "$PLUGIN_PRE_UPDATE_HEAD" \
    "$DESKTOP_TARGET" "$DESKTOP_ROLLBACK_SNAPSHOT" "$DESKTOP_PRE_HASH" <<'PY'
import json, os, sys, time
(path, phase, owner, repo, snapshot, head, origin, stable, deployed_receipt, service_targets,
 patch_only, plugin_repo, plugin_snapshot, plugin_head, desktop_target, desktop_snapshot, desktop_hash) = sys.argv[1:]
payload = {
    "owner_pid": int(owner),
    "phase": phase,
    "repo_path": repo,
    "rollback_snapshot": snapshot,
    "pre_update_head": head,
    "original_origin_url": origin,
    "stable_commit": stable,
    "deployed_receipt": deployed_receipt,
    "updated_at": time.time(),
    "service_targets": [item for item in service_targets.split(",") if item],
}
if patch_only == "1":
    payload["mode"] = ("cua-compat-only" if os.environ.get("CUA_COMPAT_ONLY") == "1" else "cron-completion-delivery-only" if os.environ.get("CRON_COMPLETION_DELIVERY_ONLY") == "1" else "patch-only")
if plugin_repo and plugin_snapshot and plugin_head:
    payload["extra_targets"] = [{
        "repo_path": plugin_repo,
        "rollback_snapshot": plugin_snapshot,
        "pre_update_head": plugin_head,
    }]
if desktop_target and desktop_snapshot and desktop_hash:
    payload["desktop_target"] = {
        "target_path": desktop_target,
        "rollback_snapshot": desktop_snapshot,
        "pre_update_hash": desktop_hash,
    }
tmp = f"{path}.tmp.{owner}"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, path)
dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
PY
}

restore_origin_url(){
  local cleanup_rc=0
  if [ "$ORIGIN_URL_SWAPPED" = "1" ] && [ -n "$ORIGINAL_ORIGIN_URL" ]; then
    if git -C "$REPO_DIR" remote set-url origin "$ORIGINAL_ORIGIN_URL"; then
      ORIGIN_URL_SWAPPED=0
    else
      log "ERROR: failed to restore the production origin URL"
      cleanup_rc=1
    fi
  fi
  if [ -n "$STABLE_STAGING_ROOT" ] && [ -d "$STABLE_STAGING_ROOT" ]; then
    rm -rf "$STABLE_STAGING_ROOT"
  fi
  return "$cleanup_rc"
}

desktop_restore_snapshot(){
  local failed="${DESKTOP_TARGET}.failed-update-$(date +%Y%m%d-%H%M%S)-$$"
  if [ ! -d "$DESKTOP_ROLLBACK_SNAPSHOT" ]; then
    log "ERROR: Desktop rollback snapshot is missing: $DESKTOP_ROLLBACK_SNAPSHOT"
    return 1
  fi
  if [ -d "$DESKTOP_TARGET" ] && ! mv "$DESKTOP_TARGET" "$failed"; then
    log "ERROR: could not preserve failed Desktop app at $failed"
    return 1
  fi
  if ! mv "$DESKTOP_ROLLBACK_SNAPSHOT" "$DESKTOP_TARGET"; then
    log "ERROR: could not restore Desktop rollback snapshot"
    [ -d "$failed" ] && [ ! -d "$DESKTOP_TARGET" ] && mv "$failed" "$DESKTOP_TARGET" 2>/dev/null || true
    return 1
  fi
  if [ "$(desktop_tree_sha256 "$DESKTOP_TARGET" 2>/dev/null || true)" != "$DESKTOP_PRE_HASH" ]; then
    log "ERROR: restored Desktop app integrity differs from pre-update snapshot"
    return 1
  fi
  log "Desktop rollback snapshot restored; failed app preserved at $failed"
  return 0
}

desktop_activate(){
  [ "$PATCH_ONLY_DESKTOP" = "1" ] || return 0
  local staged="${DESKTOP_TARGET}.update-new-$$" failed="${DESKTOP_TARGET}.failed-update-activation-$$"
  if [ -e "$staged" ] || [ -e "$failed" ]; then
    log "ERROR: Desktop staging path already exists"
    return 1
  fi
  if ! ditto "$DESKTOP_ARTIFACT" "$staged"; then
    log "ERROR: could not stage prebuilt Desktop artifact"
    return 1
  fi
  if [ "$(desktop_tree_sha256 "$staged" 2>/dev/null || true)" != "$DESKTOP_EXPECTED_HASH" ]; then
    log "ERROR: staged Desktop app integrity differs from approved artifact"
    rm -rf "$staged"
    return 1
  fi
  # Two renames make recovery deterministic, but are not a claim of a single
  # atomic replacement. The journaled snapshot remains armed through health.
  if ! mv "$DESKTOP_TARGET" "$failed" || ! mv "$staged" "$DESKTOP_TARGET"; then
    log "ERROR: Desktop replacement failed; EXIT rollback will restore snapshot"
    return 1
  fi
  if [ "$(desktop_tree_sha256 "$DESKTOP_TARGET" 2>/dev/null || true)" != "$DESKTOP_EXPECTED_HASH" ]; then
    log "ERROR: installed Desktop app integrity mismatch; EXIT rollback will restore snapshot"
    return 1
  fi
  log "Verified: prebuilt Desktop app installed from the approved hash-bound artifact."
  return 0
}

cleanup_synthetic_refs(){
  # This runs after checkout has returned to managed main. If checkout rollback
  # failed, Git will refuse to delete the active branch and preserve evidence.
  [ -n "$STABLE_TAG" ] || return 0
  git -C "$REPO_DIR" update-ref -d "refs/heads/$STABLE_UPDATE_BRANCH" 2>/dev/null || true
  git -C "$REPO_DIR" update-ref -d "refs/remotes/origin/$STABLE_UPDATE_BRANCH" 2>/dev/null || true
}

ensure_system_daemon(){
  local service="$1"
  local plist="/Library/LaunchDaemons/ai.hermes.$service.plist"
  if sudo -n launchctl print "system/ai.hermes.$service" >/dev/null 2>&1; then
    return 0
  fi
  sudo -n launchctl bootstrap system "$plist" >/dev/null 2>&1 && \
    sudo -n launchctl print "system/ai.hermes.$service" >/dev/null 2>&1
}

ensure_gui_agent(){
  local profile="$1"
  local label="com.hermes.chrome-debug-$profile"
  local plist="$AISERVER_HOME/Library/LaunchAgents/$label.plist"
  if launchctl print "gui/$GUI_UID/$label" >/dev/null 2>&1; then
    return 0
  fi
  launchctl bootstrap "gui/$GUI_UID" "$plist" >/dev/null 2>&1 && \
    launchctl print "gui/$GUI_UID/$label" >/dev/null 2>&1
}

stop_system_daemon(){
  local service="$1"
  local domain="system/ai.hermes.$service"
  if ! sudo -n launchctl print "$domain" >/dev/null 2>&1; then
    return 0
  fi
  sudo -n launchctl bootout "$domain" >/dev/null 2>&1 || true
  local attempt
  for attempt in {1..40}; do
    if ! sudo -n launchctl print "$domain" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

stop_gui_agent(){
  local profile="$1"
  local domain="gui/$GUI_UID/com.hermes.chrome-debug-$profile"
  if ! launchctl print "$domain" >/dev/null 2>&1; then
    return 0
  fi
  launchctl bootout "$domain" >/dev/null 2>&1 || true
  local attempt
  for attempt in {1..40}; do
    if ! launchctl print "$domain" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

# Use the scheduler's actual cross-process jobs fence only through the first
# gateway stop. Holding it longer is unsafe: scheduler lock contention degrades
# after 30 seconds, whereas gateway-default stop is bounded to 10 seconds.
CRON_WORKER_GUARD_PID=""
CRON_WORKER_GUARD_OUT=""
CRON_WORKER_JOB_ID="493d8cbbde22"
CRON_WORKER_MIN_IDLE_SECONDS=900
# CLT SQLite cannot establish this production ledger's normal WAL read snapshot
# when its coordination files are absent.  The Homebrew runtime has the SQLite
# WAL reader needed by the approved normal mode=ro contract.  It may create the
# native WAL/SHM coordination files, so this selector remains pending explicit
# authorization for that production filesystem side effect.
CRON_LEDGER_PYTHON="/opt/homebrew/bin/python3"
release_cron_worker_guard(){
  local pid="${CRON_WORKER_GUARD_PID:-}" out="${CRON_WORKER_GUARD_OUT:-}"
  if [ -n "$pid" ]; then kill "$pid" >/dev/null 2>&1 || true; wait "$pid" >/dev/null 2>&1 || true; fi
  [ -z "$out" ] || rm -f "$out"
  CRON_WORKER_GUARD_PID=""; CRON_WORKER_GUARD_OUT=""
}
acquire_cron_worker_guard(){
  [ "$CRON_COMPLETION_DELIVERY_ONLY" = "1" ] || return 0
  if [ ! -x "$CRON_LEDGER_PYTHON" ]; then
    log "ERROR: compatible SQLite ledger runtime is unavailable: $CRON_LEDGER_PYTHON"
    return 1
  fi
  CRON_WORKER_GUARD_OUT=$(mktemp "${TMPDIR:-/tmp}/hermes-cron-worker-guard.XXXXXX") || return 1
  "$CRON_LEDGER_PYTHON" - "$AISERVER_HOME/.hermes" "$CRON_WORKER_JOB_ID" "$CRON_WORKER_MIN_IDLE_SECONDS" >"$CRON_WORKER_GUARD_OUT" 2>&1 <<'PY' &
import datetime as dt
import fcntl
import json
import signal
import sqlite3
import sys
import time
from pathlib import Path
home, job_id, minimum = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
def blocked(message):
    print(f"BLOCKED: {message}", flush=True)
    raise SystemExit(1)
cron = home / "cron"
try:
    lock = (cron / ".jobs.lock").open("a+", encoding="utf-8")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except (OSError, IOError) as exc:
    blocked(f"could not acquire the scheduler jobs lock {cron / '.jobs.lock'}: {exc}")
try:
    raw = json.loads((cron / "jobs.json").read_text(encoding="utf-8"))
except Exception as exc:
    blocked(f"could not read scheduler jobs ledger: {exc}")
jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
if not isinstance(jobs, list): blocked("scheduler jobs ledger has an unexpected shape")
job = next((item for item in jobs if isinstance(item, dict) and item.get("id") == job_id), None)
if job is None: blocked(f"protected Buried City job {job_id} was not found")
if job.get("fire_claim"): blocked("protected Buried City job has a fire claim")
# The jobs lock fences scheduler admission through the bounded first gateway
# stop, but the execution ledger has independent SQLite coordination; this is
# one normal WAL snapshot at the stop boundary, not an absolute writer guarantee.
# Do not weaken it with immutable=1/nolock=1 or a raw-copy bypass.
conn = None
try:
    conn = sqlite3.connect(f"file:{cron / 'executions.db'}?mode=ro", uri=True, timeout=1)
    rows = conn.execute("SELECT id FROM executions WHERE job_id=? AND status IN ('claimed','running')", (job_id,)).fetchall()
except Exception as exc:
    blocked(f"could not read scheduler execution ledger: {exc}")
finally:
    if conn is not None:
        conn.close()
if rows: blocked("protected Buried City execution is active: " + ", ".join(str(row[0]) for row in rows))
# The only approved relaxation is the supported paused state.  A merely disabled
# or malformed job is not evidence that a future start cannot race this stop.
if job.get("enabled") is False and job.get("state") == "paused":
    print(f"READY: protected Buried City is quiescent and explicitly paused; holding {cron / '.jobs.lock'}", flush=True)
else:
    if job.get("enabled") is False:
        blocked("protected Buried City is disabled but not explicitly paused")
    next_run = job.get("next_run_at")
    if not isinstance(next_run, str): blocked("protected Buried City next-run timestamp is unavailable")
    try:
        when = dt.datetime.fromisoformat(next_run)
        if when.tzinfo is None: blocked("protected Buried City next-run timestamp is timezone-naive")
    except ValueError as exc:
        blocked(f"protected Buried City next-run timestamp is invalid: {exc}")
    remaining = (when - dt.datetime.now(when.tzinfo)).total_seconds()
    if remaining < minimum: blocked(f"only {remaining:.0f}s until the protected Buried City next run; require >= {minimum}s")
    print(f"READY: protected Buried City is quiescent; {remaining:.0f}s until next run; holding {cron / '.jobs.lock'}", flush=True)
stop = False
def finish(_signum, _frame):
    global stop
    stop = True
signal.signal(signal.SIGTERM, finish)
signal.signal(signal.SIGINT, finish)
while not stop: time.sleep(0.1)
PY
  CRON_WORKER_GUARD_PID=$!
  local wait_until=$(( $(date +%s) + 5 ))
  while :; do
    if grep -q '^READY:' "$CRON_WORKER_GUARD_OUT" 2>/dev/null; then log "Verified protected-worker quiescence at the pre-stop boundary."; return 0; fi
    if ! kill -0 "$CRON_WORKER_GUARD_PID" >/dev/null 2>&1; then cat "$CRON_WORKER_GUARD_OUT" >&2 || true; wait "$CRON_WORKER_GUARD_PID" 2>/dev/null || true; CRON_WORKER_GUARD_PID=""; rm -f "$CRON_WORKER_GUARD_OUT"; CRON_WORKER_GUARD_OUT=""; return 1; fi
    if [ "$(date +%s)" -ge "$wait_until" ]; then log "ERROR: timed out acquiring protected-worker scheduler guard"; release_cron_worker_guard; return 1; fi
    /bin/sleep 0.05
  done
}

restore_services(){
  local restore_rc=$?
  local rollback_safe=1
  local cleanup_rc=0
  local s p
  trap - EXIT
  set +e

  # Release the scheduler fence before any potentially long recovery path.
  release_cron_worker_guard
  # Restore the real remote before any rollback sync. This must run on every
  # exit, including failures before the inner updater starts.
  restore_origin_url || cleanup_rc=1

  if [ "$DESKTOP_STAGING_ACTIVE" = "1" ]; then
    log "Restoring Desktop rollback snapshot..."
    desktop_restore_snapshot || { rollback_safe=0; cleanup_rc=1; }
  fi

  if [ "$UPDATE_STAGING_ACTIVE" = "1" ] && [ -n "$PRE_UPDATE_HEAD" ]; then
    log "Restoring APFS rollback snapshot for pre-update HEAD $PRE_UPDATE_HEAD..."
    local failed_tree="${REPO_DIR}.failed-update-$(date +%Y%m%d-%H%M%S)-$$"
    if [ ! -d "$ROLLBACK_SNAPSHOT" ]; then
      log "ERROR: rollback snapshot is missing: $ROLLBACK_SNAPSHOT"
      rollback_safe=0
      cleanup_rc=1
    elif ! mv "$REPO_DIR" "$failed_tree"; then
      log "ERROR: could not preserve the failed checkout at $failed_tree"
      rollback_safe=0
      cleanup_rc=1
    elif ! mv "$ROLLBACK_SNAPSHOT" "$REPO_DIR"; then
      log "ERROR: could not move rollback snapshot into production path"
      mv "$failed_tree" "$REPO_DIR" 2>/dev/null || true
      rollback_safe=0
      cleanup_rc=1
    elif [ "$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)" != "$PRE_UPDATE_HEAD" ]; then
      log "ERROR: restored rollback snapshot does not match $PRE_UPDATE_HEAD"
      rollback_safe=0
      cleanup_rc=1
    else
      log "Rollback snapshot restored; failed tree preserved at $failed_tree"
    fi
  fi

  if [ "$PLUGIN_STAGING_ACTIVE" = "1" ] && [ -n "$PLUGIN_PRE_UPDATE_HEAD" ]; then
    log "Restoring plugin rollback snapshot for pre-update HEAD $PLUGIN_PRE_UPDATE_HEAD..."
    local plugin_failed_tree
    plugin_failed_tree="${PLUGIN_DIR}.failed-update-$(date +%Y%m%d-%H%M%S)-$$"
    if [ ! -d "$PLUGIN_ROLLBACK_SNAPSHOT" ]; then
      log "ERROR: plugin rollback snapshot is missing: $PLUGIN_ROLLBACK_SNAPSHOT"
      rollback_safe=0
      cleanup_rc=1
    elif ! mv "$PLUGIN_DIR" "$plugin_failed_tree"; then
      log "ERROR: could not preserve the failed plugin checkout at $plugin_failed_tree"
      rollback_safe=0
      cleanup_rc=1
    elif ! mv "$PLUGIN_ROLLBACK_SNAPSHOT" "$PLUGIN_DIR"; then
      log "ERROR: could not move plugin rollback snapshot into place"
      mv "$plugin_failed_tree" "$PLUGIN_DIR" 2>/dev/null || true
      rollback_safe=0
      cleanup_rc=1
    elif [ "$(git -C "$PLUGIN_DIR" rev-parse HEAD 2>/dev/null)" != "$PLUGIN_PRE_UPDATE_HEAD" ]; then
      log "ERROR: restored plugin rollback snapshot does not match $PLUGIN_PRE_UPDATE_HEAD"
      rollback_safe=0
      cleanup_rc=1
    else
      log "Plugin rollback snapshot restored; failed tree preserved at $plugin_failed_tree"
    fi
  fi

  # Record "rolled-back" only once every armed target is back; a partial
  # rollback keeps the mutating phase so the guardian retries the remainder.
  if { [ "$UPDATE_STAGING_ACTIVE" = "1" ] || [ "$PLUGIN_STAGING_ACTIVE" = "1" ] || [ "$DESKTOP_STAGING_ACTIVE" = "1" ]; } && [ "$rollback_safe" -eq 1 ]; then
    write_update_journal "rolled-back" || cleanup_rc=1
  fi

  cleanup_synthetic_refs

  # Supervision restoration is unconditional. Even a rollback failure must not
  # leave launchd jobs unloaded; launchd will retain/retry the canonical jobs
  # while the preserved journal and failed tree expose the stronger fault.
  if [ "$PATCH_ONLY" != "1" ]; then
    log "Restoring chrome-debug agents..."
    for p in "${CHROME_AGENTS[@]}"; do
      ensure_gui_agent "$p" || { log "ERROR: failed to restore supervised agent com.hermes.chrome-debug-$p"; cleanup_rc=1; }
    done
  fi
  log "Restoring only the system-supervised Hermes services stopped by this transaction..."
  for s in "${UPDATE_SERVICE_DAEMONS[@]}"; do
    ensure_system_daemon "$s" || { log "ERROR: failed to restore system/ai.hermes.$s supervision"; cleanup_rc=1; }
  done

  # Keep the marker through normal post-start health verification. On failure,
  # release only after rollback and every supervisor restoration attempt passed;
  # otherwise leave the transaction for the independent guardian to retry.
  if [ "$KEEP_MARKER_THROUGH_HEALTH" != "1" ]; then
    if [ "$cleanup_rc" -eq 0 ] && [ "$rollback_safe" -eq 1 ]; then
      release_update_marker
    else
      log "ERROR: cleanup incomplete; retaining update marker for guardian recovery"
    fi
  fi

  if [ "$restore_rc" -eq 0 ] && [ "$cleanup_rc" -ne 0 ]; then
    restore_rc=$cleanup_rc
  fi
  return "$restore_rc"
}
# Always clean temporary Git state and restore supervision on ordinary exits.
# Catchable fatal signals are converted into an exit so the EXIT transaction
# cleanup runs. SIGKILL is handled independently by hermes-update-guardian.py.
trap restore_services EXIT
on_update_signal(){
  local signal="$1"
  trap - HUP INT TERM
  case "$signal" in
    HUP) exit 129 ;;
    INT) exit 130 ;;
    TERM) exit 143 ;;
    *) exit 1 ;;
  esac
}
trap 'on_update_signal HUP' HUP
trap 'on_update_signal INT' INT
trap 'on_update_signal TERM' TERM

# Persist rollback identity before pausing anything. The health watchdog can
# safely restore services if this process dies at any later point.
PRE_UPDATE_HEAD=$(git -C "$REPO_DIR" rev-parse HEAD)
ORIGINAL_ORIGIN_URL=$(git -C "$REPO_DIR" config --get remote.origin.url)
if [ -z "$ORIGINAL_ORIGIN_URL" ]; then
  log "ERROR: production checkout has no origin URL"
  exit 1
fi
if [ "$PATCH_ONLY" = "1" ] && [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
  PLUGIN_PRE_UPDATE_HEAD=$(git -C "$PLUGIN_DIR" rev-parse HEAD)
  if [ -z "$PLUGIN_PRE_UPDATE_HEAD" ]; then
    log "ERROR: cannot resolve plugin HEAD before pausing services"
    exit 1
  fi
fi
write_update_journal "services-pausing"

log "Pausing selected Hermes services (gateway-health remains loaded as recovery guardian)..."
if [ "$CRON_COMPLETION_DELIVERY_ONLY" = "1" ]; then
  if [ "${UPDATE_SERVICE_DAEMONS[0]:-}" != "gateway-default" ]; then log "ERROR: protected-worker selector lost gateway-default-first service scope"; exit 1; fi
  acquire_cron_worker_guard || { log "ERROR: protected-worker pre-stop guard failed; nothing was paused"; exit 1; }
  if ! stop_system_daemon gateway-default; then
    release_cron_worker_guard
    log "ERROR: failed to stop system/ai.hermes.gateway-default; refusing to mutate"
    exit 1
  fi
  release_cron_worker_guard
  for s in "${UPDATE_SERVICE_DAEMONS[@]:1}"; do
    if ! stop_system_daemon "$s"; then log "ERROR: failed to stop system/ai.hermes.$s; refusing to mutate"; exit 1; fi
  done
else
  for s in "${UPDATE_SERVICE_DAEMONS[@]}"; do
    if ! stop_system_daemon "$s"; then log "ERROR: failed to stop system/ai.hermes.$s; refusing to mutate"; exit 1; fi
  done
fi
if [ "$PATCH_ONLY" != "1" ]; then
  log "Pausing chrome-debug agents (they hold sqlite locks in profile dirs)..."
  for p in "${CHROME_AGENTS[@]}"; do
    if ! stop_gui_agent "$p"; then
      log "ERROR: failed to stop gui/$GUI_UID/com.hermes.chrome-debug-$p; refusing to mutate"
      exit 1
    fi
  done
fi

# Snapshot the exact known-good checkout + managed venv after long-lived code
# users are stopped but before the first Git/venv mutation. APFS clonefile keeps
# this fast and copy-on-write, but the clone decays into real bytes as the live
# tree is rewritten (about 2-3 GiB per update in practice) and a plain-copy
# fallback needs the full ~5.5 GiB. preflight_free_space (called before the
# detached session is even submitted) therefore refuses to start below
# MIN_FREE_GIB (15) free on the Data volume. (This comment used to claim a
# "51 GiB free-space preflight" that did not exist.)
write_update_journal "snapshotting"
mkdir -p "$ROLLBACK_ROOT"
if ! cp -cRp "$REPO_DIR" "$ROLLBACK_SNAPSHOT"; then
  log "ERROR: could not create rollback snapshot; refusing checkout mutation"
  exit 1
fi
if [ "$(git -C "$ROLLBACK_SNAPSHOT" rev-parse HEAD 2>/dev/null)" != "$PRE_UPDATE_HEAD" ]; then
  log "ERROR: rollback snapshot HEAD verification failed"
  exit 1
fi
if [ "$PATCH_ONLY" = "1" ] && [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ]; then
  if ! cp -cRp "$PLUGIN_DIR" "$PLUGIN_ROLLBACK_SNAPSHOT"; then
    log "ERROR: could not create plugin rollback snapshot; refusing checkout mutation"
    exit 1
  fi
  if [ "$(git -C "$PLUGIN_ROLLBACK_SNAPSHOT" rev-parse HEAD 2>/dev/null)" != "$PLUGIN_PRE_UPDATE_HEAD" ]; then
    log "ERROR: plugin rollback snapshot HEAD verification failed"
    exit 1
  fi
  PLUGIN_JOURNAL_DIR="$PLUGIN_DIR"
fi
if [ "$PATCH_ONLY_DESKTOP" = "1" ]; then
  DESKTOP_ROLLBACK_SNAPSHOT="$ROLLBACK_ROOT/desktop-previous.app"
  if ! ditto "$DESKTOP_TARGET" "$DESKTOP_ROLLBACK_SNAPSHOT"; then
    log "ERROR: could not create Desktop rollback snapshot; refusing mutation"
    exit 1
  fi
  if [ "$(desktop_tree_sha256 "$DESKTOP_ROLLBACK_SNAPSHOT" 2>/dev/null || true)" != "$DESKTOP_PRE_HASH" ]; then
    log "ERROR: Desktop rollback snapshot integrity verification failed"
    exit 1
  fi
fi
write_update_journal "mutation-armed"
UPDATE_STAGING_ACTIVE=1
[ "$PATCH_ONLY" = "1" ] && [ "$PATCH_ONLY_HAS_PLUGIN" = "1" ] && PLUGIN_STAGING_ACTIVE=1
[ "$PATCH_ONLY_DESKTOP" = "1" ] && DESKTOP_STAGING_ACTIVE=1

if [ "$PATCH_ONLY" != "1" ]; then
# The upstream CLI updater is branch-oriented and insists on fetching its target
# from origin. A release tag and a same-named local branch are ambiguous to Git:
# `rev-parse --abbrev-ref HEAD` normalizes the branch to `heads/<tag>`, causing
# the updater's final branch check to reject its own successful merge. Use a
# collision-free synthetic branch served by a temporary local bare remote. The
# resolver's peeled commit remains the immutable source of truth; the real
# origin URL is restored on every exit before services can restart.
STABLE_STAGING_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/hermes-stable-origin.XXXXXX")
STABLE_STAGING_REMOTE="$STABLE_STAGING_ROOT/origin.git"
if ! git clone -q --bare --shared "$REPO_DIR" "$STABLE_STAGING_REMOTE"; then
  log "ERROR: could not create isolated stable staging remote"
  exit 1
fi
if ! git --git-dir="$STABLE_STAGING_REMOTE" update-ref \
  "refs/heads/$STABLE_UPDATE_BRANCH" "$STABLE_COMMIT"; then
  log "ERROR: could not pin staging branch $STABLE_UPDATE_BRANCH"
  exit 1
fi
if ! git -C "$REPO_DIR" checkout -q -B "$STABLE_UPDATE_BRANCH" "$STABLE_BASE"; then
  log "ERROR: could not stage stable update branch $STABLE_UPDATE_BRANCH"
  exit 1
fi
if ! git -C "$REPO_DIR" remote set-url origin "$STABLE_STAGING_REMOTE"; then
  log "ERROR: could not isolate the updater from the moving production remote"
  exit 1
fi
ORIGIN_URL_SWAPPED=1

log "Updating aiserver install to $STABLE_TAG via $STABLE_UPDATE_BRANCH ($BACKUP_FLAG)..."
write_update_journal "updating"
INNER_UPDATE_OUT=$(mktemp "${TMPDIR:-/tmp}/hermes-inner-update.XXXXXX") || INNER_UPDATE_OUT=""
# Initial handoff intentionally resolves the PRE-update tree. Its historical
# updater owns the post-swap PM completion child; do not run new imports here.
hermes_runtime_run "$REPO_DIR" cli update --yes "$BACKUP_FLAG" --branch "$STABLE_UPDATE_BRANCH" 2>&1 | tee ${INNER_UPDATE_OUT:+"$INNER_UPDATE_OUT"}
AISERVER_RC=${PIPESTATUS[0]}

# Upstream ships the fix for the post-swap mixed-module TypeError IN the release we are
# moving TO (drop the stale submodule attribute in the post-pull purge; finish the update in
# an interpreter born on the pulled code). It cannot protect the hop INTO that release,
# because the OLD interpreter drives it: new `dashboard_procs` calls old `main_dashboard`
# via the protected parent package's stale submodule attribute and raises
#   TypeError: _find_stale_dashboard_pids() got an unexpected keyword argument 'scope_home'
# That fires in post-update dashboard cleanup, AFTER the checkout+deps already landed, so a
# fully-successful update is reported as exit 1 and rolled back. Accept that ONE case only
# when the update verifiably completed: the updater printed its completion banner AND the
# checkout is parked exactly on the pinned candidate commit. Everything else stays fatal,
# and this whole branch self-extinguishes once we are running the fixed release.
if [ $AISERVER_RC -ne 0 ] && [ -n "$INNER_UPDATE_OUT" ] && [ -f "$INNER_UPDATE_OUT" ]; then
  if grep -q "_find_stale_dashboard_pids() got an unexpected keyword argument 'scope_home'" "$INNER_UPDATE_OUT" \
    && grep -q "Update complete!" "$INNER_UPDATE_OUT"; then
    INNER_HEAD=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null)
    if [ "$INNER_HEAD" = "$STABLE_COMMIT" ]; then
      log "NOTE: inner updater exited $AISERVER_RC in post-update dashboard cleanup (known upstream"
      log "      cross-version TypeError, fixed in $STABLE_TAG itself). Update verified complete:"
      log "      banner printed and HEAD is pinned candidate ${STABLE_COMMIT}. Treating as success."
      AISERVER_RC=0
    else
      log "ERROR: post-update cleanup TypeError seen but HEAD ($INNER_HEAD) is not the pinned"
      log "       candidate ($STABLE_COMMIT); treating as a real failure"
    fi
  fi
fi
rm -f ${INNER_UPDATE_OUT:+"$INNER_UPDATE_OUT"}
if ! restore_origin_url; then
  log "ERROR: inner update completed but production origin restoration failed"
  AISERVER_RC=1
fi
if [ $AISERVER_RC -ne 0 ]; then
  log "ERROR: stable updater exited $AISERVER_RC; refusing carried-patch replay"
  exit 1
fi
if [ "$(git -C "$REPO_DIR" rev-parse HEAD)" != "$STABLE_COMMIT" ]; then
  log "ERROR: updater did not land exact pinned commit $STABLE_COMMIT"
  exit 1
fi
git -C "$REPO_DIR" checkout -q -B main "$STABLE_COMMIT" || {
  log "ERROR: could not attach main to the stable commit"
  exit 1
}
cleanup_synthetic_refs
write_update_journal "patching"

# The staged deployment remains rollback-active until every carried patch,
# generated lock, installed client version, and compile gate has passed. The
# carried patches below are the source of truth for local changes, so always
# establish the exact pinned stable baseline before reapplying them.
REPO_DIR="$AISERVER_HOME/.hermes/hermes-agent"
if [ "$(id -u)" -eq 0 ]; then GITCMD=(sudo -u aiserver -H git); else GITCMD=(git); fi
log "Resetting Hermes checkout to pinned stable baseline $STABLE_TAG ($STABLE_COMMIT)..."
if ! "${GITCMD[@]}" -C "$REPO_DIR" reset --hard "$STABLE_COMMIT" >/dev/null; then
  log "ERROR: could not reset Hermes checkout to pinned stable commit; refusing to apply carried patches"
  exit 1
fi
fi
# --- end ordinary-mode stable update ---------------------------------------

# --- Re-apply carried local patches (dropped by `hermes update` reset) ---
# Static .patch files in ~/.hermes/scripts/patches/, detected by a content
# marker unique to each patch (grep for a symbol/string the patch introduces)
# rather than a pinned commit SHA. This mirrors the Merittas Forge install's
# proven pattern (~/.hermes/scripts/hermes_safe_update.sh there) instead of
# ai-server's previous SHA-list approach (carried-patches.txt, retired
# 2026-07-16): cherry-picking regenerates a new SHA on every reapply, so a
# SHA list silently goes stale the moment the source feature branch is
# deleted and the old commit object is garbage-collected — at which point
# `hermes update`'s `git reset --hard origin/main` fallback (triggered
# whenever local commits diverge from upstream, which they always do here)
# would wipe these carried fixes with NO surviving way to reapply them.
# Patch files don't have this failure mode — they're content, not a git
# object reachability bet.
#
# Each block: skip if the marker is already present (patch is already in
# HEAD, from this reapply or from upstream having independently merged an
# equivalent change); otherwise `git apply --3way` + commit. Non-fatal on
# failure — logs loudly so it's never a silent gap, matching Merittas'
# WARNING style.
PATCH_DIR="$AISERVER_HOME/.hermes/scripts/patches"
REPO_DIR="$AISERVER_HOME/.hermes/hermes-agent"
if [ "$(id -u)" -eq 0 ]; then GITCMD=(sudo -u aiserver -H git); else GITCMD=(git); fi

# Every reapply_carried_patch call records "marker_file|marker_string|label"
# here so the tree can be re-verified after the update (see the verification
# pass after the carried-patch block).
CARRIED_PATCH_CHECKS=""
CARRIED_PATCH_MISSING=""
CARRIED_PATCH_MISSING_COUNT=0
CARRIED_PATCH_TOTAL=0

reapply_carried_patch() {
  local patch_file="$1" marker_file="$2" marker_string="$3" label="$4" commit_msg="$5"
  CARRIED_PATCH_CHECKS="${CARRIED_PATCH_CHECKS}${marker_file}|${marker_string}|${label}
"
  if grep -Fq "$marker_string" "$REPO_DIR/$marker_file" 2>/dev/null; then
    log "  carried patch '$label' already present"
    return 0
  fi
  if [ ! -f "$PATCH_DIR/$patch_file" ]; then
    log "  ERROR: carried patch '$label' missing ($PATCH_DIR/$patch_file not found) — fix absent until restored manually"
    return 1
  fi
  if "${GITCMD[@]}" -C "$REPO_DIR" apply --3way --whitespace=nowarn "$PATCH_DIR/$patch_file" 2>/dev/null; then
    # `git apply --3way` exiting 0 means "the hunks applied", NOT "the hunks
    # were correct". A patch regenerated against a STALE BASE contains reverse
    # hunks of newer upstream work, and those apply just as cleanly. That is
    # exactly how merittas-ai went down on 2026-08-21: a carried patch silently
    # deleted USER_PROFILE_GUIDANCE, the updater saw exit 0, committed, and
    # restarted into an ImportError on every turn.
    #
    # So before trusting this apply, ask whether it DELETED upstream code.
    # Scoped to this patch (--patch) so one bad patch is attributed and rolled
    # back on its own, without condemning the others.
    # `git apply --3way` can also report "Applied patch ... with conflicts" while
    # leaving unmerged stages in the index. Treat any unmerged entry as a failed
    # apply: never commit conflict markers as if the patch landed.
    if [ -n "$("${GITCMD[@]}" -C "$REPO_DIR" ls-files -u 2>/dev/null)" ]; then
      "${GITCMD[@]}" -C "$REPO_DIR" reset --hard HEAD >/dev/null 2>&1 || true
      log "  ERROR: carried patch '$label' left unmerged index entries (3-way conflict) — rolled back, fix absent until re-ported against current upstream"
      return 1
    fi
    local staleness_check="$AISERVER_HOME/.hermes/scripts/hermes-check-patch-staleness.py"
    local stale_out="" stale_rc=0
    if [ -f "$staleness_check" ]; then
      # Pass the repo/patch dir explicitly so the detector inspects exactly the
      # tree this helper is patching, never whatever `~` happens to resolve to.
      stale_out="$(HERMES_AGENT_REPO="$REPO_DIR" HERMES_PATCH_DIR="$PATCH_DIR" \
        python3 "$staleness_check" --base "${STABLE_COMMIT:-HEAD}" --patch "$patch_file" --quiet 2>&1)" || stale_rc=$?
      # Only rc 1 (positive evidence of deletion) blocks. rc 2 means "couldn't
      # check" (no origin ref, unparseable file) and must never brick an update.
      if [ "$stale_rc" -eq 1 ]; then
        log "  ERROR: carried patch '$label' applied cleanly but DELETED upstream symbols — regenerated against a stale base; rolling it back"
        printf '%s\n' "$stale_out" | while IFS= read -r line; do
          log "    $line"
        done
        # Roll back to the last known-good commit. `git checkout -- <path>`
        # would be wrong here: --3way already STAGED its result, so the index
        # holds the poisoned content and checkout would restore the damage.
        "${GITCMD[@]}" -C "$REPO_DIR" reset --hard HEAD >/dev/null 2>&1 || true
        log "  carried patch '$label' rolled back — fix absent until re-ported against current upstream"
        return 1
      elif [ "$stale_rc" -ne 0 ]; then
        # rc 2 = "could not check": non-blocking by design, but never silent.
        log "  WARNING: staleness detector could not check carried patch '$label' (rc $stale_rc): $(printf '%s' "$stale_out" | head -n 1)"
      fi
    else
      # Never skip the gate silently: a missing detector is exactly the
      # condition that let the 2026-08-21 outage through, and a quiet skip
      # looks identical to a clean pass in the log.
      log "  WARNING: staleness detector not found at $staleness_check — carried patch '$label' applied WITHOUT a stale-base check"
    fi
    "${GITCMD[@]}" -C "$REPO_DIR" add -A
    if ! "${GITCMD[@]}" -C "$REPO_DIR" commit -m "$commit_msg" >/dev/null 2>&1; then
      # A patch that applied but did not commit is not "re-applied": the next
      # block's commit would absorb it, or a dirty tree would reach validation.
      "${GITCMD[@]}" -C "$REPO_DIR" reset --hard HEAD >/dev/null 2>&1 || true
      log "  ERROR: carried patch '$label' applied but could not be committed — rolled back, fix absent until fixed manually"
      return 1
    fi
    log "  carried patch '$label' re-applied"
    return 0
  else
    # `git apply --3way` can leave both conflict markers in the worktree and
    # unmerged stages in the index. `git checkout -- .` only cleans the
    # worktree; a later `git add -A && git commit` can then commit those
    # conflict markers as part of an unrelated carried patch. Reset the whole
    # failed attempt back to the last known-good commit instead.
    "${GITCMD[@]}" -C "$REPO_DIR" reset --hard HEAD >/dev/null 2>&1 || true
    log "  ERROR: carried patch '$label' FAILED to apply (rejected — likely needs manual re-port against new upstream code) — fix absent until fixed manually"
    return 1
  fi
}

if [ "$PATCH_ONLY" = "1" ]; then
patch_only_apply_fix_set
else
if [ -d "$PATCH_DIR" ]; then
  log "Re-applying carried local patches..."

  # Repeated compression can bury a standalone todo snapshot before later
  # assistant/tool rows, causing each boundary to append another copy. The
  # refresh path must strip stale snapshots across the whole compacted context
  # before inserting the current one (upstream PR #96934).
  # [registry-closure 2026-09-08] MARKER FIXED: "_fold_todo_snapshot" already
  # exists in pristine v2026.9.7 (2 hits at 2237be3559), so this block skipped
  # on every replay and commit d2b003da05 never landed. New marker is the call
  # line only that commit adds (0 hits at base, 1 in candidate). Patch content
  # regenerated from d2b003da05 (was stale: rejected at conversation_compression.py:3406).
  reapply_carried_patch \
    "compression_buried_todo_dedupe.patch" \
    "agent/conversation_compression.py" \
    "_todo_snapshot_is_only_content(_todo_content, _todo_stripped)" \
    "compression-buried-todo-dedupe" \
    "fix(compression): dedupe buried todo snapshots [carried]"

  # A post-watermark row may already be represented in the compressor handoff.
  # Avoid inserting that copy and cloning the same durable source row again
  # during archive_and_compact (upstream PR #96934).
  # [release-package 2026-09-08] STALE PATH+MARKER, CORRECTED: upstream split
  # hermes_state.py -> hermes_state_messages.py (relocation) and the internal
  # identifier represented_untimed_occurrences -> represented_ids (rename,
  # same contract) as of commit e4fb8dcc87dff7d13e28173a845af474b5315867.
  # Verified via `git show e4fb8dcc87 --stat` + direct read of
  # hermes_state_messages.py:508-539 (_split_concurrent_tail). See
  # release-package/reports/report.json registration #2 for full evidence.
  reapply_carried_patch \
    "compression_watermark_overlap.REPORTED-v2026.9.24.patch" \
    "hermes_state_messages.py" \
    "_split_concurrent_tail" \
    "compression-watermark-overlap" \
    "fix(state): avoid cloning represented compaction tail [carried]"

  # Claude-OAuth trigger-phrase scrub for direct Claude and MoA routes. The
  # acting MoA model name hides Claude slots, so the helper resolves the preset
  # and scrubs when any reference/aggregator targets Claude. MEDIA: is
  # deliberately preserved because gateway attachment routing matches it
  # verbatim. This is one combined carried patch (ref hermes#10575).
  # RETIRED 2026-09-06: upstream #82154 (in v0.21.0, our installed base) reworded the filter-tripping sentence; merittas measured it safe 2026-09-01 (6/6 CLI A/B, 4/4 gateway runs, billing canary 0 hits). Code stays until the next reset+replay; if "out of extra usage" 400s return post-update, restore this block and the file from patches/retired/.
  # reapply_carried_patch \
  # "claude_oauth_scrub_main.patch" \
  # "agent/chat_completion_helpers.py" \
  # "def _moa_preset_targets_claude" \
  # "claude-oauth-scrub-main" \
  # "fix(auth): restore Claude OAuth scrub for direct and MoA routes [carried]"

  # Claude-OAuth scrub, max-iteration summary path (bypasses build_api_kwargs).
  # RETIRED 2026-09-06: upstream #82154 (in v0.21.0, our installed base) reworded the filter-tripping sentence; merittas measured it safe 2026-09-01 (6/6 CLI A/B, 4/4 gateway runs, billing canary 0 hits). Code stays until the next reset+replay; if "out of extra usage" 400s return post-update, restore this block and the file from patches/retired/.
  # reapply_carried_patch \
  # "claude_oauth_scrub_summary.patch" \
  # "agent/chat_completion_helpers.py" \
  # "See _scrub_claude_oauth_triggers" \
  # "claude-oauth-scrub-summary" \
  # "hermes-local: also scrub Claude-OAuth triggers on max-iteration summary path [carried]"

  # /model MoA-preset gating. DEFAULT_CONFIG ships a preset literally named
  # "default", and model_switch checked MoA presets (from the MERGED config)
  # before alias resolution -- so "/model default" silently switched a session
  # into MoA mode. Gate preset matching to presets the user wrote in raw
  # config.yaml (ours is named "council").
  # RETIRED 2026-09-08 (release-package worker): the registered marker
  # _VIRTUAL_ROUTING_PROVIDERS is not present anywhere in the candidate tree
  # and moa_preset_switch_rawconfig.patch does not apply -- but the actual
  # defect (#55187, /model default silently routing to MoA) is closed by a
  # different, already-committed mechanism: hermes_cli/moa_config.py's
  # exact_moa_preset_name() gate, wired into the no-explicit-provider switch
  # path at hermes_cli/model_switch.py:1188-1189. It intercepts before the
  # static-catalog lookup that would otherwise route to MoA, and additionally
  # honors per-preset `enabled` (disabled presets can't be silently matched).
  # detect_static_provider_for_model('default','anthropic') still returns
  # ('moa','default') at the static-catalog layer, but that path is never
  # reached for a bare `/model default` because exact_moa_preset_name
  # intercepts first. Verified live in this tree 2026-09-08; see
  # release-package/reports/report.json registration #3. Patch file remains
  # at patches/moa_preset_switch_rawconfig.patch for reference/rollback; not
  # moved to patches/retired/ by this worker (out of this task's file-move
  # scope) -- flagged for the next wrapper maintenance pass.
  # reapply_carried_patch \
  #   "moa_preset_switch_rawconfig.patch" \
  #   "hermes_cli/models.py" \
  #   "_VIRTUAL_ROUTING_PROVIDERS" \
  #   "moa-preset-switch-rawconfig" \
  #   "fix(model): MoA default-preset fix, final PR #71640 form (both routes) [carried]"

  # Optional-backend diagnostics guards (lazy_deps/hindsight/doctor).
  # [release-package 2026-09-08] STALE PATH, CORRECTED: upstream extracted
  # doctor_tools.py from doctor.py; marker string unchanged. Verified via
  # `git show 4ad0c9d37d --stat` + direct read of doctor_tools.py:55,111.
  reapply_carried_patch \
    "backend_diagnostics_guards.REPORTED-v2026.9.14.patch" \
    "hermes_cli/doctor_tools.py" \
    "def _is_default_off_toolset" \
    "backend-diagnostics-guards" \
    "fix(doctor): omit intentionally disabled toolsets [carried]"

  # /v1/skills 500: upstream a6ee31f55a (#94266, Collective Wisdom Agent V1)
  # calls _find_all_skills(include_editorial=True), but that function is
  # keyword-only `(*, skip_disabled=False)` and never accepted the kwarg — so
  # every request raised TypeError into the broad except and returned HTTP 500.
  # Surfaced as repeated /v1/skills failures from the Android relay client.
  # Bug is live upstream at v2026.9.11; drop this block when upstream fixes it.
  reapply_carried_patch \
    "api_server_skills_kwarg_fix.patch" \
    "gateway/platforms/api_server.py" \
    "_sort_skills(_find_all_skills(skip_disabled=False))" \
    "api-server-skills-kwarg-fix" \
    "fix(gateway): stop /v1/skills 500 from bad _find_all_skills kwarg [carried]"

  # Gateway runtime-status ownership: write_runtime_status() unconditionally
  # re-stamped kind/pid/argv/start_time, so a short-lived CLI process (notably
  # `hermes send`) building an adapter would overwrite the live gateway's
  # identity and leave gateway_state.json naming a dead PID once it exited —
  # the stale-state defect behind "default gateway absent" alerts while the
  # gateway was in fact running. Now only the runtime owner (lock handle, or
  # gateway argv before the lock is taken) may claim top-level identity.
  reapply_carried_patch \
    "gateway_status_runtime_ownership.REPORTED-v2026.9.24.patch" \
    "gateway/status.py" \
    "owns_runtime = owns_gateway_runtime_lock() or looks_like_gateway_runtime_command_line(" \
    "gateway-status-runtime-ownership" \
    "fix(gateway): only the runtime owner may stamp gateway status identity [carried]"

  # Default gateway argv ownership: _command_line_belongs_to_profile() assumed
  # the default gateway always runs bare and rejected ANY cmdline carrying a
  # profile flag. Our launchd daemon passes `--profile default`, so the live
  # default gateway matched no profile at all and `gateway list` /
  # _check_gateway_running called a healthy gateway "not running" — which is
  # what prompted a worker to delete the gateway-list health check rather than
  # fix it. Only a DIFFERENT profile on argv may disqualify the default home.
  reapply_carried_patch \
    "gateway_status_default_profile_argv.REPORTED-v2026.9.14.patch" \
    "gateway/status.py" \
    "if _named_profile is not None and _named_profile != \"default\":" \
    "gateway-status-default-profile-argv" \
    "fix(gateway): accept an explicitly-named default gateway on argv [carried]"

  # Plugin shared-checkout symlink fix (upstream PR #60343, open/unmerged as
  # of 2026-07-16). Once #60343 merges, delete this patch file and block —
  # don't let it silently duplicate/conflict with the real merge.
  reapply_carried_patch \
    "plugin_symlink_fix.patch" \
    "hermes_cli/plugins_cmd.py" \
    "allow_symlink" \
    "plugin-symlink-fix" \
    "fix(plugins): allow update/read to follow shared-checkout symlinks"

  # Discord clarify-button-label and 2,000-character fallback-content fix
  # (upstream PR #62291). Once #62291 merges, delete this patch file and block.
  # RETIRED 2026-09-06: Discord platform decommissioned 2026-09-01 (platforms.discord.enabled=false on both boxes). File moved to patches/retired/. Local commit 97e921afac disappears at the next reset+replay.
  # reapply_carried_patch \
  # "discord_clarify_button_label.patch" \
  # "plugins/platforms/discord/adapter.py" \
  # "[additional choice text truncated]" \
  # "discord-clarify-button-label" \
  # "fix(discord): make clarify button labels always legible"

  # Late-token reconnect fallback (upstream PR #68746). Non-multiplex gateways
  # may recover a rotated token after a supported runtime .env reload; multiplex
  # gateways must not borrow a process-global token from another profile.
  reapply_carried_patch \
    "gateway_late_token_reconnect.REPORTED-v2026.9.14.patch" \
    "gateway/run_adapters.py" \
    "allow_env_fallback" \
    "gateway-late-token-reconnect" \
    "fix(gateway): scope late-token reconnect fallback [carried]"

  # Discord slash-auth channel-key fix + ingress-drop logging.
  # _evaluate_slash_authorization tested membership against channel_keys (built
  # from interaction.channel, often None/id-less) while deriving channel_ids
  # from interaction.channel_id -- so every slash command in an allow-listed
  # channel was rejected. Breaks /reset and /new, which are the manual escape
  # hatch from a wedged session. Also logs every previously-silent ingress
  # rejection at INFO for allow-listed humans. No upstream issue as of
  # 2026-07-25 -- worth filing.
  # RETIRED 2026-08-14: functionality landed upstream (verified against
  # c896c09c42); the patch no longer applies and its marker is absent by
  # design, not by regression. Kept commented for audit history.
  # reapply_carried_patch \
  # "discord_ingress_visibility.patch" \
  # "plugins/platforms/discord/adapter.py" \
  # "_log_ingress_drop" \
  # "discord-ingress-visibility" \
  # "fix(discord): slash auth tested the wrong channel key set; log every ingress drop [carried]"

  # Gateway busy-path visibility. A message absorbed while a session is
  # considered busy produces no log line at all, so a leaked _running_agents
  # slot (upstream #28686 zombie-session state) silently swallows every
  # follow-up until the stale-agent sweep fires ~HERMES_AGENT_TIMEOUT later.
  reapply_carried_patch \
    "gateway_busy_path_logging.patch" \
    "gateway/run_busy.py" \
    "Busy-path absorbed inbound message" \
    "gateway-busy-path-logging" \
    "fix(gateway): log messages absorbed by the busy path [carried]"

  # Hindsight embedded-runtime env isolation.
  # hindsight_api/config.py runs load_dotenv(find_dotenv(usecwd=True),
  # override=True) at import time. A gateway's cwd is the hermes-agent
  # checkout, so find_dotenv walks up to the DEFAULT profile's ~/.hermes/.env
  # regardless of which profile is running, and override=True replaces ~25
  # already-loaded vars -- DISCORD_BOT_TOKEN, DISCORD_ALLOWED_CHANNELS,
  # DISCORD_FREE_RESPONSE_CHANNELS, MATRIX_ACCESS_TOKEN, ... Memory inits
  # lazily, so a non-default profile answers exactly one message and then goes
  # permanently silent. This is the root cause of the 2026-07-24/25 Oracle
  # outages. If Hindsight ever drops the module-level load_dotenv (or Hermes
  # upstream lands its own guard), delete this patch file and block.
  # RETIRED 2026-08-14: functionality landed upstream (verified against
  # c896c09c42); the patch no longer applies and its marker is absent by
  # design, not by regression. Kept commented for audit history.
  # reapply_carried_patch \
  # "hindsight_env_isolation.patch" \
  # "plugins/memory/hindsight/__init__.py" \
  # "def _import_guarded" \
  # "hindsight-env-isolation" \
  # "fix(memory): stop the Hindsight import from rewriting the process env [carried]"

  # Second half of the same PR (#71534): the installed-dependency probes in
  # hermes_cli import candidate modules for real and must restore os.environ.
  # Same patch file; distinct marker so partial application is visible.
  # RETIRED 2026-08-14: functionality landed upstream (verified against
  # c896c09c42); the patch no longer applies and its marker is absent by
  # design, not by regression. Kept commented for audit history.
  # reapply_carried_patch \
  # "hindsight_env_isolation.patch" \
  # "hermes_cli/web_server.py" \
  # "snapshot = dict(os.environ)" \
  # "hindsight-probe-env-guard" \
  # "fix(memory): dependency probes must not keep import-time env rewrites [carried]"

  # The memory dependency loop must restore its snapshot in finally, including
  # when a dependency mutates os.environ and raises a non-ImportError.
  # RETIRED 2026-08-14: functionality landed upstream (verified against
  # c896c09c42); the patch no longer applies and its marker is absent by
  # design, not by regression. Kept commented for audit history.
  # reapply_carried_patch \
  # "hindsight_env_isolation.patch" \
  # "tests/plugins/test_hindsight_env_isolation.py" \
  # "test_memory_setup_dependency_probe_restores_environment_when_import_raises" \
  # "hindsight-probe-exception-guard" \
  # "fix(memory): restore env when dependency probes fail [carried]"

  # Native mid-conversation system messages for Fable 5/Mythos 5/Opus 4.8
  # (upstream PR #68275, open/unmerged as of 2026-07-20). Once #68275
  # merges, delete this patch file and block — don't let it silently
  # duplicate/conflict with the real merge.
  # RETIRED 2026-08-14: functionality landed upstream (verified against
  # c896c09c42); the patch no longer applies and its marker is absent by
  # design, not by regression. Kept commented for audit history.
  # reapply_carried_patch \
  # "anthropic_mid_conversation_system.patch" \
  # "agent/prompt_builder.py" \
  # "_hermes_trusted_steer" \
  # "anthropic-mid-conversation-system" \
  # "Re-apply Anthropic mid-conversation system messages for Fable 5/Mythos 5/Opus 4.8 [carried]"

  # Desktop status-bar "Update now" button used to spawn raw
  # `python -m hermes_cli.main update` inside the live gateway process, with
  # no LaunchDaemon-aware pause. That restarts the gateway mid-request and
  # a connected Desktop client's un-backed-off WebSocket reconnect loop
  # hammers the restarting process -- confirmed root cause of the
  # 2026-07-31 credential-loss incident (restart storm drained file
  # descriptors into an EMFILE misread as ~/.hermes/auth.json corruption,
  # wiping live openai-codex OAuth credentials). This script already
  # pauses/restores the LaunchDaemons and self-detaches safely from inside
  # a live gateway; route the button through it instead of raw `hermes
  # update`. Only changes behavior on this ai-server install (guarded by
  # this script's own on-disk existence) -- not upstreamable, nobody else
  # runs this wrapper.
  reapply_carried_patch \
    "desktop_update_use_safe_wrapper.REPORTED-v2026.9.24.patch" \
    "hermes_cli/web_server_gateway.py" \
    "cmd_override" \
    "desktop-update-safe-wrapper" \
    "fix(desktop): route \"Update now\" through the sanctioned safe-update wrapper [carried]"

  # Matrix live-adapter cross-loop reentry guard. cron's standalone-delivery
  # fallback (cron/scheduler.py) runs _send_to_platform inside a fresh
  # asyncio.run() loop when the primary live-adapter attempt fails/times out.
  # _send_matrix_via_adapter reused the gateway's live adapter unconditionally,
  # but that adapter's aiohttp session and wait_for timeout context are bound
  # to the gateway's own loop -- awaiting them from the standalone loop raises
  # "Timeout context manager should be used inside a task" and the delivery is
  # logged as a hard error (observed in production 2026-08-02, alerts-infra
  # room, two cron jobs after a transient DNS failure). Only take the
  # live-adapter fast path when actually executing on runner._gateway_loop.
  # This commit existed only as a local git commit with NO patch file until
  # 2026-08-03 -- it would have been wiped by the next reset to origin/main.
  # MARKER FIXED 2026-09-06: previous marker "_gateway_loop" also matches an upstream line
# (tools/send_message_tool.py:996), so every update saw "present" and skipped the
# apply -- the patch was never in the tree. Marker is now a string only the patch adds.
reapply_carried_patch \
    "matrix_crossloop_adapter_reuse.patch" \
    "tools/send_message_senders.py" \
    "cross-loop timeout-context error" \
    "matrix-crossloop-adapter-reuse" \
    "fix(matrix): guard live-adapter reuse against cross-loop reentry from cron fallback [carried]"

  # Desktop gateway soft-switch left the socket dead. A soft switch suppresses
  # reconnect scheduling for its whole duration (the onState guard skips while
  # switching) and HermesGateway de-dupes identical states -- so a socket left
  # closed/errored by a FAILED re-dial emits no further transition and sits
  # dead until a manual reload. An assistant turn completing server-side while
  # that socket is dead is exactly the missing-message defect: the reply is
  # persisted but never painted. Re-arm reconnect after the switching flag
  # clears. Like matrix_crossloop_adapter_reuse above, this existed ONLY as a
  # local git commit (4a8f974044, 2026-08-21) with NO patch file until
  # 2026-08-26 -- the next reset to origin/main would have silently wiped it.
  # Found by the readiness verifier's registered-coverage check.
  reapply_carried_patch \
    "desktop_gateway_boot_rearm.patch" \
    "apps/desktop/src/app/gateway/hooks/use-gateway-boot.ts" \
    "Re-arm here," \
    "desktop-gateway-boot-rearm" \
    "fix(desktop): re-arm gateway reconnect after a failed soft switch [carried]"

  # Gateway lifecycle guard false-positives. The referenced-script walk yields
  # any '/'-containing token out of a scanned file, so (a) a bare directory
  # path literal inside a .py source (e.g. onepassword_sdk.py's ~/.hermes/bin
  # constant) was opened, found non-regular, and failed closed -- hard-blocking
  # an ordinary credential read; and (b) a NUL-carrying path token made os.open
  # raise ValueError (not OSError), which escaped the handler and surfaced as
  # "Failed to execute command: embedded null byte" -- the guard crashing the
  # terminal tool it protects. Both observed 2026-08-06 while storing Matrix
  # bot credentials. Upstream NousResearch/hermes-agent#77173 and #79704 are
  # both still OPEN; drop this patch once they land.
  # RETIRED 2026-08-14: functionality landed upstream (verified against
  # c896c09c42); the patch no longer applies and its marker is absent by
  # design, not by regression. Kept commented for audit history.
  # reapply_carried_patch \
  # "lifecycle_guard_dir_and_nul.patch" \
  # "cron/lifecycle_guard.py" \
  # "S_ISDIR" \
  # "lifecycle-guard-dir-and-nul" \
  # "fix(cron): stop lifecycle guard blocking on directories and NUL-byte paths [carried]"

  # Gateway false "not running" alerts (root-caused 2026-08-14). Two defects
  # made a healthy gateway report as down, and the alert delivery itself kept
  # the loop alive across restarts:
  #   1. write_runtime_status() let ANY process claim the state record's
  #      identity fields. Alerts are delivered by `hermes send`, which builds a
  #      platform adapter -> _mark_connected() -> write_runtime_status(); that
  #      short-lived process stamped its own PID into gateway_state.json and
  #      exited, so the liveness fallback saw a dead PID and alerted again.
  #   2. _cleanup_invalid_pid_path() unlinked gateway.pid/gateway.lock from a
  #      liveness decision taken earlier by its caller, deleting a LIVE
  #      gateway's on-disk record during the restart window.
  #
  # RE-PORTED 2026-08-17 (upstream now @ 4323c67dcc, 1111 commits ahead):
  # upstream commits d66341ab28 / f755ed5e90 / c127d0c3b1 ("OOF-3 strict
  # writer-identity ownership for aggregated platform entries") rewrote
  # gateway/status.py and ABSORBED defect #1 — per-entry writer_pid /
  # writer_start_time provenance is upstream now. Only defect #2 (the TOCTOU
  # liveness guard) is still missing upstream, so the carried patch shrank to
  # that 9-line guard: gateway_false_down_alerts.REPORTED.patch.
  #
  # The old marker "owns_runtime" no longer exists ANYWHERE upstream (upstream
  # renamed the concept), so it would report a permanent false MISSING in the
  # verification pass below. Marker is now the guard's own call line, which is
  # unique to us (0 hits in pristine upstream, 1 in the patched tree). Both
  # grep sites use -Fq, so the parentheses are literal, not regex.
  # Drop this patch entirely if the liveness guard lands upstream.
  reapply_carried_patch \
    "gateway_false_down_alerts.REPORTED-v2026.9.14.patch" \
    "gateway/status.py" \
    "is_gateway_runtime_lock_active(_get_gateway_lock_path(pid_path))" \
    "gateway-false-down-alerts" \
    "fix(gateway): stop false 'not running' alerts from poisoned runtime state [carried]"

  # Auth store must never be flushed to disk after a failed load: an
  # unreadable/transient-IO store would otherwise be overwritten with empty
  # state, destroying live credentials. Also content-addresses the corrupt
  # backup and caps retention. Verified still absent from origin/main
  # 2026-08-14 (bug reproduced on main: 7 failing tests).
  # RETIRED 2026-09-08 (release-package worker): the registered marker
  # _LOAD_FAILURE_MARKER / AuthStoreWriteGuardError mechanism is not present
  # in the candidate tree, but the same incident class is closed one layer
  # earlier and more strongly: hermes_cli/auth.py:_load_auth_store() now
  # `raise`s OSError on a transient read failure (EMFILE/EACCES/EIO) instead
  # of falling through to an empty store (verified live at
  # hermes_cli/auth.py:623-631, `raise` on the except OSError branch, no
  # empty-store fallback reachable from that path). Fail-loud beats a
  # write-guard-after-the-fact for the same defect. See
  # release-package/reports/report.json registration #12. Patch file remains
  # at patches/auth_loader_transient_io_safety.patch for reference/rollback;
  # not moved to patches/retired/ by this worker (out of this task's
  # file-move scope) -- flagged for the next wrapper maintenance pass.
  # reapply_carried_patch \
  #   "auth_loader_transient_io_safety.patch" \
  #   "hermes_cli/auth.py" \
  #   "_LOAD_FAILURE_MARKER" \
  #   "auth-loader-transient-io-safety" \
  #   "fix(auth): guard against flushing a load-failure store to disk [carried]"

  # A config change must not bounce a *healthy* shared embedded Hindsight
  # daemon; other profiles share it. Verified still absent from origin/main
  # 2026-08-14.
  # [registry-closure 2026-09-08] REGENERATED as ONE logical patch
  # (hindsight_defer_restart_preserve_key_pin.patch = accepted candidate commit
  # 0fee358bb5) that carries this fix, hindsight-preserve-stored-api-key and
  # hindsight-client-pin-alignment together; the three per-feature files were
  # stale against v2026.9.7 (all rejected) and are kept unregistered under
  # patches/superseded/. This block applies the group; the later blocks that
  # name the same file are presence checks for their own markers (same pattern
  # as dashboard_ws_handshake_offload.patch below).
  # [RETIRED for v2026.9.24+: bundled hindsight removed] reapply_carried_patch \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight_defer_restart_preserve_key_pin.REPORTED-v2026.9.14.patch" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "plugins/memory/hindsight/__init__.py" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "Config changed; healthy daemon left running" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight-defer-live-daemon-restart" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "fix(hindsight): defer restart of a healthy shared embedded daemon [carried]"

  # membership=invite fan-out for OTHER users in a shared room must not be
  # treated as an invite of ourselves; gate on state_key, not just sender.
  # Verified still absent from origin/main 2026-08-14.
  reapply_carried_patch \
    "matrix_invite_target_guard.patch" \
    "plugins/platforms/matrix/adapter.py" \
    "state_key = str(getattr(event" \
    "matrix-invite-target-guard" \
    "fix(matrix): normalize MXIDs in the invite target guard [carried]"

  # The target guard above is necessary but not sufficient: mautrix's
  # MembershipEventDispatcher fans out EVERY membership=invite event as
  # InternalEventType.INVITE regardless of which sync section carried it, and
  # connect() uses MemorySyncStore, so every reconnect is a full initial sync
  # that re-dispatches each joined room's recent timeline. Invites addressed to
  # US therefore re-fire _on_invite on every boot. Ignore anything whose
  # SyncStream stamp isn't INVITED_ROOM, or whose room we already joined.
  # MUST apply AFTER matrix_invite_target_guard.patch — same hunk in _on_invite.
  # Upstream: NousResearch/hermes-agent#111170 (still OPEN, rechecked 2026-09-20;
  # head branch kvnloo:fix/matrix-invite-state-key-76292). That PR currently
  # carries ONLY the state_key guard, which does NOT cover the bot's own
  # replayed historic invite -- @bit-incarnas posted the two extra guards this
  # patch implements (_is_invited_room_source + _joined_rooms) as a
  # cherry-pickable commit decb6b888 on 2026-09-19, not yet picked up by kvnloo.
  # So: even if #111170 merges as-is, this block is NOT retirable -- verify the
  # merged adapter actually contains _is_invited_room_source before retiring,
  # and per the sibling block below, verify against the resolved stable TAG
  # rather than origin/main.
  reapply_carried_patch \
    "matrix_invite_replay_guard.patch" \
    "plugins/platforms/matrix/adapter.py" \
    "_is_invited_room_source" \
    "matrix-invite-replay-guard" \
    "fix(matrix): ignore membership invites replayed from joined rooms [carried]"

  # _on_invite rejects an unauthorized inviter, then _schedule_pending_invite_joins
  # joins the same room out of rooms.invite with no gate at all. _absorb_sync calls
  # it unconditionally, so it fires on live incremental syncs, not just restarts.
  # Observed here: 13 rooms rejected, reconciled, and joined anyway against a
  # one-entry allowlist. NOTE this carries the FIX but not upstream's helper --
  # we gate via our existing scope-aware _is_authorized_user, because
  # gateway/AGENTS.md forbids a bare os.getenv read of GATEWAY_ALLOW_ALL_USERS.
  #
  # RETIREMENT CONDITION (checked 2026-09-20): upstream MERGED this as #117189
  # (salvage of #87258, which is now closed) -> origin/main f458cba714, with the
  # fix itself in 439f2b1cf6 by @iainlane. kshitijk4poor's cleanup DROPPED the
  # standalone _is_authorized_inviter helper and the reconcile gate now calls
  # _is_authorized_user, i.e. upstream no longer does the bare os.getenv read
  # that was our reason to carry a divergent version. The AGENTS.md objection
  # is therefore resolved and this block is retirable on CONTENT grounds.
  #
  # DO NOT RETIRE YET. This updater pins a canonical stable RELEASE TAG, not
  # origin/main. As of 2026-09-20 the deployed base is v2026.9.14 and NONE of
  # v2026.9.14 / v2026.9.11 / v2026.9.7 contain f458cba714. Dropping this block
  # now would reset the tree to a tag without the gate and silently restore the
  # auto-join bypass (13 rooms joined from an unallowlisted inviter here).
  # Retire only when this passes:
  #   git -C "$REPO_DIR" merge-base --is-ancestor f458cba714 <resolved-stable-tag>
  # At that point delete this block AND scripts/patches/matrix_pending_invite_auth.patch.
  reapply_carried_patch \
    "matrix_pending_invite_auth.REPORTED-v2026.9.24.patch" \
    "plugins/platforms/matrix/adapter.py" \
    "_extract_invite_dm_signal" \
    "matrix-pending-invite-auth" \
    "fix(matrix): gate reconciled pending invites on the inviter allowlist [carried]"

  # A transient empty secret lookup must not be written over the stored
  # Hindsight API key: the profile env is rebuilt with O_TRUNC on every
  # session start, so an empty resolution blanked the key permanently.
  # Verified still absent from origin/main 2026-08-14.
  # [RETIRED for v2026.9.24+: bundled hindsight removed] reapply_carried_patch \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight_defer_restart_preserve_key_pin.REPORTED-v2026.9.14.patch" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "plugins/memory/hindsight/embedded.py" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "A transient miss" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight-preserve-stored-api-key" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "fix(hindsight): never persist an empty API key over a stored one [carried]"

  # Root cause of the above: bare threads don't inherit ContextVars, so the
  # profile secret scope vanished on the daemon-start/writer/prefetch threads
  # and get_secret resolved "" -- which then got persisted over the good key.
  # Must apply AFTER hindsight_preserve_stored_api_key (same files).
  # Verified still absent from origin/main 2026-08-14.
  # [registry-closure 2026-09-08] UPSTREAM-EQUIVALENT AT BASE: _context_thread
  # is defined in pristine v2026.9.7 (4 hits at 2237be3559), so no hunk is
  # required and this block skips by design. Points at the hindsight group
  # patch so no stale patch file remains registered.
  # [RETIRED for v2026.9.24+: bundled hindsight removed] reapply_carried_patch \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight_defer_restart_preserve_key_pin.REPORTED-v2026.9.14.patch" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "plugins/memory/hindsight/__init__.py" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "spawn_context_thread(self._writer_loop, name=\"hindsight-writer\")" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight-scoped-worker-threads" \
    # [RETIRED for v2026.9.24+: bundled hindsight removed] "fix(hindsight): propagate the profile secret scope into worker threads [carried]"
fi

# Two upstream timing tests measure wall clock, which on this machine measures
# the scheduler: a nominal time.sleep(0.03) really takes ~0.18s here, so the
# budgets are unsatisfiable regardless of product correctness. Rewritten to
# assert enforcement (events consumed / idle ratio) instead. Product code
# untouched; both remain load-bearing (verified by mutation). Drop this patch
# if upstream ever de-flakes them itself.
reapply_carried_patch \
  "agent_timing_test_deflake.REPORTED-v2026.9.24.patch" \
  "tests/agent/test_auxiliary_client.py" \
  "events_yielded < 5" \
  "agent-timing-test-deflake" \
  "test(agent): measure timeout enforcement, not scheduler latency"

# Hermes lazily reinstalls hindsight-client on cold start from the pin in
# tools/lazy_deps.py. If that pin drifts below the running Hindsight server
# (0.9.2), a gateway restart SILENTLY DOWNGRADES the client — recall/retain
# keep "working" against the newer API, so nothing alerts. This was lost in
# the v0.20.5 update because it was a plain local commit with no registered
# patch file, and the stash/replay cycle dropped it.
# NOTE: uv.lock is the THIRD pin and is NOT in this patch (it is regenerated,
# not text-patched). After this replays, run:
#   uv lock --upgrade-package hindsight-client
# and confirm venv/bin/pip show hindsight-client == the server version.
# [RETIRED for v2026.9.24+: bundled hindsight removed] reapply_carried_patch \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight_defer_restart_preserve_key_pin.REPORTED-v2026.9.14.patch" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "tools/lazy_deps.py" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight-client==0.9.2" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight-client-pin-alignment" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "chore(hindsight): align client dependency at 0.9.2 [carried]"

# The desktop mints a NEW session id on every WS reconnect, orphaning the old
# sid; _schedule_ws_orphan_reap then interrupts that session's turn at
# ws_orphan_reap_grace_s even while it is healthy and still producing output.
# Killed ordinary long tool calls here and surfaced as "Operation interrupted."
# plus a spurious orphan-recovery banner (client_gone interrupts logged
# 2026-09-01 13:50:27 and 13:52:05, each exactly one grace after detach).
# Upstream 67de93862c gates the interrupt on activity staleness, but landed
# after v2026.8.31 was cut, so carry it until a stable release ships it.
# ABSORBED ON origin/main (git cherry match, 2026-09-06) but NOT in v2026.8.31.
# Retire at the first stable release that contains it; marker will show present-by-upstream.
  # RETIRED for v2026.9.24: upstream 67de93862c is an ancestor of v2026.9.24 (code + moved tests present; 16 ws_orphan tests pass). See reports924/REPORT.md.
  # reapply_carried_patch \
  # "ws_orphan_activity_stale_gate.patch" \
  # "tui_gateway/server.py" \
  # "_WS_ORPHAN_ACTIVITY_STALE_S" \
  # "ws-orphan-activity-stale-gate" \
  # "fix(tui-gateway): gate ws-orphan interrupt on activity staleness [carried]"
# gpt-6-astra on openai-codex accepts low..max (rejects none) but the Codex
# effort vocabulary only recognised gpt-5.6 for ``max``, so Astra's max was
# silently clamped to xhigh. Port of upstream PR #103570 (issue #103556),
# 2026-09-05. Drop once upstream merges it.
  # RETIRED for v2026.9.24: upstream v2026.9.24 has CODEX_ASTRA_EFFORTS/is_astra_model with max; carried vocab+transport assertions pass on pristine. See reports924/REPORT.md.
  # reapply_carried_patch \
  # "codex_astra_max_effort.patch" \
  # "agent/reasoning_effort.py" \
  # "CODEX_ASTRA_EFFORTS" \
  # "codex-astra-max-effort" \
  # "fix(codex): preserve max reasoning effort for gpt-6-astra on openai-codex [carried]"
# The auxiliary adapters rebuilt usage as bare prompt/completion/total, which
# discarded the cache breakdown before normalize_usage() ever saw it -- so every
# adapter-path aux call (compression, vision, title_generation, ...) was
# recorded and priced as fully uncached, while the main-loop client path
# recorded cache correctly. Carry the cache fields through in both adapters.
# NOTE: passing api_mode from aux_accounting is NOT the fix -- on an
# adapter-shaped usage object it zeroes input_tokens. Reported upstream
# 2026-09-06.
# [final-integration 2026-09-08] CLOSED: this registration's patch_file was
# updated (not just its marker) to the full closure -- the original
# aux_adapter_cache_usage.patch only covered the base cache-passthrough fix
# and never actually applied to this pinned tree (it predates the upstream
# auxiliary_client.py refactor this candidate is built on); wip-review and
# combined-replay's three follow-on commits closed the gap on top of the
# refactored file (7228e8052f, 0cf28a0967, 1104de74ff -- the wip chain
# accepted onto this candidate). The registration's patch_file now contains
# 105f2899->1104de74ff diff for agent/auxiliary_client.py +
# tests/agent/test_aux_adapter_cache_usage.py (regenerated 2026-09-08 from the
# accepted chain as git diff 105f2899 1104de74ff -- <those two files>; exact
# because no other base..HEAD commit touches them) and reproduces the exact
# final-integration tree state for these two files.
# The original file is preserved as
# aux_adapter_cache_usage.ORIGINAL_OPEN_GAP.patch.bak for audit; not deleted.
reapply_carried_patch \
  "aux_adapter_cache_usage.patch" \
  "agent/auxiliary_client.py" \
  "_extract_cached_token_details" \
  "aux-adapter-cache-usage" \
  "fix(aux): preserve cache token details through auxiliary adapters, full guard + safe diagnostic [carried]"
# dashboard_ws_handshake_offload.patch's own registrations (#23/#24) were
# closed on marker presence, but no test in the tree ever exercised
# _READY_EXECUTOR or _coalesced_to_thread until this package. New, separate
# registration for the regression coverage itself (not a marker-string check
# -- this one's presence check is the test files' own existence + a green
# run, since "coverage exists" isn't a source-code substring).
reapply_carried_patch \
  "dashboard_ws_regression_coverage.patch" \
  "tests/tui_gateway/test_dashboard_ws_ready_executor_offload.py" \
  "_READY_EXECUTOR" \
  "dashboard-ws-regression-coverage" \
  "test(dashboard): real regression coverage for WS handshake offload"
# Model picker listed disabled MoA presets (the undeletable built-in "default",
# disabled via enabled:false, still showed next to council). _moa_provider_row now
# honors enabled:, matching exact_moa_preset_name (#55187). Tests in test_inventory.py.
reapply_carried_patch \
  "moa_picker_hide_disabled.REPORTED-v2026.9.24.patch" \
  "hermes_cli/inventory.py" \
  "if p.get(\"enabled\", True)]" \
  "moa-picker-hide-disabled" \
  "fix(moa): hide disabled presets from the model picker [carried]"
# Incident 2026-09-05 18:40 CDT: desktop WS reconnect storm. 14 identical 44k-row
# transcript reads saturated asyncio's default executor and the gateway.ready
# frame (resolve_skin via to_thread) queued behind them past the client deadline.
# ws.py: resolve_skin on a dedicated executor; sessions.py: identical in-flight
# /messages reads share one shielded DB read; status.py: psutil before forking ps.
# Was a bare local commit (f6e78011d5) with no patch file until the 2026-09-06
# patch audit -- the next reset+replay would have dropped it.
reapply_carried_patch \
  "dashboard_ws_handshake_offload.REPORTED-v2026.9.24.patch" \
  "tui_gateway/ws.py" \
  "_READY_EXECUTOR = concurrent.futures.ThreadPoolExecutor" \
  "dashboard-ws-handshake-offload" \
  "fix(dashboard): keep the WS handshake off the shared executor; coalesce transcript reads; no fork for cmdline [carried]"
reapply_carried_patch \
  "dashboard_ws_handshake_offload.REPORTED-v2026.9.24.patch" \
  "hermes_cli/web_routers/sessions.py" \
  "def _coalesced_to_thread" \
  "dashboard-ws-handshake-offload/sessions" \
  "fix(dashboard): keep the WS handshake off the shared executor; coalesce transcript reads; no fork for cmdline [carried]"
# The gateway/status.py half of this patch (psutil before the forking `ps` in
# _read_process_cmdline) landed upstream in v2026.9.21 with equivalent behavior and a better
# docstring, so the regenerated patch no longer carries that file. Retire it marker-first:
# the marker below is UPSTREAM text, so reapply_carried_patch verifies it is present and
# skips instead of trying to commit an empty diff.
reapply_carried_patch \
  "dashboard_ws_handshake_offload.REPORTED-v2026.9.24.patch" \
  "gateway/status.py" \
  "the process table in-process" \
  "dashboard-ws-handshake-offload/status" \
  "fix(dashboard): keep the WS handshake off the shared executor; coalesce transcript reads; no fork for cmdline [carried]"
# Ported from merittas 2026-09-06 (patch audit: Neo/Forge parity). Narrows the
# broad `curl ... $TOKEN` exfil heuristic: a secret in the URL path or POST body is
# exfiltration; a secret in an Authorization header to an API host is correct usage
# (the broad rule flagged every infra doc's curl example and trained readers to
# ignore the warning). Tests cover both directions incl. header-as-evasion.
reapply_carried_patch \
  "exfil_precision.patch" \
  "tools/threat_patterns.py" \
  "exfil_url_secret" \
  "exfil-precision" \
  "fix(threat_patterns): distinguish secret-in-URL/body exfil from auth-header usage [carried]"
# _SECRET_VAR's optional-close tail (\}?\b) left a stray closing brace after a
# braced ${TOKEN} reference, defeating the (?!["']) auth-header exemption for
# every braced secret. Gives the helper two disjoint branches (explicitly-
# closed braced form vs. bare form) instead of one optional-close tail.
# [registry-closure 2026-09-08] MOVED after exfil-precision: candidate commit
# 4171cc0d83 edits the same two files on top of 48b1994a12, so it must apply
# second. Patch content regenerated from 4171cc0d83.
# New patch closing a gap the exfil_precision.patch marker alone did not
# cover (marker `exfil_url_secret` matches on the unrelated base fix, not
# this braced-var hardening). See report.json registration note.
reapply_carried_patch \
  "threat_patterns_braced_var_close.patch" \
  "tests/tools/test_threat_patterns.py" \
  "test_auth_header_with_braced_var_is_not_exfiltration" \
  "threat-patterns-braced-var-close" \
  "fix(threat_patterns): correctly close braced \${VAR} secret references [carried, hardened]"
# Static fast-mode override (speed/service_tier) re-derived for the destination
# route on provider fallback and /model switch; explicit non-marker values are
# preserved. Accepted candidate e1d5506226 (three commits consolidated; the
# final commit alone is not sufficient). First deployed via --patch-only on
# 2026-09-08; registered here so the next stable replay keeps it.
reapply_carried_patch \
  "fast_fallback_final.REPORTED-v2026.9.24.patch" \
  "agent/fast_mode.py" \
  "def rescope_route_fast_override(" \
  "fast-fallback-rescope" \
  "fix(fast-mode): rescope static fast override on fallback/switch [carried]"
# Manual cron runs never consume a future scheduled occurrence (registered
# 2026-09-08, fix set cron-manual-fix-20260908). Accepted source commit
# 9a9c8e30219e8e4d2a9b8701a5c1b43ba6d1dc60: claim_job_for_fire() gains an
# explicit manual=False kwarg, tools/cronjob_tools.py::_claim_for_manual_run
# claims with manual=True, and advance_next_runs() skips a job with a live
# pending manual fire (manual_run_at == next_run_at). Ships the new regression
# file tests/cron/test_manual_occurrence_claim.py plus two updated tool tests.
# First deployed via --patch-only; registered here so the next stable replay
# keeps it. Marker is the DEFINITION (new signature line in cron/jobs.py).
reapply_carried_patch \
  "cron_manual_occurrence_claim.REPORTED-v2026.9.14.patch" \
  "cron/jobs.py" \
  "if job.get(\"manual_run_at\") and job.get(\"manual_run_at\") == job.get(\"next_run_at\"):" \
  "cron-manual-occurrence-claim" \
  "fix(cron): manual runs never consume a future scheduled occurrence [carried]"
# LCM live-compression-config guard (registered 2026-09-08). Parent-reviewed
# candidate f8219634a3 at lcm-config-fix/candidate: _apply_live_compression_config()
# unconditionally poked ContextCompressor-private attrs/methods
# (_coerce_threshold_tokens_cap, tail_mode, proactive_prune_*, model_thresholds,
# ...) on whatever is bound to agent.context_compressor. When context.engine is
# an external plugin (hermes-lcm's LCMEngine implements only the public
# ContextEngine ABC), every live `hermes config set compression.*` against a
# running Desktop/TUI session raised AttributeError, silently swallowed by the
# caller's except Exception -- 3 occurrences logged post-2026-09-08-update with
# no config actually taking effect. Per the host contract already documented in
# agent/agent_init.py::_build_context_engine (and fix 60391d0eef1 for the
# analogous autoraise-notice boundary), guard the whole block with the same
# isinstance(cc, ContextCompressor) pattern already used at the two other call
# sites that touch this private state (tools/delegate_tool.py, agent/
# conversation_compression.py). 13-line guard + 178-line regression test file;
# 25 tests passed (test_compression_config_live_external_engine.py [4 new],
# test_compression_config_hot_reload.py [18, unaffected/native path proof],
# test_delegate_child_compression_cap.py [3, sibling guarded caller unaffected])
# via a dry-run apply + narrow pytest against a disposable worktree pinned at
# pristine v2026.9.7 -- confirmed byte-for-byte non-stale before registration.
# MUST apply before hindsight-client-generated-lock below: that registration's
# post-replay uv.lock amendment (`git commit --amend`) requires the exact
# current tip subject to be the Hindsight pin commit, so this block cannot be
# the final carried-patch registration.
reapply_carried_patch \
  "lcm_live_compression_guard.patch" \
  "tui_gateway/session_compression.py" \
  "isinstance(cc, ContextCompressor)" \
  "lcm-live-compression-guard" \
  "fix(tui-gateway): skip ContextCompressor-private tuning for external context engines [carried]"
# Preserve the reviewed generated lock as a replayable artifact. The former
# post-replay regeneration could not amend the Hindsight commit after later
# registered patches had committed, and therefore always failed this release.
# [RETIRED for v2026.9.24+: bundled hindsight removed] reapply_carried_patch \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight_client_generated_lock.patch" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "uv.lock" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight_client-0.9.2-py3-none-any.whl" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "hindsight-client-generated-lock" \
  # [RETIRED for v2026.9.24+: bundled hindsight removed] "chore(hindsight): align client dependency at 0.9.2 [carried]"
# CUA-driver 0.24 schema compatibility. The dedicated --cua-compat-only route
# applies this exact patch at the pinned incident base; this ordinary registration
# keeps it through the next approved stable replay. The marker is in the added
# strict-schema regression file, so code without its required tests cannot pass.
reapply_carried_patch \
  "cua_snapshot_compat_20260909.REPORTED-v2026.9.24.patch" \
  "tests/tools/test_computer_use_snapshot_id.py" \
  "test_action_attaches_snapshot_id_when_no_per_element_token_but_schema_accepts_it" \
  "cua-snapshot-compat" \
  "fix(computer_use): preserve CUA 0.24 schema compatibility [carried]"
# Neo context-warning and Matrix reply target fixes. The dedicated --patch-only
# route applies the exact paired source fixes; this ordinary registration preserves the
# core half through a later approved stable replay. The source patch includes regression tests.
# The real ordinary path never resets or applies to the LCM checkout: it only snapshots
# and mutates PLUGIN_DIR inside PATCH_ONLY=1, so the paired LCM commit is preserved.
reapply_carried_patch \
  "neo_context_warning_matrix_replies.REPORTED-v2026.9.24.patch" \
  "agent/turn_preflight.py" \
  "def _request_pressure_compaction_eligible(" \
  "neo-context-warning-matrix-replies" \
  "fix(context): Neo warnings and Matrix reply targeting [carried]"
# Approved Retry/compression recovery core half. The dedicated
# --retry-recovery-only path applies this together with its LCM peer at exact
# incident bases; this ordinary registration preserves the core source plus
# regression tests through a later separately approved stable replay. Ordinary
# updates do not mutate the LCM checkout, so the paired plugin's own receipt and
# exact-source transaction remain authoritative for that repository.
reapply_carried_patch \
  "retry_recovery_core_20260915.REPORTED-v2026.9.24.patch" \
  "tests/agent/test_external_engine_no_progress.py" \
  "test_host_rejects_external_rewrite_without_material_token_reduction" \
  "retry-recovery-core-20260915" \
  "fix(compression): recover external engine and Desktop retry [carried]"

# Matrix false-fallback fixes (2026-09-17). Two independent bugs made a healthy
# Anthropic route look dead and then STAY dead for the life of a Matrix room:
#
# 1. anthropic 0.87.0 leaks a bare jiter ValueError out of its non-beta
#    streaming tool-input accumulator (upstream anthropic-sdk-python#1265).
#    Hermes already retries such stream-parse errors on the SAME provider, but
#    the detector matched two hardcoded wordings and missed the live one
#    ("expected `:` at line 1 column 1419"), so a parse bug was reported as
#    "provider unavailable" and cascaded to openai-codex. Detection is now
#    structural (any trailing "at line N column M"), still gated to
#    anthropic_messages + ValueError.
# 2. The gateway persisted the ACTIVE route after each turn. During a fallback
#    that route is the fallback, so it was written into durable gateway_runtime;
#    the next turn resumed on it, snapshotted it as _primary_runtime, and
#    "restored" the fallback onto itself forever. Matrix rooms live for days, so
#    one transient blip pinned a room to OpenAI (observed pinned since 2026-08-04).
#
# Markers are the comment/function this change introduces (0 hits at base).
reapply_carried_patch \
  "matrix_stream_parse_no_false_fallback.patch" \
  "agent/api_error_summary.py" \
  "def is_provider_stream_parse_message(" \
  "matrix-stream-parse-no-false-fallback" \
  "fix(providers): treat streamed tool-JSON parse errors as retryable [carried]"

reapply_carried_patch \
  "matrix_fallback_route_not_persisted.patch" \
  "gateway/run_turn.py" \
  "Keep the session's routable identity on the PRIMARY" \
  "matrix-fallback-route-not-persisted" \
  "fix(gateway): never persist a fallback route as the session route [carried]"
# GPT Image 2.5 (Flare/Sunburst, Sep 8 2026) through Codex OAuth. Upstream added 2.5 to the
# API-key `openai` plugin and the FAL catalog but not to `openai-codex`, which is the provider
# both this box and merittas actually run (image_gen.provider: openai-codex). The Codex backend
# accepts the 2.5 ids in the hosted image_generation tool -- verified live, both variants, every
# quality tier -- so this is a pure client-side gap. Marker is the shared 2.5 catalog constant.
reapply_carried_patch \
  "codex_image_gen_25.REPORTED-v2026.9.24.patch" \
  "plugins/image_gen/_common.py" \
  "GPT_IMAGE_25_TIERS" \
  "codex-image-gen-25" \
  "feat(image_gen): GPT Image 2.5 Flare/Sunburst through Codex OAuth [carried]"
# On Python 3.11+ concurrent.futures.TimeoutError IS builtin TimeoutError, so a poll loop around
# future.result(timeout=...) cannot tell "wait slice expired" from "worker raised TimeoutError".
# auxiliary_client raises bare TimeoutError on a stalled summary stream, so the host re-waited on an
# already-settled future and spun at ~2k iterations/sec -- one session burned 535s and wrote 90k
# duplicate "still streaming" log lines before failing with context_compression_timeout (2026-09-20).
# Guard is `if future.done()`. Upstream main carries the same defect. Marker is the guard's comment.
  # RETIRED for v2026.9.24: upstream v2026.9.24 carries the future.done() guards (#117261/#63892); carried tests pass on pristine and fail with guards disabled. See reports924/REPORT.md.
  # reapply_carried_patch \
  # "future_timeout_worker_death.patch" \
  # "tests/agent/test_future_timeout_worker_death.py" \
  # "test_compression_wait_returns_promptly_when_worker_dies" \
  # "future-timeout-worker-death" \
  # "fix(compression): don't mistake a worker's TimeoutError for a poll timeout [carried]"

reapply_carried_patch \
  "resume_route_model_coherence.patch" \
  "tui_gateway/server.py" \
  "ignoring stale route" \
  "resume-route-model-coherence" \
  "fix(resume): don't fuse a session's model with another model's route [carried]"
# Desktop/TUI client disconnect past ws_orphan_reap_grace_s reaped the session and agent.close()
# killed every background process it owned (detached workers died silently, notify swallowed).
# Port of open upstream PR NousResearch/hermes-agent#120648 + real-chain reap regression test.
# Upstream: NousResearch/hermes-agent#133088 (supersedes #120648).
# Drop once #133088 (or equivalent) is in the stable release.
reapply_carried_patch \
  "recoverable_close_keeps_background_processes.patch" \
  "agent/client_lifecycle.py" \
  "Recoverable close (agent_close) resumes this session on the same task_id" \
  "recoverable-close-keeps-bg-processes" \
  "fix(lifecycle): keep background processes alive on recoverable session close [carried]"
# --- end carried patches ---

# --- hindsight CATALOG-PLUGIN carry (v2026.9.24+) ---------------------------
# Upstream moved the Hindsight provider out of core into the catalog plugin
# vectorize-io/hindsight. `hermes update` (inner step above) already installed it into every
# profile home that names it. Our two carried fixes (defer healthy-daemon restart; keep the stored
# API key) now live as plugin-tree patches applied by hindsight-ensure-plugin-carry.sh. This MUST
# run before services are restored so the gateways import the carried code. Idempotent; exit 0
# already carried, 10 changed, 1 attention (tree untouched), 2 never (only with --check).
HS_PLUGIN_ENSURE="$AISERVER_HOME/.hermes/scripts/hindsight-ensure-plugin-carry.sh"
if [ ! -x "$HS_PLUGIN_ENSURE" ]; then
  log "ERROR: $HS_PLUGIN_ENSURE missing; Hindsight plugin carries NOT applied"
  CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
else
  # `hermes update` (migrate_all_homes) normally installed the plugin already. If that step was
  # skipped or failed (offline, old in-memory updater code), install it here, per profile home,
  # BEFORE the carry check; never reinstall over an existing tree (would drop the carry).
  for _hs_prof in default quant; do
    if [ "$_hs_prof" = "default" ]; then _hs_home="$AISERVER_HOME/.hermes"; else _hs_home="$AISERVER_HOME/.hermes/profiles/$_hs_prof"; fi
    [ -d "$_hs_home" ] || continue
    if [ ! -d "$_hs_home/plugins/hindsight" ]; then
      log "  hindsight plugin missing for profile $_hs_prof; installing from the catalog..."
      if ! hermes_runtime_run "$REPO_DIR" cli --profile "$_hs_prof" plugins install hindsight --enable </dev/null 2>&1 | while IFS= read -r l; do log "    $l"; done; then
        log "ERROR: PM plugin admission failed for $_hs_prof"
        CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
      fi
    fi
  done
  log "Ensuring hindsight catalog-plugin carries (default + profiles)..."
  if [ "$(id -u)" -eq 0 ]; then
    HS_PC_OUT=$(sudo -u aiserver -H env HERMES_ROOT="$AISERVER_HOME/.hermes" "$HS_PLUGIN_ENSURE" 2>&1); HS_PC_RC=$?
  else
    HS_PC_OUT=$(HERMES_ROOT="$AISERVER_HOME/.hermes" "$HS_PLUGIN_ENSURE" 2>&1); HS_PC_RC=$?
  fi
  printf '%s\n' "$HS_PC_OUT" | while IFS= read -r l; do log "  $l"; done
  case "$HS_PC_RC" in
    0|10) log "  hindsight plugin carry: OK (rc=$HS_PC_RC)" ;;
    *)    log "ERROR: hindsight plugin carry needs a re-port (rc=$HS_PC_RC)"
          CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1)) ;;
  esac
fi
# --- end hindsight catalog-plugin carry -------------------------------------

# [RETIRED v2026.9.24+] # Verify/regenerate the lock from the patched pyproject, fold any resolver-only
# [RETIRED v2026.9.24+] # normalization into its dedicated carried lock commit, install the exact
# [RETIRED v2026.9.24+] # client into the live venv, and fail closed on any version skew.
# [RETIRED v2026.9.24+] UV_BIN="$AISERVER_HOME/.hermes/bin/uv"
# [RETIRED v2026.9.24+] [ -x "$UV_BIN" ] || UV_BIN="/opt/homebrew/bin/uv"
# [RETIRED v2026.9.24+] if [ ! -x "$UV_BIN" ]; then
# [RETIRED v2026.9.24+]   log "ERROR: uv is unavailable; cannot align the Hindsight client lock"
# [RETIRED v2026.9.24+]   CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+] else
# [RETIRED v2026.9.24+]   log "Regenerating uv.lock for hindsight-client 0.9.2..."
# [RETIRED v2026.9.24+]   if [ "$(id -u)" -eq 0 ]; then
# [RETIRED v2026.9.24+]     sudo -u aiserver -H "$UV_BIN" lock --upgrade-package hindsight-client --directory "$REPO_DIR"
# [RETIRED v2026.9.24+]   else
# [RETIRED v2026.9.24+]     "$UV_BIN" lock --upgrade-package hindsight-client --directory "$REPO_DIR"
# [RETIRED v2026.9.24+]   fi
# [RETIRED v2026.9.24+]   UV_LOCK_RC=$?
# [RETIRED v2026.9.24+]   if [ "$UV_LOCK_RC" -ne 0 ]; then
# [RETIRED v2026.9.24+]     log "ERROR: uv lock failed ($UV_LOCK_RC)"
# [RETIRED v2026.9.24+]     CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+]   else
# [RETIRED v2026.9.24+]     # Gate the fold on the WHOLE lock, not just hindsight-client. Regenerating a
# [RETIRED v2026.9.24+]     # lockfile is how a dependency silently moves; asserting one package and
# [RETIRED v2026.9.24+]     # folding the other 257 unchecked is how a supply-chain change rides in on an
# [RETIRED v2026.9.24+]     # unrelated update. assert_lock_versions.py permits the deliberate
# [RETIRED v2026.9.24+]     # --upgrade-package move (and asserts where it landed), and refuses any other
# [RETIRED v2026.9.24+]     # version change, addition, or removal. Marker/ordering churn still passes.
# [RETIRED v2026.9.24+]     LOCK_GUARD_OUT=$("$REPO_DIR/venv/bin/python" \
# [RETIRED v2026.9.24+]       "$AISERVER_HOME/.hermes/scripts/assert_lock_versions.py" \
# [RETIRED v2026.9.24+]       --repo "$REPO_DIR" --allow-change "hindsight-client==0.9.2" 2>&1)
# [RETIRED v2026.9.24+]     LOCK_GUARD_RC=$?
# [RETIRED v2026.9.24+]     while IFS= read -r line; do
# [RETIRED v2026.9.24+]       [ -n "$line" ] && log "  $line"
# [RETIRED v2026.9.24+]     done <<EOF
# [RETIRED v2026.9.24+] $LOCK_GUARD_OUT
# [RETIRED v2026.9.24+] EOF
# [RETIRED v2026.9.24+]     if [ "$LOCK_GUARD_RC" -ne 0 ]; then
# [RETIRED v2026.9.24+]       log "ERROR: uv.lock regeneration moved undeclared dependency versions; refusing to fold"
# [RETIRED v2026.9.24+]       CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+]     elif ! "${GITCMD[@]}" -C "$REPO_DIR" diff --quiet -- uv.lock; then
# [RETIRED v2026.9.24+]       # `uv lock` re-normalizes the whole file (unordered override sets get
# [RETIRED v2026.9.24+]       # reshuffled, upstream exclusions tighten unrelated markers), so a dirty
# [RETIRED v2026.9.24+]       # uv.lock here is expected and benign -- the guard above has already proven
# [RETIRED v2026.9.24+]       # the churn moved no version except the declared hindsight-client pin.
# [RETIRED v2026.9.24+]       # What must not happen is folding that
# [RETIRED v2026.9.24+]       # churn into an unrelated commit.
# [RETIRED v2026.9.24+]       #
# [RETIRED v2026.9.24+]       # Fold it into the lock-alignment commit wherever that commit sits in the
# [RETIRED v2026.9.24+]       # carried range. Requiring it at HEAD assumed the lock patch was the LAST
# [RETIRED v2026.9.24+]       # registry block; any patch registered after it (cua-snapshot-compat and
# [RETIRED v2026.9.24+]       # neo-context-warning-matrix-replies both are) left the real lock commit
# [RETIRED v2026.9.24+]       # buried and failed the whole update with a message about carried patches.
# [RETIRED v2026.9.24+]       LOCK_SUBJECT="chore(hindsight): align client dependency at 0.9.2 [carried]"
# [RETIRED v2026.9.24+]       LOCK_COMMIT=$("${GITCMD[@]}" -C "$REPO_DIR" rev-list "$STABLE_COMMIT..HEAD" \
# [RETIRED v2026.9.24+]         --fixed-strings --grep="$LOCK_SUBJECT" | head -n 1)
# [RETIRED v2026.9.24+]       if [ -z "$LOCK_COMMIT" ]; then
# [RETIRED v2026.9.24+]         log "ERROR: generated uv.lock changed but no Hindsight lock-alignment commit exists in $STABLE_COMMIT..HEAD; refusing an unregistered lock-only commit"
# [RETIRED v2026.9.24+]         CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+]       elif [ "$LOCK_COMMIT" = "$("${GITCMD[@]}" -C "$REPO_DIR" rev-parse HEAD)" ]; then
# [RETIRED v2026.9.24+]         "${GITCMD[@]}" -C "$REPO_DIR" add uv.lock
# [RETIRED v2026.9.24+]         "${GITCMD[@]}" -C "$REPO_DIR" commit --amend --no-edit >/dev/null || CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+]       else
# [RETIRED v2026.9.24+]         # The lock commit is buried under later carried patches. Amend it in
# [RETIRED v2026.9.24+]         # place with a non-interactive autosquash so the later commits keep
# [RETIRED v2026.9.24+]         # their content, order, and messages.
# [RETIRED v2026.9.24+]         "${GITCMD[@]}" -C "$REPO_DIR" add uv.lock
# [RETIRED v2026.9.24+]         if ! "${GITCMD[@]}" -C "$REPO_DIR" commit --fixup "$LOCK_COMMIT" >/dev/null 2>&1 \
# [RETIRED v2026.9.24+]           || ! GIT_SEQUENCE_EDITOR=true GIT_EDITOR=true "${GITCMD[@]}" -C "$REPO_DIR" \
# [RETIRED v2026.9.24+]                rebase --autosquash --autostash "${LOCK_COMMIT}^" >/dev/null 2>&1; then
# [RETIRED v2026.9.24+]           "${GITCMD[@]}" -C "$REPO_DIR" rebase --abort >/dev/null 2>&1 || true
# [RETIRED v2026.9.24+]           log "ERROR: could not fold the regenerated uv.lock into $LOCK_COMMIT"
# [RETIRED v2026.9.24+]           CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+]         fi
# [RETIRED v2026.9.24+]       fi
# [RETIRED v2026.9.24+]     fi
# [RETIRED v2026.9.24+]   fi

# [RETIRED v2026.9.24+]   log "Installing hindsight-client 0.9.2 into the Hermes venv..."
# [RETIRED v2026.9.24+]   if [ "$(id -u)" -eq 0 ]; then
# [RETIRED v2026.9.24+]     sudo -u aiserver -H env VIRTUAL_ENV="$REPO_DIR/venv" "$UV_BIN" pip install --python "$REPO_DIR/venv/bin/python" "hindsight-client==0.9.2"
# [RETIRED v2026.9.24+]   else
# [RETIRED v2026.9.24+]     env VIRTUAL_ENV="$REPO_DIR/venv" "$UV_BIN" pip install --python "$REPO_DIR/venv/bin/python" "hindsight-client==0.9.2"
# [RETIRED v2026.9.24+]   fi
# [RETIRED v2026.9.24+]   CLIENT_INSTALL_RC=$?
# [RETIRED v2026.9.24+]   INSTALLED_HINDSIGHT=$("$REPO_DIR/venv/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("hindsight-client"))' 2>/dev/null)
# [RETIRED v2026.9.24+]   if [ "$CLIENT_INSTALL_RC" -ne 0 ] || [ "$INSTALLED_HINDSIGHT" != "0.9.2" ]; then
# [RETIRED v2026.9.24+]     log "ERROR: live Hermes venv has hindsight-client ${INSTALLED_HINDSIGHT:-missing}, expected 0.9.2"
# [RETIRED v2026.9.24+]     CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
# [RETIRED v2026.9.24+]   fi
# [RETIRED v2026.9.24+] fi

# --- dotenv-search sentinel -------------------------------------------------
# hermes-agent and its venv live INSIDE ~/.hermes, which is also the default
# profile's HERMES_HOME. python-dotenv's find_dotenv() walks up from either the
# process cwd or from a package's own file in venv/.../site-packages -- both
# paths cross this directory on the way to ~/.hermes/.env. Without a .env here,
# any dependency calling load_dotenv() at import time silently loads the DEFAULT
# profile's environment into whichever profile's process imported it. That is
# how the quant/Oracle gateway ended up running with Neo's DISCORD_BOT_TOKEN,
# channel allow-list and Matrix identity on 2026-07-24/25.
#
# The file is gitignored, so a fresh clone or a stray clean loses it silently.
# Re-create it (empty) every update. Never put configuration in it: it would
# apply to every profile at once.
REPO_ENV_SENTINEL="${HERMES_AGENT_DIR:-/Users/aiserver/.hermes/hermes-agent}/.env"
if [ ! -f "$REPO_ENV_SENTINEL" ]; then
  log "dotenv sentinel missing; re-creating $REPO_ENV_SENTINEL"
  {
    echo "# Intentionally empty - do not put configuration here."
    echo "#"
    echo "# Terminates upward .env discovery at the repo root so a dependency"
    echo "# calling python-dotenv cannot load the DEFAULT profile's ~/.hermes/.env"
    echo "# into another profile's process. See hermes-safe-update.sh."
    echo "#"
    echo "# Hermes reads \$HERMES_HOME/.env, never this file."
  } > "$REPO_ENV_SENTINEL"
elif [ -s "$REPO_ENV_SENTINEL" ] && grep -qE '^[A-Z][A-Z0-9_]*=' "$REPO_ENV_SENTINEL"; then
  log "ERROR: $REPO_ENV_SENTINEL has real assignments; it must stay empty (it applies to ALL profiles)"
fi
  # no-agent Bot Chat cron completion notices: append a durable display-only row,
  # then emit a TUI/Desktop-compatible cron completion event. This exact payload
  # is first activated by --cron-completion-delivery-only; ordinary stable updates
  # replay it so reset-to-release cannot drop the server behavior or its regression tests.
  reapply_carried_patch \
    "cron_completion_delivery_20260920.REPORTED-v2026.9.24.patch" \
    "tools/bot_live_delivery.py" \
    "def deliver_completion_notice(" \
    "cron-completion-delivery" \
    "fix(cron): deliver no-agent Bot Chat completions [carried]"

  reapply_carried_patch \
    "matrix_mentions_authoritative_20260923.patch" \
    "plugins/platforms/matrix/adapter.py" \
    "mentions_field_present = mention_user_ids is not None" \
    "matrix-mentions-authoritative" \
    "fix(matrix): trust m.mentions over bare-localpart text match [carried]"

  reapply_carried_patch \
    "unauthorized_hook_suppression_20260923.REPORTED-v2026.9.24.patch" \
    "gateway/platforms/base.py" \
    "def _precheck_hook_suppression(" \
    "unauthorized-hook-suppression" \
    "fix(gateway): suppress processing hooks for unauthorized senders [carried]"

  # 1Password vault backend: `op item get <id>` fails under a service account unless the
  # item's vault is named ("a vault query must be provided when this command is called by a
  # service account"), so browser_vault_fill never worked for service-account users.
  # list_items records item->vault and _item_get passes --vault. Upstream PR candidate:
  # branch fix/op-vault-service-account. Drop once equivalent is in the stable release.
  reapply_carried_patch \
    "onepassword_vault_service_account_20261007.patch" \
    "agent/vault_backends/onepassword.py" \
    "def _item_get(" \
    "onepassword-vault-service-account" \
    "fix(vault): name the item's vault on 1Password reads so service accounts can fill [carried]"

  # Matrix per-room link-preview opt-out: outbound text in rooms listed under matrix:
  # disable_link_previews carries empty m.url_previews/com.beeper.linkpreviews (MSC4095), so
  # Sable renders no cards for the news digest without a client/room setting. Upstream PR
  # candidate: branch fix/matrix-disable-link-previews. Drop once in the stable release.
  reapply_carried_patch \
    "matrix_link_preview_opt_out_20261007.patch" \
    "plugins/platforms/matrix/adapter.py" \
    "def _parse_link_preview_opt_out(" \
    "matrix-link-preview-opt-out" \
    "feat(matrix): opt rooms out of link previews via empty bundled previews [carried]"

fi
# --- end mode-specific patching --------------------------------------------

# --- verify carried patches actually landed --------------------------------
# reapply_carried_patch only logs an ERROR and returns 1 on failure, and this
# script runs with `set -uo pipefail` (no -e) — so before this block a rejected
# patch scrolled past and the run still finished "OK: update complete". That is
# how anthropic_mid_conversation_system.patch was silently dropped in the
# 2026-07-23 reset to origin/main. Re-grep every marker in the FINAL tree
# (stronger than trusting return codes) and refuse to report success without
# them.
while IFS='|' read -r _mfile _mstr _label; do
  [ -z "$_mfile" ] && continue
  CARRIED_PATCH_TOTAL=$((CARRIED_PATCH_TOTAL + 1))
  # Patch-only plugin registrations record an absolute marker path.
  case "$_mfile" in
    /*) _mfile_abs="$_mfile" ;;
    *) _mfile_abs="$REPO_DIR/$_mfile" ;;
  esac
  if ! grep -Fq "$_mstr" "$_mfile_abs" 2>/dev/null; then
    CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
    CARRIED_PATCH_MISSING="${CARRIED_PATCH_MISSING}  - ${_label}  [${_mfile}]
"
  fi
done <<VERIFY_CARRIED_PATCHES
${CARRIED_PATCH_CHECKS}
VERIFY_CARRIED_PATCHES

if [ "$CUA_COMPAT_ONLY" = "1" ] && [ "$(git -C "$REPO_DIR" rev-parse HEAD^{tree} 2>/dev/null)" != "$PATCH_ONLY_CORE_EXPECTED_TREE" ]; then
  log "ERROR: CUA-only post-apply tree differs from the pinned expected tree $PATCH_ONLY_CORE_EXPECTED_TREE"
  CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
fi
if [ "$CARRIED_PATCH_MISSING_COUNT" -gt 0 ]; then
  if [ -n "$CARRIED_PATCH_MISSING" ]; then
    log "ERROR: $CARRIED_PATCH_MISSING_COUNT validation failure(s) after this update; carried patches NOT in the tree:"
    # printf '%s' emits no trailing newline, so `read` returns false on the final
    # line and drops it -- with a single missing patch the whole list vanished
    # and the log named nothing at all. printf '%s\n' terminates the last line.
    printf '%s\n' "$CARRIED_PATCH_MISSING" | while IFS= read -r _line; do
      [ -n "$_line" ] && log "$_line"
    done
    log "  Each needs a manual re-port against the new upstream code."
  else
    # The counter is shared with non-patch validation failures (Hindsight lock
    # alignment, client install). Reporting those as missing carried patches
    # sent the last update hunting a patch that was present all along.
    log "ERROR: $CARRIED_PATCH_MISSING_COUNT validation failure(s) after this update; all $CARRIED_PATCH_TOTAL carried patches are present."
    log "  The failure is non-patch validation -- see the ERROR lines above."
  fi
else
  log "Verified: all $CARRIED_PATCH_TOTAL carried patches present in the tree."
fi

# Content markers alone are insufficient: a marker can exist inside a Git
# conflict block. Compile all Python source areas touched by carried patches so
# conflict markers, indentation damage, and syntax errors block success before
# the gateways are brought back.
write_update_journal "validating"
log "Compiling patched Python tree..."
COMPILE_PYCACHE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-safe-update-pycache.XXXXXX")"
if ! PYTHONPYCACHEPREFIX="$COMPILE_PYCACHE_DIR" hermes_runtime_run "$REPO_DIR" module compileall -q \
  "$REPO_DIR/agent" \
  "$REPO_DIR/hermes_cli" \
  "$REPO_DIR/gateway" \
  "$REPO_DIR/gateway" \
  "$REPO_DIR/plugins/platforms/discord"; then  # [v2026.9.24+] plugins/memory/hindsight left core
  log "ERROR: patched Python tree failed compilation — refusing to report a successful update"
  CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
else
  log "Verified: patched Python tree compiles."
fi
if [ -n "$PATCH_ONLY_COMPILE_FILES" ]; then
  log "Compiling files touched by the pinned fix set..."
  while IFS= read -r _pyfile; do
    [ -z "$_pyfile" ] && continue
    if ! PYTHONPYCACHEPREFIX="$COMPILE_PYCACHE_DIR" hermes_runtime_run "$REPO_DIR" module py_compile "$_pyfile"; then
      log "ERROR: $_pyfile failed compilation — refusing to report a successful update"
      CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
    fi
  done <<PATCH_ONLY_COMPILE
${PATCH_ONLY_COMPILE_FILES}
PATCH_ONLY_COMPILE
fi
rm -rf "$COMPILE_PYCACHE_DIR"
if [ "$PATCH_ONLY_DESKTOP" = "1" ]; then
  log "Installing the prebuilt Desktop artifact after source-pair validation..."
  if ! desktop_activate; then
    log "ERROR: Desktop activation failed; rolling back source pair and Desktop snapshot"
    CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
  fi
fi

if [ "$PATCH_ONLY" != "1" ]; then
# hermes_cli/web_dist is gitignored build output (dashboard UI from web/): the
# tag checkout never touches it and every dashboard daemon starts with
# --skip-build, so nothing rebuilds it after a tag bump and the dashboard keeps
# serving the previous tag's bundle (merittas served an Aug-28 bundle behind
# v2026.8.31 until 2026-09-07). Rebuild through Hermes's own content-hash-gated
# builder: a no-op when the stamp already matches web/, a real vite build
# otherwise. A failed build counts as a failed validation and takes the same
# rollback path as a missing carried patch.
log "Checking dashboard web UI bundle against web/ source..."
# Resolve the current PM generation at every launch; never cache its Python path.
# Compatibility note: _web_ui_build_needed/_build_web_ui moved from
# hermes_cli/main.py into hermes_cli/main_web_build.py in the release
# pinned at 2237be355906fbe6065ce1815711eee52b2d646e. main.py re-exports
# _build_web_ui but NOT _web_ui_build_needed there, and no __getattr__
# chain resolves it, so a bare `hm._web_ui_build_needed(...)` raises
# AttributeError on that release. Resolve both symbols across old/new
# layouts below; _web_ui_build_needed is informational only (rebuilt vs
# current in the log line) and fails CLOSED (assumes a build is needed)
# if it cannot be found anywhere, but _build_web_ui's real return value
# is always what gates success/failure -- this must never be weakened.
if (cd "$REPO_DIR" && hermes_runtime_run "$REPO_DIR" stdin <<'PYWEB'
import sys
from pathlib import Path
import hermes_cli.main as hm


def _resolve(mod, name):
    try:
        return getattr(mod, name)
    except AttributeError:
        return None


build_fn = _resolve(hm, "_build_web_ui")
build_source = "main"
if build_fn is None:
    import hermes_cli.main_web_build as hwb
    build_fn = _resolve(hwb, "_build_web_ui")
    build_source = "main_web_build"
if build_fn is None:
    print("web UI bundle: BUILD FAILED (compat: _build_web_ui not found in "
          "hermes_cli.main or hermes_cli.main_web_build)")
    sys.exit(1)

needed_fn = _resolve(hm, "_web_ui_build_needed")
needed_source = "main"
if needed_fn is None:
    import hermes_cli.main_web_build as hwb
    needed_fn = _resolve(hwb, "_web_ui_build_needed")
    needed_source = "main_web_build"
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
PYWEB
); then
  log "Verified: dashboard web UI bundle matches web/ source."
else
  log "ERROR: dashboard web UI bundle build failed — refusing to report a successful update"
  CARRIED_PATCH_MISSING_COUNT=$((CARRIED_PATCH_MISSING_COUNT + 1))
fi
fi
# --- end carried-patch verification ----------------------------------------

if [ "$CARRIED_PATCH_MISSING_COUNT" -gt 0 ]; then
  log "ERROR: update validation failed before service restoration; rolling back instead of starting an incomplete tree"
  exit 1
fi

# Code validation is green, but rollback remains externally armed through
# supervisor startup and live health. The shell no longer rolls back on its own
# normal restore call; the marker+journal guardian still can if this process is
# killed or a post-start gate fails.
UPDATE_STAGING_ACTIVE=0
PLUGIN_STAGING_ACTIVE=0
DESKTOP_STAGING_ACTIVE=0
KEEP_MARKER_THROUGH_HEALTH=1
write_update_journal "service-recovery"

trap - EXIT
restore_services
RESTORE_RC=$?
if [ "$RESTORE_RC" -ne 0 ]; then
  log "ERROR: supervisor restoration reported rc=$RESTORE_RC; leaving marker for guardian recovery"
  exit "$RESTORE_RC"
fi

sleep 20
ALL_SERVICES_HEALTHY=0
for _attempt in {1..30}; do
  ALL_SERVICES_HEALTHY=1
  for s in "${UPDATE_SERVICE_DAEMONS[@]}"; do
    if ! sudo -n launchctl print "system/ai.hermes.$s" 2>/dev/null | grep -q $'\tstate = running'; then
      ALL_SERVICES_HEALTHY=0
    fi
  done
  if ! sudo -n launchctl print system/ai.hermes.gateway-health >/dev/null 2>&1; then
    ALL_SERVICES_HEALTHY=0
  fi
  for url in ${HEALTH_URLS[@]+"${HEALTH_URLS[@]}"}; do
    HTTP_CODE=$(curl -sS -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)
    case "$HTTP_CODE" in 2??|3??|4??) ;; *) ALL_SERVICES_HEALTHY=0 ;; esac
  done
  [ "$ALL_SERVICES_HEALTHY" -eq 1 ] && break
  sleep 2
done
if [ "$ALL_SERVICES_HEALTHY" -ne 1 ]; then
  log "ERROR: one or more supervised Hermes services/endpoints failed live recovery; leaving marker for guardian rollback"
  exit 1
fi
log "Verified: all selected supervisors present and dashboard/WebUI endpoints responsive."

COUNT=0
for p in ${UPDATE_GATEWAY_PROFILES[@]+"${UPDATE_GATEWAY_PROFILES[@]}"}; do
  if sudo -n launchctl print "system/ai.hermes.gateway-$p" 2>/dev/null | grep -q $'\tstate = running'; then
    COUNT=$((COUNT + 1))
  fi
done
log "Target gateway services running: $COUNT (expected $UPDATE_EXPECTED_GATEWAYS)"
ps -eo pid,ppid,user,etime,command | grep -E "[h]ermes_cli\\.main --profile ($(IFS='|'; echo "${GATEWAY_PROFILES[*]}")) gateway run --replace"
if [ "$COUNT" -ne "$UPDATE_EXPECTED_GATEWAYS" ]; then
  log "WARN: gateway service count mismatch — check ~/.hermes/profiles/*/logs/gateway.error.log"
  exit 1
fi
if [ "$CARRIED_PATCH_MISSING_COUNT" -gt 0 ]; then
  log "FAILED: services are up, but $CARRIED_PATCH_MISSING_COUNT carried patch(es) are missing — see the ERROR block above."
  exit 1
fi

if [ "$PATCH_ONLY" = "1" ]; then
write_patch_only_receipt || {
  log "ERROR: could not record the patch-only receipt; leaving marker for guardian recovery"
  exit 1
}
log "Recorded pinned fix set '$PATCH_ONLY_FIX_SET' at $PATCH_ONLY_RECEIPT ($JOURNAL_COMMIT_ID)"
else
# --- hindsight runtime patch (upstream #3588) ------------------------------
# Not a hermes-agent repo patch, so it can't use reapply_carried_patch: it edits
# the isolated uv-archive hindsight-api runtime. Re-assert it here because a
# hindsight upgrade or `uv cache clean` replaces that archive and silently
# restores the 30-60s recall / TimeoutError behavior.
#
# Applies upstream 5f137bf3 (#3588), merged 2026-08-18. Latest core release
# v0.9.1 is tagged 2026-08-14, so no released version carries it yet. Once a
# release ships with it this becomes a no-op and the script can be retired.
# It also reverts the earlier #3715 shape, which was CLOSED upstream as a revert
# of #3451 that regresses on aged banks (#3085). See the script header.
if [ -x "$HOME/.hermes/scripts/hindsight-ensure-3588.sh" ]; then
  log "Checking hindsight #3588 runtime patch..."
  HS_OUT=$("$HOME/.hermes/scripts/hindsight-ensure-3588.sh" 2>&1); HS_RC=$?
  printf '%s\n' "$HS_OUT" | while IFS= read -r l; do log "  $l"; done
  case "$HS_RC" in
    0)  log "  hindsight #3588: OK" ;;
    10) log "  hindsight #3588: RE-APPLIED — restart the hindsight daemon to load it" ;;
    *)  log "  WARN: hindsight #3588 check needs attention (rc=$HS_RC)" ;;
  esac
fi
# --- end hindsight runtime patch -------------------------------------------

# --- hindsight defer-clamp local carry (2026-09-23) -------------------------
# Not a hermes-agent repo patch (edits the isolated hindsight-api runtime, both
# server-venv and every cached uv archive), so it can't use
# reapply_carried_patch. Re-assert it here for the same reason as #3588 above:
# a hindsight upgrade or `uv cache clean` replaces the runtime and silently
# restores the unbounded-defer behavior.
#
# WHY: memory_engine.py's ProviderRateLimitResetError catch site trusted a
# provider-reported retry_at verbatim, with no ceiling, before constructing
# DeferOperation. DeferOperation deliberately does not count as a failure (no
# retry_count bump, no error_message), so a stale/conservative provider quota
# report (observed: OmniRoute Codex pool reporting "reset after 64h55m" for a
# window that actually cleared in ~7h; an earlier incident reported +12 days)
# parks retain work for days, invisible to every error-based health check.
# Confirmed NOT fixed upstream as of vectorize-io/hindsight v0.10.1 (main,
# 2026-09-23) -- see the drafted upstream issue in
# ~/.hermes/cache/scratch/hindsight-defer-clamp-upstream.md. Retire this block
# once upstream ships an equivalent ceiling (re-check the marker below against
# a new release before assuming so).
if [ -x "$HOME/.hermes/scripts/hindsight-ensure-defer-clamp.sh" ]; then
  log "Checking hindsight defer-clamp runtime patch..."
  HS_OUT=$("$HOME/.hermes/scripts/hindsight-ensure-defer-clamp.sh" "$HOME/.hindsight/server-venv/bin/python" 2>&1); HS_RC=$?
  printf '%s\n' "$HS_OUT" | while IFS= read -r l; do log "  $l"; done
  case "$HS_RC" in
    0)  log "  hindsight defer-clamp: OK" ;;
    10) log "  hindsight defer-clamp: RE-APPLIED — restart the hindsight daemon to load it" ;;
    *)  log "  WARN: hindsight defer-clamp check needs attention (rc=$HS_RC)" ;;
  esac
fi
# Read-only pin/sibling-selection gate: never silently return to uvx.
HS_RUNTIME_OUT=$("$HOME/.hindsight/server-venv/bin/python" "$HOME/.hermes/scripts/hindsight-runtime-check.py" 2>&1); HS_RUNTIME_RC=$?
printf '%s\n' "$HS_RUNTIME_OUT" | while IFS= read -r l; do log "  $l"; done
if [ "$HS_RUNTIME_RC" -ne 0 ]; then
  log "FAIL: persistent Hindsight runtime verification failed (rc=$HS_RUNTIME_RC)"
  exit 1
fi
# --- end hindsight defer-clamp local carry ----------------------------------

# Persist the last *successful* stable deployment only after source validation
# and supervised service recovery passed. The candidate receipt is separate
# because its existence alone is not proof that anything was deployed.
DEPLOYED_TMP="${DEPLOYED_STABLE_RECEIPT}.tmp.$$"
/usr/bin/python3 - "$STABLE_RECEIPT" "$DEPLOYED_TMP" "$DEPLOYED_STABLE_RECEIPT" <<'PY'
import os, shutil, sys
source, temporary, destination = sys.argv[1:]
shutil.copyfile(source, temporary)
os.chmod(temporary, 0o600)
with open(temporary, "rb") as handle:
    os.fsync(handle.fileno())
os.replace(temporary, destination)
dir_fd = os.open(os.path.dirname(destination), os.O_RDONLY)
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
PY
log "Recorded deployed stable release: $STABLE_TAG at $STABLE_COMMIT"
fi

write_update_journal "complete"
release_update_marker
log "Quick rollback snapshot retained at $ROLLBACK_SNAPSHOT"
log "OK: update complete, supervision restored."
