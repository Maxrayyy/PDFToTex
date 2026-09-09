"""Resume these approved batches with explicit provenance for reused old pages."""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import shutil
import sys

from lexoid.core.recognition.cache import RecognitionCache, build_cache_key
from lexoid.core.recognition.models import RecognitionConfig
from lexoid.core.recognition.service import PageRecognizer
from lexoid.core.recognition.vision import PROMPT_VERSION
from texopt.stages import BatchConfig, _run, artifact_paths, run_batch
from texopt.textio import write_utf8_atomic


def save(path, data):
    write_utf8_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-key", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(str(args.source))
    total = len(document)
    document.close()
    config = BatchConfig.from_env()
    if (config.vision_model, config.fallback_model, config.render_dpi,
            config.vision_concurrency) != ("gpt-5.6-sol", "gpt-6-astra", 240, 2):
        raise ValueError("Resume configuration differs from the approved settings")
    recognition = RecognitionConfig(ocr="none", initial_render_dpi=240,
        retry_crop_dpi=480, vision_concurrency=2, max_page_attempts=1,
        min_output_tokens=8192, enable_vl_fallback=False, reasoning_effort="none")
    versions = {"adapter_config": "layout-only-v2-arm-static-ir"}
    for name in ("paddleocr", "paddlex", "paddlepaddle"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "unavailable"
    source_hash = hashlib.sha256(args.source.read_bytes()).hexdigest()
    key_args = (versions, f"{PROMPT_VERSION}:orient=True", config.vision_model)
    legacy_key = build_cache_key(source_hash, replace(recognition, reasoning_effort=None), *key_args)
    if legacy_key != args.legacy_key:
        raise ValueError("Legacy cache does not match source PDF and recognition configuration")
    new_key = build_cache_key(source_hash, recognition, *key_args)
    cache_root = args.output / ".cache" / "recognition"
    legacy = RecognitionCache(cache_root, legacy_key)
    current = RecognitionCache(cache_root, new_key)
    reader = PageRecognizer(recognition, legacy, total, None, None, None, None)
    manifest_path = args.output / "resume-none.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (manifest["source_sha256"], manifest["legacy_key"], manifest["new_key"]) != (
                source_hash, legacy_key, new_key):
            raise ValueError("Existing resume manifest belongs to another configuration")
    else:
        pages = sorted(int(p.stem) for p in (cache_root / "draft" / legacy_key).glob("*.json"))
        if not pages or any(reader.cached_page(page) is None for page in pages):
            raise ValueError("Legacy draft validation failed")
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
            "source": str(args.source), "source_sha256": source_hash, "total_pages": total,
            "legacy_key": legacy_key, "new_key": new_key, "reused_pages": pages,
            "reused_reasoning_effort": None, "new_reasoning_effort": "none",
            "reason": "Explicitly preserve completed pages while switching future calls to none",
            "source_hashes": {str(page): {
                suffix: hashlib.sha256(legacy.path("draft", page, suffix).read_bytes()).hexdigest()
                for suffix in (".json", ".tex")} for page in pages}}
        save(manifest_path, manifest)
    for page in manifest["reused_pages"]:
        for suffix in (".json", ".tex"):
            path = legacy.path("draft", page, suffix)
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["source_hashes"][str(page)][suffix]:
                raise ValueError("Legacy page changed after resume was prepared")
        existing = current.load_json("draft", page)
        if existing is not None:
            if (existing["latex"], existing["evidence"]) != (
                    legacy.load_json("draft", page)["latex"], legacy.load_json("draft", page)["evidence"]):
                raise ValueError("Current draft conflicts with approved legacy page")
            continue
        payload = legacy.load_json("draft", page)
        payload["cache_reuse_provenance"] = {"legacy_key": legacy_key,
            "original_reasoning_effort": None, "manifest": str(manifest_path)}
        current.save_text("draft", page, legacy.load_text("draft", page))
        current.save_json("draft", page, payload)
    verifier = PageRecognizer(recognition, current, total, None, None, None, None)
    if any(verifier.cached_page(page) is None for page in manifest["reused_pages"]):
        raise ValueError("Imported page validation failed")
    paths = artifact_paths(args.source, args.source.parent, args.output,
                           publish_root=config.publish_root)
    backup = args.output / "resume-before-none"
    backup.mkdir(exist_ok=True)
    for path in (paths["raw"], paths["evidence"]):
        if path.exists() and not (backup / path.name).exists():
            shutil.copyfile(path, backup / path.name)
    print(json.dumps({"source": str(args.source), "reused_pages": manifest["reused_pages"],
        "new_reasoning_effort": "none", "dpi": 240, "concurrency": 2,
        "mode": "prepared" if args.prepare_only else "resume"}), flush=True)
    if args.prepare_only:
        return 0

    def runner(stage, log):
        status = _run(stage, log, config.timeout)
        if status == 0 and stage.stage == "recognize":
            evidence = json.loads(paths["evidence"].read_text())
            evidence["resume_provenance"] = manifest
            evidence["page_recognition_settings"] = {}
            for page in range(1, total + 1):
                model = evidence["page_models"][str(page)]
                reused = page in manifest["reused_pages"]
                evidence["page_recognition_settings"][str(page)] = {
                    "model": model,
                    "reasoning_effort": "none" if model == "gpt-5.6-sol" and not reused else None,
                    "origin": "model_upgrade" if model != "gpt-5.6-sol" else
                              "previous_run" if reused else "current_run"}
            save(paths["evidence"], evidence)
        return status

    return run_batch(args.source.parent, args.output, config=config, runner=runner,
                     include={args.source.name})


if __name__ == "__main__":
    sys.exit(main())
