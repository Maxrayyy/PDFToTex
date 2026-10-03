#!/usr/bin/env python3
"""Recompile retained production layout inputs without altering jobs or caches."""

import argparse
import json
from pathlib import Path

from texopt.optimization.cli import _compile_latex
from texopt.optimization.page_layout import prepare_layout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('stems', nargs='+')
    args = parser.parse_args()
    summaries = []
    for stem in args.stems:
        if Path(stem).name != stem:
            parser.error('stems must be document basenames')
        worker = args.workers / stem
        original = worker / '.pipeline' / stem / (stem + '.optimized.tex')
        evidence = worker / 'tex' / (stem + '.recognition.json')
        output = args.output / stem
        output.mkdir(parents=True, exist_ok=True)
        target = output / (stem + '.tex')
        source, report = prepare_layout(original.read_text(), json.loads(evidence.read_text()))
        target.write_text(source)
        ok, log = _compile_latex(target, original.parent, 'xelatex', args.timeout, layout_report=report)
        target.with_suffix('.compile.log').write_text(log)
        summary = {'stem': stem, 'compile_ok': ok, 'source': str(original),
                   'report': str(target.with_suffix('.layout.json')),
                   **{key: report.get(key) for key in ('expected_pages', 'actual_pages',
                       'outside_pages', 'wrong_size_pages', 'overflow', 'export_ok')}}
        summaries.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    (args.output / 'summary.json').write_text(json.dumps(summaries, ensure_ascii=False, indent=2))
    return int(any(not item['compile_ok'] for item in summaries))


if __name__ == '__main__':
    raise SystemExit(main())
