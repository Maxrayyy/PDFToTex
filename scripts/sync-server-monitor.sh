#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "$repo_root/.." && pwd)"

server="${1:-root@123.57.160.96}"
remote_file="${2:-/srv/pdftotex/data/monitoring/realtime/latest.md}"
output_file="${3:-$workspace_root/data/monitoring/server/latest.md}"

output_dir="$(dirname "$output_file")"
mkdir -p "$output_dir"
temporary="$(mktemp "$output_dir/.latest.md.XXXXXX")"
trap 'rm -f "$temporary"' EXIT

/usr/bin/scp \
  -q \
  -o BatchMode=yes \
  -o ConnectTimeout=15 \
  "$server:$remote_file" \
  "$temporary"

if [[ ! -s "$temporary" ]] || ! grep -q '^# 容器监测' "$temporary"; then
  printf 'invalid server monitor document: %s:%s\n' "$server" "$remote_file" >&2
  exit 1
fi

chmod 0644 "$temporary"
mv -f "$temporary" "$output_file"
trap - EXIT
printf 'updated %s from %s:%s\n' "$output_file" "$server" "$remote_file"
