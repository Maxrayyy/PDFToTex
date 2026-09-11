"""
Interface 2/3 backend: optimise a lexoid .tex and report what changed.

    python -m texopt.cli optimise IN.tex -o OUT.tex --start-page 76 \
        --registry fields.json --report report.json
    python -m texopt.cli verify OUT.tex OUT.pdf --registry fields.json
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import opaque, preamble
from .fields import DetectorConfig, annotate_fields, write_registry, basic_fields
from .llm import DeferredBatchNamer, LLMBatchNamer
from .model_config import resolve_model
from .local_tex import normalize_tex
from .outline import normalize_outline
from .syntax_check import validate_latex
from .syntax_repair import (LLMSyntaxRepairer, canonicalize_document_terminator,
                            page_diagnostic_hints,
                            page_numbers_for_lines,
                            repair_invariant_violations)
from .tex_tables import TableStat, transform_tex
from .textio import read_text_auto, write_utf8_atomic


class _Tee:
    """Mirror stderr to the terminal and the persistent optimizer log."""

    def __init__(self, *streams) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _event(code: str, message: str, level: str = "INFO", **data) -> None:
    """Emit a timestamped, greppable event with optional structured context."""
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    suffix = (" | " + json.dumps(data, ensure_ascii=False, sort_keys=True)
              if data else "")
    print(f"{stamp} [{level}] [{code}] {message}{suffix}",
          file=sys.stderr, flush=True)


def remove_explicit_sync_anchors(source: str) -> tuple[str, int]:
    """Replace rendered-cell ``\\SA{}`` anchors at final export with one space."""
    anchor = r"\SA{}"
    count = source.count(anchor)
    return source.replace(anchor, " "), count


def _latex_diagnostics(log_text: str, limit: int = 12) -> dict:
    """Extract concise TeX errors/warnings while retaining the full probe log."""
    errors, warnings = [], []
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if (line.startswith("!") or "Emergency stop" in line
                or "Fatal error" in line):
            errors.append(line)
        elif ("Warning:" in line or line.startswith(("Overfull", "Underfull"))):
            warnings.append(line)
    return {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors[:limit],
        "warnings": warnings[:limit],
    }


def _probe_error_pages(log_text: str, probe_tex: Path) -> set[int]:
    """Locate the physical Lexoid page containing TeX's first probe error."""
    first_error = re.search(r"(?ms)^!.*?^l\.(\d+)\b", log_text)
    if not first_error or not probe_tex.exists():
        return set()
    instrumented = probe_tex.read_text("utf-8", errors="replace")
    return page_numbers_for_lines(instrumented, [int(first_error.group(1))])


def _probe_error_excerpt(
    log_text: str, probe_tex: Path, before: int = 24, after: int = 4
) -> str:
    """Return numbered source context preceding TeX's first reported error."""
    first_error = re.search(r"(?ms)^!.*?^l\.(\d+)\b", log_text)
    if not first_error or not probe_tex.exists():
        return ""
    error_line = int(first_error.group(1))
    lines = probe_tex.read_text("utf-8", errors="replace").splitlines()
    start = max(1, error_line - before)
    end = min(len(lines), error_line + after)
    return "\n".join(
        f"{line_number}: {lines[line_number - 1]}"
        for line_number in range(start, end + 1)
    )


def _source_excerpt(source: str, line: int | None,
                    before: int = 24, after: int = 4) -> str:
    if not line:
        return ""
    lines = source.splitlines()
    start = max(1, line - before)
    end = min(len(lines), line + after)
    return "\n".join(
        f"{line_number}: {lines[line_number - 1]}"
        for line_number in range(start, end + 1)
    )


def _conversion_error_line(error: BaseException) -> int | None:
    match = re.search(r"\bat line (\d+)\b", str(error))
    return int(match.group(1)) if match else None


