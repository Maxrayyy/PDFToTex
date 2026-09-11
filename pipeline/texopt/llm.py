"""
Semantic naming of tables and handwritten fields, done by an LLM, one call per TABLE.

WHY PER-TABLE AND NOT PER-CELL
------------------------------
  * Field names inside one table must be mutually distinct. A per-cell call cannot see
    its siblings, so it happily returns `value` three times.
  * The table slug and its field slugs should agree in vocabulary
    (`inspection_record` / `outer_diameter_measured`, not `jianyan` / `od_meas`).
  * One call per table instead of one per cell is ~10-30x cheaper on a form-heavy doc.

DETERMINISM
-----------
The whole request is hashed and the whole response cached. Re-running the optimiser on
an unchanged document reproduces byte-identical field IDs -- required, because these
IDs are consumed downstream.

NO NETWORK REQUIRED
-------------------
HeuristicBatchNamer is a complete offline implementation. It produces positional names
(`r01c02`) for CJK input, which are stable and greppable but not meaningful -- use it
only as a fallback, and the report will tell you how many fields ended up on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from .model_telemetry import request_json
from dataclasses import dataclass, field as dc_field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from .fields import is_hashy, slug, tex_to_plain
from .model_config import resolve_model
from .tex_tables import _read_balanced, _skip_ws, mask_comments
from .naming_cache import NamingCache, is_sqlite_lock_error

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


def openai_api_url() -> str:
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return f"{base_url.rstrip('/')}/chat/completions"


def _is_openai_model(model: str) -> bool:
    m = model.lower()
    return m.startswith(("gpt", "o1", "o3", "o4"))


# --------------------------------------------------------------------------- #
# request / response shapes
# --------------------------------------------------------------------------- #

@dataclass
class FieldSpec:
    key: str            # stable positional key, e.g. "r01c02"
    row_header: str
    col_header: str
    value: str
    label: str = ""       # Lexoid #FIELD_VALUE; strongest semantic signal
    kind: str = "value"
    unit: str = ""


@dataclass
class TableNameRequest:
    page: int
    section: str = ""          # nearest preceding \section*{...}
    bold_title: str = ""       # nearest preceding \textbf{...} sitting above the table
    caption: str = ""
    ordinal: int = 0           # 1-based index of this table within the document
    headers: List[str] = dc_field(default_factory=list)
    sample_rows: List[List[str]] = dc_field(default_factory=list)
    fields: List[FieldSpec] = dc_field(default_factory=list)
    structure: List[List[int]] = dc_field(default_factory=list)

    def payload(self) -> dict:
        return {
            "page": self.page,
            "table_index_in_document": self.ordinal,
            "section_heading": tex_to_plain(self.section),
            "bold_title_above_table": tex_to_plain(self.bold_title),
            "caption": tex_to_plain(self.caption),
            "column_headers": [tex_to_plain(h) for h in self.headers],
            "sample_rows": [[tex_to_plain(c) for c in r] for r in self.sample_rows[:4]],
            "fields": [{"key": f.key,
                        "field_label": tex_to_plain(f.label),
                        "row_label": tex_to_plain(f.row_header),
                        "column_label": tex_to_plain(f.col_header),
                        "value": tex_to_plain(f.value)} for f in self.fields],
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.payload(), ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()[:20]

    def semantic_payload(self) -> dict:
        def context(text):
            text = mask_comments(text)
            matches = list(re.finditer(r"\\(?:fieldvalue|handwritten|hwfield)\b", text))
            edits, covered = [], -1
            for match in matches:
                if match.start() < covered:
                    continue
                arg = _read_balanced(text, _skip_ws(text, match.end()), "{", "}")
                if arg is None:
                    continue
                if match.group() == r"\hwfield":
                    arg = _read_balanced(text, _skip_ws(text, arg[1]), "{", "}")
                    if arg is None:
                        continue
                covered = arg[1]
                edits.append((match.start(), covered))
            for start, end in reversed(edits):
                text = text[:start] + "VALUE" + text[end:]
            return tex_to_plain(text)

        fields = []
        for f in self.fields:
            label, row, column = (context(s) for s in (f.label, f.row_header, f.col_header))
            if row == tex_to_plain(f.value) or row == "VALUE":
                row = ""
            value = tex_to_plain(f.value)
            unit = f.unit
            if not unit:
                match = re.fullmatch(r"\s*[+-]?\d+(?:[.,]\d+)?\s*([A-Za-z%°℃μµ]+(?:/[A-Za-z]+)?)\s*", value)
                unit = match[1] if match else ""
            fields.append({"key": f.key, "field_label": label, "row_label": row,
                           "column_label": column, "kind": f.kind, "unit": unit,
                           "value_context": value if not (label or row or column) else ""})
        return {"section_heading": context(self.section), "bold_title_above_table": context(self.bold_title),
                "caption": context(self.caption), "column_headers": [context(h) for h in self.headers],
                "sample_rows": [[context(c) for c in r] for r in self.sample_rows[:4]],
                "structure": self.structure, "fields": fields}

    def semantic_fingerprint(self, model, provider):
        return hashlib.sha256(json.dumps({"schema": "semantic-naming/v2", "model": model,
            "provider": provider, "prompt": SYSTEM + PROMPT, "request": self.semantic_payload()},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class TableNaming:
    table: str
    fields: Dict[str, str]
    source: str = "llm"          # llm | cache | heuristic


class BatchNamer(Protocol):
    def name_table(self, req: TableNameRequest) -> TableNaming: ...


# --------------------------------------------------------------------------- #
# offline fallback
# --------------------------------------------------------------------------- #

def _ok(s: str) -> str:
    return "" if (not s or s == "field" or is_hashy(s)) else s


class HeuristicBatchNamer:
    """Deterministic, no network. Meaningful only when the source text is ASCII."""

    def name_table(self, req: TableNameRequest) -> TableNaming:
        table = ""
        for cand in (req.bold_title, req.caption, req.section):
            table = _ok(slug(cand, 30))
            if table:
                break
        # Without a derivable title, two CJK-titled tables on the same page would
        # collide on `tab_p076`. Disambiguate by document ordinal.
        table = table or (f"tab_p{req.page:03d}_t{req.ordinal}" if req.ordinal
                          else f"tab_p{req.page:03d}")

        out, used = {}, set()
        for f in req.fields:
            label = _ok(slug(f.label, 40))
            parts = ([label] if label else
                     [p for p in (_ok(slug(f.row_header, 24)),
                                  _ok(slug(f.col_header, 20))) if p])
            name = "_".join(dict.fromkeys(parts)) or f.key
            while name in used:
                name = f"{name}_{f.key}"
            used.add(name)
            out[f.key] = name
        return TableNaming(table=table, fields=out, source="heuristic")


class DeferredBatchNamer:
    """Stable technical identifiers while optional semantic enrichment is pending."""

    def name_table(self, req):
        return TableNaming(f"tab_p{req.page:03d}_t{req.ordinal}",
                           {f.key: f.key for f in req.fields}, "deferred")


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #

SYSTEM = (
    "You name form fields extracted from scanned engineering / administrative "
    "documents. You output ONLY JSON. Never explain."
)

PROMPT = """\
Below is one table from a document, as JSON. Some cells hold handwritten values that
need machine-readable identifiers.

