"""Compile and accept an exported document, including bounded layout recovery."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ..core.textio import write_utf8_atomic
from .page_layout import prepare_layout


@dataclass(frozen=True)
class CompilationResult:
    ok: bool
    log: str
    layout: dict | None = None


@dataclass(frozen=True)
class DocumentCompilation:
    source: str
    ok: bool
    log: str
    layout: dict | None
    attempts: int


def _run_compiler(tex_path: Path, source_dir: Path, engine: str,
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
                # Extra pages are allowed, but a successful TeX process must
                # not hide clipped text in the exported PDF.
                return pdf.stat().st_size > 0 and layout_report['export_ok'], "\n".join(chunks)
            return pdf.exists() and pdf.stat().st_size > 0, "\n".join(chunks)
    except (OSError, subprocess.TimeoutExpired) as exc:
        chunks.append(f"compile exception: {type(exc).__name__}: {exc}\n")
        return False, "\n".join(chunks)


def compile_tex(tex_path: Path, source_dir: Path, engine: str = "xelatex",
                timeout: int = 120, runs: int = 2, *, layout_report=None) -> CompilationResult:
    """Compile one attempt without mutating the caller's layout report."""
    report = deepcopy(layout_report)
    ok, log = _run_compiler(tex_path, source_dir, engine, timeout, runs,
                            layout_report=report)
    return CompilationResult(ok, log, report)


def compile_document(source: str, output: Path, source_dir: Path, *,
                     evidence=None, engine="xelatex", timeout=120,
                     event=None, compiler=None) -> DocumentCompilation:
    """Prepare, compile, inspect and accept a document in at most three attempts.

    Extra output pages are permitted. All clipped source pages advance together;
    exhausted retries are a failed acceptance even if XeLaTeX exited zero.
    The compiler seam also supports deterministic failure/recovery tests.
    """
    compiler = compiler or compile_tex
    event = event or (lambda *args, **kwargs: None)
    output = Path(output)
    profiles = {}
    report = None
    if evidence is not None:
        source, report = prepare_layout(source, evidence)
    logs = []
    for attempt in range(1, 4):
        write_utf8_atomic(output, source)
        result = compiler(output, source_dir, engine, timeout, runs=2, layout_report=report)
        report = result.layout
        logs.append(f"LAYOUT ATTEMPT {attempt}\n{result.log}")
        if result.ok or report is None or not report.get("page_map") or attempt == 3:
            break
        bad = {p["source_page"] for p in report["page_map"]
               if any(p.get("start", 0) <= n <= p.get("end", 0)
                      for n in report.get("outside_pages", []))}
        adjustable = sorted(p for p in bad if profiles.get(p, 0) < 2)
        if not adjustable:
            break
        for page in adjustable:
            profiles[page] = profiles.get(page, 0) + 1
        source, report = prepare_layout(source, evidence, profiles)
        event("LAYOUT_RETRY", "retrying bounded spacing adjustment",
              pages=adjustable, profiles={p: profiles[p] for p in adjustable},
              attempt=attempt + 1)
    if report is not None:
        report["attempts"] = attempt
        write_utf8_atomic(output.with_suffix(".layout.json"),
                          json.dumps(report, ensure_ascii=False, indent=2))
    return DocumentCompilation(source, result.ok, "\n".join(logs), report, attempt)
