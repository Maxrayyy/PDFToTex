"""Escalation must catch broken rows without paying twice for healthy pages."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from .page_fallback import check_document, compile_page, structural_issues, upgrade_pages
from lexoid.core.recognition.models import PageEvidence, RenderMetadata, RenderedPage, VisionPageResult


BROKEN = r"""\begin{tabular}{|l|l|l|l|l|l|l|}
\hline
1 & name & brand & size &
% #VALUE_ID: LEX-P0003-V0001
% #FIELD_VALUE: Batch
\fieldvalue{new}\\
% #VALUE_ID: LEX-P0003-V0002
% #FIELD_VALUE: Old batch
\fieldvalue{old}\\
% #VALUE_ID: LEX-P0003-V0003
% #FIELD_VALUE: Correction
\fieldvalue{note} & 2 & yes\\\hline
\end{tabular}
"""


def test_split_field_rows_are_detected_even_when_tex_compiles():
    assert "SPLIT_FIELD_ROW" in {issue.code for issue in structural_issues(BROKEN)}


@pytest.mark.parametrize("body", [
    r"\multicolumn{3}{l}{note}\\\hline a & b & c\\",
    r"\multirow{2}{*}{a} & b & c\\ & d & e\\",
    r"a & \begin{tabular}{l} \fieldvalue{one}\\\fieldvalue{two}\end{tabular} & c\\",
    r"a & \parbox{2cm}{\fieldvalue{one}\\\fieldvalue{two}} & c\\",
    r"\fieldvalue{note}\\\hline a & b & c\\",
])
def test_valid_spans_nested_rows_and_single_underfull_row_do_not_escalate(body):
    source = r"\begin{tabular}{lll}" + body + r"\end{tabular}"
    assert not structural_issues(source)


def test_local_compile_catches_raggedleft_row_break_and_accepts_extra_pages(tmp_path):
    preamble = r"\documentclass{article}\usepackage{array}\begin{document}"
    broken = preamble + r"\begin{tabular}{p{2cm}p{2cm}p{2cm}}a&b&\raggedleft c\\d&&e\end{tabular}\end{document}"
    result = compile_page(broken, tmp_path / "broken")
    assert "COMPILE_ERROR" in {issue.code for issue in result}
    assert not compile_page(preamble + r"one\newpage two\end{document}", tmp_path / "valid")


def sample(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source bytes")
    raw = tmp_path / "source.tex"
    preamble = r"\documentclass{article}\newcommand{\fieldvalue}[1]{#1}\begin{document}" + "\n"
    raw.write_text(preamble + "healthy\n% LEXOID_PAGE_COMPLETED: 1/3\n"
                   "middle\n% LEXOID_PAGE_COMPLETED: 2/3\n" + BROKEN +
                   "% LEXOID_PAGE_COMPLETED: 3/3\n\\end{document}\n")
    evidence_path = tmp_path / "source.recognition.json"
    evidence = {"schema": "recognition/v1", "model": "gpt-5.6-sol", "pages": [
        PageEvidence("recognition/v1", page, RenderMetadata(240, 100, 200)).to_dict()
        for page in range(1, 4)]}
    evidence_path.write_text(json.dumps(evidence))
    return source, raw, evidence_path


def test_only_broken_page_is_replaced_and_failure_or_resume_never_repeats_call(tmp_path):
    source, raw, evidence = sample(tmp_path)
    initial = raw.read_text()
    calls = []

    def recognize(page, page_evidence, total):
        calls.append((page.page, total))
        return VisionPageResult(page.page, "fixed\n% LEXOID_PAGE_COMPLETED: 3/3\n", ())

    def render(path, page, dpi, auto_orient):
        assert path == source and dpi == 240
        return RenderedPage(page, dpi, 100, 200, Image.new("RGB", (100, 200)))

    kwargs = dict(source=source, raw=raw, evidence_path=evidence, cache_dir=tmp_path / "cache",
                  primary_model="gpt-5.6-sol", fallback_model="gpt-6-astra",
                  recognize=recognize, renderer=render, compiler=lambda *_: [])
    report = upgrade_pages(**kwargs)
    assert calls == [(3, 3)]
    assert report["upgraded"] == [3]
    assert raw.read_text().startswith(initial.split(BROKEN)[0])
    assert raw.read_text().endswith("\\end{document}\n")
    payload = json.loads(evidence.read_text())
    assert payload["page_models"] == {"1": "gpt-5.6-sol", "2": "gpt-5.6-sol", "3": "gpt-6-astra"}
    assert [p["page"] for p in payload["pages"]] == [1, 2, 3]
    raw.write_text(initial)  # Primary recognition checkpoint replay.
    upgrade_pages(**kwargs)
    assert calls == [(3, 3)]


def test_failed_fallback_is_cached_and_original_tex_retained(tmp_path):
    source, raw, evidence = sample(tmp_path)
    initial = raw.read_text()
    calls = []

    def recognize(*args):
        calls.append(1)
        raise RuntimeError("upstream failure")

    kwargs = dict(source=source, raw=raw, evidence_path=evidence, cache_dir=tmp_path / "cache",
                  primary_model="gpt-5.6-sol", fallback_model="gpt-6-astra", recognize=recognize,
                  renderer=lambda _, page, dpi, auto_orient: RenderedPage(
                      page, dpi, 100, 200, Image.new("RGB", (100, 200))), compiler=lambda *_: [])
    for _ in range(2):
        result = upgrade_pages(**kwargs)
        assert result["unresolved"] == [3]
        assert raw.read_text() == initial
    assert calls == [1]


@pytest.mark.parametrize("error", [ConnectionError("private response"),
    type("HTTPFailure", (Exception,), {"status_code": 503})("private response")])
def test_transport_failure_pauses_upgrade_and_can_resume_after_recovery(tmp_path, error):
    from lexoid.core.recognition.service import AdaptiveConcurrency
    from lexoid.core.request_errors import ModelUnavailableError
    from .page_fallback import PageFallback
    from lexoid.core.recognition.models import PageRecognitionResult

    source, raw, evidence = sample(tmp_path)
    calls = []

    def recognize(page, *_):
        calls.append(1)
        if len(calls) == 1:
            raise error
        return VisionPageResult(3, "fixed\n% LEXOID_PAGE_COMPLETED: 3/3", ())

    kwargs = dict(source=source, cache_dir=tmp_path / "cache", primary_model="gpt-5.6-sol",
        fallback_model="gpt-6-astra", recognize=recognize, compiler=lambda *_: [],
        renderer=lambda _, page, dpi, auto: RenderedPage(page, dpi, 100, 200, Image.new("RGB", (100, 200))))
    result = PageRecognitionResult(3, BROKEN + "% LEXOID_PAGE_COMPLETED: 3/3",
                                  PageEvidence("recognition/v1", 3, RenderMetadata(240, 100, 200)), "key")
    gate = AdaptiveConcurrency(2)
    with pytest.raises(ModelUnavailableError):
        PageFallback(**kwargs)(result, 3, gate)
    assert gate.failure is not None
    output = PageFallback(**kwargs)(result, 3, AdaptiveConcurrency(2))
    assert output.latex.startswith("fixed")
    assert len(calls) == 2


def test_check_cache_avoids_recompiling_unchanged_pages(tmp_path):
    source, raw, evidence = sample(tmp_path)
    calls = []
    compiler = lambda tex, directory: calls.append(tex) or []
    first = check_document(raw.read_text(), tmp_path / "checks", compiler=compiler)
    second = check_document(raw.read_text(), tmp_path / "checks", compiler=compiler)
    assert first == second
    assert len(calls) == 2  # Structurally broken page does not need compilation.
    assert set(first) == {3}


def test_batch_routes_to_streaming_checker_only_when_fallback_model_differs(tmp_path):
    from .stages import BatchConfig, build_stage_commands

    config = BatchConfig(vision_model="gpt-5.6-sol", fallback_model="gpt-6-astra")
    stage = build_stage_commands(tmp_path / "a.pdf", tmp_path, tmp_path / "out", config)[0]
    assert stage.argv[:3] == ["python", "-m", "texopt.page_fallback"]
    assert stage.argv[stage.argv.index("--fallback-model") + 1] == "gpt-6-astra"
    direct = build_stage_commands(tmp_path / "a.pdf", tmp_path, tmp_path / "out",
                                  replace(config, vision_model="gpt-6-astra"))[0]
    assert direct.argv[:2] == ["lexoid", "latex"]


def test_cli_streams_checked_results_into_tex_and_matching_evidence(tmp_path, monkeypatch):
    import sys
    from . import page_fallback
    from lexoid.core.recognition import service

    source, raw, evidence = sample(tmp_path)
    original_recognizer = service.DocumentRecognizer
    original_processor = page_fallback.PageFallback

    class Vision:
        def recognize(self, page, ev, total):
            prefix = ("\\documentclass{article}\n\\begin{document}\n" if page.page == 1 else "")
            text = prefix + (BROKEN if page.page == 3 else "healthy\n")
            return VisionPageResult(page.page, text + f"% LEXOID_PAGE_COMPLETED: {page.page}/{total}\n", ())

    def render(_, page, dpi, auto_orient=True):
        return RenderedPage(page, dpi, 100, 200, Image.new("RGB", (100, 200)))

    def recognizer(**kwargs):
        assert kwargs["config"].max_page_attempts == 1
        return original_recognizer(**kwargs, renderer=render, page_counter=lambda _: 3,
                                   vision_adapter=Vision())

    def processor(*args, **kwargs):
        return original_processor(*args, **kwargs, renderer=render, compiler=lambda *_: [],
            recognize=lambda p, e, total: VisionPageResult(p.page,
                f"fixed\n% LEXOID_PAGE_COMPLETED: {p.page}/{total}\n", ()))

    monkeypatch.setattr(service, "DocumentRecognizer", recognizer)
    monkeypatch.setattr(page_fallback, "PageFallback", processor)
    monkeypatch.setattr(sys, "argv", ["page_fallback", "--input", str(source), "--output", str(raw),
        "--model", "gpt-5.6-sol", "--fallback-model", "gpt-6-astra", "--evidence-output", str(evidence),
        "--cache-dir", str(tmp_path / "cache"), "--resume"])
    page_fallback.main()
    assert "fixed" in raw.read_text() and "\\end{document}" in raw.read_text()
    payload = json.loads(evidence.read_text())
    assert payload["page_models"]["3"] == "gpt-6-astra"
    assert payload["page_fallback"]["upgraded"] == [3]
    assert [p["page"] for p in payload["pages"]] == [1, 2, 3]
