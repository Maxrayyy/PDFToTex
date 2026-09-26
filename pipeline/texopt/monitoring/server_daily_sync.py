"""Merge an authoritative server ledger into the local daily report."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

if __package__:
    from .daily_stats import poll, read_json
    from .worker_watch import atomic_write
else:
    from daily_stats import poll, read_json
    from worker_watch import atomic_write


def _validate(records: list[dict], label: str) -> None:
    seen = set()
    for line, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"{label} line {line}: expected object")
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"{label} line {line}: missing id")
        if identifier in seen:
            raise ValueError(f"{label} line {line}: duplicate id {identifier}")
        seen.add(identifier)
        value = record.get("date")
        if not isinstance(value, str):
            raise ValueError(f"{label} line {line}: missing date")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{label} line {line}: invalid date {value}") from error


def _read_ledger(path: Path) -> list[dict]:
    records = []
    for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            records.append(json.loads(text))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} line {line}: invalid JSON") from error
    _validate(records, str(path))
    return records


def merge_records(local: list[dict], remote: list[dict], start_date: str) -> list[dict]:
    date.fromisoformat(start_date)
    _validate(local, "local ledger")
    _validate(remote, "server ledger")
    merged = [record for record in local if record["date"] < start_date]
    merged.extend(record for record in remote if record["date"] >= start_date)
    return sorted(
        merged,
        key=lambda record: (
            record["date"],
            float(record.get("completed_timestamp", 0)),
            record["id"],
        ),
    )


def merge_ledger(local_path: Path, remote_path: Path, start_date: str) -> list[dict]:
    local_path = Path(local_path)
    remote_path = Path(remote_path)
    local = _read_ledger(local_path) if local_path.exists() else []
    remote = _read_ledger(remote_path)
    merged = merge_records(local, remote, start_date)
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in merged)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(local_path, text)
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--remote-ledger", required=True, type=Path)
    parser.add_argument("--start-date", required=True)
    args = parser.parse_args()

    config = read_json(args.config)
    output = Path(config["output_dir"])
    records = merge_ledger(
        output / "completions.jsonl", args.remote_ledger, args.start_date
    )
    render_config = dict(config)
    render_config["scan_roots"] = []
    snapshot = poll(render_config)
    print(json.dumps({
        "start_date": args.start_date,
        "merged_completions": len(records),
        "days": len(snapshot["days"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