{payload}

Return JSON with exactly this shape:
{{"table": "<slug>", "fields": {{"<key>": "<slug>", ...}}}}

Rules:
- Every slug is lowercase ASCII snake_case, 2-4 words, no digits unless the source
  text has them, no prefixes, no units.
- "table" describes what the table IS, derived from bold_title_above_table first,
  then caption, then section_heading. Example: "inspection_record", "vendor_details".
- Each field slug describes what the VALUE MEANS. Prefer field_label when present,
  then use row_label and column_label.
  Example row_label "外径" + column_label "实测值" -> "outer_diameter_measured".
- Translate non-English source text into English. Do not transliterate.
- Field slugs must be mutually distinct within this table.
- Include every key from "fields". Do not invent keys.
"""


class LLMBatchNamer:
    """
    OpenAI-compatible or Anthropic API, cached by semantic structure.
    Falls back to HeuristicBatchNamer on any error -- naming must never block the
    optimisation, but the report records which tables fell back.
    """

    def __init__(self, cache_path: Path, model: Optional[str] = None,
                 api_key: Optional[str] = None, max_retries: int = 2,
                 timeout: int = 60) -> None:
        self.cache_path = Path(cache_path)
        # Keep legacy JSON files intact; their value-sensitive keys are not reusable.
        if self.cache_path.suffix == ".json":
            self.cache_path = self.cache_path.with_suffix(".sqlite3")
        self.model = resolve_model("TEXOPT_MODEL", model)
        provider = os.getenv("TEXOPT_NAMING_PROVIDER", "auto").strip().lower()
        if provider not in {"auto", "openai", "anthropic"}:
            raise ValueError("TEXOPT_NAMING_PROVIDER must be auto, openai or anthropic")
        self.is_openai = (provider == "openai" or
                          (provider == "auto" and (_is_openai_model(self.model) or
                           self.model.lower().startswith(("kimi", "glm", "zhipu/")))))
        if self.is_openai:
            base = os.getenv("TEXOPT_NAMING_BASE_URL", "").strip()
            self.api_url = f"{base.rstrip('/')}/chat/completions" if base else openai_api_url()
            # An independent endpoint must never receive the vision provider's key.
            self.api_key = (api_key or os.getenv("TEXOPT_NAMING_API_KEY") or
                            ("" if base else os.getenv("OPENAI_API_KEY", "")))
        else:
            self.api_url = ANTHROPIC_API_URL
            self.api_key = api_key or os.getenv("TEXOPT_NAMING_API_KEY") or os.getenv("ANTHROPIC_API_KEY", "")
        self.max_retries = min(1, max(0, max_retries))
        self.timeout = timeout
        self.fallback = HeuristicBatchNamer()
        self.stats: Dict[str, int] = {"cache": 0, "llm": 0, "heuristic": 0}

    # ---- public ---------------------------------------------------------- #
    def name_table(self, req: TableNameRequest) -> TableNaming:
        fp = req.semantic_fingerprint(self.model, self.api_url)

        def valid(raw):
            if (not isinstance(raw, dict) or not isinstance(raw.get("table"), str)
                    or not _ok(slug(raw["table"], 30)) or not isinstance(raw.get("fields"), dict)
                    or set(raw["fields"]) != {f.key for f in req.fields}
                    or any(not isinstance(v, str) or not _ok(slug(v, 40)) for v in raw["fields"].values())):
                return False
            return True

        def compute():
            raw = self._call(req) if self.api_key else None
            return raw if valid(raw) else None

        try:
            raw, source = NamingCache(self.cache_path).get_or_compute(
                fp, compute, timeout=(self.max_retries + 1) * self.timeout + 20)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "naming_cache_error page=%s table=%s error_type=%s reason=%s "
                "sqlite_code=%s fallback=heuristic",
                req.page, req.ordinal, type(exc).__name__,
                "lock_timeout" if is_sqlite_lock_error(exc) else "cache_unavailable",
                getattr(exc, "sqlite_errorcode", None))
            raw, source = None, "cache_error"
        if not valid(raw):
            self.stats["heuristic"] += 1
            return self.fallback.name_table(req)
        self.stats[source] += 1
        return self._finish(req, raw.get("table", ""), raw.get("fields", {}), source)

    # ---- internals ------------------------------------------------------- #
    def _finish(self, req: TableNameRequest, table: str, fields: dict,
                source: str) -> TableNaming:
        """Sanitise whatever came back; never trust the model's formatting."""
        fb = self.fallback.name_table(req)
        table = _ok(slug(table, 30)) or fb.table

        out, used = {}, set()
        for f in req.fields:
            cand = _ok(slug(str(fields.get(f.key, "")), 40)) or fb.fields[f.key]
            base, i = cand, 2
            while cand in used:               # model returned duplicates
                cand = f"{base}_{i}"
                i += 1
            used.add(cand)
            out[f.key] = cand
        return TableNaming(table=table, fields=out, source=source)

    def _call(self, req: TableNameRequest) -> Optional[dict]:
        if self.is_openai:
            return self._call_openai(req)
        return self._call_anthropic(req)

    def _call_openai(self, req: TableNameRequest) -> Optional[dict]:
        body = json.dumps({
            "model": self.model,
            ("max_completion_tokens" if _is_openai_model(self.model) else "max_tokens"): 4096,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": PROMPT.format(
                    payload=json.dumps(req.semantic_payload(), ensure_ascii=False, separators=(",", ":")))},
            ],
        }).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            try:
                rq = urllib.request.Request(self.api_url, data=body, method="POST", headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {self.api_key}",
                })
                data = request_json(rq, self.timeout, stage="naming", model=self.model,
                                    attempt=attempt + 1, page=req.page, table=req.ordinal)
                text = data["choices"][0]["message"]["content"]
                return self._parse(text)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
                if attempt == self.max_retries:
                    return None
        return None

    def _call_anthropic(self, req: TableNameRequest) -> Optional[dict]:
        body = json.dumps({
            "model": self.model,
            "max_tokens": 1000,
            "temperature": 0,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": PROMPT.format(
                payload=json.dumps(req.semantic_payload(), ensure_ascii=False, separators=(",", ":")))}],
        }).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            try:
                rq = urllib.request.Request(ANTHROPIC_API_URL, data=body, method="POST", headers={
                    "content-type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                })
                data = request_json(rq, self.timeout, stage="naming", model=self.model,
                                    attempt=attempt + 1, page=req.page, table=req.ordinal)
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
                return self._parse(text)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
                if attempt == self.max_retries:
                    return None
        return None

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        t = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M).strip()
        try:
            obj = json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", t, re.S)          # model added prose anyway
            if not m:
                return None
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return obj if isinstance(obj, dict) and isinstance(obj.get("fields"), dict) else None
