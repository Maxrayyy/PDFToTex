"""
Handwritten-value detection and stable field-ID assignment.

Field ID format (as specified):   <page>-<tabularname>-<semantic>
e.g.                              p076-invoice_items-unit_price

Design constraints honoured here
  * IDEMPOTENT: re-running must not renumber existing fields. The registry is keyed by
    a content hash of (page, table, row-header, col-header, raw value), so a second run
    over the same document reproduces the same IDs.
  * NON-DESTRUCTIVE: a detected value `X` becomes `\\hwfield{ID}{X}`. `\\hwfield` is
    defined to typeset `#2` and nothing else, so rendering is unchanged.
  * PLUGGABLE DETECTION: how lexoid marks handwritten content is *not* known to this
    module. Configure `DetectorConfig.patterns` to match your actual output.

This pass runs AFTER tex_tables.transform_tex(); field arguments may span lines.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field as dc_field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol

from .tex_tables import ALIGN_ENVS, SYNC_ANCHOR, _read_balanced, mask_comments, multicolumn_span
from .textio import write_utf8_atomic

# --------------------------------------------------------------------------- #
# page tracking
# --------------------------------------------------------------------------- #

#: ASSUMPTION -- confirm against real lexoid output and adjust.
DEFAULT_PAGE_MARKERS = [
    re.compile(r"^\s*%+\s*(?:---+\s*)?page[\s_-]*(\d+)", re.I),
    re.compile(r"^\s*%+\s*\[?\s*p\.?\s*(\d+)\s*\]?\s*$", re.I),
    re.compile(r"^\s*%\s*lexoid:page=(\d+)", re.I),
]
LEXOID_PAGE_COMPLETED = re.compile(
    r"^\s*%+\s*LEXOID_PAGE_COMPLETED:\s*(\d+)\s*/\s*(\d+)", re.I
)

PAGE_ADVANCE = re.compile(r"\\(?:newpage|clearpage|pagebreak)\b")


class PageTracker:
    """Maps a line index to the *source PDF* page number."""

    def __init__(self, start_page: int = 1,
                 markers: Optional[List[re.Pattern]] = None) -> None:
        self.page = start_page
        self.markers = markers if markers is not None else DEFAULT_PAGE_MARKERS
        self.saw_explicit_marker = False

    def feed(self, line: str) -> int:
        for pat in self.markers:
            m = pat.match(line)
            if m:
                self.page = int(m.group(1))
                self.saw_explicit_marker = True
                return self.page
        if not self.saw_explicit_marker and PAGE_ADVANCE.search(line):
            self.page += 1
        return self.page


def page_map(lines: List[str], start_page: int = 1) -> List[int]:
    """Map source lines to physical pages, preferring Lexoid end-of-page markers."""
    completed = []
    for i, line in enumerate(lines):
        m = LEXOID_PAGE_COMPLETED.match(line)
        if m:
            completed.append((i, int(m.group(1))))
    if not completed:
        tracker = PageTracker(start_page)
        return [tracker.feed(line) for line in lines]

    pages = [start_page] * len(lines)
    begin = 0
    for end, page in completed:
        for i in range(begin, end + 1):
            pages[i] = page
        begin = end + 1
    trailing_page = completed[-1][1] + 1
    for i in range(begin, len(lines)):
        pages[i] = trailing_page
    return pages


# --------------------------------------------------------------------------- #
# slugging / naming
# --------------------------------------------------------------------------- #

_STRIP_TEX = re.compile(r"\\[a-zA-Z@]+\*?|[{}$&#_^~\\]|\\SA\{\}")


def tex_to_plain(s: str) -> str:
    s = s.replace(SYNC_ANCHOR, " ")
    s = re.sub(r"\\(?:textbf|textit|texttt|emph|mbox|hbox|underline)\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\multicolumn\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\{(.*)\}\s*$", r"\1", s)
    s = _STRIP_TEX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


HASHY = re.compile(r"^x[0-9a-f]{6}(_x[0-9a-f]{6})*$")


def is_hashy(s: str) -> bool:
    """True when slug() had to fall back to a hash (i.e. the text was non-ASCII)."""
    return bool(HASHY.fullmatch(s or ""))


def slug(s: str, max_len: int = 40) -> str:
    """ASCII-safe slug. CJK is transliteration-free -> falls back to a short hash."""
    plain = tex_to_plain(s)
    ascii_form = unicodedata.normalize("NFKD", plain).encode("ascii", "ignore").decode()
    out = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_form).strip("_").lower()
    if not out and plain:
        out = "x" + hashlib.sha1(plain.encode("utf-8")).hexdigest()[:6]
    return (out[:max_len].rstrip("_") or "field")


# --------------------------------------------------------------------------- #
# semantic naming strategies
# --------------------------------------------------------------------------- #

@dataclass
class CellContext:
    page: int
    table: str
    row_header: str
    col_header: str
    raw_value: str
    line_no: int
    neighbours: List[str] = dc_field(default_factory=list)
    row_index: int = -1
    col_index: int = -1

    def fingerprint(self) -> str:
        blob = "|".join([str(self.page), self.table, self.row_header,
                         self.col_header, self.raw_value])
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class SemanticNamer(Protocol):
    def name(self, ctx: CellContext) -> str: ...


class HeuristicNamer:
    """
    Deterministic, offline, no API.

    LIMITATION (matters for your documents): slug() is ASCII-only, so Chinese headers
    degrade to an unreadable hash. Rather than emit `x48c3bf_x273524`, we fall back to
    a positional name `r03c02` -- stable, greppable, and obviously provisional, so a
    reviewer can see at a glance which fields still need real semantics.
    Use CachedLLMNamer for meaningful names on CJK documents.
    """

    def name(self, ctx: CellContext) -> str:
        parts = []
        for src in (ctx.row_header, ctx.col_header):
            s = slug(src, 24)
            if s and not s.startswith("x") or (s and not re.fullmatch(r"x[0-9a-f]{6}", s)):
                parts.append(s)
        parts = [p for p in dict.fromkeys(parts) if p and p != "field"]
        if parts:
            return "_".join(parts)
        if ctx.row_index >= 0:
            return f"r{ctx.row_index:02d}c{max(ctx.col_index,0):02d}"
        return "value"


class CachedLLMNamer:
    """
    Wraps any callable(prompt) -> str with a persistent cache keyed by fingerprint,
    so IDs never drift between runs and the API is called once per distinct cell.
    Falls back to HeuristicNamer on any error.
    """

    PROMPT = (
        "You are naming a form field extracted from a scanned document.\n"
        "Table: {table}\nRow label: {row}\nColumn label: {col}\n"
        "Handwritten value: {val}\nNearby text: {near}\n\n"
        "Reply with ONE snake_case English identifier (2-4 words, ASCII, no prefix) "
        "describing the MEANING of this field. No explanation, no quotes."
    )

    def __init__(self, call: Callable[[str], str], cache_path: Path) -> None:
        self.call = call
        self.cache_path = Path(cache_path)
        self.cache: Dict[str, str] = {}
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text("utf-8"))
        self.fallback = HeuristicNamer()

    def name(self, ctx: CellContext) -> str:
        fp = ctx.fingerprint()
        if fp in self.cache:
            return self.cache[fp]
        try:
            raw = self.call(self.PROMPT.format(
                table=ctx.table, row=tex_to_plain(ctx.row_header),
                col=tex_to_plain(ctx.col_header), val=tex_to_plain(ctx.raw_value),
                near=" / ".join(tex_to_plain(n) for n in ctx.neighbours[:4]),
            ))
            out = slug(raw.strip().splitlines()[0] if raw.strip() else "")
        except Exception:
            out = ""
        if not out or out == "field":
            out = self.fallback.name(ctx)
        self.cache[fp] = out
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), "utf-8")
        return out


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

@dataclass
class DetectorConfig:
    #: ASSUMPTION -- replace with the markers lexoid actually emits.
    patterns: List[str] = dc_field(default_factory=lambda: [
        r"\\handwritten\s*\{(?P<val>.*)\}\s*$",
        r"\\hw\s*\{(?P<val>.*)\}\s*$",
        r"^\s*\[\s*handwritten\s*:\s*(?P<val>.*?)\s*\]\s*$",
        r"^\s*\{\\itshape\s+(?P<val>.+)\}\s*$",
    ])
    #: also treat any non-empty cell in these column indices as handwritten (0-based)
    force_columns: List[int] = dc_field(default_factory=list)

    def compiled(self) -> List[re.Pattern]:
        return [re.compile(p) for p in self.patterns]


@dataclass
class FieldRecord:
    field_id: str
    semantic_alias: str
    page: int
    table: str
    semantic: str
    row_header: str
    col_header: str
    value: str
    tex_line: int
    fingerprint: str
    title_source: str = ""     # the \section / \textbf the table name came from
    name_source: str = ""      # llm | cache | heuristic
    label: str = ""
    row: int | None = None
    column: int | None = None
    name_status: str = "pending"


# --------------------------------------------------------------------------- #
# main pass
# --------------------------------------------------------------------------- #

BEGIN_ALIGN = re.compile(r"\\begin\{(" + "|".join(re.escape(e) for e in ALIGN_ENVS) + r")\}")
END_ALIGN = re.compile(r"\\end\{(" + "|".join(re.escape(e) for e in ALIGN_ENVS) + r")\}")
LABEL_RE = re.compile(r"\\label\{([^}]*)\}")
CAPTION_RE = re.compile(r"\\caption\s*\{")
HEADING_RE = re.compile(r"\\(?:(?:sub)*section|paragraph|chapter)\*?\s*\{([^}]*)\}")
#: a bold run used as a table title, e.g. a line that is just \textbf{检验记录}
BOLD_RE = re.compile(r"\\(?:textbf|bfseries\s+|textsc|Large|large)\s*\{?([^{}]{1,80})\}?")
BOLD_LINE_RE = re.compile(r"^\s*(?:\\(?:noindent|centering|par|vspace\*?\{[^}]*\}|hfill)\s*)*"
                          r"\\(?:textbf|textsc)\s*\{(.+?)\}\s*(?:\\\\)?\s*$")
RULE_RE = re.compile(r"\\(?:hline|midrule|toprule|bottomrule|cline|cmidrule)\b")
ROW_END = re.compile(
    r"(?:\\\\|\\tabularnewline)\*?(?:\[[^\]]*\])?\s*$"
)
CELL_END = re.compile(
    r"\s*(?:&|(?:\\\\|\\tabularnewline)\*?(?:\[[^\]]*\])?)\s*$"
)
VALUE_ID_RE = re.compile(r"%\s*#VALUE(?:\\)?_ID:\s*(\S+)", re.I)
FIELD_VALUE_RE = re.compile(r"%\s*#FIELD(?:\\)?_VALUE:\s*(.*)", re.I)
FIELDVALUE_RE = re.compile(r"\\fieldvalue\s*\{")
HWFIELD_RE = re.compile(r"\\hwfield\s*\{")


def _cell_payload(line: str) -> Optional[str]:
    """Return the cell content of a formatted cell line, or None if it isn't one."""
    s = line.strip()
    if not s or s.startswith("%"):
        return None
    if RULE_RE.match(s) or s.startswith("\\begin") or s.startswith("\\end"):
        return None
    return CELL_END.sub("", s)


