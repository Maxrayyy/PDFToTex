"""Read evidence rather than infer completion from elapsed time or page maxima."""

import json
import fcntl
from copy import deepcopy
import time
from datetime import datetime
from collections import defaultdict
from pathlib import Path
import re


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r'(?i)(Bearer\s+)[^\s"\']+', r"\1[REDACTED]", text)


def _assemble(store, job, index):
    root = store.root / "jobs" / job["id"]
    calls, starts, page_errors, complete = index.summary()
    errors = []
    try:
        progress = json.loads(index.text(root / "progress.json"))
    except ValueError:
        progress = {}
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
    log = index.text(compile_log)
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
            issues = json.loads(index.text(structural))["issues"]
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

        prices = json.loads(index.text(ROOT / "pipeline/texopt/model_prices.json"))
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
    for path in index.paths("report"):
        try:
            report = json.loads(index.text(path))
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


# A projection is derived data. Raw stage evidence remains authoritative and can
# rebuild it after deletion/corruption; it never writes the pipeline state store.
PROJECTION_SCHEMA = 1


class _EvidenceIndex:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.dirty = False

    def paths(self, kind):
        return [Path(path) for path, record in self.files.items() if record['kind'] == kind]

    def text(self, path):
        return self.files.get(str(path), {}).get('text', '')

    def collect(self, root):
        from .store import ROOT

        paths = {root / 'progress.json': 'text', ROOT / 'pipeline/texopt/model_prices.json': 'text'}
        for path in (root / 'work').rglob('*'):
            if path.name.endswith('.calls.jsonl'):
                paths[path] = 'calls'
            elif path.suffix == '.tex' and path.parent.name == 'tex' and not path.name.endswith('.reconciled.tex'):
                paths[path] = 'tex'
            elif path.name.endswith('.report.json'):
                paths[path] = 'report'
        # Keep prior attempts separate; the projection selects the current one.
        for path in (root / 'result').glob('*/*'):
            if path.name in ('compile.log', 'checks.json'):
                paths[path] = 'text'
        present = set()
        for path, kind in sorted(paths.items()):
            try:
                stat = path.stat()
                if not path.is_file():
                    continue
                key = str(path)
                present.add(key)
                fingerprint = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
                old = self.files.get(key)
                if old and old['fingerprint'] == fingerprint:
                    continue
                if kind == 'calls':
                    record = self._calls(path, old, fingerprint)
                else:
                    record = {'text': path.read_text(errors='replace')}
                record.update(kind=kind, fingerprint=fingerprint)
                self.files[key] = record
                self.dirty = True
            except FileNotFoundError:
                # A writer can replace/remove evidence between discovery and open.
                continue
        for key in set(self.files) - present:
            del self.files[key]
            self.dirty = True

    def _calls(self, path, old, fingerprint):
        append = (old and old['fingerprint'][:2] == fingerprint[:2]
                  and fingerprint[2] > old['fingerprint'][2])
        with path.open('rb') as stream:
            if append:
                # Detect truncate-and-regrow between polls, even with the same inode.
                offset = old['offset']
                stream.seek(max(0, offset - 64))
                append = stream.read(min(64, offset)).hex() == old['anchor']
            if append:
                record = deepcopy(old)
                stream.seek(record['offset'])
            else:
                record = {'offset': 0, 'calls': {}, 'starts': {}, 'errors': {}}
                stream.seek(0)
            data = stream.read()
            consumed = 0
            for line in data.splitlines(keepends=True):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    if not line.endswith(b'\n'):
                        break  # Wait for the writer to finish this last JSON record.
                    consumed += len(line)
                    continue
                if isinstance(event, dict):
                    ident = event.get('call_id')
                    if ident and event.get('event') in ('start', 'finish'):
                        target = 'starts' if event['event'] == 'start' else 'calls'
                        record[target][str(ident)] = event
                    if event.get('event') == 'validation_or_request_error' and event.get('page'):
                        record['errors'][str(event['page'])] = event
                consumed += len(line)
            record['offset'] += consumed
            stream.seek(max(0, record['offset'] - 64))
            record['anchor'] = stream.read(min(64, record['offset'])).hex()
            return record

    def summary(self):
        calls, starts, errors, complete = {}, {}, {}, set()
        for record in self.files.values():
            if record['kind'] == 'calls':
                calls.update(record['calls'])
                starts.update(record['starts'])
                errors.update({int(k): v for k, v in record['errors'].items()})
            elif record['kind'] == 'tex':
                text = record['text']
                pages = {int(n) for n in re.findall(r'%\s*LEXOID_PAGE_COMPLETED:\s*(\d+)/\d+', text)}
                for match in re.finditer(
                    r'LEXOID_RECOGNITION_FALLBACK(?:(?!LEXOID_PAGE_COMPLETED:)[\s\S])*LEXOID_PAGE_COMPLETED:\s*(\d+)/', text
                ):
                    pages.discard(int(match[1]))
                complete.update(pages)
        return calls, starts, errors, complete


