"""
Lexiod Pipeline — Streamlit Web UI
====================================
Three tabs:
  1. PDF → LaTeX    (single or batch, via docker compose run lexoid)
  2. TeX Optimizer   (single or batch, via python -m texopt.optimization.cli optimise)

Batch mode: upload multiple files → they are queued and processed one-by-one.
Each file gets its own output sub-directory.  The queue auto-advances:
when one item finishes, the next one starts automatically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import streamlit as st

# ── make `texopt.*` importable ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from texopt.recognition.lexoid_job import (  # noqa: E402
    LexoidJob,
    merge_shards,
    new_job,
    resume_argv,
    run as lexoid_run,
    scan_pages,
    truncate_to_last_complete_page,
)
from texopt.recognition.json_extractor import (  # noqa: E402
    extract_fields,
    merge_reports,
    write_json,
)

# ── environment defaults ───────────────────────────────────────────────────
DEFAULT_MODEL = os.environ.get("LEXOID_MODEL", "")
HOST_WORK_DIR = os.path.expanduser(
    os.environ.get("HOST_WORK_DIR", os.environ.get("WORK_DIR", "~/Downloads"))
)
CONTAINER_WORK_DIR = os.environ.get("CONTAINER_WORK_DIR", HOST_WORK_DIR)
COMPOSE_FILE = os.environ.get("COMPOSE_FILE", None)

LOG_DIR = Path(PROJECT_ROOT) / ".pipeline-logs"
MAX_LOG_BYTES = 200_000

# ── status constants ───────────────────────────────────────────────────────
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"
SKIPPED = "skipped"

_STATUS_ICON = {
    QUEUED: "⏳",
    RUNNING: "🔄",
    DONE: "✅",
    FAILED: "❌",
    INTERRUPTED: "⚠️",
    SKIPPED: "⏭️",
}


# ═══════════════════════════════════════════════════════════════════════════
# Queue item dataclass
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class QItem:
    id: str
    filename: str
    status: str = QUEUED
    log_path: str = ""
    rc: Optional[int] = None
    job_id: Optional[str] = None  # LexoidJob.job_id (PDF mode only)
    start_time: Optional[float] = None


# ═══════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════


def _save_upload(uploaded_file, workdir: str) -> str:
    dest = Path(workdir) / uploaded_file.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return uploaded_file.name


def _read_log_tail(path: str | Path, max_bytes: int = MAX_LOG_BYTES) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        return f.read().decode("utf-8", errors="replace")


# ── single-mode status display ─────────────────────────────────────────────

_SINGLE_ICONS = {
    "idle": "⏸️ Idle",
    "running": "🔄 Running…",
    "done": "✅ Done",
    "failed": "❌ Failed",
    "error": "💥 Error",
    "interrupted": "⚠️ Interrupted",
    "degraded": "⚠️ Done (warnings)",
    "unconvertible": "🚫 Unconvertible",
}


def _format_elapsed(seconds: float) -> str:
    """Format seconds into human-readable elapsed time."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    else:
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        return f"{h}h {m}m"


def _estimate_remaining(prefix: str, elapsed: float, progress: dict) -> str:
    """Estimate remaining time based on progress."""
    current = progress.get("current_page")
    total = progress.get("total_pages")

    if current and total and current > 0 and total > current:
        # We have step progress — estimate based on linear extrapolation
        per_step = elapsed / current
        remaining_steps = total - current
        est_remaining = per_step * remaining_steps
        return f"~{_format_elapsed(est_remaining)} remaining"

    # Rough estimates based on prefix and elapsed time
    if prefix == "tex":
        # TeX optimizer: typically 30-120s total
        if elapsed < 5:
            return "est. ~30s-2min total"
        elif elapsed < 30:
            return "est. ~30s more"
        else:
            return "almost done…"
    else:
        # PDF→LaTeX (lexoid): typically 1-5min per page
        if elapsed < 10:
            return "est. ~1-5min total"
        elif elapsed < 60:
            return "est. ~1-3min more"
        else:
            return "may take a few more minutes…"