@dataclass
class TableBlock:
    begin: int                      # line index of \\begin{...}
    end: int                        # line index of \\end{...}
    rows: List[List[int]]           # per row: line indices of its cells
    rule_after: set                 # row indices immediately followed by a rule
    header_row: Optional[int] = None


def _parse_tables(lines: List[str]) -> List[TableBlock]:
    """Parse the ALREADY-FORMATTED tex (one cell per line) into table blocks."""
    blocks: List[TableBlock] = []
    stack: List[int] = []
    rows: List[List[int]] = []
    cur: List[int] = []
    rule_after: set = set()

    for i, line in enumerate(lines):
        if BEGIN_ALIGN.search(line):
            stack.append(i)
            if len(stack) == 1:
                rows, cur, rule_after = [], [], set()
            continue
        if END_ALIGN.search(line):
            if not stack:
                continue
            begin = stack.pop()
            if not stack:
                if cur:
                    rows.append(cur)
                blocks.append(TableBlock(begin, i, rows, rule_after))
                rows, cur, rule_after = [], [], set()
            continue
        if len(stack) != 1:
            continue                       # inside a nested table: handled separately
        if RULE_RE.match(line.strip()):
            if cur:
                rows.append(cur)
                cur = []
            rule_after.add(len(rows) - 1)
            continue
        if _cell_payload(line) is None:
            continue
        cur.append(i)
        if ROW_END.search(line.rstrip()):
            rows.append(cur)
            cur = []
    return blocks


