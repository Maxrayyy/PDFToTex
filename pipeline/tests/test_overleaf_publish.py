from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "overleaf_publish.py"
SCANNER_SCRIPT = ROOT / "scripts" / "publish_completed_overleaf.py"


def git(*args, cwd=None, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )


def seed_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    git("init", "--bare", str(remote))
    git("symbolic-ref", "HEAD", "refs/heads/master", cwd=remote)
    git("init", "-b", "master", str(seed))
    git("config", "user.name", "Test", cwd=seed)
    git("config", "user.email", "test@example.invalid", cwd=seed)
    (seed / "README.md").write_text("keep\n", encoding="utf-8")
    old_batch = seed / "待审核" / "BATCH-OLD"
    old_batch.mkdir(parents=True)
    (old_batch / "stale.tex").write_text("stale\n", encoding="utf-8")
    git("add", ".", cwd=seed)
    git("commit", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-u", "origin", "master", cwd=seed)
    return remote


def write_config(tmp_path: Path, remote: Path, source_root: Path) -> Path:
    config = {
        "enabled_after": "2026-09-23T00:00:00+00:00",
        "queue_dir": str(tmp_path / "queues"),
        "ledger": str(tmp_path / "ledger.jsonl"),
        "projects": {
            "U1": {
                "remote": str(remote),
                "source_root": str(source_root),
                "target_root": "待审核",
                "checkout": str(tmp_path / "projects" / "u1"),
            }
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def run_publish(config: Path, batch: Path):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--unit",
            "U1",
            "--batch-dir",
            str(batch),
        ],
        capture_output=True,
        text=True,
    )


def checkout_remote(tmp_path: Path, remote: Path) -> Path:
    checkout = tmp_path / "verify"
    git("clone", str(remote), str(checkout))
    return checkout


def test_publish_adds_batch_and_preserves_existing_files(tmp_path):
    remote = seed_remote(tmp_path)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    batch = source_root / "BATCH-001"
    (batch / "assets").mkdir(parents=True)
    (batch / ".pipeline").mkdir()
    (batch / "main.tex").write_text("new\n", encoding="utf-8")
    (batch / "assets" / "stamp.png").write_bytes(b"png")
    (batch / ".pipeline" / "private.log").write_text("secret", encoding="utf-8")
    (batch / "secret.env").write_text("TOKEN=secret", encoding="utf-8")
    config = write_config(tmp_path, remote, source_root)

    result = run_publish(config, batch)

    assert result.returncode == 0, result.stderr
    checkout = checkout_remote(tmp_path, remote)
    assert (checkout / "README.md").read_text(encoding="utf-8") == "keep\n"
    target = checkout / "待审核" / "BATCH-001"
    assert (target / "main.tex").read_text(encoding="utf-8") == "new\n"
    assert (target / "assets" / "stamp.png").read_bytes() == b"png"
    assert (checkout / "待审核" / "BATCH-OLD" / "stale.tex").read_text() == "stale\n"
    assert not (target / ".pipeline").exists()
    assert not (target / "secret.env").exists()
    records = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["unit"] == "U1"
    assert records[0]["batch"] == "BATCH-001"
    assert records[0]["status"] == "pushed"
    assert len(records[0]["commit"]) == 40


def test_publish_refuses_existing_batch_without_overwriting(tmp_path):
    remote = seed_remote(tmp_path)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    batch = source_root / "BATCH-OLD"
    batch.mkdir(parents=True)
    (batch / "stale.tex").write_text("replacement\n", encoding="utf-8")
    config = write_config(tmp_path, remote, source_root)

    result = run_publish(config, batch)

    assert result.returncode != 0
    assert "already exists" in result.stderr
    checkout = checkout_remote(tmp_path, remote)
    assert (checkout / "待审核" / "BATCH-OLD" / "stale.tex").read_text() == "stale\n"
    assert not (tmp_path / "ledger.jsonl").exists()


def test_publish_adds_missing_files_to_existing_batch(tmp_path):
    remote = seed_remote(tmp_path)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    batch = source_root / "BATCH-OLD"
    batch.mkdir(parents=True)
    (batch / "new.tex").write_text("new\n", encoding="utf-8")
    config = write_config(tmp_path, remote, source_root)
    git("clone", str(remote), str(tmp_path / "projects" / "u1"))
    seed = tmp_path / "seed"
    (seed / "待审核" / "BATCH-OLD" / "stale.tex").write_text("human edit\n")
    git("commit", "-am", "human edit", cwd=seed)
    git("push", cwd=seed)

    result = run_publish(config, batch)

    assert result.returncode == 0, result.stderr
    checkout = checkout_remote(tmp_path, remote)
    target = checkout / "待审核" / "BATCH-OLD"
    assert (target / "stale.tex").read_text() == "human edit\n"
    assert (target / "new.tex").read_text() == "new\n"


def test_publish_same_content_is_idempotent(tmp_path):
    remote = seed_remote(tmp_path)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    batch = source_root / "BATCH-001"
    batch.mkdir(parents=True)
    (batch / "main.tex").write_text("new\n", encoding="utf-8")
    config = write_config(tmp_path, remote, source_root)

    first = run_publish(config, batch)
    commit_after_first = git("rev-parse", "master", cwd=remote).stdout.strip()
    second = run_publish(config, batch)
    commit_after_second = git("rev-parse", "master", cwd=remote).stdout.strip()

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert commit_after_first == commit_after_second
    assert "already published" in second.stdout
    assert len((tmp_path / "ledger.jsonl").read_text().splitlines()) == 1


def test_failed_push_does_not_write_success_ledger(tmp_path):
    remote = seed_remote(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    batch = source_root / "BATCH-001"
    batch.mkdir(parents=True)
    (batch / "main.tex").write_text("new\n", encoding="utf-8")
    config = write_config(tmp_path, remote, source_root)

    result = run_publish(config, batch)

    assert result.returncode != 0
    ledger = tmp_path / "ledger.jsonl"
    assert not ledger.exists() or not ledger.read_text().strip()

    hook.unlink()
    retried = run_publish(config, batch)
    assert retried.returncode == 0, retried.stderr
    assert len(ledger.read_text().splitlines()) == 1


def test_publish_rejects_directory_outside_configured_source_root(tmp_path):
    remote = seed_remote(tmp_path)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    source_root.mkdir(parents=True)
    outside = tmp_path / "other" / "BATCH-001"
    outside.mkdir(parents=True)
    (outside / "main.tex").write_text("new\n", encoding="utf-8")
    config = write_config(tmp_path, remote, source_root)

    result = run_publish(config, outside)

    assert result.returncode != 0
    assert "direct child" in result.stderr


def test_publish_rejects_target_root_that_escapes_checkout(tmp_path):
    remote = seed_remote(tmp_path)
    source_root = tmp_path / "optimized" / "U1" / "批次数据"
    batch = source_root / "BATCH-001"
    batch.mkdir(parents=True)
    (batch / "main.tex").write_text("new\n", encoding="utf-8")
    config_path = write_config(tmp_path, remote, source_root)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["projects"]["U1"]["target_root"] = "../../escaped"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_publish(config_path, batch)

    assert result.returncode != 0
    assert "target root must remain inside" in result.stderr
    assert not (tmp_path / "escaped" / "BATCH-001").exists()


def load_scanner():
    spec = importlib.util.spec_from_file_location("publish_completed_overleaf", SCANNER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_publisher():
    spec = importlib.util.spec_from_file_location("overleaf_publish", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_installation_preserves_existing_paths_and_publication_start(tmp_path):
    publisher = load_publisher()
    template = json.loads((ROOT / "deploy/overleaf.example.json").read_text())
    previous = {
        "enabled_after": "2026-09-23T00:00:00+00:00",
        "ledger": "/custom/ledger.jsonl",
        "projects": {"U3": {"target_root": "custom/review", "checkout": "/custom/u3"}},
    }
    result = publisher.installation_config(template, previous, tmp_path, {"U1": "remote1", "U3": "remote3"})
    assert result["enabled_after"] == previous["enabled_after"]
    assert result["ledger"] == "/custom/ledger.jsonl"
    assert result["projects"]["U3"]["target_root"] == "custom/review"
    assert result["projects"]["U3"]["checkout"] == "/custom/u3"
    assert result["projects"]["U3"]["remote"] == "remote3"
    assert result["projects"]["U1"]["source_root"] == str(tmp_path / "data/optimized/U1/批次数据")
    assert template["projects"]["U3"]["target_root"] == "待审核"


def test_publish_rebases_and_retries_when_remote_advances(tmp_path, monkeypatch):
    publisher = load_publisher()
    source_root = tmp_path / "optimized" / "U3" / "20260808"
    batch = source_root / "BATCH-U3"
    batch.mkdir(parents=True)
    (batch / "main.tex").write_text("content\n", encoding="utf-8")
    checkout = tmp_path / "projects" / "u3"
    checkout.mkdir(parents=True)
    config = {
        "ledger": str(tmp_path / "ledger.jsonl"),
        "projects": {
            "U3": {
                "source_root": str(source_root),
                "target_root": "待审核",
                "checkout": str(checkout),
            }
        },
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    calls = []
    pushes = 0

    monkeypatch.setattr(publisher, "ensure_checkout", lambda project: checkout)

    def fake_run_git(repository, *args):
        nonlocal pushes
        calls.append(args)
        if args[:3] == ("status", "--porcelain", "--"):
            return "A  batch"
        if args == ("push", "origin", "HEAD"):
            pushes += 1
            if pushes == 1:
                raise RuntimeError("[rejected] HEAD -> main (fetch first)")
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        return ""

    monkeypatch.setattr(publisher, "run_git", fake_run_git)

    result = publisher.publish_batch(config_path, "U3", batch)

    assert result["status"] == "pushed"
    assert pushes == 2
    assert ("pull", "--rebase") in calls
    assert not any("--force" in argument for call in calls for argument in call)


def scanner_config(tmp_path: Path) -> dict:
    host_data = tmp_path / "data"
    return {
        "enabled_after": "2026-09-23T12:00:00+00:00",
        "queue_dir": str(host_data / "workers" / "queues"),
        "container_data_root": "/data",
        "host_data_root": str(host_data),
        "projects": {
            "U1": {
                "source_root": str(host_data / "optimized" / "U1" / "批次数据")
            },
            "U2": {"source_root": str(host_data / "optimized" / "U2" / "批次")},
            "U3": {
                "source_root": str(host_data / "optimized" / "U3" / "20260808")
            },
        },
    }


def write_queue_status(path: Path, *, finished_at: str, jobs: list[dict], status=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"finished_at": finished_at, "jobs": jobs}
    if status:
        payload["status"] = status
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_scanner_discovers_successful_batches_after_enable_time(tmp_path):
    scanner = load_scanner()
    config = scanner_config(tmp_path)
    queue_dir = Path(config["queue_dir"])
    write_queue_status(
        queue_dir / "mixed.status.json",
        finished_at="2026-09-23T12:01:00+00:00",
        jobs=[
            {
                "status": "done",
                "exit_code": 0,
                "output_tex": "/data/optimized/U1/批次数据/BATCH-U1/a.tex",
            },
            {
                "status": "done",
                "exit_code": 0,
                "output_tex": "/data/optimized/U1/批次数据/BATCH-U1/b.tex",
            },
            {
                "status": "done",
                "exit_code": 0,
                "output_tex": "/data/optimized/U3/20260808/BATCH-U3/c.tex",
            },
        ],
    )

    candidates = scanner.discover_completed_batches(config)

    assert [(item.unit, item.batch_dir.name) for item in candidates] == [
        ("U1", "BATCH-U1"),
        ("U3", "BATCH-U3"),
    ]
    assert all(item.queue_status.name == "mixed.status.json" for item in candidates)


def test_scanner_discovers_batch_with_existing_published_outputs(tmp_path):
    scanner = load_scanner()
    config = scanner_config(tmp_path)
    queue_dir = Path(config["queue_dir"])
    write_queue_status(
        queue_dir / "resumed.status.json",
        finished_at="2026-09-23T12:01:00+00:00",
        jobs=[
            {
                "status": "done",
                "skipped_existing": True,
                "output_tex": "/data/optimized/U3/20260808/BATCH-U3/a.tex",
            },
            {
                "status": "done",
                "exit_code": 0,
                "output_tex": "/data/optimized/U3/20260808/BATCH-U3/b.tex",
            },
        ],
    )

    candidates = scanner.discover_completed_batches(config)

    assert [(item.unit, item.batch_dir.name) for item in candidates] == [
        ("U3", "BATCH-U3")
    ]


def test_scanner_ignores_old_incomplete_and_paused_queues(tmp_path):
    scanner = load_scanner()
    config = scanner_config(tmp_path)
    queue_dir = Path(config["queue_dir"])
    done_job = {
        "status": "done",
        "exit_code": 0,
        "output_tex": "/data/optimized/U2/批次/BATCH-U2/a.tex",
    }
    write_queue_status(
        queue_dir / "old.status.json",
        finished_at="2026-09-23T11:59:59+00:00",
        jobs=[done_job],
    )
    write_queue_status(
        queue_dir / "failed.status.json",
        finished_at="2026-09-23T12:02:00+00:00",
        jobs=[{**done_job, "status": "failed", "exit_code": 1}],
        status="paused",
    )
    write_queue_status(
        queue_dir / "partial.status.json",
        finished_at="2026-09-23T12:03:00+00:00",
        jobs=[done_job, {**done_job, "status": "pending"}],
    )

    assert scanner.discover_completed_batches(config) == []


def test_scanner_rejects_successful_output_outside_project_roots(tmp_path):
    scanner = load_scanner()
    config = scanner_config(tmp_path)
    queue_dir = Path(config["queue_dir"])
    write_queue_status(
        queue_dir / "unknown.status.json",
        finished_at="2026-09-23T12:01:00+00:00",
        jobs=[
            {
                "status": "done",
                "exit_code": 0,
                "output_tex": "/data/optimized/UNKNOWN/BATCH/a.tex",
            }
        ],
    )

    try:
        scanner.discover_completed_batches(config)
    except ValueError as error:
        assert "does not match an Overleaf project" in str(error)
    else:
        raise AssertionError("unknown completed output must not be silently ignored")
