"""Opt-in actual runtime regression; uses isolated temporary data and no paid API."""

import os
from pathlib import Path
import time
import pytest
from fastapi.testclient import TestClient
from web.backend.api import create_app
from web.backend.store import DEFAULTS, Store
from web.backend.worker import Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_DOCKER_TESTS") != "1",
    reason="set WEB_DOCKER_TESTS=1 for real Docker regression",
)


def upload(c, path, content):
    if isinstance(content, str):
        content = content.encode()
    u = c.post("/api/uploads", json={"path": path, "size": len(content)}).json()
    assert c.put("/api/uploads/" + u["id"], content=content).status_code == 200
    assert c.post("/api/uploads/" + u["id"] + "/finish").status_code == 200
    return u["id"]


def test_real_compile_recovery_and_failure(tmp_path):
    config = {
        **DEFAULTS,
        "data_root": str(tmp_path),
        "min_free_bytes": 0,
        "config_file": str(tmp_path / "config.json"),
    }
    app = create_app(config)
    c = TestClient(app)
    s = app.state.store
    tex = r"""\documentclass[UTF8,fontset=fandol]{ctexart}
\usepackage{amsmath,graphicx,custom}
\begin{document}
\testword: $6.0\times10^{7}$, $x\geq 3$, $y\leq 5$, $10\,\mu\mathrm{L}$.
\input{sections/text}
\newpage 第二页。见第\pageref{end}页。\label{end}
\end{document}"""
    uploads = [
        upload(c, "project/main.tex", tex),
        upload(c, "project/custom.sty", r"\newcommand{\testword}{真实容器测试}"),
        upload(c, "project/sections/text.tex", "子文件内容。"),
    ]
    response = c.post(
        "/api/jobs",
        json={
            "kind": "tex_compile",
            "uploads": uploads,
            "main_file": "project/main.tex",
            "request_id": "valid",
        },
    )
    assert response.status_code == 200, response.text
    j = response.json()[0]
    w = Worker(s)
    w.tick()
    # Recreate the dispatcher while the independently running container retains the job.
    resumed = Worker(Store(config))
    for _ in range(60):
        resumed.tick()
        j = s.job(j["id"])
        if j["status"] in ("failed", "succeeded"):
            break
        time.sleep(0.5)
    assert j["status"] == "succeeded", c.get("/api/jobs/" + j["id"] + "/logs").text[
        -8000:
    ]
    assert j["attempt"] == 1
    pdf = next(a for a in j["artifacts"] if a["kind"] == "pdf")
    result = c.get("/api/artifacts/" + pdf["id"] + "/file")
    assert result.content.startswith(b"%PDF")
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(result.content)
    assert len(doc) == 2
    # Render the exact PDF produced by this container and preserve evidence when requested.
    output = Path(os.environ.get("WEB_TEST_ARTIFACTS", tmp_path / "rendered"))
    output.mkdir(parents=True, exist_ok=True)
    for i in range(len(doc)):
        page = doc[i]
        page.render(scale=1.5).to_pil().save(output / f"compiled-page-{i+1}.png")
        page.close()
    doc.close()
    assert c.get("/api/jobs/" + j["id"]).json()["usage"]["calls"] == 0
    broken = upload(
        c,
        "broken.tex",
        r"\documentclass{article}\begin{document}\undefinedcommand\end{document}",
    )
    bad = c.post(
        "/api/jobs",
        json={
            "kind": "tex_compile",
            "uploads": [broken],
            "main_file": "broken.tex",
            "request_id": "invalid",
        },
    ).json()[0]
    for _ in range(60):
        resumed.tick()
        bad = s.job(bad["id"])
        if bad["status"] == "failed":
            break
        time.sleep(0.5)
    assert bad["status"] == "failed" and s.paused()
    detail = c.get("/api/jobs/" + bad["id"]).json()
    assert detail["compile_errors"] and not detail["artifacts"]
    assert s.job(j["id"])["artifacts"] == j["artifacts"]


