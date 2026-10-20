"""Acceptance behavior through the repair transaction, independent of CLI wiring."""
import pytest

from texopt.optimization.repair_transaction import RepairTransaction
from texopt.optimization.syntax_repair import SyntaxRepairStats


SOURCE = r'''\begin{document}
% #VALUE_ID: LEX-P0001-V0001
% #FIELD_VALUE: batch
\fieldvalue{2026051501}
\begin{tabular}{ll}a & b\\\end{tabular}
% LEXOID_PAGE_COMPLETED: 1/1
\end{document}
'''


class CandidateGenerator:
    def __init__(self, candidate, *, cached=False, failed=0):
        self.candidate = candidate
        self.cached = cached
        self.failed = failed
        self.calls = []

    def repair_document(self, source, **kwargs):
        self.calls.append(kwargs)
        return self.candidate, SyntaxRepairStats(
            pages=1, batches=1, cache=int(self.cached),
            llm=int(not self.cached), failed=self.failed,
        )


@pytest.mark.parametrize('cached', [False, True])
def test_cached_and_fresh_harmful_candidates_restore_source(cached):
    generator = CandidateGenerator(SOURCE.replace('a & b', 'a & b & c'), cached=cached)
    result = RepairTransaction(generator).repair(SOURCE)
    assert result.ok
    assert r"\begin{tabular}{ll}a & b\\\end{tabular}" in result.source
    assert "a & b & c" not in result.source
    assert r"\fieldvalue{2026051501}" in result.source


@pytest.mark.parametrize('path', ['initial', 'probe'])
def test_successful_value_change_remains_authoritative(path):
    changed = SOURCE.replace('2026051501', '2026051503')
    transaction = RepairTransaction(CandidateGenerator(changed))
    result = (transaction.repair(SOURCE) if path == 'initial' else
              transaction.repair_probe(SOURCE, target_pages={1}, diagnostic_hint='probe error'))
    assert result.ok
    assert '2026051503' in result.source


def test_failed_partial_batch_with_changed_value_restores_input():
    changed = SOURCE.replace('2026051501', '2026051503')
    result = RepairTransaction(CandidateGenerator(changed, failed=1)).repair(SOURCE)
    assert result.exit_code == 5
    assert result.source == SOURCE


@pytest.mark.parametrize('candidate', [SOURCE, SOURCE.replace('a & b', 'a & b & c')])
def test_probe_never_returns_unchanged_or_invalid_text_as_recovered(candidate):
    generator = CandidateGenerator(candidate)
    result = RepairTransaction(generator).repair_probe(
        SOURCE, target_pages={1}, diagnostic_hint='probe error')
    assert result.exit_code == 3
    assert result.source == SOURCE
    assert len(generator.calls) == 1
