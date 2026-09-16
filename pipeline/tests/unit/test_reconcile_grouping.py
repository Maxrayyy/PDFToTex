"""Regression: unknown field coordinates must not multiply full-page calls."""
import json
from pathlib import Path

import pytest

from texopt.optimization.reconcile import reconcile_document


def fixture(tmp_path, boxes):
    fields, lines = [], []
    for index, box in enumerate(boxes, 1):
        fid = f"LEX-P0001-V{index:04d}"
        fields.append(dict(field_id=fid, label="日期", value="2026.13.01", bbox=box,
                           paddle_text="", model_guess="2026.13.01", confidence=0.5,
                           needs_review=True, history=[]))
        lines.append(f"% #VALUE_ID: {fid}\n% #FIELD_VALUE: 日期\n\\fieldvalue{{2026.13.01}}")
    source, evidence = tmp_path / "source.tex", tmp_path / "evidence.json"
    source.write_text("\n".join(lines))
    evidence.write_text(json.dumps(dict(schema="recognition/v1", document_sha256="sourcehash",
        pages=[dict(page=1, render=dict(dpi=240, width=2000, height=2800),
                    ocr_blocks=[], fields=fields)])))
    return source, Path("unused.pdf"), evidence, tmp_path / "out.tex", tmp_path / "fields.json"


def reply(value="2026.12.01"):
    return dict(value=value, confidence=0.95, needs_review=False, reason="image confirms")


class Adapter:
    model = "gpt-6-astra"

    def __init__(self, failure=False, malformed=None):
        self.calls = []
        self.failure = failure
        self.malformed = malformed

    def reconcile(self, source_pdf, candidate, retry_dpi):
        self.calls.append(("crop", [candidate.field_id]))
        return reply()

    def reconcile_page(self, source_pdf, candidates, retry_dpi, **details):
        self.calls.append(("page", [c.field_id for c in candidates]))
        if self.failure:
            raise ValueError("invalid provider response")
        items = [dict(field_id=c.field_id, **reply()) for c in candidates]
        if self.malformed == "missing":
            items.pop()
        if self.malformed == "duplicate":
            items.append(items[0])
        if self.malformed == "extra":
            items.append(dict(field_id="LEX-P0001-V9999", **reply()))
        return {"fields": items}


def test_full_page_fields_are_reviewed_once_and_cached(tmp_path):
    args = fixture(tmp_path, [[0, 0, 2000, 2800]] * 3)
    adapter = Adapter()
    result = reconcile_document(*args, adapter=adapter)
    assert adapter.calls == [("page", [f"LEX-P0001-V{i:04d}" for i in range(1, 4)])]
    assert result.selected == result.confirmed == 3
    assert all(f['value'] == '2026.12.01' and not f['needs_review'] for f in result.fields)
    reconcile_document(*args, adapter=adapter)
    assert len(adapter.calls) == 1


def test_mixed_page_keeps_accurate_local_crop(tmp_path):
    args = fixture(tmp_path, [[0, 0, 2000, 2800], [300, 400, 500, 480], [0, 0, 2000, 2800]])
    adapter = Adapter()
    result = reconcile_document(*args, adapter=adapter)
    assert sorted(adapter.calls) == [('crop', ['LEX-P0001-V0002']),
                                     ('page', ['LEX-P0001-V0001', 'LEX-P0001-V0003'])]
    assert result.confirmed == 3


@pytest.mark.parametrize('box', [None, [0, 0, 1999, 2799], [-1, 0, 2000, 2800]])
def test_missing_or_unreliable_coordinates_use_one_page_request(tmp_path, box):
    args = fixture(tmp_path, [box, box])
    adapter = Adapter()
    report = reconcile_document(*args, adapter=adapter)
    assert len(adapter.calls) == 1 and adapter.calls[0][0] == 'page'
    assert report.confirmed == 2


@pytest.mark.parametrize('malformed', ['missing', 'duplicate', 'extra'])
def test_invalid_group_response_never_confirms_or_falls_back_per_field(tmp_path, malformed):
    args = fixture(tmp_path, [[0, 0, 2000, 2800]] * 3)
    adapter = Adapter(malformed=malformed)
    result = reconcile_document(*args, adapter=adapter)
    assert len(adapter.calls) == 1
    assert result.confirmed == 0 and result.failed == 3
    assert all(f['value'] == '2026.13.01' and f['needs_review'] for f in result.fields)


