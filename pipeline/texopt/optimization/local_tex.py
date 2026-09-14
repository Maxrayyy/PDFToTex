"""Shared, deterministic repairs before vision escalation and final optimization."""

from __future__ import annotations

import re

from pylatexenc.latexwalker import (LatexWalker, LatexEnvironmentNode, LatexMacroNode,
                                   get_default_latex_context_db)
from pylatexenc.macrospec import MacroSpec

from .syntax_check import ENV_RE, _mask_verbatim
from .syntax_repair import (normalize_control_word_boundaries, normalize_math_blank_lines,
                            normalize_multicolumn_linebreaks, normalize_stray_cjk_backslashes,
                            normalize_unmatched_closing_braces,
                            normalize_hline_row_boundaries,
                            normalize_standalone_newlines, normalize_text_mode_carets,
                            normalize_text_mode_math_symbols)
from .tex_tables import (_peel_prefix, _read_balanced, _skip_ws, alignment_colspec, iter_structural,
                         mask_comments, multicolumn_span, parse_colspec, split_align_body)


VERSION = "local-tex-v6-nested-form-rows"
SUPPORT_BEGIN = "% >>> lexoid local support >>>"
SUPPORT_END = "% <<< lexoid local support <<<"


def normalize_ulem_text_scripts(source):
    """Keep LaTeX 2026 script spacing out of ulem's word-scanning groups."""
    underlines = {"sout", "uline", "uuline", "uwave", "xout", "dashuline", "dotuline"}
    scripts = {"textsuperscript", "textsubscript"}
    context = get_default_latex_context_db()
    context.add_context_category("ulem-scripts", macros=[
        MacroSpec(name, "{") for name in underlines | scripts], prepend=True)
    edits = []

    def visit(nodes, underlined=False):
        for node in nodes or []:
            if isinstance(node, LatexMacroNode):
                name = node.macroname
                if name in {"newcommand", "renewcommand", "providecommand", "def",
                            "mbox", "hbox", "makebox", "fbox"}:
                    continue
                if underlined and name in scripts:
                    edits.append((node.pos, node.pos + node.len))
                    continue
                for arg in getattr(node.nodeargd, "argnlist", []) or []:
                    if arg is not None:
                        visit(getattr(arg, "nodelist", []), underlined or name in underlines)
            else:
                visit(getattr(node, "nodelist", []), underlined)

    nodes, _, _ = LatexWalker(_mask_verbatim(mask_comments(source)), latex_context=context).get_latex_nodes()
    visit(nodes)
    for start, end in sorted(edits, reverse=True):
        source = source[:start] + r"\mbox{" + source[start:end] + "}" + source[end:]
    return source, len(edits)


FIGURE_MACRO = r"""\providecommand{\LexoidExperimentalFigure}[2]{%
  \begingroup\setlength{\fboxsep}{0pt}%
  \fbox{\rule{0pt}{\dimexpr#2-2\fboxrule\relax}%
    \hspace*{\dimexpr#1-2\fboxrule\relax}}%
  \endgroup}"""


def normalize_experimental_figure_frames(source):
    """Frame only explicitly marked omitted panels, keeping their given height."""
    visible = _mask_verbatim(source)
    masked = mask_comments(visible)
    blanks = []
    for match in re.finditer(r"\\(vspace\*?|rule)\b\*?", masked):
        position = _skip_ws(masked, match.end())
        if position < len(masked) and masked[position] == "[":
            optional = _read_balanced(masked, position, "[", "]")
            if not optional:
                continue
            position = _skip_ws(masked, optional[1])
        first = _read_balanced(masked, position, "{", "}")
        if not first:
            continue
        height, end = first
        if match[1] == "rule":
            if not re.fullmatch(r"0(?:\.0*)?(?:pt|cm|mm|bp|em|ex)", height.strip()):
                continue
            second = _read_balanced(masked, _skip_ws(masked, end), "{", "}")
            if not second:
                continue
            height, end = second
        if re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:pt|cm|mm|bp|em|ex)", height.strip()):
            blanks.append((match.start(), end, height.strip(), match[1] == "rule"))
    edits = {}
    for marker in re.finditer(r"(?<!\\)%[ \t]*LEXOID_OMITTED_EXPERIMENTAL_FIGURE\b[^\n]*", visible):
        for start, end, height, rule in blanks:
            after = start >= marker.end() and not visible[marker.end():start].strip()
            before = end <= marker.start() and not visible[end:marker.start()].strip() and "\n" not in visible[end:marker.start()]
            if not (after or before):
                continue
            replacement = rf"\LexoidExperimentalFigure{{\linewidth}}{{{height}}}"
            if not rule:
                replacement = r"\par\noindent " + replacement + r"\par "
            edits[start, end] = replacement
            break
    for (start, end), replacement in sorted(edits.items(), reverse=True):
        source = source[:start] + replacement + source[end:]
    return source, len(edits)


