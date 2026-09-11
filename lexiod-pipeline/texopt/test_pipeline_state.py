"""Durability, recovery and de-duplication tests for cron runs."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from .lexoid_job import new_job, resume_argv
from .pipeline_daemon import (Config, FolderWorker, env_bool, last_completed_page,
                              merge_lexoid_resume, mirrored_log, relative_source,
                              prepare_partial_for_resume, resume_page_after,
                              write_done_marker)
from .pipeline_state import PipelineState


class PipelineStateTests(unittest.TestCase):
    def test_pipeline_defaults_to_three_attempts(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Config.from_env().max_attempts, 3)

    def test_json_stage_flag_is_strict_and_defaults_off(self) -> None:
        from unittest.mock import patch

        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(env_bool("ENABLE_JSON_STAGE", False))
        with patch.dict("os.environ", {"ENABLE_JSON_STAGE": "true"}, clear=True):
            self.assertTrue(env_bool("ENABLE_JSON_STAGE", False))
        with patch.dict("os.environ", {"ENABLE_JSON_STAGE": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                env_bool("ENABLE_JSON_STAGE", False)

    def test_completed_fingerprint_is_not_claimed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = PipelineState(Path(directory) / "state.sqlite3")
            job = state.claim("optimizer", "/data/a.tex", "hash-a")
            self.assertIsNotNone(job)
            state.complete(int(job), "/data/a.optimized.tex")
            self.assertIsNone(state.claim("optimizer", "/data/a.tex", "hash-a"))
            self.assertIsNotNone(state.claim("optimizer", "/data/a.tex", "hash-b"))

    def test_interrupted_running_job_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = PipelineState(Path(directory) / "state.sqlite3")
            job = state.claim("json", "/data/reviewed.tex", "hash-a")
            self.assertIsNotNone(job)
            self.assertEqual(state.recover_interrupted(), 1)
            reclaimed = state.claim("json", "/data/reviewed.tex", "hash-a")
            self.assertEqual(reclaimed, job)
            self.assertEqual(state.attempts(int(job)), 2)

    def test_resume_merge_keeps_completed_base_and_new_shard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base.tex"
            shard = Path(directory) / "shard.tex"
            base.write_text(
                "\\documentclass{article}\n\\begin{document}\npage1\n"
                "% LEXOID_PAGE_COMPLETED: 1/3\npartial page2",
                encoding="utf-8",
            )
            shard.write_text(
                "\\documentclass{article}\n\\begin{document}\npage2\n"
                "% LEXOID_PAGE_COMPLETED: 2/3\npage3\n"
                "% LEXOID_PAGE_COMPLETED: 3/3\n\\end{document}\n",
                encoding="utf-8",
            )
            merged = merge_lexoid_resume(base, shard, 1)
            self.assertIn("page1", merged)
            self.assertIn("page2", merged)
            self.assertIn("page3", merged)
            self.assertNotIn("partial page2", merged)
            self.assertEqual(merged.count(r"\begin{document}"), 1)

    def test_resume_starts_after_last_completed_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "document.tex"
            partial.write_text(
                "page1\n% LEXOID_PAGE_COMPLETED: 1/5\n"
                "page2\n% LEXOID_PAGE_COMPLETED: 2/5\n"
                "page3\n\\end{document}\n% LEXOID_PAGE_COMPLETED: 3/5\n"
                "half-written page4",
                encoding="utf-8",
            )
            self.assertEqual(last_completed_page(partial), 3)
            self.assertEqual(resume_page_after(partial), 4)
            self.assertEqual(prepare_partial_for_resume(partial), 4)
            self.assertNotIn("half-written page4", partial.read_text("utf-8"))
            self.assertNotIn(r"\end{document}", partial.read_text("utf-8"))

            job = new_job(directory, "document.pdf", output_name="document.tex")
            argv, start_page = resume_argv(job)
            self.assertEqual(start_page, 4)
            self.assertEqual(argv[argv.index("--start-page") + 1], "4")
            self.assertEqual(argv[argv.index("-o") + 1], "/data/document.tex")
            self.assertFalse(any(".part0004.tex" in arg for arg in argv))
            self.assertEqual(job.last_complete_page, 3)
            durable = partial.read_text("utf-8")
            self.assertIn("LEXOID_PAGE_COMPLETED: 3/5", durable)
            self.assertNotIn("half-written page4", durable)

    def test_done_marker_does_not_look_like_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "reviewed.json"
            output.write_text("{}", encoding="utf-8")
            source = root / "reviewed.tex"
            source.write_text("source", encoding="utf-8")
            write_done_marker(output, "json", source, "hash-a", 1)
            self.assertTrue((root / "reviewed.json.pipeline.done").exists())
            self.assertEqual([path.name for path in root.glob("*.json")],
                             ["reviewed.json"])

    def test_recursive_scanner_finds_nested_files_and_skips_hidden_trees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            nested = inbox / "批次A" / "2026"
            hidden = inbox / ".working"
            nested.mkdir(parents=True)
            hidden.mkdir(parents=True)
            wanted = nested / "sample.pdf"
            wanted.write_bytes(b"pdf")
            (hidden / "ignored.pdf").write_bytes(b"pdf")
            handled = []

            def handler(source: Path, _job: int, _fingerprint: str) -> Path:
                handled.append(source)
                output = root / "output" / source.name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("done", encoding="utf-8")
                return output

            state = Mock()
            state.claim.return_value = 1
            state.attempts.return_value = 1
            worker = FolderWorker(
                "lexoid", inbox, (".pdf",),
                SimpleNamespace(stable_seconds=0, max_attempts=0),
                state, Mock(), threading.Event(), handler,
            )
            worker.scan_once()
            self.assertEqual(handled, [wanted])

    def test_nested_paths_and_logs_preserve_relative_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "input"
            source = source_root / "客户A" / "2026" / "sample.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"pdf")
            relative = relative_source(source, source_root)
            self.assertEqual(relative, Path("客户A/2026/sample.pdf"))
            log = mirrored_log(root / "logs", "lexoid", relative)
            self.assertEqual(
                log, root / "logs/lexoid/客户A/2026/sample.pdf.log"
            )


if __name__ == "__main__":
    unittest.main()
