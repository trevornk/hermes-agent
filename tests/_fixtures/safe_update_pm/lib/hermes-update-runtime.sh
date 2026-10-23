#!/bin/bash
# Invocation seam only: never install dependencies or own a supervisor.
# Initial handoff can use the historical venv. A PM tree MUST use its published
# launcher; a missing/broken launcher never falls back to the obsolete venv.
hermes_runtime_command(){
  local repo="$1" mode="$2"
  shift 2
  HERMES_RUNTIME_COMMAND=()
  if [ -f "$repo/pm/lock.json" ]; then
    local launcher="$repo/.hermes/bin/hermes"
    if [ ! -x "$launcher" ]; then
      printf '%s\n' "ERROR: PM installation launcher missing: $launcher" >&2
      return 1
    fi
    case "$mode" in
      cli) HERMES_RUNTIME_COMMAND=("$launcher" "$@") ;;
      module) HERMES_RUNTIME_COMMAND=("$launcher" --run-module "$@") ;;
      stdin) HERMES_RUNTIME_COMMAND=(/usr/bin/python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/hermes-update-stdin.py" "$repo" "$@") ;;
      *) printf 'ERROR: unknown runtime mode: %s\n' "$mode" >&2; return 2 ;;
    esac
  else
    local legacy="$repo/venv/bin/python"
    [ -x "$legacy" ] || { printf 'ERROR: legacy interpreter missing: %s\n' "$legacy" >&2; return 1; }
    case "$mode" in
      cli) HERMES_RUNTIME_COMMAND=("$legacy" -m hermes_cli.main "$@") ;;
      module) HERMES_RUNTIME_COMMAND=("$legacy" -m "$@") ;;
      stdin) HERMES_RUNTIME_COMMAND=("$legacy" - "$@") ;;
      *) printf 'ERROR: unknown runtime mode: %s\n' "$mode" >&2; return 2 ;;
    esac
  fi
}
hermes_runtime_run(){
  hermes_runtime_command "$@" || return $?
  if [ "$(id -u)" -eq 0 ]; then
    sudo -u aiserver -H env HERMES_HOME="$AISERVER_HOME/.hermes" "${HERMES_RUNTIME_COMMAND[@]}"
  else
    HERMES_HOME="$AISERVER_HOME/.hermes" "${HERMES_RUNTIME_COMMAND[@]}"
  fi
}