def _display_single_status(prefix: str) -> None:
    status = st.session_state[f"{prefix}_status"]
    log_path = st.session_state.get(f"{prefix}_log_path")
    thread = st.session_state.get(f"{prefix}_thread")
    start_time = st.session_state.get(f"{prefix}_start_time")

    st.markdown(f"**Status:** {_SINGLE_ICONS.get(status, status)}")

    if status == "running" and log_path:
        # Enhanced live log display with progress parsing
        log_content = _read_log_tail(log_path) or "Waiting for output…"
        line_count = log_content.count("\n") + 1 if log_content.strip() else 0

        # Parse progress from log
        progress_info = _parse_log_progress(log_content, prefix)

        # Calculate elapsed time
        elapsed_str = ""
        remaining_str = ""
        if start_time:
            elapsed = time.time() - start_time
            elapsed_str = f"⏱️ {_format_elapsed(elapsed)}"
            remaining_str = _estimate_remaining(prefix, elapsed, progress_info)

        with st.container(border=True):
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                st.markdown("##### 📋 Live Processing Log")
            with col2:
                if elapsed_str:
                    st.markdown(f"**{elapsed_str}**")
            with col3:
                if remaining_str:
                    st.caption(remaining_str)

            # Show progress bar if we have step/page info
            if progress_info["current_page"] and progress_info["total_pages"]:
                pct = progress_info["current_page"] / progress_info["total_pages"]
                label = progress_info.get("stage", "") or f"Step {progress_info['current_page']}/{progress_info['total_pages']}"
                st.progress(pct, text=label)
            elif progress_info["stage"]:
                st.info(f"🔄 {progress_info['stage']}")

            st.caption(f"Lines: {line_count} | Auto-refreshing every 0.5s")
            log_area = st.empty()
            log_area.code(log_content, language="text")

        if thread and not thread.is_alive():
            st.rerun()
        else:
            time.sleep(0.5)
            st.rerun()
    elif status in ("done", "degraded") and log_path:
        # Show total elapsed time on completion
        if start_time:
            elapsed = time.time() - start_time
            st.caption(f"⏱️ Completed in {_format_elapsed(elapsed)}")
        with st.expander("📜 Full log", expanded=False):
            st.code(_read_log_tail(log_path, 500_000), language="text")
    elif status in ("failed", "error", "unconvertible") and log_path:
        if start_time:
            elapsed = time.time() - start_time
            st.caption(f"⏱️ Failed after {_format_elapsed(elapsed)}")
        with st.expander("📜 Error log", expanded=True):
            st.code(_read_log_tail(log_path), language="text")
        rc = st.session_state.get(f"{prefix}_rc")
        if rc is not None:
            st.error(f"Exit code: {rc}")
    elif status == "interrupted" and log_path:
        with st.expander("📜 Log (interrupted)", expanded=True):
            st.code(_read_log_tail(log_path), language="text")


def _parse_log_progress(log_content: str, prefix: str) -> dict:
    """Extract progress information from log content."""
    import re
    result = {"current_page": None, "total_pages": None, "stage": None}

    if not log_content:
        return result

    # texopt CLI: "[N/7] step description" pattern
    step_match = re.search(r"\[(\d+)/(\d+)\]\s*(.+)", log_content)
    if step_match:
        current = int(step_match.group(1))
        total = int(step_match.group(2))
        desc = step_match.group(3).rstrip("…").strip()
        # Find the last step marker
        all_steps = re.findall(r"\[(\d+)/(\d+)\]\s*(.+)", log_content)
        if all_steps:
            last = all_steps[-1]
            current = int(last[0])
            total = int(last[1])
            desc = last[2].rstrip("…").strip()
        result["current_page"] = current
        result["total_pages"] = total
        result["stage"] = f"Step {current}/{total}: {desc}"
        return result

    # Look for page progress patterns
    # lexoid: LEXOID_PAGE_COMPLETED markers
    completed_matches = re.findall(
        r"LEXOID_PAGE_COMPLETED:\s*(\d+)\s*/\s*(\d+)",
        log_content
    )
    if completed_matches:
        last = completed_matches[-1]
        result["current_page"] = int(last[0])
        result["total_pages"] = int(last[1])
        return result

    # Generic page patterns: "processing page X of Y"
    page_match = re.search(
        r"(?:processing|page)\s+(\d+)\s*(?:of|/)\s*(\d+)",
        log_content, re.I
    )
    if page_match:
        result["current_page"] = int(page_match.group(1))
        result["total_pages"] = int(page_match.group(2))
        return result

    # Look for stage information from keywords
    lines = log_content.strip().splitlines()
    last_line = lines[-1].strip() if lines else ""
    if last_line:
        result["stage"] = last_line

    return result


