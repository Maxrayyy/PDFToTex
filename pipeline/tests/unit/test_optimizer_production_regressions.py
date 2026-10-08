import pytest

from texopt.optimization.local_tex import normalize_tex
from texopt.optimization.outline import normalize_outline
from texopt.optimization.syntax_check import validate_latex
from texopt.optimization.syntax_repair import normalize_text_mode_carets
from texopt.optimization.local_tex import normalize_scientific_notation


def test_plain_heading_with_trailing_comment_stays_balanced():
    source = r"""\documentclass{article}
\begin{document}
\textbf{4.2.1 Confirmation}
\noindent 4.2.5 Filling%
% #VALUE_ID: LEX-P0300-V0003
\fieldvalue{\handwritten{B4}}
\end{document}
"""
    result, report = normalize_outline(source)
    assert report['changed'] == 2
    assert not [i for i in validate_latex(result, require_sync_safe=False) if i.severity == 'error']
    assert '% #VALUE_ID: LEX-P0300-V0003' in result
    assert normalize_outline(result)[0] == result


def test_escaped_percent_does_not_hide_closing_math_delimiter():
    source = '$90\\%$\nplain ^{6}\n'
    result, _ = normalize_text_mode_carets(source)
    assert result == '$90\\%$\nplain \\textasciicircum{}{6}\n'


def test_rowbreak_spacing_is_not_a_display_math_opener():
    source = r'Heading\\[1em]' + '\n' + r'(p/m^{3}) and $x^{2}$'
    result, _ = normalize_text_mode_carets(source)
    assert result == r'Heading\\[1em]' + '\n' + r'(p/m\textasciicircum{}{3}) and $x^{2}$'
    assert normalize_text_mode_carets(result)[0] == result


def test_dollar_after_rowbreak_starts_math():
    source = r'Heading\\$x^{2}$ and (p/m^{3})'
    result, _ = normalize_text_mode_carets(source)
    assert result == r'Heading\\$x^{2}$ and (p/m\textasciicircum{}{3})'


def test_preamble_dollar_definition_does_not_change_body_math_state(tmp_path):
    from texopt.recognition.page_fallback import compile_page

    source = r"""\documentclass{article}
\def\dollar{$}
\begin{document}
\noindent y=(x/7419.45)\textasciicircum{}1.04679
$x\textasciicircum{}{2}$
\end{document}
"""
    result, _ = normalize_text_mode_carets(source)
    assert r'y=(x/7419.45)\textasciicircum{}1.04679' in result
    assert r'$x^{2}$' in result
    assert r'\def\dollar{$}' in result
    assert normalize_text_mode_carets(result)[0] == result
    normalized, _ = normalize_tex(source)
    assert not compile_page(normalized, tmp_path / 'preamble-dollar')


def test_scientific_notation_is_math_and_idempotent():
    source = r"""\documentclass{article}
\begin{document}
$90\%$
1.06x10^{6} cells/mL
\handwritten{6.0\times10^{7}}
$6.0\times10^{7}$
% 1.06x10^{6} remains a comment
\end{document}
"""
    result, _ = normalize_tex(source)
    assert '$1.06\\times10^{6}$ cells/mL' in result
    assert r'\handwritten{$6.0\times10^{7}$}' in result
    assert r'% 1.06x10^{6} remains a comment' in result
    assert normalize_tex(result)[0] == result


def test_scientific_notation_inside_existing_math_does_not_nest_dollars():
    source = r'\ensuremath{2+6.0x10^{7}} and $6.0x10^{7}$'
    result, _ = normalize_scientific_notation(source)
    assert result == r'\ensuremath{2+6.0\times10^{7}} and $6.0\times10^{7}$'
    assert normalize_scientific_notation(result)[0] == result


def test_repaired_heading_and_scientific_notation_compile(tmp_path):
    from texopt.recognition.page_fallback import compile_page

    source = r"""\documentclass{article}
\begin{document}
\textbf{4.2.1 Confirmation}
\noindent 4.2.5 Filling%

$90\%$\par
1.06x10^{6} cells/mL
\handwritten{6.0\times10^{7}}
\end{document}
"""
    normalized, _ = normalize_tex(source)
    result, _ = normalize_outline(normalized)
    assert not compile_page(result, tmp_path / 'compiled')


@pytest.mark.parametrize('body', [
    r'Limits: ≥ 5, ≤ 10, ≠ 0, ≈ 3, ± 1. Math: $x ≥ 5$.',
    r'Volume: 20 μL, 30 µL, 40 uL, 50 ul. Math: $20 μL$, $30 µL$.',
    r'Commands: \geq 5, \leq 10, \mu L.',
    r'Other symbols: ± × ÷ Ω ∞; $± × ÷ Ω ∞$.',
])
def test_relations_and_microliters_render_without_missing_glyphs(tmp_path, body):
    from texopt.recognition.page_fallback import compile_page

    source = "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n"
    normalized, _ = normalize_tex(source)
    directory = tmp_path / 'symbols'
    assert not compile_page(normalized, directory)
    log = (directory / 'compile.log').read_text(errors='replace')
    assert 'Missing character:' not in log
    assert normalize_tex(normalized)[0] == normalized


def test_display_symbol_units_preserve_metadata_and_literal_content():
    from texopt.optimization.local_tex import normalize_display_symbols

    source = r'''\documentclass{article}
% ≥ 20 μL
\begin{document}
% #FIELD_VALUE: ≥ 20 μL
\verb|≥ 20 μL \geq|
\begin{verbatim}≤ 30 µL \mu\end{verbatim}
volume: 20uL, 30 ul, uL, μL, µL; usual result, null, sample20ul, \ul{word}.
\geq 5 and $\mu$ and \ensuremath{2+\mu}.
\end{document}
'''
    result, _ = normalize_display_symbols(source)
    assert r'% #FIELD_VALUE: ≥ 20 μL' in result
    assert r'\verb|≥ 20 μL \geq|' in result
    assert r'\begin{verbatim}≤ 30 µL \mu\end{verbatim}' in result
    assert r'20\ensuremath{\mu\mathrm{L}}, 30 \ensuremath{\mu\mathrm{L}}' in result
    assert r'usual result, null, sample20ul, \ul{word}' in result
    assert r'\ensuremath{2+\mu}' in result
    assert normalize_display_symbols(result)[0] == result
