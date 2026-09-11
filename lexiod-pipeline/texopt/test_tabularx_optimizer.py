"""Focused regression tests for the SyncTeX-critical tabularx path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from .opaque import (convert, find_opaque, instrument,
                     remove_unused_tabularx_package, run_probe, static_widths)
from .syntax_check import validate_latex
from .tex_tables import transform_tex


class TabularxWidthTests(unittest.TestCase):
    def test_multirow_uses_its_paragraph_column_width(self) -> None:
        source = (
            r"\begin{tabular}{|p{0.20\linewidth}|p{0.14\linewidth}|}"
            "\n"
            r"device & \multirow{7}{0.14\linewidth}{"
            "1.设备应有完好标识；"
            r"\\2.设备在验证有效期内。}\\"
            "\n"
            r"\end{tabular}"
        )

        transformed = transform_tex(source, anchor=False)

        self.assertIn(
            r"\multirow{7}{=}{1.设备应有完好标识；\\2.设备在验证有效期内。}",
            transformed,
        )
        self.assertNotIn(r"\multirow{7}{0.14\linewidth}", transformed)

    def test_rowbreak_after_percent_continuation_stays_on_its_own_line(self) -> None:
        source = (
            "\\begin{tabular}{ll}\n"
            "left & \\fieldvalue{x}%\n"
            "\\\\\n"
            "next & row\\\\\n"
            "\\end{tabular}\n"
        )

        transformed = transform_tex(source, anchor=False)
        errors = [
            issue for issue in validate_latex(transformed, require_sync_safe=False)
            if issue.severity == "error"
        ]

        self.assertNotIn("%\\\\", transformed)
        self.assertEqual([], errors)

    def test_ampersand_after_percent_comment_stays_on_its_own_line(self) -> None:
        source = (
            "\\begin{tabular}{ll}\n"
            "left% TODO: first cell\n"
            "& right\\\\\n"
            "\\end{tabular}\n"
        )

        transformed = transform_tex(source, anchor=False)
        errors = [
            issue for issue in validate_latex(transformed, require_sync_safe=False)
            if issue.severity == "error"
        ]

        self.assertNotIn("TODO: first cell &", transformed)
        self.assertEqual([], errors)

    def test_cli_converts_static_tables_before_probing_dynamic_tables(self) -> None:
        from . import cli

        source_text = (
            "\\begin{document}\n"
            "\\begin{tabularx}{\\linewidth}{|p{2cm}|X|}a&b\\\\\\end{tabularx}\n"
            "\\begin{tabularx}{\\linewidth}{@{}lX@{}}c&d\\\\\\end{tabularx}\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
            "\\end{document}\n"
        )
        probed_sources = []

        def fake_probe(tex, workdir, **kwargs):
            probed_sources.append(tex)
            return {(1, 1): "20.0pt"}, ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.tex"
            output = root / "output.tex"
            source.write_text(source_text, encoding="utf-8")
            with mock.patch.object(cli.opaque, "run_probe", side_effect=fake_probe):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--no-llm", "--no-preamble", "--no-anchor",
                ])

            self.assertEqual(result, 0)
            self.assertEqual(len(probed_sources), 1)
            self.assertEqual(probed_sources[0].count(r"\begin{tabularx}"), 1)
            self.assertIn(r"@{}lX@{}", probed_sources[0])
            self.assertNotIn(r"|p{2cm}|X|", probed_sources[0])
            self.assertNotIn(r"\begin{tabularx}", output.read_text("utf-8"))

    def test_cli_stops_after_one_unchanged_targeted_probe_repair(self) -> None:
        from . import cli

        class UnchangedRepairer:
            calls = 0

            def __init__(self, *args, **kwargs):
                pass

            def repair_document(self, source: str, **kwargs):
                type(self).calls += 1
                return source, SimpleNamespace(
                    batches=1, cache=0, deterministic_end_documents_removed=0,
                    failed=0, llm=1, pages=1, rejected=0, unchanged=1,
                )

        def failed_probe(tex, workdir, jobname="texopt_probe", **kwargs):
            workdir = Path(workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            probe_path = workdir / f"{jobname}.tex"
            probe_path.write_text(tex, encoding="utf-8")
            return {}, "! Extra alignment tab has been changed to \\cr.\nl.2 broken\n"

        source_text = (
            "\\begin{document}\n"
            "\\begin{tabularx}{\\linewidth}{@{}lX@{}}a&b\\\\\\end{tabularx}\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
            "\\end{document}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.tex"
            output = root / "output.tex"
            probe_dir = root / "probe"
            source.write_text(source_text, encoding="utf-8")
            UnchangedRepairer.calls = 0
            with mock.patch.object(cli, "LLMSyntaxRepairer", UnchangedRepairer), \
                    mock.patch.object(cli.opaque, "run_probe", side_effect=failed_probe):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--llm-repair-on-failure", "--no-preamble",
                    "--probe-dir", str(probe_dir),
                ])

            self.assertEqual(result, 3)
            self.assertEqual(UnchangedRepairer.calls, 1)
            self.assertIn(
                "MODEL_REPAIR_STALLED",
                output.with_suffix(".texopt.log").read_text("utf-8"),
            )

    def test_mixed_fixed_and_x_columns_have_exact_static_widths(self) -> None:
        source = (
            r"\begin{tabularx}{\textwidth}"
            r"{|p{3.0cm}|X|p{2.2cm}|X|}a&b&c&d\\\end{tabularx}"
        )
        table = find_opaque(source)[0]
        widths = static_widths(table)
        self.assertIsNotNone(widths)
        assert widths is not None
        self.assertEqual(set(widths), {1, 3})
        self.assertIn(r"3.0cm - 2.2cm", widths[1])
        self.assertIn(r"8\tabcolsep", widths[1])
        self.assertIn(r"5\arrayrulewidth", widths[1])

        optimized, report = convert(source)
        self.assertNotIn(r"\begin{tabularx}", optimized)
        self.assertIn(r"\begin{tabular}", optimized)
        self.assertEqual(report.by_source, {"static": 1})

    def test_natural_width_column_requires_probe(self) -> None:
        source = r"\begin{tabularx}{\textwidth}{|l|X|}a&b\\\end{tabularx}"
        self.assertIsNone(static_widths(find_opaque(source)[0]))

    def test_custom_intercolumn_material_requires_probe(self) -> None:
        source = r"\begin{tabularx}{\textwidth}{@{}p{2cm}X@{}}a&b\\\end{tabularx}"
        self.assertIsNone(static_widths(find_opaque(source)[0]))

    def test_probe_continues_after_recoverable_latex_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory)

            def fake_run(argv, **kwargs):
                self.assertIn("-interaction=nonstopmode", argv)
                self.assertNotIn("-halt-on-error", argv)
                (workdir / "texopt_probe.log").write_text(
                    "TEXOPT-W 1 0 10.0pt\nTEXOPT-W 2 1 20.0pt\n",
                    encoding="utf-8",
                )

            source = (
                "\\begin{document}\n"
                "\\begin{tabularx}{\\linewidth}{XX}a & b \\\\\n"
                "\\end{tabularx}\n\\end{document}\n"
            )
            with mock.patch("subprocess.run", side_effect=fake_run):
                widths, _ = run_probe(source, workdir)
            self.assertEqual(widths, {(1, 0): "10.0pt", (2, 1): "20.0pt"})

    def test_probe_adds_row_for_columns_hidden_by_multicolumn(self) -> None:
        source = (
            r"\begin{tabularx}{\linewidth}{@{}p{1cm}X@{}}"
            r"\multicolumn{2}{l}{only spanned rows}\\\end{tabularx}"
        )
        probed, _ = instrument(source)
        self.assertIn("{} & {}", probed)
        self.assertIn(r">{\TXPROBE{1}{1}}X", probed)

    def test_nested_opaque_environments_keep_their_own_end_tokens(self) -> None:
        source = (
            r"\begin{tabularx}{\linewidth}{X}outer "
            r"\begin{tabularx}{\linewidth}{X}inner\end{tabularx} "
            r"tail\end{tabularx}"
        )
        tables = find_opaque(source)
        self.assertEqual(len(tables), 2)
        self.assertGreater(tables[0].end_pos, tables[1].end_pos)

        optimized, report = convert(source)
        self.assertEqual(len(report.converted), 2)
        self.assertEqual(optimized.count(r"\begin{tabular}"), 2)
        self.assertEqual(optimized.count(r"\end{tabular}"), 2)
        self.assertNotIn(r"\begin{tabularx}", optimized)
        self.assertNotIn(r"\end{tabularx}", optimized)

    def test_unused_tabularx_package_is_removed(self) -> None:
        source = "\\usepackage{array,tabularx,xcolor}\n\\begin{tabular}{ll}a&b\\\\\\end{tabular}\n"
        optimized, removed = remove_unused_tabularx_package(source)
        self.assertEqual(removed, 1)
        self.assertIn(r"\usepackage{array,xcolor}", optimized)
        self.assertNotIn("{array,tabularx,xcolor}", optimized)


if __name__ == "__main__":
    unittest.main()
