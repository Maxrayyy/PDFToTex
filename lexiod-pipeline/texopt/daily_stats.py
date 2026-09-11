"""Daily accounting from completed pipeline artifacts, without model calls."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

if __package__:
    from .worker_watch import atomic_write, has_recognition_placeholder
    from .daily_costs import combine_costs, cost_label, hydrate_billing, price_record
else:
    from worker_watch import atomic_write, has_recognition_placeholder
    from daily_costs import combine_costs, cost_label, hydrate_billing, price_record


ZONE = ZoneInfo("Asia/Shanghai")
TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")
SKIP_DIRS = {".cache", ".git", ".archive", ".pipeline", "monitoring", "previews",
             "benchmarks", "audits", "optimized", "tex"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def local_path(path, config):
    path = Path(path)
    for remote, local in sorted(config["path_map"].items(), key=lambda pair: -len(pair[0])):
        if path.is_relative_to(remote):
            return Path(local) / path.relative_to(remote)
    return path


def stamp(value):
    return datetime.fromtimestamp(value, ZONE).isoformat(timespec="seconds")


def parse_stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Model timestamp has no timezone")
    return parsed.timestamp()


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def state_databases(config):
    found = set()
    for root in config["scan_roots"]:
        for directory, dirs, _ in os.walk(root):
            dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
            candidate = Path(directory) / ".state" / "pipeline.sqlite3"
            if candidate.is_file():
                found.add(candidate.resolve())
    return sorted(found)


def completed_jobs(database):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY completed_at, id")]
    latest = {}
    for job in jobs:
        if job["stage"] == "optimise" and job["status"] == "completed":
            latest[job["source_path"]] = job
    return jobs, list(latest.values())


def call_summary(log_dir, until, accounted):
    summary = {"calls": 0, "retries": 0, "failed_calls": 0, "model_seconds": 0,
               "missing_usage_calls": 0, "malformed_lines": 0, "unmatched_starts": 0,
               "missing_stage_logs": [], **{key: 0 for key in TOKEN_KEYS}}
    seen, starts, finished, times = set(accounted), {}, set(), []
    call_ids = []
    for stage in ("recognize", "reconcile", "optimise"):
        path = log_dir / f"{stage}.process.calls.jsonl"
        if not path.is_file():
            summary["missing_stage_logs"].append(stage)
            continue
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    kind = event.get("event")
                    if kind not in ("start", "finish"):
                        continue
                    at = parse_stamp(event.get("finished_at") or event["started_at"])
                    if at > until:
                        continue
                    call_id = event.get("call_id") or hashlib.sha256(line.encode()).hexdigest()
                    if kind == "start":
                        starts[call_id] = at
                        continue
                    finished.add(call_id)
                    if call_id in seen:
                        continue
                    seen.add(call_id)
                    call_ids.append(call_id)
                    times.append(parse_stamp(event["started_at"]))
                    summary["calls"] += 1
                    summary["retries"] += int(event.get("attempt", 1) > 1)
                    summary["failed_calls"] += int(event.get("status") == "error")
                    summary["model_seconds"] += max(0, float(event.get("seconds") or 0))
                    usage = event.get("usage") or {}
                    values = {key: usage.get(key) for key in TOKEN_KEYS}
                    if (values["total_tokens"] is None and
                            all(type(values[key]) is int for key in TOKEN_KEYS[:2])):
                        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
                    missing = False
                    for key, value in values.items():
                        if type(value) is int and value >= 0:
                            summary[key] += value
                        else:
                            missing = True
                    summary["missing_usage_calls"] += int(missing)
                except (ValueError, KeyError, TypeError, AttributeError):
                    summary["malformed_lines"] += 1
    abandoned = {key: at for key, at in starts.items() if key not in finished and key not in accounted}
    summary["unmatched_starts"] = len(abandoned)
    # A killed request may have no finish event or usage. Retain its ID across rescans.
    call_ids.extend(abandoned)
    times.extend(abandoned.values())
    return summary, call_ids, min(times) if times else None


def completion_record(job, jobs, config, accounted, previous_at=None):
    source = local_path(job["source_path"], config)
    relative = source.relative_to(Path(config["source_root"]))
    published = Path(config["publish_root"]) / relative.with_suffix(".tex")
    work_tex = local_path(job["output_path"], config)
    scratch = work_tex.parent
    report_path = scratch / f"{source.stem}.report.json"
    report = read_json(report_path)
    layout = report.get("layout_check") or {}
    pdf = local_path(layout.get("preview_pdf") or str(work_tex.with_suffix(".layout.pdf")), config)
    if report.get("compile_check", {}).get("ok") is not True:
        raise ValueError("没有成功编译凭据")
    for path in (work_tex, published, pdf):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"完成产物缺失：{path}")
    if has_recognition_placeholder(work_tex) or has_recognition_placeholder(published):
        raise ValueError("TEX 含识别失败占位页，不计入完成统计")
    with pdf.open("rb") as stream:
        if not stream.read(5) == b"%PDF-":
            raise ValueError("编译 PDF 文件头无效")
    output_hash = checksum(work_tex)
    expected_hash = read_json_metadata(job).get("outputs", {}).get(job["output_path"])
    if expected_hash != output_hash or checksum(published) != output_hash:
        raise ValueError("发布 TEX 与完成任务校验值不一致")
    source_pages, output_pages = layout.get("expected_pages"), layout.get("actual_pages")
    if any(type(value) is not int or value <= 0 for value in (source_pages, output_pages)):
        raise ValueError("编译报告缺少有效源页数或 PDF 页数")
    completed = job["completed_at"]
    usage, call_ids, first_call = call_summary(scratch, completed, accounted)
    starts = [row["started_at"] for row in jobs if row["source_path"] == job["source_path"]
              and row["started_at"] is not None and row["started_at"] <= completed
              and (previous_at is None or row["started_at"] > previous_at)]
    if first_call is not None:
        starts.append(first_call)
    started = min(starts) if starts else None
    return {"source": str(source), "batch": source.parent.name, "pdf_name": source.name,
            "source_pages": source_pages, "pdf_pages": output_pages,
            "completed_at": stamp(completed), "completed_timestamp": completed,
            "date": datetime.fromtimestamp(completed, ZONE).date().isoformat(),
            "started_at": stamp(started) if started is not None else None,
            "elapsed_seconds": completed - started if started is not None else None,
            "compiled_pdf": str(pdf), "published_tex": str(published),
            "output_sha256": output_hash, "report": str(report_path),
            "call_ids": call_ids, **usage}


def read_json_metadata(job):
    return json.loads(job["metadata_json"])


def daily_rows(records, today, start_date, prices):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["date"]].append(record)
    first = min([start_date, *grouped])
    date = datetime.fromisoformat(first).date()
    end = datetime.fromisoformat(today).date()
    rows = []
    while date <= end:
        key = date.isoformat()
        items = sorted(grouped[key], key=lambda item: (item["completed_at"], item["source"]))
        documents = [{**{k: v for k, v in item.items() if k not in ("call_ids", "billing_calls")},
                      "cost": price_record(item, prices)} for item in items]
        row = {"date": key, "provisional": key == today, "completed_pdfs": len(items),
               "documents": documents, "cost": combine_costs(item["cost"] for item in documents)}
        for field in ("source_pages", "pdf_pages", "elapsed_seconds", "model_seconds",
                      "calls", "retries", "failed_calls", "missing_usage_calls",
                      "unmatched_starts", "malformed_lines", *TOKEN_KEYS):
            row[field] = sum(item.get(field) or 0 for item in items)
        row["documents_with_missing_logs"] = sum(bool(item["missing_stage_logs"]) for item in items)
        row["documents_with_unknown_elapsed"] = sum(item["elapsed_seconds"] is None for item in items)
        rows.append(row)
        date += timedelta(days=1)
    return rows


def duration(seconds):
    if seconds is None:
        return "未知"
    seconds = round(seconds)
    return f"{seconds // 3600}时{seconds % 3600 // 60:02d}分{seconds % 60:02d}秒"


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(rows, checked_at, warnings, unmatched, prices):
    lines = ["# 每日转换统计", "", f"更新时间：{checked_at}", "",
             "按北京时间完成日记账，跨天任务的全部已记录消耗归入完成日；不是 API 当日账单。",
             "转换页数按源 PDF 计，生成页数另列。仅计入已发布 TEX 且有成功编译 PDF 的任务。",
             "Token 为服务端已返回 usage 的已知合计，缺失 usage 不代表零消耗。",
             "费用按用户价格表、美元/百万 token 估算；缓存读取和写入从普通输入中扣除后单独计价。",
             ("短/长分界尚未配置，费用显示按短档至长档的估算范围。" if prices.get("long_context_above_tokens") is None
              else f"单次输入超过 {prices['long_context_above_tokens']:,} token 使用长档，否则使用短档。"),
             "未返回缓存明细时暂按零缓存估算；缺失 usage、无结果和未知模型未计价，金额不是完整账单。",
             "任务跨度包含暂停和重启等待；并行任务的跨度相加，不代表当天实际经过时间。",
             "同一完成任务只记一次；后续重新转换产生的新完成任务另记，已记账的调用不重复累计。",
             "", "## 每日汇总", "",
             "| 日期 | 完成 PDF | 源页数 | 生成页数 | 输入 token（已知） | 输出 token（已知） | 总 token（已知） | 费用估算（USD） | 任务跨度合计 | 模型请求耗时合计 | 重试 | usage 缺失调用 | 中断无结果调用 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | ---: | ---: | ---: |"]
    totals = {key: sum(row[key] for row in rows) for key in
              ("completed_pdfs", "source_pages", "pdf_pages", "elapsed_seconds", "model_seconds",
               "retries", "missing_usage_calls", "unmatched_starts", *TOKEN_KEYS)}
    totals["cost"] = combine_costs(row["cost"] for row in rows)
    for row in [*rows, {"date": "累计", "provisional": False, **totals}]:
        date = row["date"] + ("（进行中）" if row["provisional"] else "")
        lines.append(f"| {date} | {row['completed_pdfs']} | {row['source_pages']} | {row['pdf_pages']} "
                     f"| {row['input_tokens']:,} | {row['output_tokens']:,} | {row['total_tokens']:,} "
                     f"| {cost_label(row['cost'])} "
                     f"| {duration(row['elapsed_seconds'])} | {duration(row['model_seconds'])} "
                     f"| {row['retries']} | {row['missing_usage_calls']} | {row['unmatched_starts']} |")
    for row in rows:
        lines.extend(["", f"## {row['date']} 完成明细", "",
                      "| 批次号（上一级目录） | PDF 文件名 | 完成时间 | 源页数 | 生成页数 | 输入 token | 输出 token | 总 token（已知） | 费用估算（USD） | 任务跨度 | 重试 | 数据缺口 |",
                      "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |"])
        for item in row["documents"]:
            gaps = []
            for field, label in (("missing_usage_calls", "次 usage 缺失"),
                                 ("unmatched_starts", "次中断无结果"), ("malformed_lines", "行日志异常")):
                if item[field]:
                    gaps.append(f"{item[field]}{label}")
            if item["missing_stage_logs"]:
                gaps.append("缺少日志：" + ", ".join(item["missing_stage_logs"]))
            if item["cost"]["unpriced_calls"]:
                gaps.append(f"{item['cost']['unpriced_calls']}次未计价")
            lines.append(f"| {cell(item['batch'])} | {cell(item['pdf_name'])} "
                         f"| {item['completed_at'][11:19]} | {item['source_pages']} | {item['pdf_pages']} "
                         f"| {item['input_tokens']:,} | {item['output_tokens']:,} | {item['total_tokens']:,} "
                         f"| {cost_label(item['cost'])} "
                         f"| {duration(item['elapsed_seconds'])} | {item['retries']} | {'；'.join(gaps) or '无'} |")
        if not row["documents"]:
            lines.append("\n当天尚无符合完成条件的 PDF。")
    lines.extend(["", "## 累计模型费用", "",
                  "| 模型 | 普通输入 token | 缓存输入 token | 缓存写入 token | 输出 token | 费用估算（USD） | 未计价调用 |",
                  "| --- | ---: | ---: | ---: | ---: | --- | ---: |"])
    for model, cost in sorted(totals["cost"]["by_model"].items()):
        lines.append(f"| {cell(model)} | {cost['ordinary_input_tokens']:,} | {cost['cached_input_tokens']:,} "
                     f"| {cost['cache_write_tokens']:,} | {cost['output_tokens']:,} "
                     f"| {cost_label(cost)} | {cost['unpriced_calls']} |")
    if warnings or unmatched:
        lines.extend(["", "## 统计覆盖", "",
                      f"另有 {len(unmatched)} 份已发布 TEX 缺少可匹配的完成凭据，未计入。路径见 daily.json。"])
        lines.extend(f"- {cell(warning)}" for warning in warnings)
    return "\n".join(lines) + "\n"


def poll(config, now=None):
    now = now or datetime.now(ZONE)
    today = now.astimezone(ZONE).date().isoformat()
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    with (output / "daily.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ledger = output / "completions.jsonl"
        records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()] if ledger.exists() else []
        ids = {item["id"] for item in records}
        accounted = {call_id for item in records for call_id in item["call_ids"]}
        warnings, candidates = [], []
        for database in state_databases(config):
            try:
                jobs, completed = completed_jobs(database)
                candidates.extend((job["completed_at"], str(database), job, jobs) for job in completed)
            except (sqlite3.Error, OSError, ValueError) as exc:
                warnings.append(f"{database}：{type(exc).__name__}，状态库读取失败")
        added = 0
        for completed_at, database, job, jobs in sorted(candidates, key=lambda item: item[:2]):
            try:
                source = str(local_path(job["source_path"], config))
                identity = hashlib.sha256(f"{source}\n{completed_at}".encode()).hexdigest()
                if identity in ids or completed_at > now.timestamp():
                    continue
                previous = max((item["completed_timestamp"] for item in records
                                if item["source"] == source), default=None)
                if previous is not None and previous >= completed_at:
                    continue
                record = completion_record(job, jobs, config, accounted, previous)
                record.update(id=identity, state_database=database)
                records.append(record)
                ids.add(identity)
                accounted.update(record["call_ids"])
                added += 1
            except (OSError, ValueError, TypeError, KeyError) as exc:
                warnings.append(f"{job['source_path']}：{exc}")
        records.sort(key=lambda item: (item["completed_timestamp"], item["id"]))
        hydrated = False
        for record in records:
            hydrated = hydrate_billing(record) or hydrated
        if added or hydrated or not ledger.exists():
            # Commit the durable ledger before rebuilding its disposable daily views.
            atomic_write(ledger, "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
        published = {str(path) for path in Path(config["publish_root"]).rglob("*.tex")}
        unmatched = sorted(published - {item["published_tex"] for item in records})
        prices = read_json(config.get("pricing_file", Path(__file__).with_name("model_prices.json")))
        rows = daily_rows(records, today, config.get("start_date", today), prices)
        snapshot = {"checked_at": now.astimezone(ZONE).isoformat(timespec="seconds"),
                    "timezone": "Asia/Shanghai", "days": rows, "warnings": warnings,
                    "pricing": prices, "cost": combine_costs(row["cost"] for row in rows),
                    "unmatched_published_tex": unmatched}
        atomic_write(output / "daily.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
        atomic_write(output / "daily.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        atomic_write(output / "daily.md", render_report(rows, snapshot["checked_at"], warnings, unmatched, prices))
        print(json.dumps({"checked_at": snapshot["checked_at"], "new_completions": added,
                          "completed_pdfs": len(records), "source_pages": sum(r["source_pages"] for r in records),
                          "warnings": len(warnings), "unmatched_tex": len(unmatched)}, ensure_ascii=False))
        return snapshot


def install(config_path, config):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    job = {"Label": config["launchd_label"], "ProgramArguments": [sys.executable,
           str(Path(__file__).resolve()), "once", "--config", str(config_path)],
           "RunAtLoad": True, "StartInterval": config.get("interval_seconds", 600),
           "ProcessType": "Background", "StandardOutPath": str(output / "runner.log"),
           "StandardErrorPath": str(output / "runner.error.log")}
    # Keep the agent in LaunchAgents so accounting resumes after the next login.
    path = Path.home() / "Library" / "LaunchAgents" / f"{config['launchd_label']}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"定时任务已经存在：{path}")
    path.write_bytes(plistlib.dumps(job))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True, timeout=15)
    print(f"日报统计已启用，每 {job['StartInterval']} 秒更新一次；不随容器退出而停止。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("once", "install", "stop"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = read_json(config_path)
    if config.get("interval_seconds", 600) <= 0:
        parser.error("更新间隔必须为正数")
    if args.action == "install":
        install(config_path, config)
    elif args.action == "stop":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{config['launchd_label']}"],
                       check=True, timeout=15)
    else:
        poll(config)


if __name__ == "__main__":
    main()
