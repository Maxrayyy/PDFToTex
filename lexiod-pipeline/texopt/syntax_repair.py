"""LLM-assisted LaTeX syntax repair with strict content invariants."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from .model_telemetry import request_json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .llm import ANTHROPIC_API_URL, _is_openai_model, openai_api_url
from .model_config import resolve_model
from .textio import write_utf8_atomic
from .syntax_check import _mask_verbatim
from .tex_tables import CS_RE, _read_balanced, _skip_ws, iter_structural, mask_comments


PAGE_COMPLETED = re.compile(
    r"(?m)^[ \t]*%\s*LEXOID_PAGE_COMPLETED:\s*(\d+)\s*/\s*(\d+)[ \t]*$"
)
END_DOCUMENT_LINE = re.compile(
    r"(?m)^[ \t]*\\end\{document\}[ \t]*(?:\r?\n)?"
)
PROTECTED_METADATA_LINE = re.compile(
    r"(?m)^.*(?:#HANDWRITTEN:|#TODO\s*#HANDWRITTEN:|"
    r"LEXOID_PAGE_COMPLETED:).*$"
)
VALUE_ID_MARKER = re.compile(r"(?m)#VALUE(?:\\)?_ID:\s*(\S+)")
FIELD_VALUE_MARKER = re.compile(r"(?m)#FIELD(?:\\)?_VALUE:\s*(.*)$")
MUTABLE_NAME_LINE = re.compile(
    r"(?m)^.*(?:#VALUE(?:\\)?_ID:|#FIELD(?:\\)?_VALUE:).*(?:\r?\n|$)"
)
FIELD_MACRO_LINE = re.compile(r"\\(?:fieldvalue|hwfield)\s*\{([^\r\n]*)")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CONTROL_WORD_BEFORE_CJK = re.compile(
    r"\\(quad|qquad|enspace|enskip|hfill|vfill|dotfill|hrulefill)"
    r"(?=[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff])"
)
TEXT_MODE_MATH_SYMBOL = re.compile(
    r"(?<!\\ensuremath\{)\\(?P<name>diagup|diagdown)\b"
)

SYSTEM_PROMPT = """\
You are a conservative XeLaTeX syntax repair engine. The input is a numbered LaTeX
fragment. Return ONLY a JSON object, without Markdown fences or explanations:
{"edits":[{"start_line":12,"end_line":14,"replacement":"complete replacement text"}]}

Line numbers are 1-based and local to this batch. Each edit replaces an inclusive,
whole-line range. Edits must not overlap. Return {"edits":[]} when no repair is needed.
Do not return or repeat the complete document. Include only the smallest line ranges
that must change; all unmentioned input bytes are preserved automatically.

Repair syntax and alignment defects that prevent XeLaTeX or table-width probes from
running: unbalanced braces, mismatched environments, illegal/misplaced &, wrong row
terminators, and incorrect multicolumn grouping. A tabular/tabularx row must match its
declared physical column count after multicolumn spans are considered. You may add
structural grouping or a nested plain tabular when a row genuinely needs more logical
cells than its parent table declares.

Mandatory table audit -- perform this for EVERY tabular, tabularx, array, longtable,
and nested table before returning:
1. Derive the declared physical column count from the expanded column specification.
2. For every row, count top-level `&` separators and add all `\\multicolumn{n}{...}{...}`
   spans. A span greater than the declared column count is invalid and must be fixed.
   A row may legally omit trailing empty cells, so a smaller span is not by itself an
   error and must not be changed solely to make the counts equal.
3. Treat `&` and `\\\\` inside braces/nested tables separately from parent-row separators.
   A visual line break inside an X/p/m/b cell must not accidentally terminate the row;
   use a safe paragraph break such as `\\newline` or structural grouping when needed.
4. Trace errors reported at `\\end{tabularx}` back through ALL preceding rows: tabularx
   captures its body, so the actual bad row is commonly earlier than the reported line.
5. Pay special attention to OCR/Lexoid forms whose parent table declares four columns
   but a row contains three label/value pairs (six logical cells), or whose signature/date
   row contains five cells. Do not delete a label, value, metadata line, or `&` merely to
   make the count pass. Preserve all logical fields and represent the exceptional row
   structurally, for example with a correctly spanned nested transparent tabular, or a
   semantically equivalent row grouping consistent with the surrounding layout.
6. Verify braces around `\\fieldvalue`, `\\handwritten`, and `\\multicolumn` content,
   then recount every affected row after the repair. Do not return the input unchanged
   when any row span is inconsistent.

