import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import multiprocessing

import pytest

from .llm import FieldSpec, TableNameRequest, LLMBatchNamer


def request(value="20", *, unit="C", label="Temperature"):
    return TableNameRequest(page=1, ordinal=2, section="Incubation",
        headers=["Parameter", f"Value ({unit})"],
        sample_rows=[[label, rf"\fieldvalue{{{value}}}"]],
        fields=[FieldSpec("r01c01", label, f"Value ({unit})", value, label)])


def test_structure_cache_reuses_values_pages_and_workers_but_not_units(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda self, req: calls.append(req) or
                        {"table": "incubation", "fields": {"r01c01": "temperature"}})
    cache = tmp_path / "names.sqlite3"
    first = LLMBatchNamer(cache, model="gpt-5.6-terra", api_key="test")
    first.name_table(request("20"))
    second = LLMBatchNamer(cache, model="gpt-5.6-terra", api_key="test")
    result = second.name_table(replace(request("35"), page=78, ordinal=100))
    assert len(calls) == 1 and result.source == "cache"
    second.name_table(request("35", unit="F"))
    assert len(calls) == 2
    second.name_table(replace(request("35"), section="Freezing"))
    assert len(calls) == 3


def test_model_and_prompt_versions_do_not_share_semantic_cache(tmp_path, monkeypatch):
    from . import llm
    calls = []
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda self, req: calls.append(1) or
                        {"table": "incubation", "fields": {"r01c01": "temperature"}})
    cache = tmp_path / "names.sqlite3"
    for model in ("gpt-5.6-terra", "gpt-5.6-sol"):
        LLMBatchNamer(cache, model=model, api_key="test").name_table(request())
    monkeypatch.setattr(llm, "PROMPT", llm.PROMPT + "\nNew naming policy.")
    LLMBatchNamer(cache, model="gpt-5.6-sol", api_key="test").name_table(request())
    assert len(calls) == 3


def test_simultaneous_namers_share_one_request(tmp_path, monkeypatch):
    calls = []
    barrier = threading.Barrier(2)

    def call(self, req):
        calls.append(1)
        time.sleep(.15)
        return {"table": "incubation", "fields": {"r01c01": "temperature"}}

    monkeypatch.setattr(LLMBatchNamer, "_call", call)

    def run(value):
        namer = LLMBatchNamer(tmp_path / "names.sqlite3", model="gpt-5.6-terra", api_key="test")
        barrier.wait(timeout=5)
        return namer.name_table(request(value))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ["20", "30"]))
    assert len(calls) == 1
    assert sorted(r.source for r in results) == ["cache", "llm"]


@pytest.mark.parametrize("message, reason", [
    ("database is locked", "lock_timeout"),
    ("private detail", "cache_unavailable"),
])
def test_cache_failure_logs_reason_and_preserves_fallback(tmp_path, monkeypatch, caplog, message, reason):
    from . import llm
    import sqlite3

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr(llm, "NamingCache", unavailable)
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda *_: pytest.fail("Unexpected model request"))
    namer = LLMBatchNamer(tmp_path / "names.sqlite3", model="gpt-5.6-terra", api_key="test")
    assert namer.name_table(request()).source == "heuristic"
    assert "naming_cache_error" in caplog.text
    assert "OperationalError" in caplog.text
    assert "page=1" in caplog.text
    assert f"reason={reason}" in caplog.text
    assert "private detail" not in caplog.text


def test_unlabelled_values_retain_disambiguating_context(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda self, req: calls.append(1) or
                        {"table": "record", "fields": {"r00c00": "detail"}})
    namer = LLMBatchNamer(tmp_path / "names.sqlite3", model="gpt-5.6-terra", api_key="test")
    for value in ("Operator", "Approver"):
        namer.name_table(TableNameRequest(page=1, fields=[FieldSpec("r00c00", "", "", value)]))
    assert len(calls) == 2


def source_tex():
    return (r"\documentclass{article}\newcommand{\fieldvalue}[1]{#1}\begin{document}" +
        "\n\\begin{tabular}{ll}\nParameter & Value\\\\\\hline\nTemperature &\n"
        "% #VALUE_ID: LEX-P0001-V0001\n% #FIELD_VALUE: Temperature (C)\n"
        "\\fieldvalue{20}\\\\\n\\end{tabular}\n"
        "% #VALUE_ID: LEX-P0001-V0002\n% #FIELD_VALUE: Operator\n\\fieldvalue{Alice}\n"
        "% LEXOID_PAGE_COMPLETED: 1/1\n\\end{document}\n")


