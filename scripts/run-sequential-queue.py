"""Run approved PDFs serially in one worker, retaining per-document caches."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import subprocess
import sys

import pypdfium2 as pdfium

from texopt.stages import BatchConfig, run_batch
from texopt.textio import write_utf8_atomic


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    write_utf8_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def monitor_current(queue, job):
    config_path = Path(queue['monitor_config'])
    with config_path.with_suffix('.queue.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(config_path.read_text())
        host_data = Path(queue['host_data'])
        target = {'name': queue['container'], 'stem': job['stem'], 'pages': job['pages'],
            'work_root': str(host_data / Path(job['work_root']).relative_to('/data')),
            'output_tex': str(host_data / Path(job['output_tex']).relative_to('/data'))}
        config['containers'] = [target if item['name'] == queue['container'] else item
                                for item in config['containers']]
        if not any(item['name'] == queue['container'] for item in config['containers']):
            config['containers'].append(target)
        save(config_path, config)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('queue', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    queue = json.loads(args.queue.read_text())
    config = BatchConfig.from_env()
    assert (config.vision_model, config.fallback_model, config.render_dpi,
            config.vision_concurrency, config.reconcile_concurrency) == (
                'gpt-5.6-sol', 'gpt-6-astra', 240, 2, 2)
    assert config.optimizer_version == 'texopt-layout-v11-outline-field-safe'
    jobs = []
    for source in queue['sources']:
        path = Path(source)
        doc = pdfium.PdfDocument(str(path))
        pages = len(doc)
        doc.close()
        jobs.append({'source': source, 'stem': path.stem, 'pages': pages, 'status': 'pending',
            'work_root': str(Path('/data/workers') / path.stem),
            'output_tex': str(Path(config.publish_root) / (path.stem + '.tex'))})
    print(json.dumps({'container': queue['container'], 'jobs': jobs,
                      'dpi': config.render_dpi, 'concurrency': config.vision_concurrency},
                     ensure_ascii=False), flush=True)
    if args.prepare_only:
        return 0
    state_path = args.queue.with_suffix('.status.json')
    with args.queue.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {'container': queue['container'], 'started_at': now(), 'jobs': jobs}
        save(state_path, state)
        for job in jobs:
            job.update(status='running', started_at=now())
            state['active_source'] = job['source']
            save(state_path, state)
            monitor_current(queue, job)
            print('QUEUE START: ' + job['source'], flush=True)
            root = Path(job['work_root'])
            resume = root / 'resume-none.json'
            try:
                if resume.exists():
                    legacy = json.loads(resume.read_text())['legacy_key']
                    resume_script = Path(__file__).resolve().parent / 'resume-none/run.py'
                    result = subprocess.run([sys.executable, '-u', str(resume_script),
                        '--source', job['source'], '--output', str(root), '--legacy-key', legacy])
                    status = result.returncode
                else:
                    source = Path(job['source'])
                    status = run_batch(source.parent, root, config, include={source.name})
                job.update(status='done' if status == 0 else 'failed', exit_code=status)
            except Exception as exc:
                job.update(status='failed', error=str(exc))
            job['finished_at'] = now()
            save(state_path, state)
            print('QUEUE FINISH: ' + json.dumps(job, ensure_ascii=False), flush=True)
            if job['status'] != 'done':
                state.update(status='paused', active_source=None, paused_at=now())
                save(state_path, state)
                print('QUEUE PAUSED: failed job retained; later documents remain pending', flush=True)
                return 1
        state.update(active_source=None, finished_at=now())
        save(state_path, state)
        return int(any(job['status'] != 'done' for job in jobs))


if __name__ == '__main__':
    raise SystemExit(main())
