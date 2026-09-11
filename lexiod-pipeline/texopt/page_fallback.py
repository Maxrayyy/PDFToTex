"""Local page checks and one durable, image-grounded model escalation per page."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .model_telemetry import emit
from .local_tex import VERSION as LOCAL_TEX_VERSION, normalize_tex
from .syntax_check import SyntaxIssue, _alignment_colspec, validate_latex
from .syntax_repair import canonicalize_document_terminator, split_lexoid_pages
from .tex_tables import (_peel_prefix, iter_structural, mask_comments,
                         multicolumn_span, parse_colspec, split_align_body)
from .textio import write_utf8_atomic


CHECK_VERSION = "page-quality-v2-" + LOCAL_TEX_VERSION


def structural_issues(tex):
    issues = [issue for issue in validate_latex(tex) if issue.severity == "error"]
    if "% LEXOID_RECOGNITION_FALLBACK" in tex:
        issues.append(SyntaxIssue("error", "MISSING_VISION_TEX", 1, "No usable vision response"))

    def scan(segment, base=0):
        tokens = iter(iter_structural(mask_comments(segment)))
        for begin in tokens:
            if begin.kind != "align_begin":
                continue
            end = next((t for t in tokens if t.kind == "align_end"), None)
            if end is None:
                continue
            body = segment[begin.body_start:end.start]
            expected = len(parse_colspec(_alignment_colspec(
                segment[begin.start:begin.body_start], begin.name)))
            run, offset = [], base + begin.body_start
            for row in split_align_body(body):
                prefix, first = _peel_prefix(row.cells[0].text)
                cells = [first, *(cell.text for cell in row.cells[1:])]
                raw_row = "".join(cell.text + cell.sep for cell in row.cells)
                meaningful = any(mask_comments(cell).strip() for cell in cells)
                count = sum(multicolumn_span(cell) for cell in cells)
                if prefix.strip() or not meaningful or count >= expected:
                    run = []
                if meaningful and 1 <= count < expected and expected >= 3:
                    run.append((count, raw_row, offset))
                    # An ungrouped multi-line field becomes several short physical
                    # rows. A single short row or nested cell line break is advisory.
                    if (len(run) >= 2 and any(n == 1 and r"\fieldvalue" in text for n, text, _ in run)
                            and not any(r"\multirow" in text or r"\multicolumn" in text
                                        for _, text, _ in run)):
                        issues.append(SyntaxIssue("error", "SPLIT_FIELD_ROW",
                            tex.count("\n", 0, run[0][2]) + 1,
                            "Consecutive short table rows split an ungrouped field into the first column"))
                        run = []
                offset += len(raw_row)
            scan(body, base + begin.body_start)

    scan(tex)
    return issues


def standalone_page(chunk, preamble):
    if r"\begin{document}" not in mask_comments(chunk):
        chunk = preamble + chunk
    return canonicalize_document_terminator(chunk)[0]


def compile_page(tex, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compile-", dir=directory) as temporary:
        path = Path(temporary) / "page.tex"
        write_utf8_atomic(path, tex)
        try:
            result = subprocess.run(["xelatex", "-no-shell-escape", "-interaction=nonstopmode",
                "-halt-on-error", "-file-line-error", "page.tex"], cwd=temporary,
                capture_output=True, text=True, errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [SyntaxIssue("warning", "COMPILE_CHECK_UNAVAILABLE", 1, type(exc).__name__)]
        write_utf8_atomic(directory / "compile.log", result.stdout + result.stderr)
        if result.returncode:
            line = re.search(r"page\.tex:(\d+):", result.stdout)
            return [SyntaxIssue("error", "COMPILE_ERROR", int(line[1]) if line else 1,
                                "Page failed local XeLaTeX compilation; see compile.log")]
    return []


def check_page(chunk, preamble, directory, compiler=compile_page):
    tex, _ = normalize_tex(standalone_page(chunk, preamble))
    key = hashlib.sha256((CHECK_VERSION + tex).encode()).hexdigest()
    directory = Path(directory) / key
    path = directory / "check.json"
    if path.exists():
        try:
            return [SyntaxIssue(**issue) for issue in json.loads(path.read_text())]
        except (ValueError, TypeError):
            pass
    issues = structural_issues(tex)
    if not issues:
        issues = compiler(tex, directory)
    if not any(issue.code == "COMPILE_CHECK_UNAVAILABLE" for issue in issues):
        write_utf8_atomic(path, json.dumps([issue.payload() for issue in issues]))
    return issues


def get_preamble(chunk):
    match = re.search(r"\\begin\{document\}", mask_comments(chunk))
    return chunk[:match.end()] + "\n" if match else ""


def check_document(tex, directory, *, compiler=compile_page):
    pages = split_lexoid_pages(tex)
    preamble = get_preamble(pages[0][2])
    result = {}
    for page, _, chunk in pages:
        issues = [i for i in check_page(chunk, preamble, directory, compiler) if i.severity == "error"]
        if issues:
            result[page] = issues
    return result


class PageFallback:
    """Called in page order by Lexoid's independent, bounded checking queue."""

    def __init__(self, source, cache_dir, primary_model, fallback_model, *, render_dpi=240,
                 auto_orient=True, recognize=None, renderer=None, compiler=compile_page):
        from lexoid.core.recognition.models import RecognitionConfig
        from lexoid.core.recognition.rendering import render_pdf_page
        from lexoid.core.recognition.vision import VisionLatexAdapter

        self.source, self.cache = Path(source), Path(cache_dir) / "page-fallback"
        self.primary, self.model = primary_model, fallback_model
        self.dpi, self.auto_orient = render_dpi, auto_orient
        self.renderer, self.compiler = renderer or render_pdf_page, compiler
        adapter = VisionLatexAdapter(fallback_model, config=RecognitionConfig(
            ocr="none", initial_render_dpi=render_dpi, retry_crop_dpi=max(480, render_dpi),
            min_output_tokens=8192))
        self.recognize = recognize or adapter.recognize
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.preamble = ""
        self.report = {"checked": 0, "upgraded": [], "unresolved": [], "local_repaired": [],
                       "page_models": {}}

    def _prepare(self, chunk, number, model):
        normalized, rules = normalize_tex(chunk)
        checked, support = normalize_tex(standalone_page(normalized, self.preamble))
        rules.update(support)
        if rules:
            digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
            key = digest(self.source_hash + str(number) + model + LOCAL_TEX_VERSION + chunk + self.preamble)
            directory = self.cache / "local" / key
            write_utf8_atomic(directory / "original.tex", chunk)
            write_utf8_atomic(directory / "normalized.tex", normalized)
            write_utf8_atomic(directory / "checked.tex", checked)
            write_utf8_atomic(directory / "local-repair.json", json.dumps({
                "page": number, "model": model, "version": LOCAL_TEX_VERSION, "rules": rules,
                "original_sha256": digest(chunk), "normalized_sha256": digest(normalized),
                "checked_sha256": digest(checked),
            }, ensure_ascii=False, indent=2))
            if number not in self.report["local_repaired"]:
                self.report["local_repaired"].append(number)
            emit({"event": "page_local_repair", "stage": "recognize", "page": number,
                  "model": model, "rules": rules, "audit": str(directory / "local-repair.json")})
        return normalized

    def __call__(self, result, total, gate=None):
        from lexoid.core.request_errors import ModelUnavailableError, is_request_failure, is_request_failure_name
        from lexoid.core.model_telemetry import call_context
        from lexoid.core.recognition.models import FieldEvidence, VisionPageResult
        from lexoid.core.recognition.vision import RecoverableVisionError, validate_page_checkpoint

        number, original = result.page, result.latex
        if number == 1:
            self.preamble = get_preamble(original)
        normalized = self._prepare(original, number, self.primary)
        result = replace(result, latex=normalized)
        if number == 1:
            self.preamble = get_preamble(normalized)
        started = time.monotonic()
        issues = check_page(normalized, self.preamble, self.cache / "checks", self.compiler)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.report["checked"] += 1
        self.report["page_models"][str(number)] = self.primary
        emit({"event": "page_check", "stage": "recognize", "page": number, "total": total,
              "seconds": time.monotonic() - started, "codes": [i.code for i in issues],
              "action": "upgrade" if errors else "keep_tex"})
        if not errors or self.model == self.primary:
            return result

        from lexoid.core.recognition.vision import PROMPT_VERSION
        key_data = [self.source_hash, number, total, original, self.primary, self.model,
                    self.dpi, self.auto_orient, PROMPT_VERSION]
        key = hashlib.sha256(json.dumps(key_data).encode()).hexdigest()
        directory = self.cache / "attempts" / key
        record_path = directory / "attempt.json"
        record = None
        if record_path.exists():
            record = json.loads(record_path.read_text())
            if record.get("status") == "failed" and (record.get("retryable_service_error")
                    or is_request_failure_name(record.get("error_type", ""))):
                write_utf8_atomic(directory / "previous-request-failure.json", json.dumps(record, indent=2))
                record = None
        if record is None:
            write_utf8_atomic(directory / "primary.tex", original)
            record = {"page": number, "total": total, "primary_model": self.primary,
                      "fallback_model": self.model, "reason": [i.payload() for i in errors],
                      "status": "started"}
            # Reserve the attempt durably before making a paid call, including
            # when an interrupted run is resumed after a provider timeout.
            write_utf8_atomic(record_path, json.dumps(record, ensure_ascii=False, indent=2))
            emit({"event": "page_model_upgrade", "stage": "recognize", "page": number,
                  "from_model": self.primary, "model": self.model,
                  "codes": [i.code for i in errors], "action": "recognize_original_image"})
            try:
                page = self.renderer(self.source, number, self.dpi, self.auto_orient)
                with gate.slot() if gate else nullcontext():
                    with call_context(stage="recognize", page=number, attempt=2,
                                      fallback_from=self.primary):
                        try:
                            candidate = self.recognize(page, replace(result.evidence, fields=()), total)
                        except RecoverableVisionError as exc:
                            if exc.fallback is None:
                                raise
                            candidate = exc.fallback
                validate_page_checkpoint(candidate.latex, number, total)
                record.update(status="returned", latex=candidate.latex,
                              fields=[f.to_dict() for f in candidate.fields])
                write_utf8_atomic(directory / "fallback.tex", candidate.latex)
            except Exception as exc:
                record.update(status="failed", error_type=type(exc).__name__)
                if is_request_failure(exc):
                    record["retryable_service_error"] = True
                    write_utf8_atomic(record_path, json.dumps(record, ensure_ascii=False, indent=2))
                    error = exc if isinstance(exc, ModelUnavailableError) else ModelUnavailableError(number, exc)
                    if gate:
                        gate.stop(error)
                    emit({"event": "model_service_unavailable", "stage": "recognize", "page": number,
                          "error_type": type(exc).__name__, "action": "pause_document"})
                    raise error from None
            write_utf8_atomic(record_path, json.dumps(record, ensure_ascii=False, indent=2))

        if record["status"] == "returned":
            candidate = VisionPageResult(number, record["latex"], tuple(
                FieldEvidence.from_dict(f) for f in record["fields"]))
            candidate = replace(candidate, latex=self._prepare(candidate.latex, number, self.model))
            candidate_issues = check_page(candidate.latex, self.preamble, self.cache / "checks", self.compiler)
            if not any(i.severity == "error" for i in candidate_issues):
                self.report["upgraded"].append(number)
                self.report["page_models"][str(number)] = self.model
                if number == 1:
                    self.preamble = get_preamble(candidate.latex)
                emit({"event": "page_upgrade_finish", "stage": "recognize", "page": number,
                      "model": self.model, "action": "use_fallback_tex"})
                return replace(result, latex=candidate.latex,
                               evidence=replace(result.evidence, fields=candidate.fields))
        self.report["unresolved"].append(number)
        emit({"event": "page_upgrade_finish", "stage": "recognize", "page": number,
              "model": self.model, "action": "keep_original_tex", "status": record["status"]})
        return result