def _show_tex_report(report_file: Path) -> None:
    try:
        report = json.loads(report_file.read_text("utf-8"))
    except (json.JSONDecodeError, KeyError):
        return
    with st.expander("📊 Optimization Report", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tables", report.get("tables", "?"))
        c2.metric("Cells", report.get("cells", "?"))
        c3.metric("Fields", report.get("fields", "?"))
        c4.metric("Lines",
                  f"{report.get('lines_before', '?')} → {report.get('lines_after', '?')}")
        naming = report.get("naming", {})
        if naming:
            st.write("**Naming sources:**", naming)
        fallbacks = report.get("naming_fallbacks", [])
        if fallbacks:
            st.warning(f"{len(fallbacks)} table(s) used positional names: "
                       f"{', '.join(fallbacks[:5])}")
        risks = report.get("format_risks", [])
        if risks:
            st.warning(f"{len(risks)} format risk(s). Run verify to confirm.")
        remaining = report.get("opaque_remaining", [])
        if remaining:
            st.error(f"{len(remaining)} opaque table(s) could not be converted.")


# ── single-mode thread targets ─────────────────────────────────────────────


def _single_run_pdf(job: LexoidJob, argv: list[str], log_path: Path) -> None:
    try:
        rc = lexoid_run(job, argv, log_path)
        st.session_state.pdf_status = (
            "done" if rc == 0 else
            "interrupted" if rc in (124, 130, 137) else "failed"
        )
        st.session_state.pdf_rc = rc
    except Exception as exc:
        st.session_state.pdf_status = "error"
        with open(log_path, "a") as log:
            log.write(f"\n[ERROR] {exc}\n")


def _single_run_tex(argv: list[str], log_path: Path) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as log:
            log.write(f"$ {sys.executable} -m texopt.optimization.cli {' '.join(argv)}\n".encode())
            log.flush()
            proc = subprocess.Popen(
                [sys.executable, "-m", "texopt.optimization.cli"] + argv,
                stdout=log, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT),
            )
            rc = proc.wait()
        st.session_state.tex_status = (
            "done" if rc == 0 else "degraded" if rc == 2 else
            "unconvertible" if rc == 3 else "failed"
        )
        st.session_state.tex_rc = rc
    except Exception as exc:
        st.session_state.tex_status = "error"
        with open(log_path, "a") as log:
            log.write(f"\n[ERROR] {exc}\n")


# ── background runners ─────────────────────────────────────────────────────

def _run_pdf_in_thread(
    job: LexoidJob,
    argv: list[str],
    log_path: Path,
    item: QItem,
    queue: list[QItem],
    q_key: str,
    idx_key: str,
) -> None:
    """Background thread target for a PDF queue item."""
    try:
        rc = lexoid_run(job, argv, log_path)
        item.rc = rc
        item.status = (
            DONE if rc == 0 else
            INTERRUPTED if rc in (124, 130, 137) else
            FAILED
        )
    except Exception as exc:
        item.status = FAILED
        item.rc = -1
        with open(log_path, "a") as log:
            log.write(f"\n[ERROR] {exc}\n")
    # signal main thread to check and advance
    st.session_state[f"{q_key}_advance"] = True


