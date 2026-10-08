#!/usr/bin/env python3
"""Run the same monitoring jobs through macOS launchd or Linux systemd."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
MODULES = {'realtime-monitor': 'worker_watch.py', 'daily-stats': 'daily_stats.py'}


def read_settings(path, repo=REPO):
    path = Path(path).resolve()
    settings = json.loads(path.read_text())
    workspace = Path(settings.get('workspace', str(repo.parent))).expanduser().resolve()

    def resolve(value):
        value = value.replace('{workspace}', str(workspace)).replace('{repo}', str(repo))
        p = Path(value).expanduser()
        return str(p if p.is_absolute() else path.parent / p)

    settings['python'] = resolve(settings.get('python', sys.executable))
    settings['path'] = str(path)
    settings['repo'] = str(repo)
    settings.setdefault('name', 'pdftotex')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', settings['name']):
        raise ValueError('name must contain only letters, numbers, dot, underscore or hyphen')
    jobs = settings.get('jobs', {})
    if not jobs or set(jobs) - MODULES.keys():
        raise ValueError(f'jobs must select from {", ".join(MODULES)}')
    for name, job in jobs.items():
        job['config'] = resolve(job['config'])
        interval = job.get('interval_seconds', 120 if name == 'realtime-monitor' else 600)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
            raise ValueError('interval_seconds must be a positive integer')
        job['interval_seconds'] = interval
        if job.get('environment_file'):
            job['environment_file'] = resolve(job['environment_file'])
    user = settings.get('linux', {}).get('user', 'pdftotex')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', user):
        raise ValueError('invalid Linux service user')
    return settings


def command(settings, target, job):
    args = [settings['python'], str(Path(settings['repo']) / 'scripts/monitor.py'),
            target, 'once', '--config', settings['path'], '--job', job]
    if target == 'linux' and settings['jobs'][job].get('environment_file'):
        args.append('--environment-loaded')
    return args


def unit_quote(value, *, expand_dollars=False):
    # systemd expands specifiers and dollar variables even in quoted ExecStart.
    if '\n' in value or '\r' in value:
        raise ValueError('newlines are not supported in service paths')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if expand_dollars:
        value = value.replace('$', '$$')
    return '"' + value + '"'


def render(settings, target, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = []
    for name, job in settings['jobs'].items():
        stem = settings['name'] + '-' + name
        if target == 'macos':
            label = 'com.' + stem
            report_dir = Path(json.loads(Path(job['config']).read_text())['output_dir'])
            data = {'Label': label, 'ProgramArguments': command(settings, target, name),
                    'RunAtLoad': True, 'StartInterval': job['interval_seconds'],
                    'ProcessType': 'Background', 'WorkingDirectory': settings['repo'],
                    'StandardOutPath': str(report_dir / 'scheduler.log'),
                    'StandardErrorPath': str(report_dir / 'scheduler.error.log')}
            path = output / (label + '.plist')
            path.write_bytes(plistlib.dumps(data))
            files.append(path)
        else:
            user = settings.get('linux', {}).get('user', 'pdftotex')
            service = output / (stem + '.service')
            environment = []
            if job.get('environment_file'):
                environment = ['EnvironmentFile=' + job['environment_file'].replace('%', '%%')]
            service.write_text('\n'.join([
                '[Unit]', f'Description=PDFToTex {name}', 'After=network-online.target docker.service',
                '', '[Service]', 'Type=oneshot', f'User={user}',
                'WorkingDirectory=' + settings['repo'].replace('%', '%%'),
                'ExecStart=' + ' '.join(unit_quote(v, expand_dollars=True) for v in command(settings, target, name)),
                'Environment=PYTHONUNBUFFERED=1', *environment, 'UMask=0027', '',
            ]))
            timer = output / (stem + '.timer')
            timer.write_text('\n'.join([
                '[Unit]', f'Description=Schedule PDFToTex {name}', '', '[Timer]',
                'OnBootSec=30s', f'OnUnitActiveSec={job["interval_seconds"]}s', 'AccuracySec=5s',
                'Persistent=true', f'Unit={stem}.service', '', '[Install]', 'WantedBy=timers.target', '',
            ]))
            files.extend([service, timer])
    return files


def check_host(target):
    expected = 'Darwin' if target == 'macos' else 'Linux'
    if platform.system() != expected:
        raise ValueError(f'{target} monitoring requires {expected}; use render to inspect another platform')


def environment_file(path):
    """Read literal KEY=value credentials without executing a shell."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.removeprefix('export ').partition('=')
        key = key.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('environment file requires KEY=value lines')
        # Quoted strings allow spaces; unquoted values remain literal ($ is not expanded).
        if value.startswith(('"', "'")):
            tokens = shlex.split(value, comments=True)
            if len(tokens) != 1:
                raise ValueError('environment file contains an invalid quoted value')
            value = tokens[0]
        values[key] = value
    return values


