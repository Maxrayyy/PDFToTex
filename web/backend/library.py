"""Read-only catalog of explicitly configured legacy output roots."""

import hashlib
from pathlib import Path
import re
from .store import uid


def catalog(store):
    records = []
    for unit, directory in store.config.get("legacy_artifact_roots", {}).items():
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.tex")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.resolve().relative_to(root)
            except ValueError:
                continue
            info = path.stat()
            ident = hashlib.sha256(
                f"{unit}:{path}:{info.st_mtime_ns}:{info.st_size}".encode()
            ).hexdigest()[:32]
            batch = next(
                (p for p in relative.parts if re.match(r"^A\d+Z\d+", p)),
                relative.parent.name,
            )
            records.append(
                {
                    "id": ident,
                    "unit": unit,
                    "batch": store.config.get("batch_aliases", {}).get(batch, batch),
                    "name": relative.as_posix(),
                    "size": info.st_size,
                    "updated_at": info.st_mtime,
                    "path": str(path),
                }
            )
    return records


def entry(store, ident):
    result = next((r for r in catalog(store) if r["id"] == ident), None)
    if result is None:
        raise KeyError(ident)
    return result


def compile_copy(store, ident, request_id):
    record = entry(store, ident)
    source = Path(record["path"])
    upload = store.upload_create({"path": source.name, "size": source.stat().st_size})
    import shutil

    shutil.copyfile(source, store.upload_path(upload["id"]))
    store.finish_upload(upload["id"])
    return store.submit(
        {
            "kind": "tex_compile",
            "uploads": [upload["id"]],
            "main_file": source.name,
            "unit": record["unit"],
            "batch": record["batch"],
            "request_id": request_id or uid(),
        }
    )
