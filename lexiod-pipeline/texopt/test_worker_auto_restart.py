from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

import pytest

from . import worker_watch as watch


def setup_outage(tmp_path, monkeypatch, error="APIConnectionError"):
    work = tmp_path / "work"
    log = work / ".pipeline" / "sample" / "recognize.process.log"
    log.parent.mkdir(parents=True)
    log.write_text("recognize started\n"
        f"lexoid.core.request_errors.ModelUnavailableError: Model service unavailable at page 47: {error}; "
        "progress retained, resume after service recovery\n")
    os.utime(log, (1400, 1400))
    (work / "manifest.json").write_text(json.dumps({"files": [{
        "source": "sample.pdf", "status": "paused", "stages": [
            {"stage": "recognize", "status": "failed"}]}]}))
    config = {"docker": "docker", "output_dir": str(tmp_path / "monitor"),
        "auto_restart": {"enabled": True, "cooldown_seconds": 600, "max_attempts": 3},
        "containers": [{"name": "worker-a", "stem": "sample", "pages": 69,
            "work_root": str(work), "output_tex": str(tmp_path / "out.tex")} ]}
    iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    info = {"Id": "container-id", "State": {"Status": "exited", "ExitCode": 1,
        "StartedAt": iso(1000), "FinishedAt": iso(1400), "OOMKilled": False}}
    clock = [2001]
    calls = []
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    monkeypatch.setattr(watch, "inspect_container", lambda *_: info)

    def start(argv, **kwargs):
        # The budget must survive a monitor crash immediately after docker start.
        state = json.loads((Path(config["output_dir"]) / "state.json").read_text())
        assert state["worker-a"]["auto_restart"]["attempts"] == len(calls) + 1
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "container-id", "")

    monkeypatch.setattr(watch.subprocess, "run", start)
    return config, info, clock, calls, log


def snapshot(config):
    return json.loads((Path(config["output_dir"]) / "latest.json").read_text())


@pytest.mark.parametrize("error", ["APIConnectionError", "APITimeoutError", "InternalServerError",
                                  "InternalError", "RateLimitError"])
def test_network_pause_restarts_original_container_and_keeps_monitor_active(tmp_path, monkeypatch, error):
    config, _, _, calls, _ = setup_outage(tmp_path, monkeypatch, error)
    assert watch.poll(config) is False
    assert calls == [["docker", "start", "container-id"]]
    item = snapshot(config)["containers"][0]
    assert item["auto_restart"]["status"] == "started"
    assert item["auto_restart"]["error_type"] == error
    assert item["auto_restart"]["attempts"] == 1
    assert "自动重启" in (Path(config["output_dir"]) / "latest.md").read_text()


@pytest.mark.parametrize("error", ["AuthenticationError", "PermissionDeniedError",
                                  "BadRequestError", "ValueError"])
def test_non_connection_errors_do_not_restart(tmp_path, monkeypatch, error):
    config, _, _, calls, _ = setup_outage(tmp_path, monkeypatch, error)
    assert watch.poll(config) is True
    assert calls == []


@pytest.mark.parametrize("change", ["disabled", "success", "oom", "running", "stale", "not_paused"])
def test_only_fresh_network_failure_of_paused_document_can_restart(tmp_path, monkeypatch, change):
    config, info, _, calls, log = setup_outage(tmp_path, monkeypatch)
    if change == "disabled":
        config.pop("auto_restart")
    elif change == "success":
        info["State"]["ExitCode"] = 0
    elif change == "oom":
        info["State"]["OOMKilled"] = True
    elif change == "running":
        info["State"]["Status"] = "running"
    elif change == "stale":
        os.utime(log, (900, 900))
    else:
        (tmp_path / "work" / "manifest.json").write_text('{"files": []}')
    watch.poll(config)
    assert calls == []


def test_cooldown_and_persistent_per_document_restart_limit(tmp_path, monkeypatch):
    config, _, clock, calls, _ = setup_outage(tmp_path, monkeypatch)
    clock[0] = 1500
    assert watch.poll(config) is False
    assert snapshot(config)["containers"][0]["auto_restart"]["status"] == "waiting"
    assert calls == []
    for now in (2001, 2602, 3203):
        clock[0] = now
        assert watch.poll(config) is False
        if len(calls) < 3:
            assert watch.poll(config) is False
    assert len(calls) == 3
    clock[0] = 3804
    assert watch.poll(config) is True
    assert snapshot(config)["containers"][0]["auto_restart"]["status"] == "limit_reached"
    assert len(calls) == 3


def test_failed_docker_start_is_logged_and_waits_before_retry(tmp_path, monkeypatch):
    config, _, _, _, _ = setup_outage(tmp_path, monkeypatch)
    calls = []

    def unavailable(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(watch.subprocess, "run", unavailable)
    assert watch.poll(config) is False
    assert snapshot(config)["containers"][0]["auto_restart"]["status"] == "start_failed"
    assert watch.poll(config) is False
    assert len(calls) == 1


@pytest.mark.parametrize("status, expected", [(503, True), (429, True), (408, True),
                                             (401, False), (403, False), (400, False)])
def test_http_status_classifies_api_status_errors(tmp_path, monkeypatch, status, expected):
    config, _, _, calls, log = setup_outage(tmp_path, monkeypatch, "APIStatusError")
    lines = log.read_text().splitlines()
    event = {"event": "model_service_unavailable", "page": 47,
             "error_type": "APIStatusError", "http_status": status, "action": "pause_document"}
    log.write_text(lines[0] + "\n[LLM_CALL] " + json.dumps(event) + "\n" + lines[-1] + "\n")
    watch.poll(config)
    assert bool(calls) is expected


def test_prior_network_error_does_not_restart_later_compile_failure(tmp_path, monkeypatch):
    config, _, _, calls, log = setup_outage(tmp_path, monkeypatch)
    with log.open("a") as stream:
        stream.write("recognize started\nValueError: malformed TeX\n")
    assert watch.poll(config) is True
    assert calls == []


def test_next_document_gets_its_own_restart_budget(tmp_path, monkeypatch):
    config, _, _, calls, _ = setup_outage(tmp_path, monkeypatch)
    output = Path(config["output_dir"])
    output.mkdir()
    (output / "state.json").write_text(json.dumps({"worker-a": {"auto_restart": {
        "document": "previous-pdf", "attempts": 3, "last_attempt_at": 2000}}}))
    assert watch.poll(config) is False
    assert len(calls) == 1
    assert snapshot(config)["containers"][0]["auto_restart"]["attempts"] == 1
