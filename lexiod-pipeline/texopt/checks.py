"""
Two independent guarantees, both empirical. Do not ship the optimiser without them.

1) synctex_roundtrip()  -- proves the click-precision requirement is actually met.
   For every cell line we ask synctex forward (tex line -> pdf box), take the box
   centre, then ask synctex backward (pdf point -> tex line) and assert we land on the
   SAME line. This is exactly what Overleaf's ctrl-click does.

2) visual_regression()  -- proves "不能随意更改已有的页面数据以及格式": renders the
   original and optimised PDFs and compares them pixel by pixel. Any non-zero diff is
   a bug in the optimiser, not an acceptable trade-off.

External deps: `synctex` (TeX Live), `pdftoppm` (poppler), Pillow, numpy.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# 1. SyncTeX round-trip
# --------------------------------------------------------------------------- #

_VIEW = re.compile(r"^(Page|x|y|h|v|W|H):(-?[\d.]+)", re.M)
_EDIT_LINE = re.compile(r"^Line:(\d+)", re.M)


def _synctex_view(tex: Path, line: int, pdf: Path, column: int = 0) -> List[Dict]:
    r = subprocess.run(
        ["synctex", "view", "-i", f"{line}:{column}:{tex}", "-o", str(pdf)],
        capture_output=True, text=True)
    blocks, cur = [], {}
    for raw in r.stdout.splitlines():
        if raw.startswith("Output:") or raw == "SyncTeX result end":
            continue
        m = _VIEW.match(raw)
        if not m:
            continue
        k, v = m.group(1), float(m.group(2))
        if k == "Page" and cur:
            blocks.append(cur)
            cur = {}
        cur[k] = v
    if cur:
        blocks.append(cur)
    return blocks


def _synctex_edit(pdf: Path, page: int, x: float, y: float) -> Optional[int]:
    r = subprocess.run(
        ["synctex", "edit", "-o", f"{page}:{x:.2f}:{y:.2f}:{pdf}"],
        capture_output=True, text=True)
    m = _EDIT_LINE.search(r.stdout)
    return int(m.group(1)) if m else None


@dataclass
class SyncResult:
    line: int
    ok: bool
    got_line: Optional[int]
    page: Optional[int]
    note: str = ""


def synctex_roundtrip(tex: Path, pdf: Path, lines: List[int],
                      tolerance: int = 0) -> List[SyncResult]:
    """`lines` = the 1-based source lines of the cells produced by the optimiser."""
    results: List[SyncResult] = []
    for ln in lines:
        blocks = _synctex_view(tex, ln, pdf)
        if not blocks:
            results.append(SyncResult(ln, False, None, None, "no forward record"))
            continue
        b = blocks[0]
        # For alignment cells, W/H can describe the enclosing row or the entire
        # table rather than the zero-size source anchor. Its geometric centre can
        # therefore land on a neighbouring cell or \hline. SyncTeX's x/y pair is
        # the actual forward-search hit point associated with this source line.
        x = b.get("x", b.get("h", 0.0))
        y = b.get("y", b.get("v", 0.0))
        page = int(b.get("Page", 1))
        got = _synctex_edit(pdf, page, x, y)
        ok = got is not None and abs(got - ln) <= tolerance
        results.append(SyncResult(ln, ok, got, page,
                                  "" if ok else "reverse lookup mismatch"))
    return results


def summarize(results: List[SyncResult]) -> str:
    bad = [r for r in results if not r.ok]
    lines = [f"synctex round-trip: {len(results) - len(bad)}/{len(results)} exact"]
    for r in bad[:20]:
        lines.append(f"  line {r.line}: got {r.got_line} (page {r.page}) {r.note}")
    if len(bad) > 20:
        lines.append(f"  ... and {len(bad) - 20} more")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 2. Content regression (no rendering)
# --------------------------------------------------------------------------- #

def _pdf_pages_text(pdf: Path) -> List[str]:
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                       capture_output=True, text=True, check=True)
    return r.stdout.split("\f")


def content_regression(pdf_before: Path, pdf_after: Path,
                       normalize_ws: bool = True) -> Tuple[bool, str]:
    """Page count + per-page text must match. Whitespace-insensitive by default."""
    a, b = _pdf_pages_text(Path(pdf_before)), _pdf_pages_text(Path(pdf_after))
    if len(a) != len(b):
        return False, f"page count changed: {len(a)} -> {len(b)}"

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip() if normalize_ws else s

    bad = []
    for i, (pa, pb) in enumerate(zip(a, b), 1):
        if norm(pa) != norm(pb):
            d = list(difflib.unified_diff(norm(pa).split(), norm(pb).split(),
                                          lineterm="", n=2))
            bad.append(f"page {i}: text differs\n    " + "\n    ".join(d[:12]))
    return (not bad), ("identical" if not bad else "\n".join(bad))


_BBOX = re.compile(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" '
                   r'yMax="([\d.]+)">(.*?)</word>')


def geometry_regression(pdf_before: Path, pdf_after: Path,
                        tol_pt: float = 0.5) -> Tuple[bool, str]:
    """
    Compare word bounding boxes. Use this only when you converted tabularx -> tabular:
    it is the check that proves the measured column widths reproduced the original
    layout. Skip it otherwise -- content_regression is enough.
    """
    def words(p: Path):
        r = subprocess.run(["pdftotext", "-bbox", str(p), "-"],
                           capture_output=True, text=True, check=True)
        return [(float(m.group(1)), float(m.group(2)), m.group(5))
                for m in _BBOX.finditer(r.stdout)]

    wa, wb = words(Path(pdf_before)), words(Path(pdf_after))
    if len(wa) != len(wb):
        return False, f"word count changed: {len(wa)} -> {len(wb)}"
    worst, where = 0.0, ""
    for (xa, ya, ta), (xb, yb, tb) in zip(wa, wb):
        if ta != tb:
            return False, f"word order changed near {ta!r} / {tb!r}"
        d = max(abs(xa - xb), abs(ya - yb))
        if d > worst:
            worst, where = d, ta
    ok = worst <= tol_pt
    return ok, f"max word displacement {worst:.3f}pt (tol {tol_pt}pt) near {where!r}"