Absolute invariants:
- Preserve the semantic meaning, association, and order of visible text and values.
  You MAY add LaTeX escaping required to represent the same value safely, such as
  changing a literal percent sign in a field value from `%` to `\\%`.
- VALUE_ID and FIELD_VALUE are the only metadata you MAY improve. Keep exactly one of
  each existing marker in the same field order and association. VALUE_ID values must
  be unique, non-whitespace identifiers; prefer retaining the LEX-P####-V#### locator
  and adding a concise meaningful semantic suffix. FIELD_VALUE may become a clearer
  field name. Do not move either marker to another value.
- Preserve every HANDWRITTEN, TODO, fieldvalue/hwfield value, and
  LEXOID_PAGE_COMPLETED marker. Field-value bytes may change only for syntax-safe
  escaping that leaves the rendered value unchanged.
- Preserve every \\checkboxfield{ID}{checked|unchecked}{label}. Never replace an
  editable checkbox with a raw \\square, \\Box, \\boxtimes, or \\checkmark symbol.
- Do not add/delete/reorder fields or renumber pages.
- Do not convert tabularx or choose column widths; a later deterministic stage does it.
- Do not add a preamble to a page fragment.
- Only the final physical page may contain \\end{document}; non-final pages must not.
"""


@dataclass
class SyntaxRepairStats:
    pages: int = 0
    batches: int = 0
    llm: int = 0
    cache: int = 0
    unchanged: int = 0
    rejected: int = 0
    failed: int = 0
    deterministic_end_documents_removed: int = 0


def normalize_multicolumn_linebreaks(source: str) -> tuple[str, int]:
    """Keep visual line breaks inside paragraph-style multicolumn cells local."""
    replacements: list[tuple[int, int]] = []
    i = 0
    while i < len(source):
        if source[i] == "%":
            newline = source.find("\n", i)
            i = len(source) if newline < 0 else newline + 1
            continue
        if source[i] != "\\":
            i += 1
            continue
        command = CS_RE.match(source, i)
        if command is None or command.group(0) != r"\multicolumn":
            i = command.end() if command else i + 1
            continue
        first = _read_balanced(source, _skip_ws(source, command.end()), "{", "}")
        second = (_read_balanced(source, _skip_ws(source, first[1]), "{", "}")
                  if first else None)
        third_open = _skip_ws(source, second[1]) if second else len(source)
        third = (_read_balanced(source, third_open, "{", "}")
                 if third_open < len(source) else None)
        if not first or not second or not third:
            i = command.end()
            continue
        if re.search(r"(?:^|[^\\])[pmb]\s*\{", second[0]):
            tokens = list(iter_structural(third[0], inside_alignment=True))
            nested: list[tuple[int, int]] = []
            nested_start = None
            for token in tokens:
                if token.kind == "align_begin" and nested_start is None:
                    nested_start = token.start
                elif token.kind == "align_end" and nested_start is not None:
                    nested.append((nested_start, token.end))
                    nested_start = None
            for token in tokens:
                inside_nested = any(
                    start <= token.start < end for start, end in nested
                )
                if (token.kind == "rowbreak" and token.depth == 0
                        and not inside_nested):
                    replacements.append(
                        (third_open + 1 + token.start, third_open + 1 + token.end))
        i = third[1]
    for start, end in reversed(replacements):
        source = source[:start] + r"\newline{}" + source[end:]
    return source, len(replacements)


def normalize_control_word_boundaries(source: str) -> tuple[str, int]:
    """Delimit known zero-argument control words glued to visible text.

    XeTeX treats CJK letters as part of a control word, so ``\\quad至`` is read as
    one undefined command rather than ``\\quad`` followed by visible text.  Empty
    grouping terminates the command without changing rendered content.  Restrict
    ASCII newline suffixes to uppercase text, retaining primitives like newlinechar.
    Explicitly declared macros, comments and verbatim text are preserved.
    """
    masked = _mask_verbatim(mask_comments(source))
    letters = r"a-zA-Z@\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    declared = set(re.findall(
        r"\\(?:newcommand|renewcommand|providecommand|DeclareRobustCommand)\*?\s*\{?\s*(\\["
        + letters + r"]+)|\\(?:def|gdef|edef|xdef|let)\s*(\\[" + letters + r"]+)", masked))
    declared_names = {name for pair in declared for name in pair if name}
    edits = []
    for token in re.finditer(r"\\[" + letters + r"]+|\\[\s\S]", masked):
        command = token.group()
        if command in declared_names:
            continue
        spacing = CONTROL_WORD_BEFORE_CJK.match(command)
        if spacing:
            edits.append(token.start() + spacing.end())
        elif command.startswith(r"\newline"):
            suffix = command[len(r"\newline"):]
            if suffix and ("A" <= suffix[0] <= "Z" or CJK.match(suffix)):
                edits.append(token.start() + len(r"\newline"))
    for position in reversed(edits):
        source = source[:position] + "{}" + source[position:]
    return source, len(edits)


def normalize_math_blank_lines(source: str) -> tuple[str, int]:
    """Comment out paragraph breaks inside paired math delimiters, retaining lines."""
    masked = _mask_verbatim(mask_comments(source))
    closing = {"$": "$", "$$": "$$", r"\(": r"\)", r"\[": r"\]"}
    expected = None
    start = 0
    spans = []
    # Consume all control sequences so escaped dollars cannot open math mode.
    for token in re.finditer(r"\\[a-zA-Z@]+|\\[\s\S]|\$\$?", masked):
        value = token.group()
        if expected is None and value in closing:
            start, expected = token.end(), closing[value]
        elif expected is not None and value == expected:
            spans.append((start, token.start()))
            expected = None
    edits = []
    span_index = 0
    for blank in re.finditer(r"(?m)^[ \t]*(?=\r?\n)", source):
        while span_index < len(spans) and spans[span_index][1] <= blank.start():
            span_index += 1
        if (span_index < len(spans)
                and spans[span_index][0] <= blank.start() < spans[span_index][1]):
            edits.append(blank.end())
    for position in reversed(edits):
        source = source[:position] + "%" + source[position:]
    return source, len(edits)


def normalize_text_mode_math_symbols(source: str) -> tuple[str, int]:
    """Make standalone diagonal cancellation marks valid in text-mode fields."""
    changed = 0
    out: list[str] = []
    for line in source.splitlines(keepends=True):
        comment_at = len(line)
        for index, char in enumerate(line):
            if char != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                comment_at = index
                break

        def replace(match: re.Match[str]) -> str:
            nonlocal changed
            changed += 1
            return rf"\ensuremath{{\{match.group('name')}}}"

        out.append(TEXT_MODE_MATH_SYMBOL.sub(replace, line[:comment_at])
                   + line[comment_at:])
    return "".join(out), changed


class LLMSyntaxRepairer:
    """Repair consecutive Lexoid-page batches and cache accepted responses."""

    def __init__(self, cache_path: Path, model: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: int = 120,
                 max_retries: int = 2, batch_pages: int = 5,
                 event: Optional[Callable[..., None]] = None) -> None:
        self.cache_path = Path(cache_path)
        self.model = resolve_model("TEXOPT_REPAIR_MODEL", model)
        self.is_openai = _is_openai_model(self.model)
        self.api_key = api_key or os.environ.get(
            "OPENAI_API_KEY" if self.is_openai else "ANTHROPIC_API_KEY", ""
        )
        self.timeout = timeout
        self.max_retries = min(1, max(0, max_retries))
        self.batch_pages = max(1, batch_pages)
        self.event = event
        self.last_response_meta: dict[str, object] = {}
        self.cache: dict[str, str] = {}
        if self.cache_path.exists():
            try:
                loaded = json.loads(self.cache_path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    self.cache = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, json.JSONDecodeError):
                self.cache = {}

    def repair_document(
        self, source: str, *, target_pages: Optional[set[int]] = None,
        diagnostic_hint: str = "",
        diagnostic_hints: Optional[dict[int, str]] = None,
        bypass_cache: bool = False,
    ) -> tuple[str, SyntaxRepairStats]:
        source, removed = remove_intermediate_end_documents(source)
        pages = split_lexoid_pages(source)
        if target_pages is None:
            chunks = group_lexoid_pages(pages, self.batch_pages)
        else:
            chunks = [
                (page, page, total, chunk)
                for page, total, chunk in pages if page in target_pages
            ]
        stats = SyntaxRepairStats(
            pages=len(pages), batches=len(chunks),
            deterministic_end_documents_removed=removed
        )
        repaired_chunks: dict[int, str] = {}
        for batch_number, (page_start, page_end, total, chunk) in enumerate(chunks, 1):
            violations: list[str] = []
            chunk_hint = diagnostic_hint
            if diagnostic_hints:
                chunk_hint = "\n".join(
                    diagnostic_hints[page]
                    for page in range(page_start, page_end + 1)
                    if page in diagnostic_hints
                )
            key = self._fingerprint(
                page_start, page_end, total, chunk, chunk_hint
            )
            if not bypass_cache and key in self.cache:
                candidate = self.cache[key]
                stats.cache += 1
                source_kind = "cache"
            elif not self.api_key:
                candidate = chunk
                stats.failed += 1
                source_kind = "no_api_key"
            else:
                candidate = self._call(
                    page_start, page_end, total, chunk, chunk_hint
                )
                if candidate is None:
                    candidate = chunk
                    stats.failed += 1
                    source_kind = "failed"
                else:
                    # Model output is authoritative by user policy. Keep the former
                    # invariants as audit-only signals; never discard a valid patch.
                    violations = repair_invariant_violations(chunk, candidate)
                    source_kind = "llm"
                    self.cache[key] = candidate
                    # Persist every valid model patch immediately. A container stop in
                    # a later batch must not repeat already completed LLM work.
                    self._flush()
                    stats.llm += 1
            if candidate == chunk:
                stats.unchanged += 1
            repaired_chunks[page_start] = candidate
            if self.event:
                self.event(
                    "SYNTAX_REPAIR_BATCH", "syntax-repair batch processed",
                    batch=batch_number, total_batches=len(chunks),
                    page_start=page_start, page_end=page_end,
                    pages_in_batch=page_end - page_start + 1,
                    total_pages=total, source=source_kind,
                    changed=candidate != chunk,
                    rejection_reasons=[], audit_flags=violations,
                )
        if target_pages is None:
            return "".join(repaired_chunks[start] for start, *_ in chunks), stats
        return "".join(
            repaired_chunks.get(page, chunk) for page, _, chunk in pages
        ), stats

    def _fingerprint(self, page_start: int, page_end: int, total: int,
                     chunk: str, diagnostic_hint: str = "") -> str:
        # Keep ordinary first-pass keys compatible with the existing v2 cache.
        # Diagnostic retries use a separate namespace so a previously incomplete
        # answer cannot suppress the targeted repair.
        if diagnostic_hint:
            payload = (f"syntax-repair-v3\0{self.model}\0"
                       f"{page_start}-{page_end}/{total}\0"
                       f"{diagnostic_hint}\0{chunk}")
        else:
            payload = (f"syntax-repair-v2\0{self.model}\0"
                       f"{page_start}-{page_end}/{total}\0{chunk}")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _call(self, page_start: int, page_end: int, total: int,
              chunk: str, diagnostic_hint: str = "") -> Optional[str]:
        numbered = "".join(
            f"{line_no:06d}: {line}"
            for line_no, line in enumerate(chunk.splitlines(keepends=True), 1)
        )
        if numbered and not numbered.endswith("\n"):
            numbered += "\n"
        prompt = (
            f"Consecutive physical-page batch: {page_start}-{page_end}/{total}. "
            f"Contains final page: {'yes' if page_end == total else 'no'}. "
            "Use the surrounding pages to keep environments, table structure, and "
            "formatting consistent across page boundaries.\n"
            + (f"Validator errors that remain after an earlier repair pass:\n"
               f"{diagnostic_hint}\n" if diagnostic_hint else "")
            + "\n"
            f"<LATEX_LINES>\n{numbered}</LATEX_LINES>"
        )
        for attempt in range(self.max_retries + 1):
            try:
                self._call_scope = {"attempt": attempt + 1, "page_start": page_start,
                                    "page_end": page_end}
                if self.is_openai:
                    result = self._call_openai(prompt)
                else:
                    result = self._call_anthropic(prompt)
                if self.event:
                    self.event(
                        "SYNTAX_REPAIR_RESPONSE",
                        "syntax-repair model response received",
                        page_start=page_start, page_end=page_end,
                        response_chars=len(result), **self.last_response_meta,
                    )
                return apply_line_edits(chunk, result)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError,
                    json.JSONDecodeError) as exc:
                if self.event:
                    status = getattr(exc, "code", None)
                    detail = str(exc)
                    if isinstance(exc, urllib.error.HTTPError):
                        try:
                            body = exc.read().decode("utf-8", errors="replace")
                            parsed = json.loads(body)
                            detail = str(parsed.get("error", {}).get("message") or body)
                        except Exception:
                            pass
                    self.event(
                        "SYNTAX_REPAIR_REQUEST_FAILED",
                        "syntax-repair model request failed",
                        level="WARNING", page_start=page_start, page_end=page_end,
                        attempt=attempt + 1, max_attempts=self.max_retries + 1,
                        model=self.model, error_type=type(exc).__name__,
                        http_status=status, detail=detail[:1200],
                    )
                if attempt == self.max_retries:
                    return None
                time.sleep(1.5 * (attempt + 1))
        return None

    def _call_openai(self, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "max_completion_tokens": 30000,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            openai_api_url(), data=body, method="POST",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {self.api_key}"},
        )
        data = request_json(request, self.timeout, stage="syntax_repair", model=self.model,
                            **getattr(self, "_call_scope", {}))
        choice = data["choices"][0]
        self.last_response_meta = {
            "finish_reason": choice.get("finish_reason"),
            "completion_tokens": data.get("usage", {}).get("completion_tokens"),
        }
        return choice["message"]["content"]

    def _call_anthropic(self, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "max_tokens": 30000,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = urllib.request.Request(
            ANTHROPIC_API_URL, data=body, method="POST",
            headers={"content-type": "application/json",
                     "x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01"},
        )
        data = request_json(request, self.timeout, stage="syntax_repair", model=self.model,
                            **getattr(self, "_call_scope", {}))
        self.last_response_meta = {
            "finish_reason": data.get("stop_reason"),
            "completion_tokens": data.get("usage", {}).get("output_tokens"),
        }
        return "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )

    def _flush(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_utf8_atomic(
            self.cache_path,
            json.dumps(self.cache, ensure_ascii=False, indent=2, sort_keys=True),
        )


def remove_intermediate_end_documents(source: str) -> tuple[str, int]:
    matches = list(END_DOCUMENT_LINE.finditer(source))
    if len(matches) <= 1:
        return source, 0
    keep_start = matches[-1].start()
    out = []
    cursor = 0
    removed = 0
    for match in matches:
        out.append(source[cursor:match.start()])
        if match.start() == keep_start:
            out.append(match.group(0))
        else:
            removed += 1
        cursor = match.end()
    out.append(source[cursor:])
    return "".join(out), removed


def canonicalize_document_terminator(source: str) -> tuple[str, int]:
    """Remove generated terminators and restore one canonical final terminator.

    Lexoid can emit ``\\end{document}`` at a temporary stopping point.  Merely
    keeping the last occurrence is insufficient when the only occurrence is in the
    middle of a subsequently resumed document.  For a complete document, move the
    terminator after all generated pages; for an input fragment, remove it entirely.
    """
    matches = list(END_DOCUMENT_LINE.finditer(source))
    without = END_DOCUMENT_LINE.sub("", source)
    final_was_already_last = bool(
        matches and not source[matches[-1].end():].strip()
    )
    removed = len(matches) - (1 if final_was_already_last else 0)
    if not re.search(r"\\begin\s*\{document\}", without):
        return without, len(matches)

    # A standalone document must retain exactly one final terminator to compile on
    # Overleaf.  It is deliberately regenerated rather than trusting a temporary
    # Lexoid terminator found before later page checkpoints.
    body = without.rstrip()
    newline = "\r\n" if "\r\n" in source else "\n"
    canonical = body + newline + r"\end{document}" + newline
    return canonical, removed


def split_lexoid_pages(source: str) -> list[tuple[int, int, str]]:
    matches = list(PAGE_COMPLETED.finditer(source))
    if not matches:
        return [(1, 1, source)]
    chunks = []
    start = 0
    for match in matches:
        end = source.find("\n", match.end())
        end = len(source) if end < 0 else end + 1
        chunks.append((int(match.group(1)), int(match.group(2)), source[start:end]))
        start = end
    if start < len(source):
        page, total, chunk = chunks[-1]
        chunks[-1] = (page, total, chunk + source[start:])
    return chunks


def group_lexoid_pages(
    pages: list[tuple[int, int, str]], batch_pages: int
) -> list[tuple[int, int, int, str]]:
    """Join consecutive physical pages into bounded, lossless LLM batches."""
    size = max(1, batch_pages)
    batches: list[tuple[int, int, int, str]] = []
    for offset in range(0, len(pages), size):
        group = pages[offset:offset + size]
        batches.append((
            group[0][0], group[-1][0], group[-1][1],
            "".join(chunk for _, _, chunk in group),
        ))
    return batches


def page_numbers_for_lines(source: str, lines: list[int]) -> set[int]:
    """Map whole-document validator line numbers to physical Lexoid pages."""
    wanted = set(lines)
    matched: set[int] = set()
    first_line = 1
    for page, _, chunk in split_lexoid_pages(source):
        line_count = chunk.count("\n") + (0 if chunk.endswith("\n") else 1)
        last_line = first_line + max(line_count - 1, 0)
        if any(first_line <= line <= last_line for line in wanted):
            matched.add(page)
        first_line = last_line + 1
    return matched


def page_diagnostic_hints(source: str, issues: list[object]) -> dict[int, str]:
    """Render whole-document validator issues with page-local line numbers."""
    hints: dict[int, list[str]] = {}
    first_line = 1
    for page, _, chunk in split_lexoid_pages(source):
        line_count = chunk.count("\n") + (0 if chunk.endswith("\n") else 1)
        last_line = first_line + max(line_count - 1, 0)
        for issue in issues:
            line = int(getattr(issue, "line"))
            if first_line <= line <= last_line:
                local_line = line - first_line + 1
                hints.setdefault(page, []).append(
                    f"document line {line}, local line {local_line}: "
                    f"{getattr(issue, 'code')}: {getattr(issue, 'message')}"
                )
        first_line = last_line + 1
    return {page: "\n".join(lines) for page, lines in hints.items()}


def repair_invariants_hold(before: str, after: str) -> bool:
    return not repair_invariant_violations(before, after)


def _field_payloads(source: str) -> list[str]:
    """Protect field contents while allowing repairs to trailing table syntax."""
    return [match.group(1).rstrip("} \\t\\r\\n&\\\\")
            for match in FIELD_MACRO_LINE.finditer(source)]


def repair_invariant_violations(before: str, after: str) -> list[str]:
    violations = []
    if PROTECTED_METADATA_LINE.findall(before) != PROTECTED_METADATA_LINE.findall(after):
        violations.append("protected_metadata_changed")
    if _field_payloads(before) != _field_payloads(after):
        violations.append("field_payloads_changed")
    before_ids = VALUE_ID_MARKER.findall(before)
    after_ids = VALUE_ID_MARKER.findall(after)
    if len(before_ids) != len(after_ids):
        violations.append("value_id_count_changed")
    if len(after_ids) != len(set(after_ids)):
        violations.append("duplicate_value_ids")
    if len(FIELD_VALUE_MARKER.findall(before)) != len(FIELD_VALUE_MARKER.findall(after)):
        violations.append("field_name_count_changed")
    # VALUE_ID/FIELD_VALUE metadata is intentionally mutable; exclude those comment
    # lines when protecting visible CJK document text.
    visible_before = MUTABLE_NAME_LINE.sub("", before)
    visible_after = MUTABLE_NAME_LINE.sub("", after)
    if CJK.findall(visible_before) != CJK.findall(visible_after):
        violations.append("visible_cjk_text_changed")
    before_markers = [(int(a), int(b)) for a, b in PAGE_COMPLETED.findall(before)]
    after_markers = [(int(a), int(b)) for a, b in PAGE_COMPLETED.findall(after)]
    if before_markers != after_markers:
        violations.append("page_markers_changed")
    return violations


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:latex|tex)?\s*\n?", "", stripped, flags=re.I)
    stripped = re.sub(r"\n?```$", "", stripped)
    return stripped + ("\n" if text.endswith("\n") and not stripped.endswith("\n") else "")


def apply_line_edits(source: str, response: str) -> str:
    """Apply model-produced whole-line edits while preserving every other byte."""
    payload = json.loads(strip_code_fence(response))
    if not isinstance(payload, dict) or not isinstance(payload.get("edits"), list):
        raise ValueError("syntax repair response must contain an edits array")
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    for raw in payload["edits"]:
        if not isinstance(raw, dict):
            raise ValueError("syntax repair edit must be an object")
        start, end, replacement = (
            raw.get("start_line"), raw.get("end_line"), raw.get("replacement")
        )
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or not isinstance(replacement, str)
                or start < 1 or end < start or end > len(lines)):
            raise ValueError("syntax repair edit has an invalid line range")
        if lines[end - 1].endswith(("\n", "\r")) and replacement and not replacement.endswith("\n"):
            replacement += "\n"
        edits.append((start - 1, end, replacement))
    ordered = sorted(edits)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError("syntax repair edits overlap")
    out = source
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for start, end, replacement in reversed(ordered):
        out = out[:offsets[start]] + replacement + out[offsets[end]:]
    return out
