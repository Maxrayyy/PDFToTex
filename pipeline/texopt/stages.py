"""File-contract orchestration: recognize, reconcile, then compile and publish."""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .pipeline_state import PipelineState
from .model_config import resolve_model
from .textio import write_utf8_atomic


class ModelServicePaused(RuntimeError):
    pass


def _service_pause_recorded(path, offset):
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        stream.seek(offset)
        for line in stream:
            try:
                event = json.loads(line)
                if event.get("event") == "model_service_unavailable":
                    return True
            except (ValueError, AttributeError):
                continue
    return False


@dataclass(frozen=True)
class BatchConfig:
    ocr: str = "none"
    vision_model: str = field(default_factory=lambda: resolve_model("LEXOID_MODEL"))
    fallback_model: str | None = field(default_factory=lambda: os.getenv("VISION_FALLBACK_MODEL") or None)
    reconcile_model: str = field(default_factory=lambda: resolve_model("RECONCILE_MODEL"))
    optimizer_model: str = field(default_factory=lambda: os.getenv("TEXOPT_MODEL", ""))
    semantic_naming: str = field(default_factory=lambda: os.getenv("TEXOPT_SEMANTIC_NAMING", "deferred"))
    name_cache: str | None = field(default_factory=lambda: os.getenv("TEXOPT_NAME_CACHE") or None)
    repair_model: str = field(default_factory=lambda: resolve_model("TEXOPT_REPAIR_MODEL"))
    render_dpi: int = 240
    retry_dpi: int = 480
    vision_concurrency: int = 4
    reconcile_concurrency: int = 2
    optimizer_version: str = "texopt-layout-v11-outline-field-safe"
    timeout: int = 7200
    publish_root: str | None = None

    def __post_init__(self):
        if self.ocr not in {"none", "paddleocr"}:
            raise ValueError(f"Unsupported OCR mode: {self.ocr}")
        if self.timeout < 1:
            raise ValueError("Stage timeout must be positive")
        if self.semantic_naming not in {"deferred", "inline"}:
            raise ValueError("Unsupported semantic naming mode")

    @classmethod
    def from_env(cls, *, vision_model=None):
        return cls(
            ocr=os.getenv("RECOGNITION_OCR", "none"),
            vision_model=resolve_model("LEXOID_MODEL", vision_model),
            render_dpi=int(os.getenv("RENDER_DPI", "240")),
            retry_dpi=int(os.getenv("RETRY_DPI", "480")),
            vision_concurrency=int(os.getenv("VISION_CONCURRENCY", "4")),
            reconcile_concurrency=int(os.getenv("RECONCILE_CONCURRENCY", "2")),
            timeout=int(os.getenv("PIPELINE_STAGE_TIMEOUT_SECONDS", "7200")),
            publish_root=os.getenv("PIPELINE_PUBLISH_ROOT") or None,
        )


@dataclass(frozen=True)
class StageCommand:
    stage: str
    argv: list[str]
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...]
    version: str


def artifact_paths(source, source_root, output_root, *, publish_root=None):
    relative = Path(source).relative_to(source_root)
    group = Path(output_root) / relative.parent
    stem = relative.stem
    raw = group / "tex" / f"{stem}.tex"
    scratch = group / ".pipeline" / stem
    return {
        "raw": raw, "evidence": raw.with_suffix(".recognition.json"),
        "reconciled": raw.with_suffix(".reconciled.tex"), "fields": raw.with_suffix(".fields.json"),
        "optimized": (Path(publish_root) if publish_root else Path(output_root) / "optimized") / relative.with_suffix(".tex"),
        "work_optimized": scratch / f"{stem}.optimized.tex",
        "report": scratch / f"{stem}.report.json", "registry": scratch / f"{stem}.fields.json",
        "compile_log": scratch / f"{stem}.compile.log", "log_dir": scratch,
    }


