"""
SyncTeX-opaque table environments: detection, width probing, conversion.

WHY THIS MODULE EXISTS
----------------------
tabularx / tabulary / tabu / longtabu / xltabular absorb the environment body into a
token list and re-typeset it several times to solve for X-column widths. Tokens
replayed from a token register no longer carry their original input-file line numbers,
so SyncTeX attributes the *whole table* to the replay point. Splitting cells onto
separate source lines therefore has NO effect inside these environments.

The observable proof of the same mechanism: `\\verb` does not work inside tabularx.
Rule of thumb -- if `\\verb` breaks in an environment, SyncTeX cannot see its cells.

THE FIX
-------
Convert the environment to plain `tabular`, replacing each X column with `p{<w>}`
where <w> is the width tabularx itself computed. The layout is unchanged (same
widths, same column decorations); only the two-pass machinery is removed, which is
what restores real line numbers.

Getting <w> without patching package internals: inside a p/X column, `\\hsize` IS the
column width. We temporarily inject `>{\\TXPROBE{t}{c}}` which \\typeout's `\\the\\hsize`,
compile once, and read the LAST reported value per (table, column) -- the last one is
the final pass.

THREE-STEP USE
--------------
    probe_tex, probes = instrument(src)          # write probe_tex, compile once
    widths = parse_widths(Path("job.log").read_text())
    out, report = convert(src, widths)           # then run tex_tables.transform_tex()
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .tex_tables import CS_RE, _read_balanced, _skip_ws, mask_comments

# --------------------------------------------------------------------------- #

#: body is absorbed + replayed -> line numbers destroyed
OPAQUE_ENVS = {"tabularx", "tabulary", "tabu", "longtabu", "xltabular"}

#: body is read straight from the file -> line numbers survive
TRANSPARENT_ENVS = {"tabular", "tabular*", "array", "longtable", "supertabular"}

#: (env, width-arg?, target env after conversion)
CONVERSION = {
    "tabularx":  (True,  "tabular"),
    "tabulary":  (True,  "tabular"),
    "tabu":      (False, "tabular"),      # tabu takes an OPTIONAL `to <len>` / `spread`
    "longtabu":  (False, "longtable"),
    "xltabular": (True,  "longtable"),
}

#: column types that consume the flexible space and must become p{<measured>}
FLEX_TYPES = {"X", "L", "C", "R", "J", "N"}

PROBE_MACRO = r"""
%% --- texopt width probe (REMOVE after measuring) --------------------------------
\makeatletter
\providecommand{\TXPROBE}[2]{\typeout{TEXOPT-W #1 #2 \the\hsize}}
\makeatother
"""

PROBE_LINE = re.compile(r"TEXOPT-W\s+(\d+)\s+(\d+)\s+([\d.]+pt)")
TABULARX_PACKAGE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)\\(?P<command>usepackage|RequirePackage)"
    r"(?P<options>\[[^\]\r\n]*\])?\{(?P<packages>[^{}\r\n]+)\}"
    r"(?P<tail>[ \t]*(?:%[^\r\n]*)?)(?P<newline>\r?\n|$)"
)


# --------------------------------------------------------------------------- #
# colspec tokenisation (preserves everything, unlike parse_colspec)
# --------------------------------------------------------------------------- #

@dataclass
class SpecItem:
    kind: str        # 'col' | 'sep'
    raw: str         # verbatim source of this item, including >{} <{} and args
    letter: str = ""  # primary type letter, for kind == 'col'
    pre: str = ""     # >{...} decorations
    post: str = ""    # <{...} decorations
    arg: str = ""     # {...} / [...] arguments of the type letter
    index: int = -1   # column index among 'col' items


def expand_colspec(spec: str) -> str:
    """Flatten `*{n}{...}` so downstream code sees a linear spec. Rendering-neutral."""
    out, i, n = [], 0, len(spec)
    while i < n:
        c = spec[i]
        if c == "*":
            gn = _read_balanced(spec, _skip_ws(spec, i + 1), "{", "}")
            if not gn:
                out.append(c); i += 1; continue
            gb = _read_balanced(spec, _skip_ws(spec, gn[1]), "{", "}")
            if not gb:
                out.append(c); i += 1; continue
            try:
                reps = max(0, int(gn[0].strip()))
            except ValueError:
                reps = 1
            out.append(expand_colspec(gb[0]) * reps)
            i = gb[1]
        elif c in "@!><":
            g = _read_balanced(spec, _skip_ws(spec, i + 1), "{", "}")
            if not g:
                out.append(c); i += 1; continue
            out.append(spec[i:g[1]])
            i = g[1]
        elif c == "\\":
            m = CS_RE.match(spec, i)
            end = m.end() if m else i + 1
            out.append(spec[i:end]); i = end
        else:
            out.append(c); i += 1
    return "".join(out)


def tokenize_colspec(spec: str) -> List[SpecItem]:
    items: List[SpecItem] = []
    i, n, col = 0, len(spec), 0
    pending_pre = ""
    while i < n:
        c = spec[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == ">":
            g = _read_balanced(spec, _skip_ws(spec, i + 1), "{", "}")
            if g:
                pending_pre += spec[i:g[1]]
                i = g[1]
                continue
            i += 1
            continue
        if c in "|@!":
            if c == "|":
                items.append(SpecItem("sep", "|"))
                i += 1
            else:
                g = _read_balanced(spec, _skip_ws(spec, i + 1), "{", "}")
                items.append(SpecItem("sep", spec[i:g[1]] if g else c))
                i = g[1] if g else i + 1
            continue
        if c == "\\":
            m = CS_RE.match(spec, i)
            end = m.end() if m else i + 1
            pending_pre += spec[i:end]
            i = end
            continue

        # a column type letter
        letter, start = c, i
        i += 1
        arg = ""
        while i < n:
            j = _skip_ws(spec, i)
            if j < n and spec[j] in "[{":
                g = (_read_balanced(spec, j, "[", "]") if spec[j] == "["
                     else _read_balanced(spec, j, "{", "}"))
                if not g:
                    break
                arg += spec[j:g[1]]
                i = g[1]
                continue
            break
        post = ""
        while True:
            j = _skip_ws(spec, i)
            if j < n and spec[j] == "<":
                g = _read_balanced(spec, _skip_ws(spec, j + 1), "{", "}")
                if not g:
                    break
                post += spec[j:g[1]]
                i = g[1]
                continue
            break
        items.append(SpecItem("col", pending_pre + spec[start:i], letter,
                              pending_pre, post, arg, col))
        pending_pre = ""
        col += 1
    if pending_pre:
        items.append(SpecItem("sep", pending_pre))
    return items


# --------------------------------------------------------------------------- #
# locating opaque tables
# --------------------------------------------------------------------------- #

@dataclass
class OpaqueTable:
    tid: int
    env: str
    begin_pos: int          # index of \begin
    body_pos: int           # index just past the arguments
    end_pos: int            # index of \end{env}
    line: int
    width_arg: str = ""     # the {W} of tabularx, verbatim incl. braces
    pre_args: str = ""      # everything between \begin{env} and the colspec
    spec: str = ""          # raw colspec (inside braces)
    spec_span: Tuple[int, int] = (-1, -1)   # absolute span of the colspec CONTENT
    flex_cols: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


BEGIN_OPAQUE = re.compile(
    r"\\begin\{(" + "|".join(re.escape(e) for e in sorted(OPAQUE_ENVS)) + r")\}")


def _matching_environment_end(masked_tex: str, env: str, start: int) -> int:
    """Return the matching end token, accounting for nested copies of *env*."""
    token = re.compile(r"\\(begin|end)\{" + re.escape(env) + r"\}")
    depth = 1
    for match in token.finditer(masked_tex, start):
        depth += 1 if match.group(1) == "begin" else -1
        if depth == 0:
            return match.start()
    return -1


def find_opaque(tex: str) -> List[OpaqueTable]:
    out: List[OpaqueTable] = []
    tid = 0
    masked = mask_comments(tex)
    for m in BEGIN_OPAQUE.finditer(masked):
        env = m.group(1)
        i = m.end()
        pre_start = i
        width_arg = ""

        # tabu / longtabu: optional `to <len>` or `spread <len>` (no braces)
        if env in ("tabu", "longtabu"):
            mm = re.compile(r"\s*(?:to|spread)\s*[^{\[]*").match(tex, i)
            if mm:
                i = mm.end()
        else:
            j = _skip_ws(tex, i)
            g = _read_balanced(tex, j, "{", "}")
            if g:
                width_arg = tex[j:g[1]]
                i = g[1]

        j = _skip_ws(tex, i)
        if j < len(tex) and tex[j] == "[":
            g = _read_balanced(tex, j, "[", "]")
            if g:
                i = g[1]

        j = _skip_ws(tex, i)
        g = _read_balanced(tex, j, "{", "}")
        if not g:
            continue
        spec, body = g[0], g[1]
        end = _matching_environment_end(masked, env, body)
        tid += 1

        flat = expand_colspec(spec)
        items = tokenize_colspec(flat)
        flex = [it.index for it in items if it.kind == "col" and it.letter in FLEX_TYPES]

        t = OpaqueTable(tid=tid, env=env, begin_pos=m.start(), body_pos=body,
                        end_pos=end if end >= 0 else len(tex),
                        line=tex.count("\n", 0, m.start()) + 1,
                        width_arg=width_arg, pre_args=tex[pre_start:j],
                        spec=flat, spec_span=(j + 1, g[1] - 1), flex_cols=flex)
        if not flex:
            t.notes.append("no flexible column found -- can be converted verbatim")
        if env in ("tabu", "longtabu") and re.search(r"X\s*\[", flat):
            t.notes.append("tabu X[..] weights/alignment parsed -- verify geometry")
        if re.search(r"\\hsize\s*=", flat):
            t.notes.append("\\hsize override in colspec -- stripped on conversion")
        out.append(t)
    return out


# --------------------------------------------------------------------------- #
# step 1: instrument
# --------------------------------------------------------------------------- #

def instrument(tex: str) -> Tuple[str, List[OpaqueTable]]:
    """Return (tex_with_probes, tables). Compile the result ONCE, then parse the log."""
    tables = find_opaque(tex)
    edits: List[Tuple[int, int, str]] = []
    for t in tables:
        items = tokenize_colspec(t.spec)
        columns = [it for it in items if it.kind == "col"]
        new_spec = []
        for it in items:
            if it.kind == "col":
                new_spec.append(r">{\TXPROBE{%d}{%d}}%s" % (t.tid, it.index, it.raw))
            else:
                new_spec.append(it.raw)
        edits.append((t.spec_span[0], t.spec_span[1], "".join(new_spec)))
        # A column that is covered by \multicolumn in every real row never executes
        # its >{\TXPROBE} preamble, so its width used to be missing from the log.
        # An empty probe-only row exercises every column without increasing any
        # natural column width.  The instrumented document is never user output.
        if columns:
            sentinel = "\n" + " & ".join("{}" for _ in columns) + r" \\" + "\n"
            edits.append((t.body_pos, t.body_pos, sentinel))

    out = tex
    for a, b, rep in sorted(edits, reverse=True):
        out = out[:a] + rep + out[b:]

    m = re.search(r"^\s*\\begin\{document\}", out, re.M)
    return (out[:m.start()] + PROBE_MACRO + out[m.start():] if m
            else PROBE_MACRO + out), tables


def parse_widths(log_text: str) -> Dict[Tuple[int, int], str]:
    r"""
    LAST value wins: earlier ones come from tabularx's trial passes.

    The value is kept as the VERBATIM literal printed by `\the\hsize`
    (e.g. "213.39569pt"). TeX guarantees that string round-trips back to the exact
    same sp value; parsing it into a float and re-formatting with %.2f loses up to
    0.005pt per column, which can flip a line break in a tightly-fitting cell.
    """
    widths: Dict[Tuple[int, int], str] = {}
    for m in PROBE_LINE.finditer(log_text.replace("\n", " ")):
        widths[(int(m.group(1)), int(m.group(2)))] = m.group(3)
    return widths


def remove_unused_tabularx_package(tex: str) -> tuple[str, int]:
    """Remove ``tabularx`` package declarations after all such tables are gone."""
    if find_opaque(tex):
        return tex, 0
    removed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        packages = [item.strip() for item in match.group("packages").split(",")]
        if "tabularx" not in packages:
            return match.group(0)
        removed += 1
        remaining = [item for item in packages if item != "tabularx"]
        if not remaining:
            return ""
        return (f'{match.group("indent")}\\{match.group("command")}'
                f'{match.group("options") or ""}{{{",".join(remaining)}}}'
                f'{match.group("tail")}{match.group("newline")}')

    return TABULARX_PACKAGE.sub(replace, tex), removed


# --------------------------------------------------------------------------- #
# step 2: convert
# --------------------------------------------------------------------------- #

HSIZE_ASSIGN = re.compile(r"\\hsize\s*=\s*[^\\{}]*(?:\\[a-zA-Z@]+)?\s*")


# --------------------------------------------------------------------------- #
# level 2: closed-form widths, no compile needed
# --------------------------------------------------------------------------- #

#: `>{\hsize=<k>\hsize}` (tabularx idiom) -- k multiplies the common X width
HSIZE_FACTOR = re.compile(r"\\hsize\s*=\s*([\d.]*)\s*\\hsize")
#: tabu `X[<k>]`, `X[<k>,<align>]`, `X[<align>,<k>]`
TABU_OPT = re.compile(r"\[([^\]]*)\]")

DEFAULT_TARGET = {"tabularx": r"\linewidth", "tabulary": r"\linewidth",
                  "xltabular": r"\linewidth", "tabu": r"\linewidth",
                  "longtabu": r"\linewidth"}


def _weight(it: SpecItem, env: str) -> float:
    """Relative width coefficient of a flexible column (1.0 when unspecified)."""
    if env in ("tabu", "longtabu"):
        m = TABU_OPT.search(it.arg or "")
        if m:
            for part in m.group(1).split(","):
                part = part.strip()
                try:
                    return float(part)
                except ValueError:
                    continue
        return 1.0
    m = HSIZE_FACTOR.search(it.pre or "")
    if m:
        return float(m.group(1)) if m.group(1) not in ("", ".") else 1.0
    return 1.0


def static_widths(t: "OpaqueTable") -> Optional[Dict[int, str]]:
    """
    Closed-form tabularx/xltabular widths for uniform X columns mixed with explicit
    p/m/b columns. Natural-width l/c/r columns, weighted X columns and custom
    inter-column material require a probe because their widths are content-dependent.
    """
    items = tokenize_colspec(t.spec)
    cols = [it for it in items if it.kind == "col"]
    if t.env not in ("tabularx", "xltabular") or not cols:
        return None
    if any(it.kind == "sep" and it.raw.startswith(("@", "!")) for it in items):
        return None

    flex = [it for it in cols if it.letter == "X"]
    if not flex or len(flex) != len(t.flex_cols):
        return None
    if any(_weight(it, t.env) != 1.0 for it in flex):
        return None

    fixed_widths: List[str] = []
    for it in cols:
        if it.letter == "X":
            continue
        if it.letter not in ("p", "m", "b"):
            return None
        arg = _first_braced_arg(it.arg)
        if not arg:
            return None
        fixed_widths.append(arg)

    n = len(cols)
    n_flex = len(flex)
    rules = sum(1 for it in items if it.kind == "sep" and it.raw == "|")
    target = t.width_arg.strip("{}") or DEFAULT_TARGET.get(t.env, r"\linewidth")
    fixed = "".join(rf" - {width}" for width in fixed_widths)
    leftover = (rf"{target}{fixed} - {2 * n}\tabcolsep"
                + (rf" - {rules}\arrayrulewidth" if rules else ""))

    out: Dict[int, str] = {}
    share = rf"\dimexpr({leftover})/{n_flex}\relax"
    for pos, it in enumerate(flex):
        if pos == n_flex - 1 and n_flex > 1:
            others = " - ".join(share for _ in flex[:-1])
            expr = rf"\dimexpr({leftover}) - {others}\relax"
        else:
            expr = share
        out[it.index] = expr
    return out


def _first_braced_arg(arg: str) -> str:
    """Return the content of a column type's first mandatory argument verbatim."""
    i = _skip_ws(arg, 0)
    got = _read_balanced(arg, i, "{", "}")
    return got[0].strip() if got else ""


@dataclass
class ConvertReport:
    converted: List[int] = field(default_factory=list)
    skipped: List[Tuple[int, str]] = field(default_factory=list)
    details: List[str] = field(default_factory=list)
    by_source: Dict[str, int] = field(default_factory=dict)   # measured / static

    def extend(self, other: "ConvertReport") -> None:
        self.converted.extend(other.converted)
        self.skipped.extend(other.skipped)
        self.details.extend(other.details)
        for source, count in other.by_source.items():
            self.by_source[source] = self.by_source.get(source, 0) + count


class UnconvertibleTable(RuntimeError):
    """Raised in strict mode: an opaque table would have survived optimisation."""


def convert(tex: str, widths: Optional[Dict[Tuple[int, int], str]] = None,
            slack_pt: float = 0.0, strict: bool = True,
            allow_static: bool = True) -> Tuple[str, ConvertReport]:
    """
    Rewrite EVERY opaque environment into a transparent one.

    Width source ladder, per table:
      1. `widths` -- measured by the probe compile. Exact for any colspec.
      2. static_widths() -- exact closed-form \\dimexpr for uniform X columns
         mixed with explicit p/m/b columns.
      3. strict=True -> raise UnconvertibleTable. We never invent a width, because a
         wrong width silently changes the layout.
    """
    widths = widths or {}
    tables = find_opaque(tex)
    rep = ConvertReport()
    edits: List[Tuple[int, int, str]] = []

    for t in tables:
        _needs_width, target = CONVERSION[t.env]
        items = tokenize_colspec(t.spec)

        measured = {c: widths[(t.tid, c)] for c in t.flex_cols if (t.tid, c) in widths}
        source = ""
        exprs: Dict[int, str] = {}

        if len(measured) == len(t.flex_cols):
            source = "measured"
            exprs = {c: (measured[c] if not slack_pt
                         else rf"\dimexpr {measured[c]} - {slack_pt}pt\relax")
                     for c in t.flex_cols}
        elif allow_static:
            st = static_widths(t)
            if st is not None:
                source = "static"
                exprs = st

        if not exprs and t.flex_cols:
            why = (f"{t.env} at line {t.line}: no measured width "
                   f"(missing cols {[c for c in t.flex_cols if c not in measured]}) "
                   f"and the colspec is not closed-form solvable "
                   f"({'; '.join(t.notes) or 'mixed rigid/flexible or @{} material'}). "
                   f"Run the probe compile so it can be measured.")
            if strict:
                raise UnconvertibleTable(why)
            rep.skipped.append((t.line, why))
            continue
        if not t.flex_cols:
            source = source or "verbatim"

        new_spec: List[str] = []
        for it in items:
            if it.kind != "col" or it.letter not in FLEX_TYPES:
                new_spec.append(it.raw)
                continue
            pre = HSIZE_ASSIGN.sub("", it.pre)      # p{w} sets \hsize itself
            pre = re.sub(r">\{\s*\}", "", pre)
            align = _flex_align(it, t.env)
            if align:
                pre += r">{%s\arraybackslash}" % align
            new_spec.append(f"{pre}p{{{exprs[it.index]}}}{it.post}")

        pos = re.search(r"\[[tbc]\]", t.pre_args)
        head_new = "\\begin{%s}%s{%s}" % (target, pos.group(0) if pos else "",
                                          "".join(new_spec))
        spec_hash = hashlib.sha256(t.spec.encode("utf-8")).hexdigest()[:12]
        head_new = (
            f"% texopt: table=t{t.tid:04d} from={t.env} to={target} "
            f"method={source} spec_sha256={spec_hash}\n" + head_new
        )
        edits.append((t.begin_pos, t.body_pos, head_new))
        edits.append((t.end_pos, t.end_pos + len(r"\end{%s}" % t.env),
                      r"\end{%s}" % target))
        rep.converted.append(t.line)
        rep.by_source[source] = rep.by_source.get(source, 0) + 1
        rep.details.append(
            f"line {t.line}: {t.env} -> {target} [{source}]  "
            + ", ".join(f"col{c}={exprs[c]}" for c in sorted(exprs))
            + ("; " + "; ".join(t.notes) if t.notes else ""))

    out = tex
    for a, b, r in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:a] + r + out[b:]

    leftover = find_opaque(out)
    if leftover and strict:
        raise UnconvertibleTable(
            f"{len(leftover)} opaque table(s) survived conversion at lines "
            f"{[t.line for t in leftover]}")
    return out, rep


def _flex_align(it: SpecItem, env: str) -> str:
    """Alignment implied by the flexible column type / tabu option."""
    if env in ("tabu", "longtabu"):
        m = TABU_OPT.search(it.arg or "")
        if m:
            for part in (x.strip() for x in m.group(1).split(",")):
                if part in ("l", "c", "r"):
                    return {"l": r"\raggedright", "c": r"\centering",
                            "r": r"\raggedleft"}[part]
        return ""
    return {"L": r"\raggedright", "C": r"\centering",
            "R": r"\raggedleft", "J": "", "X": "", "N": ""}.get(it.letter, "")


# --------------------------------------------------------------------------- #
# level 1 automation: run the probe compile ourselves
# --------------------------------------------------------------------------- #

def run_probe(tex: str, workdir: Path, engine: str = "xelatex",
              source_dir: Optional[Path] = None,
              jobname: str = "texopt_probe", timeout: int = 300
              ) -> Tuple[Dict[Tuple[int, int], str], str]:
    """
    Write an instrumented copy, compile it once, return (widths, log).
    Never raises on a LaTeX error: a partial log still yields usable widths for the
    tables that were reached, and convert() falls back or fails loudly per table.
    """
    import subprocess
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    workdir = workdir.resolve()
    source_dir = Path(source_dir).resolve() if source_dir else workdir
    inst, _tables = instrument(tex)
    src = workdir / f"{jobname}.tex"
    src.write_text(inst, "utf-8")
    try:
        # This is a measuring pass, not a validity gate. One recoverable alignment
        # error early in a Lexoid document must not prevent later tabularx tables
        # from reporting their widths. The caller still parses and logs all errors.
        subprocess.run([engine, "-interaction=nonstopmode",
                        "-synctex=0", f"-jobname={jobname}",
                        f"-output-directory={workdir}", str(src)],
                       cwd=source_dir, capture_output=True, timeout=timeout)
    except Exception:
        pass
    log = workdir / f"{jobname}.log"
    text = log.read_text("utf-8", errors="replace") if log.exists() else ""
    return parse_widths(text), text


# --------------------------------------------------------------------------- #
# format-preservation guard
# --------------------------------------------------------------------------- #

#: constructs whose behaviour differs between a replayed body (N passes) and a
#: directly-read body (1 pass). Converting is CORRECT for all of these, but the
#: output can legitimately differ from the pre-conversion PDF, so they must be
#: surfaced rather than silently "preserved".
MULTIPASS_SENSITIVE = {
    r"\\footnote": "footnote: tabularx routes these through \\TX@ftn; plain tabular "
                   "does not. Placement may change.",
    r"\\footnotemark": "footnote mark numbering may change",
    r"\\stepcounter": "counter stepped once instead of once per trial pass",
    r"\\refstepcounter": "counter/label value may change (previously stepped per pass)",
    r"\\addtocounter": "counter arithmetic now applied once",
    r"\\label": "label may bind to a different counter value",
    r"\\caption": "caption numbering may change",
    r"\\marginpar": "marginpar emitted once instead of per pass",
    r"\\index": "index entry emitted once instead of per pass",
}

#: constructs that were BROKEN before and merely start working -- report, do not warn
MULTIPASS_FIXED = {
    r"\\verb": "\\verb now works (it could not survive the replayed body)",
}


@dataclass
class FormatRisk:
    line: int
    env: str
    construct: str
    note: str
    severity: str          # 'warn' | 'info'


def format_guard(tex: str) -> List[FormatRisk]:
    """Scan the bodies of opaque tables for constructs whose output may shift."""
    risks: List[FormatRisk] = []
    for t in find_opaque(tex):
        body = tex[t.body_pos:t.end_pos]
        base_line = tex.count("\n", 0, t.body_pos) + 1
        for pat, note in MULTIPASS_SENSITIVE.items():
            for m in re.finditer(pat + r"\b", body):
                risks.append(FormatRisk(base_line + body.count("\n", 0, m.start()),
                                        t.env, pat.replace("\\\\", "\\"), note, "warn"))
        for pat, note in MULTIPASS_FIXED.items():
            if re.search(pat + r"\b", body):
                risks.append(FormatRisk(t.line, t.env, pat.replace("\\\\", "\\"),
                                        note, "info"))
    return risks


def audit(tex: str) -> str:
    """Human-readable diagnosis: which tables can never sync, and why."""
    tables = find_opaque(tex)
    if not tables:
        return "No SyncTeX-opaque table environments found. Cell splitting is sufficient."
    lines = [f"{len(tables)} SyncTeX-opaque table(s) -- cell-level sync is IMPOSSIBLE "
             f"in these until converted:"]
    for t in tables:
        solvable = "closed-form" if static_widths(t) is not None else "NEEDS PROBE"
        lines.append(f"  line {t.line:>6}  {t.env:<10} flex cols {str(t.flex_cols or '-'):<10}"
                     f" {solvable}"
                     + (f"  [{'; '.join(t.notes)}]" if t.notes else ""))
    risks = format_guard(tex)
    if risks:
        lines.append("")
        lines.append("format-preservation risks inside those tables:")
        for r in risks:
            lines.append(f"  [{r.severity}] line {r.line:>6}  {r.construct}: {r.note}")
    return "\n".join(lines)
