"""Executed inside an isolated container. Reuses the production pipeline unchanged."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path("/job")


def progress(**data):
    p = ROOT / "progress.json"
    temp = p.with_suffix(".tmp")
    temp.write_text(
        json.dumps(dict(updated_at=time.time(), **data), ensure_ascii=False)
    )
    temp.replace(p)


def compile_tex(project, main, output, timeout):
    output.mkdir(parents=True, exist_ok=True)
    main = Path(main)
    log = output / "compile.log"
    text = (project / main).read_text("utf-8", errors="replace")
    from texopt.optimization.syntax_check import validate_latex

    issues = [issue.payload() for issue in validate_latex(text)]
    (output / "checks.json").write_text(
        json.dumps({"main_file": str(main), "issues": issues}, ensure_ascii=False)
    )
    target = main.name
    ctex = re.search(
        r"\\documentclass(?:\[([^\]]*)\])?\{(ctexart|ctexbook|ctexrep)\}", text
    )
    if ctex and "fontset=" not in (ctex[1] or ""):
        target = (
            r"\PassOptionsToClass{fontset=fandol}{"
            + ctex[2]
            + r"}\input{"
            + target
            + "}"
        )
    env = {
        **os.environ,
        "TEXINPUTS": str(project) + "//:",
        "openin_any": "a",
        "openout_any": "p",
    }
    with log.open("w") as out:
        for n in (1, 2):
            progress(stage="compile", compile_pass=n)
            out.write(f"XeLaTeX pass {n}/2\n")
            out.flush()
            result = subprocess.run(
                [
                    "xelatex",
                    "-no-shell-escape",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    "-synctex=1",
                    "-jobname=result",
                    "-output-directory=" + str(output),
                    target,
                ],
                cwd=project / main.parent,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            if result.returncode:
                return result.returncode
    return 0 if (output / "result.pdf").is_file() else 1


def convert(job):
    from texopt.runtime.stages import BatchConfig, run_batch, artifact_paths

    secrets = json.loads(Path("/run/web-credentials.json").read_text())
    settings = job["config"]
    cfg = BatchConfig(
        vision_model=settings["recognize"]["model"],
        fallback_model=settings.get("fallback", {}).get("model"),
        reconcile_model=settings["reconcile"]["model"],
        optimizer_model=settings["optimise"]["model"],
        repair_model=settings.get("repair", settings["reconcile"])["model"],
        vision_concurrency=settings["page_concurrency"],
        reconcile_concurrency=settings.get("reconcile_concurrency", 1),
        semantic_naming="deferred",
        publish_root=str(ROOT / "published"),
    )
    retryable = False

    def run(stage, log):
        nonlocal retryable
        progress(stage=stage.stage)
        provider = secrets[settings[stage.stage]["provider"]]
        repair = secrets[settings.get("repair", settings["reconcile"])["provider"]]
        env = {
            **os.environ,
            "OPENAI_API_KEY": provider["key"],
            "OPENAI_BASE_URL": provider["base_url"],
            "LEXOID_MODEL_CALL_LOG": str(log.with_suffix(".calls.jsonl")),
            "TEXOPT_NAMING_PROVIDER": "openai",
            "TEXOPT_NAMING_API_KEY": provider["key"],
            "TEXOPT_NAMING_BASE_URL": provider["base_url"],
        }
        if stage.stage == "optimise":
            env.update(OPENAI_API_KEY=repair["key"], OPENAI_BASE_URL=repair["base_url"])
        argv = list(stage.argv)
        if stage.stage == "recognize" and not cfg.fallback_model:
            argv.extend(["--api", "openai"])
        log.parent.mkdir(parents=True, exist_ok=True)
        calls = log.with_suffix(".calls.jsonl")
        offset = calls.stat().st_size if calls.exists() else 0
        with log.open("a") as stream:
            status = subprocess.run(
                argv,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=job["timeout"],
            ).returncode
        events = []
        if calls.exists():
            with calls.open("rb") as stream:
                stream.seek(offset)
                for line in stream:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
        retryable = any(
            e.get("event") == "model_service_unavailable" for e in events
        ) and not any(e.get("http_status") in (401, 403) for e in events)
        return status

    source = ROOT / "input" / job["name"]
    status = run_batch(
        ROOT / "input", ROOT / "work", cfg, runner=run, include_paths={job["name"]}
    )
    if status:
        manifest = json.loads((ROOT / "work/manifest.json").read_text())
        progress(
            stage="failed",
            retryable=retryable
            and any(f.get("status") == "paused" for f in manifest["files"]),
        )
        return status
    paths = artifact_paths(
        source, ROOT / "input", ROOT / "work", publish_root=cfg.publish_root
    )
    # A standalone PDF artifact with its own two-pass log, without changing the original pipeline.
    return compile_tex(
        paths["work_optimized"].parent,
        paths["work_optimized"].name,
        ROOT / "result" / str(job["attempt"]),
        job["compile_timeout"],
    )


def main():
    job = json.loads((ROOT / "run.json").read_text())
    try:
        if job["kind"] == "tex_compile":
            status = compile_tex(
                ROOT / "input",
                job["config"]["main_file"],
                ROOT / "result" / str(job["attempt"]),
                job["compile_timeout"],
            )
        else:
            status = convert(job)
    except Exception as exc:
        # Do not print exception messages which may include credentials or provider responses.
        progress(stage="failed", error=type(exc).__name__)
        print(type(exc).__name__, flush=True)
        return 1
    previous = (
        json.loads((ROOT / "progress.json").read_text())
        if (ROOT / "progress.json").exists()
        else {}
    )
    progress(
        stage="done" if status == 0 else "failed",
        exit_code=status,
        retryable=previous.get("retryable", False),
    )
    return status


if __name__ == "__main__":
    sys.exit(main())
