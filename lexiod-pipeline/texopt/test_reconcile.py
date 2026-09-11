import json
from pathlib import Path

import pytest

from .reconcile import (
    _normalized, field_segments, plain_value, reconcile_document, render_crop,
    select_exceptional_fields,
)


TEX = r"""\documentclass{article}
\begin{document}
% #VALUE_ID: LEX-P0001-V0001
% #FIELD_VALUE: Operator
% #TODO #HANDWRITTEN: Chang; uncertain
\fieldvalue{\handwritten{Chang}}
% LEXOID_PAGE_COMPLETED: 1/1
\end{document}
"""


def evidence():
    return {"schema": "recognition/v1", "document_sha256": "unused", "pages": [{
        "page": 1, "render": {"dpi": 240, "width": 100, "height": 200},
        "ocr_blocks": [{"text": "Zhang", "bbox": [10, 20, 50, 40], "score": 0.5}],
        "fields": [{"field_id": "LEX-P0001-V0001", "label": "Operator",
            "bbox": [10, 20, 50, 40], "value": "Chang", "paddle_text": "Zhang",
            "model_guess": "Chang", "confidence": 0.5, "needs_review": True, "history": []}],
    }]}


def date_fixture(year="2023", month="07", day="28"):
    ev = evidence()
    ev["pages"][0]["ocr_blocks"] = []
    fields = []
    lines = []
    for index, (unit, value) in enumerate(zip(("年", "月", "日"), (year, month, day)), 1):
        fid = f"LEX-P0001-V{index:04d}"
        label = f"测试日期 {unit}"
        fields.append({**ev["pages"][0]["fields"][0], "field_id": fid,
            "label": label, "value": value, "model_guess": value,
            "paddle_text": "", "confidence": 0.99, "needs_review": False})
        lines.append(f"% #VALUE_ID: {fid}\n% #FIELD_VALUE: {label}\n"
                     + rf"\underline{{\fieldvalue{{{value}}}}}" + unit)
    ev["pages"][0]["fields"] = fields
    return "\n".join(lines), ev


@pytest.mark.parametrize("parts", [("2023", "07", "28"), ("2024", "02", "29")])
def test_valid_split_date_needs_no_model_review(parts):
    tex, ev = date_fixture(*parts)
    assert select_exceptional_fields(tex, ev) == []


@pytest.mark.parametrize("parts", [("2023", "02", "29"), ("2023", "04", "31"),
                                    ("2023", "13", "01")])
def test_invalid_combined_date_is_still_reviewed(parts):
    tex, ev = date_fixture(*parts)
    candidates = select_exceptional_fields(tex, ev)
    assert candidates
    assert all("critical_format_invalid" in c.reasons for c in candidates)


def test_split_date_does_not_hide_other_review_reasons():
    tex, ev = date_fixture()
    ev["pages"][0]["fields"][1]["needs_review"] = True
    candidates = select_exceptional_fields(tex, ev)
    assert len(candidates) == 1
    assert candidates[0].reasons == ("model_uncertainty",)


def test_selector_retains_all_reasons_for_one_field():
    result = select_exceptional_fields(TEX, evidence())
    assert len(result) == 1
    assert set(result[0].reasons) >= {"paddle_model_conflict", "handwritten_review_marker", "low_paddle_score"}


class Adapter:
    model = "gpt-6-astra"
    def __init__(self, uncertain=False, fail=False):
        self.uncertain, self.fail = uncertain, fail
    def reconcile(self, source_pdf, candidate, retry_dpi):
        if self.fail:
            raise RuntimeError("Unavailable")
        return {"value": "Zhang", "confidence": 0.95, "needs_review": self.uncertain,
                "reason": "crop confirms strokes"}


def run(tmp_path, adapter):
    source, ev = tmp_path / "raw.tex", tmp_path / "raw.json"
    source.write_text(TEX)
    ev.write_text(json.dumps(evidence()))
    return reconcile_document(source, Path("source.pdf"), ev,
        tmp_path / "out.tex", tmp_path / "fields.json", adapter=adapter,
        review_content=True)