def _run_tex_in_thread(
    argv: list[str],
    log_path: Path,
    item: QItem,
    queue: list[QItem],
    q_key: str,
    idx_key: str,
) -> None:
    """Background thread target for a TeX queue item."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as log:
            log.write(f"$ {sys.executable} -m texopt.optimization.cli {' '.join(argv)}\n".encode())
            log.flush()
            proc = subprocess.Popen(
                [sys.executable, "-m", "texopt.optimization.cli"] + argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT_ROOT),
            )
            rc = proc.wait()
        item.rc = rc
        item.status = DONE if rc == 0 else FAILED
    except Exception as exc:
        item.status = FAILED
        item.rc = -1
        with open(log_path, "a") as log:
            log.write(f"\n[ERROR] {exc}\n")
    st.session_state[f"{q_key}_advance"] = True


# ── queue advance logic ────────────────────────────────────────────────────

def _advance_queue(q_key: str, idx_key: str, thread_key: str) -> None:
    """
    Called on every rerun.  If the current item finished, move to the next.
    If all items are done, mark the batch as complete.
    """
    queue: list[QItem] = st.session_state[q_key]
    idx: int = st.session_state[idx_key]
    thread = st.session_state.get(thread_key)

    if not queue or idx >= len(queue):
        return

    item = queue[idx]

    # If the current item is still running, check the thread
    if item.status == RUNNING:
        if thread is not None and not thread.is_alive():
            # Thread ended but status wasn't updated yet — wait for signal
            if st.session_state.get(f"{q_key}_advance"):
                st.session_state[f"{q_key}_advance"] = False
                _move_to_next(q_key, idx_key, thread_key)
            return
        # Still running — keep polling
        return

    # Current item is in a terminal state — advance
    if st.session_state.get(f"{q_key}_advance"):
        st.session_state[f"{q_key}_advance"] = False
        _move_to_next(q_key, idx_key, thread_key)


def _move_to_next(q_key: str, idx_key: str, thread_key: str) -> None:
    """Advance to the next queue item and start it."""
    queue: list[QItem] = st.session_state[q_key]
    idx = st.session_state[idx_key] + 1
    st.session_state[idx_key] = idx
    st.session_state[thread_key] = None

    if idx < len(queue):
        # Start next item
        item = queue[idx]
        _start_item(q_key, idx_key, thread_key, item, idx)


def _start_item(
    q_key: str, idx_key: str, thread_key: str,
    item: QItem, idx: int,
) -> None:
    """Launch a background thread for the given queue item."""
    item.status = RUNNING
    item.start_time = time.time()

    if q_key == "pdf_queue":
        # Reconstruct the job from stored info
        job = _make_pdf_job(item.filename, st.session_state.get("pdf_model", DEFAULT_MODEL),
                            st.session_state.get("pdf_start_page", 1))
        log_path = LOG_DIR / f"pdf-{item.id}.log"
        item.log_path = str(log_path)
        item.job_id = job.job_id

        t = threading.Thread(
            target=_run_pdf_in_thread,
            args=(job, job.argv(), log_path, item,
                  st.session_state[q_key], q_key, idx_key),
            daemon=True,
        )
    else:
        # TeX mode
        log_path = LOG_DIR / f"tex-{item.id}.log"
        item.log_path = str(log_path)
        argv = _make_tex_argv(item.filename)

        t = threading.Thread(
            target=_run_tex_in_thread,
            args=(argv, log_path, item,
                  st.session_state[q_key], q_key, idx_key),
            daemon=True,
        )

    t.start()
    st.session_state[thread_key] = t


def _make_pdf_job(filename: str, model: str, start_page: int) -> LexoidJob:
    output_name = f"{Path(filename).stem}.tex"
    return new_job(
        host_workdir=HOST_WORK_DIR,
        input_name=filename,
        model=model,
        start_page=start_page,
        output_name=output_name,
        compose_file=COMPOSE_FILE,
    )


def _make_tex_argv(filename: str) -> list[str]:
    stem = Path(filename).stem
    input_path = str(Path(HOST_WORK_DIR) / filename)
    output_path = str(Path(HOST_WORK_DIR) / f"{stem}.opt.tex")
    report_path = str(Path(HOST_WORK_DIR) / f"{stem}.report.json")
    registry_path = str(Path(HOST_WORK_DIR) / f"{stem}.fields.json")

    argv = [
        "optimise", input_path,
        "-o", output_path,
        "--start-page", str(st.session_state.get("tex_start_page", 1)),
        "--registry", registry_path,
        "--report", report_path,
    ]
    if st.session_state.get("tex_no_llm"):
        argv.append("--no-llm")
    else:
        argv += ["--llm-model",
                 st.session_state.get("tex_llm_model", os.environ.get("TEXOPT_MODEL", ""))]
    if st.session_state.get("tex_allow_opaque"):
        argv.append("--allow-opaque")
    return argv


# ── queue display ──────────────────────────────────────────────────────────

def _display_queue(q_key: str, idx_key: str, thread_key: str,
                   label: str = "Queue") -> None:
    """Render the queue list with status icons and the current item's log."""
    queue: list[QItem] = st.session_state[q_key]
    if not queue:
        return

    idx = st.session_state[idx_key]

    # ── summary bar ────────────────────────────────────────────────
    n_done = sum(1 for it in queue if it.status == DONE)
    n_fail = sum(1 for it in queue if it.status in (FAILED, INTERRUPTED))
    n_total = len(queue)
    st.markdown(f"**{label}** — {n_done}/{n_total} done"
                + (f", {n_fail} failed" if n_fail else ""))

    # ── queue list ─────────────────────────────────────────────────
    rows = []
    for i, it in enumerate(queue):
        icon = _STATUS_ICON.get(it.status, "❓")
        marker = " ◀ **current**" if i == idx and it.status == RUNNING else ""
        rows.append(f"{icon} `{it.filename}`{marker}")
    st.markdown("  \n".join(rows))

    # ── current item log ───────────────────────────────────────────
    if 0 <= idx < len(queue):
        current = queue[idx]
        if current.status == RUNNING and current.log_path:
            st.code(
                _read_log_tail(current.log_path) or "Waiting for output…",
                language="text",
            )
        elif current.status in (FAILED, INTERRUPTED) and current.log_path:
            with st.expander(f"📜 Error log — {current.filename}", expanded=True):
                st.code(_read_log_tail(current.log_path), language="text")
                if current.rc is not None:
                    st.error(f"Exit code: {current.rc}")
        elif current.status == DONE and current.log_path:
            with st.expander(f"📜 Log — {current.filename}", expanded=False):
                st.code(_read_log_tail(current.log_path, 500_000), language="text")

    # ── batch complete ─────────────────────────────────────────────
    if idx >= len(queue) and all(
        it.status in (DONE, FAILED, SKIPPED) for it in queue
    ):
        st.success(f"🎉 Batch complete! {n_done}/{n_total} succeeded.")


