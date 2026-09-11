"""Expose numbered form headings to TeX editors without changing their typesetting."""

import re

from pylatexenc.latexwalker import (LatexWalker, LatexEnvironmentNode, LatexMacroNode,
                                   LatexGroupNode, LatexCharsNode)

from .syntax_check import _mask_verbatim
from .tex_tables import mask_comments


LEVELS = ("section", "subsection", "subsubsection", "paragraph", "subparagraph")
NUMBER_ONLY = r"[1-9][0-9]?(?:\.[1-9][0-9]?){0,4}"
NUMBER = re.compile(r"^(" + NUMBER_ONLY + r")(?:\s+|[.\u3001\uff0e]\s*)([^\d\W]|[\u3400-\u9fff])", re.UNICODE)
STYLED = re.compile(r"\{\s*(?:\\(?:large|Large|LARGE|huge|Huge|bfseries)\s*)+(?P<title>[^{}]+)\}\Z")
PREFIX = re.compile(r"(?:\s|[{}]|\\(?:par|medskip|smallskip|bigskip|noindent|centering|raggedright|raggedleft|small|normalsize|large|Large|LARGE|huge|Huge)\b)*\Z")
SUPPORT = r"\providecommand{\LexoidOutlineBold}[2]{\textbf{#2}}"
ORIGINAL_SUPPORT = r"\providecommand{\LexoidOutlineOriginal}[2]{\LexoidOutlineContent}"


def normalize_outline(source):
    masked = _mask_verbatim(mask_comments(source))
    edits, headings, plain_candidates = [], [], []

    def title_text(title):
        return re.sub(r"\\(?:quad|qquad)\b\s*", " ", title).strip()

    def original_heading(start, end, title, match):
        level = LEVELS[match[1].count(".")]
        headings.append({"number": match[1], "title": title, "level": level,
                         "line": source.count("\n", 0, start) + 1})
        replacement = (r"{\long\def\LexoidOutlineContent{" + source[start:end] +
                       "}\\let\\" + level + "\\LexoidOutlineOriginal\n\\" +
                       level + "*{" + title + "}}")
        edits.append((start, end, replacement))

    def standalone(position):
        prefix = masked[masked.rfind("\n", 0, position) + 1:position]
        return bool(PREFIX.fullmatch(prefix))

    def visit(nodes, allowed=False):
        for node in nodes or []:
            raw = source[node.pos:node.pos + node.len]
            if isinstance(node, LatexEnvironmentNode):
                # Tables, lists and figure panels contain numbered values too.
                if node.environmentname in {"document", "center", "flushleft", "flushright"}:
                    visit(node.nodelist, node.environmentname == "document" or allowed)
            elif isinstance(node, LatexMacroNode):
                if not allowed or node.macroname not in {"textbf", *LEVELS}:
                    continue
                args = getattr(node.nodeargd, "argnlist", []) or []
                arg = args[-1] if args else None
                if arg is None or not hasattr(arg, "nodelist"):
                    continue
                title = source[arg.pos + 1:arg.pos + arg.len - 1]
                plain = title_text(title)
                if node.macroname == "textbf" and re.fullmatch(NUMBER_ONLY, plain) and standalone(node.pos):
                    line_end = source.find("\n", node.pos + node.len)
                    if line_end == -1:
                        line_end = len(source)
                    tail = source[node.pos + node.len:line_end]
                    combined = plain + tail
                    match = NUMBER.match(combined)
                    if match and not re.search(r"[\\{}%&]", tail):
                        original_heading(node.pos, line_end, combined.strip(), match)
                        continue
                match = NUMBER.match(plain)
                if not match:
                    continue
                previous_line = source[:source.rfind("\n", 0, node.pos)].rsplit("\n", 1)[-1]
                if previous_line.endswith("% lexoid-outline"):
                    continue
                if not standalone(node.pos):
                    continue
                level = LEVELS[match[1].count(".")]
                if node.macroname in LEVELS and (not args[0] or args[0].latex_verbatim() != "*"):
                    continue
                if node.macroname == "textbf" and re.search(
                        r"#VALUE_ID:|\\(?:fieldvalue|handwritten|hwfield)\b", title):
                    boundary = re.search(r"[\uff08(\n%]|\\(?:fieldvalue|handwritten|hwfield|underline|makebox)\b", title)
                    prefix_title = title_text(title[:boundary.start()]) if boundary else ""
                    prefix_match = NUMBER.match(prefix_title)
                    if prefix_match:
                        original_heading(arg.pos + 1, arg.pos + 1 + boundary.start(),
                                         prefix_title, prefix_match)
                    continue
                if node.macroname == "textbf" and plain != title:
                    original_heading(node.pos, node.pos + node.len, plain, match)
                    continue
                headings.append({"number": match[1], "title": title,
                                 "level": level, "line": source.count("\n", 0, node.pos) + 1})
                if node.macroname == level:
                    continue
                # Local aliases retain the old font/spacing while editors see standard commands.
                if node.macroname == "textbf":
                    replacement = ("{\\let\\" + level + "\\LexoidOutlineBold\n" +
                                   "\\" + level + "*{" + title + "}}")
                else:
                    # Section commands set paragraph state; an extra TeX group would lose it.
                    replacement = ("\\let\\LexoidSavedHeading\\" + level +
                                   "\\let\\" + level + "\\" + node.macroname + "% lexoid-outline\n" +
                                   "\\" + level + "*{" + title + "}\\let\\" +
                                   level + "\\LexoidSavedHeading")
                edits.append((node.pos, node.pos + node.len, replacement))
            elif isinstance(node, LatexGroupNode) and raw.startswith(("{\\let\\", r"{\long\def\LexoidOutlineContent")):
                continue
            elif isinstance(node, LatexGroupNode) and allowed and standalone(node.pos) and STYLED.fullmatch(raw):
                title = title_text(STYLED.fullmatch(raw)["title"])
                match = NUMBER.match(title)
                if match:
                    original_heading(node.pos, node.pos + node.len, title, match)
                else:
                    visit(node.nodelist, allowed)
            elif isinstance(node, LatexCharsNode) and allowed:
                offset = node.pos
                for line in raw.splitlines(keepends=True):
                    title = line.strip()
                    start = offset + len(line) - len(line.lstrip())
                    match = NUMBER.match(title)
                    end = start + len(title)
                    line_end = source.find("\n", end)
                    rest = source[end:line_end if line_end != -1 else len(source)]
                    if (match and standalone(start) and len(title) <= 60 and not rest.strip()
                            and not re.search(r"[;:,!?\u3002\uff1a\uff1b\uff0c\uff01\uff1f]", title)
                            and not title.endswith(".")):
                        plain_candidates.append((start, end, title, match))
                    offset += len(line)
            else:
                visit(getattr(node, "nodelist", []), allowed)

    nodes, _, _ = LatexWalker(masked).get_latex_nodes()
    visit(nodes)
    parents = {tuple(h["number"].split(".")[:-1]) for h in headings}
    for start, end, title, match in plain_candidates:
        if tuple(match[1].split(".")[:-1]) in parents:
            original_heading(start, end, title, match)
    headings.sort(key=lambda h: h["line"])
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    if edits and SUPPORT not in source:
        source = source.replace(r"\begin{document}", SUPPORT + "\n" + r"\begin{document}", 1)
    if edits and r"\LexoidOutlineOriginal" in source and ORIGINAL_SUPPORT not in source:
        source = source.replace(r"\begin{document}", ORIGINAL_SUPPORT + "\n" + r"\begin{document}", 1)
    return source, {"changed": len(edits), "headings": headings}
