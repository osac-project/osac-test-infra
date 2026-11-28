#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 1 ]] || { echo "Usage: $0 sno|mno" >&2; exit 2; }
case "$1" in
  sno) minimum_gib=160 ;;
  mno) minimum_gib=240 ;;
  *) echo "Unsupported cluster topology: $1" >&2; exit 2 ;;
esac

available_bytes=$(df -B1 --output=avail /var/lib/cluster-tool/overlays | tail -n 1)
[[ "${available_bytes}" =~ ^[0-9]+$ ]] || {
  echo "Could not measure free space on the cluster-tool overlay filesystem." >&2
  exit 1
}
if (( available_bytes < minimum_gib * 1024 * 1024 * 1024 )); then
  echo "Insufficient space for $1 OSAC testing: need at least ${minimum_gib} GiB free on the cluster-tool overlay filesystem." >&2
  exit 1
fi

echo "Sufficient host space for $1 OSAC testing."
