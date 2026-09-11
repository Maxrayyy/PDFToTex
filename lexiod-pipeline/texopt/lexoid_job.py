"""
Interface 1 backend: turn an uploaded PDF into a lexoid invocation, and resume it.

Security note (important): the command is built as an **argv list**. Never build the
docker command by string concatenation -- a filename like `a.pdf; rm -rf /` would then
be executed. `preview()` exists only to show a copy-pasteable line and uses shlex.quote.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple
from .model_config import resolve_model

# --------------------------------------------------------------------------- #
# job model
# --------------------------------------------------------------------------- #

SAFE_NAME = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]+")


@dataclass
class LexoidJob:
    job_id: str
    host_workdir: str          # host dir bind-mounted into the container
    input_name: str            # file name inside host_workdir
    output_name: str
    model: str = field(default_factory=lambda: resolve_model("LEXOID_MODEL"))
    start_page: int = 1
    service: str = "lexoid"
    mode: str = "latex"
    container_mount: str = "/data"
    compose_file: Optional[str] = None
    extra_args: List[str] = field(default_factory=list)
    # runtime state
    status: str = "created"    # created|running|interrupted|done|failed
    last_complete_page: Optional[int] = None
    attempts: List[Dict] = field(default_factory=list)

    # ---- paths ----------------------------------------------------------- #
    @property
    def host_input(self) -> Path:
        return Path(self.host_workdir) / self.input_name

    @property
    def host_output(self) -> Path:
        return Path(self.host_workdir) / self.output_name

    def in_container(self, name: str) -> str:
        return str(PurePosixPath(self.container_mount) / name)

    # ---- command --------------------------------------------------------- #
    def argv(self, start_page: Optional[int] = None,
             output_name: Optional[str] = None) -> List[str]:
        sp = self.start_page if start_page is None else start_page
        out = output_name or self.output_name
        cmd = ["docker", "compose"]
        if self.compose_file:
            cmd += ["-f", self.compose_file]
        cmd += [
            "run", "--rm", "-T",                     # -T: no TTY -> safe under a web server
            "-v", f"{Path(self.host_workdir).resolve()}:{self.container_mount}",
            self.service, self.mode,
            "-i", self.in_container(self.input_name),
            "-o", self.in_container(out),
            "--model", self.model,
        ]
        if sp and sp > 1:
            cmd += ["--start-page", str(sp)]
        cmd += list(self.extra_args)
        return cmd

    def preview(self, **kw) -> str:
        """Human-readable, copy-pasteable. NOT used for execution."""
        return " \\\n  ".join(_chunk(self.argv(**kw)))

    # ---- persistence ------------------------------------------------------ #
    def save(self, state_dir: Path) -> Path:
        p = Path(state_dir) / f"{self.job_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, p)          # atomic; survives a crash mid-write
        return p

    @classmethod
    def load(cls, state_dir: Path, job_id: str) -> "LexoidJob":
        data = json.loads((Path(state_dir) / f"{job_id}.json").read_text("utf-8"))
        return cls(**data)


def _chunk(parts: List[str]) -> List[str]:
    """Group argv into readable flag/value pairs for the preview string."""
    out, i = [], 0
    while i < len(parts) and not parts[i].startswith("-"):
        i += 1
    if i:
        out.append(" ".join(shlex.quote(x) for x in parts[:i]))
    while i < len(parts):
        p = parts[i]
        if p.startswith("-") and i + 1 < len(parts) and not parts[i + 1].startswith("-"):
            out.append(f"{shlex.quote(p)} {shlex.quote(parts[i + 1])}")
            i += 2
        else:
            out.append(shlex.quote(p))
            i += 1
    return out


def new_job(host_workdir: str, input_name: str, model: Optional[str] = None,
            start_page: int = 1, output_name: Optional[str] = None,
            **kw) -> LexoidJob:
    stem = Path(input_name).stem
    jid = f"{SAFE_NAME.sub('_', stem)}-{int(time.time())}"
    return LexoidJob(
        job_id=jid, host_workdir=host_workdir, input_name=input_name,
        output_name=output_name or f"{stem}.tex", model=resolve_model("LEXOID_MODEL", model),
        start_page=start_page, **kw)


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #

# Legacy visible page-header marker used only by merge_shards.
PAGE_MARKER = re.compile(r"^\s*%+\s*(?:---+\s*)?page[\s_-]*(\d+)", re.I | re.M)
PAGE_COMPLETED = re.compile(
    r"%\s*LEXOID_PAGE_COMPLETED:\s*(\d+)\s*/\s*(\d+)", re.I
)


def scan_pages(tex_path: Path) -> List[Tuple[int, int]]:
    """Return durable [(completed_page, char_offset)] entries."""
    if not Path(tex_path).exists():
        return []
    text = Path(tex_path).read_text("utf-8", errors="replace")
    return [(int(m.group(1)), m.start()) for m in PAGE_COMPLETED.finditer(text)]


def truncate_to_last_complete_page(tex_path: Path,
                                   backup: bool = True) -> Optional[int]:
    """
    Cut off anything after the last durable LEXOID_PAGE_COMPLETED marker.

    Returns the last completed page, not the resume page. The caller must resume at
    last_completed + 1. Returns None if nothing durable exists.
    """
    marks = scan_pages(tex_path)
    if not marks:
        return None
    p = Path(tex_path)
    text = p.read_text("utf-8", errors="replace")
    last_page, last_off = marks[-1]
    marker = PAGE_COMPLETED.search(text, last_off)
    if marker is None:
        return None
    line_end = text.find("\n", marker.end())
    durable_end = len(text) if line_end < 0 else line_end + 1
    if backup:
        p.with_suffix(p.suffix + f".partial-{int(time.time())}").write_text(text, "utf-8")
    p.write_text(text[:durable_end], "utf-8")
    return last_page


def resume_argv(job: LexoidJob, shard_dir: Optional[Path] = None) -> Tuple[List[str], int]:
    """
    Build the resume command against the existing output file. Lexoid requires that
    the path passed to -o already end with the checkpoint immediately preceding
    --start-page, so a new shard path cannot be used for resume.
    """
    last_completed = truncate_to_last_complete_page(job.host_output)
    if last_completed is None:
        return job.argv(start_page=job.start_page), job.start_page
    resume_page = last_completed + 1
    job.last_complete_page = last_completed
    return job.argv(start_page=resume_page, output_name=job.output_name), resume_page


def merge_shards(base: Path, shards: List[Path], out: Path) -> None:
    """Concatenate base + shards in page order, dropping duplicated page blocks."""
    seen: set[int] = set()
    chunks: List[str] = []
    for f in [base, *shards]:
        if not Path(f).exists():
            continue
        text = Path(f).read_text("utf-8", errors="replace")
        marks = [(int(m.group(1)), m.start()) for m in PAGE_MARKER.finditer(text)]
        if not marks:
            chunks.append(text)
            continue
        if marks[0][1] > 0 and not chunks:
            chunks.append(text[:marks[0][1]])          # preamble of the base file
        for idx, (page, off) in enumerate(marks):
            end = marks[idx + 1][1] if idx + 1 < len(marks) else len(text)
            if page in seen:
                continue
            seen.add(page)
            chunks.append(text[off:end])
    Path(out).write_text("".join(chunks), "utf-8")


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #

def run(job: LexoidJob, argv: List[str], log_path: Path,
        timeout: Optional[int] = None) -> int:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(log_path, "ab") as log:
        log.write(f"\n$ {' '.join(shlex.quote(a) for a in argv)}\n".encode())
        log.flush()
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = 124
    job.attempts.append({"argv": argv, "rc": rc,
                         "seconds": round(time.time() - started, 1),
                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    job.status = "done" if rc == 0 else ("interrupted" if rc in (124, 130, 137) else "failed")
    return rc
