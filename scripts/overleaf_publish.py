#!/usr/bin/env python3
"""Publish one completed batch directory to an existing Overleaf Git project."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


EXCLUDED_DIRS = {".cache", ".pipeline", ".state"}
EXCLUDED_SUFFIXES = {
    ".db", ".log", ".sqlite", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal"
}
PUSH_RETRY_ATTEMPTS = 6


def included_files(batch: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(batch.rglob("*")):
        relative = path.relative_to(batch)
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {relative}")
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.is_dir():
            continue
        if path.name == ".env" or path.suffix == ".env":
            continue
        if any(path.name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
            continue
        files.append(path)
    return files


def batch_digest(batch: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(batch).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def run_git(checkout: Path | None, *args: str) -> str:
    command = ["git"]
    if checkout is not None:
        command.extend(["-C", str(checkout)])
    command.extend(args)
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return result.stdout.strip()


def push_with_rebase_retry(checkout: Path) -> None:
    for attempt in range(PUSH_RETRY_ATTEMPTS):
        try:
            run_git(checkout, "push", "origin", "HEAD")
            return
        except RuntimeError as error:
            message = str(error).lower()
            remote_advanced = "fetch first" in message or "non-fast-forward" in message
            if not remote_advanced or attempt == PUSH_RETRY_ATTEMPTS - 1:
                raise
            run_git(checkout, "pull", "--rebase")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("projects"), dict):
        raise ValueError("config projects must be an object")
    if not config.get("ledger"):
        raise ValueError("config ledger is required")
    return config


def installation_config(template: dict, previous: dict, root: Path, remotes: dict) -> dict:
    config = {**template, **previous}
    config.setdefault("enabled_after", datetime.now(timezone.utc).isoformat())
    for key in ("queue_dir", "host_data_root", "ledger"):
        config[key] = str(root / config[key])
    config["projects"] = {}
    for unit, remote in remotes.items():
        project = {**template["projects"][unit], **previous.get("projects", {}).get(unit, {})}
        project["remote"] = remote
        for key in ("source_root", "checkout"):
            project[key] = str(root / project[key])
        config["projects"][unit] = project
    return config


def validate_batch(project: dict, batch_dir: Path) -> tuple[Path, list[Path]]:
    source_root = Path(project["source_root"]).resolve(strict=True)
    batch = batch_dir.resolve(strict=True)
    if not batch.is_dir() or batch.parent != source_root:
        raise ValueError("batch directory must be a direct child of the configured source root")
    files = included_files(batch)
    if not any(path.suffix.lower() == ".tex" for path in files):
        raise ValueError("batch does not contain a TEX file")
    return batch, files


def ledger_contains(ledger: Path, unit: str, batch: str, digest: str) -> bool:
    if not ledger.exists():
        return False
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (record.get("status") == "pushed" and record.get("unit") == unit
                and record.get("batch") == batch and record.get("digest") == digest):
            return True
    return False


def append_ledger(ledger: Path, record: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_checkout(project: dict) -> Path:
    checkout = Path(project["checkout"])
    remote = str(project["remote"])
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").is_dir():
        if checkout.exists() and any(checkout.iterdir()):
            raise ValueError(f"checkout exists and is not a Git worktree: {checkout}")
        checkout.parent.mkdir(parents=True, exist_ok=True)
        run_git(None, "clone", "--depth=1", remote, str(checkout))
    else:
        run_git(checkout, "remote", "set-url", "origin", remote)
    run_git(checkout, "config", "user.name", project.get("git_name", "PDFToTex Publisher"))
    run_git(
        checkout,
        "config",
        "user.email",
        project.get("git_email", "pdftotex@localhost"),
    )
    if run_git(checkout, "status", "--porcelain"):
        raise RuntimeError(f"Overleaf checkout has uncommitted changes: {checkout}")
    run_git(checkout, "pull", "--rebase")
    return checkout


def sync_batch(batch: Path, files: list[Path], destination: Path) -> None:
    additions = []
    for source in files:
        target = destination / source.relative_to(batch)
        for parent in (target, *target.parents):
            if parent.is_symlink():
                raise ValueError(f"symbolic links are not allowed in destination: {parent}")
            if parent != target and parent.exists() and not parent.is_dir():
                raise FileExistsError(f"Overleaf parent path is not a directory: {parent}")
            if parent == destination:
                break
        if target.exists():
            if not target.is_file() or target.read_bytes() != source.read_bytes():
                raise FileExistsError(f"Overleaf file already exists with different content: {target}")
        else:
            additions.append((source, target))
    # Check every collision before changing the checkout, so conflicts leave it clean.
    for source, target in additions:
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)


def publish_batch(config_path: Path, unit: str, batch_dir: Path,
                  queue_status: Path | None = None) -> dict:
    config = load_config(config_path)
    try:
        project = config["projects"][unit]
    except KeyError as error:
        raise ValueError(f"unknown Overleaf unit: {unit}") from error
    batch, files = validate_batch(project, batch_dir)
    digest = batch_digest(batch, files)
    ledger = Path(config["ledger"])
    lock_path = Path(project["checkout"]).parent / f".{unit.lower()}.publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if ledger_contains(ledger, unit, batch.name, digest):
            return {"status": "already published", "unit": unit, "batch": batch.name}

        checkout = ensure_checkout(project)
        target = (checkout / Path(project["target_root"]) / batch.name).resolve()
        try:
            target.relative_to(checkout.resolve())
        except ValueError as error:
            raise ValueError("target root must remain inside the checkout") from error
        sync_batch(batch, files, target)
        relative_target = target.relative_to(checkout).as_posix()
        run_git(checkout, "add", "-A", "--", relative_target)
        changed = bool(run_git(checkout, "status", "--porcelain", "--", relative_target))
        if changed:
            run_git(checkout, "commit", "-m", f"上传 {unit} 批次 {batch.name}")
        push_with_rebase_retry(checkout)
        commit = run_git(checkout, "rev-parse", "HEAD")
        record = {
            "status": "pushed",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "unit": unit,
            "batch": batch.name,
            "digest": digest,
            "commit": commit,
            "target": relative_target,
        }
        if queue_status is not None:
            record["queue_status"] = str(queue_status)
        append_ledger(ledger, record)
        return record


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=Path)
    result.add_argument("--unit", required=True, choices=("U1", "U2", "U3"))
    result.add_argument("--batch-dir", required=True, type=Path)
    result.add_argument("--queue-status", type=Path)
    return result


def main() -> int:
    argument_parser = parser()
    args = argument_parser.parse_args()
    try:
        result = publish_batch(args.config, args.unit, args.batch_dir, args.queue_status)
    except (OSError, RuntimeError, ValueError) as error:
        argument_parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
