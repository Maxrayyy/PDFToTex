"""Resumable directory batch runner for ``lexoid latex``."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


CHECKPOINT_RE = re.compile(
    r"(?m)^% LEXOID_PAGE_COMPLETED: (?P<page>\d+)/(?P<total>\d+)\s*$"
)


def discover_pdfs(source_root: Path) -> list[Path]:
    source_root = Path(source_root)
    files = []
    for path in source_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        relative = path.relative_to(source_root)
        if path.name.startswith("._") or any(part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return sorted(files, key=lambda path: str(path.relative_to(source_root)))


def output_path_for(source: Path, source_root: Path, output_root: Path) -> Path:
    relative = Path(source).relative_to(source_root)
    name = f"{relative.stem}.optimized.tex"
    return Path(output_root) / relative.parent / "optimized" / name


def legacy_output_path_for(
    source: Path, source_root: Path, output_root: Path
) -> Path:
    return Path(output_root) / Path(source).relative_to(source_root).with_suffix(".tex")


def working_path_for(source: Path, source_root: Path, work_root: Path) -> Path:
    return Path(work_root) / Path(source).relative_to(source_root).with_suffix(".tex")


def optimized_work_path_for(raw_output: Path) -> Path:
    return Path(raw_output).with_name(f"{Path(raw_output).stem}.optimized.tex")


def _artifact_path_for(
    final_output: Path, output_root: Path, directory: str, suffix: str
) -> Path:
    relative = Path(final_output).relative_to(Path(output_root))
    stem = relative.name.removesuffix(".optimized.tex")
    group = relative.parent.parent if relative.parent.name == "optimized" else relative.parent
    return Path(output_root) / group / directory / f"{stem}{suffix}"


def publish_file(source: Path, destination: Path) -> None:
    """Publish across mounts while keeping the final replacement atomic."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)
    source.unlink()


def read_checkpoint(output: Path) -> Optional[tuple[int, int]]:
    if not Path(output).exists():
        return None
    matches = list(CHECKPOINT_RE.finditer(Path(output).read_text("utf-8", errors="replace")))
    if not matches:
        return None
    match = matches[-1]
    return int(match.group("page")), int(match.group("total"))


def build_lexoid_command(source: Path, output: Path, model: str) -> list[str]:
    command = [
        "lexoid",
        "latex",
        "--input",
        str(source),
        "--output",
        str(output),
        "--model",
        model,
        "--auto-orient",
    ]
    checkpoint = read_checkpoint(output)
    if checkpoint and checkpoint[0] < checkpoint[1]:
        command.extend(["--start-page", str(checkpoint[0] + 1)])
    return command


def build_optimizer_command(
    raw_output: Path,
    final_output: Path,
    output_root: Path,
    model: str,
) -> list[str]:
    raw_output = Path(raw_output)
    final_output = Path(final_output)
    output_root = Path(output_root)
    log_path = _artifact_path_for(
        final_output, output_root, "_logs", ".texopt.log"
    )
    state_root = output_root / "_state"
    return [
        "texopt",
        "optimise",
        str(raw_output),
        "-o",
        str(optimized_work_path_for(raw_output)),
        "--registry",
        str(raw_output.with_suffix(".fields.json")),
        "--report",
        str(raw_output.with_suffix(".report.json")),
        "--log-file",
        str(log_path),
        "--llm-model",
        model,
        "--name-cache",
        str(state_root / "texopt-names.json"),
        "--syntax-repair-cache",
        str(state_root / "texopt-syntax-repairs.json"),
        "--llm-repair-on-failure",
        "--compile-check",
    ]


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def _run_one(command: list[str], log_path: Path, label: str) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{time.strftime('%Y-%m-%dT%H:%M:%S')}] starting {label}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            if "Progress:" in line or "Error:" in line:
                print(f"[{label}] {line.rstrip()}", flush=True)
        return process.wait()


def run_batch(
    source_root: Path,
    output_root: Path,
    work_root: Path,
    model: str,
) -> int:
    from .stages import BatchConfig, run_batch as run_stages

    config = BatchConfig.from_env(vision_model=model)
    return run_stages(source_root, output_root, config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=None,
                        help="accepted for compatibility; durable artifacts now live under output-root")
    parser.add_argument("--model", help="recognition model (environment: LEXOID_MODEL)")
    args = parser.parse_args()
    return run_batch(args.source_root, args.output_root, args.work_root, args.model)


if __name__ == "__main__":
    raise SystemExit(main())
