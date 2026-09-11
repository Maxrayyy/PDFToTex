import json
from pathlib import Path

import pytest

from .fields import FieldRecord, write_registry


@pytest.mark.parametrize("failure", ["value", "ids", "malformed", "unexpected"])
def test_registry_failure_does_not_block_compiled_tex(tmp_path, monkeypatch, failure):
    from . import cli

    source = tmp_path / "source.tex"
    source.write_text(
        "\\documentclass{article}\n\\newcommand{\\fieldvalue}[1]{#1}\n"
        "\\begin{document}\n% #VALUE_ID: LEX-P0001-V0001\n"
        "% #FIELD_VALUE: Name\n\\fieldvalue{Complete text}\n"
        "% LEXOID_PAGE_COMPLETED: 1/1\n\\end{document}\n"
    )
    provenance = tmp_path / "incoming.json"
    field = {"field_id": "LEX-P0001-V0001", "value": "Wrong value"}
    if failure == "ids":
        field["field_id"] = "LEX-P0001-V9999"
    provenance.write_text("invalid json" if failure == "malformed" else
                          json.dumps({"fields": [field]}))
    if failure == "unexpected":
        def fail(*args, **kwargs):
            raise RuntimeError("Registry serializer failed")
        monkeypatch.setattr(cli, "write_registry", fail)
    output = tmp_path / "output.tex"
    registry = tmp_path / "fields.json"
    report = tmp_path / "report.json"
    assert cli.main([
        "optimise", str(source), "-o", str(output), "--no-llm",
        "--compile-check", "--registry", str(registry),
        "--source-registry", str(provenance), "--report", str(report),
    ]) == 0
    assert "Complete text" in output.read_text()
    result = json.loads(report.read_text())
    assert result["compile_check"]["ok"] is True
    assert result["registry_check"]["ok"] is False
    assert result["registry_check"]["error"]
    degraded = json.loads(registry.read_text())
    assert degraded["status"] == "degraded"
    assert len(degraded["fields"]) == 1
    retained = degraded["fields"][0]
    assert retained["field_id"] == "LEX-P0001-V0001"
    assert retained["value"] == "Complete text"
    assert retained["label"] == "Name"
    assert retained["name_status"] == "pending"


def test_registry_keeps_review_history_and_checks_tex_value(tmp_path):
    incoming = tmp_path / "reconciled.fields.json"
    field = {"field_id": "LEX-P0001-V0001", "value": "Zhang", "needs_review": True,
             "paddle_text": "Chang", "model_guess": "Zhang", "confidence": 0.7,
             "history": [{"stage": "reconcile", "from": "Chang", "to": "Zhang", "reason": "crop"}]}
    incoming.write_text(json.dumps({"fields": [field]}))
    output = tmp_path / "optimized.fields.json"
    record = FieldRecord(field["field_id"], "operator", 1, "table", "operator", "", "",
                         "Zhang", 12, "fp")
    tex = "% #VALUE_ID: LEX-P0001-V0001\n% #TODO #HANDWRITTEN: Zhang\n\\fieldvalue{\\handwritten{Zhang}}"
    write_registry([record], output, provenance_path=incoming, tex=tex)
    got = json.loads(output.read_text())["fields"][0]
    assert got["history"] == field["history"]
    assert got["needs_review"] is True
    assert got["semantic_alias"] == "operator"
    with pytest.raises(ValueError, match="value"):
        write_registry([record], output, provenance_path=incoming, tex=tex.replace("{Zhang}", "{Changed}"))
