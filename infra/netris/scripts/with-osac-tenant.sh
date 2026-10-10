#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 TENANT COMMAND [ARG...]" >&2
  exit 2
fi

target_tenant=$1
shift
if [[ -z "$target_tenant" ]]; then
  echo "Tenant name must not be empty" >&2
  exit 2
fi

current_tenant=$(osac tenant)
case "$current_tenant" in
  "Current tenant: "*) previous_tenant=${current_tenant#Current tenant: } ;;
  "No tenant is currently set."*) previous_tenant= ;;
  *)
    printf 'Could not determine the current OSAC tenant from: %s\n' "$current_tenant" >&2
    exit 1
    ;;
esac

restore_tenant() {
  local command_status=$?
  local restore_status=0
  trap - EXIT INT TERM HUP

  if osac tenant --clear >/dev/null; then
    if [[ -n "$previous_tenant" ]]; then
      osac tenant "$previous_tenant" >/dev/null || restore_status=$?
    fi
  else
    restore_status=$?
  fi

  if (( restore_status != 0 )); then
    if [[ -n "$previous_tenant" ]]; then
      printf 'Could not restore the prior OSAC tenant. Restore it with: osac tenant --clear && osac tenant %q\n' "$previous_tenant" >&2
    else
      echo 'Could not clear the temporary OSAC tenant. Run: osac tenant --clear' >&2
    fi
    if (( command_status == 0 )); then
      command_status=1
    fi
  fi
  exit "$command_status"
}

trap restore_tenant EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

osac tenant --clear >/dev/null
osac tenant "$target_tenant" >/dev/null
"$@"