def run_once(settings, selected=None, environment_loaded=False):
    if selected and selected not in settings['jobs']:
        raise ValueError(f'job is not configured: {selected}')
    for name, job in settings['jobs'].items():
        if selected and selected != name:
            continue
        env = dict(os.environ)
        if job.get('environment_file') and not environment_loaded:
            env.update(environment_file(job['environment_file']))
        script = Path(settings['repo']) / 'pipeline/texopt/monitoring' / MODULES[name]
        subprocess.run([settings['python'], str(script), 'once', '--config', job['config']],
                       env=env, check=True)


def loaded_labels():
    result = subprocess.run(['launchctl', 'list'], text=True, capture_output=True, check=True)
    return {line.split()[-1]: line for line in result.stdout.splitlines()[1:] if line.split()}


def manage(settings, target, action, output):
    check_host(target)
    names = [settings['name'] + '-' + name for name in settings['jobs']]
    if action == 'status':
        if target == 'macos':
            jobs = loaded_labels()
            for name in names:
                print(jobs.get('com.' + name, 'not loaded: com.' + name))
        else:
            subprocess.run(['systemctl', 'show', *[n + '.timer' for n in names],
                            *[n + '.service' for n in names],
                            '--property=Id,ActiveState,SubState,Result,ExecMainStatus'], check=True)
        return
    if target == 'linux' and os.geteuid() != 0:
        raise ValueError('Linux install/stop requires root (sudo)')
    if action == 'stop':
        if target == 'macos':
            loaded = loaded_labels()
            for name in names:
                label = 'com.' + name
                if label in loaded:
                    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], check=True)
                (Path.home() / 'Library/LaunchAgents' / (label + '.plist')).unlink(missing_ok=True)
        else:
            subprocess.run(['systemctl', 'disable', '--now', *[n + '.timer' for n in names]], check=True)
            subprocess.run(['systemctl', 'stop', *[n + '.service' for n in names]], check=True)
        return
    # Validate inputs and prepare new output directories before activating jobs.
    owner = None
    if target == 'linux':
        import pwd
        owner = pwd.getpwnam(settings.get('linux', {}).get('user', 'pdftotex'))
    for job in settings['jobs'].values():
        config = json.loads(Path(job['config']).read_text())
        if job.get('environment_file'):
            environment_file(job['environment_file'])
        report_dir = Path(config['output_dir'])
        if not report_dir.is_absolute():
            raise ValueError('monitor output_dir must be absolute')
        created = not report_dir.exists()
        report_dir.mkdir(parents=True, exist_ok=True)
        if created and owner:
            os.chown(report_dir, owner.pw_uid, owner.pw_gid)
    files = render(settings, target, output)
    if target == 'macos':
        directory = Path.home() / 'Library/LaunchAgents'
        directory.mkdir(parents=True, exist_ok=True)
        loaded = loaded_labels()
        for source in files:
            label = source.stem
            if label in loaded:
                subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], check=True)
            path = directory / source.name
            shutil.copyfile(source, path)
            subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    else:
        subprocess.run(['systemd-analyze', 'verify', *map(str, files)], check=True)
        for source in files:
            destination = Path('/etc/systemd/system') / source.name
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'enable', '--now', *[n + '.timer' for n in names]], check=True)
        subprocess.run(['systemctl', 'start', *[n + '.service' for n in names]], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('platform', choices=['macos', 'linux'])
    parser.add_argument('action', choices=['render', 'once', 'install', 'stop', 'status'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--job', choices=list(MODULES))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--environment-loaded', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        settings = read_settings(args.config)
        if args.action == 'render':
            output = args.output or args.config.resolve().parent / 'scheduler-rendered' / args.platform
            for path in render(settings, args.platform, output):
                print(path)
        elif args.action == 'once':
            check_host(args.platform)
            run_once(settings, args.job, args.environment_loaded)
        else:
            output = args.output or args.config.resolve().parent / 'scheduler-rendered' / args.platform
            manage(settings, args.platform, args.action, output)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'monitor: {error}\n')


if __name__ == '__main__':
    main()