# ═══════════════════════════════════════════════════════════════════════════
# session state initialisation
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULTS = {
    # Single-file state (kept for backward compat)
    "pdf_status": "idle",
    "pdf_job": None,
    "pdf_thread": None,
    "pdf_log_path": None,
    "pdf_shards": [],
    "pdf_uploaded_name": None,
    "pdf_rc": None,
    "pdf_start_time": None,
    "tex_status": "idle",
    "tex_thread": None,
    "tex_log_path": None,
    "tex_uploaded_name": None,
    "tex_rc": None,
    "tex_start_time": None,
    # Batch queue state
    "pdf_queue": [],
    "pdf_q_idx": 0,
    "pdf_q_thread": None,
    "pdf_q_advance": False,
    "pdf_batch_started": False,
    "tex_queue": [],
    "tex_q_idx": 0,
    "tex_q_thread": None,
    "tex_q_advance": False,
    "tex_batch_started": False,
    # JSON extraction state
    "json_results": {},       # filename -> ExtractionReport
    "json_files_saved": [],   # list of saved filenames
}

for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ═══════════════════════════════════════════════════════════════════════════
# page
# ═══════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="Lexiod Pipeline", page_icon="📄", layout="wide")
st.title("📄 Lexiod Pipeline")

tab_pdf, tab_tex, tab_json = st.tabs(["PDF → LaTeX", "TeX Optimizer", "JSON Extract"])

workdir = HOST_WORK_DIR

# ──────────────────────────────────────────────────────────────────────────
# TAB 1 — PDF → LaTeX
# ──────────────────────────────────────────────────────────────────────────