def normalize_missing_graphics(source):
    """Guard model-referenced images that are not shipped with the document."""
    pattern = re.compile(
        r"\\includegraphics(?P<options>\[[^\]\n]*\])?\{(?P<path>[^{}\n]+)\}"
    )

    def replace(match):
        options = match.group("options") or ""
        path = match.group("path")
        command = rf"\includegraphics{options}{{{path}}}"
        return rf"\IfFileExists{{{path}}}{{{command}}}{{\LexoidExperimentalFigure{{\linewidth}}{{1.2cm}}}}"

    return pattern.subn(replace, source)


def normalize_panel_rules(source):
    """A hline directly inside a minipage is a visible rule, not a table row."""
    edits = []

    def visit(nodes, environment=""):
        for node in nodes or []:
            if isinstance(node, LatexEnvironmentNode):
                visit(node.nodelist, node.environmentname)
            elif isinstance(node, LatexMacroNode):
                if node.macroname == "hline" and environment == "minipage":
                    edits.append((node.pos, node.pos + len(r"\hline")))
                if node.macroname not in {"newcommand", "renewcommand", "providecommand", "def"}:
                    for arg in getattr(node.nodeargd, "argnlist", []) or []:
                        if arg is not None:
                            visit(getattr(arg, "nodelist", []), environment)
            else:
                visit(getattr(node, "nodelist", []), environment)

    # Mask comments and verbatim text but retain offsets for exact local edits.
    nodes, _, _ = LatexWalker(_mask_verbatim(mask_comments(source))).get_latex_nodes()
    visit(nodes)
    for start, end in reversed(edits):
        source = source[:start] + r"\par\noindent\rule{\linewidth}{0.4pt}\par " + source[end:]
    return source, len(edits)


def normalize_table_row_endings(source):
    """Restore row endings overridden by an ungrouped paragraph alignment command."""
    edits = []

    def direct_alignment(text):
        depth = 0
        for token in re.finditer(r"\\[a-zA-Z@]+|\\[\s\S]|[{}]", mask_comments(text)):
            value = token.group()
            if value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
            elif depth == 0 and value in {r"\centering", r"\raggedleft", r"\raggedright"}:
                return True
        return False

    def scan(segment, base=0):
        tokens = iter(iter_structural(mask_comments(segment)))
        for begin in tokens:
            if begin.kind != "align_begin":
                continue
            end = next((t for t in tokens if t.kind == "align_end"), None)
            if end is None:
                continue
            body = segment[begin.body_start:end.start]
            offset = base + begin.body_start
            for row in split_align_body(body):
                for cell in row.cells:
                    scan(cell.text, offset)
                    if cell.sep.startswith(r"\\") and direct_alignment(cell.text):
                        edits.append((offset + len(cell.text), offset + len(cell.text) + 2))
                    offset += len(cell.text) + len(cell.sep)

    scan(source)
    for start, end in sorted(edits, reverse=True):
        source = source[:start] + r"\tabularnewline" + source[end:]
    return source, len(edits)


