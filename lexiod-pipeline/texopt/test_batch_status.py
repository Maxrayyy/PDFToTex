import json
from pathlib import Path

import pytest

from .batch_status import parse_batch_code, collect_batches, render_batch_report


@pytest.mark.parametrize('code,expected', [
    ('A39Z201202605032', ('A39', 'Z2', '01', '2026-05', '032')),
    ('A37Z2202507004', ('A37', 'Z2', '', '2025-07', '004')),
    ('A39Z205202601005-01', ('A39', 'Z2', '05', '2026-01', '005')),
    ('A46P202604001', ('A46', 'P', '', '2026-04', '001')),
    ('A33P2309009', ('A33', 'P', '', '2023-09', '009')),
])
def test_batch_date_is_separate_from_line_and_sequence(code, expected):
    parsed = parse_batch_code(code)
    assert tuple(parsed[k] for k in ('batch', 'process', 'line', 'month', 'sequence')) == expected


def test_invalid_month_is_not_sorted_as_a_valid_date():
    assert parse_batch_code('A39Z201202613032')['month'] is None


def test_combined_batch_requires_every_pdf_and_valid_publications(tmp_path):
    settings = {k: str(tmp_path / k) for k in ('source_root', 'publish_root', 'work_root')}
    queue_dir = tmp_path / 'queues'
    queue_dir.mkdir()
    relatives = [Path('U1/batches/A37Z201202605032/a.pdf'),
                 Path('U3/20260808/A37Z201202605032/b.pdf'),
                 Path('U1/batches/A37Z201202604023/c.pdf')]
    for relative in relatives:
        path = Path(settings['source_root']) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'pdf')
    jobs = []
    for relative in relatives[:2]:
        jobs.append({'source': str(Path('/input') / relative), 'status': 'done', 'exit_code': 0})
    queue = queue_dir / 'test.status.json'
    queue.write_text(json.dumps({'jobs': jobs}))
    first = Path(settings['publish_root']) / relatives[0].with_suffix('.tex')
    first.parent.mkdir(parents=True)
    first.write_text(r'\begin{document}checked\end{document}')
    rows = collect_batches(settings, queue_dir)
    assert rows[0]['code'] == 'A37Z201202605032'
    assert rows[0]['completed'] == 1 and rows[0]['total'] == 2
    assert rows[0]['status'] != '已完成'
    assert rows[0]['units'] == ['U1', 'U3']
    second = Path(settings['publish_root']) / relatives[1].with_suffix('.tex')
    second.parent.mkdir(parents=True)
    second.write_text('% LEXOID_RECOGNITION_FALLBACK\nmissing')
    assert collect_batches(settings, queue_dir)[0]['completed'] == 1
    second.write_text(r'\begin{document}checked\end{document}')
    assert collect_batches(settings, queue_dir)[0]['status'] == '已完成'
    jobs[1]['status'] = 'running'
    queue.write_text(json.dumps({'jobs': jobs}))
    assert collect_batches(settings, queue_dir)[0]['status'] == '处理中'
    report = render_batch_report(settings, queue_dir, '2026-09-09T18:00:00+08:00')
    assert report.index('2026-05') < report.index('2026-04')
    assert '1/2' in report and '处理中' in report


def test_old_tex_alone_is_not_proof_and_future_month_is_flagged(tmp_path):
    settings = {k: str(tmp_path / k) for k in ('source_root', 'publish_root', 'work_root')}
    relative = Path('U2/batches/A37Z2202707004/a.pdf')
    source = Path(settings['source_root']) / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b'pdf')
    published = Path(settings['publish_root']) / relative.with_suffix('.tex')
    published.parent.mkdir(parents=True)
    published.write_text(r'\begin{document}old\end{document}')
    row = collect_batches(settings, tmp_path / 'queues')[0]
    assert row['completed'] == 0 and row['status'] == '待核验'
    report = render_batch_report(settings, tmp_path / 'queues', '2026-09-09T18:00:00+08:00')
    assert '2027-07' in report and '未来年月' in report
