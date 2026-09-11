"""Preserve source-page boundaries and validate the actual compiled PDF."""

from __future__ import annotations

import math
from pathlib import Path
import re

from .syntax_repair import PAGE_COMPLETED, canonicalize_document_terminator, split_lexoid_pages
from .tex_tables import _peel_prefix, iter_structural, mask_comments, split_align_body


BLOCK_START = "% >>> lexoid physical layout >>>"
BLOCK_END = "% <<< lexoid physical layout <<<"
BLOCK = r"""
% >>> lexoid physical layout >>>
\usepackage{geometry}
\usepackage{adjustbox,etoolbox}
\makeatletter
% Route form symbols to the CJK font instead of Latin Modern's missing glyphs.
\@ifpackageloaded{xeCJK}{%
  \xeCJKDeclareCharClass{CJK}{"2460 -> "2469, "25CF}}{}
\renewcommand{\maketitle}{%
  \par\begingroup\centering
  {\large\bfseries\@title\par}%
  \ifx\@author\@empty\else{\normalsize\let\and\quad\@author\par}\fi
  \ifx\@date\@empty\else{\normalsize\@date\par}\fi
  \@thanks\endgroup\par}
\newcount\LexoidAbsolutePage
\AddToHook{shipout/before}{\global\advance\LexoidAbsolutePage by 1}
% XeTeX needs the paper special on every physical page, including spill pages.
\AddToHook{shipout/background}{%
  \put(0,0){\special{papersize=\the\paperwidth,\the\paperheight}}}
\newwrite\LexoidLayoutStream
\AtBeginDocument{\immediate\openout\LexoidLayoutStream=\jobname.lxp}
\newcommand{\LexoidPageMark}[2]{%
  \write\LexoidLayoutStream{#1,#2,\the\LexoidAbsolutePage}}
\newcommand{\LexoidPageStart}[4]{%
  \clearpage
  \newgeometry{layoutwidth=#2,layoutheight=#3,left=12mm,right=12mm,top=12mm,bottom=12mm}%
  \setlength{\paperwidth}{#2}\setlength{\paperheight}{#3}%
  \begingroup
  \ifnum#4>0
    \setlength{\parskip}{0pt}\setlength{\tabcolsep}{2pt}%
    \renewcommand{\arraystretch}{1.0}%
    \@ifpackageloaded{titlesec}{%
      \titlespacing*{\section}{0pt}{3pt}{2pt}%
      \titlespacing*{\subsection}{0pt}{2pt}{1pt}%
      \titlespacing*{\subsubsection}{0pt}{2pt}{1pt}}{}%
  \fi
  \ifnum#4>1 \fontsize{9pt}{10pt}\selectfont\fi
  \LexoidPageMark{#1}{start}}
\newcommand{\LexoidPageEnd}[1]{%
  \par\LexoidPageMark{#1}{end}\endgroup}
% Fixed-height boxes must report their full content size before outer fitting.
\patchcmd{\@iiiparbox}{\setlength\@tempdimb{#2}}{%
  \setlength\@tempdimb{#2}%
  \ifdim\@tempdimb<\dimexpr\ht\@tempboxa+\dp\@tempboxa\relax
    \@tempdimb=\dimexpr\ht\@tempboxa+\dp\@tempboxa\relax
  \fi}{}{\PackageWarning{lexoid}{Fixed-height box fitting unavailable}}
% Fit outer unbreakable panels after headings, reserving room for a footer.
\newdimen\LexoidPanelHeight
\newcommand{\LexoidPanelLimit}{%
  \LexoidPanelHeight=\dimexpr\pagegoal-\pagetotal\relax
  \ifdim\LexoidPanelHeight>\textheight \LexoidPanelHeight=\textheight\fi
  \ifdim\LexoidPanelHeight<.5\textheight \LexoidPanelHeight=\textheight\fi
  \advance\LexoidPanelHeight by -3\baselineskip}
\newcommand{\LexoidPanelBegin}{%
  \begingroup
  \ifinner
    \let\LexoidPanelEnd\relax
  \else
    \LexoidPanelLimit
    \def\LexoidPanelEnd{\csname end\endcsname{adjustbox}}%
    \csname begin\endcsname{adjustbox}{max totalsize={\linewidth}{\LexoidPanelHeight},valign=t}%
  \fi}
\BeforeBeginEnvironment{minipage}{\LexoidPanelBegin}
\AfterEndEnvironment{minipage}{\LexoidPanelEnd\endgroup}
\BeforeBeginEnvironment{tabular}{\LexoidPanelBegin}
\AfterEndEnvironment{tabular}{\LexoidPanelEnd\endgroup}
\let\LexoidOriginalRotatebox\rotatebox
\renewcommand{\rotatebox}[3][]{%
  \ifinner\LexoidOriginalRotatebox[#1]{#2}{#3}%
  \else\begingroup\LexoidPanelLimit
    \adjustbox{max totalsize={\linewidth}{\LexoidPanelHeight}}{%
      \LexoidOriginalRotatebox[#1]{#2}{#3}}%
  \endgroup\fi}
\makeatother
% <<< lexoid physical layout <<<
"""


