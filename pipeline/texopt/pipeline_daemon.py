"""Durable folder-driven Lexoid -> texopt -> human review -> JSON pipeline."""

from __future__ import annotations

import hashlib
import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .json_extractor import extract_fields, write_json
from .pipeline_state import PipelineState
from .model_config import resolve_model
from .textio import read_text_auto, write_utf8_atomic


PAGE_COMPLETED = re.compile(
    r"%\s*LEXOID_PAGE_COMPLETED:\s*(\d+)\s*/\s*(\d+)", re.I
)


@dataclass(frozen=True)
class Config:
    root: Path
    pdf_input_dir: Path
    lexoid_tex_dir: Path
    optimized_tex_dir: Path
    reviewed_tex_dir: Path
    json_output_dir: Path
    state_db: Path
    log_file: Path
    poll_seconds: float
    stable_seconds: float
    retry_base_seconds: float
    retry_max_seconds: float
    max_attempts: int
    lexoid_command: str
    lexoid_model: str
    lexoid_timeout_seconds: int
    optimizer_command: str
    optimizer_extra_args: str
    optimizer_timeout_seconds: int
    enable_json_stage: bool

    @classmethod
    def from_env(cls) -> "Config":
        root = Path(os.environ.get("PIPELINE_ROOT", "/data")).resolve()

        def path(name: str, default: str) -> Path:
            return Path(os.environ.get(name, str(root / default))).resolve()

        return cls(
            root=root,
            pdf_input_dir=path("PDF_INPUT_DIR", "01-pdf-input"),
            lexoid_tex_dir=path("LEXOID_TEX_DIR", "02-lexoid-tex"),
            optimized_tex_dir=path("OPTIMIZED_TEX_DIR", "03-review-pending"),
            reviewed_tex_dir=path("REVIEWED_TEX_DIR", "04-reviewed-tex"),
            json_output_dir=path("JSON_OUTPUT_DIR", "05-json-output"),
            state_db=path("PIPELINE_STATE_DB", "state/pipeline.sqlite3"),
            log_file=path("PIPELINE_LOG_FILE", "logs/pipeline.jsonl"),
            poll_seconds=float(os.environ.get("POLL_INTERVAL_SECONDS", "5")),
            stable_seconds=float(os.environ.get("FILE_STABLE_SECONDS", "3")),
            retry_base_seconds=float(os.environ.get("RETRY_BASE_SECONDS", "15")),
            retry_max_seconds=float(os.environ.get("RETRY_MAX_SECONDS", "900")),
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
            lexoid_command=os.environ.get(
                "LEXOID_COMMAND",
                "lexoid latex -i {input} -o {output} --model {model} "
                "--start-page {start_page}",
            ),
            lexoid_model=os.environ.get("LEXOID_MODEL", ""),
            lexoid_timeout_seconds=int(os.environ.get("LEXOID_TIMEOUT_SECONDS", "7200")),
            optimizer_command=os.environ.get("OPTIMIZER_COMMAND", "texopt"),
            optimizer_extra_args=os.environ.get(
                "OPTIMIZER_EXTRA_ARGS", "--llm-repair-on-failure --compile-check"
            ),
            optimizer_timeout_seconds=int(
                os.environ.get("OPTIMIZER_TIMEOUT_SECONDS", "3600")
            ),
            enable_json_stage=env_bool("ENABLE_JSON_STAGE", False),
        )

    def ensure_dirs(self) -> None:
        for directory in (
            self.pdf_input_dir, self.lexoid_tex_dir, self.optimized_tex_dir,
            self.reviewed_tex_dir, self.json_output_dir, self.state_db.parent,
            self.log_file.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class PipelineLog:
    def __init__(self, path: Path, state: PipelineState) -> None:
        self.path = path
        self.state = state
        self.lock = threading.Lock()

    def emit(self, stage: str, event: str, source: str = "", level: str = "INFO",
             **payload) -> None:
        record = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": level,
            "stage": stage,
            "event": event,
            "source": source,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.lock:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
            print(line, file=sys.stderr, flush=True)
        self.state.event(stage, source, event, payload)


class StabilityGate:
    def __init__(self, stable_seconds: float) -> None:
        self.stable_seconds = stable_seconds
        self.seen: dict[str, tuple[int, int, float]] = {}

    def ready(self, path: Path) -> bool:
        stat = path.stat()
        # Cron creates a new process (and therefore a new gate) every run. File age
        # provides a durable readiness signal without requiring in-memory history.
        if time.time() - stat.st_mtime >= self.stable_seconds:
            return True
        key = str(path.resolve())
        signature = (stat.st_size, stat.st_mtime_ns)
        previous = self.seen.get(key)
        now = time.monotonic()
        if not previous or previous[:2] != signature:
            self.seen[key] = (*signature, now)
            return self.stable_seconds <= 0
        return now - previous[2] >= self.stable_seconds


class FolderWorker(threading.Thread):
    def __init__(self, stage: str, directory: Path, suffixes: tuple[str, ...],
                 config: Config, state: PipelineState, log: PipelineLog,
                 stop: threading.Event, handler: Callable[[Path, int, str], Path]) -> None:
        super().__init__(name=f"pipeline-{stage}", daemon=True)
        self.stage = stage
        self.directory = directory
        self.suffixes = tuple(s.lower() for s in suffixes)
        self.config = config
        self.state = state
        self.log = log
        self.stop_event = stop
        self.handler = handler
        self.gate = StabilityGate(config.stable_seconds)

    def run(self) -> None:
        self.log.emit(self.stage, "WORKER_START", directory=str(self.directory))
        while not self.stop_event.is_set():
            try:
                self.scan_once()
            except Exception as exc:
                self.log.emit(self.stage, "SCAN_ERROR", level="ERROR",
                              error=str(exc), traceback=traceback.format_exc(limit=8))
            self.stop_event.wait(self.config.poll_seconds)
        self.log.emit(self.stage, "WORKER_STOP")

    def scan_once(self) -> None:
        for path in sorted(self.directory.rglob("*")):
            if self.stop_event.is_set():
                return
            relative = path.relative_to(self.directory)
            if (not path.is_file()
                    or any(part.startswith(".") for part in relative.parts)
                    or path.suffix.lower() not in self.suffixes
                    or ".partial." in path.name or ".resume-" in path.name):
                continue
            if not self.gate.ready(path):
                continue
            fingerprint = sha256_file(path)
            job_id = self.state.claim(
                self.stage, str(path.resolve()), fingerprint, self.config.max_attempts
            )
            if job_id is None:
                continue
            attempts = self.state.attempts(job_id)
            self.log.emit(self.stage, "JOB_START", str(path), job_id=job_id,
                          fingerprint=fingerprint, attempt=attempts)
            try:
                output = self.handler(path, job_id, fingerprint)
                metadata = {"fingerprint": fingerprint, "attempt": attempts}
                self.state.complete(job_id, str(output.resolve()), metadata)
                write_done_marker(output, self.stage, path, fingerprint, attempts)
                self.log.emit(self.stage, "JOB_COMPLETE", str(path), job_id=job_id,
                              output=str(output), attempt=attempts)
            except Exception as exc:
                delay = min(
                    self.config.retry_max_seconds,
                    self.config.retry_base_seconds * (2 ** max(0, attempts - 1)),
                )
                self.state.fail(job_id, str(exc), delay)
                self.log.emit(self.stage, "JOB_RETRY", str(path), level="ERROR",
                              job_id=job_id, attempt=attempts,
                              retry_after_seconds=delay, error=str(exc),
                              traceback=traceback.format_exc(limit=12))


class Pipeline:
    def __init__(self, config: Config) -> None:
        self.config = config
        config.ensure_dirs()
        self.state = PipelineState(config.state_db)
        self.log = PipelineLog(config.log_file, self.state)
        self.stop = threading.Event()
        recovered = self.state.recover_interrupted()
        self.log.emit("system", "DAEMON_START", recovered_jobs=recovered,
                      pid=os.getpid(), config=_safe_config(config))
        self.workers = [
            FolderWorker("lexoid", config.pdf_input_dir, (".pdf",), config,
                         self.state, self.log, self.stop, self.run_lexoid),
            FolderWorker("optimizer", config.lexoid_tex_dir, (".tex",), config,
                         self.state, self.log, self.stop, self.run_optimizer),
        ]
        if config.enable_json_stage:
            self.workers.append(
                FolderWorker("json", config.reviewed_tex_dir, (".tex",), config,
                             self.state, self.log, self.stop, self.run_json)
            )
        else:
            self.log.emit("json", "STAGE_DISABLED",
                          reason="ENABLE_JSON_STAGE is false")

    def run_forever(self) -> None:
        for worker in self.workers:
            worker.start()
        while not self.stop.wait(1):
            pass
        for worker in self.workers:
            worker.join(timeout=30)
        self.log.emit("system", "DAEMON_STOP")

    def run_once(self) -> None:
        """One cron-safe scan of every stage; stages remain logically independent."""
        started = time.monotonic()
        self.log.emit("system", "CRON_RUN_START")
        for worker in self.workers:
            worker.scan_once()
        self.log.emit("system", "CRON_RUN_FINISH",
                      seconds=round(time.monotonic() - started, 3))

    def run_lexoid(self, source: Path, job_id: int, fingerprint: str) -> Path:
        relative = relative_source(source, self.config.pdf_input_dir)
        output_dir = self.config.lexoid_tex_dir / relative.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        final = output_dir / f"{source.stem}.tex"
        partial = output_dir / f".{source.stem}.partial.tex"
        start_page = prepare_partial_for_resume(partial)
        # Lexoid validates --start-page against the checkpoint already present in
        # the *same* output file. Resume in place; a fresh shard has no checkpoint
        # and is rejected before conversion can continue.
        target = partial
        argv = render_command(self.config.lexoid_command, {
            "input": str(source.resolve()), "output": str(target.resolve()),
            "model": resolve_model("LEXOID_MODEL", self.config.lexoid_model or None),
            "start_page": str(start_page),
        })
        item_log = mirrored_log(self.config.log_file.parent, "lexoid", relative)
        self.log.emit("lexoid", "COMMAND", str(source), job_id=job_id,
                      argv=argv, start_page=start_page, item_log=str(item_log))
        run_command(argv, item_log, self.config.lexoid_timeout_seconds)
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(f"Lexoid returned success but produced no output: {target}")
        os.replace(target, final)
        return final

    def run_optimizer(self, source: Path, job_id: int, fingerprint: str) -> Path:
        relative = relative_source(source, self.config.lexoid_tex_dir)
        output_dir = self.config.optimized_tex_dir / relative.parent
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{source.stem}.optimized"
        output = output_dir / f"{stem}.tex"
        registry = output_dir / f"{stem}.fields.json"
        report = output_dir / f"{stem}.report.json"
        optimizer_log = mirrored_log(
            self.config.log_file.parent, "optimizer", relative, ".texopt.log"
        )
        argv = shlex.split(self.config.optimizer_command) + [
            "optimise", str(source.resolve()), "-o", str(output.resolve()),
            "--registry", str(registry.resolve()), "--report", str(report.resolve()),
            "--log-file", str(optimizer_log.resolve()),
        ] + shlex.split(self.config.optimizer_extra_args)
        item_log = mirrored_log(
            self.config.log_file.parent, "optimizer-process", relative
        )
        self.log.emit("optimizer", "COMMAND", str(source), job_id=job_id,
                      argv=argv, item_log=str(item_log))
        run_command(argv, item_log, self.config.optimizer_timeout_seconds)
        if not output.exists():
            raise RuntimeError(f"optimizer produced no output: {output}")
        return output

    def run_json(self, source: Path, job_id: int, fingerprint: str) -> Path:
        relative = relative_source(source, self.config.reviewed_tex_dir)
        output = (self.config.json_output_dir / relative).with_suffix(".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        registry_candidates = (
            source.with_suffix(".fields.json"),
            (self.config.optimized_tex_dir / relative).with_suffix(".fields.json"),
        )
        registry = next((p for p in registry_candidates if p.exists()), None)
        report = extract_fields(source, registry)
        write_json(report, output)
        self.log.emit("json", "JSON_EXTRACT", str(source), job_id=job_id,
                      output=str(output), registry=str(registry) if registry else "",
                      fields=report.total_fields, pages=report.total_pages,
                      warnings=report.warnings)
        return output


def run_command(argv: list[str], log_path: Path, timeout: int) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n[{datetime.now().astimezone().isoformat()}] $ "
                     + " ".join(shlex.quote(x) for x in argv) + "\n")
        stream.flush()
        try:
            result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"command timed out after {timeout}s: {argv[0]}") from exc
    if result.returncode != 0:
        tail = tail_text(log_path, 30)
        raise RuntimeError(
            f"command failed with exit {result.returncode}: {argv[0]}\n{tail}"
        )


