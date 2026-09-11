import json
from pathlib import Path
import pytest

from texopt.stages import BatchConfig, artifact_paths, build_stage_commands, run_batch


def test_artifacts_preserve_category_number_and_separate_tex_directories(tmp_path):
    source = tmp_path / "Downloads" / "U1" / "批次数据" / "A31" / "record.pdf"
    paths = artifact_paths(source, tmp_path / "Downloads", tmp_path / "data")
    assert paths["raw"] == tmp_path / "data/U1/批次数据/A31/tex/record.tex"
    assert paths["optimized"] == tmp_path / "data/optimized/U1/批次数据/A31/record.tex"
    assert paths["evidence"].parent == paths["raw"].parent


def test_unit_worker_can_publish_to_shared_clean_root(tmp_path, monkeypatch):
    root = tmp_path / "data/optimized/U1"
    monkeypatch.setenv("PIPELINE_PUBLISH_ROOT", str(root))
    config = BatchConfig.from_env()
    source = tmp_path / "Downloads/U1/批次数据/A31/record.pdf"
    paths = artifact_paths(source, tmp_path / "Downloads/U1", tmp_path / "data/U1",
                           publish_root=config.publish_root)
    assert paths["optimized"] == root / "批次数据/A31/record.tex"
    before = build_stage_commands(source, tmp_path / "Downloads/U1", tmp_path / "data/U1", BatchConfig())
    after = build_stage_commands(source, tmp_path / "Downloads/U1", tmp_path / "data/U1", config)
    assert before == after


def test_three_stages_use_distinct_models_and_require_compilation(tmp_path, monkeypatch):
    monkeypatch.setenv("LEXOID_MODEL", "gpt-test-vision")
    monkeypatch.setenv("RECONCILE_MODEL", "gpt-test-review")
    monkeypatch.setenv("TEXOPT_MODEL", "gpt-test-naming")
    source = tmp_path / "Downloads/U2/批次/A37/input.pdf"
    stages = build_stage_commands(source, tmp_path / "Downloads", tmp_path / "data", BatchConfig())
    assert [stage.stage for stage in stages] == ["recognize", "reconcile", "optimise"]
    assert stages[0].argv[stages[0].argv.index("--model") + 1] == "gpt-test-vision"
    assert stages[1].argv[stages[1].argv.index("--model") + 1] == "gpt-test-review"
    assert stages[2].argv[stages[2].argv.index("--llm-model") + 1] == "gpt-test-naming"
    assert "--compile-check" in stages[2].argv
    assert "--llm-syntax-repair" not in stages[2].argv
    assert "--allow-opaque" not in stages[2].argv


def test_batch_defaults_to_vision_and_can_explicitly_select_paddle(tmp_path, monkeypatch):
    source = tmp_path / "Downloads/U1/batch/record.pdf"
    monkeypatch.delenv("RECOGNITION_OCR", raising=False)
    config = BatchConfig.from_env()
    assert config.ocr == "none"
    stages = build_stage_commands(source, tmp_path / "Downloads", tmp_path / "data", config)
    assert stages[0].argv[stages[0].argv.index("--ocr") + 1] == "none"
    assert "--evidence-output" in stages[0].argv
    monkeypatch.setenv("RECOGNITION_OCR", "paddleocr")
    stages = build_stage_commands(source, tmp_path / "Downloads", tmp_path / "data", BatchConfig.from_env())
    assert stages[0].argv[stages[0].argv.index("--ocr") + 1] == "paddleocr"
    monkeypatch.setenv("RECOGNITION_OCR", "invalid")
    with pytest.raises(ValueError, match="OCR"):
        BatchConfig.from_env()


