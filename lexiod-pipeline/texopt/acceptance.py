"""Acceptance gates for the five-document hybrid recognition corpus."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from .textio import write_utf8_atomic

CORPUS = (
    "S22C-726080515020.pdf", "S22C-726080515560.pdf", "S22C-726080516000.pdf",
    "S22C-726080516040.pdf", "S22C-726080516090.pdf",
)


@dataclass(frozen=True)
class PageAcceptance:
    passed: bool
    missing_pages: list[int]
    duplicate_pages: list[int]
    unexpected_pages: list[int]
    ordered: bool


def evaluate(document_pages, expected_pages):
    counts = Counter(document_pages)
    expected = list(range(1, expected_pages + 1))
    missing = [p for p in expected if p not in counts]
    duplicate = sorted(p for p, count in counts.items() if count > 1)
    unexpected = sorted(set(document_pages) - set(expected))
    ordered = list(document_pages) == expected
    return PageAcceptance(ordered and not missing and not duplicate and not unexpected,
                          missing, duplicate, unexpected, ordered)


@dataclass(frozen=True)
class MetricAcceptance:
    passed: bool
    failed_gates: list[str]


def evaluate_metrics(critical_improvement=None, table_accuracy=None, checkbox_accuracy=None,
                     speed_improvement=None, compiled_documents=0):
    thresholds = {"critical_improvement": (critical_improvement, .30),
                  "table_accuracy": (table_accuracy, .95),
                  "checkbox_accuracy": (checkbox_accuracy, .98),
                  "speed_improvement": (speed_improvement, .60)}
    failed = [name for name, (value, minimum) in thresholds.items()
              if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 1]
    if compiled_documents != 5:
        failed.append("compiled_documents")
    return MetricAcceptance(not failed, failed)


def preflight(source_root):
    from .pdf_batch import _page_count, discover_pdfs
    from .stages import sha256_file
    found = [path for path in discover_pdfs(Path(source_root)) if path.name in CORPUS]
    if sorted(p.name for p in found) != sorted(CORPUS):
        raise ValueError("Acceptance requires exactly one PDF for each of the five corpus filenames")
    files = [{"source": str(p.relative_to(source_root)), "sha256": sha256_file(p),
              "pages": _page_count(p)} for p in found]
    total = sum(item["pages"] for item in files)
    if total != 375:
        raise ValueError(f"Acceptance corpus has {total} pages; expected 375")
    return {"files": files, "total_pages": total}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--metrics", type=Path)
    args = parser.parse_args(argv)
    report = preflight(args.source_root)
    if args.preflight_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    from .stages import BatchConfig, run_batch
    status = run_batch(args.source_root, args.output_root, BatchConfig.from_env(), include=CORPUS)
    metrics = json.loads(args.metrics.read_text("utf-8")) if args.metrics else {}
    gates = evaluate_metrics(**metrics)
    report.update(run_status=status, metrics=metrics, acceptance=asdict(gates))
    write_utf8_atomic(args.output_root / "_hybrid-result.json", json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if status == 0 and gates.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