def render_command(template: str, values: dict[str, str]) -> list[str]:
    return [part.format(**values) for part in shlex.split(template)]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def relative_source(source: Path, root: Path) -> Path:
    """Return a safe source-relative path used to mirror nested folder trees."""
    try:
        return source.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"source is outside configured stage root: {source}") from exc


def mirrored_log(log_root: Path, stage: str, relative: Path,
                 suffix: str = ".log") -> Path:
    """Mirror the source tree below a stage-specific log directory."""
    path = log_root / stage / relative.parent / f"{relative.name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def last_completed_page(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    text = read_text_auto(path).text
    pages = [int(match.group(1)) for match in PAGE_COMPLETED.finditer(text)]
    return max(pages) if pages else None


def resume_page_after(path: Path, first_page: int = 1) -> int:
    """Resume strictly after the last durable LEXOID_PAGE_COMPLETED marker."""
    completed = last_completed_page(path)
    return completed + 1 if completed is not None else first_page


def prepare_partial_for_resume(path: Path, first_page: int = 1) -> int:
    """Trim a partial file after its last checkpoint and return checkpoint + 1."""
    if not path.exists():
        return first_page
    decoded = read_text_auto(path)
    matches = list(PAGE_COMPLETED.finditer(decoded.text))
    if not matches:
        return first_page
    marker = matches[-1]
    line_end = decoded.text.find("\n", marker.end())
    durable_end = len(decoded.text) if line_end < 0 else line_end + 1
    durable = decoded.text[:durable_end]
    # Lexoid writes a temporary \end{document} when a partial run stops. It must
    # be removed before appending the next pages; the final resumed run will add
    # the one legitimate document terminator.
    durable = re.sub(
        r"(?m)^[ \t]*\\end\{document\}[ \t]*(?:\r?\n)?", "", durable
    )
    if durable != decoded.text:
        write_utf8_atomic(path, durable)
    return int(marker.group(1)) + 1


