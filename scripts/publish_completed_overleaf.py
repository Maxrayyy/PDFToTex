#!/usr/bin/env python3
"""Publish batches from queue status files completed after automatic publishing began."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import NamedTuple

SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from overleaf_publish import load_config, publish_batch


class Candidate(NamedTuple):
    unit: str
    batch_dir: Path
    queue_status: Path


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value}")
    return parsed


def host_output_path(config: dict, output: str) -> Path:
    path = Path(output)
    container_root = Path(config.get("container_data_root", "/data"))
    try:
        relative = path.relative_to(container_root)
    except ValueError:
        return path
    return Path(config["host_data_root"]) / relative


def classify_batch(config: dict, output: Path) -> tuple[str, Path]:
    resolved = output.resolve()
    for unit, project in sorted(config["projects"].items()):
        source_root = Path(project["source_root"]).resolve()
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            continue
        if len(relative.parts) < 2:
            break
        return unit, source_root / relative.parts[0]
    raise ValueError(f"completed output does not match an Overleaf project: {output}")


def discover_completed_batches(config: dict) -> list[Candidate]:
    enabled_after = parse_time(config["enabled_after"])
    queue_dir = Path(config["queue_dir"])
    if not queue_dir.exists():
        return []
    discovered: dict[tuple[str, Path], Candidate] = {}
    for status_path in sorted(queue_dir.glob("*.status.json")):
        state = json.loads(status_path.read_text(encoding="utf-8"))
        finished_at = state.get("finished_at")
        jobs = state.get("jobs")
        if not finished_at or not isinstance(jobs, list) or not jobs:
            continue
        if parse_time(finished_at) < enabled_after:
            continue
        if state.get("status") == "paused":
            continue
        if not all(
            job.get("status") == "done"
            and (job.get("exit_code") == 0 or job.get("skipped_existing") is True)
            for job in jobs
        ):
            continue
        for job in jobs:
            output = job.get("output_tex")
            if not output:
                raise ValueError(f"completed job has no output_tex: {status_path}")
            unit, batch_dir = classify_batch(config, host_output_path(config, output))
            key = (unit, batch_dir)
            discovered[key] = Candidate(unit, batch_dir, status_path)
    return sorted(discovered.values(), key=lambda item: (item.unit, str(item.batch_dir)))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        config = load_config(args.config)
        candidates = discover_completed_batches(config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Overleaf discovery failed: {error}", file=sys.stderr)
        return 1

    failed = False
    for candidate in candidates:
        try:
            result = publish_batch(
                args.config,
                candidate.unit,
                candidate.batch_dir,
                candidate.queue_status,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        except (OSError, RuntimeError, ValueError) as error:
            failed = True
            print(
                f"Overleaf publish failed for {candidate.unit}/{candidate.batch_dir.name}: {error}",
                file=sys.stderr,
            )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