with tab_pdf:
    st.header("PDF → LaTeX Conversion")

    # ── shared config ──────────────────────────────────────────────
    col1, col2, col3 = st.columns(3)
    with col1:
        pdf_model = st.text_input("Model", value=DEFAULT_MODEL, key="pdf_model")
    with col2:
        pdf_start_page = st.number_input(
            "Start page", min_value=1, value=1, step=1, key="pdf_start_page",
        )
    with col3:
        pdf_mode = st.radio("Mode", ["Single", "Batch Queue"],
                            horizontal=True, key="pdf_mode", label_visibility="collapsed")

    # ───────────────────────────────────────────────────────────────
    # BATCH MODE
    # ───────────────────────────────────────────────────────────────
    if pdf_mode == "Batch Queue":

        if not st.session_state.pdf_batch_started:
            pdf_files = st.file_uploader(
                "Upload PDFs",
                type=["pdf"],
                accept_multiple_files=True,
                key="pdf_batch_uploader",
                help=f"Files saved to {workdir}",
            )

            if pdf_files:
                st.info(f"**{len(pdf_files)} file(s) selected.** "
                        f"Configure model/start-page above, then start the queue.")

                col_a, col_b = st.columns(2)
                with col_a:
                    if st.button("▶ Start Batch", key="pdf_batch_start", type="primary"):
                        # Save all files to disk
                        queue: list[QItem] = []
                        for f in pdf_files:
                            _save_upload(f, workdir)
                            queue.append(QItem(
                                id=f"{int(time.time())}-{uuid.uuid4().hex[:6]}",
                                filename=f.name,
                            ))
                        st.session_state.pdf_queue = queue
                        st.session_state.pdf_q_idx = 0
                        st.session_state.pdf_batch_started = True
                        # Start first item
                        if queue:
                            _start_item("pdf_queue", "pdf_q_idx", "pdf_q_thread",
                                        queue[0], 0)
                        st.rerun()

                with col_b:
                    if st.button("🗑 Clear", key="pdf_batch_clear"):
                        st.session_state.pdf_queue = []
                        st.session_state.pdf_q_idx = 0
                        st.rerun()

        # ── running / completed batch display ──────────────────────
        if st.session_state.pdf_batch_started:
            # Advance queue on every rerun
            _advance_queue("pdf_queue", "pdf_q_idx", "pdf_q_thread")
            _display_queue("pdf_queue", "pdf_q_idx", "pdf_q_thread",
                           label="PDF Batch Queue")

            # Auto-rerun while batch is active
            idx = st.session_state.pdf_q_idx
            queue = st.session_state.pdf_queue
            if idx < len(queue) and queue[idx].status == RUNNING:
                time.sleep(0.5)
                st.rerun()

            # Reset button after completion
            if st.session_state.pdf_q_idx >= len(st.session_state.pdf_queue):
                if st.button("🔄 New Batch", key="pdf_batch_reset"):
                    st.session_state.pdf_queue = []
                    st.session_state.pdf_q_idx = 0
                    st.session_state.pdf_batch_started = False
                    st.session_state.pdf_q_thread = None
                    st.rerun()

    # ───────────────────────────────────────────────────────────────
    # SINGLE MODE (original behavior)
    # ───────────────────────────────────────────────────────────────
    else:
        st.caption("Upload a single PDF to convert via the Lexoid Docker service.")
        pdf_file = st.file_uploader(
            "Upload PDF", type=["pdf"], key="pdf_uploader",
            help=f"File saved to {workdir}",
        )
        pdf_output = st.text_input(
            "Output filename", value="",
            placeholder="auto (same stem as input)", key="pdf_output",
        )

        if pdf_file is not None and st.session_state.pdf_uploaded_name != pdf_file.name:
            name = _save_upload(pdf_file, workdir)
            st.session_state.pdf_uploaded_name = name
            st.session_state.pdf_status = "idle"
            st.session_state.pdf_job = None
            st.session_state.pdf_shards = []
            st.session_state.pdf_rc = None

        if st.session_state.pdf_uploaded_name:
            st.success(f"Saved to `{workdir}/{st.session_state.pdf_uploaded_name}`")

        can_run = (
            st.session_state.pdf_uploaded_name is not None
            and st.session_state.pdf_status in ("idle", "done", "failed", "error", "interrupted")
        )

        if can_run and st.button("▶ Run Lexoid", key="pdf_run_btn", type="primary"):
            input_name = st.session_state.pdf_uploaded_name
            output_name = pdf_output.strip() or f"{Path(input_name).stem}.tex"
            job = new_job(
                host_workdir=HOST_WORK_DIR, input_name=input_name,
                model=pdf_model, start_page=pdf_start_page,
                output_name=output_name, compose_file=COMPOSE_FILE,
            )
            log_path = LOG_DIR / f"pdf-{job.job_id}.log"
            st.session_state.pdf_job = job
            st.session_state.pdf_log_path = log_path
            st.session_state.pdf_status = "running"
            st.session_state.pdf_shards = []
            st.session_state.pdf_rc = None
            st.session_state.pdf_start_time = time.time()
            t = threading.Thread(
                target=_single_run_pdf, args=(job, job.argv(), log_path), daemon=True,
            )
            t.start()
            st.session_state.pdf_thread = t
            st.rerun()

        # Command preview
        if st.session_state.pdf_status == "idle" and st.session_state.pdf_uploaded_name:
            input_name = st.session_state.pdf_uploaded_name
            output_name = pdf_output.strip() or f"{Path(input_name).stem}.tex"
            preview_job = new_job(
                host_workdir=HOST_WORK_DIR, input_name=input_name,
                model=pdf_model, start_page=pdf_start_page,
                output_name=output_name, compose_file=COMPOSE_FILE,
            )
            with st.expander("📋 Command preview"):
                st.code(preview_job.preview(), language="bash")

        # Status + log
        _display_single_status("pdf")

        # Resume for interrupted single runs
        if st.session_state.pdf_status == "interrupted" and st.session_state.pdf_job:
            job: LexoidJob = st.session_state.pdf_job
            pages = scan_pages(job.host_output)
            if pages:
                st.warning(f"⚠️ Interrupted at page ~{pages[-1][0]}.")
                if st.button("🔄 Resume", key="pdf_resume_btn"):
                    resume_page = truncate_to_last_complete_page(job.host_output)
                    if resume_page:
                        shard_name = f"{Path(job.output_name).stem}.part{resume_page:04d}.tex"
                        shard_argv, _ = resume_argv(job)
                        log_path = st.session_state.pdf_log_path
                        st.session_state.pdf_status = "running"
                        st.session_state.pdf_shards.append(Path(HOST_WORK_DIR) / shard_name)
                        t = threading.Thread(
                            target=_single_run_pdf,
                            args=(job, shard_argv, log_path), daemon=True,
                        )
                        t.start()
                        st.session_state.pdf_thread = t
                        st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# TAB 2 — TeX Optimizer
# ──────────────────────────────────────────────────────────────────────────