def _cache_projection(store, path, value):
    if store is None:
        return
    cache = getattr(store, '_observation_cache', None)
    if cache is None:
        cache = store._observation_cache = {}
    stat = path.stat()
    cache[str(path)] = ((stat.st_ino, stat.st_size, stat.st_mtime_ns), value)
    while len(cache) > 8:
        del cache[next(iter(cache))]


def _read_projection(path, store=None):
    try:
        stat = path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = getattr(store, '_observation_cache', {}).get(str(path))
        if cached and cached[0] == signature:
            return cached[1]
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('schema') != PROJECTION_SCHEMA:
            return {}
        if path.name == 'observations-index.json':
            files = value.get('files')
            if not isinstance(files, dict):
                return {}
            for record in files.values():
                if (not isinstance(record, dict) or
                        not isinstance(record.get('fingerprint'), list) or
                        len(record['fingerprint']) != 5):
                    return {}
                if record.get('kind') == 'calls':
                    if (not isinstance(record.get('offset'), int) or record['offset'] < 0 or
                            not isinstance(record.get('anchor'), str) or
                            any(not isinstance(record.get(key), dict) for key in ('calls', 'starts', 'errors'))):
                        return {}
                elif not isinstance(record.get('text'), str):
                    return {}
        else:
            if (not isinstance(value.get('snapshot'), dict) or
                    not isinstance(value.get('job_stamp'), dict) or
                    not isinstance(value.get('updated_at'), (float, int)) or
                    not {'progress', 'usage', 'checks', 'calls', 'page_rows', 'completed_pages'}.issubset(value['snapshot'])):
                return {}
        _cache_projection(store, path, value)
        return value
    except (OSError, ValueError):
        return {}


def snapshot(store, job):
    """Refresh from changed evidence. Called by the worker, never the HTTP query.

    The lock serializes refresh/rebuild across processes. Both index and public
    snapshot are atomic, so interrupted projection writes are safe to replay.
    """
    from .store import atomic_json

    root = store.root / 'jobs' / job['id']
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'observations.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _read_projection(root / 'observations-index.json', store)
        index = _EvidenceIndex(state.get('files'))
        index.collect(root)
        previous = _read_projection(root / 'observations.json', store)
        # Job state/config affects page state even if no evidence file changed.
        stamp = {key: job.get(key) for key in ('version', 'status', 'attempt', 'pages', 'started_at', 'config')}
        if not previous or index.dirty or previous.get('job_stamp') != stamp:
            result = _assemble(store, job, index)
        else:
            result = previous['snapshot']
        value = dict(schema=PROJECTION_SCHEMA, job_stamp=stamp,
                     updated_at=time.time(), snapshot=result)
        # Write the index AFTER the snapshot: a crash may replay evidence, but can
        # never advance a cursor while leaving a supposedly current stale result.
        atomic_json(root / 'observations.json', value)
        _cache_projection(store, root / 'observations.json', value)
        if index.dirty or not state:
            state = dict(schema=PROJECTION_SCHEMA, files=index.files)
            atomic_json(root / 'observations-index.json', state)
            _cache_projection(store, root / 'observations-index.json', state)
        return result


def read_snapshot(store, job):
    """Read the projection without scanning or parsing stage evidence."""
    root = store.root / 'jobs' / job['id']
    value = _read_projection(root / 'observations.json', store)
    if not value:
        result = _assemble(store, job, _EvidenceIndex())
        # No projection yet is not evidence that any check passed.
        for check in result['checks']:
            check['status'] = 'unchecked'
        return {**result, 'observation_updated_at': None, 'observation_status': 'pending'}
    stamp = value.get('job_stamp', {})
    stale = any(stamp.get(k) != job.get(k) for k in ('version', 'status', 'attempt'))
    if job.get('status') in ('starting', 'running', 'cancel_requested'):
        stale |= time.time() - value['updated_at'] > max(10, store.config.get('poll_seconds', 2) * 3)
    return {**value['snapshot'], 'observation_updated_at': value['updated_at'],
            'observation_status': 'stale' if stale else 'current'}
