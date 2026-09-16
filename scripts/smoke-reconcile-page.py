"""用途：用已有识别结果对单个真实 PDF 页面做复核实测，不重新识别整份文件。

依赖已安装的 texopt、pypdfium2、XeLaTeX；建议在测试镜像内运行。
示例：python smoke-reconcile-page.py --tex /data/raw.tex --evidence /data/raw.recognition.json
      --pdf /input/source.pdf --page 5 --output /test-output/run-01 --live
所有路径通过参数指定；输出目录必须不存在，以免覆盖已有结果。
--resume-cache 仅用于同一测试目录的缓存验收；已有次数账本保持不变，不重置调用上限。
只有传入 --live 才会请求模型；密钥和模型由容器已有环境变量提供，不打印密钥。
读取原始完整 PDF，但只发送指定页；同页未知坐标字段仅调用一次，再重复执行验证缓存。
输出输入副本、复核日志、字段结果、编译 PDF、页面预览和验收 JSON，不修改生产资料。
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import pypdfium2 as pdfium

from texopt.optimization.reconcile import reconcile_document, field_segments, select_exceptional_fields


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('tex', 'evidence', 'pdf', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--page', type=int, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--resume-cache', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('This real-provider test requires explicit --live')
    data = json.loads(args.evidence.read_text())
    page = next(p for p in data['pages'] if p['page'] == args.page)
    tex = args.tex.read_text()
    preamble, body = tex.split(r'\begin{document}', 1)
    pages = re.split(r'(?m)^% LEXOID_PAGE_COMPLETED: \d+/\d+\s*$', body)
    body = re.sub(r'^\s*\\(?:newpage|clearpage)\s*', '', pages[args.page - 1])
    tex = preamble + '\\begin{document}\n' + body + '\n\\end{document}\n'
    data['pages'] = [page]
    selected = select_exceptional_fields(tex, data)
    if args.resume_cache:
        assert (args.output / 'fields.reconcile-cache.json').exists()
        assert (args.output / 'fields.reconcile-budget.json').exists()
    args.output.mkdir(parents=True, exist_ok=args.resume_cache)
    source, evidence = args.output / 'source.tex', args.output / 'evidence.json'
    source.write_text(tex)
    evidence.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log = args.output / 'reconcile.process.calls.jsonl'
    os.environ['LEXOID_MODEL_CALL_LOG'] = str(log)
    output, fields = args.output / 'reviewed.tex', args.output / 'fields.json'
    report = reconcile_document(source, args.pdf, evidence, output, fields, max_page_requests=1)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    calls = [e for e in events if e.get('event') == 'finish']
    assert len(calls) == 1 and calls[0]['review_mode'] == 'page', calls
    assert calls[0]['field_count'] == report.selected and report.selected > 1
    assert report.failed == 0, report.errors
    before_calls = len(calls)
    repeated = reconcile_document(source, args.pdf, evidence, output, fields, max_page_requests=1)
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(e.get('event') == 'finish' for e in events) == before_calls
    assert report.fields == repeated.fields
    original_fields, reviewed_fields = field_segments(tex), field_segments(report.tex)
    selected_ids = set(calls[0]['field_ids'])
    assert set(original_fields) == set(reviewed_fields)
    assert all(original_fields[fid]['payload'] == reviewed_fields[fid]['payload']
               for fid in original_fields if fid not in selected_ids)
    for pass_number in (1, 2):
        compile_result = subprocess.run(['xelatex', '-interaction=nonstopmode', '-halt-on-error',
            '-output-directory', str(args.output), str(output)], capture_output=True, text=True, timeout=120)
        (args.output / f'compile-{pass_number}.stdout.log').write_text(compile_result.stdout)
        assert compile_result.returncode == 0, compile_result.stdout[-2000:]
    doc = pdfium.PdfDocument(str(args.pdf))
    try:
        doc[args.page - 1].render(scale=1.5).to_pil().save(args.output / 'source.png')
    finally:
        doc.close()
    doc = pdfium.PdfDocument(str(output.with_suffix('.pdf')))
    try:
        count = len(doc)
        for index in range(count):
            doc[index].render(scale=1.5).to_pil().save(args.output / f'reviewed-{index + 1}.png')
    finally:
        doc.close()
    result = dict(source_sha256=hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
                  source_page=args.page, fields=len(page['fields']), selected=report.selected,
                  confirmed=report.confirmed, needs_review=sum(f['needs_review'] for f in report.fields),
                  calls=len(calls), second_run_calls=0, usage=calls[0]['usage'],
                  cost_estimate=calls[0].get('cost_estimate'), compiled_pages=count,
                  selected_ids=sorted(selected_ids), original_candidates=len(selected))
    (args.output / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
