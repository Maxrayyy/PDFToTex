"""Move existing exports into a TeX-only tree that mirrors the source PDFs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from ..runtime.pdf_batch import discover_pdfs
from ..runtime.stages import artifact_paths, sha256_file
from ..core.textio import write_utf8_atomic


def migrate_exports(source_root, work_root, publish_root):
    """Run with workers stopped. Preflight every destination before moving texopt."""
    source_root, work_root, publish_root = map(Path, (source_root, work_root, publish_root))
    if not source_root.is_dir():
        raise ValueError(f"Source directory does not exist: {source_root}")
    planned, directories, old_directories, targets = [], set(), set(), set()
    sources = discover_pdfs(source_root)
    for source in sources:
        relative = source.relative_to(source_root)
        paths = artifact_paths(source, source_root, work_root, publish_root=publish_root)
        destination = paths["optimized"]
        if destination in targets:
            raise ValueError(f"Multiple source PDFs map to {destination}")
        targets.add(destination)
        directories.add(destination.parent)
        old_dir = work_root / relative.parent / "optimized"
        old_directories.add(old_dir)
        candidates = [(old_dir / f"{source.stem}.optimized.tex", destination, "tex")]
        for suffix in ("report.json", "fields.json", "compile.log"):
            name = f"{source.stem}.{suffix}"
            candidates.append((old_dir / name,
                paths["log_dir"] / "published-sidecars" / name, "sidecar"))
        for old, new, kind in candidates:
            if not old.is_file():
                continue
            checksum = sha256_file(old)
            if new.exists() and (not new.is_file() or sha256_file(new) != checksum):
                raise FileExistsError(f"Conflicting destination: {new}")
            planned.append({"from": str(old), "to": str(new), "kind": kind, "sha256": checksum})
    for existing in publish_root.rglob("*"):
        if existing.is_file() and not existing.name.startswith(".") and existing not in targets:
            raise ValueError(f"Unexpected file in TeX export tree: {existing}")

    for directory in sorted(directories):
        directory.mkdir(parents=True, exist_ok=True)
    for move in planned:
        old, new = Path(move["from"]), Path(move["to"])
        if sha256_file(old) != move["sha256"]:
            raise RuntimeError(f"Source changed during migration: {old}")
        new.parent.mkdir(parents=True, exist_ok=True)
        if new.exists():
            # A verified identical destination is already a retained copy.
            if sha256_file(new) != move["sha256"]:
                raise FileExistsError(new)
            old.unlink()
        else:
            shutil.move(str(old), str(new))
        if old.exists() or sha256_file(new) != move["sha256"]:
            raise RuntimeError(f"Move verification failed: {new}")
    for directory in sorted(old_directories, key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return {"source_root": str(source_root), "publish_root": str(publish_root),
            "source_pdfs": len(sources), "source_directories": len(directories),
            "tex_moved": sum(m["kind"] == "tex" for m in planned),
            "sidecars_moved": sum(m["kind"] == "sidecar" for m in planned), "moves": planned}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--publish-root", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()
    if args.audit.resolve().is_relative_to(args.publish_root.resolve()):
        parser.error("Migration audit must stay outside the TeX export tree")
    previous = json.loads(args.audit.read_text()) if args.audit.exists() else {"runs": []}
    result = migrate_exports(args.source_root, args.work_root, args.publish_root)
    result["time"] = datetime.now(timezone.utc).isoformat()
    previous["runs"].append(result)
    write_utf8_atomic(args.audit, json.dumps(previous, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "moves"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
