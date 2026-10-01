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
