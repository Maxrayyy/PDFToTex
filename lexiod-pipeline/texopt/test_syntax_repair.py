"""Tests for page-level LLM syntax repair and content invariants."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from .syntax_repair import (LLMSyntaxRepairer, SYSTEM_PROMPT, apply_line_edits,
                            canonicalize_document_terminator, group_lexoid_pages,
                            normalize_control_word_boundaries,
                            normalize_multicolumn_linebreaks,
                            normalize_text_mode_math_symbols,
                            page_numbers_for_lines,
                            remove_intermediate_end_documents,
                            repair_invariants_hold, split_lexoid_pages)


class FakeRepairer(LLMSyntaxRepairer):
    def _call(self, page_start: int, page_end: int, total: int,
              chunk: str, diagnostic_hint: str = "") -> str:
        return chunk.replace("BROKEN & & &", "BROKEN & &")


class FailedRepairer(LLMSyntaxRepairer):
    def _call(self, page_start: int, page_end: int, total: int,
              chunk: str, diagnostic_hint: str = ""):
        return None


class AuthoritativeRepairer(LLMSyntaxRepairer):
    def _call(self, page_start: int, page_end: int, total: int,
              chunk: str, diagnostic_hint: str = ""):
        return chunk.replace("中文值", "模型改值")


class SequencedRepairer(LLMSyntaxRepairer):
    def __init__(self, *args, replacements: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.replacements = iter(replacements)

    def _call(self, page_start: int, page_end: int, total: int,
              chunk: str, diagnostic_hint: str = "") -> str:
        return next(self.replacements)


class SyntaxRepairTests(unittest.TestCase):
    def test_newline_is_delimited_from_visible_letters(self) -> None:
        source = r"阳性对照\newlineIL-6-0002\newline NaN & 阴性\newline对照"
        repaired, count = normalize_control_word_boundaries(source)
        self.assertEqual(count, 2)
        self.assertEqual(repaired, r"阳性对照\newline{}IL-6-0002\newline NaN & 阴性\newline{}对照")
        self.assertEqual(normalize_control_word_boundaries(repaired), (repaired, 0))

    def test_boundary_repair_preserves_commands_comments_and_verbatim(self) -> None:
        source = (
            "\\newcommand{\\newlineCustom}{custom}\n"
            "\\newlineCustom \\newlinechar=10\n"
            "% \\newlineIL \\quad至\n"
            "\\verb|\\newlineIL \\quad至|\n"
            "\\begin{verbatim}\n\\newlineIL \\quad至\n\\end{verbatim}\n"
        )
        self.assertEqual(normalize_control_word_boundaries(source), (source, 0))

    def test_generated_multicolumn_newline_has_command_boundary(self) -> None:
        source = r"\multicolumn{1}{p{8cm}}{control\\IL-6-0002}"
        repaired, count = normalize_multicolumn_linebreaks(source)
        self.assertEqual(count, 1)
        self.assertIn(r"control\newline{}IL-6-0002", repaired)

    def test_multicolumn_paragraph_linebreak_does_not_end_table_row(self) -> None:
        source = (
            "\\begin{tabular}{|l|l|l|l|}\n"
            "label & \\multicolumn{3}{p{8cm}|}{first line\\\\\n"
            "\\fieldvalue{second line} \\shortstack{A\\\\B}\n"
            "}\\\\\\hline\n"
            "\\end{tabular}\n"
        )

        repaired, count = normalize_multicolumn_linebreaks(source)

        self.assertEqual(count, 1)
        self.assertIn(r"first line\newline", repaired)
        self.assertIn(r"\shortstack{A\\B}", repaired)
        self.assertIn("}\\\\\\hline", repaired)
        self.assertEqual(normalize_multicolumn_linebreaks(repaired), (repaired, 0))

    def test_multicolumn_normalization_preserves_nested_table_rows(self) -> None:
        source = (
            "\\begin{tabular}{|l|}\n"
            "\\multicolumn{1}{p{8cm}}{intro\\\\\n"
            "\\begin{tabular}{ll}\n"
            "a & b\\\\\n"
            "c & d\\\\\n"
            "\\end{tabular}\n"
            "}\\\\\n"
            "\\end{tabular}\n"
        )

        repaired, count = normalize_multicolumn_linebreaks(source)

        self.assertEqual(1, count)
        self.assertIn("intro\\newline", repaired)
        self.assertIn("a & b\\\\\nc & d\\\\", repaired)

    def test_standalone_diagonal_symbols_are_safe_in_text_mode(self) -> None:
        source = (
            "\\fieldvalue{\\handwritten{\\diagup}} & \\diagdown\\\\\n"
            "% #HANDWRITTEN: \\diagup\n"
            "\\ensuremath{\\diagup}\n"
        )

        repaired, count = normalize_text_mode_math_symbols(source)

        self.assertEqual(count, 2)
        self.assertIn(r"\handwritten{\ensuremath{\diagup}}", repaired)
        self.assertIn(r"& \ensuremath{\diagdown}\\", repaired)
        self.assertIn(r"% #HANDWRITTEN: \diagup", repaired)
        self.assertEqual(normalize_text_mode_math_symbols(repaired), (repaired, 0))

    def test_zero_argument_spacing_commands_are_delimited_before_cjk(self) -> None:
        source = (
            "正文\\quad至\\qquad结束\n"
            "% 注释中的 \\quad至 保持不变\n"
            "\\custom中文宏保持不变\n"
        )
        repaired, count = normalize_control_word_boundaries(source)
        self.assertEqual(count, 2)
        self.assertIn(r"正文\quad{}至\qquad{}结束", repaired)
        self.assertIn(r"% 注释中的 \quad至 保持不变", repaired)
        self.assertIn(r"\custom中文宏保持不变", repaired)

    def test_line_edit_response_preserves_unmentioned_bytes(self) -> None:
        source = "one\nbroken & row\nthree\n"
        response = json.dumps({
            "edits": [{"start_line": 2, "end_line": 2,
                       "replacement": "fixed & row"}]
        })
        self.assertEqual(
            apply_line_edits(source, response),
            "one\nfixed & row\nthree\n",
        )

    def test_line_edit_response_rejects_overlaps(self) -> None:
        response = json.dumps({"edits": [
            {"start_line": 1, "end_line": 2, "replacement": "x"},
            {"start_line": 2, "end_line": 3, "replacement": "y"},
        ]})
        with self.assertRaises(ValueError):
            apply_line_edits("a\nb\nc\n", response)

    def test_openai_request_uses_gpt56_compatible_parameters(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "unchanged"}}]
                }).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured.update(json.loads(request.data.decode("utf-8")))
            return Response()

        repairer = LLMSyntaxRepairer(
            Path("unused-cache.json"), "gpt-5.6-terra", api_key="test"
        )
        with mock.patch.dict(
            os.environ,
            {"OPENAI_BASE_URL": "http://47.85.193.244/v1/"},
        ), mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(repairer._call_openai("input"), "unchanged")
        self.assertEqual(
            captured["url"],
            "http://47.85.193.244/v1/chat/completions",
        )
        self.assertIn("max_completion_tokens", captured)
        self.assertNotIn("max_tokens", captured)
        self.assertNotIn("temperature", captured)

    def test_prompt_requires_explicit_table_row_span_audit(self) -> None:
        self.assertIn("Mandatory table audit", SYSTEM_PROMPT)
        self.assertIn("four columns", SYSTEM_PROMPT)
        self.assertIn("three label/value pairs", SYSTEM_PROMPT)
        self.assertIn("Do not return the input unchanged", SYSTEM_PROMPT)

    def test_intermediate_end_document_is_removed(self) -> None:
        source = (
            "\\begin{document}\npage1\n\\end{document}\n"
            "% LEXOID_PAGE_COMPLETED: 1/2\npage2\n\\end{document}\n"
            "% LEXOID_PAGE_COMPLETED: 2/2\n"
        )
        repaired, removed = remove_intermediate_end_documents(source)
        self.assertEqual(removed, 1)
        self.assertEqual(repaired.count(r"\end{document}"), 1)

    def test_document_terminator_is_moved_after_resumed_pages(self) -> None:
        source = (
            "\\begin{document}\npage1\n\\end{document}\n"
            "% LEXOID_PAGE_COMPLETED: 1/2\npage2\n"
            "% LEXOID_PAGE_COMPLETED: 2/2\n"
        )
        repaired, removed = canonicalize_document_terminator(source)
        self.assertEqual(removed, 1)
        self.assertEqual(repaired.count(r"\end{document}"), 1)
        self.assertGreater(repaired.index(r"\end{document}"), repaired.index("page2"))

    def test_fragment_drops_document_terminator(self) -> None:
        repaired, removed = canonicalize_document_terminator("page\n\\end{document}\n")
        self.assertEqual(removed, 1)
        self.assertNotIn(r"\end{document}", repaired)

    def test_split_preserves_every_character(self) -> None:
        source = (
            "one\n% LEXOID_PAGE_COMPLETED: 1/2\n"
            "two\n% LEXOID_PAGE_COMPLETED: 2/2\ntrailer"
        )
        chunks = split_lexoid_pages(source)
        self.assertEqual([(p, t) for p, t, _ in chunks], [(1, 2), (2, 2)])
        self.assertEqual("".join(chunk for _, _, chunk in chunks), source)

    def test_ten_page_batches_preserve_order_and_content(self) -> None:
        source = "".join(
            f"page {page}\n% LEXOID_PAGE_COMPLETED: {page}/23\n"
            for page in range(1, 24)
        )
        pages = split_lexoid_pages(source)
        batches = group_lexoid_pages(pages, 10)
        self.assertEqual(
            [(start, end, total) for start, end, total, _ in batches],
            [(1, 10, 23), (11, 20, 23), (21, 23, 23)],
        )
        self.assertEqual("".join(chunk for *_, chunk in batches), source)

    def test_validator_lines_map_to_physical_pages(self) -> None:
        source = (
            "one\ntwo\n% LEXOID_PAGE_COMPLETED: 1/2\n"
            "three\nfour\n% LEXOID_PAGE_COMPLETED: 2/2\n"
        )
        self.assertEqual(page_numbers_for_lines(source, [2, 4]), {1, 2})

    def test_targeted_retry_only_changes_selected_page(self) -> None:
        source = (
            "BROKEN & & &\n% LEXOID_PAGE_COMPLETED: 1/2\n"
            "BROKEN & & &\n% LEXOID_PAGE_COMPLETED: 2/2\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            repairer = FakeRepairer(
                Path(directory) / "cache.json", "gpt-test", api_key="test"
            )
            repaired, stats = repairer.repair_document(
                source, target_pages={2}, diagnostic_hint="unclosed table"
            )
        self.assertIn("BROKEN & & &\n% LEXOID_PAGE_COMPLETED: 1/2", repaired)
        self.assertIn("BROKEN & &\n% LEXOID_PAGE_COMPLETED: 2/2", repaired)
        self.assertEqual(stats.batches, 1)

    def test_invariants_allow_field_id_and_name_changes_but_protect_values(self) -> None:
        source = (
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: 名称\n\\fieldvalue{中文值}\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
        )
        renamed = source.replace(
            "LEX-P0001-V0001", "LEX-P0001-V0001-sample_name"
        ).replace("#FIELD_VALUE: 名称", "#FIELD_VALUE: 样品名称")
        self.assertTrue(repair_invariants_hold(source, renamed))
        self.assertFalse(repair_invariants_hold(
            source, source.replace("中文值", "修改值")
        ))

    def test_invariants_allow_closing_brace_repair_but_not_numeric_value_change(self) -> None:
        broken = (
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: Event count\n\\fieldvalue{231,314 \\\\\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
        )
        fixed = broken.replace(r"231,314 \\", r"231,314} \\")
        changed = broken.replace("231,314", "231,315")
        self.assertTrue(repair_invariants_hold(broken, fixed))
        self.assertFalse(repair_invariants_hold(broken, changed))

    def test_accepted_repairs_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "syntax-cache.json"
            source = "BROKEN & & &\n% LEXOID_PAGE_COMPLETED: 1/1\n"
            first = FakeRepairer(cache, "gpt-test", api_key="test", batch_pages=10)
            repaired, stats = first.repair_document(source)
            self.assertNotEqual(repaired, source)
            self.assertEqual(stats.llm, 1)
            self.assertTrue(cache.exists())

            second = FakeRepairer(cache, "gpt-test", api_key="test", batch_pages=10)
            cached, cached_stats = second.repair_document(source)
            self.assertEqual(cached, repaired)
            self.assertEqual(cached_stats.cache, 1)

    def test_targeted_retry_can_bypass_a_stale_cached_repair(self) -> None:
        source = "\\fieldvalue{broken\n% LEXOID_PAGE_COMPLETED: 1/1\n"
        stale = "\\fieldvalue{still-broken\n% LEXOID_PAGE_COMPLETED: 1/1\n"
        fixed = "\\fieldvalue{fixed}\n% LEXOID_PAGE_COMPLETED: 1/1\n"
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "syntax-cache.json"
            first = SequencedRepairer(
                cache, "gpt-test", api_key="test", replacements=[stale]
            )
            cached, _ = first.repair_document(
                source, target_pages={1}, diagnostic_hint="unclosed brace"
            )
            self.assertEqual(cached, stale)

            retry = SequencedRepairer(
                cache, "gpt-test", api_key="test", replacements=[fixed]
            )
            repaired, stats = retry.repair_document(
                source, target_pages={1}, diagnostic_hint="unclosed brace",
                bypass_cache=True,
            )

        self.assertEqual(repaired, fixed)
        self.assertEqual(stats.llm, 1)
        self.assertEqual(stats.cache, 0)

    def test_cli_stops_before_optimization_when_repair_remains_invalid(self) -> None:
        from . import cli

        class StillBrokenRepairer:
            def __init__(self, *args, **kwargs):
                pass

            def repair_document(self, source: str, **kwargs):
                return source, SimpleNamespace(
                    batches=1, cache=0, deterministic_end_documents_removed=0,
                    failed=0, llm=1, pages=1, rejected=0, unchanged=1,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken.tex"
            output = root / "output.tex"
            source.write_text(
                "\\begin{document}\n\\fieldvalue{broken\n"
                "% LEXOID_PAGE_COMPLETED: 1/1\n\\end{document}\n",
                encoding="utf-8",
            )
            with mock.patch.object(cli, "LLMSyntaxRepairer", StillBrokenRepairer), \
                    mock.patch.object(
                        cli, "annotate_fields",
                        side_effect=AssertionError("optimization must not run"),
                    ):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--llm-syntax-repair", "--no-preamble",
                ])

        self.assertEqual(result, 6)
        self.assertFalse(output.exists())

    def test_cli_retries_every_error_page_with_local_diagnostics_until_valid(self) -> None:
        from . import cli

        calls = []

        class IncrementalRepairer:
            def __init__(self, *args, **kwargs):
                pass

            def repair_document(self, source: str, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    repaired = source
                elif len(calls) == 2:
                    repaired = source.replace("broken {text", "broken {text}")
                else:
                    repaired = source.replace("a & b & c", "a & b")
                return repaired, SimpleNamespace(
                    batches=1, cache=0, deterministic_end_documents_removed=0,
                    failed=0, llm=1, pages=2, rejected=0, unchanged=0,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken-pages.tex"
            output = root / "output.tex"
            source.write_text(
                "\\begin{document}\n"
                "\\begin{tabular}{ll}\n"
                "a & b & c \\\\\n"
                "\\end{tabular}\n"
                "% LEXOID_PAGE_COMPLETED: 1/2\n"
                "broken {text\n"
                "% LEXOID_PAGE_COMPLETED: 2/2\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            with mock.patch.object(cli, "LLMSyntaxRepairer", IncrementalRepairer):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--llm-syntax-repair", "--no-preamble", "--no-anchor",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1]["target_pages"], {1, 2})
        self.assertEqual(calls[2]["target_pages"], {1})
        hints = calls[1]["diagnostic_hints"]
        self.assertIn("document line 3, local line 3", hints[1])
        self.assertIn("document line 6, local line 1", hints[2])

    def test_cli_continues_after_failed_batch_when_current_source_is_safe(self) -> None:
        from . import cli

        class CachedFixWithFailedBatchRepairer:
            def __init__(self, *args, **kwargs):
                pass

            def repair_document(self, source: str, **kwargs):
                return source.replace(
                    r"\fieldvalue{original", r"\fieldvalue{original}"
                ), SimpleNamespace(
                    batches=2, cache=1, deterministic_end_documents_removed=0,
                    failed=1, llm=0, pages=2, rejected=0, unchanged=1,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "valid.tex"
            output = root / "output.tex"
            source.write_text(
                "\\begin{document}\n"
                "% #VALUE_ID: LEX-P0001-V0001\n"
                "% #FIELD_VALUE: Name\n"
                "\\fieldvalue{original\n"
                "% LEXOID_PAGE_COMPLETED: 1/2\n"
                "valid text\n"
                "% LEXOID_PAGE_COMPLETED: 2/2\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                cli, "LLMSyntaxRepairer", CachedFixWithFailedBatchRepairer
            ):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--llm-syntax-repair", "--no-preamble", "--no-anchor",
                ])
            self.assertEqual(result, 0)
            self.assertTrue(output.exists())
            self.assertIn(
                "SYNTAX_REPAIR_DEGRADED_SAFE",
                output.with_suffix(".texopt.log").read_text("utf-8"),
            )

    def test_cli_blocks_failed_batch_when_repair_changes_protected_value(self) -> None:
        from . import cli

        class FailedWithUnsafeCachedChangeRepairer:
            def __init__(self, *args, **kwargs):
                pass

            def repair_document(self, source: str, **kwargs):
                return source.replace("original", "tampered"), SimpleNamespace(
                    batches=2, cache=1, deterministic_end_documents_removed=0,
                    failed=1, llm=0, pages=2, rejected=0, unchanged=1,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "valid.tex"
            output = root / "output.tex"
            source.write_text(
                "\\begin{document}\n"
                "% #VALUE_ID: LEX-P0001-V0001\n"
                "% #FIELD_VALUE: Name\n"
                "\\fieldvalue{original}\n"
                "% LEXOID_PAGE_COMPLETED: 1/2\n"
                "unchanged\n"
                "% LEXOID_PAGE_COMPLETED: 2/2\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                cli, "LLMSyntaxRepairer", FailedWithUnsafeCachedChangeRepairer
            ), mock.patch.object(
                cli, "annotate_fields",
                side_effect=AssertionError("optimization must not run"),
            ):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--llm-syntax-repair", "--no-preamble", "--no-anchor",
                ])

            self.assertEqual(result, 5)
            self.assertFalse(output.exists())
            log = output.with_suffix(".texopt.log").read_text("utf-8")
            self.assertIn("field_payloads_changed", log)

    def test_cli_blocks_failed_batch_when_structural_errors_remain(self) -> None:
        from . import cli

        class FailedPassThroughRepairer:
            def __init__(self, *args, **kwargs):
                pass

            def repair_document(self, source: str, **kwargs):
                return source, SimpleNamespace(
                    batches=1, cache=0, deterministic_end_documents_removed=0,
                    failed=1, llm=0, pages=1, rejected=0, unchanged=1,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken.tex"
            output = root / "output.tex"
            source.write_text(
                "\\begin{document}\n\\fieldvalue{broken\n"
                "% LEXOID_PAGE_COMPLETED: 1/1\n\\end{document}\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                cli, "LLMSyntaxRepairer", FailedPassThroughRepairer
            ), mock.patch.object(
                cli, "annotate_fields",
                side_effect=AssertionError("optimization must not run"),
            ):
                result = cli.main([
                    "optimise", str(source), "-o", str(output),
                    "--llm-syntax-repair", "--no-preamble", "--no-anchor",
                ])

            self.assertEqual(result, 6)
            self.assertFalse(output.exists())

    def test_failed_request_is_not_reported_as_llm_unchanged(self) -> None:
        events = []
        repairer = FailedRepairer(
            Path("unused-cache.json"), "gpt-test", api_key="test",
            event=lambda name, message, **data: events.append((name, data)),
        )
        _, stats = repairer.repair_document(
            "x\n% LEXOID_PAGE_COMPLETED: 1/1\n"
        )
        self.assertEqual(stats.failed, 1)
        batch = [data for name, data in events if name == "SYNTAX_REPAIR_BATCH"][0]
        self.assertEqual(batch["source"], "failed")

    def test_valid_model_result_is_accepted_with_audit_flags_only(self) -> None:
        events = []
        source = (
            "% #VALUE_ID: LEX-P0001-V0001\n"
            "% #FIELD_VALUE: 名称\n\\fieldvalue{中文值}\n"
            "% LEXOID_PAGE_COMPLETED: 1/1\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            repairer = AuthoritativeRepairer(
                Path(directory) / "cache.json", "gpt-test", api_key="test",
                event=lambda name, message, **data: events.append((name, data)),
            )
            repaired, stats = repairer.repair_document(source)
        self.assertIn("模型改值", repaired)
        self.assertEqual(stats.llm, 1)
        self.assertEqual(stats.rejected, 0)
        batch = [data for name, data in events if name == "SYNTAX_REPAIR_BATCH"][0]
        self.assertIn("field_payloads_changed", batch["audit_flags"])
        self.assertEqual(batch["rejection_reasons"], [])


if __name__ == "__main__":
    unittest.main()
