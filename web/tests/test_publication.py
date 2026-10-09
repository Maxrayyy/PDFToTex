import json
from pathlib import Path
import subprocess
from web.backend.publication import execute, request_publication
from web.backend.store import Store, DEFAULTS


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *map(str, args)], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_add_only_publisher_preserves_collaborator_edits(tmp_path):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    git("init", "--bare", remote)
    git("clone", remote, seed)
    git("config", "user.email", "test@example.invalid", cwd=seed)
    git("config", "user.name", "Test", cwd=seed)
    target = seed / "待审核" / "BATCH"
    target.mkdir(parents=True)
    (target / "human.tex").write_text("original collaborator content")
    git("add", ".", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("push", "origin", "HEAD", cwd=seed)
    config_path = tmp_path / "overleaf.json"
    config_path.write_text(
        json.dumps(
            {
                "ledger": str(tmp_path / "ledger.jsonl"),
                "projects": {
                    "U1": {
                        "source_root": str(tmp_path),
                        "checkout": str(checkout),
                        "remote": str(remote),
                        "target_root": "待审核",
                    }
                },
            }
        )
    )
    store = Store(
        {
            **DEFAULTS,
            "data_root": str(tmp_path / "web"),
            "min_free_bytes": 0,
            "overleaf_config": str(config_path),
        }
    )
    source = store.root / "example.tex"
    source.write_text("new generated content")
    job = {
        "id": "job1",
        "status": "succeeded",
        "attempt": 1,
        "name": "example.pdf",
        "batch": "BATCH",
        "unit": "U1",
        "artifacts": [
            {"id": "artifact1", "kind": "tex", "attempt": 1, "path": "example.tex"}
        ],
    }
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?)",
            (job["id"], job["status"], 1, json.dumps(job)),
        )
    request = request_publication(store, job, "preflight")
    plan = execute(store, request)
    assert plan["status"] == "ready"
    assert not (checkout / "待审核" / "BATCH" / "example.tex").exists()
    # Human edits the remote between Web preflight and publication.
    (target / "human.tex").write_text("new collaborator edit")
    git("add", ".", cwd=seed)
    git("commit", "-m", "human update", cwd=seed)
    git("push", "origin", "HEAD", cwd=seed)
    result = execute(store, request_publication(store, job, "publish"))
    assert result["status"] == "pushed"
    assert (
        checkout / "待审核" / "BATCH" / "human.tex"
    ).read_text() == "new collaborator edit"
    assert (
        checkout / "待审核" / "BATCH" / "example.tex"
    ).read_text() == "new generated content"
    source.write_text("conflicting content")
    assert (
        execute(store, request_publication(store, job, "preflight"))["status"]
        == "conflict"
    )
    import pytest

    with pytest.raises(FileExistsError):
        execute(store, request_publication(store, job, "publish"))
    assert (
        checkout / "待审核" / "BATCH" / "example.tex"
    ).read_text() == "new generated content"
    assert json.loads(config_path.read_text())["projects"]["U1"]["source_root"] == str(
        tmp_path
    )