def test_page_request_budget_survives_restarts_and_changed_field_values(tmp_path):
    args = fixture(tmp_path, [[0, 0, 2000, 2800]] * 3)
    adapter = Adapter(failure=True)
    for _ in range(4):
        result = reconcile_document(*args, adapter=adapter, max_page_requests=2)
    assert len(adapter.calls) == 2
    assert result.failed == 3 and result.confirmed == 0
    assert all(e['type'] == 'PageReviewBudgetExceeded' for e in result.errors)
    assert all(f['needs_review'] for f in result.fields)
    args[0].write_text(args[0].read_text().replace('2026.13.01', '2027.13.01'))
    reconcile_document(*args, adapter=adapter, max_page_requests=2)
    assert len(adapter.calls) == 2


def test_corrupt_budget_does_not_reset_and_spend_again(tmp_path):
    args = fixture(tmp_path, [[0, 0, 2000, 2800]] * 2)
    args[-1].with_suffix('.reconcile-budget.json').write_text('broken')
    adapter = Adapter()
    result = reconcile_document(*args, adapter=adapter)
    assert not adapter.calls
    assert result.failed == 2 and all(f['needs_review'] for f in result.fields)


def test_adapter_sends_one_real_image_and_logs_request_scope(tmp_path, monkeypatch):
    import base64
    import io
    import pypdfium2 as pdfium
    from PIL import Image
    from texopt.optimization import reconcile
    source = tmp_path / 'page.pdf'
    doc = pdfium.PdfDocument.new()
    try:
        doc.new_page(600, 840).close()
        doc.save(source)
    finally:
        doc.close()
    args = fixture(tmp_path, [[300, 400, 500, 480], [0, 0, 2000, 2800]])
    candidates = reconcile.select_exceptional_fields(args[0].read_text(), json.loads(args[2].read_text()))
    calls = []
    monkeypatch.setenv('OPENAI_API_KEY', 'test-only')

    def transport(request, timeout, **scope):
        payload = json.loads(request.data)
        content = payload['messages'][0]['content']
        images = [x for x in content if x['type'] == 'image_url']
        assert len(images) == 1
        with Image.open(io.BytesIO(base64.b64decode(images[0]['image_url']['url'].split(',')[1]))) as image:
            assert image.size == (scope['image_width'], scope['image_height'])
        calls.append(scope)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(reply())}}]}

    monkeypatch.setattr(reconcile, 'request_json', transport)
    adapter = reconcile.FieldReconcileAdapter('gpt-6-astra')
    adapter.reconcile(source, candidates[0], 480)
    adapter.reconcile_page(source, candidates[1:], 480, page_context=[], page_request_number=1)
    assert [(c['review_mode'], c['image_width'], c['image_height']) for c in calls] == [
        ('crop', 560, 320), ('page', 4000, 5600)]
    assert calls[1]['field_ids'] == ['LEX-P0001-V0002']
    assert calls[1]['field_count'] == calls[1]['page_request_number'] == 1


def test_budget_reservations_are_atomic_under_concurrent_workers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from texopt.optimization.reconcile_budget import reserve_page_request, PageReviewBudgetExceeded
    def attempt(_):
        try:
            return reserve_page_request(tmp_path / 'budget.json', 'source', 1, 2)
        except PageReviewBudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(10)))
    assert sorted(r for r in results if r is not None) == [1, 2]


@pytest.mark.parametrize('box', [None, 10, ['bad', 1, 2, 3]])
def test_bad_bbox_with_ocr_still_enters_grouped_review(tmp_path, box):
    args = fixture(tmp_path, [box, box])
    data = json.loads(args[2].read_text())
    data['pages'][0]['ocr_blocks'] = [{'bbox': [1, 1, 20, 20], 'score': 0.1}]
    if box is None:
        for f in data['pages'][0]['fields']:
            del f['bbox']
    args[2].write_text(json.dumps(data))
    adapter = Adapter()
    result = reconcile_document(*args, adapter=adapter)
    assert result.confirmed == 2 and len(adapter.calls) == 1
