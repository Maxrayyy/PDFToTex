from datetime import datetime
import hashlib
import json
import plistlib
import sqlite3
from unittest.mock import patch

from .daily_stats import ZONE, call_summary, install, poll
from .pipeline_state import SCHEMA


def event(call_id, *, at="2026-09-07T01:00:00+00:00", usage=None, attempt=1, status="ok"):
    return {"event": "finish", "call_id": call_id, "started_at": at, "finished_at": at,
            "seconds": 5, "usage": usage, "attempt": attempt, "status": status}


def logs(scratch, events):
    scratch.mkdir(parents=True, exist_ok=True)
    for stage in ("recognize", "reconcile", "optimise"):
        (scratch / f"{stage}.process.calls.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in (events if stage == "recognize" else [])))


def config_for(tmp_path):
    return {"scan_roots": [str(tmp_path / "data")], "source_root": str(tmp_path / "Downloads"),
            "publish_root": str(tmp_path / "data" / "optimized"),
            "output_dir": str(tmp_path / "data" / "monitoring" / "daily"),
            "path_map": {"/input": str(tmp_path / "Downloads"), "/data": str(tmp_path / "data")},
            "start_date": "2026-09-07", "launchd_label": "com.lexiod.daily-test"}


def fixture_job(tmp_path, batch="BATCH1", completed="2026-09-07T02:00:00+00:00"):
    root = tmp_path / "data" / "workers" / batch
    scratch = root / ".pipeline" / "sample"
    scratch.mkdir(parents=True)
    work = scratch / "sample.optimized.tex"
    work.write_text("complete tex")
    pdf = work.with_suffix(".layout.pdf")
    pdf.write_bytes(b"%PDF-1.7\nfixture")
    report = {"compile_check": {"ok": True}, "registry_check": {"ok": False},
              "layout_check": {"ok": False, "expected_pages": 10, "actual_pages": 12,
                               "preview_pdf": str(pdf)}}
    (scratch / "sample.report.json").write_text(json.dumps(report))
    published = tmp_path / "data" / "optimized" / "U3" / batch / "sample.tex"
    published.parent.mkdir(parents=True)
    published.write_bytes(work.read_bytes())
    db_path = root / ".state" / "pipeline.sqlite3"
    db_path.parent.mkdir()
    finish = datetime.fromisoformat(completed).timestamp()
    start = datetime.fromisoformat("2026-09-06T15:59:00+00:00").timestamp()
    remote = f"/data/workers/{batch}/.pipeline/sample/sample.optimized.tex"
    metadata = json.dumps({"outputs": {remote: hashlib.sha256(work.read_bytes()).hexdigest()}})
    with sqlite3.connect(db_path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO jobs(stage,source_path,fingerprint,status,output_path,started_at,completed_at,updated_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("optimise", f"/input/U3/{batch}/sample.pdf", "fp", "completed", remote,
                    start, finish, finish, metadata))
    logs(scratch, [event(batch, usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
                   event(batch + "-error", attempt=2, status="error")])
    return db_path, scratch, published


NOW = datetime(2026, 9, 7, 14, tzinfo=ZONE)


def test_completed_output_counts_once_and_retains_missing_usage(tmp_path):
    fixture_job(tmp_path)
    config = config_for(tmp_path)
    first = poll(config, NOW)
    row = first["days"][0]
    assert (row["completed_pdfs"], row["source_pages"], row["pdf_pages"]) == (1, 10, 12)
    assert row["total_tokens"] == 120
    assert row["missing_usage_calls"] == row["retries"] == 1
    assert row["documents"][0]["batch"] == "BATCH1"
    assert row["documents"][0]["pdf_name"] == "sample.pdf"
    assert row["date"] == "2026-09-07"
    assert row["elapsed_seconds"] > 10 * 3600
    assert poll(config, NOW)["days"] == first["days"]
    ledger = tmp_path / "data/monitoring/daily/completions.jsonl"
    assert len(ledger.read_text().splitlines()) == 1


def test_unpublished_or_hash_mismatch_is_not_complete(tmp_path):
    _, _, published = fixture_job(tmp_path)
    config = config_for(tmp_path)
    published.write_text("older export")
    result = poll(config, NOW)
    assert result["days"][0]["completed_pdfs"] == 0
    assert "校验值不一致" in result["warnings"][0]
    published.unlink()
    assert poll(config, NOW)["days"][0]["completed_pdfs"] == 0


def test_placeholder_output_is_excluded_from_completed_page_totals(tmp_path):
    db_path, scratch, published = fixture_job(tmp_path)
    tex = "% LEXOID_RECOGNITION_FALLBACK\n\\null\n% LEXOID_PAGE_COMPLETED: 1/1\n"
    work = scratch / "sample.optimized.tex"
    work.write_text(tex)
    published.write_text(tex)
    with sqlite3.connect(db_path) as db:
        remote = "/data/workers/BATCH1/.pipeline/sample/sample.optimized.tex"
        db.execute("UPDATE jobs SET metadata_json=?", (json.dumps({
            "outputs": {remote: hashlib.sha256(work.read_bytes()).hexdigest()}}),))
    result = poll(config_for(tmp_path), NOW)
    assert result["days"][0]["completed_pdfs"] == 0
    assert any("占位" in warning for warning in result["warnings"])


def test_running_job_with_pdf_is_not_counted_and_utc_day_is_converted(tmp_path):
    db_path, _, _ = fixture_job(tmp_path, completed="2026-09-06T16:01:00+00:00")
    config = config_for(tmp_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE jobs SET status='running'")
    assert poll(config, NOW)["days"][0]["completed_pdfs"] == 0
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE jobs SET status='completed'")
    row = poll(config, NOW)["days"][0]
    assert row["date"] == "2026-09-07"
    assert row["completed_pdfs"] == 1
    assert row["documents"][0]["completed_at"] == "2026-09-07T00:01:00+08:00"


def test_same_filename_in_different_batches_and_zero_day_rollover(tmp_path):
    fixture_job(tmp_path)
    fixture_job(tmp_path, "BATCH2")
    config = config_for(tmp_path)
    first = poll(config, NOW)
    assert first["days"][0]["completed_pdfs"] == 2
    assert first["days"][0]["total_tokens"] == 240
    second = poll(config, datetime(2026, 9, 9, 0, 1, tzinfo=ZONE))
    assert [row["completed_pdfs"] for row in second["days"]] == [2, 0, 0]
    assert not second["days"][0]["provisional"]
    assert second["days"][-1]["provisional"]


def test_ledger_survives_worker_artifact_removal(tmp_path):
    db, scratch, published = fixture_job(tmp_path)
    config = config_for(tmp_path)
    first = poll(config, NOW)
    db.unlink()
    published.unlink()
    (scratch / "recognize.process.calls.jsonl").unlink()
    assert poll(config, NOW)["days"] == first["days"]


def test_new_completion_does_not_charge_previous_calls_again(tmp_path):
    db_path, scratch, _ = fixture_job(tmp_path)
    config = config_for(tmp_path)
    poll(config, NOW)
    later = datetime(2026, 9, 8, 10, tzinfo=ZONE)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE jobs SET started_at=?,completed_at=?", (later.timestamp() - 100, later.timestamp()))
    new_event = event("new-call", at=later.isoformat(), usage={"input_tokens": 40, "output_tokens": 10})
    with (scratch / "recognize.process.calls.jsonl").open("a") as stream:
        stream.write(json.dumps(new_event) + "\n")
    result = poll(config, later)
    assert [row["total_tokens"] for row in result["days"]] == [120, 50]
    assert result["days"][1]["elapsed_seconds"] == 100


def test_duplicate_partial_and_future_call_logs(tmp_path):
    successful = event("a", usage={"input_tokens": 10, "output_tokens": 2})
    logs(tmp_path, [successful, successful,
                    {"event": "start", "call_id": "killed", "started_at": NOW.isoformat()},
                    event("future", at="2026-09-08T00:00:00+00:00")])
    with (tmp_path / "recognize.process.calls.jsonl").open("a") as stream:
        stream.write('{"event":')
    stats, ids, _ = call_summary(tmp_path, NOW.timestamp(), set())
    assert stats["calls"] == 1
    assert stats["total_tokens"] == 12
    assert stats["unmatched_starts"] == stats["malformed_lines"] == 1
    assert set(ids) == {"a", "killed"}


def test_missing_stage_log_is_explicit(tmp_path):
    stats, _, _ = call_summary(tmp_path, NOW.timestamp(), set())
    assert stats["missing_stage_logs"] == ["recognize", "reconcile", "optimise"]


def test_scheduler_persists_and_does_not_stop_with_workers(tmp_path):
    config = config_for(tmp_path)
    with patch("pathlib.Path.home", return_value=tmp_path), patch("subprocess.run") as run:
        install(tmp_path / "config.json", config)
    job = plistlib.loads((tmp_path / "Library/LaunchAgents/com.lexiod.daily-test.plist").read_bytes())
    assert job["StartInterval"] == 600
    assert job["RunAtLoad"] is True
    assert "--scheduled" not in job["ProgramArguments"]
    assert run.call_args.args[0][:2] == ["launchctl", "bootstrap"]
