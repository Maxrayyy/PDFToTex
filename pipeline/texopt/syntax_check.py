"""Conservative structural checks for optimizer input and output LaTeX."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List

from .opaque import find_opaque
from .tex_tables import (CS_RE, ENV_ARGS, VERBATIM_ENVS, _peel_prefix,
                         _read_balanced, _read_env_name, _skip_ws,
                         iter_structural, mask_comments, multicolumn_span,
                         parse_colspec, split_align_body)


@dataclass(frozen=True)
class SyntaxIssue:
    severity: str                 # error | warning
    code: str
    line: int
    message: str

    def payload(self) -> dict:
        return asdict(self)


ENV_RE = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")
ILLEGAL_TABULAR_WIDTH = re.compile(
    r"\\begin\s*\{tabular\}\s*\{\s*\\(?:textwidth|linewidth|columnwidth)\s*\}"
    r"\s*\{",
    re.S,
)
COLUMN_CHECK_ENVS = set(ENV_ARGS) - {"NiceTabular"}


def validate_latex(tex: str, require_sync_safe: bool = False) -> List[SyntaxIssue]:
    issues: List[SyntaxIssue] = []
    masked = _mask_verbatim(mask_comments(tex))
    issues.extend(_brace_issues(masked))
    issues.extend(_environment_issues(masked))
    issues.extend(_table_alignment_issues(tex, masked))

    for match in ILLEGAL_TABULAR_WIDTH.finditer(masked):
        issues.append(SyntaxIssue(
            "error", "ILLEGAL_TABULAR_WIDTH", _line(tex, match.start()),
            r"plain tabular accepts one column-spec argument; remove the width argument",
        ))

    for table in find_opaque(tex):
        issues.append(SyntaxIssue(
            "error" if require_sync_safe else "warning",
            "OPAQUE_TABLE", table.line,
            f"{table.env} captures/replays its body and prevents cell-level SyncTeX",
        ))

    for i, ch in enumerate(tex):
        if ord(ch) < 32 and ch not in "\n\r\t":
            issues.append(SyntaxIssue(
                "error", "CONTROL_CHARACTER", _line(tex, i),
                f"unexpected U+{ord(ch):04X} control character",
            ))
            break
    return issues


def _alignment_colspec(head: str, env: str) -> str:
    begin = re.search(r"\\begin\s*", head)
    if not begin:
        return ""
    named = _read_env_name(head, begin.end())
    if not named:
        return ""
    _, cursor = named
    mandatory: list[str] = []
    for kind in ENV_ARGS.get(env, ""):
        cursor = _skip_ws(head, cursor)
        if kind == "o":
            if cursor < len(head) and head[cursor] == "[":
                got = _read_balanced(head, cursor, "[", "]")
                if got:
                    cursor = got[1]
        elif cursor < len(head) and head[cursor] == "{":
            got = _read_balanced(head, cursor, "{", "}")
            if got:
                mandatory.append(got[0])
                cursor = got[1]
    return mandatory[-1] if mandatory else ""


def _table_alignment_issues(tex: str, masked: str) -> List[SyntaxIssue]:
    """Validate top-level row spans before a costly width-probe compile."""
    issues: List[SyntaxIssue] = []

    def scan(segment: str, masked_segment: str, base_offset: int) -> None:
        tokens = list(iter_structural(masked_segment))
        index = 0
        while index < len(tokens):
            begin = tokens[index]
            if begin.kind != "align_begin":
                index += 1
                continue
            end_index = index + 1
            while end_index < len(tokens) and tokens[end_index].kind != "align_end":
                end_index += 1
            if end_index >= len(tokens):
                break
            end = tokens[end_index]
            body = segment[begin.body_start:end.start]
            masked_body = masked_segment[begin.body_start:end.start]

            if begin.name in COLUMN_CHECK_ENVS:
                spec = _alignment_colspec(
                    segment[begin.start:begin.body_start], begin.name
                )
                expected = len(parse_colspec(spec))
                if expected:
                    cursor = 0
                    for row in split_align_body(body):
                        row_length = sum(len(cell.text) + len(cell.sep)
                                         for cell in row.cells)
                        cells = list(row.cells)
                        content_offset = 0
                        if cells:
                            original_first = cells[0].text
                            _prefix, first = _peel_prefix(original_first)
                            content_offset = len(original_first) - len(first)
                            cells[0] = type(cells[0])(first, cells[0].sep)
                        meaningful = [cell for cell in cells if cell.text.strip()]
                        if meaningful:
                            actual = sum(multicolumn_span(cell.text) for cell in cells)
                            if actual != expected:
                                offset = (base_offset + begin.body_start + cursor
                                          + content_offset)
                                while offset < len(tex) and tex[offset] in " \t\r\n":
                                    offset += 1
                                underfull = actual < expected
                                issues.append(SyntaxIssue(
                                    "warning" if underfull else "error",
                                    ("TABLE_ALIGNMENT_UNDERFULL" if underfull
                                     else "TABLE_ALIGNMENT_MISMATCH"),
                                    _line(tex, offset),
                                    f"{begin.name} row spans {actual} columns; "
                                    f"expected {expected} from its column specification",
                                ))
                        cursor += row_length

            scan(body, masked_body, base_offset + begin.body_start)
            index = end_index + 1

    scan(tex, masked, 0)
    return issues


def _brace_issues(tex: str) -> List[SyntaxIssue]:
    stack: List[int] = []
    issues: List[SyntaxIssue] = []
    i = 0
    while i < len(tex):
        if tex[i] == "\\":
            match = CS_RE.match(tex, i)
            i = match.end() if match else i + 2
            continue
        if tex[i] == "{":
            stack.append(i)
        elif tex[i] == "}":
            if not stack:
                issues.append(SyntaxIssue(
                    "error", "UNMATCHED_CLOSE_BRACE", _line(tex, i),
                    "closing brace has no matching opening brace",
                ))
            else:
                stack.pop()
        i += 1
    for pos in stack[:20]:
        issues.append(SyntaxIssue(
            "error", "UNCLOSED_BRACE", _line(tex, pos),
            "opening brace is not closed",
        ))
    return issues


def _environment_issues(tex: str) -> List[SyntaxIssue]:
    stack: List[tuple[str, int]] = []
    issues: List[SyntaxIssue] = []
    for match in ENV_RE.finditer(tex):
        kind, env = match.group(1), match.group(2).strip()
        line = _line(tex, match.start())
        if kind == "begin":
            stack.append((env, line))
            continue
        if stack and stack[-1][0] == env:
            stack.pop()
        elif env == "document" and not stack:
            issues.append(SyntaxIssue(
                "warning", "DOCUMENT_FRAGMENT_END", line,
                r"\end{document} appears in a fragment without its opening command",
            ))
        else:
            expected = stack[-1][0] if stack else "none"
            issues.append(SyntaxIssue(
                "error", "ENVIRONMENT_MISMATCH", line,
                f"encountered \\end{{{env}}}; expected \\end{{{expected}}}",
            ))
    for env, line in stack:
        issues.append(SyntaxIssue(
            "warning" if env == "document" else "error",
            "DOCUMENT_FRAGMENT_BEGIN" if env == "document" else "UNCLOSED_ENVIRONMENT",
            line, f"\\begin{{{env}}} is not closed in this input fragment",
        ))
    return issues


def _mask_verbatim(tex: str) -> str:
    out = list(tex)
    for env in VERBATIM_ENVS:
        pattern = re.compile(
            rf"\\begin\{{{re.escape(env)}\}}.*?\\end\{{{re.escape(env)}\}}", re.S
        )
        for match in pattern.finditer(tex):
            for i in range(match.start(), match.end()):
                if out[i] not in "\n\r":
                    out[i] = " "
    for match in re.finditer(r"\\verb\*?(.)", tex, re.S):
        delimiter = match.group(1)
        if delimiter.isspace():
            continue
        end = tex.find(delimiter, match.end())
        if end < 0:
            continue
        for i in range(match.start(), end + 1):
            if out[i] not in "\n\r":
                out[i] = " "
    return "".join(out)


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
