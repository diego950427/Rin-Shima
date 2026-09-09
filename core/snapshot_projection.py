"""Pure compatibility projection from the canonical decision snapshot.

Legacy renderers expect a dictionary with summary and allocation buckets.
This module deliberately imports no evaluator, registry, or allocator: all
values are read from an already-produced :class:`DecisionSnapshot`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from allocation_engine import (
    EXCLUSIVE,
    FAIL,
    NOT_APPLICABLE,
    PASS,
    SHARED_SHADOW,
    UNKNOWN,
)
from decision_snapshot import DecisionSnapshot, statistics_digest

STATISTICS_SCHEMA_VERSION = "decision-statistics.v2"
CHART_DATASET_SCHEMA_VERSION = "lieflat-datasets.v2"
PROJECTION_SCHEMA_VERSION = "snapshot-presentation.v2"
AGGREGATE_GATE_UNAVAILABLE = "AGGREGATE_GATE_UNAVAILABLE"
UNCLASSIFIED_CATEGORY = "UNCLASSIFIED"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _text(value: Any, fallback: str = "") -> str:
    return str(value).strip() if isinstance(value, (str, int, float, bool)) else fallback


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _decimal_text(value: Any) -> str:
    number = _decimal(value)
    if number == number.to_integral():
        return str(number.quantize(Decimal("1")))
    return format(number.normalize(), "f").rstrip("0").rstrip(".") or "0"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Decimal):
        return _decimal_text(value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _statistics_digest(statistics: Mapping[str, Any]) -> str:
    return statistics_digest(statistics)


def _safe_status(value: Any) -> str:
    normalized = _text(value).upper().replace("-", "_").replace(" ", "_")
    return normalized if normalized else UNKNOWN


def _sum_mapping(values: Mapping[str, Any]) -> Decimal:
    return sum((_decimal(value) for value in values.values()), Decimal("0"))


def _finite_decimal(value: Any) -> Decimal | None:
    """Return a finite decimal, preserving the distinction from missing data."""

    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _flag(value: Any) -> bool | None:
    """Parse allocator boolean fields without treating ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    normalized = _text(value).strip().upper()
    if normalized in {"TRUE", "1", "YES", "Y", "PASS", "OK"}:
        return True
    if normalized in {"FALSE", "0", "NO", "N", "FAIL", "UNKNOWN"}:
        return False
    return None


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _has_field(value: Any, key: str) -> bool:
    return key in value if isinstance(value, Mapping) else hasattr(value, key)


def _strict_nonnegative(value: Any) -> Decimal | None:
    number = _finite_decimal(value)
    return number if number is not None and number >= 0 else None