with tab_tex:
    st.header("TeX Optimizer")

    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        tex_start_page = st.number_input(
            "Start page", min_value=1, value=1, step=1, key="tex_start_page",
        )
    with col2:
        tex_no_llm = st.checkbox("Skip LLM naming", key="tex_no_llm")
        tex_allow_opaque = st.checkbox("Allow opaque (--allow-opaque)", key="tex_allow_opaque")
    with col3:
        tex_llm_model = st.text_input(
            "LLM model", value=os.environ.get("TEXOPT_MODEL", ""),
            key="tex_llm_model",
        )

    tex_mode = st.radio("Mode", ["Single", "Batch Queue"],
                        horizontal=True, key="tex_mode", label_visibility="collapsed")

    # ───────────────────────────────────────────────────────────────
    # BATCH MODE
    # ───────────────────────────────────────────────────────────────
    if tex_mode == "Batch Queue":

        if not st.session_state.tex_batch_started:
            tex_files = st.file_uploader(
                "Upload TeX files",
                type=["tex", "ltx"],
                accept_multiple_files=True,
                key="tex_batch_uploader",
                help=f"Files saved to {workdir}",
            )

            if tex_files:
                st.info(f"**{len(tex_files)} file(s) selected.**")

                col_a, col_b = st.columns(2)
                with col_a:
                    if st.button("▶ Start Batch", key="tex_batch_start", type="primary"):
                        queue: list[QItem] = []
                        for f in tex_files:
                            _save_upload(f, workdir)
                            queue.append(QItem(
                                id=f"{int(time.time())}-{uuid.uuid4().hex[:6]}",
                                filename=f.name,
                            ))
                        st.session_state.tex_queue = queue
                        st.session_state.tex_q_idx = 0
                        st.session_state.tex_batch_started = True
                        if queue:
                            _start_item("tex_queue", "tex_q_idx", "tex_q_thread",
                                        queue[0], 0)
                        st.rerun()

                with col_b:
                    if st.button("🗑 Clear", key="tex_batch_clear"):
                        st.session_state.tex_queue = []
                        st.session_state.tex_q_idx = 0
                        st.rerun()

        if st.session_state.tex_batch_started:
            _advance_queue("tex_queue", "tex_q_idx", "tex_q_thread")
            _display_queue("tex_queue", "tex_q_idx", "tex_q_thread",
                           label="TeX Batch Queue")

            idx = st.session_state.tex_q_idx
            queue = st.session_state.tex_queue
            if idx < len(queue) and queue[idx].status == RUNNING:
                time.sleep(0.5)
                st.rerun()

            if st.session_state.tex_q_idx >= len(st.session_state.tex_queue):
                if st.button("🔄 New Batch", key="tex_batch_reset"):
                    st.session_state.tex_queue = []
                    st.session_state.tex_q_idx = 0
                    st.session_state.tex_batch_started = False
                    st.session_state.tex_q_thread = None
                    st.rerun()

    # ───────────────────────────────────────────────────────────────
    # SINGLE MODE
    # ───────────────────────────────────────────────────────────────
    else:
        st.caption("Upload a `.tex` file to optimize.")
        tex_file = st.file_uploader(
            "Upload TeX", type=["tex", "ltx"], key="tex_uploader",
            help=f"File saved to {workdir}",
        )

        if tex_file is not None and st.session_state.tex_uploaded_name != tex_file.name:
            name = _save_upload(tex_file, workdir)
            st.session_state.tex_uploaded_name = name
            st.session_state.tex_status = "idle"
            st.session_state.tex_rc = None

        if st.session_state.tex_uploaded_name:
            st.success(f"Saved to `{workdir}/{st.session_state.tex_uploaded_name}`")

        can_run_tex = (
            st.session_state.tex_uploaded_name is not None
            and st.session_state.tex_status in ("idle", "done", "failed", "error",
                                                 "degraded", "unconvertible")
        )

        if can_run_tex and st.button("▶ Run Optimizer", key="tex_run_btn", type="primary"):
            input_name = st.session_state.tex_uploaded_name
            argv = _make_tex_argv(input_name)
            log_path = LOG_DIR / f"tex-{Path(input_name).stem}-{int(time.time())}.log"
            st.session_state.tex_log_path = log_path
            st.session_state.tex_status = "running"
            st.session_state.tex_rc = None
            st.session_state.tex_start_time = time.time()
            t = threading.Thread(
                target=_single_run_tex, args=(argv, log_path), daemon=True,
            )
            t.start()
            st.session_state.tex_thread = t
            st.rerun()

        # Command preview
        if st.session_state.tex_status == "idle" and st.session_state.tex_uploaded_name:
            input_name = st.session_state.tex_uploaded_name
            preview_argv = [sys.executable, "-m", "texopt.optimization.cli"] + _make_tex_argv(input_name)
            with st.expander("📋 Command preview"):
                st.code(" ".join(preview_argv), language="bash")

        _display_single_status("tex")

        # Report summary for single mode
        if (st.session_state.tex_status in ("done", "degraded")
                and st.session_state.tex_uploaded_name):
            stem = Path(st.session_state.tex_uploaded_name).stem
            report_file = Path(workdir) / f"{stem}.report.json"
            if report_file.exists():
                _show_tex_report(report_file)


# ──────────────────────────────────────────────────────────────────────────
# TAB 3 — JSON Extract
# ──────────────────────────────────────────────────────────────────────────