def _decide_header(block: TableBlock, lines: List[str], pats, cfg) -> Optional[int]:
    """
    Row 0 is a header iff: >=2 rows, it contains no handwritten value, and a rule
    immediately follows it. Misclassification only degrades col_header quality --
    it never affects ID stability or rendering.
    """
    if len(block.rows) < 2 or 0 not in block.rule_after:
        return None
    for c, li in enumerate(block.rows[0]):
        if _match_handwritten(_cell_payload(lines[li]) or "", pats, c, cfg) is not None:
            return None
    return 0


def annotate_fields(tex: str,
                    start_page: int = 1,
                    detector: Optional[DetectorConfig] = None,
                    namer=None,
                    naming_requests=None,
                    ) -> tuple[str, List["FieldRecord"], Dict[str, int]]:
    """
    Wrap detected handwritten cells in \\hwfield{ID}{...}.

    Two phases, because naming is a per-TABLE decision:
      phase 1 -- parse tables, extract title context, collect candidate fields
      phase 2 -- ask the namer for the whole table at once, then rewrite

    Returns (new_tex, records, namer_stats).
    """
    from .llm import FieldSpec, HeuristicBatchNamer, TableNameRequest

    detector = detector or DetectorConfig()
    namer = namer or HeuristicBatchNamer()
    pats = detector.compiled()

    lines = tex.split("\n")
    # Structural scanning MUST use the comment mask: the conversion pass leaves an
    # audit comment containing a literal \\begin{tabularx}{...}, which would otherwise
    # be parsed as a real table and swallow the whole block.
    masked = mask_comments(tex).split("\n")

    page_of = page_map(lines, start_page)

    headings, cur_h = [], ""
    for ln in lines:
        m = HEADING_RE.search(ln)
        if m:
            cur_h = m.group(1)
        headings.append(cur_h)

    blocks = _parse_tables(masked)
    records: List[FieldRecord] = []
    used_ids: Dict[str, int] = {}
    stats: Dict[str, int] = {"tables": 0, "fields": 0}

    for ordinal, block in enumerate(blocks, 1):
        block.header_row = _decide_header(block, masked, pats, detector)
        headers = ([_cell_payload(masked[li]) or "" for li in block.rows[block.header_row]]
                   if block.header_row is not None else [])

        # ---- phase 1: collect ------------------------------------------- #
        # (line_idx, payload, value, key, row_hdr, col_hdr, value_id, field_label)
        candidates = []
        multiline_fields = {}
        sample_rows = []
        for r_i, row in enumerate(block.rows):
            cells = [_cell_payload(masked[li]) or "" for li in row]
            if r_i != block.header_row and len(sample_rows) < 4:
                sample_rows.append(cells)
            if r_i == block.header_row:
                continue
            row_header = cells[0] if cells else ""
            for c_i, li in enumerate(row):
                value = _match_handwritten(cells[c_i], pats, c_i, detector)
                value_id, field_label = _field_metadata_above(lines, li)
                fieldvalue = _extract_fieldvalue(cells[c_i])
                # Inspect only the current cell.  Scanning the whole physical line
                # can mistake a preceding cell's fieldvalue for this cell and wrap
                # structural commands such as multicolumn inside hwfield.
                multiline = (_multiline_fieldvalue(lines, li, block.end)
                             if (FIELDVALUE_RE.search(cells[c_i])
                                 and r"\multicolumn" not in cells[c_i]) else None)
                if multiline is not None:
                    fieldvalue = multiline[0]
                    multiline_fields[(li, c_i)] = multiline
                # Lexoid marks every editable value with \fieldvalue. Handwritten
                # patterns remain supported for historical files without that macro.
                if fieldvalue is not None:
                    value = fieldvalue
                if value is None:
                    continue
                candidates.append((li, cells[c_i], value, f"r{r_i:02d}c{c_i:02d}",
                                   row_header, headers[c_i] if c_i < len(headers) else "",
                                   value_id, field_label))
        if not candidates:
            continue

        # ---- phase 2: name the whole table in one shot ------------------- #
        req = TableNameRequest(
            page=page_of[block.begin],
            ordinal=ordinal,
            section=headings[block.begin],
            bold_title=bold_title_above(lines, block.begin),
            caption=caption_near(lines, block.begin),
            headers=headers,
            sample_rows=sample_rows,
            fields=[FieldSpec(key=k, row_header=rh, col_header=ch, value=v, label=label)
                    for (_li, _p, v, k, rh, ch, _vid, label) in candidates],
            structure=[[multicolumn_span(_cell_payload(masked[li]) or "") for li in row]
                       for row in block.rows],
        )
        naming = namer.name_table(req)
        stats["tables"] += 1
        stats[naming.source] = stats.get(naming.source, 0) + 1

        field_ids = {}
        for (li, payload, value, key, row_hdr, col_hdr, value_id, field_label) in candidates:
            semantic = naming.fields.get(key, key)
            # The VALUE_ID currently present in the repaired source is authoritative
            # downstream. Syntax repair may have made it meaningful; semantic_alias
            # remains a separate, consistently generated lookup name.
            fid = value_id or _unique_legacy_id(page_of[li], used_ids)
            field_ids[key] = fid
            semantic_alias = f"{fid}-{semantic}"
            if (li, c_i) in multiline_fields and not HWFIELD_RE.search(lines[li]):
                _, start_column, end_line, end_column = multiline_fields[(li, c_i)]
                lines[end_line] = (lines[end_line][:end_column] + "}"
                                   + lines[end_line][end_column:])
                lines[li] = (lines[li][:start_column] + f"\\hwfield{{{fid}}}{{"
                             + lines[li][start_column:])
            elif (li, c_i) not in multiline_fields:
                lines[li] = _wrap_cell(lines[li], payload, fid)
            records.append(FieldRecord(
                field_id=fid, semantic_alias=semantic_alias, page=page_of[li],
                table=naming.table, semantic=semantic,
                row_header=tex_to_plain(row_hdr),
                col_header=tex_to_plain(field_label or col_hdr),
                value=tex_to_plain(value), tex_line=li + 1,
                fingerprint=req.fingerprint() + ":" + key,
                title_source=(req.bold_title or req.caption or req.section or ""),
                name_source=naming.source,
                label=tex_to_plain(field_label),
                row=int(key[1:key.index("c")]), column=int(key[key.index("c") + 1:]),
                name_status="complete" if naming.source in {"llm", "cache"} else "pending",
            ))
            stats["fields"] += 1
        if naming_requests is not None:
            naming_requests.append({"request": asdict(req), "field_ids": field_ids})

    return "\n".join(lines), records, stats


