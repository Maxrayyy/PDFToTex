"""Own candidate acceptance, structural retries and rollback for model repairs.

The generator cache stores model candidates, not accepted documents. Every path,
including cache hits and width-probe recovery, crosses this module before use.
"""
from __future__ import annotations

from dataclasses import dataclass
import sys
import time

from .local_tex import normalize_tex
from .syntax_check import validate_latex
from .syntax_repair import (canonicalize_document_terminator, page_numbers_for_lines,
                            page_diagnostic_hints, repair_invariant_violations)


@dataclass(frozen=True)
class RepairResult:
    source: str
    stats: dict
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class RepairTransaction:
    """Return accepted text or the original source, with a definitive outcome."""

    def __init__(self, generator, *, max_attempts=0, event=None):
        self.generator = generator
        self.max_attempts = max_attempts
        self.event = event or (lambda *args, **kwargs: None)

    def repair(self, src: str, *, target_pages=None) -> RepairResult:
        repair_input = src
        repair_started = time.monotonic()
        repairer = self.generator
        src, repair_stats = repairer.repair_document(src, target_pages=target_pages)
        src, _ = normalize_tex(src)
        syntax_repair_stats = vars(repair_stats)
        repaired_issues = validate_latex(src, require_sync_safe=False)
        repaired_errors = [i for i in repaired_issues if i.severity == "error"]
        # A model repair must not turn a structurally valid input into an invalid
        # document.  This is especially important for complex tables: underfull
        # rows are deferred warnings, while a model can accidentally introduce
        # hard overfull alignment errors while trying to repair them.  Keep the
        # locally normalized source in that case and let the optimizer handle
        # the original warning deterministically.
        if repaired_errors:
            original_issues = validate_latex(repair_input, require_sync_safe=False)
            original_errors = [issue for issue in original_issues if issue.severity == "error"]
            original_underfull_only = bool(original_errors) and all(
                issue.code == "TABLE_ALIGNMENT_UNDERFULL" for issue in original_errors
            )
            candidate_introduced_hard_alignment = any(
                issue.code == "TABLE_ALIGNMENT_MISMATCH" for issue in repaired_errors
            )
            if not original_errors or len(repaired_errors) >= len(original_errors) or (
                original_underfull_only and candidate_introduced_hard_alignment
            ):
                src = repair_input
                src, _ = normalize_tex(src)
                repaired_issues = validate_latex(src, require_sync_safe=False)
                repaired_errors = [
                    issue for issue in repaired_issues if issue.severity == "error"
                ]
                self.event(
                    "SYNTAX_REPAIR_REJECTED",
                    "discarded model candidate that introduced structural errors",
                    level="WARNING", introduced_errors=len(repaired_errors),
                    policy="retain_locally_normalized_input",
                )
        # Underfull rows are a recoverable layout warning.  Keep them visible in
        # the audit log, but do not block the optimizer from applying its table
        # conversion and compile checks.
        if repaired_errors and all(
            issue.code == "TABLE_ALIGNMENT_UNDERFULL" for issue in repaired_errors
        ):
            self.event(
                "SYNTAX_REPAIR_DEFERRED",
                "underfull table rows deferred to optimizer",
                level="WARNING", errors=len(repaired_errors),
            )
            repaired_errors = []
        targeted_retry_stats = None
        targeted_attempt = 0
        while (repaired_errors and not repair_stats.failed
               and targeted_attempt < self.max_attempts):
            targeted_attempt += 1
            retry_pages = page_numbers_for_lines(
                src, [issue.line for issue in repaired_errors]
            )
            diagnostic_hints = page_diagnostic_hints(src, repaired_errors)
            self.event(
                "SYNTAX_REPAIR_TARGETED_RETRY",
                "retrying only pages that still contain structural errors",
                level="WARNING", attempt=targeted_attempt,
                max_attempts=self.max_attempts,
                pages=sorted(retry_pages),
                errors=[i.payload() for i in repaired_errors],
            )
            previous_src = src
            previous_error_signature = [
                (issue.code, issue.message) for issue in repaired_errors
            ]
            candidate, targeted_stats = repairer.repair_document(
                src, target_pages=retry_pages,
                diagnostic_hints=diagnostic_hints,
                bypass_cache=True,
            )
            targeted_retry_stats = vars(targeted_stats)
            src = candidate
            src, _ = normalize_tex(src)
            repaired_issues = validate_latex(src, require_sync_safe=False)
            repaired_errors = [
                i for i in repaired_issues if i.severity == "error"
            ]
            repair_stats.failed += targeted_stats.failed
            current_error_signature = [
                (issue.code, issue.message) for issue in repaired_errors
            ]
            if src == previous_src or current_error_signature == previous_error_signature:
                # Never retain a changed candidate that did not reduce the
                # structural error set.  Subsequent retries must start from
                # the last known-safe document, not from a corrupted page.
                src = previous_src
                repaired_issues = validate_latex(src, require_sync_safe=False)
                repaired_errors = [
                    issue for issue in repaired_issues if issue.severity == "error"
                ]
                self.event(
                    "SYNTAX_REPAIR_TARGETED_STALLED",
                    "targeted syntax repair made no structural progress",
                    level="WARNING", attempt=targeted_attempt,
                    source_changed=src != previous_src,
                    remaining_structural_errors=[
                        issue.payload() for issue in repaired_errors
                    ],
                )
                break
        self.event("SYNTAX_REPAIR_FINISH", "LLM syntax repair finished",
               level="WARNING" if repaired_errors or repair_stats.failed else "INFO",
               seconds=round(time.monotonic() - repair_started, 3),
               stats=syntax_repair_stats,
               targeted_retry_stats=targeted_retry_stats,
               remaining_structural_errors=[i.payload() for i in repaired_errors])
        if repaired_errors:
            # The model is not allowed to hand an invalid document to the
            # optimizer.  Restore the pre-repair source so no damaged
            # candidate can be persisted or reused on a later restart.
            src = repair_input
            src, _ = normalize_tex(src)
            repaired_issues = validate_latex(src, require_sync_safe=False)
            repaired_errors = [
                issue for issue in repaired_issues if issue.severity == "error"
            ]
            if repaired_errors:
                self.event(
                    "SYNTAX_REPAIR_BLOCKED",
                    "structural errors remain after targeted syntax repair",
                    level="ERROR", errors=[i.payload() for i in repaired_errors],
                    policy="stop_before_optimizer_and_retry_later",
                )
                print(
                    "ERROR: structural errors remain after targeted syntax repair; "
                    "stopping before table conversion.",
                    file=sys.stderr,
                )
                return RepairResult(src, syntax_repair_stats, 6)
        if repair_stats.failed:
            integrity_violations = repair_invariant_violations(repair_input, src)
            if not integrity_violations:
                self.event(
                    "SYNTAX_REPAIR_DEGRADED_SAFE",
                    "continuing after failed syntax-repair batches because the "
                    "current source passed structural and integrity validation",
                    level="WARNING", failed_batches=repair_stats.failed,
                    remaining_structural_errors=0,
                    integrity_violations=[],
                    policy="continue_with_validated_current_source",
                )
            else:
                self.event(
                    "SYNTAX_REPAIR_BLOCKED",
                    "failed syntax-repair batches left output integrity unproven",
                    level="ERROR", failed_batches=repair_stats.failed,
                    integrity_violations=integrity_violations,
                    policy="stop_before_probe_and_retry_later",
                )
                hint = "SYNTAX_REPAIR_INTEGRITY_FAILED"
                print(
                    "ERROR: LLM syntax repair did not complete and the current "
                    "source failed integrity validation; see the optimizer log "
                    f"for {hint} details.",
                    file=sys.stderr,
                )
                return RepairResult(repair_input, syntax_repair_stats, 5)
        return RepairResult(src, syntax_repair_stats)

    def repair_probe(self, source: str, *, target_pages, diagnostic_hint,
                     attempt=1) -> RepairResult:
        """Accept only changed, structurally valid probe recovery candidates.

        A probe failure is external evidence of a defect, so a failed request or
        unchanged candidate cannot be treated as successful recovery. The caller
        owns measurement, not acceptance of the model's text.
        """
        candidate, stats = self.generator.repair_document(
            source, target_pages=target_pages, diagnostic_hint=diagnostic_hint,
            bypass_cache=True,
        )
        candidate, _ = canonicalize_document_terminator(candidate)
        issues = validate_latex(candidate, require_sync_safe=False)
        errors = [issue for issue in issues if issue.severity == "error"]
        self.event(
            "MODEL_REPAIR_RECHECK", "model repair was structurally rechecked",
            level="ERROR" if errors or stats.failed else "INFO",
            changed=candidate != source, stats=vars(stats),
            errors=[issue.payload() for issue in errors], attempt=attempt,
        )
        if stats.failed or errors:
            return RepairResult(source, vars(stats), 3)
        if candidate == source:
            self.event(
                "MODEL_REPAIR_STALLED",
                "targeted model repair made no source change; stopping without duplicate calls",
                level="ERROR", attempt=attempt, pages=sorted(target_pages),
            )
            return RepairResult(source, vars(stats), 3)
        return RepairResult(candidate, vars(stats))
