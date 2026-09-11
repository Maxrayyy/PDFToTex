"""Shared, deterministic repairs before vision escalation and final optimization."""

from __future__ import annotations

import re

from pylatexenc.latexwalker import (LatexWalker, LatexEnvironmentNode, LatexMacroNode,
                                   get_default_latex_context_db)
from pylatexenc.macrospec import MacroSpec

from .syntax_check import _mask_verbatim
from .syntax_repair import (normalize_control_word_boundaries, normalize_math_blank_lines,
                            normalize_multicolumn_linebreaks, normalize_text_mode_math_symbols)
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


def normalize_tex(source):
    changes = {}
    for name, operation in (
        ("control_word_boundaries", normalize_control_word_boundaries),
        ("text_math_symbols", normalize_text_mode_math_symbols),
        ("multicolumn_linebreaks", normalize_multicolumn_linebreaks),
        ("math_blank_lines", normalize_math_blank_lines),
        ("ulem_text_scripts", normalize_ulem_text_scripts),
        ("experimental_figure_frames", normalize_experimental_figure_frames),
        ("panel_rules", normalize_panel_rules),
        ("split_paragraph_rows", normalize_split_paragraph_rows),
        ("table_row_endings", normalize_table_row_endings),
        ("table_heading_breaks", normalize_table_heading_breaks),
        ("missing_support", inject_support),
    ):
        source, count = operation(source)
        if count:
            changes[name] = count
    return source, changes
