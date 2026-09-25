#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'this installer requires macOS launchd\n' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(cd "$repo_root/.." && pwd)"
sync_script="$repo_root/scripts/sync-server-monitor.sh"

label="${PDFTOTEX_MONITOR_SYNC_LABEL:-com.pdftotex.server-monitor-sync}"
interval="${PDFTOTEX_MONITOR_SYNC_INTERVAL:-120}"
server="${PDFTOTEX_MONITOR_SERVER:-root@123.57.160.96}"
remote_file="${PDFTOTEX_MONITOR_REMOTE_FILE:-/srv/pdftotex/data/monitoring/realtime/latest.md}"
output_file="${PDFTOTEX_MONITOR_OUTPUT:-$workspace_root/data/monitoring/server/latest.md}"
output_dir="$(dirname "$output_file")"
launch_agents="$HOME/Library/LaunchAgents"
plist="$launch_agents/$label.plist"

if [[ ! "$interval" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PDFTOTEX_MONITOR_SYNC_INTERVAL must be a positive integer\n' >&2
  exit 1
fi

mkdir -p "$output_dir" "$launch_agents"

PLIST_PATH="$plist" LABEL="$label" INTERVAL="$interval" \
SYNC_SCRIPT="$sync_script" SERVER="$server" REMOTE_FILE="$remote_file" \
OUTPUT_FILE="$output_file" OUTPUT_DIR="$output_dir" python3 - <<'PY'
import os
import plistlib
from pathlib import Path

job = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [
        os.environ["SYNC_SCRIPT"],
        os.environ["SERVER"],
        os.environ["REMOTE_FILE"],
        os.environ["OUTPUT_FILE"],
    ],
    "RunAtLoad": True,
    "StartInterval": int(os.environ["INTERVAL"]),
    "StandardOutPath": str(Path(os.environ["OUTPUT_DIR"]) / "sync.log"),
    "StandardErrorPath": str(Path(os.environ["OUTPUT_DIR"]) / "sync.error.log"),
}
path = Path(os.environ["PLIST_PATH"])
temporary = path.with_suffix(".plist.tmp")
temporary.write_bytes(plistlib.dumps(job, fmt=plistlib.FMT_XML, sort_keys=False))
temporary.replace(path)
PY

service="gui/$(id -u)/$label"
launchctl bootout "$service" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl kickstart -k "$service"

printf 'installed %s (every %s seconds)\n' "$service" "$interval"
printf 'monitor file: %s\n' "$output_file"