def _match_handwritten(payload: str, pats: List[re.Pattern], col: int,
                       cfg: DetectorConfig) -> Optional[str]:
    body = payload
    if body.startswith(SYNC_ANCHOR):
        body = body[len(SYNC_ANCHOR):]
    for p in pats:
        m = p.search(body)
        if m:
            return m.groupdict().get("val", m.group(0))
    if col in cfg.force_columns and tex_to_plain(body):
        return body
    return None


def _field_metadata_above(lines: List[str], line_idx: int,
                          window: int = 32) -> tuple[str, str]:
    """Return the nearest Lexoid (VALUE_ID, FIELD_VALUE) pair above a value line.

    Both canonical markers and escaped-underscore variants emitted by older Lexoid
    prompts are accepted. The source text is never rewritten.
    """
    value_id = ""
    label = ""
    for i in range(line_idx - 1, max(-1, line_idx - window - 1), -1):
        line = lines[i]
        if not label:
            m = FIELD_VALUE_RE.search(line)
            if m:
                label = m.group(1).strip()
        m = VALUE_ID_RE.search(line)
        if m:
            value_id = m.group(1).strip()
            break
        stripped = line.strip()
        # A Lexoid marker can precede the first cell while its \fieldvalue lives in
        # a later cell of the same logical row. Walk across preceding `&` cells, but
        # never cross a row boundary or table rule into an unrelated field.
        if (re.search(r"\\\\\*?(?:\[[^\]]*\])?\s*$", stripped)
                or RULE_RE.match(stripped)
                or BEGIN_ALIGN.search(stripped)
                or END_ALIGN.search(stripped)):
            break
    return value_id, label


