#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  printf 'run this installer as root\n' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir=/etc/systemd/system

install -m 0644 "$repo_root"/deploy/systemd/pdftotex-*.service "$unit_dir/"
install -m 0644 "$repo_root"/deploy/systemd/pdftotex-*.timer "$unit_dir/"
systemd-analyze verify \
  "$unit_dir"/pdftotex-*.service \
  "$unit_dir"/pdftotex-*.timer
systemctl daemon-reload
systemctl enable --now \
  pdftotex-realtime-monitor.timer \
  pdftotex-daily-stats.timer