def _reconcile_requirement_results(
    requirement_rows: Sequence[Any],
    raw_results: Any,
    requirement_by_id: Mapping[str, Any],
    exclusive_by_requirement: Mapping[str, Decimal],
    shadow_by_target: Mapping[str, Decimal],
    valid_waiver_targets: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Validate RequirementResult rows against the canonical allocation maps.

    RequirementResult is an observation produced by the allocator; it is not
    allowed to manufacture a second credit ledger.  This validator keeps
    legacy optional fields permissive for already-UNKNOWN rows, while a
    purported PASS/FAIL must carry enough numeric evidence to reconcile every
    counted field.
    """

    result_rows = raw_results if isinstance(raw_results, (list, tuple)) else ()
    approved_waiver_targets = set(valid_waiver_targets or ())
    result_by_id: dict[str, Any] = {}
    result_id_counts: defaultdict[str, int] = defaultdict(int)
    errors: list[str] = []
    if not isinstance(raw_results, (list, tuple)):
        errors.append("REQUIREMENT_RESULTS_MALFORMED")
    for result in result_rows:
        requirement_id = _text(_field(result, "requirement_id", ""))
        if not requirement_id:
            errors.append("REQUIREMENT_RESULT_ID_MISSING")
            continue
        result_id_counts[requirement_id] += 1
        if requirement_id not in requirement_by_id:
            errors.append("REQUIREMENT_RESULT_UNKNOWN_ID")
        result_by_id[requirement_id] = result
    errors.extend(
        "REQUIREMENT_RESULT_ID_DUPLICATE"
        for requirement_id, count in sorted(result_id_counts.items())
        if count > 1
    )

    checks: list[dict[str, Any]] = []
    for requirement_id, requirement in sorted(requirement_by_id.items(), key=lambda item: item[0]):
        result = result_by_id.get(requirement_id)
        row_errors: list[str] = []
        if result is None:
            if _decimal(exclusive_by_requirement.get(requirement_id)) > 0 or _decimal(shadow_by_target.get(requirement_id)) > 0:
                row_errors.append("REQUIREMENT_RESULT_MISSING_FOR_ALLOCATION")
            checks.append(
                {
                    "requirement_id": requirement_id,
                    "status": UNKNOWN,
                    "ok": not row_errors,
                    "reason_codes": tuple(row_errors),
                }
            )
            errors.extend(row_errors)
            continue

        status = _safe_status(_field(result, "status", UNKNOWN))
        if status not in {PASS, FAIL, UNKNOWN, NOT_APPLICABLE}:
            row_errors.append("REQUIREMENT_RESULT_STATUS_INVALID")
        numeric: dict[str, Decimal | None] = {}
        for key in ("required_credits", "exclusive_credits", "shared_shadow_credits", "effective_credits", "deficit"):
            if _has_field(result, key):
                numeric[key] = _strict_nonnegative(_field(result, key))
                if numeric[key] is None:
                    row_errors.append(f"REQUIREMENT_RESULT_{key.upper()}_INVALID")
            else:
                numeric[key] = None

        required_spec_raw = _field(requirement, "credits_required", None)
        if required_spec_raw is None:
            required_spec_raw = _field(requirement, "required_credits", None)
        required_fallback = _strict_nonnegative(required_spec_raw)
        required = numeric["required_credits"] if numeric["required_credits"] is not None else required_fallback
        exclusive = numeric["exclusive_credits"]
        shared = numeric["shared_shadow_credits"]
        effective = numeric["effective_credits"]
        deficit = numeric["deficit"]
        if status in {PASS, FAIL} and required is None:
            row_errors.append("REQUIREMENT_RESULT_REQUIRED_MISSING")
        if (
            numeric["required_credits"] is not None
            and required_fallback is not None
            and numeric["required_credits"] != required_fallback
        ):
            row_errors.append("REQUIREMENT_RESULT_REQUIRED_MISMATCH")
        if _has_field(result, "waived"):
            waived_value = _flag(_field(result, "waived"))
            if waived_value is None:
                row_errors.append("REQUIREMENT_RESULT_WAIVER_INVALID")
                waived = False
            else:
                waived = waived_value
        else:
            waived = False
        if status in {PASS, FAIL} and (required is None or exclusive is None or effective is None or deficit is None):
            row_errors.append("REQUIREMENT_RESULT_RECONCILIATION_FIELDS_MISSING")
        if exclusive is not None:
            ledger_exclusive = _decimal(exclusive_by_requirement.get(requirement_id))
            if exclusive != ledger_exclusive:
                row_errors.append("REQUIREMENT_RESULT_EXCLUSIVE_MISMATCH")
        if shared is not None:
            shadow_total = _decimal(shadow_by_target.get(requirement_id))
            if shared != shadow_total:
                row_errors.append("REQUIREMENT_RESULT_SHARED_MISMATCH")
        else:
            shared = Decimal("0")
            if _decimal(shadow_by_target.get(requirement_id)) > 0:
                row_errors.append("REQUIREMENT_RESULT_SHARED_MISSING")
        if exclusive is not None and effective is not None and effective != exclusive + shared:
            row_errors.append("REQUIREMENT_RESULT_EFFECTIVE_MISMATCH")
        if required is not None and effective is not None and deficit is not None:
            expected_deficit = Decimal("0") if waived else max(Decimal("0"), required - effective)
            if deficit != expected_deficit:
                row_errors.append("REQUIREMENT_RESULT_DEFICIT_MISMATCH")

        required_flag = _flag(_field(requirement, "required", True))
        required_flag = True if required_flag is None else required_flag
        requirement_allows_waiver = _flag(_field(requirement, "waiver", False)) is True
        if waived:
            if not requirement_allows_waiver:
                row_errors.append("REQUIREMENT_RESULT_WAIVER_NOT_ALLOWED")
            if requirement_id not in approved_waiver_targets:
                row_errors.append("REQUIREMENT_RESULT_WAIVER_UNVERIFIED")
        coverage_state = _text(_field(result, "coverage_state", _field(requirement, "coverage_state", UNKNOWN))).upper()
        evidence_state = _text(_field(result, "evidence_state", _field(requirement, "evidence_state", UNKNOWN))).upper()
        if required_flag is False and status != NOT_APPLICABLE:
            row_errors.append("REQUIREMENT_RESULT_STATUS_MISMATCH")
        elif required_flag and status == NOT_APPLICABLE:
            row_errors.append("REQUIREMENT_RESULT_STATUS_MISMATCH")
        elif status == PASS and (
            deficit is None
            or deficit != 0
            or coverage_state not in {"COMPLETE", "RESOLVED"}
            or evidence_state != "VERIFIED"
        ):
            row_errors.append("REQUIREMENT_RESULT_STATUS_MISMATCH")
        elif status == FAIL and (
            deficit is None
            or deficit <= 0
            or waived
            or coverage_state not in {"COMPLETE", "RESOLVED"}
            or evidence_state != "VERIFIED"
        ):
            row_errors.append("REQUIREMENT_RESULT_STATUS_MISMATCH")

        row_valid = not row_errors
        checks.append(
            {
                "requirement_id": requirement_id,
                "status": status if row_valid else UNKNOWN,
                "ok": row_valid,
                "reason_codes": tuple(dict.fromkeys(row_errors)),
            }
        )
        errors.extend(row_errors)

    return {
        "ok": not errors,
        "status": PASS if not errors else UNKNOWN,
        "reason_codes": tuple(dict.fromkeys(errors)),
        "checks": tuple(checks),
        "result_id_count": len(result_rows),
        "known_result_count": len(result_by_id),
    }


def _status_counter(statuses: Mapping[str, int] | None = None) -> dict[str, int]:
    return {key: int((statuses or {}).get(key, 0)) for key in (PASS, FAIL, UNKNOWN)}


def _attempt_statuses(attempts: Any) -> dict[str, Any]:
    rows = tuple(attempts) if isinstance(attempts, (list, tuple)) else ()
    counts: dict[str, int] = defaultdict(int)
    nominal: dict[str, Decimal] = defaultdict(Decimal)
    for attempt in rows:
        if isinstance(attempt, Mapping):
            status = _safe_status(attempt.get("status"))
            credits = _decimal(attempt.get("credits"))
        else:
            status = _safe_status(getattr(attempt, "status", UNKNOWN))
            credits = _decimal(getattr(attempt, "credits", 0))
        counts[status] += 1
        # Nominal credits intentionally use the transcript row's announced
        # credits, never the earned/countable ledger.  This keeps IN_PROGRESS
        # observable without allowing it into graduation credit totals.
        nominal[status] += max(Decimal("0"), credits)
    return {
        "counts": {key: counts[key] for key in sorted(counts)},
        "nominal_credits": {key: _decimal_text(nominal[key]) for key in sorted(nominal)},
        "completed_count": int(counts.get(PASS, 0)),
        "in_progress_count": int(counts.get("IN_PROGRESS", 0)),
        "unresolved_count": int(counts.get(UNKNOWN, 0)),
        "in_progress_nominal_credits": _decimal_text(nominal.get("IN_PROGRESS", Decimal("0"))),
    }


def _allocation_parts(attempts: Any, requirements: Any, allocation: Any) -> dict[str, Any]:
    """Extract one additive ledger from allocator-owned exclusive portions.

    The function does not call the allocator.  It only reads the immutable
    ``AttemptAllocation``/``CreditPortion`` records already attached to a
    snapshot, and filters non-PASS attempts so a malformed test double cannot
    promote an in-progress course into counted graduation credit.
    """

    attempt_rows = tuple(attempts) if isinstance(attempts, (list, tuple)) else ()
    requirement_rows = tuple(requirements) if isinstance(requirements, (list, tuple)) else ()
    allocations = _field(allocation, "allocations", ()) if allocation is not None else ()
    shadows = _field(allocation, "shadow_allocations", ()) if allocation is not None else ()
    attempt_by_id: dict[str, Any] = {}
    attempt_id_counts: defaultdict[str, int] = defaultdict(int)
    for attempt in attempt_rows:
        attempt_id = _text(attempt.get("attempt_id")) if isinstance(attempt, Mapping) else _text(getattr(attempt, "attempt_id", ""))
        if attempt_id:
            attempt_id_counts[attempt_id] += 1
            attempt_by_id[attempt_id] = attempt
    requirement_by_id: dict[str, Any] = {}
    for requirement in requirement_rows:
        requirement_id = _text(requirement.get("requirement_id")) if isinstance(requirement, Mapping) else _text(getattr(requirement, "requirement_id", ""))
        if requirement_id:
            requirement_by_id[requirement_id] = requirement
    requirement_result_by_id: dict[str, Any] = {}
    raw_requirement_results = _field(allocation, "requirement_results", ()) if allocation is not None else ()
    for result in raw_requirement_results if isinstance(raw_requirement_results, (list, tuple)) else ():
        requirement_id = _text(_field(result, "requirement_id", ""))
        if requirement_id:
            requirement_result_by_id[requirement_id] = result
    valid_waiver_targets: set[str] = set()
    raw_waiver_assessments = _field(allocation, "waiver_assessments", ()) if allocation is not None else ()
    for assessment in raw_waiver_assessments if isinstance(raw_waiver_assessments, (list, tuple)) else ():
        target_id = _text(_field(assessment, "target_requirement_id", ""))
        if target_id and _safe_status(_field(assessment, "status", UNKNOWN)) == PASS:
            valid_waiver_targets.add(target_id)

    # The allocator's source total is the conservation baseline.  Re-summing
    # transcript rows here would allow a missing allocation row to silently
    # lower the denominator and make a partial ledger look conserved.
    source_raw = _field(allocation, "source_earned_credits", None)
    source_value = _strict_nonnegative(source_raw)
    source = source_value or Decimal("0")
    source_baseline_known = source_value is not None
    allocation_conservation_raw = _field(allocation, "credit_conservation", None)
    allocation_conservation = _flag(allocation_conservation_raw) is True
    recomputed_source = Decimal("0")
    exclusive = Decimal("0")
    unallocated = Decimal("0")
    by_bucket: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_owner: defaultdict[str, Decimal] = defaultdict(Decimal)
    by_requirement: defaultdict[str, Decimal] = defaultdict(Decimal)
    valid_exclusive_by_attempt: defaultdict[str, Decimal] = defaultdict(Decimal)
    unclassified = Decimal("0")
    ledger_rows: list[dict[str, Any]] = []
    allocation_row_checks: list[dict[str, Any]] = []
    allocation_rows_valid = _has_field(allocation, "allocations") if allocation is not None else False
    allocation_row_errors: list[str] = []
    if not allocation_rows_valid:
        allocation_row_errors.append("ALLOCATION_ROWS_MISSING")
    seen_attempt_ids: set[str] = set()
    source_rows = allocations if isinstance(allocations, (list, tuple)) else ()
    if not isinstance(allocations, (list, tuple)):
        allocation_rows_valid = False
        allocation_row_errors.append("ALLOCATION_ROWS_MALFORMED")
    for item in source_rows:
        row_errors: list[str] = []
        attempt_id = _text(_field(item, "attempt_id", ""))
        if not attempt_id:
            row_errors.append("ALLOCATION_ROW_ATTEMPT_ID_MISSING")
        elif attempt_id not in attempt_by_id:
            row_errors.append("ALLOCATION_ROW_UNKNOWN_ATTEMPT")
        elif attempt_id_counts[attempt_id] != 1:
            row_errors.append("ALLOCATION_ROW_ATTEMPT_ID_AMBIGUOUS")
        elif attempt_id in seen_attempt_ids:
            row_errors.append("ALLOCATION_ROW_DUPLICATE_ATTEMPT_ID")
        else:
            seen_attempt_ids.add(attempt_id)
        attempt = attempt_by_id.get(attempt_id)
        attempt_status = (
            _safe_status(attempt.get("status"))
            if isinstance(attempt, Mapping)
            else _safe_status(getattr(attempt, "status", UNKNOWN))
            if attempt is not None
            else UNKNOWN
        )
        source_raw = _field(item, "source_credits", None)
        residual_raw = _field(item, "unallocated_credits", None)
        source_credits = _strict_nonnegative(source_raw) if _has_field(item, "source_credits") else None
        residual = _strict_nonnegative(residual_raw) if _has_field(item, "unallocated_credits") else None
        if source_credits is None:
            row_errors.append("ALLOCATION_ROW_SOURCE_INVALID")
        if residual is None:
            row_errors.append("ALLOCATION_ROW_RESIDUAL_INVALID")
        portions = _field(item, "portions", ())
        if not isinstance(portions, (list, tuple)):
            row_errors.append("ALLOCATION_ROW_PORTIONS_MALFORMED")
            portions = ()
        row_exclusive = Decimal("0")
        row_exclusive_portions: list[tuple[str, Decimal]] = []
        for portion in portions:
            if not isinstance(portion, Mapping) and not hasattr(portion, "credits"):
                row_errors.append("ALLOCATION_PORTION_MALFORMED")
                continue
            amount_raw = _field(portion, "credits", None)
            amount = _strict_nonnegative(amount_raw) if _has_field(portion, "credits") else None
            if amount is None:
                row_errors.append("ALLOCATION_PORTION_CREDITS_INVALID")
                continue
            kind = _safe_status(_field(portion, "allocation_kind", ""))
            if kind not in {EXCLUSIVE, SHARED_SHADOW, "SHARED_REUSE", "WAIVER", NOT_APPLICABLE}:
                row_errors.append("ALLOCATION_PORTION_KIND_INVALID")
            portion_attempt_present = _has_field(portion, "attempt_id")
            portion_attempt_id = _text(_field(portion, "attempt_id", ""))
            if kind == EXCLUSIVE:
                if not portion_attempt_present or not portion_attempt_id:
                    row_errors.append("ALLOCATION_PORTION_ATTEMPT_ID_MISSING")
                elif portion_attempt_id != attempt_id:
                    row_errors.append("ALLOCATION_PORTION_ATTEMPT_ID_MISMATCH")
                if not _text(_field(portion, "requirement_id", "")):
                    row_errors.append("ALLOCATION_PORTION_REQUIREMENT_ID_MISSING")
                elif _text(_field(portion, "requirement_id", "")) not in requirement_by_id:
                    row_errors.append("ALLOCATION_PORTION_UNKNOWN_REQUIREMENT")
            elif portion_attempt_present and portion_attempt_id and portion_attempt_id != attempt_id:
                row_errors.append("ALLOCATION_PORTION_ATTEMPT_ID_MISMATCH")
            if kind == EXCLUSIVE and amount > 0:
                requirement_id = _text(_field(portion, "requirement_id", ""))
                row_exclusive += amount
                row_exclusive_portions.append((requirement_id, amount))
        if source_credits is not None and residual is not None and row_exclusive + residual != source_credits:
            row_errors.append("ALLOCATION_ROW_PARTS_MISMATCH")
        row_valid = not row_errors
        if not row_valid:
            allocation_rows_valid = False
            allocation_row_errors.extend(row_errors)
        row_difference = (
            _decimal_text(row_exclusive + (residual or Decimal("0")) - source_credits)
            if source_credits is not None
            else ""
        )
        allocation_row_checks.append(
            {
                "attempt_id": attempt_id,
                "source_credits": _decimal_text(source_credits) if source_credits is not None else "",
                "exclusive_credits": _decimal_text(row_exclusive),
                "unallocated_credits": _decimal_text(residual) if residual is not None else "",
                "difference": row_difference,
                "status": PASS if row_valid else UNKNOWN,
                "ok": row_valid,
                "reason_codes": tuple(dict.fromkeys(row_errors)),
            }
        )
        # Only valid PASS rows can enter the additive ledger.  A malformed row
        # is retained in diagnostics but cannot contribute partial credits.
        if not row_valid or attempt_status != PASS:
            continue
        recomputed_source += source_credits or Decimal("0")
        exclusive += row_exclusive
        unallocated += residual or Decimal("0")
        valid_exclusive_by_attempt[attempt_id] += row_exclusive
        for requirement_id, amount in row_exclusive_portions:
            requirement = requirement_by_id.get(requirement_id)
            bucket = _text(requirement.get("bucket")) if isinstance(requirement, Mapping) else _text(getattr(requirement, "bucket", ""))
            owner = _text(requirement.get("owner")) if isinstance(requirement, Mapping) else _text(getattr(requirement, "owner", ""))
            bucket = bucket or UNCLASSIFIED_CATEGORY
            owner = owner or UNCLASSIFIED_CATEGORY
            by_bucket[bucket] += amount
            by_owner[owner] += amount
            by_requirement[requirement_id] += amount
            if bucket.upper() in {"UNKNOWN", "UNCLASSIFIED", "UNCLASSIFIED_CATEGORY"}:
                unclassified += amount
            ledger_rows.append(
                {
                    "attempt_id": attempt_id,
                    "requirement_id": requirement_id,
                    "bucket": bucket,
                    "owner": owner,
                    "credits": _decimal_text(amount),
                    "allocation_kind": EXCLUSIVE,
                    "counted": True,
                }
            )
        if residual and residual > 0:
            ledger_rows.append(
                {
                    "attempt_id": attempt_id,
                    "requirement_id": "",
                    "bucket": "unallocated",
                    "owner": "UNALLOCATED",
                    "credits": _decimal_text(residual),
                    "allocation_kind": "UNALLOCATED",
                    "counted": True,
                }
            )

    shadow = Decimal("0")
    shadow_by_attempt: defaultdict[str, Decimal] = defaultdict(Decimal)
    shadow_by_target: defaultdict[str, Decimal] = defaultdict(Decimal)
    shadow_rows_valid = True
    shadow_row_errors: list[str] = []
    shadow_row_checks: list[dict[str, Any]] = []
    shadow_reconciliation_checks: list[dict[str, Any]] = []
    if _has_field(allocation, "shadow_allocations") and not isinstance(shadows, (list, tuple)):
        shadow_rows_valid = False
        shadow_row_errors.append("SHARED_SHADOW_ROWS_MALFORMED")
        shadow_source_rows: Sequence[Any] = ()
    else:
        shadow_source_rows = shadows if isinstance(shadows, (list, tuple)) else ()
    for portion in shadow_source_rows:
        row_errors: list[str] = []
        if not isinstance(portion, Mapping) and not hasattr(portion, "credits"):
            row_errors.append("SHARED_SHADOW_MALFORMED")
            attempt_id = ""
            target_id = ""
            amount = None
        else:
            attempt_id = _text(_field(portion, "attempt_id", ""))
            target_id = _text(_field(portion, "requirement_id", ""))
            amount = _strict_nonnegative(_field(portion, "credits", None)) if _has_field(portion, "credits") else None
            kind = _safe_status(_field(portion, "allocation_kind", ""))
            if kind != SHARED_SHADOW:
                row_errors.append("SHARED_SHADOW_KIND_INVALID")
            if not attempt_id:
                row_errors.append("SHARED_SHADOW_ATTEMPT_ID_MISSING")
            elif attempt_id not in attempt_by_id:
                row_errors.append("SHARED_SHADOW_UNKNOWN_ATTEMPT")
            elif attempt_id_counts[attempt_id] != 1:
                row_errors.append("SHARED_SHADOW_ATTEMPT_ID_AMBIGUOUS")
            if amount is None:
                row_errors.append("SHARED_SHADOW_CREDITS_INVALID")
            if amount is not None and amount > 0 and not target_id:
                row_errors.append("SHARED_SHADOW_REQUIREMENT_ID_MISSING")
            elif target_id and target_id not in requirement_by_id:
                row_errors.append("SHARED_SHADOW_UNKNOWN_TARGET_REQUIREMENT")
            attempt = attempt_by_id.get(attempt_id)
            attempt_status = (
                _safe_status(attempt.get("status"))
                if isinstance(attempt, Mapping)
                else _safe_status(getattr(attempt, "status", UNKNOWN))
                if attempt is not None
                else UNKNOWN
            )
            if amount is not None and amount > 0 and attempt_status != PASS:
                row_errors.append("SHARED_SHADOW_NON_PASS_ATTEMPT")
        row_valid = not row_errors
        if not row_valid:
            shadow_rows_valid = False
            shadow_row_errors.extend(row_errors)
        shadow_row_checks.append(
            {
                "attempt_id": attempt_id,
                "requirement_id": target_id,
                "credits": _decimal_text(amount) if amount is not None else "",
                "status": PASS if row_valid else UNKNOWN,
                "ok": row_valid,
                "reason_codes": tuple(dict.fromkeys(row_errors)),
            }
        )
        if not row_valid or amount is None or amount <= 0:
            continue
        shadow += amount
        shadow_by_attempt[attempt_id] += amount
        shadow_by_target[target_id] += amount
    for attempt_id, amount in sorted(shadow_by_attempt.items()):
        exclusive_limit = valid_exclusive_by_attempt.get(attempt_id, Decimal("0"))
        row_errors: list[str] = []
        if exclusive_limit <= 0:
            row_errors.append("SHARED_SHADOW_NO_EXCLUSIVE_SOURCE")
        if amount > exclusive_limit:
            row_errors.append("SHARED_SHADOW_EXCEEDS_EXCLUSIVE_SOURCE")
        row_valid = not row_errors
        if not row_valid:
            shadow_rows_valid = False
            shadow_row_errors.extend(row_errors)
        shadow_reconciliation_checks.append(
            {
                "attempt_id": attempt_id,
                "shadow_credits": _decimal_text(amount),
                "exclusive_source_credits": _decimal_text(exclusive_limit),
                "status": PASS if row_valid else UNKNOWN,
                "ok": row_valid,
                "reason_codes": tuple(dict.fromkeys(row_errors)),
            }
        )
    # A shadow is non-additive, but it is still an assertion about a specific
    # requirement.  Reconcile every target against the allocator's
    # RequirementResult and enforce the same effective = exclusive + shadow
    # identity used by the allocator.  Missing/invalid result evidence is not
    # silently converted to zero.
    shadow_targets = set(shadow_by_target)
    for requirement_id, result in sorted(requirement_result_by_id.items(), key=lambda item: item[0]):
        shared_raw = _field(result, "shared_shadow_credits", None)
        shared_value = _strict_nonnegative(shared_raw) if _has_field(result, "shared_shadow_credits") else None
        effective_raw = _field(result, "effective_credits", None)
        effective_value = _strict_nonnegative(effective_raw) if _has_field(result, "effective_credits") else None
        exclusive_raw = _field(result, "exclusive_credits", None)
        exclusive_value = _strict_nonnegative(exclusive_raw) if _has_field(result, "exclusive_credits") else None
        shadow_value = shadow_by_target.get(requirement_id, Decimal("0"))
        row_errors: list[str] = []
        if requirement_id in shadow_targets and shared_value is None:
            row_errors.append("SHARED_SHADOW_RESULT_SHARED_INVALID")
        if shared_value is not None and shared_value != shadow_value:
            row_errors.append("SHARED_SHADOW_RESULT_SHARED_MISMATCH")
        if requirement_id in shadow_targets and exclusive_value is None:
            row_errors.append("SHARED_SHADOW_RESULT_EXCLUSIVE_INVALID")
        if requirement_id in shadow_targets and effective_value is None:
            row_errors.append("SHARED_SHADOW_RESULT_EFFECTIVE_INVALID")
        if (
            requirement_id in shadow_targets
            and exclusive_value is not None
            and shared_value is not None
            and effective_value is not None
            and effective_value != exclusive_value + shared_value
        ):
            row_errors.append("SHARED_SHADOW_RESULT_EFFECTIVE_MISMATCH")
        if row_errors:
            shadow_rows_valid = False
            shadow_row_errors.extend(row_errors)
        if requirement_id in shadow_targets or (shared_value is not None and shared_value > 0):
            shadow_reconciliation_checks.append(
                {
                    "requirement_id": requirement_id,
                    "shadow_credits": _decimal_text(shadow_value),
                    "result_shared_shadow_credits": _decimal_text(shared_value) if shared_value is not None else "",
                    "result_exclusive_credits": _decimal_text(exclusive_value) if exclusive_value is not None else "",
                    "result_effective_credits": _decimal_text(effective_value) if effective_value is not None else "",
                    "status": PASS if not row_errors else UNKNOWN,
                    "ok": not row_errors,
                    "reason_codes": tuple(dict.fromkeys(row_errors)),
                }
            )
    for requirement_id, shadow_value in sorted(shadow_by_target.items()):
        if requirement_id in requirement_result_by_id:
            continue
        shadow_rows_valid = False
        shadow_row_errors.append("SHARED_SHADOW_RESULT_MISSING")
        shadow_reconciliation_checks.append(
            {
                "requirement_id": requirement_id,
                "shadow_credits": _decimal_text(shadow_value),
                "result_shared_shadow_credits": "",
                "result_exclusive_credits": "",
                "result_effective_credits": "",
                "status": UNKNOWN,
                "ok": False,
                "reason_codes": ("SHARED_SHADOW_RESULT_MISSING",),
            }
        )
    requirement_reconciliation = _reconcile_requirement_results(
        requirement_rows,
        raw_requirement_results,
        requirement_by_id,
        by_requirement,
        shadow_by_target,
        valid_waiver_targets,
    )
    source_rows_match = source_baseline_known and recomputed_source == source
    parts_match = exclusive + unallocated == source
    conservation_reasons: list[str] = []
    if not source_baseline_known:
        conservation_reasons.append("SOURCE_BASELINE_MISSING")
    if not source_rows_match:
        conservation_reasons.append("ALLOCATION_ROWS_SOURCE_MISMATCH")
    if not allocation_rows_valid:
        conservation_reasons.extend(allocation_row_errors)
    if not shadow_rows_valid:
        conservation_reasons.extend(shadow_row_errors)
    if not requirement_reconciliation["ok"]:
        conservation_reasons.extend(requirement_reconciliation["reason_codes"])
    if not allocation_conservation:
        conservation_reasons.append("ALLOCATION_CREDIT_CONSERVATION_UNVERIFIED")
    if not parts_match:
        conservation_reasons.append("LEDGER_PARTS_MISMATCH")
    conservation = bool(
        source_baseline_known
        and source_rows_match
        and allocation_rows_valid
        and shadow_rows_valid
        and requirement_reconciliation["ok"]
        and allocation_conservation
        and parts_match
    )
    return {
        "additive": True,
        "ledger_basis": "EXCLUSIVE_PLUS_UNALLOCATED",
        "source_earned_credits": _decimal_text(source),
        "recomputed_source_credits": _decimal_text(recomputed_source),
        "source_baseline_known": source_baseline_known,
        "source_rows_match": source_rows_match,
        "allocation_rows_valid": allocation_rows_valid,
        "allocation_row_count": len(allocation_row_checks),
        "allocation_row_checks": tuple(allocation_row_checks),
        "shadow_rows_valid": shadow_rows_valid,
        "shadow_row_count": len(shadow_row_checks),
        "shadow_row_checks": tuple(shadow_row_checks),
        "shadow_reconciliation_checks": tuple(shadow_reconciliation_checks),
        "requirement_reconciliation": requirement_reconciliation,
        "allocation_credit_conservation": allocation_conservation,
        "exclusive_allocated_credits": _decimal_text(exclusive),
        "unallocated_credits": _decimal_text(unallocated),
        "shared_shadow_credits": _decimal_text(shadow),
        "exclusive_plus_unallocated": _decimal_text(exclusive + unallocated),
        "difference": _decimal_text((exclusive + unallocated) - source),
        "conservation": {
            "ok": conservation,
            "status": PASS if conservation else UNKNOWN,
            "source_earned_credits": _decimal_text(source),
            "recomputed_source_credits": _decimal_text(recomputed_source),
            "source_rows_match": source_rows_match,
            "allocation_rows_valid": allocation_rows_valid,
            "allocation_row_count": len(allocation_row_checks),
            "shadow_rows_valid": shadow_rows_valid,
            "shadow_row_count": len(shadow_row_checks),
            "shadow_reconciliation_checks": tuple(shadow_reconciliation_checks),
            "requirement_reconciliation": requirement_reconciliation,
            "allocation_credit_conservation": allocation_conservation,
            "exclusive_allocated_credits": _decimal_text(exclusive),
            "unallocated_credits": _decimal_text(unallocated),
            "difference": _decimal_text((exclusive + unallocated) - source),
            "reason_codes": tuple(dict.fromkeys(conservation_reasons)),
        },
        "exclusive_by_bucket": {key: _decimal_text(value) for key, value in sorted(by_bucket.items())},
        "exclusive_by_owner": {key: _decimal_text(value) for key, value in sorted(by_owner.items())},
        "exclusive_by_requirement": {key: _decimal_text(value) for key, value in sorted(by_requirement.items())},
        "shared_shadow": {
            "credits": _decimal_text(shadow),
            "by_target_requirement": {key: _decimal_text(value) for key, value in sorted(shadow_by_target.items())},
            "additive": False,
        },
        "rows": tuple(ledger_rows),
        "unclassified_exclusive_credits": _decimal_text(unclassified),
    }


def _requirement_metric_rows(requirements: Any, allocation: Any) -> tuple[dict[str, Any], ...]:
    requirement_rows = tuple(requirements) if isinstance(requirements, (list, tuple)) else ()
    results = getattr(allocation, "requirement_results", ()) if allocation is not None else ()
    if isinstance(allocation, Mapping):
        results = allocation.get("requirement_results", ())
    # Keep one sorted map for both the mapping payload and allocator dataclass
    # forms; this helper is used at the service boundary and by compatibility
    # projections.
    result_by_id = {}
    for item in results if isinstance(results, (list, tuple)) else ():
        requirement_id = _text(item.get("requirement_id")) if isinstance(item, Mapping) else _text(getattr(item, "requirement_id", ""))
        if requirement_id:
            result_by_id[requirement_id] = item
    metric_rows: list[dict[str, Any]] = []
    for requirement in requirement_rows:
        requirement_id = _text(requirement.get("requirement_id")) if isinstance(requirement, Mapping) else _text(getattr(requirement, "requirement_id", ""))
        if not requirement_id:
            continue
        result = result_by_id.get(requirement_id)
        get = (lambda key, default="": result.get(key, default)) if isinstance(result, Mapping) else (lambda key, default="": getattr(result, key, default) if result is not None else default)
        get_req = (lambda key, default="": requirement.get(key, default)) if isinstance(requirement, Mapping) else (lambda key, default="": getattr(requirement, key, default))
        exclusive_credits_present = (
            "exclusive_credits" in result
            if isinstance(result, Mapping)
            else result is not None and hasattr(result, "exclusive_credits")
        )
        status = _safe_status(get("status", UNKNOWN))
        coverage_state = _text(get("coverage_state", get_req("coverage_state", "UNKNOWN")), UNKNOWN).upper()
        evidence_state = _text(get("evidence_state", get_req("evidence_state", "UNKNOWN")), UNKNOWN).upper()
        status_reason_code = ""
        if status != NOT_APPLICABLE and (coverage_state not in {"COMPLETE", "RESOLVED"} or evidence_state != "VERIFIED"):
            # A numeric 100% result is not a verified graduation decision when
            # its official coverage/evidence is incomplete.
            status = UNKNOWN
            status_reason_code = "INCOMPLETE_EVIDENCE_OR_COVERAGE"
        owner = _text(get_req("owner"))
        metric_rows.append(
            {
                "requirement_id": requirement_id,
                "name": _text(get_req("name"), requirement_id),
                "bucket": _text(get_req("bucket"), UNCLASSIFIED_CATEGORY) or UNCLASSIFIED_CATEGORY,
                "owner": owner or UNCLASSIFIED_CATEGORY,
                "owner_present": bool(owner),
                "kind": _text(get_req("kind")),
                "required_credits": _decimal_text(get("required_credits", get_req("credits_required", 0))),
                "exclusive_credits": _decimal_text(get("exclusive_credits", 0)),
                "exclusive_credits_present": exclusive_credits_present,
                "shared_shadow_credits": _decimal_text(get("shared_shadow_credits", 0)),
                "effective_credits": _decimal_text(get("effective_credits", 0)),
                "deficit": _decimal_text(get("deficit", 0)),
                "status": status,
                "observed_status": status,
                "status_authoritative": True,
                "status_reason_code": status_reason_code,
                "coverage_state": coverage_state,
                "evidence_state": evidence_state,
                "waived": bool(get("waived", False)),
                "additive": False,
            }
        )
    return tuple(sorted(metric_rows, key=lambda item: item["requirement_id"]))


def _validated_requirement_metric_rows(
    metric_rows: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Demote requirement outcomes when their additive source is unsafe.

    The allocator's requirement results remain immutable source evidence.  A
    failed projection-level conservation check means their apparent PASS or
    FAIL status can no longer be used as an authoritative graduation result,
    so applicable rows become UNKNOWN while retaining the observed status and
    credit values for audit.
    """

    conservation = ledger.get("conservation") if isinstance(ledger.get("conservation"), Mapping) else {}
    if _flag(conservation.get("ok")) is True and _safe_status(conservation.get("status")) == PASS:
        return tuple(dict(row) for row in metric_rows)
    validated: list[dict[str, Any]] = []
    for row in metric_rows:
        item = dict(row)
        observed_status = _safe_status(item.get("observed_status", item.get("status", UNKNOWN)))
        item["observed_status"] = observed_status
        if observed_status != NOT_APPLICABLE:
            item["status"] = UNKNOWN
            item["status_authoritative"] = False
            # Retain a more specific pre-existing evidence/coverage reason
            # when one is already known.  Conservation failure still
            # demotes the status; this only keeps the explanation useful to
            # the person reading the requirement card.
            item["status_reason_code"] = _text(item.get("status_reason_code")) or "CREDIT_CONSERVATION_FAILED"
        else:
            item["status"] = NOT_APPLICABLE
            item["status_authoritative"] = True
        validated.append(item)
    return tuple(validated)


def _registry_total_gate(
    decision: Mapping[str, Any],
    primary_ids: tuple[str, ...],
    metric_rows: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    descriptor = decision.get("total_credit_requirement")
    if not isinstance(descriptor, Mapping):
        return None
    unavailable = {
        "available": False, "status": "UNAVAILABLE",
        "reason_code": AGGREGATE_GATE_UNAVAILABLE,
        "detail_code": "PRIMARY_REGISTRY_TOTAL_UNVERIFIED",
        "reason": "主修總學分門檻或採計範圍尚未核實，請先確認適用手冊及課程歸屬。",
    }
    required = _strict_nonnegative(descriptor.get("required_credits"))
    if (
        descriptor.get("authority") != "PRIMARY_CURRICULUM_REGISTRY"
        or descriptor.get("non_consuming") is not True
        or not _text(descriptor.get("curriculum_id"))
        or not _text(descriptor.get("source_reference"))
        or descriptor.get("evidence_state") != "VERIFIED"
        or descriptor.get("coverage_state") not in {"COMPLETE", "RESOLVED"}
        or required is None or required <= 0
        or not isinstance(ledger, Mapping)
    ):
        return unavailable
    conservation = ledger.get("conservation", {})
    if (
        not isinstance(conservation, Mapping)
        or _flag(conservation.get("ok")) is not True
        or conservation.get("status") != PASS
        or _strict_nonnegative(ledger.get("unclassified_exclusive_credits")) != 0
        or len(set(primary_ids)) != len(primary_ids)
    ):
        return unavailable
    by_requirement = ledger.get("exclusive_by_requirement")
    if not isinstance(by_requirement, Mapping):
        return unavailable
    completed = Decimal("0")
    for requirement_id in primary_ids:
        matches = [row for row in metric_rows if row.get("requirement_id") == requirement_id]
        if len(matches) != 1:
            return unavailable
        row = matches[0]
        amount = _strict_nonnegative(by_requirement.get(requirement_id, "0"))
        if (
            row.get("owner_present") is not True or row.get("owner") != "PRIMARY"
            or row.get("exclusive_credits_present") is not True
            or amount is None or _strict_nonnegative(row.get("exclusive_credits")) != amount
        ):
            return unavailable
        completed += amount
    return {
        "available": True, "status": "AVAILABLE", "reason_code": "", "detail_code": "",
        "gate_status": PASS if completed >= required else FAIL,
        "reason": "依適用學生手冊總門檻，僅加總已正式配置於主修的學分。",
        "curriculum_id": descriptor["curriculum_id"],
        "source_reference": descriptor["source_reference"],
        "required_credits": _decimal_text(required),
        "completed_credits": _decimal_text(completed),
        "missing_credits": _decimal_text(max(Decimal("0"), required - completed)),
        "numerator_source": "primary_requirement_scope.exclusive_credits",
        "shadow_excluded": True,
    }


def _aggregate_gate(
    metric_rows: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any] | None = None,
    decisions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    decision = decisions.get("primary_graduation") if isinstance(decisions, Mapping) else None
    raw_primary_ids = decision.get("requirement_ids") if isinstance(decision, Mapping) else ()
    primary_ids = tuple(
        _text(item)
        for item in (raw_primary_ids if isinstance(raw_primary_ids, (list, tuple, set, frozenset)) else ())
        if _text(item)
    )
    if not primary_ids:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "PRIMARY_REQUIREMENT_SCOPE_MISSING",
            "reason": "主修判定沒有明確的 requirement_ids，不能安全指定總畢業學分閘門。",
        }

    registry_total = _registry_total_gate(decision, primary_ids, metric_rows, ledger)
    if registry_total is not None:
        return registry_total
    explicit = [
        row
        for row in metric_rows
        if _text(row.get("kind")).upper() in {"AGGREGATE", "TOTAL", "TOTAL_CREDITS", "GRADUATION_TOTAL"}
        and _text(row.get("bucket")).casefold() in {"total", "graduation_total", "total_required", "graduation_total_credits"}
    ]
    scoped = [row for row in explicit if _text(row.get("requirement_id")) in primary_ids]
    if not scoped:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "PRIMARY_AGGREGATE_MISSING",
            "reason": "主修 requirement_ids 中沒有明確的總畢業學分閘門，不能推算總進度。",
        }
    if len(scoped) != 1:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "PRIMARY_AGGREGATE_NOT_UNIQUE",
            "reason": "主修 requirement_ids 中有多個總畢業學分閘門，不能安全選擇其中之一。",
        }
    row = scoped[0]
    owner = _text(row.get("owner")).upper().replace("-", "_").replace(" ", "_")
    if not bool(row.get("owner_present")) or owner != "PRIMARY":
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "PRIMARY_AGGREGATE_OWNER_INVALID",
            "reason": "總畢業學分閘門沒有明確的 PRIMARY 所有權，不能把目標端或未標記資料當作主修總量。",
            "requirement_id": row.get("requirement_id", ""),
        }
    coverage = _text(row.get("coverage_state")).upper()
    evidence = _text(row.get("evidence_state")).upper()
    if coverage not in {"COMPLETE", "RESOLVED"} or evidence != "VERIFIED":
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "AGGREGATE_GATE_UNVERIFIED",
            "reason": "總畢業學分閘門的官方覆蓋或證據尚未完整核實，不能推算總進度。",
            "requirement_id": row.get("requirement_id", ""),
        }
    required = _decimal(row.get("required_credits"))
    if required <= 0:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "AGGREGATE_GATE_REQUIRED_INVALID",
            "reason": "總畢業學分閘門沒有正數的官方要求總量，不能推算總進度。",
            "requirement_id": row.get("requirement_id", ""),
        }
    gate_status = _safe_status(row.get("status"))
    if gate_status not in {PASS, FAIL}:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "AGGREGATE_GATE_STATUS_UNKNOWN",
            "reason": "總畢業學分閘門的配置狀態仍不唯一或待確認，不能安全推算總進度。",
            "requirement_id": row.get("requirement_id", ""),
        }
    requirement_id = _text(row.get("requirement_id"))
    by_requirement = ledger.get("exclusive_by_requirement") if isinstance(ledger, Mapping) and isinstance(ledger.get("exclusive_by_requirement"), Mapping) else {}
    row_exclusive = _decimal(row.get("exclusive_credits"))
    ledger_exclusive = _decimal(by_requirement.get(requirement_id))
    if not bool(row.get("exclusive_credits_present")) or row_exclusive != ledger_exclusive:
        return {
            "available": False,
            "status": "UNAVAILABLE",
            "reason_code": AGGREGATE_GATE_UNAVAILABLE,
            "detail_code": "PRIMARY_AGGREGATE_NUMERATOR_UNVERIFIED",
            "reason": "主修總畢業學分閘門的正式配置數字無法由來源配置列重建，不能安全推算總進度。",
            "requirement_id": requirement_id,
        }
    return {
        "available": True,
        "status": "AVAILABLE",
        "gate_status": gate_status,
        "reason_code": "",
        "detail_code": "",
        "reason": "唯一明確的總畢業學分閘門已由完整官方資料核實。",
        "requirement_id": requirement_id,
        "completed_credits": _decimal_text(row_exclusive),
        "required_credits": _decimal_text(required),
        "numerator_source": "primary_aggregate_requirement.exclusive_credits",
        "shadow_excluded": True,
    }


