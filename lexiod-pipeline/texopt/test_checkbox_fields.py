"""Regression tests for editable checkbox extraction."""

from __future__ import annotations

import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path

from lexoid.cli import LATEX_CHECKBOX_FIELD_COMMAND
from .json_extractor import extract_fields
from .preamble import BLOCK
from .patch_lexoid_checkbox_prompt import (
    CHECKBOX_RULES,
    NEW_TABLE_RULE,
    patch_source,
)


class CheckboxFieldTests(unittest.TestCase):
    def test_checkbox_macro_uses_state_instead_of_raw_square(self) -> None:
        self.assertIn(r"\checkboxfield", BLOCK)
        self.assertNotIn(r"\square", BLOCK)
        self.assertIn(r"\checkmark", BLOCK)
        self.assertNotIn(r"\sffamily x", BLOCK)

    def test_lexoid_rule_prefers_plain_tabular(self) -> None:
        self.assertIn("prefer plain tabular", NEW_TABLE_RULE)
        self.assertIn("Do not use X columns", NEW_TABLE_RULE)

    def test_prompt_patch_tolerates_upstream_table_wording_change(self) -> None:
        source = r'''PROMPT = r"""
- Tables: use whichever LaTeX environment best preserves the source layout.
- Handwritten-entry annotation is MANDATORY:
"""
PREAMBLE = r"""
\newcommand{{\handwritten}}[1]{{#1}}
"""
'''
        patched = patch_source(source)
        self.assertIn(CHECKBOX_RULES.strip(), patched)
        self.assertIn(NEW_TABLE_RULE.strip(), patched)
        self.assertIn(r"\newcommand{{\checkboxfield}}", patched)
        self.assertNotIn("use whichever LaTeX environment", patched)
        self.assertEqual(patch_source(patched), patched)

    def test_prompt_patch_rejects_ambiguous_table_rule(self) -> None:
        source = r'''- Tables: first rule
- Tables: second rule
- Handwritten-entry annotation is MANDATORY:
\newcommand{{\handwritten}}[1]{{#1}}
'''
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            patch_source(source)

    def test_checked_and_unchecked_are_boolean_fields(self) -> None:
        source = (
            "% #VALUE_ID: LEX-P0001-C0001\n"
            "% #FIELD_VALUE: 正常检验\n"
            "\\checkboxfield{LEX-P0001-C0001}{checked}{正常检验}\n"
            "% #VALUE_ID: LEX-P0001-C0002\n"
            "% #FIELD_VALUE: 复检\n"
            "\\checkboxfield{LEX-P0001-C0002}{unchecked}{复检}\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkbox.tex"
            path.write_text(source, "utf-8")
            report = extract_fields(path)
        self.assertEqual(len(report.fields), 2)
        self.assertEqual(
            [(f.field_type, f.checked, f.value) for f in report.fields],
            [("checkbox", True, "checked"),
             ("checkbox", False, "unchecked")],
        )
        self.assertEqual([f.label for f in report.fields], ["正常检验", "复检"])

    def test_nested_one_argument_checkboxes_are_boolean_fields(self) -> None:
        source = (
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: 符合规定\n"
            "\\fieldvalue{\\handwritten{\\checkboxfield{checked}}} 符合规定\n"
            "% #VALUE_ID: LEX-P0001-V0002\n"
            "% #FIELD_VALUE: 不符合规定\n"
            "\\fieldvalue{\\checkboxfield{unchecked}} 不符合规定\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkbox.tex"
            path.write_text(source, "utf-8")
            report = extract_fields(path)
        self.assertEqual(
            [(f.field_type, f.checked, f.value) for f in report.fields],
            [("checkbox", True, "checked"),
             ("checkbox", False, "unchecked")],
        )
        self.assertTrue(report.fields[0].is_handwritten)

    def test_recognition_checkbox_macro_compiles_without_missing_glyphs(self) -> None:
        if not shutil.which("xelatex"):
            self.skipTest("XeLaTeX is required")
        source = (
            "\\documentclass{article}\n"
            "\\usepackage{amssymb,etoolbox}\n"
            f"{LATEX_CHECKBOX_FIELD_COMMAND}\n"
            "\\begin{document}\n"
            "\\checkboxfield{checked} yes "
            "\\checkboxfield{unchecked} no\n"
            "\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            tex = Path(directory) / "checkbox.tex"
            tex.write_text(source, "utf-8")
            result = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", "-halt-on-error",
                 f"-output-directory={directory}", str(tex)],
                capture_output=True, text=True, check=False,
            )
            visible = subprocess.run(
                ["pdftotext", str(tex.with_suffix(".pdf")), "-"],
                capture_output=True, text=True, check=False,
            ).stdout if result.returncode == 0 and shutil.which("pdftotext") else ""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Missing character", result.stdout + result.stderr)
        if visible:
            self.assertIn("✓", visible)

    def test_optimizer_preamble_preserves_one_argument_checkbox_contract(self) -> None:
        if not shutil.which("xelatex"):
            self.skipTest("XeLaTeX is required")
        source = (
            "\\documentclass{article}\n"
            "\\usepackage{amssymb,etoolbox}\n"
            f"{LATEX_CHECKBOX_FIELD_COMMAND}\n"
            "\\newcommand{\\fieldvalue}[1]{#1}\n"
            f"{BLOCK}\n"
            "\\begin{document}\n"
            "\\fieldvalue{\\checkboxfield{checked}} yes "
            "\\fieldvalue{\\checkboxfield{unchecked}} no\n"
            "\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            tex = Path(directory) / "optimized-checkbox.tex"
            tex.write_text(source, "utf-8")
            result = subprocess.run(
                ["xelatex", "-interaction=nonstopmode", "-halt-on-error",
                 f"-output-directory={directory}", str(tex)],
                capture_output=True, text=True, check=False,
            )
            visible = subprocess.run(
                ["pdftotext", str(tex.with_suffix(".pdf")), "-"],
                capture_output=True, text=True, check=False,
            ).stdout if result.returncode == 0 else ""
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Missing character", result.stdout + result.stderr)
        if visible:
            self.assertIn("✓", visible)
        self.assertIn("yes", visible)
        self.assertIn("no", visible)


if __name__ == "__main__":
    unittest.main()
