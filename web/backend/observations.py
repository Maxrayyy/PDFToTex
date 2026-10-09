"""Read evidence rather than infer completion from elapsed time or page maxima."""

import json
from datetime import datetime
from collections import defaultdict
from pathlib import Path
import re


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r'(?i)(Bearer\s+)[^\s"\']+', r"\1[REDACTED]", text)


def snapshot(store, job):
    root = store.root / "jobs" / job["id"]
    calls = {}
    starts = {}
    complete = set()
    errors = []
    page_errors = {}
    try:
        progress = json.loads((root / "progress.json").read_text())
    except (OSError, ValueError):
        progress = {}
    for path in (root / "work").rglob("*.calls.jsonl"):
        for line in path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "validation_or_request_error" and event.get(
                "page"
            ):
                page_errors[event["page"]] = event
            ident = event.get("call_id")
            if ident and event.get("event") == "start":
                starts[ident] = event
            if ident and event.get("event") == "finish":
                calls[ident] = event
    for path in (root / "work").rglob("*.tex"):
        if path.parent.name == "tex" and not path.name.endswith(".reconciled.tex"):
            text = path.read_text(errors="replace")
            complete.update(
                int(n)
                for n in re.findall(r"%\s*LEXOID_PAGE_COMPLETED:\s*(\d+)/\d+", text)
            )
            # A fallback checkpoint is not successful recognition.
            if "LEXOID_RECOGNITION_FALLBACK" in text:
                for match in re.finditer(
                    r"LEXOID_RECOGNITION_FALLBACK(?:(?!LEXOID_PAGE_COMPLETED:)[\s\S])*LEXOID_PAGE_COMPLETED:\s*(\d+)/",
                    text,
                ):
                    complete.discard(int(match[1]))
    totals = {}
    events = sorted(
        calls.values(), key=lambda e: e.get("finished_at", e.get("started_at", ""))
    )
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [(e.get("usage") or {}).get(key) for e in events]
        known = [x for x in values if type(x) is int]
        totals[key] = sum(known) if len(known) == len(values) else None
        totals["known_" + key] = sum(known)
    durations = [
        e.get("seconds")
        for e in events
        if e.get("stage") == "recognize"
        and e.get("status") == "ok"
        and isinstance(e.get("seconds"), (float, int))
    ]
    remaining = max(0, (job.get("pages") or 0) - len(complete))
    eta = (
        (
            sum(durations)
            / len(durations)
            * remaining
            / max(1, min(job.get("config", {}).get("page_concurrency", 1), remaining))
        )
        if durations and remaining and progress.get("stage") == "recognize"
        else None
    )
    by_page = defaultdict(list)
    active_by_page = defaultdict(list)
    for event in events:
        by_page[event.get("page")].append(event)
    for ident, event in starts.items():
        try:
            started = datetime.fromisoformat(event.get("started_at", "")).timestamp()
        except ValueError:
            started = 0
        if ident not in calls and started >= job.get("started_at", 0):
            active_by_page[event.get("page")].append(event)
    rows = []
    for n in range(1, (job.get("pages") or 0) + 1):
        page_calls = by_page[n]
        active = active_by_page[n] if job["status"] == "running" else []
        last = page_calls[-1] if page_calls else {}
        if active:
            state = "running"
        elif last.get("status") == "error" and (
            n not in complete or last.get("stage") != "recognize"
        ):
            state = "failed"
        elif n in complete:
            state = "completed"
        elif n in page_errors:
            state = "failed"
        elif last.get("status") == "ok":
            state = "validating"
        else:
            state = "pending"
        rows.append(
            dict(
                page=n,
                status=state,
                stage=(active[-1] if active else last).get("stage"),
                model=(active[-1] if active else last).get("model"),
                error=(
                    (page_errors.get(n) or last).get("error_message")
                    or (page_errors.get(n) or last).get("error_type")
                    if state == "failed"
                    else None
                ),
                calls=len(page_calls),
                tokens=(
                    sum(
                        (c.get("usage") or {}).get("total_tokens", 0) or 0
                        for c in page_calls
                    )
                    if page_calls
                    and all(
                        (c.get("usage") or {}).get("total_tokens") is not None
                        for c in page_calls
                    )
                    else None
                ),
            )
        )
    checks = []
    compile_log = root / "result" / str(job.get("attempt", 0)) / "compile.log"
    log = compile_log.read_text(errors="replace") if compile_log.exists() else ""
    checks.append(
        dict(
            name="编译",
            status=(
                "passed"
                if job["status"] == "succeeded"
                else (
                    "failed"
                    if log and job["status"] == "failed"
                    else (
                        "pending" if progress.get("stage") == "compile" else "unchecked"
                    )
                )
            ),
        )
    )
    log = log.split("XeLaTeX pass 2/2")[
        -1
    ]  # First-pass unresolved references may be resolved on pass two.
    for title, pattern in [
        ("缺失字符", r"Missing character:.*"),
        ("版面溢出", r"(?:Overfull|Underfull) \\[hv]box[^\n]*"),
        ("未解析引用", r"[^\n]*(?:undefined references|Reference .* undefined)[^\n]*"),
    ]:
        matches = list(dict.fromkeys(re.findall(pattern, log)))
        checks.append(
            dict(
                name=title,
                status=(
                    "warning"
                    if matches
                    else "passed" if job["status"] == "succeeded" else "unchecked"
                ),
                details=matches[:30],
            )
        )
    structural = root / "result" / str(job.get("attempt", 0)) / "checks.json"
    if structural.exists():
        try:
            issues = json.loads(structural.read_text())["issues"]
            checks.append(
                dict(
                    name="主文件括号与数学环境",
                    status="warning" if issues else "passed",
                    details=[f"第 {i['line']} 行 · {i['message']}" for i in issues],
                )
            )
        except (OSError, ValueError, KeyError):
            pass
    checks.extend(
        [
            dict(name="符号与内容准确性", status="unchecked"),
            dict(name="视觉裁切检查", status="unchecked"),
        ]
    )
    errors = re.findall(r"(?m)^.*(?:\.tex:\d+:|^! ).*$", log)
    try:
        from texopt.monitoring.daily_costs import billing_call, price_record
        from .store import ROOT

        prices = json.loads((ROOT / "pipeline/texopt/model_prices.json").read_text())
        cost = price_record(
            {
                "call_ids": list(calls),
                "billing_missing_records": 0,
                "billing_calls": [billing_call(e, e["call_id"]) for e in events],
            },
            prices,
        )
        cost["currency"] = prices["currency"]
    except (ImportError, OSError, ValueError, KeyError):
        cost = {
            "estimated_usd_low": None,
            "estimated_usd_high": None,
            "unpriced_calls": len(events),
        }
    for path in (root / "work").rglob("*.report.json"):
        try:
            report = json.loads(path.read_text())
            warnings = report.get("syntax_warnings", [])
            if "syntax_warnings" in report:
                checks.append(
                    dict(
                        name="TeX 结构检查",
                        status="warning" if warnings else "passed",
                        details=[str(w) for w in warnings],
                    )
                )
            if report.get("layout_check"):
                checks.append(
                    dict(
                        name="流水线版面检查",
                        status=(
                            "warning"
                            if report["layout_check"].get("outside_pages")
                            else (
                                "passed"
                                if report.get("compile_check", {}).get("ok")
                                else "unchecked"
                            )
                        ),
                        details=[
                            json.dumps(report["layout_check"], ensure_ascii=False)
                        ],
                    )
                )
        except (OSError, ValueError, KeyError):
            pass
    for event in events:
        event["model_source"] = (
            "requested"  # Existing pipeline does not guarantee a returned model identifier.
        )
    return dict(
        progress=progress,
        completed_pages=len(complete),
        page_rows=rows,
        calls=events,
        usage={
            **totals,
            "calls": len(events),
            "retry_calls": sum(e.get("attempt", 1) > 1 for e in events),
        },
        checks=checks,
        compile_errors=errors[:30],
        cost=cost,
        recognition_eta_seconds=eta,
    )