def _program_progress(decisions: Any, metric_rows: Sequence[Mapping[str, Any]], aggregate_gate: Mapping[str, Any]) -> dict[str, Any]:
    decisions_map = decisions if isinstance(decisions, Mapping) else {}
    names = (
        ("primary", "primary_graduation"),
        ("double_major", "double_major_qualification"),
        ("minor", "minor_application_or_qualification"),
        ("target_coursework", "target_coursework_completion"),
        ("overall", "overall"),
    )
    result: dict[str, Any] = {}
    for name, key in names:
        raw_decision = decisions_map.get(key)
        # Missing official decisions are unresolved, not not-applicable.  The
        # service emits explicit NOT_APPLICABLE records for programs the
        # student did not select; a bare/legacy payload has no such evidence.
        decision = raw_decision if isinstance(raw_decision, Mapping) else {}
        status = _safe_status(decision.get("status", UNKNOWN))
        applicable = status != NOT_APPLICABLE
        # Gate counts are non-additive status observations.  They never stand
        # in for credit totals or course counts.
        result[name] = {
            "status": status,
            "applicable": applicable,
            "gate_count": {"PASS": 1 if status == PASS else 0, "FAIL": 1 if status == FAIL else 0, "UNKNOWN": 1 if applicable and status not in {PASS, FAIL} else 0},
            "aggregate_credit_progress": dict(aggregate_gate) if name == "primary" else {"available": False, "status": "UNAVAILABLE", "reason_code": AGGREGATE_GATE_UNAVAILABLE, "reason": "此圖表只允許主修唯一總畢業學分閘門。"},
        }
    status_counts: defaultdict[str, int] = defaultdict(int)
    for row in result.values():
        if row.get("applicable"):
            status_counts[_safe_status(row.get("status"))] += 1
    result["gate_status_counts"] = {key: int(status_counts.get(key, 0)) for key in (PASS, FAIL, UNKNOWN)}
    result["requirement_gate_status_counts"] = {
        key: sum(1 for row in metric_rows if _safe_status(row.get("status")) == key)
        for key in (PASS, FAIL, UNKNOWN)
    }
    return result


