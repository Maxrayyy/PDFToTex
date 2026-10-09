"""Durable commands and isolated storage; no dependency on the legacy dispatcher."""

from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = {
    "data_root": str(ROOT.parent / "data" / "web"),
    "image": "pdftotex:local",
    "docker": "docker",
    "poll_seconds": 2,
    "page_concurrency": 1,
    "max_page_concurrency": 8,
    "min_free_bytes": 2 * 1024**3,
    "max_upload_bytes": 2 * 1024**3,
    "compile_timeout": 180,
    "conversion_timeout": 43200,
    "memory": "4g",
    "cpus": 2,
    "retry_delays": [180, 360, 720, 1440, 2880, 5760],
    "overleaf_config": None,
    "monitor_config": None,
}
ACTIVE = ("starting", "running", "cancel_requested")


def uid():
    return uuid.uuid4().hex


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("无效的相对路径")
    p = PurePosixPath(value)
    if (
        p.is_absolute()
        or any(x in ("", ".", "..") for x in value.split("/"))
        or ":" in value
    ):
        raise ValueError("文件路径必须在所选项目内")
    return p.as_posix()


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uid() + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config(path=None):
    path = Path(
        path
        or os.environ.get("PDFTOTEX_WEB_CONFIG", ROOT.parent / "data/web/config.json")
    )
    conf = {**DEFAULTS, **(json.loads(path.read_text()) if path.exists() else {})}

    def configured_path(value):
        p = Path(value).expanduser()
        return str((p if p.is_absolute() else ROOT / p).resolve())

    conf["data_root"] = configured_path(conf["data_root"])
    for key in ("monitor_snapshot", "daily_report", "overleaf_config"):
        if conf.get(key):
            conf[key] = configured_path(conf[key])
    conf["legacy_queue_status"] = [
        configured_path(p) for p in conf.get("legacy_queue_status", [])
    ]
    conf["legacy_artifact_roots"] = {
        unit: configured_path(p)
        for unit, p in conf.get("legacy_artifact_roots", {}).items()
    }
    conf["config_file"] = str(path.resolve())
    return conf


