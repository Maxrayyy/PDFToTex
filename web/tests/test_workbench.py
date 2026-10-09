import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import pytest
from fastapi.testclient import TestClient
from web.backend.api import create_app
from web.backend.store import DEFAULTS, Store
from web.backend.worker import Worker
from web.backend.observations import snapshot


@pytest.fixture
def app(tmp_path):
    return create_app(
        {
            **DEFAULTS,
            "data_root": str(tmp_path),
            "config_file": str(tmp_path / "config.json"),
            "min_free_bytes": 0,
        }
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def upload(
    client,
    path="main.tex",
    content=b"\\documentclass{article}\\begin{document}Hello\\end{document}",
):
    response = client.post(
        "/api/uploads",
        json={
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert response.status_code == 200, response.text
    u = response.json()
    assert client.put("/api/uploads/" + u["id"], content=content).status_code == 200
    assert client.post("/api/uploads/" + u["id"] + "/finish").status_code == 200
    return u


def job(client, request_id="test"):
    u = upload(client)
    r = client.post(
        "/api/jobs",
        json={
            "kind": "tex_compile",
            "uploads": [u["id"]],
            "main_file": "main.tex",
            "request_id": request_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()[0]


def test_upload_resume_and_duplicate_chunks(client):
    u = client.post("/api/uploads", json={"path": "nested/main.tex", "size": 6}).json()
    url = "/api/uploads/" + u["id"]
    assert client.put(url, content=b"abc").json()["offset"] == 3
    assert client.put(url, content=b"abc").json()["offset"] == 3
    assert client.put(url + "?offset=0", content=b"xyz").status_code == 400
    assert client.put(url + "?offset=4", content=b"f").status_code == 400
    assert client.put(url + "?offset=3", content=b"def").status_code == 200
    assert client.post(url + "/finish").status_code == 200
    assert client.put(url, content=b"new").status_code == 400


@pytest.mark.parametrize(
    "path", ["../a.tex", "/etc/passwd", "a/../../b", "a\\b", "a//b", "a/./b", "C:/x"]
)
def test_path_traversal(client, path):
    assert (
        client.post("/api/uploads", json={"path": path, "size": 1}).status_code == 400
    )


def test_invalid_pdf_and_hash(client):
    u = client.post("/api/uploads", json={"path": "fake.PDF", "size": 3}).json()
    client.put("/api/uploads/" + u["id"], content=b"abc")
    assert client.post("/api/uploads/" + u["id"] + "/finish").status_code == 400
    u = client.post(
        "/api/uploads", json={"path": "main.tex", "size": 3, "sha256": "0" * 64}
    ).json()
    client.put("/api/uploads/" + u["id"], content=b"abc")
    assert client.post("/api/uploads/" + u["id"] + "/finish").status_code == 400


def test_persistent_single_claim_and_idempotency(client, app):
    u = upload(client)
    data = {
        "kind": "tex_compile",
        "uploads": [u["id"]],
        "main_file": "main.tex",
        "request_id": "same",
    }
    a = client.post("/api/jobs", json=data).json()
    assert client.post("/api/jobs", json=data).json() == a
    job(client, "second")
    s = app.state.store
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        claims = list(pool.map(lambda _: Store(s.config).claim(), range(4)))
    assert len([c for c in claims if c]) == 1
    restored = Store(s.config)
    assert len(restored.jobs()) == 2 and restored.jobs()[0]["status"] == "starting"
    assert restored.claim() is None


def test_pause_and_stale_commands(client, app):
    j = job(client)
    assert client.post("/api/queue", json={"paused": True}).status_code == 200
    assert app.state.store.claim() is None
    assert (
        client.post(
            "/api/jobs/" + j["id"] + "/command",
            json={"action": "cancel", "version": j["version"]},
        ).json()["status"]
        == "cancelled"
    )
    assert (
        client.post(
            "/api/jobs/" + j["id"] + "/command",
            json={"action": "retry", "version": j["version"]},
        ).status_code
        == 400
    )


def test_keys_server_only_and_no_cross_origin_write(client, app):
    r = client.post(
        "/api/providers",
        json={
            "name": "local",
            "base_url": "http://localhost:9999/v1",
            "key": "secret-for-test",
            "models": ["gpt-test"],
        },
    )
    assert r.status_code == 200
    assert "secret-for-test" not in client.get("/api/providers").text
    path = app.state.store.root / "credentials" / f"{r.json()['id']}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (
        client.post(
            "/api/queue",
            json={"paused": True},
            headers={"Origin": "http://evil.example"},
        ).status_code
        == 403
    )


def test_disk_gate_and_configuration(app, client):
    app.state.store.config["min_free_bytes"] = 10**30
    assert (
        client.post("/api/uploads", json={"path": "a.tex", "size": 1}).status_code
        == 400
    )
    assert client.put("/api/config", json={"page_concurrency": 0}).status_code == 400
    assert (
        client.put("/api/config", json={"page_concurrency": 3}).json()[
            "page_concurrency"
        ]
        == 3
    )
    assert (
        json.loads(Path(app.state.store.config["config_file"]).read_text())[
            "page_concurrency"
        ]
        == 3
    )


def test_observations_out_of_order_and_missing_usage(client, app):
    j = job(client)
    s = app.state.store
    j = s.patch(j["id"], pages=3, status="running")
    folder = s.root / "jobs" / j["id"] / "work" / "tex"
    folder.mkdir(parents=True)
    (folder / "a.tex").write_text("% LEXOID_PAGE_COMPLETED: 3/3\n")
    (folder / "calls.calls.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {
                    "event": "finish",
                    "call_id": "1",
                    "page": 3,
                    "status": "ok",
                    "model": "gpt-test",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                    },
                },
                {
                    "event": "finish",
                    "call_id": "2",
                    "page": 1,
                    "status": "error",
                    "attempt": 2,
                    "usage": None,
                },
            ]
        )
    )
    result = snapshot(s, j)
    assert result["completed_pages"] == 1
    assert result["page_rows"][0]["status"] == "failed"
    assert result["page_rows"][2]["status"] == "completed"
    assert (
        result["usage"]["input_tokens"] is None
        and result["usage"]["known_input_tokens"] == 10
    )
    assert result["usage"]["retry_calls"] == 1


