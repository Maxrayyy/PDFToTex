import json
import subprocess
import plistlib
import sqlite3
import hashlib
import pytest
from unittest.mock import patch

from .worker_watch import install, probe_target, read_new_lines, summarize_calls, render_report


@pytest.mark.parametrize("message", ["error: no such object: worker-a",
                                    "Error: No such container: worker-a"])
def test_deleted_container_is_not_a_docker_connection_error(tmp_path, message):
    with patch("subprocess.run", return_value=subprocess.CompletedProcess(
            ["docker"], 1, stdout="", stderr=message)):
        result = probe_target(target(tmp_path), "docker", {}, 1000)
    assert result["status"] == "missing"
    assert result["terminal"]
    assert "docker_unavailable" not in result["alerts"]


@pytest.mark.parametrize("status,exit_code,completed,alerts,hidden", [
    ("exited", 0, True, [], True),
    ("missing", None, True, ["container_missing"], True),
    ("running", 0, True, [], False),
    ("monitor_error", None, True, ["docker_unavailable"], False),
    ("exited", 1, True, ["container_failed"], False),
    ("exited", 0, False, [], False),
    ("missing", None, False, ["container_missing"], False),
    ("exited", 0, True, ["invalid_published_tex"], False),
])
def test_finished_container_keeps_only_pdf_list(status, exit_code, completed, alerts, hidden):
    item = {"name": "worker-a", "stem": "sample", "pages": 1,
            "status": status, "exit_code": exit_code, "published": True,
            "alerts": alerts, "issues": [{"code": "old_warning"}],
            "progress": {"recognize": {"completed": True}},
            "queue": {"total": 1, "completed": int(completed), "jobs": [
                {"pdf": "sample.pdf", "completed": completed,
                 "status": "done" if completed else "pending"}]}}
    report = render_report({"checked_at": "now", "containers": [item], "all_finished": False})
    assert ("| worker-a |" not in report) == hidden
    assert ("worker-a 后续进度" not in report) == hidden
    assert ("old_warning" not in report) == hidden
    assert "`sample.pdf` |" in report
    assert "## PDF 转译列表\n\n### worker-a" in report


def test_log_cursor_ignores_old_lines_and_waits_for_partial_record(tmp_path):
    path = tmp_path / "calls.jsonl"
    cursors = {}
    path.write_bytes(b'one\ntwo')
    assert read_new_lines(path, cursors) == ["one"]
    assert read_new_lines(path, cursors) == []
    with path.open("ab") as stream:
        stream.write(b"\nthree\n")
    assert read_new_lines(path, cursors) == ["two", "three"]
    path.write_bytes(b"new\n")
    assert read_new_lines(path, cursors) == ["new"]


def test_call_summary_distinguishes_errors_warnings_and_retries():
    stats = {}
    events = [
        {"event": "finish", "status": "ok", "error_type": None},
        {"event": "validation_warning", "code": "visual_table_row_mismatch"},
        {"event": "finish", "status": "error", "error_type": "TimeoutError", "page": 2},
        {"event": "start", "attempt": 2, "page": 2},
    ]
    issues = summarize_calls([json.dumps(e) for e in events], stats)
    assert stats["request_errors"] == 1
    assert stats["warnings"] == 1
    assert stats["retries"] == 1
    assert any(issue.get("error_type") == "TimeoutError" for issue in issues)


def target(tmp_path):
    return {"name": "worker-a", "work_root": str(tmp_path / "work"),
            "stem": "sample", "pages": 10, "output_tex": str(tmp_path / "sample.tex")}


def test_successful_exit_without_published_tex_is_reported(tmp_path):
    inspector = lambda *_: {"Id": "id", "State": {"Status": "exited", "ExitCode": 0}}
    result = probe_target(target(tmp_path), "docker", {}, 1000, inspector=inspector)
    assert result["terminal"]
    assert "missing_published_tex" in result["alerts"]
    (tmp_path / "sample.tex").write_text("complete")
    result = probe_target(target(tmp_path), "docker", {}, 1000, inspector=inspector)
    assert result["alerts"] == []


