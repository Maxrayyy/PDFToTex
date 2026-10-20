"""Document acceptance is tested through its public interface."""
from copy import deepcopy
import json

from texopt.optimization.compilation import CompilationResult, compile_document, compile_tex

SOURCE = r'''\documentclass{article}
\begin{document}
Body
% LEXOID_PAGE_COMPLETED: 1/1
\end{document}
'''
EVIDENCE = {'schema': 'recognition/v1', 'pages': [{'page': 1, 'render': {'width': 595, 'height': 842, 'dpi': 72}}]}


def test_exhausted_clipping_retries_never_accept_document(tmp_path):
    attempts = []

    def clipped(path, *args, layout_report, **kwargs):
        attempts.append(path.read_text())
        report = {**layout_report, 'export_ok': False, 'outside_pages': [1],
                  'page_map': [{'source_page': 1, 'start': 1, 'end': 1}]}
        return CompilationResult(False, 'XeLaTeX exit 0; content clipped', report)

    output = tmp_path / 'result.tex'
    result = compile_document(SOURCE, output, tmp_path, evidence=EVIDENCE, compiler=clipped)
    assert not result.ok
    assert result.attempts == len(attempts) == 3
    assert result.layout['profiles'] == {'1': 2}
    assert output.read_text() == result.source
    saved = json.loads(output.with_suffix('.layout.json').read_text())
    assert saved['attempts'] == 3
    assert saved['export_ok'] is False
    assert saved['profiles'] == {'1': 2}


def test_compiler_failure_does_not_retry_without_geometric_evidence(tmp_path):
    attempts = []

    def failed(path, *args, layout_report, **kwargs):
        attempts.append(path)
        return CompilationResult(False, 'undefined control sequence', layout_report)

    result = compile_document(SOURCE, tmp_path / 'result.tex', tmp_path,
                              evidence=EVIDENCE, compiler=failed)
    assert not result.ok
    assert result.attempts == len(attempts) == 1
    assert 'undefined control sequence' in result.log


def test_compiler_exception_is_failure_and_does_not_mutate_input_report(tmp_path):
    source = tmp_path / 'input.tex'
    source.write_text(SOURCE)
    report = {'expected_pages': 1, 'profiles': {'1': 0}}
    original = deepcopy(report)
    result = compile_tex(source, tmp_path, engine='/nonexistent/pdftotex-compiler', layout_report=report)
    assert not result.ok
    assert 'FileNotFoundError' in result.log
    assert report == original
    assert result.layout is not report