@pytest.mark.parametrize("kind", ["printed", "handwritten", "ocr_conflict", "checkbox"])
def test_content_uncertainty_is_deferred_without_model_calls(tmp_path, kind):
    from . import cli

    tex = TEX.replace("% #TODO #HANDWRITTEN: Chang; uncertain\n", "")
    data = evidence()
    data["pages"][0]["ocr_blocks"] = []
    field = data["pages"][0]["fields"][0]
    field["paddle_text"] = ""
    if kind != "handwritten":
        tex = tex.replace(r"\handwritten{Chang}", "Chang")
    else:
        tex = TEX
    if kind == "ocr_conflict":
        data = evidence()
        data["pages"][0]["fields"][0]["needs_review"] = False
    if kind == "checkbox":
        tex = tex.replace(r"\fieldvalue{Chang}", r"\fieldvalue{\checkboxfield{unclear}}")
        field.update(value="unclear", model_guess="unclear")
    source, ev = tmp_path / "raw.tex", tmp_path / "raw.json"
    source.write_text(tex)
    ev.write_text(json.dumps(data))
    # A nonexistent PDF verifies that the default CLI never tries to crop/review.
    assert cli.main(["reconcile", str(source), "--source-pdf", "missing.pdf",
        "--recognition-evidence", str(ev), "-o", str(tmp_path / "out.tex"),
        "--fields", str(tmp_path / "fields.json")]) == 0
    assert (tmp_path / "out.tex").read_text() == tex
    result = json.loads((tmp_path / "fields.json").read_text())
    assert result["reconciliation"]["selected"] == 0
    assert result["reconciliation"]["deferred"] == 1
    assert result["fields"][0]["needs_review"]
    assert result["fields"][0]["history"] == []
    assert result["fields"][0]["value"] == data["pages"][0]["fields"][0]["value"]


def test_default_policy_still_reviews_invalid_date(tmp_path):
    tex, data = date_fixture("2023", "02", "29")
    source, ev = tmp_path / "raw.tex", tmp_path / "raw.json"
    source.write_text(tex)
    ev.write_text(json.dumps(data))
    calls = []

    class DateAdapter(Adapter):
        def reconcile(self, source_pdf, candidate, retry_dpi):
            calls.append(candidate.field_id)
            return {"value": candidate.field["value"], "confidence": 0.5,
                    "needs_review": True, "reason": "unresolved date"}

    report = reconcile_document(source, Path("source.pdf"), ev,
        tmp_path / "out.tex", tmp_path / "fields.json", adapter=DateAdapter())
    assert len(calls) == report.selected == 3
    assert report.deferred == 0


def test_reconcile_outage_preserves_tex_and_stops_requests_without_failing_cli(tmp_path, monkeypatch):
    from urllib.error import URLError
    from . import cli, reconcile
    tex, data = date_fixture("2023", "02", "29")
    source, ev = tmp_path / "raw.tex", tmp_path / "raw.json"
    source.write_text(tex)
    ev.write_text(json.dumps(data))
    calls = []

    class Offline(Adapter):
        def reconcile(self, *args):
            calls.append(1)
            raise URLError("Connection refused")

    monkeypatch.setattr(reconcile, "FieldReconcileAdapter", lambda _: Offline())
    output, fields = tmp_path / "out.tex", tmp_path / "fields.json"
    assert cli.main(["reconcile", str(source), "--source-pdf", "source.pdf",
        "--recognition-evidence", str(ev), "-o", str(output), "--fields", str(fields),
        "--concurrency", "1"]) == 0
    assert len(calls) == 1
    assert {k: v["value"] for k, v in field_segments(output.read_text()).items()} == {
        k: v["value"] for k, v in field_segments(tex).items()}
    assert all(f["needs_review"] for f in json.loads(fields.read_text())["fields"])


def test_confirmed_reply_updates_value_marker_and_history(tmp_path):
    report = run(tmp_path, Adapter())
    assert r"\fieldvalue{\handwritten{Zhang}}" in report.tex
    assert "% #HANDWRITTEN: Zhang" in report.tex
    assert "% #TODO #HANDWRITTEN:" not in report.tex
    assert report.fields[0]["value"] == "Zhang"
    assert report.fields[0]["history"][-1]["from"] == "Chang"
    assert not report.fields[0]["needs_review"]


