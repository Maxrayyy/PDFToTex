#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "$repo_root/.." && pwd)"

server="${1:-root@123.57.160.96}"
remote_ledger="${2:-/srv/pdftotex/data/monitoring/daily/completions.jsonl}"
config="${3:-$workspace_root/data/monitoring/daily/config.json}"
start_date="${4:-2026-09-25}"
python_executable="${5:-$(command -v python3)}"
output_dir="$("$python_executable" -c 'import json,sys; print(json.load(open(sys.argv[1]))["output_dir"])' "$config")"

mkdir -p "$output_dir"
temporary="$(mktemp "$output_dir/.server-completions.jsonl.XXXXXX")"
trap 'rm -f "$temporary"' EXIT

/usr/bin/scp \
  -q \
  -o BatchMode=yes \
  -o ConnectTimeout=15 \
  "$server:$remote_ledger" \
  "$temporary"

if [[ ! -s "$temporary" ]]; then
  printf 'empty server daily ledger: %s:%s\n' "$server" "$remote_ledger" >&2
  exit 1
fi

"$python_executable" "$repo_root/pipeline/texopt/monitoring/server_daily_sync.py" \
  --config "$config" \
  --remote-ledger "$temporary" \
  --start-date "$start_date"
