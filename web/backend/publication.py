"""Opt-in add-only publishing through the existing publisher and its shared lock."""

import argparse
import fcntl
import json
import shutil
from pathlib import Path
from .store import Store, load_config, safe_relative, atomic_json, uid
from scripts.overleaf_publish import (
    load_config as publisher_config,
    ensure_checkout,
    publish_batch,
)


def request_publication(store, job, action):
    config_path = store.config.get("overleaf_config")
    if not config_path:
        raise ValueError("尚未配置 Overleaf；现有发布方式不受影响")
    if action not in ("preflight", "publish"):
        raise ValueError("未知发布操作")
    if job["status"] != "succeeded":
        raise ValueError("仅可发布已成功完成的任务")
    batch = safe_relative(job["batch"])
    if "/" in batch:
        raise ValueError("批次号不能包含目录分隔符")
    conf = publisher_config(Path(config_path))
    if job["unit"] not in conf["projects"]:
        raise ValueError("该单元尚未启用 Overleaf 发布")
    tex = [
        a
        for a in job["artifacts"]
        if a["kind"] == "tex" and a["attempt"] == job["attempt"]
    ]
    if not tex:
        raise ValueError("当前任务没有可发布的 TeX 产物")
    return store.save(
        "publication",
        {
            "id": uid(),
            "job": job["id"],
            "action": action,
            "status": "queued",
            "batch": batch,
            "unit": job["unit"],
            "artifacts": tex,
        },
    )


def execute(store, request):
    original = publisher_config(Path(store.config["overleaf_config"]))
    folder = store.root / "publishing" / request["id"]
    source = folder / "source"
    batch = source / request["batch"]
    batch.mkdir(parents=True, exist_ok=True)
    for artifact in request["artifacts"]:
        # Retain the original file stem, not the internal '.optimized' implementation name.
        target = (
            batch / Path(store.job(request["job"])["name"]).with_suffix(".tex").name
        )
        shutil.copyfile(store.root / artifact["path"], target)
    project = {**original["projects"][request["unit"]], "source_root": str(source)}
    config = {**original, "projects": {request["unit"]: project}}
    path = folder / "config.json"
    atomic_json(path, config)
    if request["action"] == "publish":
        return publish_batch(path, request["unit"], batch)
    lock_path = (
        Path(project["checkout"]).parent / f".{request['unit'].lower()}.publish.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        checkout = ensure_checkout(project)
        target = (checkout / project["target_root"] / request["batch"]).resolve()
        target.relative_to(checkout.resolve())
        plan = []
        for source_file in batch.rglob("*.tex"):
            dest = target / source_file.relative_to(batch)
            state = (
                "add"
                if not dest.exists()
                else (
                    "unchanged"
                    if dest.read_bytes() == source_file.read_bytes()
                    else "conflict"
                )
            )
            plan.append({"file": source_file.name, "action": state})
        return {
            "status": (
                "conflict" if any(p["action"] == "conflict" for p in plan) else "ready"
            ),
            "target": str(Path(project["target_root"]) / request["batch"]),
            "files": plan,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    store = Store(load_config(args.config))
    request = store.record("publication", args.request)
    try:
        result = execute(store, request)
        request.update(status="succeeded", result=result)
    except Exception as exc:
        # Git stderr may contain remote URLs, so do not persist it in Web events.
        request.update(
            status="failed",
            error=type(exc).__name__
            + "：发布未完成，请检查服务器发布配置、凭据或同名冲突",
        )
    store.save("publication", request)


if __name__ == "__main__":
    main()