def build_stage_commands(source, source_root, output_root, config):
    p = artifact_paths(source, source_root, output_root, publish_root=config.publish_root)
    cache = Path(output_root) / ".cache" / "recognition"
    recognize = ["lexoid", "latex"]
    if config.fallback_model and config.fallback_model != config.vision_model:
        recognize = ["python", "-m", "texopt.page_fallback",
                     "--fallback-model", config.fallback_model]
    return [
        StageCommand("recognize", [*recognize, "--input", str(source),
            "--output", str(p["raw"]), "--model", config.vision_model, "--ocr", config.ocr,
            "--render-dpi", str(config.render_dpi), "--evidence-output", str(p["evidence"]),
            "--cache-dir", str(cache), "--vision-concurrency", str(config.vision_concurrency),
            "--auto-orient", "--resume"], (Path(source),), (p["raw"], p["evidence"]), "evidence-latex-v8-json-tex-safe"),
        StageCommand("reconcile", ["texopt", "reconcile", str(p["raw"]),
            "--source-pdf", str(source), "--recognition-evidence", str(p["evidence"]),
            "-o", str(p["reconciled"]), "--fields", str(p["fields"]),
            "--model", config.reconcile_model, "--retry-dpi", str(config.retry_dpi),
            "--concurrency", str(config.reconcile_concurrency)],
            (Path(source), p["raw"], p["evidence"]), (p["reconciled"], p["fields"]), "reconcile-v4-outage-deferred"),
        StageCommand("optimise", ["texopt", "optimise", str(p["reconciled"]),
            "-o", str(p["work_optimized"]), "--source-registry", str(p["fields"]),
            "--registry", str(p["registry"]), "--report", str(p["report"]),
            "--log-file", str(p["log_dir"] / "optimise.log"),
            "--llm-model", config.optimizer_model, "--repair-model", config.repair_model,
            "--semantic-naming", config.semantic_naming,
            "--page-layout-evidence", str(p["evidence"]),
            "--name-cache", config.name_cache or str(Path(output_root) / ".cache" / "names.sqlite3"),
            "--llm-repair-on-failure", "--syntax-repair-cache",
            str(Path(output_root) / ".cache" / "syntax.json"), "--probe-dir", str(p["log_dir"] / "probe"),
            "--compile-check", "--compile-log", str(p["compile_log"])],
            (p["reconciled"], p["fields"], p["evidence"]),
            (p["work_optimized"], p["report"], p["registry"], p["compile_log"]), config.optimizer_version),
    ]


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_fingerprint(stage):
    payload = {"version": stage.version, "argv": stage.argv,
               "inputs": {str(path): sha256_file(path) for path in stage.inputs}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _complete_pages(path, total):
    markers = re.findall(r"(?m)^\s*% LEXOID_PAGE_COMPLETED:\s*(\d+)/(\d+)\s*$",
                         Path(path).read_text("utf-8"))
    if markers != [(str(page), str(total)) for page in range(1, total + 1)]:
        raise ValueError(f"Missing, duplicate, or unordered physical pages in {path}")
    require_recognized_pages(path)


def require_recognized_pages(path):
    from .syntax_repair import split_lexoid_pages
    text = Path(path).read_text("utf-8")
    marker = re.compile(r"(?m)^\s*%\s*LEXOID_RECOGNITION_FALLBACK\b")
    if marker.search(text):
        pages = [number for number, _, chunk in split_lexoid_pages(text) if marker.search(chunk)]
        raise ValueError(f"Missing recognition content on pages {pages}: {path}")


def _validate(stage, total):
    for path in stage.outputs:
        if not path.is_file():
            raise ValueError(f"Missing {stage.stage} artifact: {path}")
    _complete_pages(stage.outputs[0], total)
    if stage.stage == "recognize":
        evidence = json.loads(stage.outputs[1].read_text("utf-8"))
        if evidence.get("schema") != "recognition/v1" or [p["page"] for p in evidence["pages"]] != list(range(1, total + 1)):
            raise ValueError("Recognition evidence is incomplete")
    if stage.stage == "optimise":
        report = json.loads(stage.outputs[1].read_text("utf-8"))
        check = report.get("compile_check", {})
        if not check or check.get("ok") is not True or check.get("passes") != 2:
            raise ValueError("Two-pass compilation did not succeed")


def _run(stage, log_path, timeout):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n{stage.stage} started\n")
        log.flush()
        env = {**os.environ, "LEXOID_MODEL_CALL_LOG": str(log_path.with_suffix(".calls.jsonl"))}
        return subprocess.run(stage.argv, stdout=log, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env).returncode


def _publish(source, destination):
    require_recognized_pages(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def run_batch(source_root, output_root, config=None, runner=None, page_counter=None, include=None,
              *, include_paths=None):
    from .pdf_batch import _page_count, discover_pdfs
    from .model_telemetry import summarize_calls

    source_root, output_root = Path(source_root).resolve(), Path(output_root).resolve()
    config = config or BatchConfig.from_env()
    page_counter = page_counter or _page_count
    runner = runner or (lambda stage, log: _run(stage, log, config.timeout))
    if not source_root.is_dir():
        raise ValueError(f"PDF source directory does not exist: {source_root}")
    sources = discover_pdfs(source_root)
    if include_paths is not None:
        if include is not None:
            raise ValueError("Use either include or include_paths")
        selected = set(include_paths)
        available = {p.relative_to(source_root).as_posix() for p in sources}
        if selected - available:
            raise ValueError(f"Missing source PDFs: {sorted(selected - available)}")
        sources = [p for p in sources if p.relative_to(source_root).as_posix() in selected]
    else:
        sources = [p for p in sources if not include or p.name in include]
    output_root.mkdir(parents=True, exist_ok=True)
    state = PipelineState(output_root / ".state" / "pipeline.sqlite3")
    lock_path = output_root / ".state" / "batch.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state.recover_interrupted()
        manifest = {"source_root": str(source_root), "output_root": str(output_root), "files": []}
        failures = 0
        for source in sources:
            paths = artifact_paths(source, source_root, output_root, publish_root=config.publish_root)
            entry = {"source": str(source.relative_to(source_root)), "stages": []}
            manifest["files"].append(entry)
            try:
                total = page_counter(source)
                entry["pages"] = total
                for stage in build_stage_commands(source, source_root, output_root, config):
                    fingerprint = stage_fingerprint(stage)
                    cached = state.completed_metadata(stage.stage, str(source), fingerprint)
                    hashes = {str(p): sha256_file(p) for p in stage.outputs if p.is_file()}
                    if cached and cached.get("outputs") == hashes and len(hashes) == len(stage.outputs):
                        _validate(stage, total)
                        entry["stages"].append({"stage": stage.stage, "cached": True})
                        continue
                    state.invalidate_completed(stage.stage, str(source), fingerprint)
                    job = state.claim(stage.stage, str(source), fingerprint)
                    if job is None:
                        raise RuntimeError(f"{stage.stage} is not yet retryable")
                    for path in stage.outputs:
                        path.parent.mkdir(parents=True, exist_ok=True)
                    log_path = paths["log_dir"] / f"{stage.stage}.process.log"
                    calls_path = log_path.with_suffix(".calls.jsonl")
                    calls_offset = calls_path.stat().st_size if calls_path.exists() else 0
                    started = time.monotonic()
                    print(f"START {stage.stage}: {entry['source']}", flush=True)
                    try:
                        status = runner(stage, log_path)
                        if status != 0:
                            if _service_pause_recorded(calls_path, calls_offset):
                                raise ModelServicePaused(f"Model service unavailable; progress retained; see {log_path}")
                            raise RuntimeError(f"{stage.stage} exited {status}; see {log_path}")
                        _validate(stage, total)
                    except Exception as exc:
                        entry["stages"].append({"stage": stage.stage, "cached": False,
                            "status": "failed", "model_calls": summarize_calls(calls_path, calls_offset)})
                        state.fail(job, str(exc), retry_after=0)
                        state.event(stage.stage, str(source), "failed", {"error": str(exc)})
                        raise
                    metadata = {"seconds": time.monotonic() - started,
                                "model_calls": summarize_calls(calls_path, calls_offset),
                                "outputs": {str(p): sha256_file(p) for p in stage.outputs}}
                    print(f"DONE {stage.stage}: {entry['source']} ({metadata['seconds']:.1f}s)", flush=True)
                    state.complete(job, str(stage.outputs[0]), metadata)
                    state.event(stage.stage, str(source), "completed", metadata)
                    write_utf8_atomic(stage.outputs[0].with_suffix(".pipeline.done"),
                        json.dumps({"stage": stage.stage, "fingerprint": fingerprint, **metadata}, indent=2))
                    entry["stages"].append({"stage": stage.stage, "cached": False, **metadata})
                _publish(paths["work_optimized"], paths["optimized"])
                entry["status"] = "done"
            except Exception as exc:
                failures += 1
                entry.update(status="paused" if isinstance(exc, ModelServicePaused) else "failed", error=str(exc))
                print(f"{entry['status'].upper()} {entry['source']}: {exc}", flush=True)
            write_utf8_atomic(output_root / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            if entry["status"] == "paused":
                break
        return 1 if failures else 0