def _remove_generated_layout(source):
    start = source.find(BLOCK_START)
    if start >= 0:
        end = source.find(BLOCK_END, start)
        if end < 0:
            raise ValueError("Incomplete physical page layout block")
        source = source[:start] + source[end + len(BLOCK_END):]
    return re.sub(r"(?m)^\\LexoidPage(?:Start\{\d+\}\{[\d.]+bp\}\{[\d.]+bp\}\{[012]\}|End\{\d+\})\s*\n", "", source)


def prepare_layout(source, evidence, profiles=None):
    """Use render dimensions after orientation correction; retain all source text."""
    profiles = profiles or {}
    source = _remove_generated_layout(source)
    source = _lower_empty_cell_spacers(source)
    source, _ = canonicalize_document_terminator(source)
    markers = [(int(m[1]), int(m[2])) for m in PAGE_COMPLETED.finditer(source)]
    pages = evidence.get("pages", [])
    total = len(pages)
    if (evidence.get("schema") != "recognition/v1" or not total
            or [p["page"] for p in pages] != list(range(1, total + 1))
            or markers != [(n, total) for n in range(1, total + 1)]):
        raise ValueError("Layout evidence and TeX page order must agree")
    sizes = []
    for page in pages:
        render = page["render"]
        dpi, width, height = (float(render[k]) for k in ("dpi", "width", "height"))
        if not all(math.isfinite(x) and x > 0 for x in (dpi, width, height)):
            raise ValueError("Invalid page dimensions")
        sizes.append((round(width * 72 / dpi, 3), round(height * 72 / dpi, 3)))

    chunks = []
    for number, _, chunk in split_lexoid_pages(source):
        if number == 1:
            preamble, sep, body = chunk.partition(r"\begin{document}")
            if not sep:
                raise ValueError("Layout requires a complete document")
            prefix = preamble.rstrip() + "\n" + BLOCK.strip() + "\n" + sep + "\n"
        else:
            prefix, body = "", chunk
        # Source-page breaks are owned by LexoidPageStart; internal breaks stay
        # visible to the validator instead of silently hiding a genuine overflow.
        body = re.sub(r"\A\s*\\(?:newpage|clearpage)\b\s*", "", body)
        body = body.lstrip("\r\n")
        marker = PAGE_COMPLETED.search(body)
        width, height = sizes[number - 1]
        profile = profiles.get(number, 0)
        if profile not in (0, 1, 2):
            raise ValueError("Unknown page layout profile")
        start = rf"\LexoidPageStart{{{number}}}{{{width:g}bp}}{{{height:g}bp}}{{{profile}}}" + "\n"
        end = rf"\LexoidPageEnd{{{number}}}" + "\n"
        chunks.append(prefix + start + body[:marker.start()].rstrip() + "\n" + end + body[marker.start():])
    report = {"schema": "page-layout/v1", "ok": False, "expected_pages": total,
              "expected_sizes": sizes, "profiles": {str(p): profiles.get(p, 0) for p in range(1, total + 1)}}
    return "".join(chunks), report


