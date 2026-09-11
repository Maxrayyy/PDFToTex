"""
JSON field extractor for optimized LaTeX texopt.

Reads an optimized .tex (produced by lexoid + texopt) and extracts every
field as a structured JSON entry, combining:
  - VALUE_ID  (primary key from lexoid)
  - FIELD_VALUE (label / description)
  - \\fieldvalue{...} (actual rendered value)
  - \\hwfield{ID}{value} (handwritten fields with stable IDs)
  - Page numbers from LEXOID_PAGE_COMPLETED markers
  - Registry metadata (if fields.json is available)

Output: a single JSON file per .tex, plus optional merged output for batches.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .textio import read_text_auto, write_utf8_atomic


# ── regex patterns ─────────────────────────────────────────────────────────

# Lexoid page markers
_PAGE_COMPLETED = re.compile(
    r"%\s*LEXOID_PAGE_COMPLETED:\s*(\d+)\s*/\s*(\d+)", re.I
)
_PAGE_MARKER = re.compile(
    r"^\s*%+\s*(?:---+\s*)?page[\s_-]*(\d+)", re.I
)

# Lexoid field markers (comment lines)
_VALUE_ID = re.compile(r"%\s*#VALUE(?:\\)?_ID:\s*(\S+)", re.I)
_FIELD_VALUE = re.compile(r"%\s*#FIELD(?:\\)?_VALUE:\s*(.*)", re.I)
_HANDWRITTEN_COMMENT = re.compile(r"%\s*#HANDWRITTEN:\s*(.*)", re.I)
_TODO_HANDWRITTEN = re.compile(r"%\s*#TODO\s*#HANDWRITTEN:\s*(.*)", re.I)

# Rendered value macros
_FIELDVALUE = re.compile(r"\\fieldvalue\s*\{")
_HWFIELD = re.compile(r"\\hwfield\s*\{([^}]*)\}\s*\{")
_CHECKBOXFIELD = re.compile(
    r"\\checkboxfield\s*\{([^}]*)\}\s*\{(checked|unchecked)\}\s*\{",
    re.I,
)
_NESTED_CHECKBOXFIELD = re.compile(
    r"\\checkboxfield\s*\{(checked|unchecked|unclear)\}", re.I
)

# Lexoid field ID format
_LEX_ID = re.compile(r"LEX-P(\d+)-V(\d+)", re.I)

# texopt semantic alias format
_ALIAS_ID = re.compile(r"p(\d+)-([a-zA-Z0-9_]+)-([a-zA-Z0-9_]+)")


# ── data structures ────────────────────────────────────────────────────────


@dataclass
class ExtractedField:
    field_id: str
    semantic_alias: str = ""
    label: str = ""
    value: str = ""
    raw_value: str = ""           # unprocessed TeX content inside \fieldvalue{}
    page: int = 0
    total_pages: int = 0
    tex_line: int = 0
    is_handwritten: bool = False
    handwritten_value: str = ""
    todo: str = ""                # #TODO comments
    field_type: str = "text"      # text | checkbox
    checked: Optional[bool] = None
    # From registry (if available)
    table: str = ""
    semantic: str = ""
    row_header: str = ""
    col_header: str = ""
    name_source: str = ""


@dataclass
class ExtractionReport:
    source: str
    total_fields: int = 0
    total_pages: int = 0
    handwritten_count: int = 0
    fields: List[ExtractedField] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── brace-balanced extraction ─────────────────────────────────────────────


def _extract_balanced(text: str, start: int) -> tuple[str, int]:
    """
    Starting after an opening '{', extract the balanced content.
    Returns (content, end_position) where end_position is after the closing '}'.
    """
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{" :
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "\\":
            i += 1  # skip escaped char
        i += 1
    if depth != 0:
        return text[start:], len(text)
    return text[start:i - 1], i


# ── main extraction ────────────────────────────────────────────────────────


def extract_fields(
    tex_path: str | Path,
    registry_path: Optional[str | Path] = None,
) -> ExtractionReport:
    """
    Parse an optimized .tex file and extract all field entries.

    If *registry_path* points to a fields.json produced by texopt, its metadata
    is merged into each matching field record.
    """
    tex_path = Path(tex_path)
    text = read_text_auto(tex_path).text
    lines = text.splitlines()

    report = ExtractionReport(source=tex_path.name)

    # ── load registry (optional) ───────────────────────────────────
    registry: Dict[str, dict] = {}
    if registry_path:
        rp = Path(registry_path)
        if rp.exists():
            try:
                data = json.loads(rp.read_text("utf-8"))
                for f in data.get("fields", []):
                    registry[f.get("field_id", "")] = f
            except (json.JSONDecodeError, KeyError):
                report.warnings.append(f"Could not parse registry: {rp}")

    # ── line-by-line scan ──────────────────────────────────────────
    current_page = 0
    total_pages = 0
    pending_value_id: Optional[str] = None
    pending_field_value: Optional[str] = None
    pending_handwritten: Optional[str] = None
    pending_todo: Optional[str] = None
    pending_line: int = 0

    extracted: List[ExtractedField] = []

    # LEXOID_PAGE_COMPLETED is an end-of-page marker. Build the physical page map
    # before scanning so fields preceding the marker receive that page, not page 0.
    page_of_line = [0] * (len(lines) + 1)
    block_start = 1
    completed = []
    for line_no, line in enumerate(lines, start=1):
        m = _PAGE_COMPLETED.search(line)
        if m:
            completed.append((line_no, int(m.group(1)), int(m.group(2))))
    for end, page, _total in completed:
        for i in range(block_start, end + 1):
            page_of_line[i] = page
        block_start = end + 1
    if completed:
        for i in range(block_start, len(lines) + 1):
            page_of_line[i] = completed[-1][1] + 1

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if completed:
            current_page = page_of_line[line_no]
            total_pages = completed[-1][2]

        # Page tracking
        m = _PAGE_COMPLETED.search(stripped)
        if m:
            current_page = int(m.group(1))
            total_pages = int(m.group(2))
            continue
        m = _PAGE_MARKER.match(stripped)
        if m:
            current_page = int(m.group(1))
            continue

        # VALUE_ID
        m = _VALUE_ID.search(stripped)
        if m:
            # If there was a pending ID that never got a \fieldvalue, flush it
            if pending_value_id and pending_field_value:
                ef = ExtractedField(
                    field_id=pending_value_id,
                    label=pending_field_value or "",
                    page=current_page,
                    total_pages=total_pages,
                    tex_line=pending_line,
                    is_handwritten=bool(pending_handwritten),
                    handwritten_value=pending_handwritten or "",
                    todo=pending_todo or "",
                )
                _merge_registry(ef, registry)
                extracted.append(ef)
            pending_value_id = m.group(1).strip()
            pending_field_value = None
            pending_handwritten = None
            pending_todo = None
            pending_line = line_no
            continue

        # FIELD_VALUE
        m = _FIELD_VALUE.search(stripped)
        if m:
            pending_field_value = m.group(1).strip()
            continue

        # HANDWRITTEN
        m = _HANDWRITTEN_COMMENT.search(stripped)
        if m:
            pending_handwritten = m.group(1).strip()
            continue

        # TODO HANDWRITTEN
        m = _TODO_HANDWRITTEN.search(stripped)
        if m:
            pending_todo = m.group(1).strip()
            continue

        # \fieldvalue{...} — rendered value line
        m = _FIELDVALUE.search(stripped)
        if m and pending_value_id:
            # Extract balanced content from \fieldvalue{...}
            open_pos = m.end()  # position right after the opening {
            content, _ = _extract_balanced(stripped, open_pos)
            raw_value = content

            nested_checkbox = _NESTED_CHECKBOXFIELD.search(content)
            state = nested_checkbox.group(1).lower() if nested_checkbox else None
            ef = ExtractedField(
                field_id=pending_value_id,
                label=pending_field_value or "",
                value=state or _strip_tex(content),
                raw_value=state or raw_value,
                page=current_page,
                total_pages=total_pages,
                tex_line=line_no,
                is_handwritten=(bool(pending_handwritten)
                                or r"\handwritten{" in content),
                handwritten_value=pending_handwritten or "",
                todo=pending_todo or "",
                field_type="checkbox" if state else "text",
                checked=(state == "checked" if state in ("checked", "unchecked")
                         else None),
            )

            # Check if value contains \hwfield{ID}{val}
            hw = _HWFIELD.search(content)
            if hw:
                ef.is_handwritten = True
                if not ef.handwritten_value:
                    # Extract the value arg of \hwfield
                    hw_start = hw.end()
                    hw_val, _ = _extract_balanced(content, hw_start)
                    ef.handwritten_value = _strip_tex(hw_val)

            _merge_registry(ef, registry)
            extracted.append(ef)

            # Reset pending state
            pending_value_id = None
            pending_field_value = None
            pending_handwritten = None
            pending_todo = None
            continue

        # \checkboxfield{ID}{checked|unchecked}{visible option label}
        m = _CHECKBOXFIELD.search(stripped)
        if m:
            checkbox_id = m.group(1).strip()
            state = m.group(2).lower()
            label_start = m.end()
            option_label, _ = _extract_balanced(stripped, label_start)
            if pending_value_id and pending_value_id != checkbox_id:
                report.warnings.append(
                    f"line {line_no}: checkbox ID {checkbox_id} does not match "
                    f"pending VALUE_ID {pending_value_id}"
                )
            ef = ExtractedField(
                field_id=checkbox_id,
                label=pending_field_value or _strip_tex(option_label),
                value=state,
                raw_value=state,
                page=current_page,
                total_pages=total_pages,
                tex_line=line_no,
                field_type="checkbox",
                checked=state == "checked",
            )
            _merge_registry(ef, registry)
            extracted.append(ef)
            pending_value_id = None
            pending_field_value = None
            pending_handwritten = None
            pending_todo = None
            continue

        # Standalone \hwfield{ID}{value} without preceding VALUE_ID
        m = _HWFIELD.search(stripped)
        if m and not pending_value_id:
            hw_id = m.group(1).strip()
            hw_start = m.end()
            hw_val, _ = _extract_balanced(stripped, hw_start)
            ef = ExtractedField(
                field_id=hw_id,
                value=_strip_tex(hw_val),
                raw_value=hw_val,
                page=current_page,
                total_pages=total_pages,
                tex_line=line_no,
                is_handwritten=True,
                handwritten_value=_strip_tex(hw_val),
            )
            _merge_registry(ef, registry)
            extracted.append(ef)

    # Flush last pending field
    if pending_value_id and pending_field_value:
        ef = ExtractedField(
            field_id=pending_value_id,
            label=pending_field_value or "",
            page=current_page,
            total_pages=total_pages,
            tex_line=pending_line,
            is_handwritten=bool(pending_handwritten),
            handwritten_value=pending_handwritten or "",
            todo=pending_todo or "",
        )
        _merge_registry(ef, registry)
        extracted.append(ef)

    report.fields = extracted
    report.total_fields = len(extracted)
    report.total_pages = total_pages or current_page
    report.handwritten_count = sum(1 for f in extracted if f.is_handwritten)

    return report


def _merge_registry(ef: ExtractedField, registry: Dict[str, dict]) -> None:
    """Enrich an extracted field with registry metadata if available."""
    reg = registry.get(ef.field_id)
    if not reg:
        return
    ef.table = ef.table or reg.get("table", "")
    ef.semantic_alias = ef.semantic_alias or reg.get("semantic_alias", "")
    ef.semantic = ef.semantic or reg.get("semantic", "")
    ef.row_header = ef.row_header or reg.get("row_header", "")
    ef.col_header = ef.col_header or reg.get("col_header", "")
    ef.name_source = ef.name_source or reg.get("name_source", "")
    if not ef.label and reg.get("semantic"):
        ef.label = reg["semantic"]


def _strip_tex(s: str) -> str:
    """Remove common TeX markup to produce a plain-text value."""
    s = re.sub(r"\\(?:textbf|textit|texttt|emph|mbox|hbox|underline)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\hwfield\s*\{[^}]*\}\s*\{([^}]*)\}", r"\1", s)
    s = re.sub(r"\\(?:SA|hbox|leavevmode|relax)\s*\{\}", "", s)
    s = re.sub(r"\\[a-zA-Z@]+\*?", "", s)
    s = re.sub(r"[{}$&#_^~]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ── output ─────────────────────────────────────────────────────────────────


def write_json(report: ExtractionReport, output_path: str | Path) -> Path:
    """Write the extraction report as a JSON file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "source": report.source,
        "total_fields": report.total_fields,
        "total_pages": report.total_pages,
        "handwritten_count": report.handwritten_count,
        "warnings": report.warnings,
        "fields": [asdict(f) for f in report.fields],
    }
    write_utf8_atomic(output_path, json.dumps(data, ensure_ascii=False, indent=2))
    return output_path


def merge_reports(reports: List[ExtractionReport], output_path: str | Path) -> Path:
    """Merge multiple extraction reports into a single JSON."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_fields = []
    for r in reports:
        for f in r.fields:
            d = asdict(f)
            d["_source"] = r.source
            all_fields.append(d)
    data = {
        "version": 1,
        "sources": [r.source for r in reports],
        "total_fields": len(all_fields),
        "total_pages": max((r.total_pages for r in reports), default=0),
        "handwritten_count": sum(r.handwritten_count for r in reports),
        "fields": all_fields,
    }
    write_utf8_atomic(output_path, json.dumps(data, ensure_ascii=False, indent=2))
    return output_path