def test_default_conversion_defers_names_but_preserves_basic_json_and_pdf(tmp_path, monkeypatch):
    from . import cli

    monkeypatch.delenv("TEXOPT_MODEL", raising=False)
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda *_: pytest.fail("Naming called during PDF conversion"))
    source = tmp_path / "source.tex"
    source.write_text(source_tex())
    output, registry, report = [tmp_path / name for name in ("out.tex", "fields.json", "report.json")]
    assert cli.main(["optimise", str(source), "-o", str(output), "--registry", str(registry),
                     "--report", str(report), "--compile-check"]) == 0
    data = json.loads(registry.read_text())
    assert {f["field_id"] for f in data["fields"]} == {"LEX-P0001-V0001", "LEX-P0001-V0002"}
    assert {f["value"] for f in data["fields"]} == {"20", "Alice"}
    assert all(f["name_status"] == "pending" and f["label"] for f in data["fields"])
    assert json.loads(report.read_text())["compile_check"]["ok"]
    assert output.with_suffix(".naming.json").exists()

    before = output.read_bytes()
    data["fields"][0].update(history=[{"action": "human_review"}], source_bbox=[1, 2, 3, 4],
                              needs_review=True)
    registry.write_text(json.dumps(data))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda self, req:
        {"table": "record", "fields": {f.key: "meaning_" + f.key for f in req.fields}})
    monkeypatch.setattr(cli, "_compile_latex", lambda *a, **kw: pytest.fail("Naming recompiled TEX"))
    enriched = tmp_path / "enriched.json"
    assert cli.main(["name-fields", str(output), "--registry", str(registry),
        "--output", str(enriched), "--model", "gpt-5.6-terra",
        "--name-cache", str(tmp_path / "names.sqlite3")]) == 0
    after = json.loads(enriched.read_text())
    assert output.read_bytes() == before
    for old, new in zip(data["fields"], after["fields"]):
        assert new["field_id"] == old["field_id"] and new["value"] == old["value"]
        assert new["label"] == old["label"] and new["tex_line"] == old["tex_line"]
        assert new["name_status"] == "complete"
    assert after["fields"][0]["history"] == data["fields"][0]["history"]
    assert after["fields"][0]["source_bbox"] == [1, 2, 3, 4]
    assert after["fields"][0]["needs_review"] is True


def test_unavailable_naming_and_cache_keep_pdf_conversion_successful(tmp_path, monkeypatch):
    from . import cli

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))
    source = tmp_path / "source.tex"
    source.write_text(source_tex())
    cache = tmp_path / "not-a-directory"
    cache.write_text("blocked")
    registry = tmp_path / "fields.json"
    assert cli.main(["optimise", str(source), "-o", str(tmp_path / "out.tex"),
        "--registry", str(registry), "--semantic-naming", "inline", "--llm-model", "gpt-5.6-terra",
        "--name-cache", str(cache / "names.sqlite3"), "--compile-check"]) == 0
    assert all(f["name_status"] == "pending" for f in json.loads(registry.read_text())["fields"])