def test_stage_timeout_can_accommodate_large_documents(monkeypatch):
    monkeypatch.setenv("PIPELINE_STAGE_TIMEOUT_SECONDS", "21600")
    assert BatchConfig.from_env().timeout == 21600
    monkeypatch.setenv("PIPELINE_STAGE_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="timeout"):
        BatchConfig.from_env()


def test_optimizer_change_reuses_recognition_and_keeps_raw_tex(tmp_path):
    source = tmp_path / "Downloads/U1/批次数据/A31/record.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    called = []
    def runner(stage, log_path):
        called.append(stage.stage)
        for path in stage.outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".tex":
                path.write_text("page\n% LEXOID_PAGE_COMPLETED: 1/1\n")
            elif path.name.endswith(".recognition.json"):
                path.write_text(json.dumps({"schema": "recognition/v1", "pages": [{"page": 1, "fields": []}]}))
            elif path.name.endswith(".report.json"):
                path.write_text(json.dumps({"compile_check": {"ok": True, "passes": 2},
                    "layout_check": {"ok": True, "expected_pages": 1, "actual_pages": 1,
                                     "page_map": [{"source_page": 1, "start": 1, "end": 1}]}}))
            else:
                path.write_text("{}")
        return 0
    kwargs = dict(source_root=tmp_path / "Downloads", output_root=tmp_path / "data",
                  runner=runner, page_counter=lambda path: 1)
    assert run_batch(**kwargs) == 0
    assert called == ["recognize", "reconcile", "optimise"]
    paths = artifact_paths(source, kwargs["source_root"], kwargs["output_root"])
    assert list(paths["optimized"].parent.iterdir()) == [paths["optimized"]]
    assert paths["report"].is_file()
    assert paths["registry"].is_file()
    assert paths["compile_log"].is_file()
    called.clear()
    assert run_batch(**kwargs, config=BatchConfig(optimizer_model="gpt-6-astra")) == 0
    assert called == ["optimise"]
    assert artifact_paths(source, kwargs["source_root"], kwargs["output_root"])["raw"].exists()


def test_failed_compile_never_publishes_optimized_tex(tmp_path):
    source = tmp_path / "Downloads/U1/批次数据/A31/record.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    def runner(stage, log_path):
        for path in stage.outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".tex":
                path.write_text("page\n% LEXOID_PAGE_COMPLETED: 1/1\n")
            elif path.name.endswith(".recognition.json"):
                path.write_text(json.dumps({"schema": "recognition/v1", "pages": [{"page": 1, "fields": []}]}))
            else:
                path.write_text("{}")
        return 6 if stage.stage == "optimise" else 0
    assert run_batch(tmp_path / "Downloads", tmp_path / "data", runner=runner,
                     page_counter=lambda path: 1) == 1
    paths = artifact_paths(source, tmp_path / "Downloads", tmp_path / "data")
    assert paths["raw"].exists()
    assert not paths["optimized"].exists()


@pytest.mark.parametrize("bad_stage", ["recognize", "optimise"])
def test_placeholder_never_publishes_even_with_successful_compile(tmp_path, bad_stage):
    source = tmp_path / "input/record.pdf"
    source.parent.mkdir()
    source.write_bytes(b"pdf")
    paths = artifact_paths(source, source.parent, tmp_path / "out")
    paths["optimized"].parent.mkdir(parents=True)
    paths["optimized"].write_text("previous good result")
    called = []

    def runner(stage, log):
        called.append(stage.stage)
        for path in stage.outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".tex":
                body = "% LEXOID_RECOGNITION_FALLBACK\n\\null" if stage.stage == bad_stage else "content"
                path.write_text(body + "\n% LEXOID_PAGE_COMPLETED: 1/1\n")
            elif path.name.endswith(".recognition.json"):
                path.write_text(json.dumps({"schema": "recognition/v1", "pages": [{"page": 1}]}))
            elif path.name.endswith(".report.json"):
                path.write_text(json.dumps({"compile_check": {"ok": True, "passes": 2}}))
            else:
                path.write_text("{}")
        return 0

    assert run_batch(source.parent, tmp_path / "out", runner=runner, page_counter=lambda _: 1) == 1
    assert paths["optimized"].read_text() == "previous good result"
    if bad_stage == "recognize":
        assert called == ["recognize"]


def test_publish_rejects_placeholder_but_allows_recognized_blank_page(tmp_path):
    from texopt.stages import _publish
    source, target = tmp_path / "source.tex", tmp_path / "public.tex"
    source.write_text("% LEXOID_RECOGNITION_FALLBACK\n\\null\n% LEXOID_PAGE_COMPLETED: 1/1\n")
    with pytest.raises(ValueError, match="recognition"):
        _publish(source, target)
    source.write_text("\\null\n% LEXOID_PAGE_COMPLETED: 1/1\n")
    _publish(source, target)
    assert target.exists()


def test_service_outage_pauses_batch_before_the_next_document(tmp_path):
    source_root = tmp_path / "input"
    source_root.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (source_root / name).write_bytes(b"pdf")
    visited = []

    def runner(stage, log):
        visited.append(stage.inputs[0].name)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.with_suffix(".calls.jsonl").write_text(json.dumps({
            "event": "model_service_unavailable", "stage": "recognize", "page": 2}) + "\n")
        return 1

    assert run_batch(source_root, tmp_path / "out", runner=runner, page_counter=lambda _: 2) == 1
    assert visited == ["a.pdf"]
    result = json.loads((tmp_path / "out/manifest.json").read_text())
    assert result["files"][0]["status"] == "paused"


def test_exact_path_queue_distinguishes_same_names_and_empty_means_no_work(tmp_path):
    source_root = tmp_path / "input"
    for relative in ("A31/record.pdf", "A32/record.pdf"):
        source = source_root / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(b"pdf")
    visited = []
    kwargs = dict(source_root=source_root, output_root=tmp_path / "data",
                  page_counter=lambda p: visited.append(p.relative_to(source_root).as_posix()) or 1,
                  runner=lambda stage, log: 1)
    assert run_batch(**kwargs, include_paths=["A32/record.pdf"]) == 1
    assert visited == ["A32/record.pdf"]
    visited.clear()
    assert run_batch(**kwargs, include_paths=[]) == 0
    assert visited == []
    with pytest.raises(ValueError, match="Missing"):
        run_batch(**kwargs, include_paths=["A99/missing.pdf"])
    assert visited == []