def normalize_split_paragraph_rows(source):
    """Rejoin a ruled form row whose ungrouped cell breaks reset the column.

    Only repair an unambiguous column budget: across the entire ruled block the
    cells must occupy exactly one complete row, with field-bearing continuations.
    A one-row multirow label followed by instructions and a nested result panel
    also has an unambiguous column budget. Actual multirows stay untouched.
    """
    edits = []

    def nested_instruction_panel(rows, columns, first_cells):
        if (len(columns) != 3 or len(first_cells) != 2
                or len(rows[-1][0].cells) != 2
                or any(len(row.cells) != 1 for row, _ in rows[1:-1])):
            return False
        label = mask_comments(first_cells[0]).strip()
        if not label.startswith(r"\multirow"):
            return False
        cursor, args = len(r"\multirow"), []
        for _ in range(3):
            got = _read_balanced(label, _skip_ws(label, cursor), "{", "}")
            if got is None:
                return False
            value, cursor = got
            args.append(value.strip())
        if args[:2] != ["1", "="] or label[cursor:].strip():
            return False
        remaining = first_cells[1:] + [cell.text for row, _ in rows[1:] for cell in row.cells]
        if any(r"\multirow" in mask_comments(text) for text in remaining):
            return False
        panel = mask_comments(rows[-1][0].cells[-1].text).strip()
        tokens = [token for token in iter_structural(panel)
                  if token.kind in {"align_begin", "align_end"}]
        return (len(tokens) == 2 and tokens[0].kind == "align_begin"
                and tokens[0].name == "tabular" and tokens[0].start == 0
                and tokens[1].kind == "align_end" and tokens[1].end == len(panel)
                and r"\fieldvalue" in panel)

    def repair_block(rows, columns):
        if len(rows) < 2 or not _peel_prefix(rows[0][0].cells[0].text)[0]:
            return
        first = rows[0][0]
        first_cells = [_peel_prefix(first.cells[0].text)[1],
                       *(cell.text for cell in first.cells[1:])]
        first_span = sum(multicolumn_span(cell) for cell in first_cells)
        if len(first.cells) < 2 or (first_span >= len(columns)
                                  and not any(multicolumn_span(c) > 1 for c in first_cells)):
            return
        nested_panel = nested_instruction_panel(rows, columns, first_cells)
        if not nested_panel and not any(
                len(row.cells) == 1 and r"\fieldvalue" in mask_comments(row.cells[0].text)
                for row, _ in rows[1:]):
            return
        if not nested_panel and any(r"\multirow" in mask_comments(cell.text)
                                    for row, _ in rows for cell in row.cells):
            return
        pending, column = [], 0
        for index, (row, offset) in enumerate(rows):
            for cell_index, cell in enumerate(row.cells):
                text = _peel_prefix(cell.text)[1] if index == cell_index == 0 else cell.text
                span = multicolumn_span(text)
                if column + span > len(columns):
                    return
                if cell.sep == "&":
                    column += span
                elif index < len(rows) - 1:
                    spacing = (re.fullmatch(r"\\\\\s*\[\s*(\d+(?:\.\d+)?(?:pt|bp|mm|cm|em|ex))\s*\]", cell.sep)
                               if nested_panel else None)
                    if ((cell.sep != r"\\" and spacing is None) or span != 1
                            or columns[column] not in {"p", "m", "b", "X"}):
                        return
                    replacement = r"\newline{}"
                    if spacing:
                        replacement += r"\vspace{" + spacing[1] + "}"
                    if nested_panel:
                        replacement += r"\ignorespaces"
                    pending.append((offset + len(cell.text),
                                    offset + len(cell.text) + len(cell.sep), replacement))
                else:
                    column += span
                offset += len(cell.text) + len(cell.sep)
        if column == len(columns):
            edits.extend(pending)

    def scan(segment, base=0):
        tokens = iter(iter_structural(segment))
        for begin in tokens:
            if begin.kind != "align_begin":
                continue
            end = next((token for token in tokens if token.kind == "align_end"), None)
            if end is None:
                continue
            body = segment[begin.body_start:end.start]
            columns = parse_colspec(alignment_colspec(segment[begin.start:begin.body_start], begin.name))
            block, offset = [], base + begin.body_start
            for row in split_align_body(body):
                prefix, first = _peel_prefix(row.cells[0].text)
                meaningful = any(mask_comments(text).strip()
                                 for text in [first, *(cell.text for cell in row.cells[1:])])
                if prefix or not meaningful:
                    repair_block(block, columns)
                    block = []
                if meaningful:
                    block.append((row, offset))
                offset += sum(len(cell.text) + len(cell.sep) for cell in row.cells)
            repair_block(block, columns)
            scan(body, base + begin.body_start)

    scan(_mask_verbatim(source))
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source, len(edits)