def test_placeholder_publication_is_not_reported_as_success(tmp_path):
    spec = target(tmp_path)
    (tmp_path / "sample.tex").write_text("% LEXOID_RECOGNITION_FALLBACK\n\\null")
    inspector = lambda *_: {"Id": "id", "State": {"Status": "exited", "ExitCode": 0}}
    result = probe_target(spec, "docker", {}, 1000, inspector=inspector)
    assert not result["published"]
    assert "invalid_published_tex" in result["alerts"]


def test_docker_unavailable_does_not_mean_tasks_finished(tmp_path):
    def inspector(*_):
        raise subprocess.TimeoutExpired("docker", 15)
    result = probe_target(target(tmp_path), "docker", {}, 1000, inspector=inspector)
    assert not result["terminal"]
    assert result["status"] == "monitor_error"


def test_running_worker_reports_only_new_log_errors(tmp_path):
    spec = target(tmp_path)
    logs = tmp_path / "work" / ".pipeline" / "sample"
    logs.mkdir(parents=True)
    (logs / "recognize.process.calls.jsonl").write_text(
        json.dumps({"event": "finish", "status": "error", "error_type": "TimeoutError"}) + "\n")
    (logs / "recognize.process.log").write_text(
        '[LLM_CALL] {"status":"error"}\n[WARNING] advisory\n[ERROR] compile failed\n')
    inspector = lambda *_: {"Id": "id", "State": {"Status": "running"}}
    saved = {}
    first = probe_target(spec, "docker", saved, 1000, inspector=inspector)
    second = probe_target(spec, "docker", saved, 1001, inspector=inspector)
    assert first["new_counts"]["request_errors"] == 1
    assert first["new_counts"]["process_errors"] == 1
    assert second["new_counts"]["request_errors"] == 0
    assert second["new_counts"]["process_errors"] == 0


def test_oom_exit_is_reported(tmp_path):
    inspector = lambda *_: {"Id": "id", "State": {
        "Status": "exited", "ExitCode": 137, "OOMKilled": True}}
    result = probe_target(target(tmp_path), "docker", {}, 1000, inspector=inspector)
    assert {"container_failed", "oom_killed"} <= set(result["alerts"])


def test_schedule_runs_immediately_and_every_ten_minutes(tmp_path):
    config = {"output_dir": str(tmp_path), "launchd_label": "com.lexiod.test",
              "interval_seconds": 600}
    with patch("subprocess.run") as run:
        install(tmp_path / "config.json", config)
    job = plistlib.loads((tmp_path / "com.lexiod.test.plist").read_bytes())
    assert job["StartInterval"] == 600
    assert job["RunAtLoad"] is True
    assert "--scheduled" in job["ProgramArguments"]
    assert run.call_args.args[0][0:2] == ["launchctl", "bootstrap"]


def progress_probe(tmp_path, stage, content, saved=None):
    spec = target(tmp_path)
    logs = tmp_path / "work" / ".pipeline" / "sample"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{stage}.process.log").write_text(content)
    inspector = lambda *_: {"Id": "id", "State": {"Status": "running"}}
    return probe_target(spec, "docker", saved if saved is not None else {}, 1000,
                        inspector=inspector)


def call(**event):
    return "[LLM_CALL] " + json.dumps(event) + "\n"


def test_reconcile_progress_deduplicates_retries_and_resets_new_attempt(tmp_path):
    content = "reconcile started\n" + call(
        event="finish", stage="reconcile", status="ok", field_id="old", page=9)
    content += "reconcile started\n" + call(
        event="reconcile_selection", selected=3, deferred=8)
    for status in ("error", "ok", "ok"):
        content += call(event="finish", stage="reconcile", status=status,
                        field_id="f1", page=2)
    content += call(event="start", stage="reconcile", field_id="f2", page=3)
    content += '[LLM_CALL] {"event":'  # An in-progress log write.
    saved = {}
    first = progress_probe(tmp_path, "reconcile", content, saved)
    second = progress_probe(tmp_path, "reconcile", content, saved)
    progress = first["progress"]["reconcile"]
    assert progress["returned_fields"] == 1
    assert progress["selected"] == 3
    assert progress["page"] == 3
    assert progress["deferred"] == 8
    assert second["progress"] == first["progress"]


