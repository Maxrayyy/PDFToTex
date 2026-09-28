#!/usr/bin/env python3
"""Start the next configured batch for each idle PDFToTex unit."""

from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable


ContainerState = tuple[str, int] | None


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def queue_complete(status_path: Path) -> bool:
    try:
        state = read_json(status_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    jobs = state.get("jobs")
    return bool(jobs) and all(
        job.get("status") == "done"
        and (job.get("exit_code") == 0 or job.get("skipped_existing") is True)
        for job in jobs
    )


def docker_container_state(docker: str, name: str) -> ContainerState:
    result = subprocess.run(
        [docker, "inspect", "--format", "{{.State.Status}}|{{.State.ExitCode}}", name],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        error = result.stderr.lower()
        if "no such object" in error or "no such container" in error:
            return None
        raise RuntimeError(f"docker inspect failed for {name}: {result.stderr.strip()}")
    status, exit_code = result.stdout.strip().split("|", 1)
    return status, int(exit_code)


def describe_container(state: ContainerState) -> str:
    if state is None:
        return "missing"
    status, exit_code = state
    return status if status == "running" else f"{status} ({exit_code})"


def copy_queue(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def start_queue(config: dict, queue: dict, source: Path, target: Path) -> None:
    copy_queue(source, target)
    docker = config.get("docker", "/usr/bin/docker")
    command = [
        docker,
        "compose",
        "run",
        "-d",
        "--no-deps",
        "--name",
        queue["container"],
        "-e",
        "VISION_CONCURRENCY=2",
        "-e",
        "RECONCILE_CONCURRENCY=2",
        "-e",
        "RENDER_DPI=240",
        "-e",
        "PIPELINE_PUBLISH_ROOT=/data/optimized",
        "worker",
        "python",
        "-u",
        "/opt/pdftotex/run-sequential-queue.py",
        f"/data/workers/queues/{target.name}",
    ]
    subprocess.run(command, cwd=config["repo_root"], check=True)


def dispatch_unit(
    unit: str,
    queue_names: list[str],
    staged_root: Path,
    active_root: Path,
    inspect_container: Callable[[str], ContainerState],
    launch_queue: Callable[[dict, Path, Path], None],
) -> str:
    for queue_name in queue_names:
        if Path(queue_name).name != queue_name or not queue_name.endswith(".json"):
            raise ValueError(f"invalid {unit} queue name: {queue_name}")
        staged_path = staged_root / queue_name
        if not staged_path.is_file():
            raise FileNotFoundError(f"staged queue does not exist: {staged_path}")
        queue = read_json(staged_path)
        container = queue.get("container")
        if not isinstance(container, str) or not container:
            raise ValueError(f"queue has no container name: {staged_path}")
        active_path = active_root / queue_name
        status_path = active_path.with_suffix(".status.json")

        if status_path.exists():
            if queue_complete(status_path):
                continue
            state = inspect_container(container)
            prefix = "active" if state is not None and state[0] == "running" else "blocked"
            return f"{prefix} {queue_name}: {describe_container(state)}"

        state = inspect_container(container)
        if state is not None:
            prefix = "active" if state[0] == "running" else "blocked"
            return f"{prefix} {queue_name}: {describe_container(state)}"

        launch_queue(queue, staged_path, active_path)
        return f"started {queue_name}"
    return "complete"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = read_json(args.config)
        staged_root = Path(config["staged_root"])
        active_root = Path(config["active_root"])
        units = config["units"]
        if not isinstance(units, dict) or not units:
            raise ValueError("units must be a non-empty object")
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"queue dispatch configuration error: {error}", file=sys.stderr)
        return 1

    args.config.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.config.with_suffix(args.config.suffix + ".lock")
    failed = False
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for unit, queue_names in units.items():
            try:
                if not isinstance(queue_names, list):
                    raise ValueError(f"{unit} queues must be a list")
                inspect = lambda name: docker_container_state(
                    config.get("docker", "/usr/bin/docker"), name
                )
                if args.dry_run:
                    launch = lambda queue, source, target: None
                else:
                    launch = lambda queue, source, target: start_queue(
                        config, queue, source, target
                    )
                outcome = dispatch_unit(
                    unit,
                    queue_names,
                    staged_root,
                    active_root,
                    inspect,
                    launch,
                )
                print(f"{unit}: {outcome}")
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                failed = True
                print(f"{unit}: dispatch failed: {error}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