def normalize_table_heading_breaks(source):
    """End a standalone colon-terminated heading before its block table."""
    masked = _mask_verbatim(mask_comments(source))
    edits = []
    tokens = iter(iter_structural(masked))
    for begin in tokens:
        if begin.kind != "align_begin":
            continue
        next((token for token in tokens if token.kind == "align_end"), None)
        prefix = masked[:begin.start]
        heading = re.search(
            r"(?m)^[ \t]*(?:\\par[ \t]*)?\\noindent[^\n&]*[:：][ \t}]*\n([ \t]*\\noindent[ \t]*)$",
            prefix)
        if heading:
            edits.append(heading.start(1))
    for position in reversed(edits):
        source = source[:position] + r"\par" + source[position:]
    return source, len(edits)


PACKAGE_USES = {
    "array": r"\\(?:arraybackslash|newcolumntype)\b|\\begin\{array\}",
    "amsmath": r"\\(?:text|overset|underset|dfrac|tfrac)\b|\\begin\{(?:aligned|align\*?|gather\*?)\}",
    "amssymb": r"\\(?:diagup|diagdown|checkmark|square|boxtimes)\b",
    "upgreek": r"\\up(?:alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|nu|xi|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega)\b",
    "graphicx": r"\\(?:includegraphics|resizebox|rotatebox|scalebox)\b",
    "pict2e": r"\\begin\{picture\}",
    "multirow": r"\\multirow\b",
    "booktabs": r"\\(?:toprule|midrule|bottomrule|cmidrule)\b",
    "makecell": r"\\(?:makecell|thead)\b",
    "tabularx": r"\\begin\{tabularx\}",
    "longtable": r"\\begin\{longtable\}",
    "tikz": r"\\begin\{tikzpicture\}|\\tikz\b",
    "ragged2e": r"\\(?:RaggedRight|RaggedLeft|Centering|justifying)\b",
    "enumitem": r"\\setlist\b",
    "xcolor": r"\\(?:textcolor|color|definecolor)\b",
    "ulem": r"\\(?:sout|uline|uuline|uwave|xout|dashuline|dotuline)\b",
}