def _multiline_fieldvalue(lines, start_line, table_end):
    """Locate a complete field argument without changing physical line offsets."""
    call = FIELDVALUE_RE.search(lines[start_line])
    if call is None or _read_balanced(lines[start_line], call.end() - 1, "{", "}"):
        return None
    tail = "\n".join(lines[start_line:table_end])
    argument = _read_balanced(tail, call.end() - 1, "{", "}")
    if argument is None:
        return None
    value, end = argument
    prefix = tail[:end]
    return (value, call.start(), start_line + prefix.count("\n"),
            len(prefix.rsplit("\n", 1)[-1]))


def _extract_fieldvalue(payload: str) -> Optional[str]:
    """Extract a brace-balanced \fieldvalue argument from one formatted cell."""
    m = FIELDVALUE_RE.search(payload)
    if not m:
        return None
    depth = 1
    i = m.end()
    start = i
    while i < len(payload):
        if payload[i] == "\\":
            i += 2
            continue
        if payload[i] == "{":
            depth += 1
        elif payload[i] == "}":
            depth -= 1
            if depth == 0:
                return payload[start:i]
        i += 1
    return payload[start:]


def _wrap_cell(line: str, payload: str, fid: str) -> str:
    if HWFIELD_RE.search(payload):
        return line
    # A parser cell may share a physical line with a following multicolumn cell.
    # Never wrap that structural command as part of the handwritten field.
    if r"\multicolumn" in payload:
        if not FIELDVALUE_RE.search(payload):
            return line
        call = FIELDVALUE_RE.search(payload)
        argument = _read_balanced(payload, call.end() - 1, "{", "}")
        if argument is None:
            return line
        end = argument[1]
        replacement = (payload[:call.start()] + f"\\hwfield{{{fid}}}{{"
                       + payload[call.start():end] + "}" + payload[end:])
        return line.replace(payload, replacement, 1)
    call = FIELDVALUE_RE.search(payload)
    if call:
        argument = _read_balanced(payload, call.end() - 1, "{", "}")
        if argument is not None:
            # Surround only the field, never a partial formula or outer group.
            end = argument[1]
            replacement = (payload[:call.start()] + f"\\hwfield{{{fid}}}{{"
                           + payload[call.start():end] + "}" + payload[end:])
            return line.replace(payload, replacement, 1)
    body = payload
    lead = ""
    if body.startswith(SYNC_ANCHOR):
        lead, body = SYNC_ANCHOR, body[len(SYNC_ANCHOR):]
    # A malformed-but-tolerated source cell can end in one bare backslash. If the
    # wrapper brace is appended immediately, TeX sees ``\}`` and the new hwfield
    # argument becomes unclosed. Terminate that control-space first; this changes
    # no source characters and keeps the wrapper structurally independent.
    trailing_backslashes = len(body) - len(body.rstrip("\\"))
    if trailing_backslashes % 2:
        body += " "
    replacement = f"{lead}\\hwfield{{{fid}}}{{{body}}}"
    return line.replace(payload, replacement, 1)


