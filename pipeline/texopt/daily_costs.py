"""Persist provider usage, then price it without changing conversion counts."""

from decimal import Decimal
import hashlib
import json
from pathlib import Path


USAGE_VERSION = 1


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def billing_call(event, call_id):
    usage = event.get("usage") or {}
    raw = event.get("usage_raw") or {}
    details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", raw.get("cache_read_input_tokens"))
    written = details.get("cache_write_tokens", raw.get("cache_creation_input_tokens"))
    inputs = token_count(usage.get("input_tokens", raw.get("prompt_tokens", raw.get("input_tokens"))))
    outputs = token_count(usage.get("output_tokens", raw.get("completion_tokens", raw.get("output_tokens"))))
    cached_count, written_count = token_count(cached), token_count(written)
    cached_count = 0 if cached is None else cached_count
    written_count = 0 if written is None else written_count
    if inputs is not None and "input_tokens" in raw and "prompt_tokens" not in raw:
        # Anthropic reports cache reads/writes outside input_tokens; OpenAI includes them.
        if "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
            if cached_count is not None and written_count is not None:
                inputs += cached_count + written_count
    return {"call_id": call_id, "model": event.get("model"), "input_tokens": inputs,
            "output_tokens": outputs, "cached_input_tokens": cached_count,
            "cache_write_tokens": written_count,
            "cache_usage_unreported": cached is None or written is None}


def hydrate_billing(record):
    if record.get("billing_usage_version") == USAGE_VERSION:
        return False
    wanted = set(record["call_ids"])
    found = {}
    for path in sorted(Path(record["report"]).parent.glob("*.process.calls.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    call_id = event.get("call_id") or hashlib.sha256(line.encode()).hexdigest()
                    if event.get("event") == "finish" and call_id in wanted and call_id not in found:
                        found[call_id] = billing_call(event, call_id)
                except (ValueError, TypeError, AttributeError):
                    continue
    record["billing_calls"] = list(found.values())
    record["billing_missing_records"] = len(wanted - found.keys())
    record["billing_usage_version"] = USAGE_VERSION
    return True


def empty_cost():
    return {"estimated_usd_low": "0", "estimated_usd_high": "0", "priced_calls": 0,
            "unpriced_calls": 0, "tier_unresolved_calls": 0, "cache_usage_unreported_calls": 0,
            "ordinary_input_tokens": 0, "cached_input_tokens": 0, "cache_write_tokens": 0,
            "output_tokens": 0}


def add_cost(target, source):
    for key in target:
        if key.startswith("estimated_usd_"):
            target[key] = str(Decimal(target[key]) + Decimal(source[key]))
        else:
            target[key] += source[key]


def price_record(record, prices):
    total, models = empty_cost(), {}
    divisor = Decimal(prices["tokens_per_unit"])
    threshold = prices.get("long_context_above_tokens")
    for call in record.get("billing_calls", []):
        model = call["model"] or "unknown"
        tally = models.setdefault(model, empty_cost())
        rates = prices["models"].get(model)
        inputs, outputs = call["input_tokens"], call["output_tokens"]
        cached, written = call["cached_input_tokens"], call["cache_write_tokens"]
        if (not rates or any(token_count(v) is None for v in (inputs, outputs, cached, written))
                or cached + written > inputs):
            tally["unpriced_calls"] += 1
            continue
        tally["priced_calls"] += 1
        tally["cache_usage_unreported_calls"] += int(call["cache_usage_unreported"])
        tokens = {"input": inputs - cached - written, "cached_input": cached,
                  "cache_write": written, "output": outputs}
        if threshold is None:
            tiers = ("short", "long")
            tally["tier_unresolved_calls"] += 1
        else:
            tiers = ("long" if inputs > threshold else "short",)
        amounts = [sum(Decimal(tokens[key]) * Decimal(rates[tier][key]) for key in tokens) / divisor
                   for tier in tiers]
        tally["estimated_usd_low"] = str(Decimal(tally["estimated_usd_low"]) + min(amounts))
        tally["estimated_usd_high"] = str(Decimal(tally["estimated_usd_high"]) + max(amounts))
        for field, count in (("ordinary_input_tokens", tokens["input"]), ("cached_input_tokens", cached),
                             ("cache_write_tokens", written), ("output_tokens", outputs)):
            tally[field] += count
    for tally in models.values():
        add_cost(total, tally)
    total["unpriced_calls"] += record.get("billing_missing_records", len(record["call_ids"]))
    return {**total, "by_model": models}


def combine_costs(costs):
    total, models = empty_cost(), {}
    for cost in costs:
        add_cost(total, cost)
        for model, tally in cost.get("by_model", {}).items():
            add_cost(models.setdefault(model, empty_cost()), tally)
    return {**total, "by_model": models}


def cost_label(cost):
    if not cost["priced_calls"] and cost["unpriced_calls"]:
        return "未计价"
    low, high = (Decimal(cost[key]) for key in ("estimated_usd_low", "estimated_usd_high"))
    amount = f"${low:.4f}" if low == high else f"${low:.4f} - ${high:.4f}"
    return amount + ("（部分）" if cost["unpriced_calls"] else "")
