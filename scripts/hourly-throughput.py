"""Record rolling hourly PDF recognition throughput and model cost."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_COSTS_PATH = PROJECT_ROOT / "pipeline" / "texopt" / "monitoring" / "daily_costs.py"
_SPEC = importlib.util.spec_from_file_location("hourly_daily_costs", DAILY_COSTS_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"Unable to load pricing helpers: {DAILY_COSTS_PATH}")
_DAILY_COSTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DAILY_COSTS)
billing_call = _DAILY_COSTS.billing_call
combine_costs = _DAILY_COSTS.combine_costs
price_record = _DAILY_COSTS.price_record


ZONE = ZoneInfo("Asia/Shanghai")
STATE_VERSION = 1


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def local_stamp(epoch):
    return datetime.fromtimestamp(epoch, ZONE).isoformat(timespec="seconds")


def parse_stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("model timestamp has no timezone")
    return parsed.timestamp()


def has_recognition_placeholder(path):
    return bool(re.search(r"(?m)^\s*%\s*LEXOID_RECOGNITION_FALLBACK\b",
                          Path(path).read_text(encoding="utf-8")))


def queue_info(queue_path, data_root):
    queue_path = Path(queue_path).resolve()
    queue = read_json(queue_path)
    status_path = queue_path.with_suffix(".status.json")
    status = read_json(status_path) if status_path.is_file() else {}
    pages = {job["stem"]: job.get("pages") for job in status.get("jobs", [])}
    sources = [{"source": source, "stem": Path(source).stem,
                "work_root": str(Path(data_root) / "workers" / Path(source).stem),
                "pages": pages.get(Path(source).stem)} for source in queue["sources"]]
    return {"path": str(queue_path), "status_path": str(status_path),
            "container": queue["container"], "sources": sources}


def current_recognition_pages(info):
    found = {}
    for source in info["sources"]:
        root = Path(source["work_root"]) / ".cache" / "recognition" / "draft"
        generations = [path for path in root.glob("*") if path.is_dir()]
        if not generations:
            continue
        current = max(generations, key=lambda path: path.stat().st_mtime)
        for metadata in current.glob("*.json"):
            tex = metadata.with_suffix(".tex")
            if not metadata.stem.isdigit() or not tex.is_file():
                continue
            try:
                if has_recognition_placeholder(tex):
                    continue
                key = f"{source['stem']}:{int(metadata.stem)}"
                found[key] = max(metadata.stat().st_mtime, tex.stat().st_mtime)
            except (OSError, UnicodeError):
                continue
    return found


def finished_calls(info, started_at, checked_at):
    found = {}
    malformed = 0
    for source in info["sources"]:
        logs = Path(source["work_root"]) / ".pipeline" / source["stem"]
        for path in sorted(logs.glob("*.process.calls.jsonl")):
            try:
                stream = path.open(encoding="utf-8")
            except OSError:
                continue
            with stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        if event.get("event") != "finish":
                            continue
                        finished_at = parse_stamp(event["finished_at"])
                        if not started_at <= finished_at <= checked_at:
                            continue
                        call_id = event.get("call_id") or hashlib.sha256(line.encode()).hexdigest()
                        found.setdefault(call_id, (finished_at, billing_call(event, call_id), event))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        malformed += 1
    return list(found.values()), malformed


def bucket_index(epoch, anchor, seconds):
    return max(0, int((epoch - anchor) // seconds))


def usage_and_cost(events, prices):
    calls = [billing for _, billing, _ in events]
    cost = price_record({"call_ids": [item["call_id"] for item in calls],
                         "billing_calls": calls, "billing_missing_records": 0}, prices)
    input_tokens = sum(item["input_tokens"] or 0 for item in calls)
    output_tokens = sum(item["output_tokens"] or 0 for item in calls)
    return {"calls": len(calls), "input_tokens": input_tokens,
            "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
            "missing_usage_calls": sum(item["input_tokens"] is None or item["output_tokens"] is None
                                       for item in calls), "cost": cost}


def money(low, high):
    return f"${low:.4f}" if low == high else f"${low:.4f} - ${high:.4f}"


def cost_amounts(cost, pages, prices):
    page_cost = Decimal(pages) * Decimal(str(prices.get("source_page_usd", "0")))
    model_low = Decimal(cost["estimated_usd_low"])
    model_high = Decimal(cost["estimated_usd_high"])
    return model_low, model_high, page_cost, model_low + page_cost, model_high + page_cost


def queue_runtime(info):
    path = Path(info["status_path"])
    if not path.is_file():
        return {"active": "-", "done": 0, "total": len(info["sources"]), "state": "unknown"}
    status = read_json(path)
    jobs = status.get("jobs", [])
    active = Path(status.get("active_source") or "-").name
    state = status.get("status") or ("running" if any(job.get("status") == "running" for job in jobs)
                                     else "pending")
    return {"active": active, "done": sum(job.get("status") == "done" for job in jobs),
            "total": len(jobs), "state": state}


def render_bucket(index, anchor, seconds, now, rows, prices, malformed):
    start = anchor + index * seconds
    end = start + seconds
    complete = now >= end
    total_pages = sum(row["pages"] for row in rows)
    total_usage = {"calls": sum(row["usage"]["calls"] for row in rows),
                   "input_tokens": sum(row["usage"]["input_tokens"] for row in rows),
                   "output_tokens": sum(row["usage"]["output_tokens"] for row in rows),
                   "total_tokens": sum(row["usage"]["total_tokens"] for row in rows),
                   "missing_usage_calls": sum(row["usage"]["missing_usage_calls"] for row in rows)}
    total_cost = combine_costs(row["usage"]["cost"] for row in rows)
    amounts = cost_amounts(total_cost, total_pages, prices)
    status = "已完成" if complete else "统计中"
    lines = [f"# 四容器小时统计 - 第 {index + 1} 小时", "",
             f"- 状态：{status}", f"- 开始：{local_stamp(start)}", f"- 结束：{local_stamp(end)}",
             f"- 最近采样：{local_stamp(now)}", "",
             "| 容器 | 新增识别页 | 输入 token | 输出 token | 总 token | 模型费用 | 页费 | 合计 | 当前任务 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in rows:
        low, high, page_cost, total_low, total_high = cost_amounts(
            row["usage"]["cost"], row["pages"], prices)
        runtime = row["runtime"]
        current = f"{runtime['done']}/{runtime['total']}，{runtime['active']}"
        lines.append(f"| {row['container']} | {row['pages']:,} | "
                     f"{row['usage']['input_tokens']:,} | {row['usage']['output_tokens']:,} | "
                     f"{row['usage']['total_tokens']:,} | {money(low, high)} | ${page_cost:.4f} | "
                     f"{money(total_low, total_high)} | {current} |")
    lines.extend(["", "## 合计", "",
                  f"- 识别页数：{total_pages:,}",
                  f"- 输入 token：{total_usage['input_tokens']:,}",
                  f"- 输出 token：{total_usage['output_tokens']:,}",
                  f"- 总 token：{total_usage['total_tokens']:,}",
                  f"- 模型费用：{money(amounts[0], amounts[1])}",
                  f"- 页费：${amounts[2]:.4f}",
                  f"- 最终费用：{money(amounts[3], amounts[4])}",
                  f"- 模型调用：{total_usage['calls']:,}",
                  f"- usage 缺失调用：{total_usage['missing_usage_calls']:,}",
                  f"- 未计价调用：{total_cost['unpriced_calls']:,}",
                  f"- 无法解析的日志行：{malformed:,}", ""])
    return "\n".join(lines), {"index": index + 1, "start": local_stamp(start),
            "end": local_stamp(end), "complete": complete, "pages": total_pages,
            **total_usage, "model_cost_low": str(amounts[0]), "model_cost_high": str(amounts[1]),
            "page_cost": str(amounts[2]), "total_cost_low": str(amounts[3]),
            "total_cost_high": str(amounts[4]), "unpriced_calls": total_cost["unpriced_calls"]}


def poll(config):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / "sample.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state_path = output / "state.json"
        now = time.time()
        infos = [queue_info(path, config["data_root"]) for path in config["queues"]]
        queue_names = [info["container"] for info in infos]
        if state_path.is_file():
            state = read_json(state_path)
            if state.get("version") != STATE_VERSION or state.get("containers") != queue_names:
                raise ValueError("统计配置与已有状态不一致；请先停止并归档旧状态")
        else:
            state = {"version": STATE_VERSION, "anchor": now, "containers": queue_names,
                     "page_events": {name: {} for name in queue_names}}
        anchor = state["anchor"]
        seconds = int(config.get("window_seconds", 3600))
        for info in infos:
            events = state["page_events"][info["container"]]
            for key, modified_at in current_recognition_pages(info).items():
                if key not in events:
                    events[key] = modified_at if modified_at >= anchor else None
        prices = read_json(config["pricing_file"])
        calls_by_queue = {}
        malformed = 0
        for info in infos:
            calls, bad = finished_calls(info, anchor, now)
            calls_by_queue[info["container"]] = calls
            malformed += bad
        last_index = bucket_index(now, anchor, seconds)
        summaries = []
        latest = ""
        for index in range(last_index + 1):
            start, end = anchor + index * seconds, anchor + (index + 1) * seconds
            rows = []
            for info in infos:
                name = info["container"]
                pages = sum(at is not None and start <= at < end
                            for at in state["page_events"][name].values())
                selected = [event for event in calls_by_queue[name] if start <= event[0] < end]
                rows.append({"container": name, "pages": pages,
                             "usage": usage_and_cost(selected, prices),
                             "runtime": queue_runtime(info)})
            report, summary = render_bucket(index, anchor, seconds, now, rows, prices, malformed)
            report_name = datetime.fromtimestamp(start, ZONE).strftime(f"hour-{index + 1:03d}-%Y%m%d-%H%M%S.md")
            atomic_write(output / report_name, report)
            summaries.append({**summary, "report": report_name})
            if index == last_index:
                latest = report
        history = ["# 四容器小时统计", "", f"统计起点：{local_stamp(anchor)}", "",
                   "| 小时 | 时间范围 | 状态 | 页数 | 总 token | 最终费用 |",
                   "| ---: | --- | --- | ---: | ---: | ---: |"]
        for item in summaries:
            status = "已完成" if item["complete"] else "统计中"
            final_cost = money(Decimal(item["total_cost_low"]), Decimal(item["total_cost_high"]))
            history.append(f"| [{item['index']}]({item['report']}) | {item['start']} 至 {item['end']} | "
                           f"{status} | {item['pages']:,} | {item['total_tokens']:,} | {final_cost} |")
        state["last_sample_at"] = now
        atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
        atomic_write(output / "latest.md", latest)
        atomic_write(output / "history.md", "\n".join(history) + "\n")
        snapshot = {"sampled_at": local_stamp(now), "anchor": local_stamp(anchor),
                    "current_hour": last_index + 1, "current": summaries[-1]}
        atomic_write(output / "latest.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)


def install(config_path, config):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    label = config["launchd_label"]
    plist_path = output / f"{label}.plist"
    job = {"Label": label, "ProgramArguments": [sys.executable, str(Path(__file__).resolve()),
           "once", "--config", str(config_path)], "RunAtLoad": True,
           "StartInterval": int(config.get("interval_seconds", 120)), "ProcessType": "Background",
           "StandardOutPath": str(output / "runner.log"),
           "StandardErrorPath": str(output / "runner.error.log")}
    plist_path.write_bytes(plistlib.dumps(job))
    service = f"gui/{os.getuid()}/{label}"
    subprocess.run(["launchctl", "bootout", service], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=15)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                   check=True, timeout=15)
    print(f"小时统计已启用，每 {job['StartInterval']} 秒采样一次。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("once", "install", "stop"))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = read_json(config_path)
    if len(config.get("queues", [])) != 4:
        parser.error("本统计任务必须配置四个队列")
    if int(config.get("interval_seconds", 120)) <= 0 or int(config.get("window_seconds", 3600)) <= 0:
        parser.error("采样间隔和统计窗口必须为正数")
    service = f"gui/{os.getuid()}/{config['launchd_label']}"
    if args.action == "once":
        poll(config)
    elif args.action == "install":
        install(config_path, config)
    else:
        subprocess.run(["launchctl", "bootout", service], check=True, timeout=15)


if __name__ == "__main__":
    main()
