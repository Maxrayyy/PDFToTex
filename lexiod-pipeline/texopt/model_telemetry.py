"""Provider call timing and usage for the standalone optimizer package."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import uuid


def emit(event):
    line = json.dumps(event, ensure_ascii=True)
    print("[LLM_CALL] " + line, file=sys.stderr, flush=True)
    path = os.getenv("LEXOID_MODEL_CALL_LOG")
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            print("[LLM_CALL_LOG_ERROR] Unable to append model call log", file=sys.stderr)


def request_json(request, timeout, *, stage, model, attempt=1, **scope):
    event = {"call_id": uuid.uuid4().hex, "stage": stage, "model": model,
             "attempt": attempt, "retry_count": attempt - 1, **scope,
             "started_at": datetime.now(timezone.utc).isoformat()}
    started = time.monotonic()
    emit({**event, "event": "start"})
    data, error = None, None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        raw = (data or {}).get("usage")
        usage = None
        if isinstance(raw, dict):
            usage = {"input_tokens": raw.get("prompt_tokens", raw.get("input_tokens")),
                     "output_tokens": raw.get("completion_tokens", raw.get("output_tokens")),
                     "total_tokens": raw.get("total_tokens")}
        choice = ((data or {}).get("choices") or [{}])[0]
        emit({**event, "event": "finish", "seconds": time.monotonic() - started,
              "finished_at": datetime.now(timezone.utc).isoformat(),
              "status": "error" if error else "ok", "error_type": error,
              "finish_reason": choice.get("finish_reason", (data or {}).get("stop_reason")),
              "response_id": (data or {}).get("id"),
              "usage": usage, "usage_raw": raw})


def summarize_calls(path, offset=0):
    """Sum only usage reported by the provider; unknown counts remain explicit."""
    events = []
    if Path(path).exists():
        with Path(path).open("rb") as stream:
            stream.seek(offset)
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if event.get("event") == "finish":
                    events.append(event)
    result = {"log": str(path), "call_count": len(events),
              "retry_calls": sum(e.get("attempt", 1) > 1 for e in events),
              "failed_calls": sum(e.get("status") == "error" for e in events),
              "request_seconds": sum(e["seconds"] for e in events)}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        counts = [(e.get("usage") or {}).get(key) for e in events]
        known = [v for v in counts if type(v) is int]
        result[key] = sum(known) if len(known) == len(counts) else None
        result["known_" + key] = sum(known)
        result["missing_" + key + "_calls"] = len(counts) - len(known)
    return result