with tab_json:
    st.header("JSON Field Extractor")
    st.caption(
        "Upload optimized `.tex` file(s) to extract field IDs and values into JSON. "
        "Parses `% #VALUE_ID`, `% #FIELD_VALUE`, `\\fieldvalue{}`, and `\\hwfield{}{}` markers."
    )

    json_mode = st.radio("Mode", ["Single", "Batch"],
                         horizontal=True, key="json_mode", label_visibility="collapsed")

    if json_mode == "Single":
        json_file = st.file_uploader(
            "Upload optimized TeX", type=["tex", "ltx"],
            key="json_uploader", help=f"File saved to {workdir}",
        )
        use_registry = st.checkbox(
            "Merge fields.json registry metadata (if available)",
            key="json_use_registry", value=True,
        )

        if json_file is not None:
            _save_upload(json_file, workdir)
            fname = json_file.name
            stem = Path(fname).stem
            tex_path = Path(workdir) / fname
            reg_path = Path(workdir) / f"{stem}.fields.json" if use_registry else None

            if st.button("🔍 Extract Fields", key="json_extract_btn", type="primary"):
                with st.spinner("Extracting fields…"):
                    report = extract_fields(tex_path, registry_path=reg_path)
                    out_path = Path(workdir) / f"{stem}.extracted.json"
                    write_json(report, out_path)

                st.success(
                    f"✅ Extracted **{report.total_fields}** fields "
                    f"({report.handwritten_count} handwritten) "
                    f"across **{report.total_pages}** pages."
                )

                if report.warnings:
                    for w in report.warnings:
                        st.warning(w)

                # Summary metrics
                c1, c2, c3 = st.columns(3)
                c1.metric("Total Fields", report.total_fields)
                c2.metric("Handwritten", report.handwritten_count)
                c3.metric("Pages", report.total_pages)

                # Fields table
                if report.fields:
                    st.subheader("Extracted Fields")
                    rows = []
                    for f in report.fields:
                        rows.append({
                            "Field ID": f.field_id,
                            "Label": f.label,
                            "Value": f.value,
                            "Page": f.page,
                            "HW": "✓" if f.is_handwritten else "",
                            "Table": f.table,
                        })
                    st.dataframe(rows, use_container_width=True, hide_index=True)

                # JSON preview
                with st.expander("📋 JSON Preview"):
                    st.json([
                        {
                            "field_id": f.field_id,
                            "label": f.label,
                            "value": f.value,
                            "page": f.page,
                            "is_handwritten": f.is_handwritten,
                        }
                        for f in report.fields[:20]
                    ])
                    if len(report.fields) > 20:
                        st.info(f"Showing first 20 of {len(report.fields)} fields. "
                                f"Full JSON saved to `{out_path.name}`")

    else:
        # ── Batch mode ─────────────────────────────────────────────
        json_files = st.file_uploader(
            "Upload optimized TeX files",
            type=["tex", "ltx"],
            accept_multiple_files=True,
            key="json_batch_uploader",
            help=f"Files saved to {workdir}",
        )
        use_registry_batch = st.checkbox(
            "Merge fields.json registry metadata (if available)",
            key="json_use_registry_batch", value=True,
        )

        if json_files:
            st.info(f"**{len(json_files)} file(s) ready for extraction.**")

            if st.button("🔍 Extract All", key="json_batch_extract", type="primary"):
                reports = []
                progress = st.progress(0)
                for i, f in enumerate(json_files):
                    _save_upload(f, workdir)
                    stem = Path(f.name).stem
                    tex_path = Path(workdir) / f.name
                    reg_path = (Path(workdir) / f"{stem}.fields.json"
                                if use_registry_batch else None)
                    report = extract_fields(tex_path, registry_path=reg_path)
                    out_path = Path(workdir) / f"{stem}.extracted.json"
                    write_json(report, out_path)
                    reports.append(report)
                    progress.progress((i + 1) / len(json_files))

                # Merged output
                merged_path = Path(workdir) / "merged_extracted.json"
                merge_reports(reports, merged_path)

                total_fields = sum(r.total_fields for r in reports)
                total_hw = sum(r.handwritten_count for r in reports)
                st.success(
                    f"🎉 Extracted **{total_fields}** fields "
                    f"({total_hw} handwritten) from **{len(reports)}** texopt. "
                    f"Merged JSON: `{merged_path.name}`"
                )

                # Per-file summary table
                st.subheader("Per-File Summary")
                summary_rows = []
                for r in reports:
                    summary_rows.append({
                        "File": r.source,
                        "Fields": r.total_fields,
                        "Handwritten": r.handwritten_count,
                        "Pages": r.total_pages,
                    })
                st.dataframe(summary_rows, use_container_width=True, hide_index=True)

                # All fields preview
                with st.expander("📋 All Extracted Fields (preview)"):
                    all_rows = []
                    for r in reports:
                        for f in r.fields[:50]:
                            all_rows.append({
                                "Source": r.source,
                                "Field ID": f.field_id,
                                "Label": f.label,
                                "Value": f.value,
                                "Page": f.page,
                                "HW": "✓" if f.is_handwritten else "",
                            })
                    st.dataframe(all_rows, use_container_width=True, hide_index=True)
