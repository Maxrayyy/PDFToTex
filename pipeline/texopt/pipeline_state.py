"""SQLite-backed durable state for the folder pipeline."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    source_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    output_path TEXT NOT NULL DEFAULT '',
    next_retry_at REAL NOT NULL DEFAULT 0,
    started_at REAL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    last_error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(stage, source_path, fingerprint)
);
CREATE INDEX IF NOT EXISTS jobs_ready
ON jobs(stage, status, next_retry_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    stage TEXT NOT NULL,
    source_path TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
"""


class PipelineState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def recover_interrupted(self) -> int:
        """Make jobs interrupted by a process/container restart claimable again."""
        now = time.time()
        with self._connect() as db:
            cur = db.execute(
                "UPDATE jobs SET status='retry', next_retry_at=?, updated_at=?, "
                "last_error=CASE WHEN last_error='' THEN 'process interrupted; recovered' "
                "ELSE last_error END WHERE status='running'",
                (now, now),
            )
            return cur.rowcount

    def claim(self, stage: str, source_path: str, fingerprint: str,
              max_attempts: int = 0) -> Optional[int]:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE stage=? AND source_path=? AND fingerprint=?",
                (stage, source_path, fingerprint),
            ).fetchone()
            if row:
                if row["status"] in ("completed", "running"):
                    return None
                if row["next_retry_at"] > now:
                    return None
                if max_attempts and row["attempts"] >= max_attempts:
                    return None
                attempts = row["attempts"] + 1
                db.execute(
                    "UPDATE jobs SET status='running', attempts=?, started_at=?, "
                    "updated_at=?, last_error='' WHERE id=?",
                    (attempts, now, now, row["id"]),
                )
                return int(row["id"])
            cur = db.execute(
                "INSERT INTO jobs(stage,source_path,fingerprint,status,attempts,"
                "started_at,updated_at) VALUES(?,?,?,'running',1,?,?)",
                (stage, source_path, fingerprint, now, now),
            )
            return int(cur.lastrowid)

    def complete(self, job_id: int, output_path: str,
                 metadata: Optional[dict] = None) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status='completed', output_path=?, completed_at=?, "
                "updated_at=?, metadata_json=? WHERE id=?",
                (output_path, now, now,
                 json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True), job_id),
            )

    def fail(self, job_id: int, error: str, retry_after: float) -> None:
        now = time.time()
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET status='retry', next_retry_at=?, updated_at=?, "
                "last_error=? WHERE id=?",
                (now + retry_after, now, error[-8000:], job_id),
            )

    def attempts(self, job_id: int) -> int:
        with self._connect() as db:
            row = db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            return int(row[0]) if row else 1

    def event(self, stage: str, source_path: str, event: str,
              payload: Optional[dict] = None) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(created_at,stage,source_path,event,payload_json) "
                "VALUES(?,?,?,?,?)",
                (time.time(), stage, source_path, event,
                 json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)),
            )

    def status_snapshot(self, recent: int = 20) -> dict:
        with self._connect() as db:
            counts = [dict(row) for row in db.execute(
                "SELECT stage,status,COUNT(*) AS count FROM jobs "
                "GROUP BY stage,status ORDER BY stage,status"
            ).fetchall()]
            jobs = [dict(row) for row in db.execute(
                "SELECT id,stage,source_path,fingerprint,status,attempts,output_path,"
                "next_retry_at,completed_at,updated_at,last_error FROM jobs "
                "ORDER BY updated_at DESC LIMIT ?", (recent,)
            ).fetchall()]
        return {"counts": counts, "recent_jobs": jobs}

    def completed_metadata(self, stage: str, source_path: str, fingerprint: str):
        with self._connect() as db:
            row = db.execute("SELECT metadata_json FROM jobs WHERE stage=? AND source_path=? "
                             "AND fingerprint=? AND status='completed'",
                             (stage, source_path, fingerprint)).fetchone()
        return json.loads(row[0]) if row else None

    def invalidate_completed(self, stage: str, source_path: str, fingerprint: str):
        with self._connect() as db:
            db.execute("UPDATE jobs SET status='retry', next_retry_at=0 WHERE stage=? "
                       "AND source_path=? AND fingerprint=? AND status='completed'",
                       (stage, source_path, fingerprint))
