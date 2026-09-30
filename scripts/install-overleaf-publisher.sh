#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'run this installer as root\n' >&2
  exit 1
fi

: "${OVERLEAF_U1_REMOTE:?set OVERLEAF_U1_REMOTE}"
: "${OVERLEAF_U3_REMOTE:?set OVERLEAF_U3_REMOTE}"

if [[ -t 0 ]]; then
  printf 'Overleaf Git authentication token: ' >&2
fi
IFS= read -r overleaf_token
if [[ -z "$overleaf_token" ]]; then
  printf 'Overleaf token must not be empty\n' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
deployment_root="${PDFTOTEX_ROOT:-$(cd "$repo_root/.." && pwd)}"
if [[ "$deployment_root" != /* ]]; then
  printf 'PDFTOTEX_ROOT must be an absolute path: %s\n' "$deployment_root" >&2
  exit 1
fi

overleaf_root="$deployment_root/overleaf"
home_dir="$overleaf_root/home"
projects_dir="$overleaf_root/projects"
config_file="$overleaf_root/config.json"
credentials_file="$home_dir/.git-credentials"
unit_dir=/etc/systemd/system

install -d -o pdftotex -g pdftotex -m 0750 \
  "$overleaf_root" "$home_dir" "$projects_dir"
umask 077
printf 'https://git:%s@git.overleaf.com\n' "$overleaf_token" > "$credentials_file"
unset overleaf_token
chown pdftotex:pdftotex "$credentials_file"
chmod 0600 "$credentials_file"

runuser -u pdftotex -- env HOME="$home_dir" \
  git config --global credential.helper "store --file=$credentials_file"
runuser -u pdftotex -- env HOME="$home_dir" \
  git config --global credential.useHttpPath false
runuser -u pdftotex -- env HOME="$home_dir" \
  git config --global http.version HTTP/1.1
if [[ -n "${OVERLEAF_GIT_PROXY:-}" ]]; then
  runuser -u pdftotex -- env HOME="$home_dir" \
    git config --global http.proxy "$OVERLEAF_GIT_PROXY"
else
  runuser -u pdftotex -- env HOME="$home_dir" \
    git config --global --unset-all http.proxy 2>/dev/null || true
fi
chmod 0600 "$home_dir/.gitconfig"

DEPLOYMENT_ROOT="$deployment_root" CONFIG_FILE="$config_file" \
OVERLEAF_U1_REMOTE="$OVERLEAF_U1_REMOTE" \
OVERLEAF_U3_REMOTE="$OVERLEAF_U3_REMOTE" \
python3 - <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path

root = Path(os.environ["DEPLOYMENT_ROOT"])
path = Path(os.environ["CONFIG_FILE"])
previous = {}
if path.exists():
    previous = json.loads(path.read_text(encoding="utf-8"))
config = {
    "enabled_after": previous.get("enabled_after", datetime.now(timezone.utc).isoformat()),
    "queue_dir": str(root / "data/workers/queues"),
    "container_data_root": "/data",
    "host_data_root": str(root / "data"),
    "ledger": str(root / "overleaf/publish-ledger.jsonl"),
    "projects": {
        "U1": {
            "remote": os.environ["OVERLEAF_U1_REMOTE"],
            "source_root": str(root / "data/optimized/U1/批次数据"),
            "target_root": "待审核",
            "checkout": str(root / "overleaf/projects/u1"),
        },
        "U3": {
            "remote": os.environ["OVERLEAF_U3_REMOTE"],
            "source_root": str(root / "data/optimized/U3/20260808"),
            "target_root": "待审核",
            "checkout": str(root / "overleaf/projects/u3"),
        },
    },
}
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary.replace(path)
PY
chown pdftotex:pdftotex "$config_file"
chmod 0600 "$config_file"

clone_or_fetch() {
  local remote="$1"
  local checkout="$2"
  if [[ -d "$checkout/.git" ]]; then
    runuser -u pdftotex -- env HOME="$home_dir" git -C "$checkout" remote set-url origin "$remote"
    runuser -u pdftotex -- env HOME="$home_dir" git -C "$checkout" fetch origin
  else
    for attempt in 1 2 3; do
      rm -rf "$checkout"
      if runuser -u pdftotex -- env HOME="$home_dir" \
          git clone --depth=1 "$remote" "$checkout"; then
        return 0
      fi
      if [[ "$attempt" -lt 3 ]]; then
        printf 'clone failed; retrying %s (attempt %s/3)\n' "$remote" "$((attempt + 1))" >&2
        sleep 5
      fi
    done
    return 1
  fi
}

clone_or_fetch "$OVERLEAF_U1_REMOTE" "$projects_dir/u1"
clone_or_fetch "$OVERLEAF_U3_REMOTE" "$projects_dir/u3"

escaped_root="${deployment_root//\\/\\\\}"
escaped_root="${escaped_root//&/\\&}"
escaped_root="${escaped_root//|/\\|}"
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed "s|@PDFTOTEX_ROOT@|$escaped_root|g" \
  "$repo_root/deploy/systemd/pdftotex-overleaf-publish.service" > "$rendered"
install -m 0644 "$rendered" "$unit_dir/pdftotex-overleaf-publish.service"
install -m 0644 "$repo_root/deploy/systemd/pdftotex-overleaf-publish.timer" "$unit_dir/"
systemd-analyze verify \
  "$unit_dir/pdftotex-overleaf-publish.service" \
  "$unit_dir/pdftotex-overleaf-publish.timer"
systemctl daemon-reload
systemctl enable --now pdftotex-overleaf-publish.timer
