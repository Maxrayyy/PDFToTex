"""Regression tests for field annotation structural safety."""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from .cli import _compile_latex
from .preamble import inject
from .syntax_repair import normalize_math_blank_lines
from .fields import annotate_fields
from .llm import HeuristicBatchNamer
from .syntax_check import validate_latex
from .reconcile import field_segments
from .tex_tables import transform_tex


class FieldAnnotationTests(unittest.TestCase):
    def test_multicolumn_cell_is_not_wrapped_by_preceding_field(self) -> None:
        source = (
            "\\begin{tabular}{|l|l|l|l|}\\hline\n"
            "Device & Maker & Status & Date\\\\\\hline\n"
            "% #VALUE_ID: LEX-P0053-V0018\n"
            "% #FIELD_VALUE: status\n"
            "Pipette & Eppendorf & \\fieldvalue{\\handwritten{expired}} & "
            "\\multicolumn{1}{c|}{\\handwritten{/}}\\\\\\hline\n"
            "\\end{tabular}\n"
        )
        annotated, _, _ = annotate_fields(
            transform_tex(source, anchor=False), namer=HeuristicBatchNamer()
        )
        self.assertIn(r"\multicolumn{1}{c|}{\handwritten{/}}", annotated)
        self.assertNotIn(r"\hwfield{LEX-P0053-V0018}{\multicolumn", annotated)
        self.assertEqual([], [issue for issue in validate_latex(
            annotated, require_sync_safe=False) if issue.severity == "error"])

    def test_split_scientific_notation_compiles_after_annotation(self) -> None:
        source = (
            "\\documentclass{article}\n"
            "\\newcommand{\\fieldvalue}[1]{#1}\n"
            "\\newcommand{\\handwritten}[1]{#1}\n"
            "\\begin{document}\n\\begin{tabular}{p{5cm}}\n"
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: coefficient\n"
            "\\fieldvalue{\\handwritten{1.04}}$\\times10^{\n"
            "% #VALUE_ID: LEX-P0001-V0002\n"
            "% #FIELD_VALUE: exponent\n\n"
            "\\fieldvalue{\\handwritten{7}}}$\\\\\n"
            "\\end{tabular}\n\\end{document}\n"
        )
        normalized, count = normalize_math_blank_lines(source)
        self.assertEqual(count, 1)
        transformed = transform_tex(normalized, anchor=False)
        annotated, records, _ = annotate_fields(
            transformed, namer=HeuristicBatchNamer()
        )
        self.assertIn(
            r"\hwfield{LEX-P0001-V0001}{\fieldvalue{\handwritten{1.04}}}$\times10^{",
            annotated,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.tex"
            for text in (inject(transformed), inject(annotated)):
                path.write_text(text, encoding="utf-8")
                ok, log = _compile_latex(path, path.parent, "xelatex", 30, runs=2)
                self.assertTrue(ok, log[-2500:])
        self.assertEqual([record.value for record in records], ["1.04", "7"])

    def test_multiline_field_keeps_every_paragraph_inside_value(self) -> None:
        source = (
            "\\begin{tabular}{|p{2cm}|p{10cm}|}\n"
            "Label & Details\\\\\\hline\n"
            "Investigation &\n"
            "% #VALUE_ID: LEX-P0007-V0005\n"
            "% #FIELD_VALUE: Investigation\n"
            "\\fieldvalue{First paragraph.\\par\n"
            "Second paragraph with \\textbf{nested {content}}.\\par\n"
            "Final paragraph with \\{escaped braces\\}.}\n"
            "\\\\\\hline\n"
            "Result &\n"
            "% #VALUE_ID: LEX-P0007-V0006\n"
            "% #FIELD_VALUE: Result\n"
            "\\fieldvalue{Unchanged}\\\\\n"
            "\\end{tabular}\n"
        )
        annotated, records, _ = annotate_fields(
            transform_tex(source, anchor=False), namer=HeuristicBatchNamer()
        )
        before, after = field_segments(source), field_segments(annotated)
        self.assertEqual(set(before), set(after))
        for fid in before:
            self.assertEqual(before[fid]["payload"], after[fid]["payload"])
        self.assertIn("Final paragraph", records[0].value)
        self.assertIn(r"\hwfield{LEX-P0007-V0005}{\fieldvalue{First", annotated)
        self.assertEqual([], [issue for issue in validate_latex(
            annotated, require_sync_safe=False) if issue.severity == "error"])

    def test_tabularnewline_stays_outside_field_wrapper(self) -> None:
        source = (
            "\\begin{tabular}{|l|l|}\n"
            "\\SA{}plain &\n"
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: left\n"
            "\\SA{}\\fieldvalue{one}\\tabularnewline\n"
            "\\SA{}plain &\n"
            "% #VALUE_ID: LEX-P0001-V0002\n"
            "% #FIELD_VALUE: right\n"
            "\\SA{}\\fieldvalue{two}\\tabularnewline\n"
            "\\end{tabular}\n"
        )

        annotated, records, _ = annotate_fields(
            source, namer=HeuristicBatchNamer()
        )
        errors = [
            issue for issue in validate_latex(annotated, require_sync_safe=False)
            if issue.severity == "error"
        ]

        self.assertEqual(2, len(records))
        self.assertNotIn(r"\tabularnewline}", annotated)
        self.assertEqual([], errors)

    def test_trailing_backslash_does_not_escape_hwfield_closing_brace(self) -> None:
        source = (
            "\\begin{tabular}{|l|}\n"
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: start time\n"
            "\\fieldvalue{11:22}\\ \\textasciitilde\\\n"
            "\\\\ \\hline\n"
            "\\end{tabular}\n"
        )

        annotated, records, _ = annotate_fields(
            source, namer=HeuristicBatchNamer()
        )
        errors = [
            issue for issue in validate_latex(annotated, require_sync_safe=False)
            if issue.severity == "error"
        ]

        self.assertEqual(1, len(records))
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
