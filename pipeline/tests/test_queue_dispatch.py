import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "queue_dispatch.py"


def load_module():
    spec = importlib.util.spec_from_file_location("queue_dispatch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_queue(path: Path, container: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"container": container, "sources": ["/input/a.pdf"]}))


def write_status(path: Path, status: str, exit_code=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    job = {"status": status}
    if exit_code is not None:
        job["exit_code"] = exit_code
    path.write_text(json.dumps({"jobs": [job]}))


def test_dispatch_skips_completed_queue_and_starts_next(tmp_path):
    module = load_module()
    staged = tmp_path / "staged"
    active = tmp_path / "active"
    write_queue(staged / "first.json", "pdftotex-u1-first")
    write_queue(staged / "second.json", "pdftotex-u1-second")
    write_status(active / "first.status.json", "done", 0)
    started = []

    result = module.dispatch_unit(
        "U1",
        ["first.json", "second.json"],
        staged,
        active,
        lambda name: None,
        lambda queue, source, target: started.append((queue["container"], source, target)),
    )

    assert result == "started second.json"
    assert started == [("pdftotex-u1-second", staged / "second.json", active / "second.json")]


def test_dispatch_does_not_pass_failed_queue(tmp_path):
    module = load_module()
    staged = tmp_path / "staged"
    active = tmp_path / "active"
    write_queue(staged / "failed.json", "pdftotex-u3-failed")
    write_queue(staged / "later.json", "pdftotex-u3-later")
    write_status(active / "failed.status.json", "failed", 1)
    started = []

    result = module.dispatch_unit(
        "U3",
        ["failed.json", "later.json"],
        staged,
        active,
        lambda name: ("exited", 1),
        lambda *args: started.append(args),
    )

    assert result == "blocked failed.json: exited (1)"
    assert started == []


def test_dispatch_reports_incomplete_running_queue_as_active(tmp_path):
    module = load_module()
    staged = tmp_path / "staged"
    active = tmp_path / "active"
    write_queue(staged / "current.json", "pdftotex-u3-current")
    write_status(active / "current.status.json", "running")

    result = module.dispatch_unit(
        "U3",
        ["current.json"],
        staged,
        active,
        lambda name: ("running", 0),
        lambda *args: None,
    )

    assert result == "active current.json: running"


def test_dispatch_waits_for_running_container_before_status_exists(tmp_path):
    module = load_module()
    staged = tmp_path / "staged"
    active = tmp_path / "active"
    write_queue(staged / "current.json", "pdftotex-u1-current")
    started = []

    result = module.dispatch_unit(
        "U1",
        ["current.json"],
        staged,
        active,
        lambda name: ("running", 0),
        lambda *args: started.append(args),
    )

    assert result == "active current.json: running"
    assert started == []


def test_empty_or_incomplete_status_is_not_complete(tmp_path):
    module = load_module()
    status = tmp_path / "queue.status.json"
    status.write_text('{"jobs": []}')
    assert not module.queue_complete(status)
    write_status(status, "done", 0)
    assert module.queue_complete(status)
    write_status(status, "done")
    assert not module.queue_complete(status)


def test_docker_missing_container_error_is_case_insensitive(monkeypatch):
    module = load_module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="error: no such object: pdftotex-u3-next\n",
        ),
    )

    assert module.docker_container_state("/usr/bin/docker", "pdftotex-u3-next") is None
