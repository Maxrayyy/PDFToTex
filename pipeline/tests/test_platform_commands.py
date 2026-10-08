"""Platform entry points must preserve the shared jobs and caller configuration."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('platform_monitor', ROOT / 'scripts/monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


@pytest.fixture
def settings(tmp_path):
    work = tmp_path / 'workspace with spaces'
    work.mkdir()
    jobs = {}
    for name in monitor.MODULES:
        p = work / (name + '.json')
        p.write_text(json.dumps({'output_dir': str(work / name)}))
        jobs[name] = {'config': str(p), 'interval_seconds': 123}
    p = work / 'scheduler.json'
    p.write_text(json.dumps({'name': 'compat-check', 'jobs': jobs, 'linux': {'user': 'root'}}))
    return monitor.read_settings(p)


def test_launchd_executes_common_runner_and_persists(settings, tmp_path):
    files = monitor.render(settings, 'macos', tmp_path / 'agents')
    assert len(files) == 2
    for f in files:
        job = plistlib.loads(f.read_bytes())
        assert job['StartInterval'] == 123
        assert job['RunAtLoad']
        assert job['ProgramArguments'][-3:] == [settings['path'], '--job', f.stem.removeprefix('com.compat-check-')]
        assert '--scheduled' not in job['ProgramArguments']
        assert 'EnvironmentVariables' not in job


def test_systemd_uses_same_runner_config_and_intervals(settings, tmp_path):
    files = monitor.render(settings, 'linux', tmp_path / 'systemd')
    assert len(files) == 4
    for f in files:
        body = f.read_text()
        if f.suffix == '.service':
            assert '"linux" "once"' in body
            assert monitor.unit_quote(settings['path']) in body
            assert 'User=root' in body
            assert 'WorkingDirectory=' + settings['repo'] in body
            assert 'WorkingDirectory="' not in body
            assert 'EnvironmentFile' not in body  # credentials loaded only by the runner
        else:
            assert 'OnUnitActiveSec=123s' in body
            assert 'WantedBy=timers.target' in body


def test_once_passes_environment_without_shell_or_scheduler(settings, tmp_path, monkeypatch):
    env = tmp_path / 'email.env'
    env.write_text("MAIL_PASSWORD='literal $value with spaces'\n")
    settings['jobs']['realtime-monitor']['environment_file'] = str(env)
    calls = []
    monkeypatch.setattr(monitor.subprocess, 'run', lambda *a, **kw: calls.append((a, kw)))
    monitor.run_once(settings, 'realtime-monitor')
    assert len(calls) == 1
    command, options = calls[0][0][0], calls[0][1]
    assert command[-3:] == ['once', '--config', settings['jobs']['realtime-monitor']['config']]
    assert command[1].endswith('worker_watch.py')
    assert options['env']['MAIL_PASSWORD'] == 'literal $value with spaces'
    assert 'shell' not in options


def test_reject_wrong_host_before_service_mutation(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(monitor.platform, 'system', lambda: 'Darwin')
    with pytest.raises(ValueError, match='requires Linux'):
        monitor.manage(settings, 'linux', 'install', tmp_path)


def test_bad_interval_and_job_fail_before_install(settings):
    p = Path(settings['path'])
    original = json.loads(p.read_text())
    original['jobs']['daily-stats']['interval_seconds'] = 0
    p.write_text(json.dumps(original))
    with pytest.raises(ValueError, match='positive integer'):
        monitor.read_settings(p)
    with pytest.raises(ValueError, match='not configured'):
        monitor.run_once(settings, 'unknown')


@pytest.mark.parametrize('target,engine,expected', [
    ('macos', 'aarch64', 'linux/arm64'), ('linux', 'x86_64', 'linux/amd64'),
])
def test_build_profiles_select_native_architecture(target, engine, expected, tmp_path):
    # Stop at the first build; no images or user services are changed by this test.
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n'
                      f'if [ "$1" = info ]; then echo {engine}; exit 0; fi\n'
                      'echo "platform=$DOCKER_DEFAULT_PLATFORM args=$*"\nexit 42\n')
    docker.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH']}
    env.pop('PDFTOTEX_PLATFORM', None)
    env.pop('PDFTOTEX_PLATFORM_CONFIG', None)
    # The test container is not a git checkout; stub only the revision lookup.
    git = tmp_path / 'git'
    git.write_text('#!/bin/sh\necho test-revision\n')
    git.chmod(0o755)
    result = subprocess.run(['bash', str(ROOT / 'scripts/build-images.sh'), target], env=env,
                            text=True, capture_output=True)
    assert result.returncode == 42, result.stderr
    assert f'platform={expected} args=compose --profile build build runtime' in result.stdout


def test_build_rejects_wrong_architecture_before_build(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n[ "$1" = info ] && echo aarch64\n')
    docker.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'], 'PDFTOTEX_PLATFORM': 'linux/amd64'}
    env.pop('PDFTOTEX_PLATFORM_CONFIG', None)
    result = subprocess.run(['bash', str(ROOT / 'scripts/build-images.sh'), 'linux'], env=env,
                            text=True, capture_output=True)
    assert result.returncode == 1
    assert 'native build requires' in result.stderr


def test_linux_systemd_reads_root_only_credentials_before_dropping_user(settings, tmp_path, monkeypatch):
    env = tmp_path / 'root only.env'
    env.write_text('MAIL_PASSWORD=example\n')
    settings['jobs']['realtime-monitor']['environment_file'] = str(env)
    files = monitor.render(settings, 'linux', tmp_path / 'units')
    service = next(p for p in files if p.name.endswith('realtime-monitor.service')).read_text()
    assert 'EnvironmentFile=' + str(env) in service
    assert 'EnvironmentFile="' not in service
    assert '"--environment-loaded"' in service
    assert 'MAIL_PASSWORD' not in service
    monkeypatch.setattr(monitor, 'environment_file', lambda _: pytest.fail('service user must not read root-only file'))
    calls = []
    monkeypatch.setattr(monitor.subprocess, 'run', lambda *args, **kw: calls.append(kw))
    monkeypatch.setenv('MAIL_PASSWORD', 'inherited-from-systemd')
    monitor.run_once(settings, 'realtime-monitor', environment_loaded=True)
    assert calls[0]['env']['MAIL_PASSWORD'] == 'inherited-from-systemd'
