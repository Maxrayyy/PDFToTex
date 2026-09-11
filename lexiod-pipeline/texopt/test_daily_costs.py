from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

from .daily_costs import billing_call, combine_costs, hydrate_billing, price_record


PRICES = json.loads(Path(__file__).with_name("model_prices.json").read_text())
PRICES["long_context_above_tokens"] = None


def record_for(*calls):
    return {"call_ids": [call["call_id"] for call in calls], "billing_calls": list(calls),
            "billing_missing_records": 0}


def call(model="gpt-6-astra", inputs=1000, outputs=100, cached=400, written=100):
    return billing_call({"model": model, "usage": {"input_tokens": inputs, "output_tokens": outputs},
                         "usage_raw": {"prompt_tokens": inputs, "prompt_tokens_details": {
                             "cached_tokens": cached, "cache_write_tokens": written}}}, "call1")


def test_cached_read_and_write_not_double_charged():
    result = price_record(record_for(call()), PRICES)
    assert result["ordinary_input_tokens"] == 500
    assert result["cached_input_tokens"] == 400
    assert result["cache_write_tokens"] == 100
    assert Decimal(result["estimated_usd_low"]) == Decimal("0.01165")
    assert Decimal(result["estimated_usd_high"]) == Decimal("0.0208")
    assert result["tier_unresolved_calls"] == 1


def test_threshold_applies_to_total_input_including_cache_and_boundary():
    prices = deepcopy(PRICES)
    prices["long_context_above_tokens"] = 1000
    short = price_record(record_for(call()), prices)
    long = price_record(record_for(call(inputs=1001)), prices)
    assert short["estimated_usd_low"] == short["estimated_usd_high"]
    assert Decimal(long["estimated_usd_low"]) == Decimal("0.02082")
    assert long["tier_unresolved_calls"] == 0


def test_all_user_model_prices_and_precise_aggregation():
    costs = [price_record(record_for(call(model=model, inputs=1000000, outputs=1000000,
                                          cached=0, written=0)), PRICES)
             for model in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra")]
    assert [Decimal(c["estimated_usd_low"]) for c in costs] == [60, 24, 14]
    assert [Decimal(c["estimated_usd_high"]) for c in costs] == [95, 38, 22]
    total = combine_costs(costs)
    assert Decimal(total["estimated_usd_low"]) == 98
    assert Decimal(total["estimated_usd_high"]) == 155


def test_missing_usage_unknown_model_and_invalid_cache_are_unpriced():
    for item in (call(inputs=None), call(model="other"), call(cached=1001), call(written=-1)):
        result = price_record(record_for(item), PRICES)
        assert result["unpriced_calls"] == 1
        assert result["priced_calls"] == 0


def test_missing_cache_details_are_marked_as_estimated():
    item = billing_call({"model": "gpt-6-astra", "usage": {
        "input_tokens": 1000, "output_tokens": 100}}, "a")
    cost = price_record(record_for(item), PRICES)
    assert cost["cache_usage_unreported_calls"] == 1
    assert Decimal(cost["estimated_usd_low"]) == Decimal("0.015")


def test_exclusive_cache_usage_normalizes_to_total_input():
    item = billing_call({"model": "gpt-6-astra", "usage_raw": {"input_tokens": 500,
        "output_tokens": 100, "cache_read_input_tokens": 400, "cache_creation_input_tokens": 100}}, "a")
    assert item["input_tokens"] == 1000
    assert Decimal(price_record(record_for(item), PRICES)["estimated_usd_low"]) == Decimal("0.01165")


def test_backfill_only_owned_calls_deduplicates_and_survives_removed_logs(tmp_path):
    item = {"call_ids": ["owned", "interrupted"], "report": str(tmp_path / "report.json")}
    payload = {"call_id": "owned", "event": "finish", "model": "gpt-6-astra",
               "usage": {"input_tokens": 1000, "output_tokens": 100}}
    log = tmp_path / "recognize.process.calls.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (payload, payload, {**payload, "call_id": "not-owned"})))
    assert hydrate_billing(item)
    assert len(item["billing_calls"]) == 1
    assert item["billing_missing_records"] == 1
    before = price_record(item, PRICES)
    log.unlink()
    assert not hydrate_billing(item)
    assert price_record(item, PRICES) == before
    assert before["unpriced_calls"] == 1