def test_real_pdf_pipeline_with_local_provider(tmp_path):
    import json
    import platform
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import pypdfium2 as pdfium

    calls = []
    tex = r"""\documentclass[UTF8,fontset=fandol]{ctexart}
\usepackage{amsmath,amssymb}
\begin{document}
网页流水线验证。$6.0\times10^{7}$，$x\geq 3$。
% LEXOID_PAGE_COMPLETED: 1/1
\end{document}"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request["model"])
            result = {
                "id": "fixture-1",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": tex},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 60,
                    "total_tokens": 180,
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = (
            "host.docker.internal"
            if platform.system() == "Darwin"
            else subprocess.check_output(
                [
                    "docker",
                    "network",
                    "inspect",
                    "bridge",
                    "--format",
                    "{{(index .IPAM.Config 0).Gateway}}",
                ],
                text=True,
            ).strip()
        )
        config = {
            **DEFAULTS,
            "data_root": str(tmp_path),
            "min_free_bytes": 0,
            "config_file": str(tmp_path / "config.json"),
        }
        app = create_app(config)
        c = TestClient(app)
        s = app.state.store
        provider = c.post(
            "/api/providers",
            json={
                "name": "固定响应测试服务",
                "base_url": f"http://{host}:{server.server_port}/v1",
                "key": "fixture-not-a-real-key",
            },
        ).json()
        doc = pdfium.PdfDocument.new()
        doc.new_page(595, 842)
        path = tmp_path / "source.pdf"
        doc.save(path)
        doc.close()
        u = upload(c, "batch/source.pdf", path.read_bytes())
        spec = {"provider": provider["id"], "model": "gpt-web-fixture"}
        response = c.post(
            "/api/jobs",
            json={
                "kind": "pdf_convert",
                "uploads": [u],
                "request_id": "convert",
                "config": {"recognize": spec},
            },
        )
        assert response.status_code == 200, response.text
        j = response.json()[0]
        w = Worker(s)
        for _ in range(100):
            w.tick()
            j = s.job(j["id"])
            if j["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.5)
        assert j["status"] == "succeeded", c.get("/api/jobs/" + j["id"] + "/logs").text[
            -8000:
        ]
        detail = c.get("/api/jobs/" + j["id"]).json()
        assert (
            detail["completed_pages"] == 1
            and detail["usage"]["input_tokens"] == 120
            and detail["usage"]["output_tokens"] == 60
        )
        assert calls == ["gpt-web-fixture"]
        assert {a["kind"] for a in detail["artifacts"]} == {"pdf", "tex", "log"}
        assert (
            next(a for a in detail["artifacts"] if a["kind"] == "tex")["name"]
            == "source.tex"
        )
        archive = c.get("/api/batches/download?batch=")
        assert archive.status_code == 200
        import io, zipfile, shutil

        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert any(name.endswith("/source.tex") for name in z.namelist())
        assert detail["page_rows"][0]["model"] == "gpt-web-fixture"
        text = c.get("/api/jobs/" + j["id"] + "/logs").json()["text"]
        assert "fixture-not-a-real-key" not in text
        output = Path(os.environ.get("WEB_TEST_ARTIFACTS", tmp_path / "rendered"))
        output.mkdir(parents=True, exist_ok=True)
        pdf = next(a for a in detail["artifacts"] if a["kind"] == "pdf")
        for artifact in detail["artifacts"]:
            shutil.copyfile(
                s.root / artifact["path"],
                output / ("pipeline-result." + artifact["kind"]),
            )
        rendered = pdfium.PdfDocument(s.root / pdf["path"])
        page = rendered[0]
        page.render(scale=1.5).to_pil().save(output / "pipeline-page-1.png")
        page.close()
        rendered.close()
        (output / "pipeline-result.json").write_text(
            json.dumps(
                {
                    "container": j["container"],
                    "image_id": j["image_id"],
                    "checks": detail["checks"],
                    "usage": detail["usage"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