def test_recovery_adopts_live_container_without_launch(client, app, monkeypatch):
    j = job(client)
    s = app.state.store
    j = s.claim()
    w = Worker(s)
    monkeypatch.setattr(
        w, "inspect", lambda name: {"Running": True, "Status": "running"}
    )
    monkeypatch.setattr(w, "launch", lambda job: pytest.fail("must not launch again"))
    w.tick()
    assert s.job(j["id"])["status"] == "running"


def test_cancel_starting_does_not_launch(client, app, monkeypatch):
    j = job(client)
    s = app.state.store
    j = s.claim()
    s.command(j["id"], "cancel", j["version"])
    w = Worker(s)
    monkeypatch.setattr(w, "inspect", lambda name: None)
    monkeypatch.setattr(
        w, "launch", lambda job: pytest.fail("cancelled job must not launch")
    )
    w.tick()
    assert s.job(j["id"])["status"] == "cancelled"


def test_valid_pdf_upload_and_queue_config_isolation(client, app, tmp_path):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 200)
    path = tmp_path / "source.pdf"
    pdf.save(path)
    pdf.close()
    u = upload(client, "batch/source.PDF", path.read_bytes())
    assert client.get("/api/uploads/" + u["id"]).json()["pages"] == 1
    p = client.post(
        "/api/providers",
        json={"name": "test", "base_url": "http://localhost:9876/v1", "key": "key"},
    ).json()
    spec = {"provider": p["id"], "model": "gpt-test"}
    r = client.post(
        "/api/jobs",
        json={
            "kind": "pdf_convert",
            "uploads": [u["id"]],
            "request_id": "pdf",
            "config": {"recognize": spec},
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()[0]
    assert j["config"]["page_concurrency"] == 1
    client.put("/api/config", json={"page_concurrency": 3})
    assert app.state.store.job(j["id"])["config"]["page_concurrency"] == 1
    assert client.delete("/api/providers/" + p["id"]).status_code == 400


def test_failure_is_not_misreported_as_compile_failure(client, app):
    j = job(client)
    s = app.state.store
    j = s.patch(j["id"], status="failed", pages=1)
    root = s.root / "jobs" / j["id"] / "work"
    root.mkdir()
    event = {
        "event": "validation_or_request_error",
        "stage": "recognize",
        "page": 1,
        "error_type": "RecoverableVisionError",
        "error_message": "Missing page marker",
    }
    (root / "recognize.calls.jsonl").write_text(json.dumps(event) + "\n")
    result = snapshot(s, j)
    assert result["checks"][0]["status"] == "unchecked"
    assert result["page_rows"][0]["status"] == "failed"
    assert result["page_rows"][0]["error"] == "Missing page marker"


def test_only_final_compile_pass_warnings_count(client, app):
    j = job(client)
    s = app.state.store
    j = s.patch(j["id"], status="succeeded", attempt=1)
    root = s.root / "jobs" / j["id"] / "result" / "1"
    root.mkdir(parents=True)
    (root / "compile.log").write_text(
        "XeLaTeX pass 1/2\nLaTeX Warning: There were undefined references.\nXeLaTeX pass 2/2\nOutput written on result.pdf"
    )
    assert (
        next(c for c in snapshot(s, j)["checks"] if c["name"] == "未解析引用")["status"]
        == "passed"
    )


def test_nonzero_exited_container_fails_and_stops_queue(client, app, monkeypatch):
    job(client)
    s = app.state.store
    j = s.claim()
    w = Worker(s)
    monkeypatch.setattr(
        w,
        "inspect",
        lambda name: {
            "Running": False,
            "Status": "exited",
            "ExitCode": 1,
            "OOMKilled": False,
        },
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        w, "docker", lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0)
    )
    w.tick()
    assert s.job(j["id"])["status"] == "failed" and s.paused()


def test_changed_payload_cannot_reuse_submission_id(client):
    u = upload(client)
    data = {
        "kind": "tex_compile",
        "uploads": [u["id"]],
        "main_file": "main.tex",
        "request_id": "fixed",
    }
    assert client.post("/api/jobs", json=data).status_code == 200
    assert (
        client.post("/api/jobs", json={**data, "batch": "different"}).status_code == 400
    )


def test_queued_config_update_and_template_are_versioned(client, app, tmp_path):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(595, 842)
    source = tmp_path / "source.pdf"
    pdf.save(source)
    pdf.close()
    u = upload(client, "source.pdf", source.read_bytes())
    provider = client.post(
        "/api/providers",
        json={"name": "test", "base_url": "http://localhost:9876/v1", "key": "secret"},
    ).json()
    config = {
        "recognize": {"provider": provider["id"], "model": "gpt-test"},
        "page_concurrency": 1,
    }
    template = client.post(
        "/api/templates",
        json={"name": "模板", "config": {**config, "api_key": "must-not-persist"}},
    )
    assert (
        template.status_code == 200
        and "must-not-persist" not in client.get("/api/templates").text
    )
    j = client.post(
        "/api/jobs",
        json={
            "kind": "pdf_convert",
            "uploads": [u["id"]],
            "config": config,
            "request_id": "pdf",
        },
    ).json()[0]
    r = client.put(
        "/api/jobs/" + j["id"] + "/config",
        json={"version": j["version"], "page_concurrency": 3},
    )
    assert r.status_code == 200 and r.json()["config"]["page_concurrency"] == 3
    app.state.store.claim()
    assert (
        client.put(
            "/api/jobs/" + j["id"] + "/config",
            json={"version": r.json()["version"], "page_concurrency": 1},
        ).status_code
        == 400
    )


def test_start_ack_does_not_lose_concurrent_cancel(client, app):
    job(client)
    s = app.state.store
    j = s.claim()
    s.command(j["id"], "cancel", j["version"])
    assert s.started(j["id"], "sha256:test")["status"] == "cancel_requested"


def test_retry_budget_oom_and_notifications(client, app, monkeypatch):
    from types import SimpleNamespace

    j = job(client)
    s = app.state.store
    j = s.claim()
    folder = s.root / "jobs" / j["id"]
    (folder / "progress.json").write_text(json.dumps({"retryable": True}))
    w = Worker(s)
    monkeypatch.setattr(
        w, "docker", lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0)
    )
    w.finalize(j, {"ExitCode": 1, "OOMKilled": False})
    retry = s.job(j["id"])
    assert retry["status"] == "retry_wait" and retry["restarts"] == 1
    assert 175 < retry["next_retry_at"] - time.time() <= 180
    j = s.patch(j["id"], status="running", restarts=6, attempt=7)
    w.finalize(j, {"ExitCode": 1, "OOMKilled": False})
    assert s.job(j["id"])["status"] == "failed"
    notices = s.events(j["id"])
    assert any(
        e.get("event") == "notification" and e["status"] == "unconfigured"
        for e in notices
    )
    j = s.patch(j["id"], status="running", restarts=0)
    w.finalize(j, {"ExitCode": 137, "OOMKilled": True})
    assert s.job(j["id"])["status"] == "failed" and "OOM" in s.job(j["id"])["error"]