def merge_lexoid_resume(base_path: Path, shard_path: Path, last_page: int) -> str:
    base = read_text_auto(base_path).text
    shard = read_text_auto(shard_path).text
    matches = list(PAGE_COMPLETED.finditer(base))
    if not matches:
        raise RuntimeError("cannot resume: partial Lexoid TeX has no completed page marker")
    end = base.find("\n", matches[-1].end())
    base_complete = base[:len(base) if end < 0 else end + 1]
    base_complete = re.sub(r"\\end\{document\}\s*$", "", base_complete)
    begin = re.search(r"\\begin\{document\}", shard)
    shard_body = shard[begin.end():] if begin else shard
    first = PAGE_COMPLETED.search(shard_body)
    if not first or int(first.group(1)) <= last_page:
        raise RuntimeError("resume shard does not contain a page after the partial output")
    return base_complete.rstrip() + "\n" + shard_body.lstrip()


def write_done_marker(output: Path, stage: str, source: Path, fingerprint: str,
                      attempts: int) -> None:
    # Keep completion metadata visible without polluting the JSON output glob.
    marker = output.with_name(output.name + ".pipeline.done")
    payload = {
        "stage": stage, "source": str(source.resolve()),
        "output": str(output.resolve()), "fingerprint": fingerprint,
        "attempts": attempts,
        "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_utf8_atomic(marker, json.dumps(payload, ensure_ascii=False, indent=2))


def tail_text(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def _safe_config(config: Config) -> dict:
    data = asdict(config)
    return {key: str(value) if isinstance(value, Path) else value
            for key, value in data.items()}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="texopt-pipeline")
    parser.add_argument("--once", action="store_true",
                        help="scan all stages once and exit (recommended for crontab)")
    parser.add_argument("--status", action="store_true",
                        help="print persisted job status from previous cron runs")
    args = parser.parse_args(argv)
    if os.environ.get("PDF_SOURCE_ROOT"):
        from .stages import BatchConfig, run_batch
        output_root = Path(os.environ.get("PIPELINE_OUTPUT_ROOT", "/data"))
        if args.status:
            print(json.dumps(PipelineState(output_root / ".state" / "pipeline.sqlite3").status_snapshot(),
                             ensure_ascii=False, indent=2))
            return 0
        while True:
            status = run_batch(Path(os.environ["PDF_SOURCE_ROOT"]), output_root, BatchConfig.from_env())
            if args.once:
                return status
            time.sleep(float(os.environ.get("POLL_INTERVAL_SECONDS", "30")))
    config = Config.from_env()
    config.ensure_dirs()
    if args.status:
        print(json.dumps(PipelineState(config.state_db).status_snapshot(),
                         ensure_ascii=False, indent=2))
        return 0
    lock_path = config.state_db.with_suffix(config.state_db.suffix + ".lock")
    lock_stream = lock_path.open("a+")
    try:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "level": "WARNING", "stage": "system", "event": "CRON_OVERLAP_SKIPPED",
            "lock": str(lock_path),
        }), file=sys.stderr, flush=True)
        return 0

    pipeline = Pipeline(config)
    if args.once:
        pipeline.run_once()
        return 0

    def stop(_signum, _frame) -> None:
        pipeline.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    pipeline.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
