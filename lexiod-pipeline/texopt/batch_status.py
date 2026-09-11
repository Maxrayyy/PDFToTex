"""Date-ordered batch progress from input inventory and verified queue results."""

from collections import Counter
import json
from pathlib import Path
import re


def parse_batch_code(code):
    clean = code.removesuffix('_skip')
    match = re.fullmatch(r'(?P<prefix>[A-Z]\d{2}[A-Z0-9]*?)(?P<year>20\d{2})'
                         r'(?P<month>0[1-9]|1[0-2])(?P<sequence>\d{3})(?:-\d+)?', clean)
    legacy = False
    if match is None:
        match = re.fullmatch(r'(?P<prefix>[A-Z]\d{2}P)(?P<year>\d{2})'
                             r'(?P<month>0[1-9]|1[0-2])(?P<sequence>\d{3})(?:-\d+)?', clean)
        legacy = match is not None
    result = {'code': code, 'batch': clean[:3], 'process': '', 'line': '',
              'month': None, 'sequence': '', 'notes': []}
    if match is None:
        result['notes'].append('编号年月待核对')
        return result
    year = ('20' if legacy else '') + match['year']
    result.update(month=f"{year}-{match['month']}", sequence=match['sequence'])
    prefix = re.fullmatch(r'([A-Z]\d{2})([A-Z]\d?)(\d{2})?', match['prefix'])
    if prefix:
        result.update(batch=prefix[1], process=prefix[2], line=prefix[3] or '')
    else:
        result['notes'].append('工艺及线编码待核对')
    if legacy:
        result['notes'].append('两位年份按20xx解析')
    return result


def _read_json(path):
    try:
        value = path.read_text(encoding='utf-8')
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid_publication(path):
    try:
        text = path.read_text(encoding='utf-8')
        return (r'\begin{document}' in text and r'\end{document}' in text
                and 'LEXOID_RECOGNITION_FALLBACK' not in text)
    except (OSError, ValueError):
        return False


def collect_batches(settings, queue_dir):
    sources = Path(settings['source_root'])
    published = Path(settings['publish_root'])
    workers = Path(settings['work_root'])
    jobs = {}
    for path in sorted(Path(queue_dir).glob('*.status.json'), key=lambda p: p.stat().st_mtime_ns):
        for job in _read_json(path).get('jobs', []):
            try:
                relative = Path(job['source']).relative_to('/input')
            except (KeyError, ValueError, TypeError):
                continue
            jobs[relative] = job
    groups = {}
    inventory = []
    for directory in sorted(sources.glob('*/*/*')):
        if not directory.is_dir():
            continue
        files = [p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf'
                 and not any(part.startswith('.') for part in p.relative_to(directory).parts)]
        if not files:
            continue
        row = groups.setdefault(directory.name, {**parse_batch_code(directory.name),
            'units': set(), 'total': 0, 'completed': 0, 'counts': Counter(), 'directories': []})
        row['directories'].append(str(directory))
        row['units'].add(directory.relative_to(sources).parts[0])
        for path in files:
            inventory.append((path, row, directory.name.endswith('_skip')))
    stem_counts = Counter(path.stem for path, _, _ in inventory)
    for path, row, skipped in inventory:
        relative = path.relative_to(sources)
        job = jobs.get(relative)
        output = published / relative.with_suffix('.tex')
        valid = _valid_publication(output)
        state = 'unstarted'
        if skipped:
            state = 'skipped'
        elif job is not None:
            state = job.get('status', 'pending')
            if state == 'done' and not (job.get('exit_code') == 0 and valid):
                state = 'review'
        elif valid and stem_counts[path.stem] == 1:
            stage = workers / path.stem / '.pipeline' / path.stem
            done = _read_json(stage / (path.stem + '.optimized.pipeline.done'))
            report = _read_json(stage / (path.stem + '.report.json'))
            state = ('done' if done.get('stage') == 'optimise'
                     and report.get('compile_check', {}).get('ok') is True else 'review')
        elif output.is_file():
            state = 'review'
        row['total'] += 1
        row['completed'] += state == 'done'
        row['counts'][state] += 1
    for row in groups.values():
        counts = row.pop('counts')
        row['units'] = sorted(row['units'])
        if counts['skipped'] == row['total']:
            status = '已排除'
        elif row['completed'] == row['total']:
            status = '已完成'
        elif counts['running']:
            status = '处理中'
        elif counts['failed']:
            status = '失败待处理'
        elif counts['pending']:
            status = '排队中'
        elif counts['review']:
            status = '待核验'
        elif row['completed']:
            status = '部分完成'
        else:
            status = '未开始'
        row['status'] = status
    return sorted(groups.values(), key=lambda row: (row['month'] or '', row['sequence'], row['code']), reverse=True)


def render_batch_report(settings, queue_dir, checked_at):
    rows = collect_batches(settings, queue_dir)
    completed = sum(row['status'] == '已完成' for row in rows)
    lines = ['# 批次转译进度', '', f'更新时间：{checked_at}', '',
             f'共 {len(rows)} 个批次，已完成 {completed} 个。按编号年月由近到远排列，同月按流水号倒序。', '',
             '编号示例：`A39 | Z2 | 01 | 202605 | 032` = 批次 | 工艺 | 线 | 年月 | 流水号。',
             '同一批次跨 U1/U2/U3 合并统计；全部 PDF 有成功完成记录且正式 TEX 有效，才标记已完成。', '',
             '| 年月 | 批次号 | 批次 | 工艺 | 线 | 目录 | 完成 PDF | 状态 | 备注 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for row in rows:
        notes = list(row['notes'])
        if row['month'] and row['month'] > checked_at[:7]:
            notes.append('未来年月，需核对目录编号')
        if row['status'] == '已排除':
            notes.append('_skip 目录')
        locations = '、'.join(row['units'])
        cells = [row['month'] or '待核对', f"`{row['code']}`", row['batch'], row['process'] or '-',
                 row['line'] or '-', locations, f"{row['completed']}/{row['total']}",
                 row['status'], '；'.join(notes) or '-']
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'