def _unique_id(page: int, table: str, semantic: str, used: Dict[str, int]) -> str:
    base = f"p{page:03d}-{table}-{semantic}"
    if base not in used:
        used[base] = 1
        return base
    used[base] += 1
    return f"{base}-{used[base]}"


def _unique_legacy_id(page: int, used: Dict[str, int]) -> str:
    """Generate a stable-shape fallback only for historical fields without an ID."""
    prefix = f"LEX-P{page:04d}-V"
    n = sum(1 for key in used if key.startswith(prefix)) + 1
    fid = f"{prefix}{n:04d}"
    used[fid] = 1
    return fid


def bold_title_above(lines: List[str], begin_idx: int, window: int = 12) -> str:
    """
    The bold run that acts as this table's title: scan upwards from \\begin{...},
    stopping at the previous table's \\end or at a sectioning command so we never
    steal a neighbouring table's heading.

    Two shapes are accepted, in order of confidence:
      1. a line that is ONLY a bold run  -> \\textbf{检验记录}
      2. a bold run anywhere in the gap  -> ... 见 \\textbf{附表 3} ...
    """
    loose = ""
    for i in range(begin_idx - 1, max(-1, begin_idx - window - 1), -1):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("%"):
            continue
        if END_ALIGN.search(line) or HEADING_RE.search(line):
            break
        m = BOLD_LINE_RE.match(stripped)
        if m:
            return m.group(1).strip()
        if not loose:
            m2 = BOLD_RE.search(stripped)
            if m2:
                loose = m2.group(1).strip()
    return loose


