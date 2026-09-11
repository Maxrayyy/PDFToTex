import io
import json
import urllib.error
import urllib.request

import pytest

from .model_telemetry import request_json, summarize_calls


def test_provider_usage_counts_retries_and_keeps_unknown_totals(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("LEXOID_MODEL_CALL_LOG", str(log))
    calls = []

    def respond(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("private endpoint")
        return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                      "prompt_tokens_details": {"cached_tokens": 30}}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", respond)
    with pytest.raises(urllib.error.URLError):
        request_json(None, 60, stage="naming", model="test")
    request_json(None, 60, stage="naming", model="test", attempt=2)
    summary = summarize_calls(log)
    assert summary["call_count"] == 2
    assert summary["retry_calls"] == 1
    assert summary["total_tokens"] is None
    assert summary["known_total_tokens"] == 140
    assert summary["missing_total_tokens_calls"] == 1
    assert "private" not in log.read_text()
    assert json.loads(log.read_text().splitlines()[-1])["usage_raw"]["prompt_tokens_details"] == {"cached_tokens": 30}
