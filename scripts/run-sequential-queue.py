"""用途：在一个已配置的工作进程中，按队列顺序处理 PDF，并保留每份文档的缓存。

推荐在项目已有的流水线容器/环境中运行；需要 texopt、lexoid、pypdfium2 及流水线依赖。
使用了 fcntl 文件锁，适用于 Linux/macOS；不能直接作为原生 Windows 脚本使用。
以下示例在 PDFToTex 根目录执行（需预先配置模块搜索路径、模型凭据和环境变量）：
    python scripts/run-sequential-queue.py /data/workers/queues/example.json --prepare-only
    python scripts/run-sequential-queue.py /data/workers/queues/example.json

路径配置不在本脚本顶部，而是在队列 JSON 中；各字段用途如下：
    sources：按处理顺序排列的 PDF 路径数组，文件名去掉扩展名后必须互不重复。
    container：监控配置中该工作进程/容器的名称；脚本不会自行创建容器。
    monitor_config：监控配置 JSON 的路径，正式运行时会更新其中 containers 列表。
    host_data：宿主机 data 目录的绝对路径，用于将容器 /data 路径转换为监控路径。
    source_root：可选，PDF 的共同根目录；配置后会在发布目录下保留相对目录结构。
请使用当前运行环境可访问的路径；工作目录固定为 /data/workers/<PDF文件名主干>，
适合项目现有的 /data 挂载布局，并非任意本地路径直接可用的通用转换器。

模型和发布路径从 BatchConfig.from_env() 读取；必须符合脚本已核准的检查条件：
    LEXOID_MODEL=gpt-5.6-sol，VISION_FALLBACK_MODEL=gpt-6-astra，
    RENDER_DPI=240，VISION_CONCURRENCY=2，RECONCILE_CONCURRENCY=2。
优化器版本必须为 texopt-layout-v11-outline-field-safe。
应设置 PIPELINE_PUBLISH_ROOT 为发布根目录，并按现有流水线要求配置其余模型/凭据。
这些检查用于防止误用配置，不应仅为绕过报错而修改。

--prepare-only：读取 PDF 页数并打印计划，不启动处理、不写队列状态或监控配置。
正式运行：会调用模型/流水线、写缓存和产物、更新队列同名 .status.json 以及监控配置。
任务失败后暂停队列，后续 PDF 保持 pending；再次运行会重建状态并依靠缓存恢复，
并非依据上一份状态文件跳过 done。存在 resume-none.json 时调用 resume-none/run.py。
不要同时启动同一队列；脚本使用队列 .lock 和监控 .queue.lock 文件防止写入冲突。
"""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import pypdfium2 as pdfium

from texopt.runtime.stages import BatchConfig, run_batch
from texopt.core.textio import write_utf8_atomic


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    write_utf8_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def publication_path(source, queue, config):
    relative = source.relative_to(queue['source_root']) if queue.get('source_root') else Path(source.name)
    return Path(config.publish_root) / relative.with_suffix('.tex')


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
    if len({Path(source).stem for source in queue['sources']}) != len(queue['sources']):
        raise ValueError('Queue PDF stems must be unique to keep work directories separate')
    jobs = []
    for source in queue['sources']:
        path = Path(source)
        doc = pdfium.PdfDocument(str(path))
        pages = len(doc)
        doc.close()
        jobs.append({'source': source, 'stem': path.stem, 'pages': pages, 'status': 'pending',
            'work_root': str(Path('/data/workers') / path.stem),
            'output_tex': str(publication_path(path, queue, config))})
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
            job_config = replace(config, publish_root=str(Path(job['output_tex']).parent))
            resume = root / 'resume-none.json'
            try:
                if resume.exists():
                    legacy = json.loads(resume.read_text())['legacy_key']
                    resume_script = Path(__file__).resolve().parent / 'resume-none/run.py'
                    result = subprocess.run([sys.executable, '-u', str(resume_script),
                        '--source', job['source'], '--output', str(root), '--legacy-key', legacy],
                        env={**os.environ, 'PIPELINE_PUBLISH_ROOT': job_config.publish_root})
                    status = result.returncode
                else:
                    source = Path(job['source'])
                    status = run_batch(source.parent, root, job_config, include={source.name})
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
