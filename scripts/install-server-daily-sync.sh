#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'this installer requires macOS launchd\n' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "$repo_root/.." && pwd)"
sync_script="$repo_root/scripts/sync-server-daily.sh"

label="${PDFTOTEX_DAILY_SYNC_LABEL:-com.pdftotex.server-daily-sync}"
interval="${PDFTOTEX_DAILY_SYNC_INTERVAL:-120}"
server="${PDFTOTEX_DAILY_SERVER:-root@123.57.160.96}"
remote_ledger="${PDFTOTEX_DAILY_REMOTE_LEDGER:-/srv/pdftotex/data/monitoring/daily/completions.jsonl}"
config="${PDFTOTEX_DAILY_CONFIG:-$workspace_root/data/monitoring/daily/config.json}"
start_date="${PDFTOTEX_DAILY_START_DATE:-2026-09-25}"
python_executable="${PDFTOTEX_DAILY_PYTHON:-$(command -v python3)}"
output_dir="$("$python_executable" -c 'import json,sys; print(json.load(open(sys.argv[1]))["output_dir"])' "$config")"
launch_agents="$HOME/Library/LaunchAgents"
plist="$launch_agents/$label.plist"

if [[ ! "$interval" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PDFTOTEX_DAILY_SYNC_INTERVAL must be a positive integer\n' >&2
  exit 1
fi

mkdir -p "$output_dir" "$launch_agents"

PLIST_PATH="$plist" LABEL="$label" INTERVAL="$interval" \
SYNC_SCRIPT="$sync_script" SERVER="$server" REMOTE_LEDGER="$remote_ledger" \
CONFIG="$config" START_DATE="$start_date" PYTHON_EXECUTABLE="$python_executable" \
OUTPUT_DIR="$output_dir" "$python_executable" - <<'PY'
import os
import plistlib
from pathlib import Path

job = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [
        os.environ["SYNC_SCRIPT"],
        os.environ["SERVER"],
        os.environ["REMOTE_LEDGER"],
        os.environ["CONFIG"],
        os.environ["START_DATE"],
        os.environ["PYTHON_EXECUTABLE"],
    ],
    "RunAtLoad": True,
    "StartInterval": int(os.environ["INTERVAL"]),
    "ProcessType": "Background",
    "StandardOutPath": str(Path(os.environ["OUTPUT_DIR"]) / "server-sync.log"),
    "StandardErrorPath": str(Path(os.environ["OUTPUT_DIR"]) / "server-sync.error.log"),
}
path = Path(os.environ["PLIST_PATH"])
temporary = path.with_suffix(".plist.tmp")
temporary.write_bytes(plistlib.dumps(job, fmt=plistlib.FMT_XML, sort_keys=False))
temporary.replace(path)
PY

# The server ledger owns records from the cutover date onward. Disable the old
# local scanner so it cannot append a second source of truth after the merge.
old_label="com.lexiod.daily-stats"
launchctl bootout "gui/$(id -u)/$old_label" 2>/dev/null || true
rm -f "$launch_agents/$old_label.plist"

service="gui/$(id -u)/$label"
launchctl bootout "$service" 2>/dev/null || true
loaded=false
for _ in {1..5}; do
  if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
    loaded=true
    break
  fi
  sleep 1
done
if [[ "$loaded" != true ]]; then
  printf 'failed to load %s\n' "$service" >&2
  exit 1
fi
launchctl kickstart -k "$service"

printf 'installed %s (every %s seconds)\n' "$service" "$interval"
printf 'daily report: %s/daily.md\n' "$output_dir"