def test_equivalent_plain_reply_preserves_existing_tex_formatting(tmp_path):
    source, ev = tmp_path / "raw.tex", tmp_path / "raw.json"
    tex_value = r"10ml：$5\times10^{7}$个细胞"
    registry_value = "10ml：5×10^7个细胞"
    source.write_text(TEX.replace("Chang", tex_value))
    data = evidence()
    data["pages"][0]["fields"][0].update(
        value=registry_value, model_guess=registry_value, paddle_text="")
    ev.write_text(json.dumps(data))

    class CaretAdapter(Adapter):
        def reconcile(self, source_pdf, candidate, retry_dpi):
            return {"value": registry_value, "confidence": 0.95, "needs_review": False,
                    "reason": "crop confirms value"}

    report = reconcile_document(source, Path("source.pdf"), ev,
        tmp_path / "out.tex", tmp_path / "fields.json", adapter=CaretAdapter(),
        review_content=True)

    assert tex_value in report.tex
    visible_value = field_segments(report.tex)["LEX-P0001-V0001"]["value"]
    assert report.fields[0]["value"] == visible_value
    assert not report.fields[0]["needs_review"]


def test_final_tex_value_wins_when_rendered_value_differs_from_registry(tmp_path, monkeypatch):
    from . import reconcile

    monkeypatch.setattr(reconcile, "escape_tex", lambda value: "Rendered")
    report = run(tmp_path, Adapter())

    assert report.fields[0]["value"] == "Rendered"
    assert report.fields[0]["needs_review"]
    assert report.fields[0]["history"][-1]["stage"] == "tex_registry_sync"


def test_reconciliation_and_optimizer_preserve_underlined_field(tmp_path):
    from . import cli

    source, ev = tmp_path / "raw.tex", tmp_path / "raw.json"
    tex = TEX.replace(r"\begin{document}",
        "\\newcommand{\\fieldvalue}[1]{#1}\n"
        "\\newcommand{\\handwritten}[1]{#1}\n\\begin{document}")
    tex = tex.replace(r"\fieldvalue{\handwritten{Chang}}",
        r"\underline{\makebox[2cm][c]{\fieldvalue{\handwritten{Chang}}}}")
    source.write_text(tex)
    ev.write_text(json.dumps(evidence()))
    reconciled = tmp_path / "reconciled.tex"
    result = reconcile_document(source, Path("source.pdf"), ev,
        reconciled, tmp_path / "fields.json", adapter=Adapter(), review_content=True)
    expected = r"\underline{\makebox[2cm][c]{\fieldvalue{\handwritten{Zhang}}}}"
    assert expected in result.tex
    output = tmp_path / "optimized.tex"
    assert cli.main(["optimise", str(reconciled), "-o", str(output),
        "--no-llm", "--compile-check"]) == 0
    assert expected in output.read_text()


@pytest.mark.parametrize("adapter", [Adapter(uncertain=True), Adapter(fail=True)])
def test_unresolved_reply_preserves_guess_and_review_marker(tmp_path, adapter):
    report = run(tmp_path, adapter)
    assert r"\fieldvalue{\handwritten{Chang}}" in report.tex
    assert "% #TODO #HANDWRITTEN: Chang" in report.tex
    assert report.fields[0]["needs_review"]


def test_selector_rejects_tex_evidence_value_mismatch():
    with pytest.raises(ValueError, match="value"):
        select_exceptional_fields(TEX.replace("{Chang}", "{Changed}"), evidence())


@pytest.mark.parametrize("latex,visible", [
    (r"\handwritten{$\checkmark$}", "\u2713"),
    (r"12ml:$6.0\times10^7$", "12ml:6.0\u00d710\u2077"),
    (r"\handwritten{$4.01\times10^{10}$}", "4.01\u00d710\u00b9\u2070"),
    (r"\textbf{A\&B}", "A&B"),
])
def test_formatted_values_compare_without_losing_mathematical_meaning(latex, visible):
    assert _normalized(plain_value(latex)) == _normalized(visible)


def test_exponents_are_not_equivalent_to_plain_digits():
    assert _normalized(plain_value(r"$10^2$")) != _normalized("102")


def test_crop_uses_installed_pdfium_api(tmp_path):
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument.new()
    source = tmp_path / "source.pdf"
    try:
        page = document.new_page(100, 200)
        page.close()
        document.save(source)
    finally:
        document.close()
    candidate = select_exceptional_fields(TEX, evidence())[0]
    assert render_crop(source, candidate, 480).startswith("data:image/png;base64,")
