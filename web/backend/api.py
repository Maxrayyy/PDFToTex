"""HTTP commands and queries. Conversion runs in the separate worker process."""

import fcntl
import os
import json
from pathlib import Path
import time
import zipfile
import tempfile
from starlette.background import BackgroundTask
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .store import Store, load_config, atomic_json, ROOT, ACTIVE, uid
from .observations import snapshot, redact


def archive_response(root, filename, traceability, files):
    """Build a download and remove its temporary ZIP on failure or after sending."""
    fd, path = tempfile.mkstemp(suffix=".zip", dir=root)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "traceability.json",
                json.dumps(traceability, ensure_ascii=False, indent=2),
            )
            for source, name in files:
                archive.write(source, name)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(Path(path).unlink, missing_ok=True),
    )


def create_app(config=None):
    store = Store(config or load_config())
    app = FastAPI(title="Lexiod Workbench")
    app.state.store = store

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "记录不存在"}, status_code=404)

    @app.middleware("http")
    async def same_origin(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            from urllib.parse import urlparse

            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "不允许跨站写入"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/api/overview")
    def overview():
        try:
            health = json.loads((store.root / "heartbeat.json").read_text())
        except (OSError, ValueError):
            health = {}
        return dict(
            jobs=store.jobs(),
            paused=store.paused(),
            disk=store.disk(),
            worker={**health, "online": time.time() - health.get("time", 0) < 15},
        )

    @app.get("/api/operations")
    def operations():
        from datetime import datetime
        from zoneinfo import ZoneInfo

        reports = {}
        for key in ("monitor_snapshot", "daily_report"):
            path = store.config.get(key)
            if path:
                try:
                    p = Path(path)
                    reports[key] = {
                        "text": p.read_text()[-200000:],
                        "updated_at": p.stat().st_mtime,
                    }
                except OSError:
                    reports[key] = {"error": "配置的报告文件无法读取"}
        daily = {}
        for job in store.jobs():
            if job.get("status") != "succeeded":
                continue
            day = datetime.fromtimestamp(
                job["finished_at"],
                ZoneInfo(store.config.get("timezone", "Asia/Shanghai")),
            ).strftime("%Y-%m-%d")
            entry = daily.setdefault(
                day,
                {
                    "date": day,
                    "converted": 0,
                    "compiled": 0,
                    "pages": 0,
                    "calls": 0,
                    "known_tokens": 0,
                    "unreported_calls": 0,
                },
            )
            entry["converted" if job["kind"] == "pdf_convert" else "compiled"] += 1
            entry["pages"] += job.get("pages") or 0
            usage = (job.get("summary") or {}).get("usage", {})
            entry["calls"] += usage.get("calls", 0)
            entry["known_tokens"] += usage.get("known_total_tokens", 0)
            entry["unreported_calls"] += int(usage.get("total_tokens") is None)
        queues = []
        for path in store.config.get("legacy_queue_status", []):
            try:
                value = json.loads(Path(path).read_text())
                queues.append(
                    {
                        "path": path,
                        "status": value.get("status"),
                        "jobs": [
                            {
                                k: j.get(k)
                                for k in (
                                    "source",
                                    "stem",
                                    "pages",
                                    "status",
                                    "exit_code",
                                )
                            }
                            for j in value.get("jobs", [])
                        ],
                    }
                )
            except (OSError, ValueError):
                queues.append({"path": path, "error": "无法读取外部队列状态"})
        return {
            "reports": reports,
            "daily": sorted(daily.values(), key=lambda d: d["date"], reverse=True),
            "legacy_queues": queues,
            "notifications_configured": bool(store.config.get("email")),
            "retry_delays": store.config["retry_delays"],
        }

    @app.get("/api/library")
    def library():
        from .library import catalog

        return [{k: v for k, v in r.items() if k != "path"} for r in catalog(store)]

    @app.get("/api/library/{ident}/source")
    def library_source(ident: str):
        from .library import entry

        return FileResponse(entry(store, ident)["path"], media_type="text/plain")

    @app.post("/api/library/{ident}/compile")
    def library_compile(ident: str, data: dict):
        from .library import compile_copy

        return compile_copy(store, ident, data.get("request_id"))

    @app.get("/api/config")
    def config_get():
        return {
            k: store.config[k]
            for k in (
                "page_concurrency",
                "max_page_concurrency",
                "max_upload_bytes",
                "min_free_bytes",
                "image",
            )
        }

    @app.put("/api/config")
    def config_put(data: dict):
        n = data.get("page_concurrency")
        if type(n) is not int or not 1 <= n <= store.config["max_page_concurrency"]:
            raise ValueError("页并发超出范围")
        store.config["page_concurrency"] = n
        atomic_json(
            store.config["config_file"],
            {k: v for k, v in store.config.items() if k != "config_file"},
        )
        return config_get()

    @app.get("/api/templates")
    def templates():
        return store.records("template")

    @app.post("/api/templates")
    def template_save(data: dict):
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("请输入模板名称")
        config = store.model_config(data.get("config") or {})
        return store.save(
            "template", {"id": uid(), "name": name[:100], "config": config}
        )

    @app.get("/api/providers")
    def providers():
        return store.providers()

    @app.post("/api/providers")
    def provider_save(data: dict):
        return store.provider_save(data)

    @app.delete("/api/providers/{ident}")
    def provider_delete(ident: str):
        store.record("provider", ident)
        if any(
            ident in json.dumps(j["config"])
            and j["status"] in (*ACTIVE, "queued", "retry_wait", "paused")
            for j in store.jobs()
        ):
            raise ValueError("仍有任务引用此供应商，不能删除")
        with store.db() as db:
            db.execute("DELETE FROM records WHERE kind='provider' AND id=?", (ident,))
        (store.root / "credentials" / f"{ident}.json").unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/providers/{ident}/test")
    def provider_test(ident: str):
        provider = store.record("provider", ident)
        try:
            result = httpx.get(
                provider["base_url"] + "/models",
                headers={"Authorization": "Bearer " + store.secret(ident)},
                timeout=20,
            )
            if result.status_code != 200:
                return {"ok": False, "message": f"服务返回 HTTP {result.status_code}"}
            models = [
                x["id"]
                for x in result.json().get("data", [])
                if isinstance(x, dict) and isinstance(x.get("id"), str)
            ]
            provider["models"] = models
            store.save("provider", provider)
            return {
                "ok": True,
                "models": models,
                "message": "连接有效；模型列表查询不代表图像识别能力已验证",
            }
        except (httpx.HTTPError, ValueError):
            return {"ok": False, "message": "连接失败、超时或模型列表格式不兼容"}

    @app.post("/api/uploads")
    def upload_create(data: dict):
        return store.upload_create(data)

    @app.get("/api/uploads/{ident}")
    def upload_get(ident: str):
        value = store.record("upload", ident)
        path = store.upload_path(ident)
        value["offset"] = path.stat().st_size if path.exists() else 0
        return value

    @app.put("/api/uploads/{ident}")
    async def upload_chunk(ident: str, request: Request, offset: int = 0):
        value = store.record("upload", ident)
        if value["complete"]:
            raise ValueError("文件已完成，不可修改")
        chunks = []
        length = 0
        async for chunk in request.stream():
            length += len(chunk)
            if length > 4 * 1024**2:
                raise HTTPException(413, "单片不能超过 4 MiB")
            chunks.append(chunk)
        content = b"".join(chunks)
        store.require_space(length)
        if offset < 0 or offset + length > value["size"]:
            raise ValueError("分片超出声明大小")
        path = store.upload_path(ident)
        with path.open("a+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0, 2)
            current = f.tell()
            if offset < current:
                f.seek(offset)
                if f.read(length) != content:
                    raise ValueError("分片冲突，请重新上传")
            elif offset != current:
                raise ValueError("上传偏移不匹配")
            else:
                f.write(content)
                f.flush()
        return upload_get(ident)

    @app.post("/api/uploads/{ident}/finish")
    def upload_finish(ident: str):
        return store.finish_upload(ident)

    @app.post("/api/jobs")
    def submit(data: dict):
        return store.submit(data)

    @app.get("/api/jobs/{ident}")
    def detail(ident: str):
        job = store.job(ident)
        return {**job, **snapshot(store, job)}

    @app.get("/api/jobs/{ident}/publication")
    def publication_get(ident: str):
        store.job(ident)
        return {
            "enabled": bool(store.config.get("overleaf_config")),
            "requests": [r for r in store.records("publication") if r["job"] == ident],
        }

    @app.post("/api/jobs/{ident}/publication")
    def publication_request(ident: str, data: dict):
        from .publication import request_publication

        return request_publication(store, store.job(ident), data.get("action"))

    @app.get("/api/jobs/{ident}/events")
    def events(ident: str, after: int = 0):
        store.job(ident)
        return store.events(ident, after)

    @app.put("/api/jobs/{ident}/config")
    def job_config(ident: str, data: dict):
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            job = json.loads(row[0])
            if (
                job["status"] not in ("queued", "paused")
                or job["attempt"]
                or job["kind"] != "pdf_convert"
            ):
                raise ValueError("只能修改尚未启动的 PDF 转换任务")
            if data.get("version") != job["version"]:
                raise ValueError("任务已更新，请刷新后重试")
            job["config"] = store.model_config(
                {**job["config"], "page_concurrency": data.get("page_concurrency")}
            )
            store._write(db, job)
        store.event(ident, event="config_updated", config=job["config"])
        return job

    @app.post("/api/jobs/{ident}/command")
    def command(ident: str, data: dict):
        return store.command(ident, data.get("action"), data.get("version"))

    @app.post("/api/queue")
    def queue(data: dict):
        if type(data.get("paused")) is not bool:
            raise ValueError("缺少暂停状态")
        store.pause(data["paused"])
        return {"paused": store.paused()}

    @app.get("/api/jobs/{ident}/logs")
    def logs(ident: str):
        job = store.job(ident)
        root = store.root / "jobs" / ident
        paths = [
            root / f"attempt-{job['attempt']}.log",
            root / "result" / str(job["attempt"]) / "compile.log",
            *(root / "work").rglob("*.process.log"),
        ]
        text = "\n".join(
            p.name + "\n" + p.read_text(errors="replace")[-40000:]
            for p in paths
            if p.is_file()
        )
        keys = [store.secret(p["id"]) for p in store.providers()]
        return {"text": redact(text, keys)[-120000:]}

    @app.get("/api/jobs/{ident}/source")
    def source(ident: str):
        job = store.job(ident)
        path = store.root / "jobs" / ident / "input" / job["name"]
        return FileResponse(
            path,
            media_type=(
                "application/pdf" if job["kind"] == "pdf_convert" else "text/plain"
            ),
        )

    @app.get("/api/jobs/{ident}/download")
    def download(ident: str):
        job = store.job(ident)
        root = store.root / "jobs" / ident
        store.require_space()
        files = [
            (source, "source/" + str(source.relative_to(root / "input")))
            for source in (root / "input").rglob("*")
            if source.is_file()
        ]
        files.extend(
            (store.root / a["path"], f"attempt-{a['attempt']}/" + Path(a["path"]).name)
            for a in job["artifacts"]
            if (store.root / a["path"]).is_file()
        )
        return archive_response(
            store.root,
            ident + ".zip",
            {"job": job, "events": store.events(ident)},
            files,
        )

    @app.get("/api/batches/download")
    def batch_download(batch: str = ""):
        jobs = [j for j in store.jobs() if j["batch"] == batch and j.get("artifacts")]
        if not jobs:
            raise ValueError("此批次尚无产物")
        artifacts = [(j, a) for j in jobs for a in j["artifacts"]]
        store.require_space(
            sum((store.root / a["path"]).stat().st_size for _, a in artifacts)
            + 1024 * 1024
        )
        # IDs separate versions and same-name files without modifying originals.
        files = (
            (
                store.root / a["path"],
                f"{j['id']}/attempt-{a['attempt']}/{Path(a['name']).name}",
            )
            for j, a in artifacts
        )
        return archive_response(store.root, "batch.zip", jobs, files)

    @app.get("/api/artifacts/{ident}")
    def artifact(ident: str):
        return store.record("artifact", ident)

    @app.get("/api/artifacts/{ident}/file")
    def artifact_file(ident: str, download: bool = False):
        a = store.record("artifact", ident)
        path = store.root / a["path"]
        if not path.is_file():
            raise HTTPException(404, "产物已不存在")
        return FileResponse(
            path,
            media_type="application/pdf" if a["kind"] == "pdf" else "text/plain",
            filename=a["name"] if download else None,
        )

    @app.post("/api/jobs/{ident}/recompile")
    def recompile(ident: str, data: dict):
        job = store.job(ident)
        if job["kind"] == "tex_compile":
            uploads = [u["id"] for u in job["uploads"]]
            main_file = job["config"]["main_file"]
        else:
            tex = next((a for a in reversed(job["artifacts"]) if a["kind"] == "tex"), None)
            if not tex:
                raise ValueError("尚未生成 TeX")
            path = store.root / tex["path"]
            up = store.upload_create({"path": path.name, "size": path.stat().st_size})
            import shutil

            shutil.copyfile(path, store.upload_path(up["id"]))
            store.finish_upload(up["id"])
            uploads = [up["id"]]
            main_file = path.name
        return store.submit(
            dict(
                kind="tex_compile",
                uploads=uploads,
                main_file=main_file,
                request_id=data.get("request_id"),
                unit=job["unit"],
                batch=job["batch"],
            )
        )

    @app.post("/api/jobs/{ident}/review")
    def review(ident: str, data: dict):
        if data.get("review") not in ("pending", "approved", "needs_changes"):
            raise ValueError("无效审核状态")
        return store.patch(ident, review=data["review"])

    dist = ROOT / "web/frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        if (dist / "pdfjs").is_dir():
            app.mount("/pdfjs", StaticFiles(directory=dist / "pdfjs"), name="pdfjs")

        @app.get("/{path:path}")
        def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")

    return app


app = create_app()