def test_completed_reconcile_uses_summary_including_cached_fields(tmp_path):
    content = "reconcile started\n" + call(event="reconcile_selection", selected=3)
    content += ('2026-09-07T07:17:10+00:00 [INFO] [RECONCILE_FINISH] done | '
                '{"selected":3,"confirmed":2,"failed":0,"deferred":8,"needs_review":9}\n')
    item = progress_probe(tmp_path, "reconcile", content)
    progress = item["progress"]["reconcile"]
    assert progress["completed"] is True
    assert progress["selected"] == 3
    assert progress["returned_fields"] == 0
    report = render_report({"checked_at": "now", "containers": [item], "all_finished": False})
    assert "已完成" in report and "确认 2" in report
    assert "reconcile.process.log" in report


def test_optimizer_tracks_position_separately_from_returned_tables_and_compile(tmp_path):
    content = "optimise started\n[5/7] Transforming and annotating fields...\n"
    content += "  Transformed 448 table(s), 16210 cell(s)\n"
    for _ in range(2):
        content += call(event="finish", stage="naming", status="ok", page=70, table=145)
    item = progress_probe(tmp_path, "optimise", content)
    progress = item["progress"]["optimise"]
    assert progress["substep"] == "语义命名"
    assert progress["table"] == 145 and progress["returned_tables"] == 1
    assert progress["tables"] == 448
    content += ('2026-09-07T07:20:00+00:00 [INFO] [FIELD_SCAN] done | {"fields":500}\n'
                '[6/7] Writing output...\n'
                '2026-09-07T07:20:01+00:00 [INFO] [COMPILE_CHECK_START] check | {"engine":"xelatex"}\n')
    progress = progress_probe(tmp_path, "optimise", content)["progress"]["optimise"]
    assert progress["substep"] == "XeLaTeX 编译检查"
    assert "page" not in progress and "table" not in progress
    content += ('2026-09-07T07:20:02+00:00 [INFO] [COMPILE_CHECK_FINISH] done | {"ok":false}\n')
    progress = progress_probe(tmp_path, "optimise", content)["progress"]["optimise"]
    assert progress["compile_ok"] is False
    assert not progress.get("completed")


def test_stage_elapsed_stops_at_completion(tmp_path):
    state_dir = tmp_path / "work" / ".state"
    state_dir.mkdir(parents=True)
    with sqlite3.connect(state_dir / "pipeline.sqlite3") as db:
        db.execute("CREATE TABLE jobs (id INTEGER, stage TEXT, status TEXT, started_at REAL, "
                   "completed_at REAL, updated_at REAL)")
        db.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)", [
            (1, "reconcile", "completed", 100, 200, 200),
            (2, "optimise", "running", 200, None, 200)])
    item = progress_probe(tmp_path, "optimise", "optimise started\n")
    assert item["stage"] == "optimise"
    assert item["progress"]["reconcile"]["elapsed_seconds"] == 100
    assert item["progress"]["optimise"]["elapsed_seconds"] == 800


def test_opt_in_publication_recovery_requires_completed_matching_output(tmp_path):
    spec = target(tmp_path)
    spec["recover_publish_from"] = str(tmp_path / "misplaced.tex")
    misplaced = tmp_path / "misplaced.tex"
    misplaced.write_bytes(b"container-produced tex\n")
    work = tmp_path / "work"
    scratch = work / ".pipeline" / "sample" / "sample.optimized.tex"
    scratch.parent.mkdir(parents=True)
    scratch.write_bytes(misplaced.read_bytes())
    digest = hashlib.sha256(misplaced.read_bytes()).hexdigest()
    manifest = {"files": [{"source": "sample.pdf", "status": "done", "stages": [
        {"stage": "optimise", "outputs": {"/data/work/.pipeline/sample/sample.optimized.tex": digest}}]}]}
    (work / "manifest.json").write_text(json.dumps(manifest))
    running = lambda *_: {"State": {"Status": "running"}}
    stopped = lambda *_: {"State": {"Status": "exited", "ExitCode": 0}}
    assert not probe_target(spec, "docker", {}, 1000, inspector=running)["published"]
    misplaced.write_bytes(b"unrelated old result")
    assert not probe_target(spec, "docker", {}, 1000, inspector=stopped)["published"]
    assert misplaced.exists()
    misplaced.write_bytes(scratch.read_bytes())
    result = probe_target(spec, "docker", {}, 1000, inspector=stopped)
    assert result["published"]
    assert result["publication_recovered"] is True
    assert (tmp_path / "sample.tex").read_bytes() == scratch.read_bytes()
    assert not misplaced.exists()
    assert probe_target(spec, "docker", {}, 1000, inspector=stopped)["published"]


