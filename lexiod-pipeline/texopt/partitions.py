"""Assign disjoint PDF queues to containers with independent durable work roots."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

from .pdf_batch import _page_count, discover_pdfs
from .pipeline_state import PipelineState
from .stages import BatchConfig, artifact_paths, run_batch


def create_plan(source_root, output_root, publish_root, workers, *, page_counter=None):
    source_root, output_root, publish_root = (
        Path(p).resolve() for p in (source_root, output_root, publish_root))
    if workers < 1:
        raise ValueError("Worker count must be positive")
    if not source_root.is_dir():
        raise ValueError(f"Missing source directory: {source_root}")
    counter = page_counter or _page_count
    lock_path = output_root / ".state/batch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        # The original worker must be stopped before assigning its in-flight texopt.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows, unreadable, pinned = [], [], set()
        for source in discover_pdfs(source_root):
            relative = source.relative_to(source_root).as_posix()
            try:
                pages = counter(source)
            except Exception as exc:
                pages = 0
                unreadable.append({"source": relative, "error": str(exc)})
            rows.append({"source": relative, "pages": pages})
            paths = artifact_paths(source, source_root, output_root, publish_root=publish_root)
            if any(paths[key].exists() for key in ("raw", "log_dir", "optimized")):
                pinned.add(relative)
        assignments = [{"id": f"{n:02d}", "output_root": str(
            output_root if n == 1 else output_root / ".workers" / f"{n:02d}"),
            "files": [], "pages": 0} for n in range(1, workers + 1)]
        for row in sorted(rows, key=lambda r: (r["source"] not in pinned, -r["pages"], r["source"])):
            worker = assignments[0] if row["source"] in pinned else min(
                assignments, key=lambda w: (w["pages"], len(w["files"]), w["id"]))
            worker["files"].append(row["source"])
            worker["pages"] += row["pages"]
        for worker in assignments:
            worker["files"].sort()
        plan = {"schema": "pdf-partitions/v1", "created_at": datetime.now(timezone.utc).isoformat(),
                "source_root": str(source_root), "output_root": str(output_root),
                "publish_root": str(publish_root), "sources": rows, "workers": assignments,
                "pinned_to_first_worker": sorted(pinned), "unreadable": unreadable}
        validate_plan(plan)
        return plan


def validate_plan(plan):
    if plan.get("schema") != "pdf-partitions/v1" or not plan.get("workers"):
        raise ValueError("Invalid partition plan")
    source_root, output_root, publish_root = (
        Path(plan[key]).resolve() for key in ("source_root", "output_root", "publish_root"))
    if (publish_root.is_relative_to(output_root) or output_root.is_relative_to(publish_root)
            or output_root.is_relative_to(source_root) or source_root.is_relative_to(output_root)
            or publish_root.is_relative_to(source_root) or source_root.is_relative_to(publish_root)):
        raise ValueError("Source, work and publish roots must be separate")
    expected = {p.relative_to(source_root).as_posix() for p in discover_pdfs(source_root)}
    recorded = [r["source"] for r in plan["sources"]]
    assigned = []
    for n, worker in enumerate(plan["workers"], 1):
        work = output_root if n == 1 else output_root / ".workers" / f"{n:02d}"
        if worker["id"] != f"{n:02d}" or Path(worker["output_root"]).resolve() != work:
            raise ValueError("Each worker must have its own fixed work root")
        for relative in worker["files"]:
            path = Path(relative)
            if (path.is_absolute() or ".." in path.parts or path.as_posix() != relative
                    or not (source_root / path).resolve().is_relative_to(source_root)):
                raise ValueError(f"Invalid source path: {relative}")
        assigned.extend(worker["files"])
    if (len(recorded) != len(set(recorded)) or set(recorded) != expected
            or len(assigned) != len(set(assigned)) or set(assigned) != expected):
        raise ValueError("Partitions must cover every source PDF exactly once")
    if not set(plan["pinned_to_first_worker"]).issubset(plan["workers"][0]["files"]):
        raise ValueError("Existing checkpoints must stay with the first worker")
    targets = [Path(name).with_suffix(".tex") for name in assigned]
    if len(targets) != len(set(targets)):
        raise ValueError("Multiple PDFs would publish to the same TeX file")


def run_partition(plan, worker_id, *, config=None, runner=None, page_counter=None):
    validate_plan(plan)
    worker = next((w for w in plan["workers"] if w["id"] == worker_id), None)
    if worker is None:
        raise ValueError(f"Unknown worker: {worker_id}")
    config = replace(config or BatchConfig.from_env(), publish_root=plan["publish_root"])
    print(json.dumps({"worker": worker_id, "files": len(worker["files"]),
                      "pages": worker["pages"], "output_root": worker["output_root"],
                      "publish_root": config.publish_root}, ensure_ascii=False), flush=True)
    return run_batch(plan["source_root"], worker["output_root"], config,
                     runner, page_counter, include_paths=worker["files"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("plan")
    prepare.add_argument("--source-root", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--publish-root", type=Path, required=True)
    prepare.add_argument("--workers", type=int, default=4)
    prepare.add_argument("--plan", type=Path, required=True)
    for name in ("run", "status"):
        command = sub.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--worker", required=True)
    args = parser.parse_args()
    if args.command == "plan":
        if args.plan.exists():
            parser.error("Plan already exists; reuse it when restarting workers")
        if args.plan.resolve().is_relative_to(args.publish_root.resolve()):
            parser.error("Plan must stay outside the clean TeX tree")
        plan = create_plan(args.source_root, args.output_root, args.publish_root, args.workers)
        args.plan.parent.mkdir(parents=True, exist_ok=True)
        with args.plan.open("x", encoding="utf-8") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"sources": len(plan["sources"]), "unreadable": len(plan["unreadable"]),
            "workers": [{"id": w["id"], "files": len(w["files"]), "pages": w["pages"]}
                        for w in plan["workers"]]}, ensure_ascii=False, indent=2))
        return 0
    plan = json.loads(args.plan.read_text("utf-8"))
    if args.command == "run":
        return run_partition(plan, args.worker)
    validate_plan(plan)
    worker = next(w for w in plan["workers"] if w["id"] == args.worker)
    state = PipelineState(Path(worker["output_root"]) / ".state/pipeline.sqlite3")
    print(json.dumps(state.status_snapshot(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
