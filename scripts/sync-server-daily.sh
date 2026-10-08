#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "$repo_root/.." && pwd)"

server="${1:-root@123.57.160.96}"
remote_ledger="${2:-/srv/pdftotex/data/monitoring/daily/completions.jsonl}"
config="${3:-$workspace_root/data/monitoring/daily/config.json}"
start_date="${4:-2026-09-25}"
python_executable="${5:-$(command -v python3)}"
exec "$python_executable" "$repo_root/pipeline/texopt/monitoring/server_daily_sync.py" \
  --config "$config" \
  --server "$server" \
  --remote-ledger "$remote_ledger" \
  --start-date "$start_date"