def test_monitor_tracks_page_checks_and_model_upgrades_without_double_counting(tmp_path):
    content = "recognize started\n"
    for page in (1, 2, 2):
        content += call(event="page_check", stage="recognize", page=page, total=3)
    content += call(event="page_model_upgrade", stage="recognize", page=1, model="gpt-6-astra")
    content += call(event="page_upgrade_finish", stage="recognize", page=1,
                    action="use_fallback_tex", model="gpt-6-astra")
    item = progress_probe(tmp_path, "recognize", content)
    progress = item["progress"]["recognize"]
    assert progress["checked_pages"] == 2
    assert progress["upgraded_pages"] == [1]
    report = render_report({"checked_at": "now", "containers": [item], "all_finished": False})
    assert "本地已检查 2/3 页" in report
    assert "GPT-6" in report


def test_queue_report_keeps_order_and_uses_completion_records(tmp_path, monkeypatch):
    from . import worker_watch as watch

    queue_dir = tmp_path / "queues"
    queue_dir.mkdir()
    data = {"container": "worker-a", "jobs": [
        {"source": "/input/first.pdf", "status": "done", "exit_code": 0},
        {"source": "/input/current.pdf", "status": "running"},
        {"source": "/input/last.pdf", "status": "pending"}],
        "active_source": "/input/current.pdf"}
    path = queue_dir / "arbitrary-queue-name.status.json"
    path.write_text(json.dumps(data))
    (queue_dir / "other.status.json").write_text(json.dumps({
        "container": "worker-b", "jobs": [{"source": "unrelated.pdf", "status": "done"}]}))
    config = {"docker": "docker", "output_dir": str(tmp_path / "monitor"),
              "queue_dir": str(queue_dir), "containers": [target(tmp_path)]}
    monkeypatch.setattr(watch, "inspect_container", lambda *_:
                        {"Id": "id", "State": {"Status": "running"}})
    watch.poll(config)
    report = (tmp_path / "monitor" / "latest.md").read_text()
    assert "已完成 1/3" in report
    assert "`first.pdf` | 已完成" in report
    assert "`current.pdf` | 未完成（处理中）" in report
    assert "`last.pdf` | 未完成" in report
    assert report.index("first.pdf") < report.index("current.pdf") < report.index("last.pdf")
    assert "unrelated.pdf" not in report

    data["jobs"][1].update(status="done", exit_code=0)
    data["jobs"][2]["status"] = "running"
    data["active_source"] = "/input/last.pdf"
    path.write_text(json.dumps(data))
    watch.poll(config)
    report = (tmp_path / "monitor" / "latest.md").read_text()
    assert "已完成 2/3" in report
    assert "`current.pdf` | 已完成" in report
    assert "`last.pdf` | 未完成（处理中）" in report


def test_paused_queue_and_unreadable_status_are_not_reported_as_complete(tmp_path, monkeypatch):
    from . import worker_watch as watch

    queue_dir = tmp_path / "queues"
    queue_dir.mkdir()
    path = queue_dir / "queue.status.json"
    path.write_text(json.dumps({"container": "worker-a", "status": "paused", "jobs": [
        {"source": "/input/failed.pdf", "status": "failed", "exit_code": 1},
        {"source": "/input/pending.pdf", "status": "pending"}]}))
    config = {"docker": "docker", "output_dir": str(tmp_path / "monitor"),
              "queue_dir": str(queue_dir), "containers": [target(tmp_path)]}
    monkeypatch.setattr(watch, "inspect_container", lambda *_:
                        {"Id": "id", "State": {"Status": "exited", "ExitCode": 1}})
    watch.poll(config)
    report = (tmp_path / "monitor" / "latest.md").read_text()
    assert "已完成 0/2" in report
    assert "`failed.pdf` | 未完成（已暂停）" in report
    assert "`pending.pdf` | 未完成" in report
    path.write_text("{broken json")
    watch.poll(config)
    report = (tmp_path / "monitor" / "latest.md").read_text()
    assert "队列状态暂不可用" in report