def _choose_output_path(input_path: str) -> str:
    """Open the platform save dialog and return the selected .tex path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("native output picker requires tkinter") from exc
    source = Path(input_path).resolve()
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError(
            "native output picker is unavailable; use --output-dir and --output-name"
        ) from exc
    root.withdraw()
    try:
        root.update()
        selected = filedialog.asksaveasfilename(
            title="Export optimized LaTeX",
            initialdir=str(source.parent),
            initialfile=f"{source.stem}.opt.tex",
            defaultextension=".tex",
            filetypes=[("LaTeX files", "*.tex"), ("All files", "*")],
        )
    finally:
        root.destroy()
    return selected


def cmd_audit(a: argparse.Namespace) -> int:
    print(opaque.audit(read_text_auto(a.input).text))
    return 0


def _risks(src: str) -> list:
    return [{"line": r.line, "env": r.env, "construct": r.construct,
             "note": r.note, "severity": r.severity}
            for r in opaque.format_guard(src)]


_HANDWRITTEN_VALUE = re.compile(r"\\handwritten\{([^{}]*)\}")
_HANDWRITTEN_SCI = re.compile(r"(?<![A-Za-z0-9])([0-9]+(?:[.][0-9]+)?)\^([0-9]+)")
_HANDWRITTEN_ESCAPES = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                        "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def sanitize_handwritten_fields(src: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value, out, end = match.group(1), [], 0
        for sci in _HANDWRITTEN_SCI.finditer(value):
            out.append("".join(_HANDWRITTEN_ESCAPES.get(c, c) for c in value[end:sci.start()]))
            out.append(sci.group(1) + r"\textsuperscript{" + sci.group(2) + "}")
            end = sci.end()
        out.append("".join(_HANDWRITTEN_ESCAPES.get(c, c) for c in value[end:]))
        return r"\handwritten{" + "".join(out) + "}"
    return _HANDWRITTEN_VALUE.sub(replace, src)


def _compile_latex(tex_path: Path, source_dir: Path, engine: str,
                   timeout: int, runs: int = 2, *, layout_report=None) -> tuple[bool, str]:
    """Compile the exact exported TeX in an isolated output directory."""
    tex_path = tex_path.resolve()
    source_dir = source_dir.resolve()
    chunks: list[str] = []
    try:
        compile_target = str(tex_path)
        source_text = tex_path.read_text("utf-8", errors="replace")
        ctex_class = re.search(
            r"\\documentclass(?P<options>\[[^\]]*\])?"
            r"\{(?P<class>ctexart|ctexbook|ctexrep)\}", source_text
        )
        # Overleaf's Linux images use portable Fandol fonts.  macOS ctex defaults
        # to the "mac" fontset, which may reference optional system fonts such as
        # Kaiti SC.  Inject the Overleaf-equivalent choice only into the check run.
        if (ctex_class and
                "fontset=" not in (ctex_class.group("options") or "")):
            class_name = ctex_class.group("class")
            compile_target = (
                rf"\PassOptionsToClass{{fontset=fandol}}{{{class_name}}}"
                rf"\input{{{tex_path}}}"
            )
        with tempfile.TemporaryDirectory(
            prefix=".texopt-compile-", dir=str(tex_path.parent)
        ) as directory:
            output_dir = Path(directory).resolve()
            env = os.environ.copy()
            search_dirs = os.pathsep.join((str(source_dir), str(tex_path.parent), ""))
            env["TEXINPUTS"] = search_dirs + env.get("TEXINPUTS", "")
            for pass_number in range(1, runs + 1):
                result = subprocess.run(
                    [engine, "-interaction=nonstopmode", "-halt-on-error",
                     "-file-line-error", "-synctex=1",
                     f"-jobname={tex_path.stem}",
                     f"-output-directory={output_dir}", compile_target],
                    cwd=source_dir, env=env, capture_output=True, text=True,
                    errors="replace", timeout=timeout, check=False,
                )
                chunks.append(
                    f"===== {engine} pass {pass_number}/{runs} "
                    f"(exit {result.returncode}) =====\n{result.stdout}{result.stderr}"
                )
                if result.returncode != 0:
                    return False, "\n".join(chunks)
            pdf = output_dir / f"{tex_path.stem}.pdf"
            if layout_report is not None and pdf.exists():
                import shutil
                from .page_layout import inspect_layout

                inspect_layout(pdf, layout_report)
                preview = tex_path.with_suffix(".layout.pdf")
                shutil.copyfile(pdf, preview)
                layout_report["preview_pdf"] = str(preview)
                write_utf8_atomic(tex_path.with_suffix(".layout.json"),
                                  json.dumps(layout_report, ensure_ascii=False, indent=2))
                chunks.append("LAYOUT_CHECK: " + json.dumps(layout_report, ensure_ascii=False))
                return pdf.stat().st_size > 0, "\n".join(chunks)
            return pdf.exists() and pdf.stat().st_size > 0, "\n".join(chunks)
    except (OSError, subprocess.TimeoutExpired) as exc:
        chunks.append(f"compile exception: {type(exc).__name__}: {exc}\n")
        return False, "\n".join(chunks)


def cmd_probe(a: argparse.Namespace) -> int:
    """Emit an instrumented copy. Compile it ONCE, then pass its .log to `optimise`."""
    src = read_text_auto(a.input).text
    inst, tables = opaque.instrument(src)
    write_utf8_atomic(a.output, inst)
    print(f"wrote {a.output} with probes for {len(tables)} opaque table(s).")
    print("next: xelatex -interaction=nonstopmode "
          f"{Path(a.output).name}   # then: optimise --widths-log <that>.log")
    return 0


def cmd_optimise(a: argparse.Namespace) -> int:
    started = time.monotonic()
    _event("OPT_START", "optimizer started", input=str(Path(a.input).resolve()),
           output=str(Path(a.output).resolve()), log=str(Path(a.log_file).resolve()),
           probe=bool(a.probe), allow_opaque=bool(a.allow_opaque),
           llm=not bool(a.no_llm))
    print(f"[1/7] Reading input: {a.input}", file=sys.stderr, flush=True)
    decoded = read_text_auto(a.input)
    src = decoded.text
    src, removed_terminators = canonicalize_document_terminator(src)
    if removed_terminators:
        _event("END_DOCUMENT_NORMALIZED",
               "removed temporary/intermediate document terminators",
               removed=removed_terminators,
               final_terminators=src.count(r"\end{document}"))
    src, local_repairs = normalize_tex(src)
    delimited_controls = local_repairs.get("control_word_boundaries", 0)
    if delimited_controls:
        _event(
            "CONTROL_WORD_BOUNDARY_REPAIRED",
            "delimited zero-argument control words before CJK text",
            repairs=delimited_controls,
        )
    normalized_math_symbols = local_repairs.get("text_math_symbols", 0)
    if normalized_math_symbols:
        _event(
            "TEXT_MATH_NORMALIZED",
            "standalone math symbols made safe in text mode",
            repairs=normalized_math_symbols,
        )
    normalized_multicolumn_breaks = local_repairs.get("multicolumn_linebreaks", 0)
    if normalized_multicolumn_breaks:
        _event(
            "MULTICOLUMN_LINEBREAK_REPAIRED",
            "kept visual line breaks inside paragraph-style multicolumn cells",
            repairs=normalized_multicolumn_breaks,
        )
    normalized_math_blanks = local_repairs.get("math_blank_lines", 0)
    if normalized_math_blanks:
        _event("MATH_BLANK_LINES_REPAIRED",
               "blank lines inside math replaced with comment lines",
               repairs=normalized_math_blanks)
    if local_repairs:
        _event("LOCAL_TEX_REPAIRED", "shared deterministic TeX repairs applied", rules=local_repairs)
    layout_evidence = None
    layout_report = None
    if getattr(a, "page_layout_evidence", None):
        from .page_layout import prepare_layout

        layout_evidence = json.loads(Path(a.page_layout_evidence).read_text("utf-8"))
        src, layout_report = prepare_layout(src, layout_evidence)
        _event("LAYOUT_PREPARED", "source reading orientation and page boundaries applied",
               pages=layout_report["expected_pages"])
    _event("INPUT_READ", "LaTeX input loaded", bytes=len(src.encode("utf-8")),
           lines=src.count("\n") + 1, detected_encoding=decoded.encoding,
           input_bom=decoded.had_bom, output_encoding="utf-8")
    input_issues = validate_latex(src, require_sync_safe=False)
    for issue in input_issues:
        _event("SYNTAX_INPUT", issue.message,
               level="WARNING", original_severity=issue.severity,
               deferred_to_optimizer=True, issue=issue.payload())
    input_errors = [issue for issue in input_issues if issue.severity == "error"]
    if input_errors:
        _event("SYNTAX_INPUT_DEFERRED",
               "input structural errors recorded but not used as a pre-filter",
               level="WARNING", errors=len(input_errors),
               policy="continue_into_optimizer")

    syntax_repair_stats = None
    if (a.llm_syntax_repair or (input_errors and a.llm_repair_on_failure)) and not a.no_llm:
        repair_model = resolve_model("TEXOPT_REPAIR_MODEL", getattr(a, "repair_model", None))
        _event("SYNTAX_REPAIR_START", "LLM syntax repair started",
               model=repair_model,
               cache=str(Path(a.syntax_repair_cache).resolve()))
        repair_started = time.monotonic()
        repairer = LLMSyntaxRepairer(
            Path(a.syntax_repair_cache), model=repair_model,
            timeout=a.syntax_repair_timeout,
            batch_pages=a.syntax_repair_batch_pages, event=_event,
        )
        repair_input = src
        src, repair_stats = repairer.repair_document(src, target_pages=(
            page_numbers_for_lines(src, [issue.line for issue in input_errors])
            if input_errors and not a.llm_syntax_repair else None))
        syntax_repair_stats = vars(repair_stats)
        repaired_issues = validate_latex(src, require_sync_safe=False)
        repaired_errors = [i for i in repaired_issues if i.severity == "error"]
        targeted_retry_stats = None
        targeted_attempt = 0
        while (repaired_errors and not repair_stats.failed
               and targeted_attempt < a.repair_on_failure_attempts):
            targeted_attempt += 1
            retry_pages = page_numbers_for_lines(
                src, [issue.line for issue in repaired_errors]
            )
            diagnostic_hints = page_diagnostic_hints(src, repaired_errors)
            _event(
                "SYNTAX_REPAIR_TARGETED_RETRY",
                "retrying only pages that still contain structural errors",
                level="WARNING", attempt=targeted_attempt,
                max_attempts=a.repair_on_failure_attempts,
                pages=sorted(retry_pages),
                errors=[i.payload() for i in repaired_errors],
            )
            previous_src = src
            previous_error_signature = [
                (issue.code, issue.message) for issue in repaired_errors
            ]
            candidate, targeted_stats = repairer.repair_document(
                src, target_pages=retry_pages,
                diagnostic_hints=diagnostic_hints,
                bypass_cache=True,
            )
            targeted_retry_stats = vars(targeted_stats)
            src = candidate
            repaired_issues = validate_latex(src, require_sync_safe=False)
            repaired_errors = [
                i for i in repaired_issues if i.severity == "error"
            ]
            repair_stats.failed += targeted_stats.failed
            current_error_signature = [
                (issue.code, issue.message) for issue in repaired_errors
            ]
            if src == previous_src or current_error_signature == previous_error_signature:
                _event(
                    "SYNTAX_REPAIR_TARGETED_STALLED",
                    "targeted syntax repair made no structural progress",
                    level="WARNING", attempt=targeted_attempt,
                    source_changed=src != previous_src,
                    remaining_structural_errors=[
                        issue.payload() for issue in repaired_errors
                    ],
                )
                break
        _event("SYNTAX_REPAIR_FINISH", "LLM syntax repair finished",
               level="WARNING" if repaired_errors or repair_stats.failed else "INFO",
               seconds=round(time.monotonic() - repair_started, 3),
               stats=syntax_repair_stats,
               targeted_retry_stats=targeted_retry_stats,
               remaining_structural_errors=[i.payload() for i in repaired_errors])
        if repaired_errors:
            _event(
                "SYNTAX_REPAIR_BLOCKED",
                "structural errors remain after targeted syntax repair",
                level="ERROR", errors=[i.payload() for i in repaired_errors],
                policy="stop_before_optimizer_and_retry_later",
            )
            print(
                "ERROR: structural errors remain after targeted syntax repair; "
                "stopping before table conversion.",
                file=sys.stderr,
            )
            return 6
        # This is an internal stage, not a deliverable. Persist it only when the
        # caller explicitly requests a debugging/audit snapshot.
        if a.syntax_repair_output:
            snapshot_path = Path(a.syntax_repair_output)
            write_utf8_atomic(snapshot_path, src)
            _event("SYNTAX_REPAIR_OUTPUT", "model-repaired LaTeX snapshot written",
                   path=str(snapshot_path.resolve()), bytes=len(src.encode("utf-8")),
                   audit_only=True)
        if repair_stats.failed:
            integrity_violations = repair_invariant_violations(repair_input, src)
            if not integrity_violations:
                _event(
                    "SYNTAX_REPAIR_DEGRADED_SAFE",
                    "continuing after failed syntax-repair batches because the "
                    "current source passed structural and integrity validation",
                    level="WARNING", failed_batches=repair_stats.failed,
                    remaining_structural_errors=0,
                    integrity_violations=[],
                    policy="continue_with_validated_current_source",
                )
            else:
                _event(
                    "SYNTAX_REPAIR_BLOCKED",
                    "failed syntax-repair batches left output integrity unproven",
                    level="ERROR", failed_batches=repair_stats.failed,
                    integrity_violations=integrity_violations,
                    policy="stop_before_probe_and_retry_later",
                )
                hint = "SYNTAX_REPAIR_INTEGRITY_FAILED"
                print(
                    "ERROR: LLM syntax repair did not complete and the current "
                    "source failed integrity validation; see the optimizer log "
                    f"for {hint} details.",
                    file=sys.stderr,
                )
                return 5
    elif a.llm_syntax_repair:
        _event("SYNTAX_REPAIR_SKIPPED",
               "LLM syntax repair requested but --no-llm is active",
               level="WARNING")

    # Lexoid-generated checkbox fields need their rendering macro during width-probe
    # compilation. Inject now; the later pre-annotation call is intentionally
    # idempotent and keeps registry line numbers correct.
    if not a.no_preamble:
        src = preamble.inject(src)

    print("[2/7] Auditing tables and format risks…", file=sys.stderr, flush=True)
    conv_report = None
    format_risks = _risks(src)
    tables = opaque.find_opaque(src)
    print(f"  Found {len(tables)} opaque table(s), {len(format_risks)} format risk(s)",
          file=sys.stderr, flush=True)
    _event("TABLE_AUDIT", "table audit completed", opaque_tables=len(tables),
           format_risks=len(format_risks))
    for table in tables:
        _event("TABLE_FOUND", "SyncTeX-opaque table detected",
               table_id=f"t{table.tid:04d}", environment=table.env,
               source_line=table.line, flex_columns=table.flex_cols,
               width_strategy=("static" if opaque.static_widths(table) is not None
                               else "probe"), notes=table.notes)
    if tables:
        widths = {}
        if a.widths_log:
            widths = opaque.parse_widths(
                Path(a.widths_log).read_text("utf-8", errors="replace"))

        # Convert every table whose width is already proven before launching the
        # measuring compile.  Leaving closed-form tabularx bodies in the probe makes
        # malformed alignment rows replay several times and can exhaust TeX's error
        # limit before a later content-dependent table is reached.
        src, conv_report = opaque.convert(src, widths, strict=False)
        if conv_report.converted:
            _event(
                "PREPROBE_CONVERT",
                "converted already-solvable opaque tables before width probing",
                converted=len(conv_report.converted),
                width_sources=conv_report.by_source,
            )
        # These skips are provisional: the measuring pass below is specifically
        # intended to resolve them, so they must not leak into the final report.
        conv_report.skipped.clear()
        # Table ids are positional.  Once earlier tables are converted, stale keys
        # must never be reused for the re-indexed unresolved set.
        widths = {}
        tables = opaque.find_opaque(src)

        # Which tables cannot be solved in closed form? Only those need a compile.
        need_probe = [t for t in tables
                      if opaque.static_widths(t) is None
                      and not all((t.tid, c) in widths for c in t.flex_cols)]
        if need_probe and a.probe:
            print(f"[3/7] Probing column widths: {len(need_probe)} table(s) need "
                  f"measuring; running {a.probe_engine} probe compile...",
                  file=sys.stderr, flush=True)
            _event("PROBE_START", "XeLaTeX width probe started",
                   tables=[f"t{t.tid:04d}" for t in need_probe],
                   engine=a.probe_engine,
                   probe_dir=str(Path(a.probe_dir or ".texopt-probe").resolve()))
            probe_started = time.monotonic()
            measured, log = opaque.run_probe(src, Path(a.probe_dir or ".texopt-probe"),
                                             engine=a.probe_engine,
                                             source_dir=Path(a.input).resolve().parent)
            diagnostics = _latex_diagnostics(log)
            probe_level = ("ERROR" if not measured or diagnostics["error_count"]
                           else "WARNING" if diagnostics["warning_count"] else "INFO")
            _event("PROBE_RESULT", "XeLaTeX width probe finished",
                   level=probe_level,
                   seconds=round(time.monotonic() - probe_started, 3),
                   measured_columns=len(measured), diagnostics=diagnostics)
            for (table_id, column), width in sorted(measured.items()):
                _event("PROBE_WIDTH", "measured column width",
                       table_id=f"t{table_id:04d}", column=column, width=width)
            if not measured:
                print("  probe produced no widths -- check "
                      f"{a.probe_dir or '.texopt-probe'}/texopt_probe.log", file=sys.stderr)
            else:
                print(f"  Probe measured {len(measured)} column width(s)",
                      file=sys.stderr, flush=True)
            widths.update(measured)
        else:
            static_count = len(tables) - len(need_probe)
            print(f"[3/7] Column widths: {static_count} table(s) solved statically, "
                  f"probe skipped", file=sys.stderr, flush=True)
            _event("WIDTH_PLAN", "probe skipped", static_tables=static_count,
                   unresolved_tables=len(need_probe), probe_enabled=bool(a.probe))

        try:
            print("[4/7] Converting opaque tables…", file=sys.stderr, flush=True)
            src, final_conv_report = opaque.convert(
                src, widths, strict=not a.allow_opaque
            )
            conv_report.extend(final_conv_report)
            if conv_report:
                print(f"  Converted {len(conv_report.converted)} table(s)",
                      file=sys.stderr, flush=True)
                for detail in conv_report.details:
                    _event("TABLE_CONVERTED", "opaque table converted", detail=detail)
        except opaque.UnconvertibleTable as e:
            # Width probing can be blocked by malformed LaTeX before the target
            # table is reached.  In production, give the conservative syntax
            # repairer one chance, validate its patch, then measure and convert
            # again.  The model repairs structure only; XeLaTeX remains the source
            # of truth for every content-dependent width.
            can_retry = a.llm_repair_on_failure and not a.no_llm
            _event("CONVERT_BLOCKED", "opaque table conversion blocked",
                   level="WARNING" if can_retry else "ERROR", reason=str(e),
                   model_retry=can_retry)
            if not can_retry:
                print(f"ERROR: {e}", file=sys.stderr)
                return 3

            current_error: opaque.UnconvertibleTable = e
            current_log = log if "log" in locals() else ""
            current_probe = (
                Path(a.probe_dir or ".texopt-probe") / "texopt_probe.tex"
            )
            # Large OCR documents can contain several independent malformed pages.
            # Each successful repair lets XeLaTeX progress to the next one, so keep
            # the loop bounded but high enough to uncover a realistic error chain.
            max_probe_retries = a.repair_on_failure_attempts
            previous_reason = str(current_error)
            previous_measured = len(widths)
            stagnant_reprobes = 0
            forced_target_pages: set[int] | None = None
            for retry_number in range(1, max_probe_retries + 1):
                _event(
                    "MODEL_REPAIR_RETRY_START",
                    "sending the latest blocked probe page to syntax repair model",
                    attempt=retry_number, max_attempts=max_probe_retries,
                )
                probe_pages = (forced_target_pages or
                               _probe_error_pages(current_log, current_probe))
                targeting_blocker = forced_target_pages is not None
                forced_target_pages = None
                if not probe_pages:
                    print(f"ERROR: could not locate the probe error page: "
                          f"{current_error}", file=sys.stderr)
                    return 3
                probe_diagnostics = _latex_diagnostics(current_log)
                probe_excerpt = (
                    _source_excerpt(src, _conversion_error_line(current_error))
                    if targeting_blocker else
                    _probe_error_excerpt(current_log, current_probe)
                )
                probe_hint = (
                    "The width-probe compile failed on this physical page before "
                    "later tables could be measured. Audit every table row, math "
                    "delimiter, comment marker, and brace; repair the first real "
                    "defect. Probe errors: "
                    + "; ".join(probe_diagnostics["errors"])
                    + f". Conversion symptom: {current_error}"
                    + (". This pass targets the first table whose width is still "
                       "missing, because the preceding diagnostic did not advance. "
                       if targeting_blocker else ". The first error is usually ")
                    + "caused by a defect shortly BEFORE the reported end-of-table "
                    "line. Probe source context:\n"
                    + probe_excerpt
                )
                _event(
                    "MODEL_REPAIR_PROBE_TARGET",
                    "identified physical pages containing the first probe error",
                    pages=sorted(probe_pages),
                    probe_tex=str(current_probe.resolve()),
                    attempt=retry_number,
                )
                repairer = LLMSyntaxRepairer(
                    Path(a.syntax_repair_cache), model=getattr(a, "repair_model", None),
                    timeout=a.syntax_repair_timeout,
                    batch_pages=a.syntax_repair_batch_pages, event=_event,
                )
                repaired, retry_stats = repairer.repair_document(
                    src, target_pages=probe_pages,
                    diagnostic_hint=probe_hint,
                    bypass_cache=True,
                )
                repaired, _ = canonicalize_document_terminator(repaired)
                retry_issues = validate_latex(repaired, require_sync_safe=False)
                retry_errors = [
                    issue for issue in retry_issues if issue.severity == "error"
                ]
                _event(
                    "MODEL_REPAIR_RECHECK",
                    "model repair was structurally rechecked",
                    level=("ERROR" if retry_errors or retry_stats.failed else "INFO"),
                    changed=repaired != src, stats=vars(retry_stats),
                    errors=[issue.payload() for issue in retry_errors],
                    attempt=retry_number,
                )
                syntax_repair_stats = vars(retry_stats)
                if retry_stats.failed or retry_errors:
                    print(f"ERROR: model retry could not repair probe input: "
                          f"{current_error}", file=sys.stderr)
                    return 3
                if repaired == src:
                    _event(
                        "MODEL_REPAIR_STALLED",
                        "targeted model repair made no source change; stopping "
                        "without duplicate calls",
                        level="ERROR", attempt=retry_number,
                        pages=sorted(probe_pages), reason=str(current_error),
                    )
                    print(f"ERROR: targeted model repair made no progress: "
                          f"{current_error}", file=sys.stderr)
                    return 3

                src = repaired
                tables = opaque.find_opaque(src)
                jobname = f"texopt_probe_retry_{retry_number}"
                measured, current_log = opaque.run_probe(
                    src, Path(a.probe_dir or ".texopt-probe"),
                    engine=a.probe_engine,
                    source_dir=Path(a.input).resolve().parent,
                    jobname=jobname,
                )
                current_probe = (
                    Path(a.probe_dir or ".texopt-probe") / f"{jobname}.tex"
                )
                _event(
                    "MODEL_REPAIR_REPROBE",
                    "repaired LaTeX was probed again",
                    measured_columns=len(measured),
                    diagnostics=_latex_diagnostics(current_log),
                    attempt=retry_number,
                )
                try:
                    src, conv_report = opaque.convert(
                        src, measured, strict=not a.allow_opaque
                    )
                except opaque.UnconvertibleTable as retry_error:
                    current_error = retry_error
                    reason = str(retry_error)
                    measured_count = len(measured)
                    if reason == previous_reason and measured_count <= previous_measured:
                        stagnant_reprobes += 1
                    else:
                        stagnant_reprobes = 0
                    previous_reason = reason
                    previous_measured = measured_count
                    _event(
                        "CONVERT_RETRY_BLOCKED",
                        "conversion still blocked after model repair and re-probe",
                        level=("ERROR" if retry_number == max_probe_retries
                               else "WARNING"),
                        reason=str(retry_error), attempt=retry_number,
                        max_attempts=max_probe_retries,
                    )
                    if stagnant_reprobes >= 2:
                        _event(
                            "MODEL_REPAIR_STALLED",
                            "two changed repairs produced the same conversion gap "
                            "without measuring more columns",
                            level="ERROR", attempt=retry_number,
                            measured_columns=measured_count, reason=reason,
                        )
                        print(f"ERROR: model repairs are not advancing the probe: "
                              f"{retry_error}", file=sys.stderr)
                        return 3
                    if stagnant_reprobes == 1:
                        blocker_line = _conversion_error_line(retry_error)
                        blocker_pages = (page_numbers_for_lines(src, [blocker_line])
                                         if blocker_line else set())
                        if blocker_pages:
                            forced_target_pages = blocker_pages
                            _event(
                                "MODEL_REPAIR_TARGET_SWITCH",
                                "probe did not advance; switching the next repair "
                                "to the first unmeasured table",
                                level="WARNING", pages=sorted(blocker_pages),
                                source_line=blocker_line,
                            )
                    continue
                _event(
                    "CONVERT_RETRY_OK",
                    "conversion succeeded after model repair and re-probe",
                    converted=len(conv_report.converted), attempt=retry_number,
                )
                break
            else:
                print(f"ERROR: {current_error}", file=sys.stderr)
                return 3
    else:
        print("[3/7] No opaque tables — all tables are SyncTeX-safe",
              file=sys.stderr, flush=True)
        print("[4/7] Skipping conversion (nothing to convert)",
              file=sys.stderr, flush=True)

    remaining = opaque.find_opaque(src)
    if remaining:
        for table in remaining:
            _event("OPAQUE_REMAINING", "table is still not SyncTeX-safe",
                   level="WARNING", table_id=f"t{table.tid:04d}",
                   environment=table.env, source_line=table.line, notes=table.notes)
    else:
        _event("OPAQUE_CLEAR", "no SyncTeX-opaque table remains")
        src, removed_packages = opaque.remove_unused_tabularx_package(src)
        if removed_packages:
            _event("TABULARX_PACKAGE_REMOVED",
                   "removed unused tabularx package declaration",
                   declarations=removed_packages)

    src, outline_report = normalize_outline(src)
    _event("OUTLINE_NORMALIZED", "numbered headings exposed in editor outline",
           changed=outline_report["changed"], headings=len(outline_report["headings"]))
    print("[5/7] Transforming and annotating fields…", file=sys.stderr, flush=True)
    stats: list[TableStat] = []
    src = sanitize_handwritten_fields(src)
    step1 = transform_tex(src, anchor=not a.no_anchor, stats=stats)
    print(f"  Transformed {len(stats)} table(s), {sum(s.cells for s in stats)} cell(s)",
          file=sys.stderr, flush=True)

    cfg = DetectorConfig()
    if a.force_columns:
        cfg.force_columns = [int(c) for c in a.force_columns.split(",") if c.strip()]
    if a.detect_pattern:
        cfg.patterns = a.detect_pattern

    # ORDER MATTERS: the preamble must be injected BEFORE field annotation, otherwise
    # every tex_line in the registry is off by the size of the macro block and the
    # SyncTeX verification silently checks the wrong lines.
    step1b = preamble.inject(step1) if not a.no_preamble else step1
    if a.no_llm or a.semantic_naming == "deferred":
        namer = DeferredBatchNamer()
    else:
        namer = LLMBatchNamer(Path(a.name_cache), model=a.llm_model)
        if not namer.api_key:
            print("ANTHROPIC_API_KEY / OPENAI_API_KEY not set -- field names will be "
                  "positional (r01c02), not meaningful. Set the key or pass --no-llm "
                  "to silence.", file=sys.stderr)
        else:
            print(f"  LLM naming enabled (model: {a.llm_model})",
                  file=sys.stderr, flush=True)
    naming_requests = []
    out, records, name_stats = annotate_fields(step1b, start_page=a.start_page,
                                               detector=cfg, namer=namer,
                                               naming_requests=naming_requests)
    _event("FIELD_SCAN", "field annotation completed", fields=len(records),
           naming=name_stats)
    for record in records:
        _event("FIELD_READY", "field has a source locator",
               field_id=record.field_id, semantic_alias=record.semantic_alias,
               page=record.page, tex_line=record.tex_line, table=record.table,
               naming_source=record.name_source)

    output_issues = validate_latex(out, require_sync_safe=not a.allow_opaque)
    for issue in output_issues:
        _event("SYNTAX_OUTPUT", issue.message,
               level="ERROR" if issue.severity == "error" else "WARNING",
               issue=issue.payload())
    output_errors = [issue for issue in output_issues if issue.severity == "error"]
    if output_errors:
        _event("SYNTAX_BLOCKED", "optimized LaTeX failed structural validation",
               level="ERROR", errors=len(output_errors))
        return 4
    _event("SYNTAX_OK", "optimized LaTeX structural validation passed",
           warnings=sum(i.severity == "warning" for i in output_issues))

    # Cell-level SyncTeX anchors are useful while transforming and annotating the
    # document.  The exported artifact no longer needs their explicit ``\SA{}``
    # calls, so replace each with one ordinary space as the final cleanup step.
    # Keep ``\SA%`` inside \hwfield itself: that macro still owns field anchoring.
    out, sync_anchors_removed = remove_explicit_sync_anchors(out)
    if sync_anchors_removed:
        _event("SYNC_ANCHORS_REMOVED",
               "explicit cell SyncTeX anchors replaced with spaces",
               count=sync_anchors_removed)

    if layout_evidence is not None:
        out, layout_report = prepare_layout(out, layout_evidence)

    print("[6/7] Writing output…", file=sys.stderr, flush=True)
    write_utf8_atomic(a.output, out)
    _event("OUTPUT_WRITE", "optimized LaTeX written",
           path=str(Path(a.output).resolve()), bytes=len(out.encode("utf-8")),
           lines=out.count("\n") + 1)
    print(f"  Output: {a.output} ({len(records)} fields)",
          file=sys.stderr, flush=True)
    compile_result = None
    if a.compile_check or layout_evidence is not None:
        compile_log = (Path(a.compile_log) if a.compile_log else
                       Path(a.output).with_suffix(".compile.log"))
        _event("COMPILE_CHECK_START",
               "two-pass XeLaTeX compatibility check started",
               engine=a.compile_engine, log=str(compile_log.resolve()))
        compile_logs = []
        profiles = {}
        for attempt in range(3):
            kwargs = {"layout_report": layout_report} if layout_report is not None else {}
            compile_ok, compile_text = _compile_latex(
                Path(a.output), Path(a.input).resolve().parent,
                a.compile_engine, a.compile_timeout, runs=2, **kwargs,
            )
            compile_logs.append(f"LAYOUT ATTEMPT {attempt + 1}\n{compile_text}")
            if compile_ok or layout_report is None or not layout_report.get("page_map") or attempt == 2:
                break
            # Only compact the first overflowing source page. Later mappings may
            # be displaced by that page without having any layout defect themselves.
            bad = next((p["source_page"] for p in layout_report["page_map"]
                        if p.get("start") != p["source_page"] or p.get("end") != p["source_page"]), None)
            if bad is None and layout_report.get("outside_pages"):
                bad = layout_report["outside_pages"][0]
            if bad is None or profiles.get(bad, 0) >= 2:
                break
            profiles[bad] = profiles.get(bad, 0) + 1
            out, layout_report = prepare_layout(out, layout_evidence, profiles)
            write_utf8_atomic(a.output, out)
            _event("LAYOUT_RETRY", "retrying bounded spacing adjustment",
                   page=bad, profile=profiles[bad], attempt=attempt + 2)
        compile_text = "\n".join(compile_logs)
        if layout_report is not None:
            layout_report["attempts"] = attempt + 1
            write_utf8_atomic(Path(a.output).with_suffix(".layout.json"),
                              json.dumps(layout_report, ensure_ascii=False, indent=2))
        write_utf8_atomic(compile_log, compile_text)
        compile_result = {
            "ok": compile_ok, "engine": a.compile_engine, "passes": 2,
            "log": str(compile_log),
        }
        _event("COMPILE_CHECK_FINISH",
               "optimized TeX compiled successfully" if compile_ok else
               "optimized TeX failed the compatibility compile",
               level="INFO" if compile_ok else "ERROR", **compile_result)
        if not compile_ok:
            if a.report:
                write_utf8_atomic(a.report, json.dumps({
                    "compile_check": compile_result, "layout_check": layout_report,
                }, ensure_ascii=False, indent=2))
            print(f"ERROR: optimized TeX failed compilation; see {compile_log}",
                  file=sys.stderr)
            return 6
    registry_result = None
    if a.registry:
        try:
            write_registry(records, Path(a.registry),
                           provenance_path=getattr(a, "source_registry", None), tex=out)
            registry_result = {"ok": True, "status": "complete"}
            _event("REGISTRY_WRITE", "field registry written",
                   path=str(Path(a.registry).resolve()), fields=len(records))
        except Exception as exc:
            # Registry enrichment is optional: its failure must not discard the
            # compiled document or leave a previous registry looking current.
            registry_result = {"ok": False, "status": "degraded",
                              "error_type": type(exc).__name__, "error": str(exc)}
            try:
                retained_fields = basic_fields(records, out)
            except Exception:
                retained_fields = []
            write_utf8_atomic(Path(a.registry), json.dumps(
                {"version": 1, "count": len(retained_fields), "fields": retained_fields, **registry_result},
                ensure_ascii=False, indent=2))
            _event("REGISTRY_DEGRADED", "field JSON unavailable; preserving TeX output",
                   level="WARNING", path=str(Path(a.registry).resolve()), **registry_result)
        print(f"  Registry: {a.registry}", file=sys.stderr, flush=True)

    from .semantic_naming import write_plan, stamp_registry
    naming_plan = Path(a.naming_plan) if a.naming_plan else Path(a.output).with_suffix(".naming.json")
    naming_plan_result = {"status": "pending", "path": str(naming_plan)}
    try:
        plan = write_plan(naming_plan, out, records, naming_requests)
        if a.registry:
            stamp_registry(a.registry, out, evidence_path=a.page_layout_evidence,
                           provenance_path=a.source_registry)
        naming_plan_result["status"] = "ready"
        _event("NAMING_PLAN_READY", "optional semantic naming can run independently of PDF compilation",
               path=str(naming_plan), tables=len(plan["requests"]), mode=a.semantic_naming)
    except Exception as exc:
        naming_plan_result.update(status="degraded", error_type=type(exc).__name__)
        _event("NAMING_PLAN_DEGRADED", "preserving TEX/PDF when optional naming metadata is unavailable",
               level="WARNING", error_type=type(exc).__name__, error=str(exc))

    print("[7/7] Generating report…", file=sys.stderr, flush=True)
    report = {
        "input": str(a.input), "output": str(a.output), "log": str(a.log_file),
        "input_encoding": decoded.encoding, "output_encoding": "utf-8",
        "syntax_repair": syntax_repair_stats,
        "compile_check": compile_result,
        "registry_check": registry_result,
        "layout_check": layout_report,
        "outline": outline_report,
        "syntax_warnings": [i.payload() for i in output_issues
                            if i.severity == "warning"],
        "tables": len(stats),
        "cells": sum(s.cells for s in stats),
        "rows": sum(s.rows for s in stats),
        "fields": len(records),
        "lines_before": src.count("\n") + 1,
        "lines_after": out.count("\n") + 1,
        "field_ids": [r.field_id for r in records],
        "naming": name_stats,
        "semantic_naming_mode": "deferred" if a.no_llm else a.semantic_naming,
        "naming_plan": str(naming_plan),
        "naming_plan_check": naming_plan_result,
        "naming_fallbacks": sorted({r.table for r in records
                                    if r.name_source == "heuristic"}),
        "opaque_converted": conv_report.converted if conv_report else [],
        "opaque_width_source": conv_report.by_source if conv_report else {},
        "opaque_detail": conv_report.details if conv_report else [],
        "format_risks": format_risks,
        "opaque_skipped": conv_report.skipped if conv_report else [],
        "opaque_remaining": [{"line": t.line, "env": t.env, "notes": t.notes}
                             for t in remaining],
        "explicit_sync_anchors_removed": sync_anchors_removed,
    }
    fb = report["naming_fallbacks"]
    if fb and not a.no_llm:
        print(f"  NAMING: {len(fb)} table(s) fell back to positional names "
              f"(model unavailable or response rejected): {fb[:5]}", file=sys.stderr)
    warns = [r for r in format_risks if r["severity"] == "warn"]
    if warns:
        print(f"  FORMAT RISK: {len(warns)} construct(s) inside converted tables behave "
              f"differently under a single-pass body. See report.format_risks; "
              f"confirm with `verify --check-geometry`.", file=sys.stderr)
        for r in warns[:8]:
            print(f"    line {r['line']}: {r['construct']} -- {r['note']}", file=sys.stderr)
    if remaining:
        print(f"  WARNING: {len(remaining)} table(s) still in a SyncTeX-opaque "
              f"environment; their cells cannot be located precisely. "
              f"Run `texopt probe` + compile + `--widths-log` to convert them.",
              file=sys.stderr)
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        _event("REPORT_WRITE", "optimizer report written",
               path=str(Path(a.report).resolve()))
        print(f"  Report: {a.report}", file=sys.stderr, flush=True)
    if a.diff:
        Path(a.diff).write_text("\n".join(difflib.unified_diff(
            src.splitlines(), out.splitlines(),
            fromfile="before", tofile="after", lineterm="")), "utf-8")
        print(f"  Diff: {a.diff}", file=sys.stderr, flush=True)

    print("✓ Optimization complete", file=sys.stderr, flush=True)
    _event("OPT_FINISH", "optimizer completed",
           level="WARNING" if remaining else "INFO",
           exit_code=2 if remaining else 0,
           seconds=round(time.monotonic() - started, 3), fields=len(records),
           converted_tables=len(conv_report.converted) if conv_report else 0,
           opaque_remaining=len(remaining))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if remaining else 0


def cmd_verify(a: argparse.Namespace) -> int:
    from .checks import (content_regression, geometry_regression,
                         summarize, synctex_roundtrip)

    tex_path = Path(a.tex)
    pdf_path = Path(a.pdf)
    _event("VERIFY_START", "SyncTeX verification started",
           tex=str(tex_path.resolve()), pdf=str(pdf_path.resolve()),
           registry=str(Path(a.registry).resolve()) if a.registry else "")
    if not tex_path.exists() or not pdf_path.exists():
        _event("VERIFY_INPUT_MISSING", "optimized TeX or PDF does not exist",
               level="ERROR")
        print("VERIFY FAIL: optimized TeX or PDF does not exist", file=sys.stderr)
        return 2
    sync_path = pdf_path.with_suffix(".synctex.gz")
    if not sync_path.exists():
        _event("SYNCTEX_MISSING", "SyncTeX file is missing", level="ERROR",
               expected=str(sync_path.resolve()))
        print(f"VERIFY FAIL: missing {sync_path.name}; compile with -synctex=1",
              file=sys.stderr)
        return 2
    remaining = opaque.find_opaque(tex_path.read_text("utf-8", errors="replace"))
    if remaining:
        where = ", ".join(f"{t.env}@line{t.line}" for t in remaining)
        _event("VERIFY_OPAQUE", "opaque tables remain", level="ERROR", tables=where)
        print(f"VERIFY FAIL: SyncTeX-opaque tables remain: {where}", file=sys.stderr)
        return 3

    reg = json.loads(Path(a.registry).read_text("utf-8")) if a.registry else {"fields": []}
    lines = [f["tex_line"] for f in reg["fields"]] or list(range(1, 2))
    res = synctex_roundtrip(tex_path, pdf_path, lines)
    print(summarize(res))
    for item in res:
        _event("SYNCTEX_FIELD", "field round-trip checked",
               level="INFO" if item.ok else "ERROR", tex_line=item.line,
               reverse_line=item.got_line, pdf_page=item.page, ok=item.ok,
               note=item.note)

    if a.baseline_pdf:
        ok, rep = content_regression(Path(a.baseline_pdf), Path(a.pdf))
        print(("CONTENT OK: " if ok else "CONTENT FAIL: ") + rep)
        if not ok:
            return 2
        if a.check_geometry:
            ok2, rep2 = geometry_regression(Path(a.baseline_pdf), Path(a.pdf),
                                            tol_pt=a.geometry_tol)
            print(("GEOMETRY OK: " if ok2 else "GEOMETRY FAIL: ") + rep2)
            if not ok2:
                return 2
    ok = all(r.ok for r in res)
    _event("VERIFY_FINISH", "SyncTeX verification completed",
           level="INFO" if ok else "ERROR", checked=len(res),
           passed=sum(1 for r in res if r.ok), exit_code=0 if ok else 1)
    return 0 if ok else 1


def cmd_reconcile(a) -> int:
    from .reconcile import FieldReconcileAdapter, reconcile_document
    report = reconcile_document(Path(a.input), Path(a.source_pdf), Path(a.recognition_evidence),
        Path(a.output), Path(a.fields), adapter=FieldReconcileAdapter(a.model),
        concurrency=a.concurrency, retry_dpi=a.retry_dpi, review_content=a.review_content)
    _event("RECONCILE_FINISH", "field reconciliation completed", selected=report.selected,
           confirmed=report.confirmed, failed=report.failed, deferred=report.deferred,
           needs_review=sum(field["needs_review"] for field in report.fields), errors=report.errors)
    return 0


def cmd_name_fields(a):
    from .semantic_naming import enrich

    try:
        result = enrich(a)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        _event("NAMING_FAILED", "optional JSON naming failed", level="WARNING",
               error_type=type(exc).__name__, error=str(exc))
        return 2
    _event("NAMING_FINISH", "optional JSON naming finished", **result)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="texopt")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("reconcile")
    r.add_argument("input")
    r.add_argument("--source-pdf", required=True)
    r.add_argument("--recognition-evidence", required=True)
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--fields", required=True)
    r.add_argument("--model", help="field review model (environment: RECONCILE_MODEL)")
    r.add_argument("--retry-dpi", type=int, default=480)
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--review-content", action="store_true",
                   help="also review uncertain text/OCR/checkbox content; default: format errors only")
    r.set_defaults(func=cmd_reconcile)

    o = sub.add_parser("optimise")
    o.add_argument("input")
    o.add_argument("-o", "--output",
                   help="complete output path (alternative to --output-dir/name)")
    o.add_argument("--output-dir",
                   help="directory in which to export the optimized LaTeX")
    o.add_argument("--output-name",
                   help="export filename, e.g. invoice.optimized.tex")
    o.add_argument("--choose-output", action="store_true",
                   help="open a native Save As dialog to choose folder and filename")
    o.add_argument("--start-page", type=int, default=1)
    o.add_argument("--registry")
    o.add_argument("--source-registry", help="reconciled field registry whose provenance must be preserved")
    o.add_argument("--page-layout-evidence",
                   help="recognition JSON; preserve page orientation and require actual PDF page mapping")
    o.add_argument("--report")
    o.add_argument("--diff")
    o.add_argument("--log-file",
                   help="persistent detailed log (default: <output>.texopt.log)")
    o.add_argument("--no-anchor", action="store_true")
    o.add_argument("--no-preamble", action="store_true")
    o.add_argument("--force-columns", default="")
    o.add_argument("--detect-pattern", action="append")
    o.add_argument("--no-llm", action="store_true",
                   help="skip the model; names become positional (r01c02)")
    o.add_argument("--semantic-naming", choices=("deferred", "inline"),
                   default=os.getenv("TEXOPT_SEMANTIC_NAMING", "deferred"),
                   help="defer optional semantic aliases (default), independently of syntax repair")
    o.add_argument("--naming-plan", help="optional naming sidecar; default: <output>.naming.json")
    o.add_argument("--llm-model", help="semantic naming model (environment: TEXOPT_MODEL)")
    o.add_argument("--repair-model", help="syntax repair model (environment: TEXOPT_REPAIR_MODEL)")
    o.add_argument("--name-cache", default=os.getenv("TEXOPT_NAME_CACHE", ".texopt-names.sqlite3"),
                   help="shared SQLite semantic cache; legacy JSON caches remain untouched")
    o.add_argument("--llm-syntax-repair", action="store_true",
                   help="repair LaTeX syntax in Lexoid page batches with the LLM")
    o.add_argument("--llm-repair-on-failure", action="store_true",
                   help="if probing/conversion is blocked, ask the syntax model for "
                        "bounded minimal repairs, validating and retrying each pass")
    o.add_argument("--repair-on-failure-attempts", type=int, default=6,
                   help="maximum targeted probe-repair passes (default: 6; "
                        "unchanged or stalled repairs stop earlier)")
    o.add_argument("--syntax-repair-cache", default=".texopt-syntax-repairs.json",
                   help="persistent cache for accepted LLM syntax repairs")
    o.add_argument("--syntax-repair-timeout", type=int, default=120,
                   help="seconds per syntax-repair batch request")
    o.add_argument("--syntax-repair-batch-pages", type=int, default=5,
                   help="consecutive physical pages per LLM repair call (default: 5)")
    o.add_argument("--syntax-repair-output",
                   help="optionally save the intermediate model-repaired TeX for "
                        "debugging; omitted by default")
    o.add_argument("--widths-log", help="log of an instrumented compile (optional; "
                                        "the probe is run automatically when needed)")
    o.add_argument("--no-probe", dest="probe", action="store_false",
                   help="do not run a probe compile; closed-form tables still convert")
    o.add_argument("--probe-engine", default="xelatex")
    o.add_argument("--probe-dir", default=".texopt-probe")
    o.add_argument("--compile-check", action="store_true",
                   help="compile the exported document twice with XeLaTeX and fail "
                        "the job if either pass fails")
    o.add_argument("--compile-engine", default="xelatex")
    o.add_argument("--compile-timeout", type=int, default=300,
                   help="seconds allowed for each final compilation pass")
    o.add_argument("--compile-log",
                   help="final compilation log; default: <output>.compile.log")
    o.add_argument("--allow-opaque", action="store_true",
                   help="escape hatch: leave unconvertible tables in place instead of "
                        "failing (their cells will NOT be locatable)")
    o.set_defaults(func=cmd_optimise)

    n = sub.add_parser("name-fields", help="enrich JSON aliases without modifying or compiling TEX")
    n.add_argument("tex")
    n.add_argument("--registry", required=True)
    n.add_argument("-o", "--output", required=True)
    n.add_argument("--plan", help="default: <tex>.naming.json")
    n.add_argument("--model", help="semantic naming model (environment: TEXOPT_MODEL)")
    n.add_argument("--name-cache", default=os.getenv("TEXOPT_NAME_CACHE", ".texopt-names.sqlite3"))
    n.set_defaults(func=cmd_name_fields)

    au = sub.add_parser("audit")
    au.add_argument("input")
    au.set_defaults(func=cmd_audit)

    pr = sub.add_parser("probe")
    pr.add_argument("input")
    pr.add_argument("-o", "--output", required=True)
    pr.set_defaults(func=cmd_probe)

    v = sub.add_parser("verify")
    v.add_argument("tex")
    v.add_argument("pdf")
    v.add_argument("--registry")
    v.add_argument("--baseline-pdf")
    v.add_argument("--check-geometry", action="store_true")
    v.add_argument("--geometry-tol", type=float, default=0.05,
                   help="pt; tight by default because the conversion is "
                        "supposed to reproduce the layout exactly")
    v.add_argument("--log-file",
                   help="persistent verification log (default: <tex>.verify.log)")
    v.set_defaults(func=cmd_verify)

    a = p.parse_args(argv)
    if getattr(a, "cmd", None) == "optimise":
        explicit = bool(a.output or a.output_dir or a.output_name)
        if a.choose_output and explicit:
            p.error("--choose-output cannot be combined with other output options")
        if a.output and (a.output_dir or a.output_name):
            p.error("use either --output or --output-dir/--output-name, not both")
        if a.choose_output:
            try:
                a.output = _choose_output_path(a.input)
            except RuntimeError as exc:
                p.error(str(exc))
            if not a.output:
                p.error("output selection was cancelled")
        elif not a.output:
            source = Path(a.input)
            export_dir = Path(a.output_dir) if a.output_dir else source.parent
            export_name = a.output_name or f"{source.stem}.opt.tex"
            if Path(export_name).name != export_name:
                p.error("--output-name must be a filename; use --output-dir for its directory")
            a.output = str(export_dir / export_name)
        if not a.log_file:
            a.log_file = str(Path(a.output).with_suffix(".texopt.log"))
    try:
        if a.cmd == "optimise" and not a.no_llm:
            if a.semantic_naming == "inline":
                a.llm_model = resolve_model("TEXOPT_MODEL", a.llm_model)
            if a.llm_syntax_repair or a.llm_repair_on_failure:
                a.repair_model = resolve_model("TEXOPT_REPAIR_MODEL", a.repair_model)
        elif a.cmd == "reconcile":
            a.model = resolve_model("RECONCILE_MODEL", a.model)
        elif a.cmd == "name-fields":
            a.model = resolve_model("TEXOPT_MODEL", a.model)
    except ValueError as exc:
        p.error(str(exc))
    if getattr(a, "cmd", None) == "verify" and not a.log_file:
        a.log_file = str(Path(a.tex).with_suffix(".verify.log"))
    if getattr(a, "cmd", None) in ("optimise", "verify"):
        log_path = Path(a.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log_stream:
            with contextlib.redirect_stderr(_Tee(sys.stderr, log_stream)):
                return a.func(a)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
