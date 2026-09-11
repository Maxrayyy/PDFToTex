from .acceptance import evaluate, evaluate_metrics


def test_acceptance_rejects_missing_duplicate_and_unordered_pages():
    result = evaluate([1, 2, 2, 4], 4)
    assert not result.passed
    assert result.missing_pages == [3]
    assert result.duplicate_pages == [2]
    assert not evaluate([2, 1, 3, 4], 4).passed


def test_acceptance_thresholds_are_inclusive_and_missing_metrics_fail():
    assert evaluate_metrics(critical_improvement=.30, table_accuracy=.95,
        checkbox_accuracy=.98, speed_improvement=.60, compiled_documents=5).passed
    assert not evaluate_metrics(critical_improvement=.29, table_accuracy=.95,
        checkbox_accuracy=.98, speed_improvement=.60, compiled_documents=5).passed
    assert not evaluate_metrics(compiled_documents=5).passed