class Store:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["data_root"])
        for folder in ("uploads", "jobs", "credentials"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / "credentials", 0o700)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT,position INTEGER,data TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS single_active ON jobs((1)) WHERE status IN ('starting','running','cancel_requested');
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,job TEXT,created REAL,data TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT OR IGNORE INTO settings VALUES('paused','false');
            """)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "state.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, kind, ident):
        with self.db() as db:
            row = db.execute(
                "SELECT data FROM records WHERE kind=? AND id=?", (kind, ident)
            ).fetchone()
        if not row:
            raise KeyError(ident)
        return json.loads(row[0])

    def records(self, kind):
        with self.db() as db:
            return [
                json.loads(x[0])
                for x in db.execute("SELECT data FROM records WHERE kind=?", (kind,))
            ]

    def save(self, kind, value):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO records VALUES(?,?,?)",
                (kind, value["id"], json.dumps(value)),
            )
        return value

    def event(self, job, **data):
        with self.db() as db:
            db.execute(
                "INSERT INTO events(job,created,data) VALUES(?,?,?)",
                (job, time.time(), json.dumps(data)),
            )

    def events(self, job, after=0):
        with self.db() as db:
            return [
                dict(id=r["id"], created=r["created"], **json.loads(r["data"]))
                for r in db.execute(
                    "SELECT * FROM events WHERE job=? AND id>? ORDER BY id LIMIT 500",
                    (job, after),
                )
            ]

    def jobs(self):
        with self.db() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT data FROM jobs ORDER BY position,id")
            ]

    def job(self, ident):
        with self.db() as db:
            row = db.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError(ident)
        return json.loads(row[0])

    def _write(self, db, job):
        job["updated_at"] = time.time()
        job["version"] = job.get("version", 0) + 1
        db.execute(
            "UPDATE jobs SET status=?,data=? WHERE id=?",
            (job["status"], json.dumps(job), job["id"]),
        )

    def started(self, ident, image_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = json.loads(
                db.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()[0]
            )
            if job["status"] == "starting":
                job["status"] = "running"
            job["image_id"] = image_id
            self._write(db, job)
        return job

    def patch(self, ident, **fields):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = json.loads(
                db.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()[0]
            )
            job.update(fields)
            self._write(db, job)
        return job

    def paused(self):
        with self.db() as db:
            return json.loads(
                db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[
                    0
                ]
            )

    def pause(self, value):
        with self.db() as db:
            db.execute(
                "UPDATE settings SET value=? WHERE key='paused'", (json.dumps(value),)
            )

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if json.loads(
                db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[
                    0
                ]
            ):
                return None
            if db.execute(
                "SELECT 1 FROM jobs WHERE status IN ('starting','running','cancel_requested')"
            ).fetchone():
                return None
            row = db.execute(
                "SELECT data FROM jobs WHERE status IN ('queued','retry_wait') ORDER BY position,id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            if job.get("next_retry_at", 0) > time.time():
                return None
            job["previous_error"] = job.get("error")
            job.update(
                status="starting",
                attempt=job.get("attempt", 0) + 1,
                started_at=time.time(),
                finished_at=None,
                error=None,
                next_retry_at=0,
            )
            job["container"] = "pdftotex-web-" + job["id"] + "-" + str(job["attempt"])
            self._write(db, job)
        self.event(job["id"], event="starting", attempt=job["attempt"])
        return job

    def command(self, ident, action, version):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            job = json.loads(row[0])
            status = job["status"]
            if version != job["version"]:
                raise ValueError("任务已更新，请刷新后重试")
            if action == "cancel" and status in (
                *ACTIVE,
                "queued",
                "retry_wait",
                "paused",
            ):
                job["status"] = "cancel_requested" if status in ACTIVE else "cancelled"
            elif action == "pause" and status in ("queued", "retry_wait"):
                job["status"] = "paused"
            elif action in ("resume", "retry") and status in (
                "paused",
                "failed",
                "cancelled",
            ):
                job.update(status="queued", next_retry_at=0)
            elif action == "first" and status == "queued":
                pos = db.execute(
                    "SELECT COALESCE(MIN(position),0)-1 FROM jobs"
                ).fetchone()[0]
                db.execute("UPDATE jobs SET position=? WHERE id=?", (pos, ident))
            else:
                raise ValueError("当前状态不能执行该操作")
            self._write(db, job)
        self.event(ident, event=action)
        return job

    def disk(self):
        total, used, free = shutil.disk_usage(self.root)
        return dict(
            total=total, used=used, free=free, minimum=self.config["min_free_bytes"]
        )

    def require_space(self, size=0):
        if self.disk()["free"] - size < self.config["min_free_bytes"]:
            raise ValueError("磁盘剩余空间不足，暂不能接收或启动新任务")

    def providers(self):
        return self.records("provider")

    def provider_save(self, data):
        from urllib.parse import urlparse

        base = data.get("base_url", "").rstrip("/")
        u = urlparse(base)
        if (
            u.scheme not in ("http", "https")
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("请输入有效的 API Base URL")
        key = data.get("key", "").strip()
        if not key:
            raise ValueError("请输入 API Key")
        ident = uid()
        atomic_json(self.root / "credentials" / f"{ident}.json", {"key": key})
        return self.save(
            "provider",
            dict(
                id=ident,
                name=data.get("name", "兼容服务")[:100],
                base_url=base,
                models=data.get("models", []),
                configured=True,
                created_at=time.time(),
            ),
        )

    def secret(self, ident):
        return json.loads((self.root / "credentials" / f"{ident}.json").read_text())[
            "key"
        ]

    def upload_create(self, data):
        path = safe_relative(data.get("path"))
        size = data.get("size")
        if type(size) is not int or size <= 0 or size > self.config["max_upload_bytes"]:
            raise ValueError("文件大小超出限制或文件为空")
        self.require_space(size)
        sha = data.get("sha256", "")
        if sha and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise ValueError("无效 SHA256")
        ident = uid()
        folder = self.root / "uploads" / ident
        folder.mkdir()
        return self.save(
            "upload",
            dict(
                id=ident,
                path=path,
                size=size,
                offset=0,
                sha256=sha,
                complete=False,
                created_at=time.time(),
            ),
        )

    def upload_path(self, ident):
        self.record("upload", ident)
        return self.root / "uploads" / ident / "content"

    def finish_upload(self, ident):
        record = self.record("upload", ident)
        path = self.upload_path(ident)
        if not path.exists() or path.stat().st_size != record["size"]:
            raise ValueError("文件上传尚未完成")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024**2), b""):
                digest.update(block)
        sha = digest.hexdigest()
        if record["sha256"] and sha != record["sha256"]:
            raise ValueError("文件校验失败")
        record.update(sha256=sha, complete=True, offset=record["size"])
        if record["path"].lower().endswith(".pdf"):
            import pypdfium2 as pdfium

            try:
                with contextlib.closing(pdfium.PdfDocument(path)) as doc:
                    record["pages"] = len(doc)
            except Exception:
                raise ValueError("PDF 无法读取或已加密") from None
        self.save("upload", record)
        return record

    def model_config(self, data):
        concurrency = data.get("page_concurrency", self.config["page_concurrency"])
        if (
            type(concurrency) is not int
            or not 1 <= concurrency <= self.config["max_page_concurrency"]
        ):
            raise ValueError("页并发超出范围")
        config = {"page_concurrency": concurrency}
        for stage in ("recognize", "reconcile", "optimise"):
            spec = data.get(stage) or data.get("recognize")
            if (
                not isinstance(spec, dict)
                or not isinstance(spec.get("model"), str)
                or not spec["model"].strip()
            ):
                raise ValueError("请先选择供应商和模型")
            provider = self.record("provider", spec["provider"])
            config[stage] = {"provider": provider["id"], "model": spec["model"].strip()}
        return config

    def submit(self, data):
        kind = data.get("kind")
        if kind not in ("pdf_convert", "tex_compile"):
            raise ValueError("未知任务类型")
        uploads = [self.record("upload", i) for i in data.get("uploads", [])]
        if not uploads or any(not u["complete"] for u in uploads):
            raise ValueError("请先完成文件上传")
        if len({u["path"] for u in uploads}) != len(uploads):
            raise ValueError("文件相对路径重复")
        if kind == "pdf_convert":
            if any(not u.get("pages") for u in uploads):
                raise ValueError("转换队列只能包含有效 PDF")
            config = self.model_config(data.get("config") or {})
            groups = [[u] for u in uploads]
        else:
            main = safe_relative(data.get("main_file", ""))
            if main not in [u["path"] for u in uploads] or not main.lower().endswith(
                ".tex"
            ):
                raise ValueError("请选择项目中的 TeX 主文件")
            config = {"main_file": main}
            groups = [uploads]
        key = data.get("request_id")
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError("缺少提交标识")
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "kind": kind,
                    "uploads": [u["id"] for u in uploads],
                    "config": config,
                    "unit": data.get("unit", ""),
                    "batch": data.get("batch", ""),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.require_space(sum(u["size"] for u in uploads))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT data FROM records WHERE kind='submission' AND id=?", (key,)
            ).fetchone()
            if previous:
                submission = json.loads(previous[0])
                if submission.get("fingerprint") != fingerprint:
                    raise ValueError("同一提交标识对应的文件或配置发生变化，请重新提交")
                return [self.job(i) for i in submission["jobs"]]
            pos = db.execute("SELECT COALESCE(MAX(position),0) FROM jobs").fetchone()[0]
            jobs = []
            for index, group in enumerate(groups):
                ident = uid()
                project = self.root / "jobs" / ident / "input"
                project.mkdir(parents=True)
                for upload in group:
                    target = project / upload["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.upload_path(upload["id"]), target)
                job = dict(
                    id=ident,
                    kind=kind,
                    name=(
                        group[0]["path"]
                        if kind == "pdf_convert"
                        else config["main_file"]
                    ),
                    status="queued",
                    version=1,
                    attempt=0,
                    restarts=0,
                    unit=str(data.get("unit", ""))[:30],
                    batch=self.config.get("batch_aliases", {}).get(
                        str(data.get("batch", "")), str(data.get("batch", ""))
                    )[:100],
                    original_batch=str(data.get("batch", ""))[:100],
                    config=config,
                    uploads=group,
                    created_at=time.time(),
                    updated_at=time.time(),
                    pages=group[0].get("pages"),
                    artifacts=[],
                    review="pending",
                )
                db.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?)",
                    (ident, "queued", pos + index + 1, json.dumps(job)),
                )
                jobs.append(job)
            db.execute(
                "INSERT INTO records VALUES(?,?,?)",
                (
                    "submission",
                    key,
                    json.dumps(
                        {"jobs": [j["id"] for j in jobs], "fingerprint": fingerprint}
                    ),
                ),
            )
        return jobs