def _lower_empty_cell_spacers(source):
    """A blank cell's minimum height extends downward from its first baseline."""
    edits = []
    rule = re.compile(r"\s*(\\rule\s*\{\s*0(?:pt|bp|cm|mm|in)\s*\}\s*\{([^{}]+)\})\s*\Z")

    def scan(segment, base=0):
        tokens = iter(iter_structural(mask_comments(segment)))
        for begin in tokens:
            if begin.kind != "align_begin":
                continue
            end = next((token for token in tokens if token.kind == "align_end"), None)
            if end is None:
                continue
            offset = base + begin.body_start
            for row in split_align_body(segment[begin.body_start:end.start]):
                for cell in row.cells:
                    scan(cell.text, offset)
                    _, payload = _peel_prefix(mask_comments(cell.text))
                    match = rule.fullmatch(payload)
                    if match:
                        height = match[2]
                        replacement = rf"\rule[-\dimexpr{height}-\ht\strutbox\relax]{{0pt}}{{{height}}}"
                        prefix_size = len(cell.text) - len(payload)
                        edits.append((offset + prefix_size + match.start(1),
                                      offset + prefix_size + match.end(1), replacement))
                    offset += len(cell.text) + len(cell.sep)

    scan(source)
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    return source


def validate_page_map(expected, records, actual):
    errors = []
    if actual != expected:
        errors.append(f"PDF page count differs: expected {expected}, got {actual}")
    wanted = [(n, side, n) for n in range(1, expected + 1) for side in ("start", "end")]
    if records != wanted:
        errors.append("Source page boundaries do not match output PDF pages")
    return errors


def inspect_layout(pdf_path, report):
    import pypdfium2 as pdfium

    records = []
    marker_file = Path(pdf_path).with_suffix(".lxp")
    if marker_file.exists():
        for line in marker_file.read_text().splitlines():
            match = re.fullmatch(r"(\d+),(start|end),(\d+)", line.strip())
            if match:
                records.append((int(match[1]), match[2], int(match[3])))
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        actual = len(document)
        sizes = []
        outside = []
        for n in range(actual):
            page = document[n]
            width, height = page.get_size()
            sizes.append((width, height))
            textpage = page.get_textpage()
            for i in range(textpage.count_chars()):
                if not textpage.get_text_range(i, 1).strip():
                    continue
                left, bottom, right, top = textpage.get_charbox(i)
                if left < -1 or bottom < -1 or right > width + 1 or top > height + 1:
                    outside.append(n + 1)
                    break
            textpage.close()
            page.close()
    finally:
        document.close()
    errors = validate_page_map(report["expected_pages"], records, actual)
    if outside:
        errors.append(f"Text extends outside PDF page bounds: {outside}")
    page_map = []
    for n in range(1, report["expected_pages"] + 1):
        entry = {"source_page": n}
        for number, side, target in records:
            if number == n:
                entry[side] = target
        page_map.append(entry)
    wrong_sizes = set()
    for entry, expected_size in zip(page_map, report["expected_sizes"]):
        start, end = entry.get("start", 0), entry.get("end", 0)
        if not 1 <= start <= end <= actual:
            continue
        for target in range(start, end + 1):
            if any(abs(a - b) > 1 for a, b in zip(sizes[target - 1], expected_size)):
                wrong_sizes.add(target)
    if not records and len(sizes) == len(report["expected_sizes"]):
        wrong_sizes.update(n for n, (pair, target) in enumerate(
            zip(sizes, report["expected_sizes"]), 1)
            if any(abs(a - b) > 1 for a, b in zip(pair, target)))
    if wrong_sizes:
        errors.append("Output page dimensions differ from source reading orientation")
    report.update(ok=not errors, actual_pages=actual, actual_sizes=sizes,
                  page_map=page_map, errors=errors, outside_pages=outside,
                  wrong_size_pages=sorted(wrong_sizes))
    return not errors
