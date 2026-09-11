"""Regression tests for LaTeX syntax gates and encoding normalization."""

from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path

from .cli import _compile_latex, remove_explicit_sync_anchors
from .preamble import inject
from .syntax_check import validate_latex
from .syntax_repair import normalize_math_blank_lines
from .textio import read_text_auto, write_utf8_atomic


class EncodingTests(unittest.TestCase):
    def test_utf8_bom_is_detected_and_removed_on_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tex"
            output = Path(directory) / "output.tex"
            source.write_bytes(codecs.BOM_UTF8 + "中文".encode("utf-8"))
            decoded = read_text_auto(source)
            self.assertEqual(decoded.encoding, "utf-8-sig")
            self.assertTrue(decoded.had_bom)
            write_utf8_atomic(output, decoded.text)
            self.assertEqual(output.read_bytes(), "中文".encode("utf-8"))

    def test_gb18030_is_normalized_to_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.tex"
            source.write_bytes("字段值".encode("gb18030"))
            decoded = read_text_auto(source)
            self.assertEqual(decoded.encoding, "gb18030")
            self.assertEqual(decoded.text, "字段值")


class SyntaxTests(unittest.TestCase):
    def test_strikeout_loads_missing_dependency_without_option_clash(self) -> None:
        for existing in ("", r"\usepackage{ulem}", r"\newcommand{\sout}[1]{#1}"):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                source = ("\\documentclass{article}\n" + existing + "\n"
                          "\\begin{document}\n\\sout{1384} 1385\n\\end{document}\n")
                out = inject(source)
                self.assertEqual(inject(out), out)
                path = Path(directory) / "strikeout.tex"
                path.write_text(out, encoding="utf-8")
                ok, log = _compile_latex(path, path.parent, "xelatex", 30, runs=2)
                self.assertTrue(ok, log[-1500:])

    def test_math_blank_lines_preserve_comments_and_line_numbers(self) -> None:
        source = "$10^{\n% #FIELD_VALUE: exponent\n\n7}$\n\nText\n"
        fixed, count = normalize_math_blank_lines(source)
        self.assertEqual(fixed, "$10^{\n% #FIELD_VALUE: exponent\n%\n7}$\n\nText\n")
        self.assertEqual(count, 1)
        self.assertEqual(normalize_math_blank_lines(fixed), (fixed, 0))

    def test_math_blank_lines_ignore_literals_and_unclosed_math(self) -> None:
        source = ("\\$5\n\ntext\n% $ ignored\n\n"
                  "\\verb|$|\n\n\\begin{verbatim}\n$\n\n$\n\\end{verbatim}\n"
                  "$unclosed\n\ntext")
        self.assertEqual(normalize_math_blank_lines(source), (source, 0))

    def test_math_blank_lines_support_display_and_parentheses(self) -> None:
        for opening, closing in [("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)")]:
            source = opening + "x\n\ny" + closing
            self.assertEqual(normalize_math_blank_lines(source),
                             (opening + "x\n%\ny" + closing, 1))

    def test_explicit_sync_anchors_are_spaces_in_final_output(self) -> None:
        source = "  \\SA{}样品名称 & \\SA{}字段值 \\SA%\n"
        cleaned, count = remove_explicit_sync_anchors(source)
        self.assertEqual(count, 2)
        self.assertEqual(cleaned, "   样品名称 &  字段值 \\SA%\n")

    def test_unclosed_table_is_an_error(self) -> None:
        issues = validate_latex(r"\begin{tabular}{ll} a & b")
        self.assertIn("UNCLOSED_ENVIRONMENT", {issue.code for issue in issues})

    def test_illegal_tabular_width_argument_is_an_error(self) -> None:
        source = r"\begin{tabular}{\textwidth}{ll}a&b\\\end{tabular}"
        issues = validate_latex(source)
        self.assertIn("ILLEGAL_TABULAR_WIDTH", {issue.code for issue in issues})

    def test_inline_verb_braces_are_ignored(self) -> None:
        issues = validate_latex(r"text \verb|{| remains structurally valid")
        self.assertFalse([issue for issue in issues if issue.severity == "error"])

    def test_table_row_with_too_many_columns_is_an_error(self) -> None:
        source = (
            "\\begin{tabularx}{\\linewidth}{|l|X|l|X|}\n"
            "a & b & c & d & e \\\\\n"
            "\\end{tabularx}\n"
        )
        issues = validate_latex(source)
        alignment = [issue for issue in issues
                     if issue.code == "TABLE_ALIGNMENT_MISMATCH"]
        self.assertEqual(len(alignment), 1)
        self.assertEqual(alignment[0].line, 2)
        self.assertIn("spans 5 columns; expected 4", alignment[0].message)

    def test_table_row_with_omitted_trailing_cells_is_only_a_warning(self) -> None:
        source = (
            "\\begin{tabular}{|l|l|l|}\n"
            "a & b \\\\\n"
            "\\end{tabular}\n"
        )
        alignment = [
            issue for issue in validate_latex(source)
            if issue.code == "TABLE_ALIGNMENT_UNDERFULL"
        ]
        self.assertEqual(len(alignment), 1)
        self.assertEqual(alignment[0].severity, "warning")
        self.assertIn("spans 2 columns; expected 3", alignment[0].message)

    def test_multicolumn_row_satisfies_declared_span(self) -> None:
        source = (
            "\\begin{tabular}{|l|l|l|l|}\n"
            "\\hline\n"
            "\\multicolumn{3}{c}{combined} & value \\\\\n"
            "\\hline\n"
            "\\end{tabular}\n"
        )
        issues = validate_latex(source)
        self.assertNotIn("TABLE_ALIGNMENT_MISMATCH",
                         {issue.code for issue in issues})

    def test_line_breaks_inside_nested_minipage_are_not_table_rows(self) -> None:
        source = (
            "\\begin{tabularx}{\\linewidth}{|c|X|c|}\n"
            "label & \\begin{minipage}{\\linewidth}\n"
            "first\\\\\nsecond\\\\\nthird\n"
            "\\end{minipage} & result \\\\\n"
            "\\end{tabularx}\n"
        )
        issues = validate_latex(source)
        self.assertNotIn("TABLE_ALIGNMENT_MISMATCH",
                         {issue.code for issue in issues})

    def test_invalid_nested_table_is_checked(self) -> None:
        source = (
            "\\begin{tabular}{l}\n"
            "\\begin{tabular}{ll}\n"
            "a & b\\newline\n"
            "c & d \\\\\n"
            "\\end{tabular} \\\\\n"
            "\\end{tabular}\n"
        )

        issues = validate_latex(source)

        self.assertIn("TABLE_ALIGNMENT_MISMATCH",
                      {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