def inject_support(source):
    """Add only known, missing dependencies to a complete document's preamble."""
    original = source
    prior = re.search(re.escape(SUPPORT_BEGIN) + r"(.*?)" + re.escape(SUPPORT_END), source, re.S)
    prior_support = prior[1] if prior else ""
    source = re.sub(re.escape(SUPPORT_BEGIN) + r".*?" + re.escape(SUPPORT_END) + r"\n?",
                    "", source, flags=re.S)
    masked = _mask_verbatim(mask_comments(source))
    begin = re.search(r"\\begin\{document\}", masked)
    if begin is None:
        return original, 0
    preamble = masked[:begin.start()]
    packages = {part.strip() for m in re.finditer(
        r"\\(?:usepackage|RequirePackage)(?:\[[^]]*\])?\{([^}]+)\}", preamble)
        for part in m[1].split(",")}
    lines = []
    for package, pattern in PACKAGE_USES.items():
        if package not in packages and (re.search(pattern, masked)
                or rf"\@ifpackageloaded{{{package}}}" in prior_support):
            options = "[normalem]" if package == "ulem" else ""
            lines.append(rf"\@ifpackageloaded{{{package}}}{{}}{{\RequirePackage{options}{{{package}}}}}")
    for macro in ("fieldvalue", "handwritten"):
        defined = re.search(r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{?\\" + macro + r"\b|\\def\s*\\" + macro + r"\b", preamble)
        if not defined and (re.search(r"\\" + macro + r"\b", masked[begin.end():])
                or rf"\providecommand{{\{macro}}}" in prior_support):
            lines.append(rf"\providecommand{{\{macro}}}[1]{{#1}}")
    if (r"\LexoidExperimentalFigure" in masked[begin.end():]
            and not re.search(r"\\(?:newcommand|renewcommand|providecommand)\*?\s*\{?\\LexoidExperimentalFigure\b", preamble)):
        lines.append(FIGURE_MACRO)
    if lines:
        block = (SUPPORT_BEGIN + "\n\\makeatletter\n" + "\n".join(lines) +
                 "\n\\makeatother\n" + SUPPORT_END + "\n")
        source = source[:begin.start()] + block + source[begin.start():]
    return source, int(source != original)


def normalize_literal_model_newlines(source):
    """Turn model-emitted ``\\n`` separators into real line breaks.

    Do not touch control words such as ``\\newpage`` or ``\\noindent``: those
    continue with a lowercase letter. Instrument output commonly emits the
    literal separator before an uppercase row label, CJK text, or ``&``.
    """
    pattern = re.compile(r"\\n(?=[A-Z\u3400-\u9fff&]|\s*$)")
    normalized = pattern.sub("\n", source)
    return normalized, int(normalized != source)


def normalize_numeric_text_backslashes(source):
    """Escape doubled backslashes that model output placed before digits.

    In instrument tables, identifiers such as ``A37...\\2605031`` are text.
    TeX interprets the doubled slash as a row break, which creates phantom
    columns/rows. A row break is followed by whitespace or a line ending, so
    restricting this repair to a digit is safe for generated tables.
    """
    normalized = re.sub(r"\\\\(?=[0-9])", r"\\textbackslash{}", source)
    return normalized, int(normalized != source)


def normalize_handwritten_text_backslashes(source):
    """Render doubled backslashes inside literal handwritten fields as text.

    Handwritten values are text evidence. A value such as ``3\\\\#A`` must
    not become a table row break when it appears inside ``\\handwritten{...}``.
    Keep inline ``$...$`` fragments intact; those are the explicit exception
    used for scientific notation and units.
    """
    from .reconcile import escape_handwritten_tex

    edits = []
    cursor = 0
    marker = r"\handwritten"
    while True:
        start = source.find(marker, cursor)
        if start < 0:
            break
        open_at = _skip_ws(source, start + len(marker))
        parsed = _read_balanced(source, open_at, "{", "}")
        if parsed is None:
            cursor = start + len(marker)
            continue
        value, end = parsed
        if r"\\" in value:
            # Model output uses ``\\\\`` for one literal backslash. Fold the
            # pair before escaping TeX specials so it renders once.
            literal = value.replace(r"\\", "\\")
            replacement = r"\handwritten{" + escape_handwritten_tex(literal) + "}"
            edits.append((start, end, replacement))
        cursor = end
    for start, end, replacement in reversed(edits):
        source = source[:start] + replacement + source[end:]
    return source, len(edits)


def normalize_text_hashes(source: str) -> tuple[str, int]:
    """Escape literal hash characters in document text, preserving definitions."""
    lines = []
    changed = 0
    in_support = False
    for line in source.splitlines(keepends=True):
        if re.match(r"%\s*>>>\s*lexoid\b", line):
            in_support = True
        if in_support:
            lines.append(line)
            if re.match(r"%\s*<<<\s*lexoid", line):
                in_support = False
            continue
        visible = line.split("%", 1)[0]
        if re.search(r"\\(?:newcommand|renewcommand|providecommand|def|edef|gdef)\b", visible):
            lines.append(line)
            continue
        edits = []
        for index, char in enumerate(visible):
            if char != "#" or (index and visible[index - 1] == "\\"):
                continue
            edits.append(index)
        for index in reversed(edits):
            line = line[:index] + r"\#" + line[index + 1:]
        changed += len(edits)
        lines.append(line)
    return "".join(lines), changed


def normalize_unclosed_makebox_rows(source):
    """Close truncated underline/makebox wrappers on a single table row.

    Vision output occasionally omits the final braces of a handwritten value
    wrapped in ``\\underline{\\makebox{...}{\\fieldvalue{...}}}``.  Restrict
    recovery to lines that contain that explicit wrapper and a row terminator;
    unrelated braces and page boundaries are left untouched.
    """
    changed = 0
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if r"\underline{" not in line or r"\makebox" not in line:
            continue
        row_break = re.search(r"(?<!\\)\\\\(?=\s*(?:%.*)?$)", line.rstrip("\n"))
        if not row_break:
            continue
        body = line[:row_break.start()]
        depth = 0
        escaped = False
        for char in body:
            if char == "{" and not escaped:
                depth += 1
            elif char == "}" and not escaped and depth:
                depth -= 1
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        if depth:
            lines[index] = line[:row_break.start()] + ("}" * depth) + line[row_break.start():]
            changed += 1
    return "".join(lines), changed


def normalize_unclosed_field_rows(source):
    """Close an incomplete field wrapper before a same-line table break."""
    lines = source.splitlines(keepends=True)
    changed = 0
    for index, line in enumerate(lines):
        if r"\fieldvalue{" not in line:
            continue
        row_break = re.search(r"(?<!\\)\\\\(?=\s*(?:%.*)?$)", line.rstrip("\n"))
        if not row_break:
            continue
        body = line[:row_break.start()]
        depth = 0
        escaped = False
        for char in body:
            if char == "{" and not escaped:
                depth += 1
            elif char == "}" and not escaped and depth:
                depth -= 1
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        if depth:
            lines[index] = line[:row_break.start()] + ("}" * depth) + line[row_break.start():]
            changed += 1
    return "".join(lines), changed

def normalize_unclosed_tabular_specs(source):
    """Close a truncated repeated-column specification on its begin line."""
    lines = source.splitlines(keepends=True)
    changed = 0
    for index, line in enumerate(lines):
        if r"\begin{tabular}" not in line or "*{" not in line:
            continue
        body = line.rstrip("\n")
        depth = 0
        escaped = False
        for char in body:
            if char == "{" and not escaped:
                depth += 1
            elif char == "}" and not escaped and depth:
                depth -= 1
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        if depth == 1:
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = body + "}" + newline
            changed += 1
    return "".join(lines), changed


def normalize_uniform_table_overflow(source):
    """Expand a table spec only when every populated row has one extra cell."""
    edits = []
    masked = mask_comments(source)
    for begin in iter_structural(masked):
        if begin.kind != "align_begin" or begin.name != "tabular":
            continue
        end = next((token for token in iter_structural(masked[begin.end:])
                    if token.kind == "align_end"), None)
        if end is None:
            continue
        end_start = begin.end + end.start
        head = source[begin.start:begin.body_start]
        spec = alignment_colspec(head, "tabular")
        expected = len(parse_colspec(spec))
        if not expected:
            continue
        rows = [row for row in split_align_body(source[begin.body_start:end_start])
                if any(cell.text.strip() for cell in row.cells)]
        spans = [sum(multicolumn_span(cell.text) for cell in row.cells) for row in rows]
        overflow = sum(span == expected + 1 for span in spans)
        longest_overflow_run = 0
        current_overflow_run = 0
        for span in spans:
            if span == expected + 1:
                current_overflow_run += 1
                longest_overflow_run = max(longest_overflow_run, current_overflow_run)
            else:
                current_overflow_run = 0
        # A table may omit one trailing column from its specification while
        # keeping that column in a consecutive header/data run.  Require a
        # repeated run for narrow forms and reject any larger overflow; mixed
        # narrow forms remain untouched for visual fallback.
        if (expected < 7 and (expected < 2 or len(spans) < 2
                             or longest_overflow_run < 2)
                or any(span > expected + 1 for span in spans)):
            continue
        spec_pos = head.rfind("{" + spec + "}")
        if spec_pos < 0:
            continue
        start = begin.start + spec_pos + 1
        edits.append((start + len(spec), "l"))
    for position, suffix in reversed(edits):
        source = source[:position] + suffix + source[position:]
    return source, len(edits)


def normalize_handwritten_raw_superscripts(source):
    """Keep OCR superscripts in plain handwritten text out of math mode."""
    pattern = re.compile(
        r"(?P<prefix>\\handwritten\{[^${}\n]*)(?:\^|\\textasciicircum\{\})"
        r"(?:\{(?P<braced>[^{}\n]+)\}|(?P<bare>[A-Za-z0-9]))"
    )
    return pattern.subn(
        lambda match: (match.group("prefix") + r"\textsuperscript{" +
                       (match.group("braced") or match.group("bare")) + "}"),
        source,
    )


def normalize_field_metadata_comments(source: str) -> tuple[str, int]:
    """Restore metadata markers escaped during table-cell assembly."""
    pattern = re.compile(
        r"(?m)^(?P<indent>\s*)\\%(?P<space>\s+)"
        r"(?P<marker>#(?:VALUE|FIELD)(?:\\)?_[A-Z]+:)"
    )
    return pattern.subn(
        lambda match: f"{match.group('indent')}% {match.group('marker')}",
        source,
    )


def normalize_inline_field_metadata_comments(source: str) -> tuple[str, int]:
    """Put field metadata markers on a physical comment line before the field."""
    pattern = re.compile(r"(?m)(?P<prefix>[^\n])\s*%\s*#VALUE(?:\\)?_ID:")
    return pattern.subn(lambda match: match.group("prefix") + "\n% #VALUE_ID:", source)


def normalize_page_boundary_closures(source):
    """Close unambiguous groups/environments before a page completion marker."""
    marker = re.compile(r"\n(?=%\s*LEXOID_PAGE_COMPLETED:\s*\d+/\d+)")
    edits = []
    start = 0
    for match in marker.finditer(source):
        segment = source[start:match.start()]
        masked = _mask_verbatim(mask_comments(segment))
        depth = 0
        for ch in masked:
            if ch == "{": depth += 1
            elif ch == "}" and depth: depth -= 1
        envs = [m.group(2) for m in ENV_RE.finditer(masked)
                if m.group(1) == "begin"]
        for m in ENV_RE.finditer(masked):
            if m.group(1) == "end" and m.group(2) in envs:
                envs.remove(m.group(2))
        closers = "}" * depth + "".join(rf"\end{{{env}}}" for env in reversed(envs)
                                         if env in {"tabular", "tabularx", "table"})
        if closers:
            edits.append((match.start(), closers))
        start = match.end()
    for position, text in reversed(edits):
        source = source[:position] + text + source[position:]
    return source, len(edits)


def normalize_tex(source):
    changes = {}
    for name, operation in (
        ("literal_model_newlines", normalize_literal_model_newlines),
        ("numeric_text_backslashes", normalize_numeric_text_backslashes),
        ("text_hashes", normalize_text_hashes),
        ("handwritten_text_backslashes", normalize_handwritten_text_backslashes),
        ("unclosed_makebox_rows", normalize_unclosed_makebox_rows),
        ("unclosed_field_rows", normalize_unclosed_field_rows),
        ("unclosed_tabular_specs", normalize_unclosed_tabular_specs),
        ("control_word_boundaries", normalize_control_word_boundaries),
        ("text_math_symbols", normalize_text_mode_math_symbols),
        ("text_mode_carets", normalize_text_mode_carets),
        ("handwritten_raw_superscripts", normalize_handwritten_raw_superscripts),
        ("field_metadata_comments", normalize_field_metadata_comments),
        ("inline_field_metadata_comments", normalize_inline_field_metadata_comments),
        ("stray_cjk_backslashes", normalize_stray_cjk_backslashes),
        ("unmatched_closing_braces", normalize_unmatched_closing_braces),
        ("hline_row_boundaries", normalize_hline_row_boundaries),
        ("standalone_newlines", normalize_standalone_newlines),
        ("multicolumn_linebreaks", normalize_multicolumn_linebreaks),
        ("math_blank_lines", normalize_math_blank_lines),
        ("ulem_text_scripts", normalize_ulem_text_scripts),
        ("experimental_figure_frames", normalize_experimental_figure_frames),
        ("missing_graphics", normalize_missing_graphics),
        ("panel_rules", normalize_panel_rules),
        ("split_paragraph_rows", normalize_split_paragraph_rows),
        ("table_row_endings", normalize_table_row_endings),
        ("table_heading_breaks", normalize_table_heading_breaks),
        ("uniform_table_overflow", normalize_uniform_table_overflow),
        ("missing_support", inject_support),
    ):
        source, count = operation(source)
        if count:
            changes[name] = count
    return source, changes