def build_statistics_v2(
    attempts: Any,
    requirements: Any,
    allocation: Any,
    *,
    decisions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical statistics v2 object from one allocator result.

    This function is pure aggregation: it never calls curriculum resolvers,
    evaluators, or the allocator.  Both the service and compatibility
    projection use the same shape, so chart and export consumers cannot invent
    their own credit totals.
    """

    course_observations = _attempt_statuses(attempts)
    ledger = _allocation_parts(attempts, requirements, allocation)
    metric_rows = _validated_requirement_metric_rows(_requirement_metric_rows(requirements, allocation), ledger)
    status_counts: defaultdict[str, int] = defaultdict(int)
    deficits: list[dict[str, Any]] = []
    for row in metric_rows:
        status_counts[_safe_status(row.get("status"))] += 1
        if _decimal(row.get("deficit")) > 0:
            deficits.append(
                {
                    "requirement_id": row.get("requirement_id", ""),
                    "name": row.get("name", ""),
                    "deficit": row.get("deficit", "0"),
                    "status": row.get("status", UNKNOWN),
                }
            )
    aggregate_gate = _aggregate_gate(metric_rows, ledger, decisions)
    progress = _program_progress(decisions, metric_rows, aggregate_gate)
    # Keep course observations and requirement outcomes in separate maps.
    # They describe different units: one row in a transcript can be observed
    # without satisfying any requirement, while one requirement can remain
    # UNKNOWN even when several course rows are unresolved.  The explicit
    # names are part of the v2 contract and prevent downstream consumers from
    # inferring one count from the other.
    course_counts = {
        key: int(value)
        for key, value in sorted(course_observations.get("counts", {}).items(), key=lambda item: str(item[0]))
    }
    requirement_counts = {
        key: int(value)
        for key, value in sorted(status_counts.items(), key=lambda item: str(item[0]))
    }
    for key in (PASS, FAIL, UNKNOWN):
        requirement_counts.setdefault(key, 0)
    requirement_counts["deficit_count"] = len(deficits)
    directions = tuple(
        f"{item['name']} 尚缺 {item['deficit']} 學分；請依官方課表補修或人工確認。"
        for item in sorted(deficits, key=lambda entry: (entry.get("status") != FAIL, str(entry.get("requirement_id"))))
    )
    non_credit_results = decisions.get("non_credit_results", {}) if isinstance(decisions, Mapping) else {}
    subset_results = decisions.get("subset_results", ()) if isinstance(decisions, Mapping) else ()
    if not isinstance(non_credit_results, Mapping):
        non_credit_results = {}
    if not isinstance(subset_results, (list, tuple)):
        subset_results = ()
    non_credit_status_counts: defaultdict[str, int] = defaultdict(int)
    for scoped_rows in non_credit_results.values():
        if not isinstance(scoped_rows, (list, tuple)):
            continue
        for row in scoped_rows:
            if isinstance(row, Mapping):
                non_credit_status_counts[_safe_status(row.get("status"))] += 1
    statistics: dict[str, Any] = {
        "schema_version": STATISTICS_SCHEMA_VERSION,
        "statistics_schema": STATISTICS_SCHEMA_VERSION,
        "credit_ledger": ledger,
        "requirement_metrics": {
            "items": metric_rows,
            "status_counts": {key: int(status_counts[key]) for key in sorted(status_counts)},
            "deficits": tuple(deficits),
            "additive": False,
        },
        "program_progress": progress,
        "course_observations": course_observations,
        "remediation": {
            "status": "DIRECTION_ONLY",
            "directions": directions,
            "minimality_proven": False,
            "reason": "目前只提供安全補修方向，尚未證明全域最小補修集合。",
        },
        # Compatibility fields have precise names and meanings.  The old
        # ``total_graduation_credits`` label is intentionally not emitted.
        "source_earned_credits": ledger["source_earned_credits"],
        "exclusive_source_credits": ledger["exclusive_allocated_credits"],
        "recognized_credits": ledger["exclusive_allocated_credits"],
        "unallocated_credits": ledger["unallocated_credits"],
        "shared_shadow_credits": ledger["shared_shadow_credits"],
        # Kept only as a deprecated compatibility alias.  It deliberately
        # equals exclusive counted credits; adding a shadow row must never
        # inflate a graduation numerator.
        "effective_recognized_credits": ledger["exclusive_allocated_credits"],
        "credit_conservation": bool(ledger["conservation"]["ok"]),
        # These fields are copied from the allocator result so projections
        # can distinguish a feasible witness from search/route ambiguity
        # without recalculating or downgrading the decision.
        "feasibility": _safe_status(_field(allocation, "feasibility", UNKNOWN)),
        "feasible_witness": _flag(_field(allocation, "feasible_witness", False)) is True,
        "search_complete": _flag(_field(allocation, "search_complete", None)),
        "optimality": _text(_field(allocation, "optimality", ""), "UNKNOWN"),
        "allocation_ambiguous": _flag(_field(allocation, "allocation_ambiguous", False)) is True,
        "route_ambiguity": _flag(_field(allocation, "route_ambiguity", False)) is True,
        "decision_ambiguity": _flag(_field(allocation, "decision_ambiguity", False)) is True,
        "non_credit_results": non_credit_results,
        "non_credit_status_counts": {
            key: int(non_credit_status_counts.get(key, 0))
            for key in (PASS, FAIL, UNKNOWN)
        },
        "subset_results": tuple(subset_results),
        "by_bucket": ledger["exclusive_by_bucket"],
        "by_owner": ledger["exclusive_by_owner"],
        "minor_credits": ledger["exclusive_by_owner"].get("MINOR", "0"),
        "requirement_status_counts": {key: int(status_counts[key]) for key in sorted(status_counts)},
        "course_status_counts": course_observations["counts"],
        "course_counts": course_counts,
        "requirement_counts": requirement_counts,
        "completed": course_observations["completed_count"],
        "in_progress": course_observations["in_progress_count"],
        "course_unresolved_count": course_observations["unresolved_count"],
        "in_progress_nominal_credits": course_observations["in_progress_nominal_credits"],
        "deficits": tuple(deficits),
        "safe_remediation_directions": directions,
        "statistics_validation": {
            "status": "VALID",
            "valid": True,
            "reason_code": "",
            "reason": "統計已由目前 AllocationResult 的來源列、要求結果與守恆欄位重新驗證。",
        },
    }
    statistics["statistics_digest"] = _statistics_digest(statistics)
    return statistics


def _split_rows(rows: Sequence[Mapping[str, Any]], size: int) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    return tuple(tuple(rows[index : index + size]) for index in range(0, len(rows), size))


def _presentation_warnings(chart_datasets: Mapping[str, Any]) -> tuple[str, ...]:
    """Expose chart availability failures as a shared, non-policy warning list."""

    warnings: list[str] = []
    for chart_id in ("f1", "f5", "f7", "f11"):
        dataset = chart_datasets.get(chart_id)
        if not isinstance(dataset, Mapping) or dataset.get("available"):
            continue
        reason_code = _text(dataset.get("reason_code"), "CHART_UNAVAILABLE")
        warnings.append(f"{chart_id.upper()}：{reason_code}")
    return tuple(warnings)


def build_chart_datasets(statistics: Mapping[str, Any], *, snapshot_id: str = "") -> Mapping[str, Any]:
    """Return stable, render-ready chart data without recalculating decisions."""

    stats = _plain(statistics) if isinstance(statistics, Mapping) else {}
    ledger = stats.get("credit_ledger") if isinstance(stats.get("credit_ledger"), Mapping) else {}
    conservation = ledger.get("conservation") if isinstance(ledger.get("conservation"), Mapping) else {}
    conservation_ok = _flag(conservation.get("ok")) is True and _safe_status(conservation.get("status")) == PASS
    metrics = stats.get("requirement_metrics") if isinstance(stats.get("requirement_metrics"), Mapping) else {}
    rows = tuple(item for item in metrics.get("items", ()) if isinstance(item, Mapping))
    # A normalized v2 payload already carries the digest of its canonical
    # statistics object.  Preserve it across the renderer's privacy copy so
    # screen and exports identify the same snapshot; only a hand-built mapping
    # without a digest needs a local deterministic fallback.
    digest = _text(stats.get("statistics_digest")) or _statistics_digest(stats)
    f5_rows = tuple(
        {
            "label": _text(row.get("name"), _text(row.get("requirement_id"))),
            "completed": row.get("effective_credits", "0"),
            "required": row.get("required_credits", "0"),
            "unit": "學分",
            "requirement_id": row.get("requirement_id", ""),
            "status": row.get("status", UNKNOWN),
        }
        for row in rows
        if _decimal(row.get("required_credits")) > 0
    )
    f5_available = bool(f5_rows) and conservation_ok
    f5 = {
        "chart_type": "F5_TICK_ROWS",
        "available": f5_available,
        "reason_code": "" if f5_available else "CREDIT_CONSERVATION_FAILED" if not conservation_ok else "REQUIREMENT_METRICS_UNAVAILABLE",
        "row_limit": 8,
        "groups": tuple(
            {"dataset_id": f"f5:{index + 1}", "rows": group, "row_count": len(group), "max_row_count": 8}
            for index, group in enumerate(_split_rows(f5_rows, 8))
        ),
        "snapshot_id": snapshot_id,
        "statistics_digest": digest,
    }
    by_bucket = ledger.get("exclusive_by_bucket") if isinstance(ledger.get("exclusive_by_bucket"), Mapping) else {}
    unclassified = _decimal(ledger.get("unclassified_exclusive_credits"))
    unclassified_bucket_present = any(
        _text(key).upper() in {"UNKNOWN", "UNCLASSIFIED", "UNCLASSIFIED_CATEGORY"}
        for key in by_bucket
    )
    f1_rows = tuple(
        {"label": _text(key), "value": value, "unit": "學分", "allocation_kind": EXCLUSIVE}
        for key, value in sorted(by_bucket.items(), key=lambda item: str(item[0]))
        if _decimal(value) > 0
    )
    f1 = {
        "chart_type": "F1_RUNG_BARS",
        "available": bool(f1_rows) and conservation_ok and not unclassified_bucket_present and unclassified <= 0,
        "reason_code": "" if f1_rows and conservation_ok and not unclassified_bucket_present and unclassified <= 0 else "CREDIT_CONSERVATION_FAILED" if not conservation_ok else "UNCLASSIFIED_CREDIT_BUCKET" if unclassified_bucket_present or unclassified > 0 else "EXCLUSIVE_CREDIT_LEDGER_EMPTY",
        "rows": f1_rows,
        "row_limit": 8,
        "snapshot_id": snapshot_id,
        "statistics_digest": digest,
    }
    # F7 groups requirement gate *counts*, not credits.  Categories are
    # official buckets only; an unknown bucket would produce a misleading
    # named category, so fail closed.
    f7_groups: defaultdict[str, dict[str, int]] = defaultdict(lambda: {PASS: 0, FAIL: 0, UNKNOWN: 0})
    unclassified_category = False
    for row in rows:
        bucket = _text(row.get("bucket")) or UNCLASSIFIED_CATEGORY
        if bucket.upper() in {"UNKNOWN", "UNCLASSIFIED", "UNCLASSIFIED_CATEGORY"}:
            unclassified_category = True
        status = _safe_status(row.get("status"))
        if status in {PASS, FAIL, UNKNOWN}:
            f7_groups[bucket][status] += 1
    f7_rows = tuple(
        {"label": bucket, "PASS": values[PASS], "FAIL": values[FAIL], "UNKNOWN": values[UNKNOWN], "total": sum(values.values())}
        for bucket, values in sorted(f7_groups.items(), key=lambda item: item[0])
        if sum(values.values()) > 0
    )
    f7 = {
        "chart_type": "F7_GATE_STATUS_RUNG_COUNTS",
        "available": bool(f7_rows) and not unclassified_category and len(f7_rows) <= 4,
        "reason_code": "" if f7_rows and not unclassified_category and len(f7_rows) <= 4 else "UNCLASSIFIED_GATE_CATEGORY" if unclassified_category else "TOO_MANY_GATE_CATEGORIES" if len(f7_rows) > 4 else "GATE_COUNTS_UNAVAILABLE",
        "mode": "gate_counts",
        "max_category_count": 4,
        "statuses": (PASS, FAIL, UNKNOWN),
        "rows": f7_rows,
        "snapshot_id": snapshot_id,
        "statistics_digest": digest,
    }
    aggregate = stats.get("program_progress", {}).get("primary", {}).get("aggregate_credit_progress", {}) if isinstance(stats.get("program_progress"), Mapping) and isinstance(stats.get("program_progress", {}).get("primary", {}), Mapping) else {}
    numerator_proven = (
        _text(aggregate.get("numerator_source")) in {
            "primary_aggregate_requirement.exclusive_credits",
            "primary_requirement_scope.exclusive_credits",
        }
        and bool(aggregate.get("shadow_excluded"))
    )
    f11_available = bool(aggregate.get("available")) and conservation_ok and numerator_proven and _decimal(aggregate.get("required_credits")) > 0
    f11_reason_code = (
        ""
        if f11_available
        else "CREDIT_CONSERVATION_FAILED"
        if not conservation_ok
        else "PRIMARY_AGGREGATE_NUMERATOR_UNVERIFIED"
        if aggregate.get("available") and not numerator_proven
        else AGGREGATE_GATE_UNAVAILABLE
    )
    f11_reason = (
        "來源學分守恆檢查失敗，不能安全推算總進度。"
        if f11_reason_code == "CREDIT_CONSERVATION_FAILED"
        else "主修總畢業學分閘門的正式配置數字缺少可追溯的來源證明。"
        if f11_reason_code == "PRIMARY_AGGREGATE_NUMERATOR_UNVERIFIED"
        else aggregate.get("reason", "沒有唯一且明確的已核實總畢業學分閘門，不能推算總進度。")
    )
    f11 = {
        "chart_type": "F11_TICK_GAUGE",
        "available": f11_available,
        "reason_code": f11_reason_code,
        "reason": f11_reason,
        "completed": aggregate.get("completed_credits", "0") if f11_available else "",
        "required": aggregate.get("required_credits", "0") if f11_available else "",
        "numerator_source": aggregate.get("numerator_source", ""),
        "shadow_excluded": True,
        "snapshot_id": snapshot_id,
        "statistics_digest": digest,
    }
    datasets: dict[str, Any] = {
        "schema_version": CHART_DATASET_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "statistics_digest": digest,
        "f1": f1,
        "f5": f5,
        "f7": f7,
        "f11": f11,
    }
    datasets["presentation_warnings"] = _presentation_warnings(datasets)
    return _freeze(datasets)


def _fail_closed_statistics(reason_code: str, reason: str) -> Mapping[str, Any]:
    """Return a safe empty v2 object when a persisted statistics payload is stale."""

    result = build_statistics_v2(
        (),
        (),
        {"source_earned_credits": "0", "credit_conservation": False, "allocations": (), "requirement_results": ()},
        decisions={},
    )
    result["statistics_validation"] = {
        "status": "UNKNOWN",
        "valid": False,
        "reason_code": reason_code,
        "reason": reason,
    }
    result["statistics_digest"] = _statistics_digest(result)
    return _freeze(result)


def _expected_statistics_body(
    expected: Mapping[str, Any],
    existing: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the comparable body, accounting for fields omitted by snapshots.

    ``DecisionSnapshot.as_dict()`` intentionally keeps a compact requirement
    projection and does not serialize ``RequirementSpec.kind``.  The service
    statistics may still contain that value.  Treat only that specifically
    unavailable field as unknown; every numeric, status, owner, scope, and
    ledger invariant remains an exact comparison.
    """

    body = _plain(expected)
    stored = _plain(existing)
    payload_requirements = {
        _text(item.get("requirement_id")): item
        for item in payload.get("requirements", ())
        if isinstance(item, Mapping) and _text(item.get("requirement_id"))
    }
    stored_items = {}
    stored_metrics = stored.get("requirement_metrics") if isinstance(stored.get("requirement_metrics"), Mapping) else {}
    for item in stored_metrics.get("items", ()) if isinstance(stored_metrics, Mapping) else ():
        if isinstance(item, Mapping) and _text(item.get("requirement_id")):
            stored_items[_text(item.get("requirement_id"))] = item
    expected_metrics = body.get("requirement_metrics") if isinstance(body.get("requirement_metrics"), Mapping) else {}
    expected_items = expected_metrics.get("items", ()) if isinstance(expected_metrics, Mapping) else ()
    for item in expected_items:
        if not isinstance(item, dict):
            continue
        requirement_id = _text(item.get("requirement_id"))
        if requirement_id and requirement_id in stored_items and requirement_id in payload_requirements:
            if "kind" not in payload_requirements[requirement_id] and not item.get("kind"):
                item["kind"] = stored_items[requirement_id].get("kind", "")
    return {key: value for key, value in body.items() if key != "statistics_digest"}


def statistics_projection_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Normalize a snapshot payload to statistics v2 without evaluating it.

    New service snapshots already carry v2 and are copied losslessly.  Older
    test fixtures or persisted snapshots are upgraded from their immutable
    allocation/requirement records through :func:`build_statistics_v2`; no
    resolver or allocator is invoked here.
    """

    if not isinstance(payload, Mapping):
        return _freeze(build_statistics_v2((), (), {}, decisions={}))
    existing = payload.get("statistics")
    expected = build_statistics_v2(
        payload.get("attempts", ()),
        payload.get("requirements", ()),
        payload.get("allocation", {}),
        decisions=payload.get("decisions") if isinstance(payload.get("decisions"), Mapping) else {},
    )
    if not (isinstance(existing, Mapping) and _text(existing.get("schema_version")) == STATISTICS_SCHEMA_VERSION):
        return _freeze(expected)

    normalized = _plain(existing)
    normalized["schema_version"] = STATISTICS_SCHEMA_VERSION
    normalized["statistics_schema"] = STATISTICS_SCHEMA_VERSION
    stored_digest = _text(normalized.get("statistics_digest"))
    expected_body = _expected_statistics_body(expected, normalized, payload)
    stored_body = {key: value for key, value in normalized.items() if key != "statistics_digest"}
    if stored_digest != _statistics_digest(normalized) or stored_body != expected_body:
        return _fail_closed_statistics(
            "STALE_STATISTICS_PAYLOAD",
            "快照中的 decision-statistics.v2 與目前 AllocationResult 不一致，已停止沿用舊統計。",
        )
    return _freeze(normalized)


def build_snapshot_statistics_projection(snapshot: DecisionSnapshot) -> Mapping[str, Any]:
    """Read one DecisionSnapshot and return its stable statistics projection."""

    if not isinstance(snapshot, DecisionSnapshot):
        raise TypeError("build_snapshot_statistics_projection expects a DecisionSnapshot")
    payload = snapshot.as_dict()
    result = dict(statistics_projection_from_payload(payload))
    # A snapshot may have been produced by an old service without chart data;
    # charts are still derived from the same normalized statistics object.
    result["chart_datasets"] = _freeze(build_chart_datasets(result, snapshot_id=snapshot.snapshot_id))
    result["presentation_warnings"] = result["chart_datasets"].get("presentation_warnings", ())
    return _freeze(result)


def build_snapshot_projection(snapshot: DecisionSnapshot) -> Mapping[str, Any]:
    """Compatibility entry point for the canonical screen projection.

    The implementation remains in :mod:`snapshot_renderer` because it owns
    HTML-specific requirement/course views.  Keeping this lazy wrapper here
    gives callers one projection namespace without introducing an import
    cycle: the renderer imports the pure statistics helpers above, while this
    function imports the renderer only when a caller actually asks for the
    full presentation view.
    """

    from snapshot_renderer import build_snapshot_projection as _build_snapshot_projection

    return _build_snapshot_projection(snapshot)


def snapshot_to_legacy_report(snapshot: DecisionSnapshot) -> Mapping[str, Any]:
    """Return a frozen, lossless-enough legacy report without recalculation.

    ``allocation_buckets`` contains exclusive source credit only.  Shared
    shadow credit is retained in its own list and therefore cannot inflate
    transcript credit totals.  The source conservation identity is copied
    directly from the snapshot's allocator result.
    """

    if not isinstance(snapshot, DecisionSnapshot):
        raise TypeError("snapshot_to_legacy_report expects a DecisionSnapshot")
    payload = snapshot.as_dict()
    allocation = payload["allocation"]
    statistics = statistics_projection_from_payload(payload)
    chart_datasets = build_chart_datasets(statistics, snapshot_id=snapshot.snapshot_id)
    requirements = {item["requirement_id"]: item for item in payload["requirements"]}
    buckets: defaultdict[str, Decimal] = defaultdict(Decimal)
    course_rows: list[dict[str, Any]] = []
    for item in allocation["allocations"]:
        attempt_id = _text(item.get("attempt_id"))
        attempt_rows = [
            {
                "attempt_id": attempt_id,
                "requirement_id": _text(portion.get("requirement_id")),
                "credits": _text(portion.get("credits")),
                "allocation_kind": _text(portion.get("allocation_kind")),
                "direction": _text(portion.get("direction")),
                "binding_id": _text(portion.get("binding_id")),
            }
            for portion in item.get("portions", ())
        ]
        course_rows.extend(attempt_rows)
        for portion in attempt_rows:
            if portion["allocation_kind"] != "EXCLUSIVE":
                continue
            requirement = requirements.get(portion["requirement_id"], {})
            bucket = _text(requirement.get("bucket")) or "unclassified"
            buckets[bucket] += _decimal(portion["credits"])
        unallocated = _decimal(item.get("unallocated_credits"))
        if unallocated > 0:
            buckets["unallocated"] += unallocated
    # The legacy bucket list is still a consumer of the canonical additive
    # ledger.  Do not re-count raw portions here (which could include a
    # malformed in-progress row or a shared shadow).
    buckets.clear()
    for key, value in statistics["credit_ledger"]["exclusive_by_bucket"].items():
        buckets[key] = _decimal(value)
    unallocated_total = _decimal(statistics["credit_ledger"].get("unallocated_credits"))
    if unallocated_total > 0:
        buckets["unallocated"] = unallocated_total
    allocation_buckets = tuple(
        {"bucket": key, "credits": str(value)}
        for key, value in sorted(buckets.items(), key=lambda item: item[0])
    )
    summary = {
        "verdict": payload["verdict"],
        "primary_graduation": payload["decisions"].get("primary_graduation", {}).get("status", "UNKNOWN"),
        "double_major_qualification": payload["decisions"].get("double_major_qualification", {}).get("status", "NOT_APPLICABLE"),
        "formal_double_major_award": payload["decisions"].get("formal_double_major_award", {}).get("status", "NOT_APPLICABLE"),
        "minor_application_or_qualification": payload["decisions"].get("minor_application_or_qualification", {}).get("status", "NOT_APPLICABLE"),
        "minor_coursework_completion": payload["decisions"].get("minor_coursework_completion", {}).get("status", "NOT_APPLICABLE"),
        "formal_minor_award": payload["decisions"].get("formal_minor_award", {}).get("status", "NOT_APPLICABLE"),
        "overall": payload["decisions"].get("overall", {}).get("status", payload["verdict"]),
        "source_earned_credits": statistics["source_earned_credits"],
        "exclusive_source_credits": statistics["credit_ledger"]["exclusive_allocated_credits"],
        "legacy_aliases": {
            "exclusive_source_credits": {
                "value": statistics["credit_ledger"]["exclusive_allocated_credits"],
                "deprecated": True,
                "meaning": "正式配置的 EXCLUSIVE 學分；來源總量另見 source_earned_credits",
            },
        },
        "recognized_credits": statistics["recognized_credits"],
        "effective_recognized_credits": statistics["effective_recognized_credits"],
        "unallocated_credits": statistics["unallocated_credits"],
        "shared_shadow_credits": statistics["shared_shadow_credits"],
        "minor_credits": statistics.get("minor_credits", "0"),
        "credit_conservation": bool(statistics["credit_conservation"]),
        "statistics_schema": statistics.get("schema_version", STATISTICS_SCHEMA_VERSION),
        "statistics_digest": statistics.get("statistics_digest", ""),
        "search_complete": bool(payload.get("search_complete")),
        "optimality": payload.get("optimality", "UNKNOWN"),
    }
    report = {
        "_snapshot_id": payload["snapshot_id"],
        "_projection_schema": "legacy-report.v2",
        "_source": "DecisionSnapshot",
        "statistics_schema": statistics.get("schema_version", STATISTICS_SCHEMA_VERSION),
        "statistics_digest": statistics.get("statistics_digest", ""),
        "summary": summary,
        "allocation_buckets": allocation_buckets,
        "courses": tuple(course_rows),
        "requirements": payload["requirements"],
        "allocation": allocation,
        "decisions": payload["decisions"],
        "rule_provenance": payload["rule_provenance"],
        "input_confirmation": payload["input_confirmation"],
        "statistics": statistics,
        "chart_datasets": chart_datasets,
        "presentation_warnings": chart_datasets.get("presentation_warnings", ()),
        "blockers": payload["blockers"],
        "warnings": payload["warnings"],
        "remediation_suggestions": statistics.get("safe_remediation_directions", payload["remediation_suggestions"]),
    }
    return _freeze(report)


__all__ = [
    "AGGREGATE_GATE_UNAVAILABLE",
    "CHART_DATASET_SCHEMA_VERSION",
    "PROJECTION_SCHEMA_VERSION",
    "STATISTICS_SCHEMA_VERSION",
    "build_chart_datasets",
    "build_snapshot_projection",
    "build_snapshot_statistics_projection",
    "build_statistics_v2",
    "snapshot_to_legacy_report",
    "statistics_projection_from_payload",
]