def test_kimi_uses_isolated_naming_transport(tmp_path, monkeypatch):
    from . import llm

    monkeypatch.setenv("OPENAI_BASE_URL", "https://vision.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "vision-test")
    monkeypatch.setenv("TEXOPT_NAMING_PROVIDER", "openai")
    monkeypatch.setenv("TEXOPT_NAMING_BASE_URL", "https://names.example/v1/")
    monkeypatch.setenv("TEXOPT_NAMING_API_KEY", "naming-test")
    calls = []

    def respond(req, timeout, **kwargs):
        calls.append(req)
        assert req.full_url == "https://names.example/v1/chat/completions"
        assert req.get_header("Authorization") == "Bearer naming-test"
        body = json.loads(req.data)
        assert body["model"] == "kimi-k3"
        assert body["max_tokens"] == 4096
        assert "max_completion_tokens" not in body
        return {"choices": [{"message": {"content": json.dumps({
            "table": "incubation_record", "fields": {"r01c01": "culture_temperature"}})}}]}

    monkeypatch.setattr(llm, "request_json", respond)
    namer = LLMBatchNamer(tmp_path / "names.sqlite3", model="kimi-k3")
    assert namer.name_table(request()).source == "llm"
    assert namer.name_table(request("35")).source == "cache"
    assert len(calls) == 1
    assert llm.openai_api_url() == "https://vision.example/v1/chat/completions"


def _cache_process(path, barrier, count, results):
    from .naming_cache import NamingCache
    barrier.wait(timeout=10)
    cache = NamingCache(path)

    def compute():
        with count.get_lock():
            count.value += 1
        time.sleep(.2)
        return {"table": "record"}

    results.put(cache.get_or_compute("shared", compute))


def test_independent_processes_only_send_one_naming_request(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier, count, results = ctx.Barrier(2), ctx.Value("i", 0), ctx.Queue()
    jobs = [ctx.Process(target=_cache_process,
        args=(str(tmp_path / "names.sqlite3"), barrier, count, results)) for _ in range(2)]
    try:
        for job in jobs:
            job.start()
        returned = [results.get(timeout=15) for _ in jobs]
        for job in jobs:
            job.join(timeout=5)
            assert job.exitcode == 0
        assert count.value == 1
        assert sorted(source for _, source in returned) == ["cache", "llm"]
    finally:
        for job in jobs:
            if job.is_alive():
                job.terminate()
                job.join()


@pytest.mark.parametrize("change", ["stale", "overwrite_tex"])
def test_invalid_enrichment_rejected_before_network(tmp_path, monkeypatch, change):
    from .semantic_naming import enrich, tex_hash
    from types import SimpleNamespace

    tex = tmp_path / "source.tex"
    tex.write_text(source_tex())
    digest = tex_hash(tex.read_text())
    plan, registry = tmp_path / "plan.json", tmp_path / "fields.json"
    plan.write_text(json.dumps({"schema": "semantic-naming-plan/v1", "source_tex_sha256": digest,
                               "requests": []}))
    registry.write_text(json.dumps({"source_tex_sha256": digest, "fields": []}))
    if change == "stale":
        tex.write_text(source_tex() + "% edited")
    before = tex.read_bytes()
    monkeypatch.setattr(LLMBatchNamer, "__init__", lambda *a, **kw: pytest.fail("Model initialized"))
    args = SimpleNamespace(tex=tex, registry=registry, plan=plan,
                           output=tex if change == "overwrite_tex" else tmp_path / "enriched.json")
    with pytest.raises(ValueError):
        enrich(args)
    assert tex.read_bytes() == before


def test_partial_metadata_error_retains_other_fields(tmp_path):
    from .fields import basic_fields

    tex = source_tex().replace("\\fieldvalue{Alice}", "\\fieldvalue{Alice")
    got = basic_fields([], tex)
    assert len(got) == 2
    assert got[0]["value"] == "20"
    assert got[1]["field_id"] == "LEX-P0001-V0002"
    assert got[1]["extraction_status"] == "invalid"
    assert got[1]["raw_tex_segment"]


def test_deferred_naming_still_allows_syntax_repair(tmp_path, monkeypatch):
    from . import cli
    from types import SimpleNamespace
    called = []

    class Repairer:
        def __init__(self, *args, **kwargs):
            assert kwargs["model"] == "gpt-5.6-sol"

        def repair_document(self, source, **kwargs):
            called.append(1)
            return source, SimpleNamespace(batches=1, cache=0, failed=0, llm=1, pages=1,
                rejected=0, unchanged=1, deterministic_end_documents_removed=0)

    monkeypatch.setattr(cli, "LLMSyntaxRepairer", Repairer)
    monkeypatch.setattr(LLMBatchNamer, "_call", lambda *a: pytest.fail("Naming called"))
    source = tmp_path / "source.tex"
    source.write_text(source_tex())
    assert cli.main(["optimise", str(source), "-o", str(tmp_path / "out.tex"),
        "--semantic-naming", "deferred", "--llm-syntax-repair", "--repair-model", "gpt-5.6-sol",
        "--compile-check"]) == 0
    assert called == [1]


def test_independent_endpoint_never_borrows_vision_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TEXOPT_NAMING_BASE_URL", "https://names.example/v1")
    monkeypatch.delenv("TEXOPT_NAMING_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "vision-test")
    namer = LLMBatchNamer(tmp_path / "names.sqlite3", model="kimi-k3")
    assert not namer.api_key


def test_kind_and_table_structure_invalidate_cache():
    original = request()
    key = original.semantic_fingerprint("kimi-k3", "provider")
    changed = replace(original, fields=[replace(original.fields[0], kind="checkbox")])
    assert changed.semantic_fingerprint("kimi-k3", "provider") != key
    assert replace(original, structure=[[1, 2]]).semantic_fingerprint("kimi-k3", "provider") != key


def test_cache_recovers_expired_owner_and_backoffs_failure(tmp_path):
    from .naming_cache import NamingCache
    cache = NamingCache(tmp_path / "names.sqlite3")
    with cache.connect() as db:
        db.execute("INSERT INTO names VALUES(?,? ,?,'pending',NULL)", ("key", "dead", time.time() - 1))
    calls = []
    assert cache.get_or_compute("key", lambda: calls.append(1))[0] is None
    assert cache.get_or_compute("key", lambda: pytest.fail("Immediate retry"))[0] is None
    assert calls == [1]
    with cache.connect() as db:
        db.execute("UPDATE names SET expires=0 WHERE key='key'")
    assert cache.get_or_compute("key", lambda: {"table": "record"}) == ({"table": "record"}, "llm")