def caption_near(lines: List[str], begin_idx: int, window: int = 12) -> str:
    text = "\n".join(lines[max(0, begin_idx - window):begin_idx + window * 3])
    m = CAPTION_RE.search(text)
    if not m:
        return ""
    depth, buf = 1, []
    for ch in text[m.end():]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        buf.append(ch)
    return "".join(buf).strip()


def _derive_table_name(lines: List[str], begin_idx: int, ordinal: int,
                       heading: str,
                       hint: Optional[Callable[[int], str]]) -> str:
    def _ok(x: str) -> str:
        return "" if (not x or x == "field" or is_hashy(x)) else x

    if hint:
        h = _ok(slug(hint(ordinal) or "", 30))
        if h:
            return h
    window = "\n".join(lines[max(0, begin_idx - 12):begin_idx + 40])
    m = LABEL_RE.search(window)
    if m:
        lbl = _ok(slug(re.sub(r"^(tab|tbl|table):", "", m.group(1)), 30))
        if lbl:
            return lbl
    m = CAPTION_RE.search(window)
    if m:
        tail = window[m.end():]
        depth, buf = 1, []
        for ch in tail:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            buf.append(ch)
        cap = _ok(slug("".join(buf), 30))
        if cap:
            return cap
    if heading:
        s = _ok(slug(heading, 24))
        if s:
            return f"{s}_t{ordinal}"
    return f"tab{ordinal:02d}"


def write_registry(records: List[FieldRecord], path: Path, *, provenance_path=None, tex=None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = basic_fields(records, tex) if tex is not None else [asdict(r) for r in records]
    if provenance_path is not None:
        from .reconcile import field_segments, _normalized
        incoming = json.loads(Path(provenance_path).read_text("utf-8"))["fields"]
        originals = {item["field_id"]: item for item in incoming}
        if len(originals) != len(incoming):
            raise ValueError("Duplicate field IDs in source registry")
        segments = field_segments(tex or "")
        if set(originals) != set(segments):
            raise ValueError("Optimized TeX and source registry field IDs differ")
        structural = {item["field_id"]: item for item in fields}
        fields = []
        for fid, original in originals.items():
            if _normalized(original["value"]) != _normalized(segments[fid]["value"]):
                raise ValueError(f"Optimized field value changed: {fid}")
            merged = {**original, **structural.get(fid, {})}
            for key in ("value", "label", "paddle_text", "model_guess", "confidence", "needs_review", "history"):
                if key in original:
                    merged[key] = original[key]
            merged["tex_line"] = (tex or "")[:segments[fid]["start"]].count("\n") + 1
            fields.append(merged)
    write_utf8_atomic(path, json.dumps(
        {"version": 1, "count": len(fields), "fields": fields},
        ensure_ascii=False, indent=2))


def basic_fields(records, tex):
    """Recover all marked values, including fields outside tables, from the TEX."""
    from .reconcile import field_segments, _ID

    fields = {r.field_id: asdict(r) for r in records}
    pages = page_map(tex.split("\n"))
    result = []
    markers = list(_ID.finditer(tex))
    for index, marker in enumerate(markers):
        fid = marker[1]
        end = markers[index + 1].start() if index + 1 < len(markers) else len(tex)
        raw = tex[marker.start():end]
        line = tex[:marker.start()].count("\n")
        label = FIELD_VALUE_RE.search(raw)
        entry = dict(fields.pop(fid, {"field_id": fid, "semantic_alias": "", "semantic": "",
            "table": "", "row_header": "", "col_header": "", "row": None, "column": None,
            "name_source": "deferred", "name_status": "pending"}))
        entry.update(label=label[1].strip() if label else entry.get("label", ""),
                     page=pages[line], tex_line=line + 1)
        try:
            segment = field_segments(raw)[fid]
            entry.update(value=segment["value"], raw_tex_value=segment["payload"], extraction_status="complete")
        except (ValueError, KeyError) as exc:
            entry.update(value=None, extraction_status="invalid", extraction_error=str(exc),
                         raw_tex_segment=raw, needs_review=True, name_status="pending")
        result.append(entry)
    result.extend(fields.values())
    return sorted(result, key=lambda f: f["tex_line"])
