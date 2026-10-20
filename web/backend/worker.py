"""Single persistent dispatcher, owning only pdftotex-web-* containers."""

import argparse
import fcntl
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import os
import signal
import sys
import time
from .store import Store, load_config, atomic_json, ACTIVE, ROOT, uid


class Worker:
    def __init__(self, store):
        self.store = store
        self.config = store.config
        self._observed_versions = {}

    def docker(self, *args, check=True):
        return subprocess.run(
            [self.config["docker"], *args],
            capture_output=True,
            text=True,
            timeout=45,
            check=check,
        )

    def inspect(self, name):
        result = self.docker("inspect", name, check=False)
        if result.returncode:
            if "No such" in result.stderr:
                return None
            raise RuntimeError("Docker 状态读取失败")
        return json.loads(result.stdout)[0]["State"]

    def notify(self, job, phase):
        settings = self.config.get("email")
        if not settings:
            self.store.event(
                job["id"], event="notification", phase=phase, status="unconfigured"
            )
            return
        try:
            from texopt.monitoring.worker_watch import send_email_message

            send_email_message(
                settings,
                f"Web 任务重启 {phase}（第 {max(1,job.get('attempt',1)-1)} 次）",
                f"文件：{job['name']}\n状态：{phase}\n{job.get('error') or job.get('previous_error') or ''}",
            )
            self.store.event(
                job["id"], event="notification", phase=phase, status="sent"
            )
        except Exception as exc:
            self.store.event(
                job["id"],
                event="notification",
                phase=phase,
                status="failed",
                error=type(exc).__name__,
            )

    def launch(self, job):
        if self.store.job(job["id"])["status"] == "cancel_requested":
            self.store.patch(job["id"], status="cancelled")
            return
        folder = self.store.root / "jobs" / job["id"]
        atomic_json(
            folder / "run.json",
            {
                **job,
                "timeout": self.config["conversion_timeout"],
                "compile_timeout": self.config["compile_timeout"],
            },
        )
        (folder / "progress.json").unlink(missing_ok=True)
        args = [
            "create",
            "--name",
            job["container"],
            "--label",
            "pdftotex.web=true",
            "--memory",
            self.config["memory"],
            "--cpus",
            str(self.config["cpus"]),
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=1g",
            "--mount",
            f"type=bind,src={folder},dst=/job",
            "--mount",
            f"type=bind,src={folder / 'input'},dst=/job/input,readonly",
            "--mount",
            f'type=bind,src={ROOT / "web/backend/runner.py"},dst=/runner.py,readonly',
            "-e",
            "HOME=/tmp",
            "-e",
            "PYTHONUNBUFFERED=1",
        ]
        if job["kind"] == "tex_compile":
            args += ["--network", "none"]
        else:
            creds = {}
            for stage in ("recognize", "reconcile", "optimise", "repair", "fallback"):
                spec = job["config"].get(stage)
                if spec:
                    provider = self.store.record("provider", spec["provider"])
                    creds[provider["id"]] = {
                        **provider,
                        "key": self.store.secret(provider["id"]),
                    }
            secret_path = self.store.root / "credentials" / f"job-{job['id']}.json"
            atomic_json(secret_path, creds)
            args += [
                "--mount",
                f"type=bind,src={secret_path},dst=/run/web-credentials.json,readonly",
            ]
        args += [self.config["image"], "python", "/runner.py"]
        self.docker(*args)
        self.docker("start", job["container"])
        image_id = self.docker(
            "inspect", "--format", "{{.Image}}", job["container"]
        ).stdout.strip()
        self.store.started(job["id"], image_id)
        self.store.event(
            job["id"],
            event="container_started",
            container=job["container"],
            image_id=image_id,
        )
        if job.get("attempt", 0) > 1:
            self.notify(job, "启动成功")

    def finalize(self, job, state):
        folder = self.store.root / "jobs" / job["id"]
        result = self.docker("logs", "--tail", "4000", job["container"], check=False)
        (folder / f"attempt-{job['attempt']}.log").write_text(
            result.stdout + result.stderr
        )
        status = (
            "cancelled"
            if job["status"] == "cancel_requested"
            else "succeeded" if state["ExitCode"] == 0 else "failed"
        )
        artifacts = list(job.get("artifacts", []))
        if status == "succeeded":
            output = folder / "result" / str(job["attempt"])
            if not (output / "result.pdf").is_file():
                status = "failed"
            else:
                for path in [
                    output / "result.pdf",
                    output / "compile.log",
                    *(folder / "published").rglob("*.tex"),
                ]:
                    artifact = dict(
                        id=hashlib.sha256(
                            f"{job['id']}:{job['attempt']}:{path}".encode()
                        ).hexdigest()[:32],
                        job=job["id"],
                        name=(
                            Path(job["name"]).with_suffix(".tex").name
                            if path.suffix == ".tex"
                            else Path(job["name"]).stem + path.suffix
                        ),
                        path=str(path.relative_to(self.store.root)),
                        kind=path.suffix[1:],
                        attempt=job["attempt"],
                        created_at=time.time(),
                    )
                    # Published TeX is mutable across retries; capture an immutable version.
                    if path.suffix == ".tex":
                        target = output / path.name
                        shutil.copyfile(path, target)
                        artifact["path"] = str(target.relative_to(self.store.root))
                    self.store.save("artifact", artifact)
                    if not any(a["id"] == artifact["id"] for a in artifacts):
                        artifacts.append(artifact)
        error = (
            "内存不足 (OOM)"
            if state.get("OOMKilled")
            else (
                None
                if status in ("succeeded", "cancelled")
                else "编译或转换失败，请查看检查结果与日志"
            )
        )
        updates = dict(
            status=status, artifacts=artifacts, error=error, finished_at=time.time()
        )
        if status == "failed":
            try:
                transient = (
                    json.loads((folder / "progress.json").read_text()).get("retryable")
                    is True
                )
            except (OSError, ValueError):
                transient = False
            count = job.get("restarts", 0)
            if (
                transient
                and not state.get("OOMKilled")
                and count < len(self.config["retry_delays"])
            ):
                updates.update(
                    status="retry_wait",
                    next_retry_at=time.time() + self.config["retry_delays"][count],
                    restarts=count + 1,
                )
            else:
                self.store.pause(True)
            if job.get("attempt", 0) > 1:
                self.notify(job, "失败")
        finished = self.store.patch(job["id"], **updates)
        from .observations import snapshot

        summary = snapshot(self.store, finished)
        self.store.patch(
            job["id"],
            summary={key: summary[key] for key in ("usage", "cost", "completed_pages")},
        )
        self.store.event(
            job["id"], event="finished", status=status, exit_code=state["ExitCode"]
        )
        self.docker("rm", job["container"], check=False)
        (self.store.root / "credentials" / f"job-{job['id']}.json").unlink(
            missing_ok=True
        )

    def tick(self):
        atomic_json(self.store.root / "heartbeat.json", {"time": time.time()})
        jobs = self.store.jobs()
        from .observations import snapshot

        for job in jobs:
            # Finished attempts are immutable; refresh once on startup or a job
            # state change, while active jobs continuously consume new evidence.
            version = job.get("version")
            if job["status"] in ACTIVE or self._observed_versions.get(job["id"]) != version:
                snapshot(self.store, job)
                self._observed_versions[job["id"]] = version
        active = next((j for j in jobs if j["status"] in ACTIVE), None)
        text = (
            "# Web 实时监控\n\n更新时间：" + time.strftime("%Y-%m-%d %H:%M:%S") + "\n\n"
        )
        text += (
            f"容器：{active['container']}\n文件：{active['name']}\n状态：{active['status']}\n"
            if active
            else "当前无运行中的 Web 容器。\n"
        )
        temp = self.store.root / "latest.md.tmp"
        temp.write_text(text)
        temp.replace(self.store.root / "latest.md")
        for job in self.store.jobs():
            if job["status"] not in ACTIVE:
                continue
            state = self.inspect(job["container"])
            if state is None:
                if job["status"] == "starting":
                    self.launch(job)
                elif job["status"] == "cancel_requested":
                    self.store.patch(job["id"], status="cancelled")
                else:
                    self.store.patch(
                        job["id"],
                        status="failed",
                        error="容器已不存在；缓存已保留，可手动重试",
                    )
                    self.store.pause(True)
            elif state["Status"] == "created":
                if job["status"] == "cancel_requested":
                    self.docker("rm", job["container"])
                    self.store.patch(job["id"], status="cancelled")
                else:
                    self.docker("start", job["container"])
                    self.store.started(job["id"], job.get("image_id", ""))
            elif state["Running"]:
                if job["status"] == "cancel_requested":
                    self.docker("stop", "--time", "10", job["container"])
                elif job["status"] == "starting":
                    self.store.started(job["id"], job.get("image_id", ""))
                elif time.time() - job["started_at"] > (
                    self.config["compile_timeout"] * 2 + 60
                    if job["kind"] == "tex_compile"
                    else self.config["conversion_timeout"]
                ):
                    self.docker("kill", job["container"])
                    self.store.event(job["id"], event="timeout")
            else:
                self.finalize(job, state)
            return
        for request in self.store.records("publication"):
            if request["status"] not in ("queued", "running"):
                continue
            request["status"] = "running"
            self.store.save("publication", request)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "web.backend.publication",
                    "--config",
                    self.config["config_file"],
                    "--request",
                    request["id"],
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            started = time.monotonic()
            while proc.poll() is None:
                atomic_json(
                    self.store.root / "heartbeat.json",
                    {"time": time.time(), "publishing": request["id"]},
                )
                if time.monotonic() - started > 180:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    break
                time.sleep(1)
            latest = self.store.record("publication", request["id"])
            if latest["status"] == "running":
                latest.update(
                    status="failed", error="发布进程中断或超时；再次预检后可重试"
                )
                self.store.save("publication", latest)
            return
        self.store.require_space()
        job = self.store.claim()
        if job:
            if job.get("attempt", 0) > 1:
                self.notify(
                    job,
                    "失败后准备重启" if job.get("previous_error") else "准备重新执行",
                )
            try:
                self.launch(job)
            except subprocess.CalledProcessError:
                # Inspect before deciding failure; an ambiguous start can have succeeded.
                state = self.inspect(job["container"])
                if state is not None and state.get("Running"):
                    self.store.started(job["id"], "")
                    return
                if state is not None and state.get("Status") == "created":
                    self.docker("rm", job["container"])
                self.store.patch(
                    job["id"],
                    status="failed",
                    error="容器启动失败，请检查镜像与 Docker 配置",
                )
                self.store.pause(True)
                if job.get("attempt", 0) > 1:
                    self.notify(job, "启动失败")
                self.store.event(
                    job["id"],
                    event="docker_start_error",
                    error="请检查镜像与 Docker 状态",
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    store = Store(load_config(args.config))
    worker = Worker(store)
    with (store.root / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                worker.tick()
            except Exception as exc:
                atomic_json(
                    store.root / "heartbeat.json",
                    {"time": time.time(), "error": str(exc)[:300]},
                )
            if args.once:
                break
            time.sleep(store.config["poll_seconds"])


if __name__ == "__main__":
    main()