def test_restart_email_contains_attempt_and_failure_reason(client, app, monkeypatch):
    from texopt.monitoring import worker_watch

    messages = []
    monkeypatch.setattr(
        worker_watch,
        "send_email_message",
        lambda settings, subject, body: messages.append((subject, body)),
    )
    app.state.store.config["email"] = {"sender": "fixture@example.invalid"}
    j = job(client)
    j.update(attempt=2, previous_error="服务暂时不可用")
    w = Worker(app.state.store)
    w.notify(j, "失败后准备重启")
    w.notify(j, "启动成功")
    w.notify(j, "失败")
    assert len(messages) == 3
    assert all("第 1 次" in subject for subject, _ in messages)
    assert all("服务暂时不可用" in body for _, body in messages)
    assert all(
        e["status"] == "sent"
        for e in app.state.store.events(j["id"])
        if e["event"] == "notification"
    )


def test_reconcile_failure_visible_after_recognition_success(client, app):
    j = job(client)
    s = app.state.store
    j = s.patch(j["id"], pages=1, status="failed")
    root = s.root / "jobs" / j["id"] / "work" / "tex"
    root.mkdir(parents=True)
    (root / "source.tex").write_text("% LEXOID_PAGE_COMPLETED: 1/1")
    (root / "calls.calls.jsonl").write_text(
        json.dumps(
            {
                "event": "finish",
                "call_id": "failed-review",
                "page": 1,
                "stage": "reconcile",
                "status": "error",
                "error_type": "TimeoutError",
            }
        )
        + "\n"
    )
    snap = snapshot(s, j)
    assert snap["completed_pages"] == 1
    assert (
        snap["page_rows"][0]["status"] == "failed"
        and snap["page_rows"][0]["stage"] == "reconcile"
    )


def test_legacy_catalog_and_compile_copy_never_modify_original(client, app, tmp_path):
    root = tmp_path / "legacy" / "U1"
    batch = root / "A33Z202410007"
    batch.mkdir(parents=True)
    path = batch / "legacy.tex"
    content = b"\\documentclass{article}\\begin{document}Legacy\\end{document}"
    path.write_bytes(content)
    app.state.store.config["legacy_artifact_roots"] = {"U1": str(root)}
    app.state.store.config["batch_aliases"] = {"A33Z202410007": "A33Z2202410007"}
    items = client.get("/api/library").json()
    assert (
        len(items) == 1
        and items[0]["batch"] == "A33Z2202410007"
        and "path" not in items[0]
    )
    assert client.get("/api/library/" + items[0]["id"] + "/source").content == content
    r = client.post(
        "/api/library/" + items[0]["id"] + "/compile",
        json={"request_id": "legacy-copy"},
    )
    assert r.status_code == 200 and r.json()[0]["kind"] == "tex_compile"
    assert path.read_bytes() == content
    assert not list(root.rglob("*.sqlite3"))