def upgrade_pages(source, raw, evidence_path, cache_dir, primary_model, fallback_model, **kwargs):
    """Replay already recognized pages through the same checker without primary calls."""
    from lexoid.core.recognition.models import PageEvidence, PageRecognitionResult

    payload = json.loads(Path(evidence_path).read_text())
    processor = PageFallback(source, cache_dir, primary_model, fallback_model, **kwargs)
    output, evidence = [], []
    for number, total, chunk in split_lexoid_pages(Path(raw).read_text()):
        page = PageEvidence.from_dict(payload["pages"][number - 1])
        result = processor(PageRecognitionResult(number, chunk, page, "replay"), total)
        output.append(result.latex)
        evidence.append(result.evidence.to_dict())
    # Later pages may introduce packages that belong in the first-page preamble.
    finalized, _ = normalize_tex(canonicalize_document_terminator("".join(output))[0])
    write_utf8_atomic(raw, finalized)
    payload.update(pages=evidence, page_models=processor.report["page_models"])
    write_utf8_atomic(evidence_path, json.dumps(payload, ensure_ascii=False, indent=2))
    return processor.report


def main():
    from lexoid.api import parse_to_latex
    from lexoid.cli import write_latex_page

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fallback-model", required=True)
    parser.add_argument("--ocr", default="none")
    parser.add_argument("--render-dpi", type=int, default=240)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--vision-concurrency", type=int, default=2)
    parser.add_argument("--auto-orient", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    processor = PageFallback(args.input, args.cache_dir, args.model, args.fallback_model,
                             render_dpi=args.render_dpi, auto_orient=args.auto_orient)
    parse_to_latex(str(args.input), model=args.model, ocr=args.ocr,
        render_dpi=args.render_dpi, evidence_output=str(args.evidence_output),
        cache_dir=str(args.cache_dir), vision_concurrency=args.vision_concurrency,
        auto_orient=args.auto_orient, resume=args.resume, max_page_attempts=1,
        page_processor=processor, page_callback=lambda page, total, tex:
            write_latex_page(args.output, page, total, tex))
    finalized, _ = normalize_tex(args.output.read_text("utf-8"))
    write_utf8_atomic(args.output, finalized)
    payload = json.loads(args.evidence_output.read_text())
    payload["page_models"] = processor.report["page_models"]
    payload["page_fallback"] = processor.report
    write_utf8_atomic(args.evidence_output, json.dumps(payload, ensure_ascii=False, indent=2))
    write_utf8_atomic(args.output.with_suffix(".page-checks.json"),
                      json.dumps(processor.report, ensure_ascii=False, indent=2))
    emit({"event": "page_checks_finish", "stage": "recognize", **processor.report})


if __name__ == "__main__":
    main()
