import os
from pathlib import Path

from texopt import worker_watch as watch


def target(tmp_path):
    work = tmp_path / "work"
    logs = work / ".pipeline" / "sample"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "optimise.process.log").write_text(
        "optimise started\n[ERROR] [COMPILE_FAILED] Missing } inserted\n",
        encoding="utf-8",
    )
    return {
        "name": "pdftotex-u1-sample-001",
        "stem": "sample",
        "pages": 12,
        "work_root": str(work),
        "output_tex": str(tmp_path / "sample.tex"),
    }


def settings():
    return {
        "enabled": True,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "sender": "sender@example.com",
        "recipient": "recipient@example.com",
        "password_env": "PDFTOTEX_SMTP_PASSWORD",
    }


def result(*, alerts=None, auto_restart=None, exit_code=1):
    value = {
        "name": "pdftotex-u1-sample-001",
        "stem": "sample",
        "status": "exited",
        "terminal": True,
        "container_id": "container-id",
        "exit_code": exit_code,
        "started_at": "2026-09-24T01:00:00+00:00",
        "finished_at": "2026-09-24T01:10:00+00:00",
        "stage": "optimise",
        "alerts": alerts or ["container_failed"],
        "issues": [{"code": "COMPILE_FAILED", "page": 7}],
        "new_counts": {"process_errors": 1},
    }
    if auto_restart is not None:
        value["auto_restart"] = auto_restart
    return value


def capture_mail(sent):
    def send(_settings, subject, body):
        sent.append((subject, body))

    return send


def test_oom_exit_sends_detailed_email_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(watch, "docker_log_tail", lambda *_: "Killed process 42 (python)")
    item = result(alerts=["container_failed", "oom_killed"], exit_code=137)
    state = {}

    watch.process_email_alert(
        target(tmp_path), item, state, settings(), "docker", 1000,
        mailer=capture_mail(sent),
    )
    watch.process_email_alert(
        target(tmp_path), item, state, settings(), "docker", 1001,
        mailer=capture_mail(sent),
    )

    assert len(sent) == 1
    subject, body = sent[0]
    assert "OOM" in subject
    assert "pdftotex-u1-sample-001" in body
    assert "退出码: 137" in body
    assert "Killed process 42" in body
    assert "Missing } inserted" in body
    assert item["email_alert"]["status"] == "already_sent"


def test_non_api_failure_sends_immediately(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(watch, "docker_log_tail", lambda *_: "xelatex failed")
    item = result()

    watch.process_email_alert(
        target(tmp_path), item, {}, settings(), "docker", 1000,
        mailer=capture_mail(sent),
    )

    assert len(sent) == 1
    assert "非 API 异常退出" in sent[0][0]
    assert "COMPILE_FAILED" in sent[0][1]
    assert item["email_alert"]["status"] == "sent"


def test_api_failure_waits_until_restart_limit_then_sends_once(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(watch, "docker_log_tail", lambda *_: "HTTP 503 Service Unavailable")
    state = {}
    base = {
        "error_type": "InternalServerError",
        "http_status": 503,
        "page": 11,
        "stage": "recognize",
        "attempts": 6,
        "max_attempts": 6,
    }
    for status in ("waiting", "starting", "started"):
        item = result(auto_restart={**base, "status": status})
        watch.process_email_alert(
            target(tmp_path), item, state, settings(), "docker", 1000,
            mailer=capture_mail(sent),
        )
    assert sent == []

    item = result(auto_restart={**base, "status": "limit_reached"})
    watch.process_email_alert(
        target(tmp_path), item, state, settings(), "docker", 1001,
        mailer=capture_mail(sent),
    )
    watch.process_email_alert(
        target(tmp_path), item, state, settings(), "docker", 1002,
        mailer=capture_mail(sent),
    )

    assert len(sent) == 1
    assert "API 重启失败" in sent[0][0]
    assert "重启次数: 6/6" in sent[0][1]
    assert "HTTP 状态: 503" in sent[0][1]


def test_email_failure_is_recorded_and_retried_after_cooldown(tmp_path, monkeypatch):
    attempts = []
    monkeypatch.setattr(watch, "docker_log_tail", lambda *_: "failure")

    def fail(*_):
        attempts.append(1)
        raise OSError("network unavailable")

    state = {}
    item = result()
    watch.process_email_alert(
        target(tmp_path), item, state, {**settings(), "retry_seconds": 600},
        "docker", 1000, mailer=fail,
    )
    watch.process_email_alert(
        target(tmp_path), item, state, {**settings(), "retry_seconds": 600},
        "docker", 1200, mailer=fail,
    )
    watch.process_email_alert(
        target(tmp_path), item, state, {**settings(), "retry_seconds": 600},
        "docker", 1601, mailer=fail,
    )

    assert len(attempts) == 2
    assert item["email_alert"]["status"] == "send_failed"
    assert item["email_alert"]["error_type"] == "OSError"


def test_smtp_uses_password_environment_without_putting_it_in_message(monkeypatch):
    calls = []

    class FakeSmtp:
        def __init__(self, host, port, **kwargs):
            calls.append(("connect", host, port, kwargs["timeout"]))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, message):
            calls.append(("send", message))
            return {}

    monkeypatch.setenv("PDFTOTEX_SMTP_PASSWORD", "secret-value")
    monkeypatch.setattr(watch.smtplib, "SMTP_SSL", FakeSmtp)

    watch.send_email_message(settings(), "subject", "body")

    assert calls[0][:3] == ("connect", "smtp.163.com", 465)
    assert calls[1] == ("login", "sender@example.com", "secret-value")
    assert calls[2][0] == "send"
    message = calls[2][1]
    assert message["To"] == "recipient@example.com"
    assert message["Date"]
    assert message["Message-ID"]
    assert "secret-value" not in message.get_content()
