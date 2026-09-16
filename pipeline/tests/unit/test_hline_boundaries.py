"""Regression cases for phantom table rows introduced before horizontal rules."""

import pytest
import shutil
import subprocess

from texopt.optimization.syntax_repair import normalize_hline_row_boundaries


@pytest.mark.parametrize('ending', [r'\\', r'\tabularnewline', r'\\[2pt]', r'\tabularnewline*[2pt]'])
def test_valid_ruled_table_is_unchanged(ending):
    source = '\\begin{tabular}{|l|l|}\n\\hline\nA & B' + ending + ' % row\n% metadata\n\\hline\n\\hline\n\\end{tabular}'
    assert normalize_hline_row_boundaries(source) == (source, 0)


def test_missing_row_ending_is_repaired_once():
    source = '\\begin{tabular}{|l|l|}\n\\hline\nA & B % keep\n\\hline\n\\end{tabular}'
    expected = source.replace('B % keep\n\\hline', 'B % keep\n\\tabularnewline\\hline')
    fixed, count = normalize_hline_row_boundaries(source)
    assert fixed == expected
    assert count == 1
    assert normalize_hline_row_boundaries(fixed) == (fixed, 0)


def test_nested_tables_and_cell_groups_are_scoped():
    source = r'\begin{tabular}{ll}\hline {\hline} & \begin{tabular}{l}\hline X\tabularnewline\hline\end{tabular}\\\hline\end{tabular}'
    assert normalize_hline_row_boundaries(source) == (source, 0)


def test_outside_tables_comments_verbatim_and_explicit_empty_rows_are_preserved():
    source = '\\newcommand{\\test}{Text\n\\hline}\n% \\begin{tabular}{l}\n\\begin{verbatim}\nText\n\\hline\n\\end{verbatim}\nText\n\\hline\n'
    source += '\\begin{tabular}{ll}\n\\hline\nA & B\\\\\n & \\\\\n\\hline\n\\end{tabular}'
    assert normalize_hline_row_boundaries(source) == (source, 0)


def test_repeated_full_normalization_does_not_add_blank_rows():
    from texopt.optimization.local_tex import normalize_tex

    source = '\\begin{tabular}{|l|p{3cm}|}\n\\hline\nA & \\centering B\\\\\n\\hline\n\\end{tabular}'
    fixed, _ = normalize_tex(source)
    assert fixed == source.replace('B\\\\', 'B\\tabularnewline')
    assert normalize_tex(fixed)[0] == fixed


@pytest.mark.parametrize('prefix', [r'\cline{1-2}', r'\noalign{\vskip 2pt}'])
def test_interrow_commands_do_not_create_phantom_rows(prefix):
    source = r'\begin{tabular}{ll}A & B\\' + prefix + '\n\\hline\n\\end{tabular}'
    assert normalize_hline_row_boundaries(source) == (source, 0)


def test_normalization_preserves_rendered_vertical_rules(tmp_path):
    from PIL import Image, ImageChops
    from texopt.optimization.local_tex import normalize_tex

    if not shutil.which('xelatex') or not shutil.which('pdftoppm'):
        pytest.skip('XeLaTeX and Poppler required for the visual regression')
    source = r'''\documentclass{article}
\usepackage{array}
\begin{document}
\begin{tabular}{|p{2cm}|p{2cm}|p{2cm}|}
\hline
Name & Date & \centering Result\tabularnewline
\hline
A & 2026.04.21 & \centering Yes\tabularnewline
\hline
\end{tabular}
\end{document}
'''
    fixed = normalize_tex(normalize_tex(source)[0])[0]
    for name, content in [('reference', source), ('normalized', fixed)]:
        (tmp_path / (name + '.tex')).write_text(content)
        subprocess.run(['xelatex', '-interaction=nonstopmode', '-halt-on-error', name + '.tex'],
                       cwd=tmp_path, check=True, stdout=subprocess.DEVNULL, timeout=30)
        subprocess.run(['pdftoppm', '-singlefile', '-png', '-r', '120', name + '.pdf', name],
                       cwd=tmp_path, check=True, stdout=subprocess.DEVNULL, timeout=30)
    with Image.open(tmp_path / 'reference.png') as expected, Image.open(tmp_path / 'normalized.png') as actual:
        assert expected.size == actual.size
        assert ImageChops.difference(expected.convert('RGB'), actual.convert('RGB')).getbbox() is None
