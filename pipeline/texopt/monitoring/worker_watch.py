"""Worker monitoring with optional verified publication recovery via macOS launchd."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time


COUNTS = ("requests", "finished_calls", "request_errors", "retries", "warnings",
          "validation_events", "process_errors")


def read_new_lines(path, cursors):
    path = Path(path)
    if not path.exists():
        return []
    stat = path.stat()
    prior = cursors.get(str(path), {})
    offset = prior.get("offset", 0)
    if prior.get("inode") != stat.st_ino or stat.st_size < offset:
        offset = 0
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read()
    end = data.rfind(b"\n") + 1
    cursors[str(path)] = {"inode": stat.st_ino, "offset": offset + end}
    return data[:end].decode("utf-8", errors="replace").splitlines()


def summarize_calls(lines, stats):
    issues = []
    for line in lines:
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("not an object")
        except ValueError:
            stats["warnings"] = stats.get("warnings", 0) + 1
            issues.append({"code": "malformed_call_log"})
            continue
        kind = event.get("event")
        increments = Counter()
        if kind == "start":
            increments["requests"] = 1
            increments["retries"] = int(event.get("attempt", 1) > 1)
        elif kind == "finish":
            increments["finished_calls"] = 1
            increments["request_errors"] = int(event.get("status") == "error")
        elif kind == "validation_warning":
            increments["warnings"] = 1
        elif kind == "validation_or_request_error":
            increments["validation_events"] = 1
        for key, amount in increments.items():
            stats[key] = stats.get(key, 0) + amount
        if event.get("page") is not None:
            stats["latest_page"] = event["page"]
        if (event.get("status") == "error" or kind in
                ("validation_warning", "validation_or_request_error")):
            issues.append({key: event[key] for key in
                ("event", "stage", "page", "field_id", "error_type", "http_status", "code", "action")
                if key in event})
    return issues


def inspect_container(docker, name):
    result = subprocess.run(
        [docker, "inspect", "--format", '{"Id":{{json .Id}},"State":{{json .State}}}', name],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        error = result.stderr.lower()
        if "no such object" in error or "no such container" in error:
            return {"State": {"Status": "missing"}}
        raise RuntimeError("Docker inspection failed")
    return json.loads(result.stdout)


def pipeline_jobs(work_root):
    path = Path(work_root) / ".state" / "pipeline.sqlite3"
    if not path.exists():
        return {}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT stage, status, started_at, completed_at, updated_at "
                          "FROM jobs ORDER BY updated_at DESC, id DESC").fetchall()
    jobs = {}
    for row in rows:
        jobs.setdefault(row["stage"], dict(row))
    return jobs


def pipeline_stage(work_root):
    return next(iter(pipeline_jobs(work_root)), "pending")


def stage_progress(path, stage, job, now):
    progress = {"returned_fields": 0} if stage == "reconcile" else {"returned_tables": 0}
    returned = set()
    checked, upgraded, unresolved = set(), set(), set()
    steps = {"1": "读取 TEX", "2": "表格结构检查", "3": "探测列宽",
             "4": "转换表格", "5": "转换与标注字段", "6": "写入 TEX", "7": "生成报告"}
    events = {"OPT_START": "启动优化", "PROBE_START": "探测列宽",
              "TABLE_CONVERTED": "转换表格", "FIELD_SCAN": "字段标注完成",
              "SYNTAX_OK": "语法检查通过", "SYNTAX_BLOCKED": "语法检查受阻",
              "OUTPUT_WRITE": "写入 TEX", "COMPILE_CHECK_START": "XeLaTeX 编译检查",
              "LAYOUT_RETRY": "调整布局并重新编译"}

    def substep(label):
        if progress.get("substep") != label:
            for key in ("page", "table", "field_id"):
                progress.pop(key, None)
        progress["substep"] = label

    # Re-read only stage logs so upgrading the monitor backfills progress without
    # moving incremental error cursors. Attempts are delimited by the runner.
    if path.exists():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    continue
                if line.strip() == f"{stage} started":
                    progress.clear()
                    returned.clear()
                    checked.clear()
                    upgraded.clear()
                    unresolved.clear()
                    continue
                step = re.match(r"\[([1-7])/7\]", line)
                if step and stage == "optimise":
                    substep(steps[step[1]])
                tables = re.search(r"Transformed (\d+) table\(s\), (\d+) cell\(s\)", line)
                if tables:
                    progress.update(tables=int(tables[1]), cells=int(tables[2]))
                code = re.search(r"\[(?:INFO|WARNING|ERROR)\] \[([^]]+)\]", line)
                marker = "[LLM_CALL] " if "[LLM_CALL] " in line else " | "
                if marker not in line:
                    continue
                try:
                    event = json.loads(line.split(marker, 1)[1])
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if marker == "[LLM_CALL] ":
                    kind = event.get("event")
                    if stage == "recognize":
                        page = event.get("page")
                        if kind == "page_check":
                            checked.add(page)
                            progress["check_total"] = event.get("total")
                            substep("视觉识别与本地检查")
                        elif kind == "page_model_upgrade":
                            substep("问题页升级 GPT-6")
                        elif kind == "page_upgrade_finish":
                            if event.get("action") == "use_fallback_tex":
                                upgraded.add(page)
                            else:
                                unresolved.add(page)
                            substep("问题页检查完成")
                        if page is not None and kind in (
                                "page_check", "page_model_upgrade", "page_upgrade_finish"):
                            progress["page"] = page
                        continue
                    if kind == "reconcile_selection" and stage == "reconcile":
                        progress.update({key: event[key] for key in
                                         ("selected", "deferred", "flagged") if key in event})
                        substep("字段协调")
                    if event.get("stage") not in ("reconcile", "naming"):
                        continue
                    if kind not in ("start", "finish"):
                        continue
                    substep("字段协调" if stage == "reconcile" else "语义命名")
                    for key in ("page", "table", "field_id"):
                        if event.get(key) is not None:
                            progress[key] = event[key]
                    identity = (event.get("page"), event.get("field_id") if stage == "reconcile"
                                else event.get("table"))
                    if kind == "finish" and event.get("status") == "ok" and identity[1] is not None:
                        returned.add(identity)
                elif code:
                    if code[1] == "RECONCILE_FINISH":
                        progress.update({key: event[key] for key in
                                         ("selected", "confirmed", "deferred", "failed", "needs_review")
                                         if key in event})
                        progress["completed"] = True
                        substep("已完成")
                    elif code[1] == "COMPILE_CHECK_FINISH":
                        progress["compile_ok"] = event.get("ok")
                        substep("编译检查通过" if event.get("ok") else "编译检查未通过")
                    elif code[1] in events:
                        substep(events[code[1]])
                        if code[1] == "LAYOUT_RETRY" and event.get("page") is not None:
                            progress["page"] = event["page"]
        progress["log"] = str(path)
    if stage == "recognize":
        progress.update(checked_pages=len(checked), upgraded_pages=sorted(upgraded),
                        unresolved_pages=sorted(unresolved))
    else:
        progress["returned_fields" if stage == "reconcile" else "returned_tables"] = len(returned)
    if job:
        progress["status"] = job["status"]
        if job["started_at"] is not None:
            end = (job["completed_at"] if job["status"] == "completed" else
                   job["updated_at"] if job["status"] != "running" else now)
            progress["elapsed_seconds"] = max(0, round((end or now) - job["started_at"]))
        if job["status"] == "completed":
            progress["completed"] = True
            substep("已完成")
    return progress


def progress_text(stage, progress):
    detail = [progress.get("substep", "等待阶段日志")]
    if stage == "recognize":
        detail.append(f"本地已检查 {progress.get('checked_pages', 0)}/{progress.get('check_total') or '?'} 页")
        detail.append(f"GPT-6 已替换页：{progress.get('upgraded_pages', [])}")
        if progress.get("unresolved_pages"):
            detail.append(f"仍需处理页：{progress['unresolved_pages']}")
    elif stage == "reconcile":
        if progress.get("completed"):
            detail.append(f"已处理 {progress.get('selected', '未知')} 个选中字段")
            if "confirmed" in progress:
                detail.append(f"确认 {progress['confirmed']}，仍待人工核对 {progress.get('needs_review', '未知')}，"
                              f"失败 {progress.get('failed', '未知')}")
        else:
            detail.append(f"模型已返回 {progress.get('returned_fields', 0)}/{progress.get('selected', '?')} 个字段")
        if "deferred" in progress:
            detail.append(f"跳过自动复核 {progress['deferred']} 个字段")
    elif stage == "optimise":
        if "tables" in progress:
            detail.append(f"已转换 {progress['tables']} 张表")
        if progress.get("returned_tables"):
            detail.append(f"命名模型已返回 {progress['returned_tables']} 张表结果")
    if "page" in progress:
        detail.append(f"最近活动：源第 {progress['page']} 页")
    if "table" in progress:
        detail.append(f"表编号 {progress['table']}")
    if "elapsed_seconds" in progress:
        minutes, seconds = divmod(progress["elapsed_seconds"], 60)
        detail.append(f"阶段耗时 {minutes} 分 {seconds} 秒")
    if progress.get("status") not in (None, "running", "completed"):
        detail.append(f"阶段状态 {progress['status']}")
    return "；".join(detail)


def cached_pages(work_root):
    root = Path(work_root) / ".cache" / "recognition" / "draft"
    generations = [path for path in root.glob("*") if path.is_dir()]
    if not generations:
        return 0
    current = max(generations, key=lambda path: path.stat().st_mtime)
    return sum(path.stem.isdigit() and path.with_suffix(".tex").is_file()
               and not has_recognition_placeholder(path.with_suffix(".tex"))
               for path in current.glob("*.json"))


def has_recognition_placeholder(path):
    return bool(re.search(r"(?m)^\s*%\s*LEXOID_RECOGNITION_FALLBACK\b",
                          Path(path).read_text("utf-8")))


def recover_publication(target):
    source = Path(target["recover_publish_from"])
    destination = Path(target["output_tex"])
    if destination.exists() or not source.is_file():
        return False
    work = Path(target["work_root"])
    stem = target["stem"]
    manifest_path = work / "manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text())
    entries = [entry for entry in manifest.get("files", [])
               if entry.get("status") == "done" and Path(entry["source"]).stem == stem]
    if len(entries) != 1:
        return False
    hashes = [digest for stage in entries[0].get("stages", []) if stage["stage"] == "optimise"
              for path, digest in stage.get("outputs", {}).items()
              if Path(path).name == f"{stem}.optimized.tex"]
    scratch = work / ".pipeline" / stem / f"{stem}.optimized.tex"
    if len(hashes) != 1 or not scratch.is_file():
        return False
    if any(hashlib.sha256(path.read_bytes()).hexdigest() != hashes[0] for path in (source, scratch)):
        raise ValueError("Published file does not match completed optimizer output")
    if has_recognition_placeholder(source):
        raise ValueError("Missing recognition content; publication recovery refused")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Hard-link first so an existing destination can never be overwritten.
    os.link(source, destination)
    source.unlink()
    return True


def probe_target(target, docker, saved, now, *, inspector=None, stale_seconds=1200):
    result = {"name": target["name"], "stem": target["stem"], "pages": target["pages"], "alerts": [],
              "terminal": False, "status": "monitor_error", "issues": []}
    try:
        info = (inspector or inspect_container)(docker, target["name"])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result.update(alerts=["docker_unavailable"], monitor_error=type(exc).__name__)
        return result
    state = info["State"]
    status = state["Status"]
    result.update(status=status, container_id=info.get("Id"), exit_code=state.get("ExitCode"),
                  started_at=state.get("StartedAt"), finished_at=state.get("FinishedAt"),
                  terminal=status in ("exited", "dead", "missing"))
    if state.get("OOMKilled"):
        result["alerts"].append("oom_killed")
    if (status == "dead" or (status == "exited" and state.get("ExitCode") != 0)):
        result["alerts"].append("container_failed")
    if status == "missing":
        result["alerts"].append("container_missing")
    if state.get("Health", {}).get("Status") == "unhealthy":
        result["alerts"].append("unhealthy")
    output = Path(target["output_tex"])
    if status == "exited" and state.get("ExitCode") == 0 and target.get("recover_publish_from"):
        try:
            result["publication_recovered"] = recover_publication(target)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["alerts"].append("publication_recovery_failed")
            result["publication_recovery_error"] = type(exc).__name__
    result["published"] = output.is_file() and output.stat().st_size > 0
    if result["published"]:
        try:
            if has_recognition_placeholder(output):
                result["published"] = False
                result["alerts"].append("invalid_published_tex")
        except (OSError, UnicodeError):
            result["published"] = False
            result["alerts"].append("invalid_published_tex")
    if status == "exited" and state.get("ExitCode") == 0 and not result["published"]:
        result["alerts"].append("missing_published_tex")

    stats = saved.setdefault("totals", {})
    before = dict(stats)
    cursors = saved.setdefault("cursors", {})
    logs = Path(target["work_root"]) / ".pipeline" / target["stem"]
    activity = saved.setdefault("first_seen", now)
    try:
        jobs = pipeline_jobs(target["work_root"])
        result["stage"] = next(iter(jobs), "pending")
        result["progress"] = {
            stage: stage_progress(logs / f"{stage}.process.log", stage, jobs.get(stage), now)
            for stage in ("recognize", "reconcile", "optimise")
            if stage in jobs or (logs / f"{stage}.process.log").exists()
        }
        result["cached_pages"] = cached_pages(target["work_root"])
        for path in sorted(logs.glob("*.process.*")):
            if path.suffix not in (".jsonl", ".log"):
                continue
            activity = max(activity, path.stat().st_mtime)
            lines = read_new_lines(path, cursors)
            if path.suffix == ".jsonl":
                result["issues"].extend(summarize_calls(lines, stats))
            else:
                for line in lines:
                    if "[LLM_CALL]" in line:
                        continue
                    if "[ERROR]" in line or line.startswith(("ERROR:", "FAILED ", "Traceback (")):
                        stats["process_errors"] = stats.get("process_errors", 0) + 1
                        code = re.search(r"\[ERROR\]\s*\[([^]]+)\]", line)
                        result["issues"].append({"code": code[1] if code else "process_error",
                                                 "log": str(path)})
        if status == "running" and now - activity >= stale_seconds:
            result["alerts"].append("no_recent_progress")
    except (OSError, sqlite3.Error) as exc:
        result["alerts"].append("progress_read_failed")
        result["monitor_error"] = type(exc).__name__
    result["new_counts"] = {key: stats.get(key, 0) - before.get(key, 0) for key in COUNTS}
    result["totals"] = dict(stats)
    result["latest_page"] = stats.get("latest_page")
    result["activity_age_seconds"] = max(0, round(now - activity))
    result["issues"] = result["issues"][-20:]
    return result


def atomic_write(path, text):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as stream:
        stream.write(text)
        temporary = stream.name
    os.replace(temporary, path)


def _timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def service_pause_reason(target, result):
    if (result["status"] != "exited" or result.get("exit_code") != 1
            or "oom_killed" in result["alerts"]):
        return None
    try:
        started = _timestamp(result["started_at"])
        finished = _timestamp(result["finished_at"])
        if started <= 0 or finished < started:
            return None
        root = Path(target["work_root"])
        manifest = json.loads((root / "manifest.json").read_text())
        entries = [entry for entry in manifest["files"]
                   if Path(entry["source"]).stem == target["stem"] and entry["status"] == "paused"]
        if len(entries) != 1:
            return None
        stage = entries[0]["stages"][-1]["stage"]
        if stage not in ("recognize", "reconcile", "optimise"):
            return None
        log = root / ".pipeline" / target["stem"] / f"{stage}.process.log"
        if log.stat().st_mtime < started:
            return None
        # Match the final exception of this run, never an earlier recoverable error.
        lines = log.read_text("utf-8", errors="replace").rstrip().splitlines()
        match = re.fullmatch(
            r"lexoid\.core\.request_errors\.ModelUnavailableError: Model service unavailable "
            r"at page (\d+): (\w+); progress retained, resume after service recovery", lines[-1])
        if not match:
            return None
        page, error = int(match[1]), match[2]
        status = None
        for line in reversed(lines):
            if line == f"{stage} started":
                break
            if not line.startswith("[LLM_CALL] "):
                continue
            try:
                event = json.loads(line[len("[LLM_CALL] "):])
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get("event") == "model_service_unavailable"
                    and event.get("page") == page):
                status = event.get("http_status")
                break
        if status in (401, 403):
            return None
        transient = {"APIConnectionError", "APITimeoutError", "ConnectionError", "TimeoutError",
                     "ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
                     "InternalError", "InternalServerError", "RateLimitError"}
        if error not in transient and not (status in (408, 429) or
                                          isinstance(status, int) and 500 <= status < 600):
            return None
        return {"page": page, "error_type": error, "http_status": status, "stage": stage}
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None


def auto_restart(target, result, saved, policy, docker, now, persist):
    if not policy.get("enabled"):
        return
    reason = service_pause_reason(target, result)
    if reason is None or not result.get("container_id"):
        return
    document = str(Path(target["work_root"]) / target["stem"])
    record = saved.get("auto_restart", {})
    if record.get("document") != document:
        record = {"document": document, "attempts": 0, "last_attempt_at": 0}
        saved["auto_restart"] = record
    limit = max(0, int(policy.get("max_attempts", 3)))
    cooldown = max(1, int(policy.get("cooldown_seconds", 600)))
    detail = {**reason, "attempts": record["attempts"], "max_attempts": limit}
    result["auto_restart"] = detail
    if record["attempts"] >= limit:
        detail["status"] = "limit_reached"
        return
    result["terminal"] = False
    ready_at = max(_timestamp(result["finished_at"]), record["last_attempt_at"]) + cooldown
    if now < ready_at:
        detail.update(status="waiting", retry_after_seconds=max(1, int(ready_at - now)))
        return
    record.update(attempts=record["attempts"] + 1, last_attempt_at=now,
                  last_error_type=reason["error_type"], status="starting")
    detail.update(attempts=record["attempts"], status="starting")
    # Reserve the budget before asking Docker, even if the monitor is interrupted.
    persist()
    try:
        subprocess.run([docker, "start", result["container_id"]], check=True,
                       capture_output=True, text=True, timeout=30)
        record["status"] = detail["status"] = "started"
        result["status"] = "restarting"
    except (OSError, subprocess.SubprocessError) as exc:
        record["status"] = detail["status"] = "start_failed"
        detail["restart_error_type"] = type(exc).__name__


def queue_progress(directory, name):
    matches = []
    for path in sorted(Path(directory).glob("*.status.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
            if data.get("container") == name:
                matches.append((path, data))
        except (OSError, ValueError, AttributeError):
            continue
    if len(matches) != 1:
        return {"error": "queue_status_unavailable"}
    path, data = matches[0]
    try:
        jobs = [{"pdf": Path(job["source"]).name, "status": job["status"],
                 "completed": job["status"] == "done" and job.get("exit_code") == 0}
                for job in data["jobs"]]
        return {"path": str(path), "jobs": jobs, "total": len(jobs),
                "completed": sum(job["completed"] for job in jobs)}
    except (KeyError, TypeError):
        return {"error": "queue_status_unavailable"}


def render_report(snapshot):
    states = {"running": "运行中", "exited": "已退出", "dead": "异常终止",
              "missing": "容器不存在", "monitor_error": "探测失败", "paused": "已暂停",
              "restarting": "重启中"}
    stages = {"recognize": "识别", "reconcile": "字段协调", "optimise": "优化编译", "pending": "等待启动"}
    alerts = {"oom_killed": "内存不足被终止", "container_failed": "容器异常退出",
              "container_missing": "容器不存在", "unhealthy": "健康检查失败",
              "missing_published_tex": "已退出但没有发布 TEX", "docker_unavailable": "无法连接 Docker",
              "invalid_published_tex": "发布文件包含识别失败占位页或无法读取，不可作为完成结果",
              "publication_recovery_failed": "发布路径归位失败，请检查文件一致性或目录权限",
              "no_recent_progress": "超过20分钟无日志更新", "progress_read_failed": "进度读取失败"}
    lines = [f"# 容器监测\n\n检查时间：{snapshot['checked_at']}\n",
             "| 容器 | 当前pdf | 状态 | 阶段 | 已缓存页 | 新增请求错误 | 新增重试 | 新增流程错误 | TEX已发布 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    notes = []
    details = []
    queues = []
    for item in snapshot["containers"]:
        queue = item.get("queue")
        if queue is not None:
            if queue.get("error"):
                queues.extend([f"\n### {item['name']}\n", "队列状态暂不可用。"])
            else:
                queues.append(f"\n### {item['name']}（已完成 {queue['completed']}/{queue['total']}）\n")
                for job in queue["jobs"]:
                    status_text = "已完成" if job["completed"] else "未完成"
                    if not job["completed"]:
                        if job["status"] == "running":
                            status_text += "（处理中）" if item["status"] == "running" else "（已暂停）"
                        elif job["status"] in ("failed", "paused"):
                            status_text += "（已暂停）"
                    queues.append(f"- `{job['pdf']}` | {status_text}")
        queue_completed = (queue is not None and not queue.get("error")
                           and queue.get("total", 0) > 0
                           and queue["completed"] == queue["total"])
        safely_finished = (
            item["status"] == "missing" and queue_completed
            or item["status"] == "exited" and item.get("exit_code") == 0
            and item.get("published") and (queue is None or queue_completed)
        )
        if safely_finished and not (set(item["alerts"]) - {"container_missing"}):
            continue
        counts = item.get("new_counts", {})
        stage = item.get("stage", "pending")
        pdf_suffix = item.get("stem", "")[-4:] or "-"
        lines.append(f"| {item['name']} | {pdf_suffix} | {states.get(item['status'], item['status'])} "
                     f"| {stages.get(stage, stage)} | {item.get('cached_pages', 0)}/{item['pages']} "
                     f"| {counts.get('request_errors', 0)} | {counts.get('retries', 0)} "
                     f"| {counts.get('process_errors', 0)} | {'是' if item.get('published') else '否'} |")
        for alert in item["alerts"]:
            notes.append(f"- {item['name']}：{alerts[alert]}")
        if item.get("publication_recovered"):
            notes.append(f"- {item['name']}：已校验容器原始 TEX 并归位到指定发布目录")
        restart = item.get("auto_restart")
        if restart:
            actions = {"waiting": f"等待自动重启，至少还需 {restart.get('retry_after_seconds', 0)} 秒",
                       "started": "已请求自动重启，从缓存续跑",
                       "start_failed": "自动重启命令失败，将在冷却后重试",
                       "limit_reached": "自动重启达到上限，需人工检查"}
            notes.append(f"- {item['name']}：{actions.get(restart['status'], restart['status'])}；"
                         f"当前 PDF 自动重启 {restart['attempts']}/{restart['max_attempts']} 次；"
                         f"原因 {restart['error_type']}，源第 {restart['page']} 页")
        for issue in item["issues"]:
            detail = issue.get("error_type") or issue.get("code") or issue.get("event", "未知")
            notes.append(f"- {item['name']}：{detail}，页码 {issue.get('page', '未知')}")
        if item.get("progress"):
            details.append(f"\n### {item['name']} 后续进度\n")
            for phase, progress in item["progress"].items():
                detail = f"- {stages.get(phase, phase)}：{progress_text(phase, progress)}"
                if progress.get("log"):
                    detail += f"。 [阶段日志](<{progress['log']}>)"
                details.append(detail)
    if queues:
        lines.extend(["\n## PDF 转译列表", *queues])
    if details:
        lines.extend(["\n协调计数按字段去重；模型返回不代表字段已确认，缓存命中以阶段结束汇总为准。"
                      "表编号表示文件内位置，各优化步骤耗时不同，不据此估算总百分比。", *details])
    if notes:
        lines.extend(["\n本次新增日志事件及状态提示：\n", *notes])
    if snapshot["all_finished"]:
        lines.append("\n所有监控容器均已退出，且无待执行的自动重启，停止定时监测。")
    return "\n".join(lines) + "\n"


def poll(config):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    with (output / "monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = output / "state.json"
        saved = json.loads(state_path.read_text()) if state_path.exists() else {}
        now = time.time()
        containers = []
        for target in config["containers"]:
            target_state = saved.setdefault(target["name"], {})
            result = probe_target(target, config["docker"], target_state, now)
            auto_restart(target, result, target_state, config.get("auto_restart", {}),
                         config["docker"], now,
                         lambda: atomic_write(state_path, json.dumps(saved, ensure_ascii=False, indent=2)))
            if config.get("queue_dir"):
                result["queue"] = queue_progress(config["queue_dir"], target["name"])
            containers.append(result)
        pending_queue = any(
            any(not job.get("completed", job.get("status") == "done")
                for job in item.get("queue", {}).get("jobs", []))
            for item in containers)
        snapshot = {"checked_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
                    "containers": containers,
                    "all_finished": all(item["terminal"] for item in containers) and not pending_queue}
        encoded = json.dumps(snapshot, ensure_ascii=False)
        atomic_write(output / "latest.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
        atomic_write(output / "latest.md", render_report(snapshot))
        if config.get("batch_status"):
            if __package__:
                from .batch_status import render_batch_report
            else:
                from batch_status import render_batch_report
            settings = config["batch_status"]
            atomic_write(Path(settings["output_file"]), render_batch_report(
                settings, config["queue_dir"], snapshot["checked_at"]))
        with (output / "history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
        atomic_write(state_path, json.dumps(saved, ensure_ascii=False, indent=2))
        print(encoded, flush=True)
        return snapshot["all_finished"]


def install(config_path, config):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    label = config["launchd_label"]
    job = {"Label": label, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
           "once", "--config", str(config_path), "--scheduled"], "RunAtLoad": True,
           "StartInterval": config.get("interval_seconds", 600), "ProcessType": "Background",
           "StandardOutPath": str(output / "runner.log"), "StandardErrorPath": str(output / "runner.error.log")}
    plist_path = output / f"{label}.plist"
    plist_path.write_bytes(plistlib.dumps(job))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], check=True, timeout=15)
    print(f"已启用监测，每 {job['StartInterval']} 秒检查一次。", flush=True)


def main():
    parser = argparse.ArgumentParser(description="定时检查识别容器、增量错误日志及发布结果")
    parser.add_argument("action", choices=("once", "install", "stop"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text())
    if not config.get("containers") or config.get("interval_seconds", 600) <= 0:
        parser.error("监测目标不能为空，间隔必须大于零")
    service = f"gui/{os.getuid()}/{config['launchd_label']}"
    if args.action == "install":
        install(config_path, config)
    elif args.action == "stop":
        subprocess.run(["launchctl", "bootout", service], check=True, timeout=15)
    elif poll(config) and args.scheduled:
        subprocess.run(["launchctl", "bootout", service], timeout=15)


if __name__ == "__main__":
    main()
