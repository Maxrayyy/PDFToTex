"""
TeX-aware scanner and tabular re-formatter.

Goal: make SyncTeX able to resolve *individual table cells*.

Why this is needed (fact, not opinion):
  TeX builds each alignment cell as an (un)set box inside \\halign. SyncTeX emits the
  box record with the line number at which the box was *completed* -- i.e. at the `&`
  or `\\\\`. If a whole table row lives on ONE source line, no macro on earth can make
  SyncTeX distinguish its cells, because there is only one line number available.

Therefore the fix has two parts:
  1. STRUCTURAL: put every cell on its own source line.  <-- this is what actually works
  2. ANCHOR: inject a zero-size sync anchor (\\SA) at the start of each cell so the
     box record is opened on the cell's own line rather than inherited.

Rendering-neutrality argument (verify with verify/visual_diff.py, do not trust it blindly):
  * LaTeX's column templates wrap cell content as `\\ignorespaces <content> \\unskip`
    (kernel + array.sty `\\insert@column`). The newline we introduce after `&` becomes a
    space token at the *start* of the next cell, which `\\ignorespaces` removes.
  * We never introduce whitespace before `\\\\`.
  * \\SA is `\\leavevmode\\hbox{}` -> zero width/height/depth, no glue, no mode change.

Everything here is pure string->string; no LaTeX is executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

#: environments whose bodies use \halign semantics (`&` = cell sep, `\\` = row sep)
ALIGN_ENVS = {
    "tabular", "tabular*", "tabularx", "tabulary", "longtable", "longtabu",
    "array", "matrix", "pmatrix", "bmatrix", "vmatrix", "Vmatrix", "Bmatrix",
    "smallmatrix", "supertabular", "xltabular", "NiceTabular",
}

#: environments whose content must never be touched
VERBATIM_ENVS = {
    "verbatim", "verbatim*", "lstlisting", "minted", "Verbatim", "alltt",
    "comment", "filecontents", "filecontents*",
}

#: argument signature after \begin{env}:  'o' = optional [..], 'm' = mandatory {..}
ENV_ARGS = {
    "tabular": "om", "tabularx": "mom", "tabulary": "mom", "tabular*": "mom",
    "longtable": "om", "xltabular": "mom", "array": "om", "longtabu": "om",
    "supertabular": "m", "NiceTabular": "omo",
}

#: macros that legitimately live *between* rows and should keep their own line
INTERROW_MACROS = (
    r"\hline", r"\toprule", r"\midrule", r"\bottomrule", r"\endhead",
    r"\endfirsthead", r"\endfoot", r"\endlastfoot", r"\cline", r"\cmidrule",
    r"\noalign", r"\rowcolor", r"\hhline", r"\addlinespace", r"\specialrule",
    r"\rowfont", r"\arrayrulecolor", r"\CodeBefore", r"\Body",
)

CS_RE = re.compile(r"\\(?:[a-zA-Z]+\*?|\n|.)", re.S)

SYNC_ANCHOR = r"\SA{}"


# --------------------------------------------------------------------------- #
# low-level scanning
# --------------------------------------------------------------------------- #

@dataclass
class Tok:
    kind: str          # 'amp' | 'rowbreak' | 'align_begin' | 'align_end'
    start: int         # index of the token itself
    end: int           # index just past the token (incl. \\ options / \begin args)
    depth: int         # brace depth at the token
    name: str = ""     # environment name for align_begin / align_end
    body_start: int = -1  # for align_begin: first index of the alignment body


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _read_balanced(text: str, i: int, open_c: str, close_c: str) -> Optional[Tuple[str, int]]:
    """`i` must point at `open_c`. Returns (inner_text, index_past_close)."""
    if i >= len(text) or text[i] != open_c:
        return None
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            m = CS_RE.match(text, j)
            j = m.end() if m else j + 2
            continue
        if c == "%":
            nl = text.find("\n", j)
            j = len(text) if nl < 0 else nl + 1
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return None


def _read_env_name(text: str, i: int) -> Optional[Tuple[str, int]]:
    """`i` is just past `\\begin` / `\\end`."""
    i = _skip_ws(text, i)
    got = _read_balanced(text, i, "{", "}")
    if got is None:
        return None
    return got[0].strip(), got[1]


def _skip_env_args(text: str, i: int, env: str) -> int:
    for spec in ENV_ARGS.get(env, ""):
        j = _skip_ws(text, i)
        if spec == "o" and j < len(text) and text[j] == "[":
            got = _read_balanced(text, j, "[", "]")
            if got:
                i = got[1]
        elif spec == "m" and j < len(text) and text[j] == "{":
            got = _read_balanced(text, j, "{", "}")
            if got:
                i = got[1]
    return i


def _skip_rowbreak_options(text: str, i: int) -> int:
    """`i` is just past `\\\\`; consume `*` and `[len]`."""
    if i < len(text) and text[i] == "*":
        i += 1
    j = _skip_ws(text, i)
    if j < len(text) and text[j] == "[":
        got = _read_balanced(text, j, "[", "]")
        if got:
            return got[1]
    return i


def mask_comments(text: str) -> str:
    """
    Same-length copy with comment bodies blanked out (newlines kept, so offsets and
    line numbers stay valid). Any structural scan that works line-by-line MUST run on
    this, otherwise a commented-out \\begin{tabular} is treated as a real one.
    """
    buf = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            m = CS_RE.match(text, i)
            i = m.end() if m else i + 2
            continue
        if c == "%":
            j = text.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                buf[k] = " "
            i = j
            continue
        i += 1
    return "".join(buf)


def iter_structural(text: str, *, inside_alignment: bool = False) -> Iterator[Tok]:
    """
    Yield alignment-relevant tokens, correctly skipping comments, \\verb, verbatim
    environments and escaped characters. Only *outermost* alignment environments are
    reported (nested ones are handled by recursion in the transformer).
    """
    i, depth, align_depth, local_env_depth = 0, 0, 0, 0
    n = len(text)
    while i < n:
        c = text[i]

        if c == "%":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue

        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue

        if c == "&":
            if local_env_depth == 0:
                yield Tok("amp", i, i + 1, depth)
            i += 1
            continue

        if c != "\\":
            i += 1
            continue

        m = CS_RE.match(text, i)
        if not m:
            i += 1
            continue
        cs, j = m.group(0), m.end()

        if cs == "\\\\":
            j = _skip_rowbreak_options(text, j)
            if local_env_depth == 0:
                yield Tok("rowbreak", i, j, depth)
            i = j
            continue

        if cs == r"\tabularnewline":
            j = _skip_rowbreak_options(text, j)
            if local_env_depth == 0:
                yield Tok("rowbreak", i, j, depth)
            i = j
            continue

        if cs in (r"\verb", r"\verb*"):
            if j < n:
                delim, k = text[j], j + 1
                end = text.find(delim, k)
                i = n if end < 0 else end + 1
            else:
                i = j
            continue

        if cs == r"\begin":
            got = _read_env_name(text, j)
            if got is None:
                i = j
                continue
            env, after = got
            if env in VERBATIM_ENVS:
                close = text.find(r"\end{%s}" % env, after)
                i = n if close < 0 else close + len(r"\end{%s}" % env)
                continue
            if env in ALIGN_ENVS:
                body = _skip_env_args(text, after, env)
                if align_depth == 0 and local_env_depth == 0:
                    yield Tok("align_begin", i, body, depth, env, body)
                align_depth += 1
                i = body
                continue
            if inside_alignment:
                local_env_depth += 1
            i = after
            continue

        if cs == r"\end":
            got = _read_env_name(text, j)
            if got is None:
                i = j
                continue
            env, after = got
            if env in ALIGN_ENVS:
                align_depth = max(0, align_depth - 1)
                if align_depth == 0 and local_env_depth == 0:
                    yield Tok("align_end", i, after, depth, env)
            elif inside_alignment and local_env_depth:
                local_env_depth -= 1
            i = after
            continue

        i = j


# --------------------------------------------------------------------------- #
# splitting an alignment body into rows / cells
# --------------------------------------------------------------------------- #

@dataclass
class Cell:
    text: str
    sep: str          # the literal separator that followed ('&', '\\\\[2pt]', or '')


@dataclass
class Row:
    prefix: str       # \hline / \noalign{...} etc. found before the first cell
    cells: List[Cell]


def split_align_body(body: str) -> List[Row]:
    """Split at depth-0 `&` / `\\\\` that are not inside a nested alignment env."""
    rows: List[Row] = []
    cur: List[Cell] = []
    last = 0

    toks = list(iter_structural(body, inside_alignment=True))
    # Precompute spans of nested alignment environments; tokens inside them are theirs.
    nested: List[Tuple[int, int]] = []
    open_at: Optional[int] = None
    for t in toks:
        if t.kind == "align_begin" and open_at is None:
            open_at = t.start
        elif t.kind == "align_end" and open_at is not None:
            nested.append((open_at, t.end))
            open_at = None

    def _inside_nested(pos: int) -> bool:
        return any(a <= pos < b for a, b in nested)

    for tok in toks:
        if tok.kind in ("align_begin", "align_end"):
            continue
        if _inside_nested(tok.start):
            continue
        if tok.depth != 0:
            continue
        if tok.kind == "amp":
            cur.append(Cell(body[last:tok.start], "&"))
            last = tok.end
        elif tok.kind == "rowbreak":
            cur.append(Cell(body[last:tok.start], body[tok.start:tok.end]))
            rows.append(Row("", cur))
            cur = []
            last = tok.end

    tail = body[last:]
    if tail.strip() or cur:
        cur.append(Cell(tail, ""))
        rows.append(Row("", cur))
    return rows


_PREFIX_RE = re.compile(
    r"^(?:\s|%[^\n]*\n)*(?:" + "|".join(re.escape(m) for m in INTERROW_MACROS) + r")\b"
)


def _peel_prefix(cell_text: str) -> Tuple[str, str]:
    """Move leading \\hline-family macros out of the first cell onto their own line."""
    prefix_parts: List[str] = []
    s = cell_text
    while True:
        m = _PREFIX_RE.match(s)
        if not m:
            break
        i = m.end()
        # consume the macro's own arguments, if any
        while i < len(s):
            j = _skip_ws(s, i)
            if j < len(s) and s[j] in "[{":
                got = _read_balanced(s, j, "[", "]") if s[j] == "[" else _read_balanced(s, j, "{", "}")
                if got is None:
                    break
                i = got[1]
                continue
            break
        prefix_parts.append(s[:i].strip())
        s = s[i:]
    return ("\n".join(p for p in prefix_parts if p), s)


# --------------------------------------------------------------------------- #
# transformation
# --------------------------------------------------------------------------- #

@dataclass
class TableStat:
    env: str
    rows: int
    cells: int


#: macros that LaTeX requires to be the *first* token of a cell -> never prefix them
MUST_BE_FIRST = (r"\multicolumn", r"\omit", r"\rowcolor", r"\hhline", r"\noalign")

#: column types whose cell body must stay machine-parseable (siunitx, dcolumn)
OPAQUE_COLUMN_TYPES = {"S", "d", "D", "N", "R"}


def parse_colspec(spec: str) -> List[str]:
    """Return the primary type letter of each column, in order. Best-effort."""
    cols: List[str] = []
    i, n = 0, len(spec)
    while i < n:
        c = spec[i]
        if c in "|() \t\r\n":
            i += 1
        elif c in "@!><":
            got = _read_balanced(spec, _skip_ws(spec, i + 1), "{", "}")
            i = got[1] if got else i + 1
        elif c == "*":
            got_n = _read_balanced(spec, _skip_ws(spec, i + 1), "{", "}")
            if not got_n:
                i += 1
                continue
            got_b = _read_balanced(spec, _skip_ws(spec, got_n[1]), "{", "}")
            if not got_b:
                i = got_n[1]
                continue
            try:
                reps = int(got_n[0].strip())
            except ValueError:
                reps = 1
            cols.extend(parse_colspec(got_b[0]) * reps)
            i = got_b[1]
        elif c == "\\":
            m = CS_RE.match(spec, i)
            i = m.end() if m else i + 1
        else:
            cols.append(c)
            i += 1
            j = _skip_ws(spec, i)
            while j < n and spec[j] in "[{":
                got = (_read_balanced(spec, j, "[", "]") if spec[j] == "["
                       else _read_balanced(spec, j, "{", "}"))
                if not got:
                    break
                j = got[1]
                i = j
                j = _skip_ws(spec, i)
    return cols


def alignment_colspec(head: str, env: str) -> str:
    """Read the last mandatory environment argument, which owns the colspec."""
    command = re.search(r"\\begin\s*\{" + re.escape(env) + r"\}", head)
    if not command:
        return ""
    i = command.end()
    result = ""
    for argument in ENV_ARGS.get(env, ""):
        i = _skip_ws(head, i)
        if argument == "o":
            if i < len(head) and head[i] == "[":
                optional = _read_balanced(head, i, "[", "]")
                if optional:
                    i = optional[1]
        elif i < len(head) and head[i] == "{":
            mandatory = _read_balanced(head, i, "{", "}")
            if mandatory:
                result = mandatory[0]
                i = mandatory[1]
    return result


def multicolumn_span(cell_text: str) -> int:
    """Return the span of a leading \\multicolumn, else 1."""
    s = cell_text.lstrip()
    if not s.startswith(r"\multicolumn"):
        return 1
    got = _read_balanced(s, _skip_ws(s, len(r"\multicolumn")), "{", "}")
    if not got:
        return 1
    try:
        return max(1, int(got[0].strip()))
    except ValueError:
        return 1


_FRACTIONAL_LINEWIDTH = re.compile(r"(?:0?\.\d+)\s*\\linewidth\Z")


def normalize_multirow_width(cell_text: str, column_type: str) -> str:
    """Let multirow inherit a paragraph column instead of shrinking it twice."""
    if column_type not in {"p", "m", "b"}:
        return cell_text

    masked = mask_comments(cell_text)
    replacements: List[Tuple[int, int]] = []
    for match in re.finditer(r"\\multirow\b", masked):
        i = _skip_ws(masked, match.end())
        if i < len(masked) and masked[i] == "[":
            optional = _read_balanced(masked, i, "[", "]")
            if not optional:
                continue
            i = _skip_ws(masked, optional[1])
        rows = _read_balanced(masked, i, "{", "}")
        if not rows:
            continue
        i = _skip_ws(masked, rows[1])
        if i < len(masked) and masked[i] == "[":
            optional = _read_balanced(masked, i, "[", "]")
            if not optional:
                continue
            i = _skip_ws(masked, optional[1])
        width_start = i
        width = _read_balanced(masked, width_start, "{", "}")
        if width and _FRACTIONAL_LINEWIDTH.fullmatch(width[0].strip()):
            replacements.append((width_start + 1, width[1] - 1))

    for start, end in reversed(replacements):
        cell_text = cell_text[:start] + "=" + cell_text[end:]
    return cell_text


def anchor_cell(stripped: str, allow: bool = True) -> str:
    """
    Insert the sync anchor at the earliest position that is *both* safe and precise.
      * \\multicolumn -> inject inside its 3rd argument
      * \\omit / \\rowcolor / ... -> no anchor (would be a LaTeX error)
      * otherwise -> plain prefix
    """
    if not allow or not stripped or stripped.startswith(SYNC_ANCHOR):
        return stripped

    if stripped.startswith(r"\multicolumn"):
        i = _skip_ws(stripped, len(r"\multicolumn"))
        g1 = _read_balanced(stripped, i, "{", "}")
        if not g1:
            return stripped
        g2 = _read_balanced(stripped, _skip_ws(stripped, g1[1]), "{", "}")
        if not g2:
            return stripped
        j = _skip_ws(stripped, g2[1])
        if j < len(stripped) and stripped[j] == "{":
            return stripped[:j + 1] + SYNC_ANCHOR + stripped[j + 1:]
        return stripped

    for macro in MUST_BE_FIRST:
        if stripped.startswith(macro):
            return stripped

    return SYNC_ANCHOR + stripped


def _last_line_opens_comment(text: str) -> bool:
    """Whether appending a token would place it inside a TeX comment."""
    line = text.rsplit("\n", 1)[-1]
    for i, char in enumerate(line):
        if char != "%":
            continue
        backslashes = 0
        j = i - 1
        while j >= 0 and line[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 0:
            return True
    return False


def _format_body(body: str, indent: str, stats: List[TableStat], env: str,
                 anchor: bool, colspec: str = "") -> str:
    rows = split_align_body(body)
    if not rows:
        return body

    col_types = parse_colspec(colspec)
    out: List[str] = [""]
    n_cells = 0
    for row in rows:
        if row.cells:
            prefix, first = _peel_prefix(row.cells[0].text)
            row.cells[0] = Cell(first, row.cells[0].sep)
            if prefix:
                for line in prefix.split("\n"):
                    out.append(f"{indent}{line}")

        col = 0
        for c_i, cell in enumerate(row.cells):
            inner = transform_tex(cell.text, anchor=anchor, stats=stats)
            ctype = col_types[col] if col < len(col_types) else ""
            inner = normalize_multirow_width(inner, ctype)
            stripped = inner.strip()
            if stripped == "" and cell.sep == "" and c_i == len(row.cells) - 1:
                continue
            n_cells += 1

            allow = anchor and bool(stripped) and ctype not in OPAQUE_COLUMN_TYPES
            content = anchor_cell(stripped, allow)
            col += multicolumn_span(stripped)

            sep = cell.sep
            if sep:
                # Keep alignment separators out of a trailing TeX comment. Joining
                # an originally separate ``&`` or ``\\`` to ``...%`` silently
                # removes that separator and merges cells or rows.
                if _last_line_opens_comment(content):
                    out.append(f"{indent}{content}")
                    out.append(f"{indent}{sep}")
                elif sep == "&":
                    out.append(f"{indent}{content} &")
                else:
                    out.append(f"{indent}{content}{sep}")
            else:
                out.append(f"{indent}{content}")

    stats.append(TableStat(env, len(rows), n_cells))
    out.append("")
    return "\n".join(out)


def transform_tex(text: str, anchor: bool = True,
                  stats: Optional[List[TableStat]] = None,
                  indent_unit: str = "  ") -> str:
    """
    Reformat every alignment environment in `text` so that each cell occupies its own
    source line, recursively. Content outside alignment environments is untouched
    byte-for-byte.
    """
    if stats is None:
        stats = []
    pieces: List[str] = []
    last = 0
    depth_indent = indent_unit

    toks = list(iter_structural(text))
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.kind != "align_begin":
            i += 1
            continue
        # find matching outermost align_end
        j = i + 1
        while j < len(toks) and toks[j].kind != "align_end":
            j += 1
        if j >= len(toks):
            break
        end_tok = toks[j]

        pieces.append(text[last:tok.body_start])
        body = text[tok.body_start:end_tok.start]
        head = text[tok.start:tok.body_start]
        spec = alignment_colspec(head, tok.name)
        pieces.append(_format_body(body, depth_indent, stats, tok.name, anchor, spec))
        last = end_tok.start
        i = j + 1

    pieces.append(text[last:])
    return "".join(pieces)
