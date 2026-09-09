"""The single, evidence-aware graduation evaluation boundary.

The Streamlit application has several historical report paths.  This module
is deliberately framework-independent: a caller hands it a frozen request
whose course rows have already crossed :mod:`input_confirmation`, and it
returns one :class:`decision_snapshot.DecisionSnapshot`.  UI, charts, and
exports can then consume that snapshot without re-running a rule engine.

The service is intentionally conservative.  A checked-in curriculum record
may expose useful planning aggregates while still lacking a named course
pool.  Such rows are compiled as non-accepting requirements and remain
``UNKNOWN`` until an official, complete source is available.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from allocation_engine import (
    COMPLETE,
    CONFLICTED,
    FAIL,
    MANUAL_REVIEW,
    MISSING,
    NONE,
    NOT_APPLICABLE,
    NOT_MEMBER,
    PARTIAL,
    PASS,
    SHARED_SHADOW,
    UNKNOWN,
    VERIFIED,
    WAIVER,
    AllocationResult,
    CourseAttempt,
    EquivalencyBinding,
    RequirementResult,
    RequirementSpec,
    _pool_membership_record,
    allocate_credits,
    canonicalize_overflow_routes,
    normalize_course_kind,
)
from application_resolution import (
    resolve_application_case,
    resolve_formal_award,
    resolve_minor_application_case,
    resolve_minor_award,
)
from curriculum_registry import (
    _program_slug,
    _track_slug,
    get_curriculum,
    list_curriculum_ids,
    resolve_rule_context,
)
from decision_snapshot import DecisionSnapshot
from handbook_rules import normalize_course_name
from input_confirmation import (
    ConfirmationState,
    CourseConfirmation,
    NormalizedCourseRow,
    fingerprint_course_rows,
    release_formal_attempts,
)
from public_course_catalog import (
    VERIFIED as PUBLIC_VERIFIED,
)
from public_course_catalog import (
    PublicCourseEvidence,
    load_public_course_catalog,
    resolve_public_evidence,
)
from snapshot_projection import build_statistics_v2

SERVICE_SCHEMA_VERSION = "graduation-evaluation.v1"
ENGINE_VERSION = "graduation-service.v1"

_DOUBLE_MAJOR = "雙主修"
_TEXT_FIELDS = frozenset({"admission_cohort", "primary_curriculum_id", "program_type", "secondary_kind", "target_curriculum_year"})
# Portal input is transcript-only.  Keep the allowlist empty so stale warning
# values from a pre-removal session cannot affect a DecisionSnapshot.
INPUT_WARNING_CODES = frozenset()
_EVIDENCE_FIELDS = (
    "target_curriculum_evidence_id",
    "application_event_evidence_id",
    "rule_applicability_evidence_id",
    "department_decision_evidence_id",
    "registrar_registration_evidence_id",
    "formal_qualification_evidence_id",
    "formal_award_evidence_id",
)


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float, bool)) else ""


def _number(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float, str)) and not isinstance(value, bool):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return default
    else:
        return default
    return result if result.is_finite() else default


def _positive_number(value: Any) -> Decimal:
    return max(Decimal("0"), _number(value))


def _freeze(value: Any) -> Any:
    """Recursively freeze a JSON-shaped value for a snapshot field."""

    from types import MappingProxyType

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _unique_text(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        return ()
    return tuple(sorted({_text(item) for item in values if _text(item)}))


def _is_double_major(value: Any) -> bool:
    normalized = _text(value)
    return normalized == _DOUBLE_MAJOR or normalized.lower() in {"double", "double_major", "dm"}


def _secondary_kind(value: Any, program_type: Any = None) -> str:
    """Normalize the additive secondary role without breaking old callers."""

    candidate = _text(value) or _text(program_type)
    normalized = candidate.lower().replace("-", "_").replace(" ", "")
    if normalized in {"minor", "minor_target", "secondary_minor", "輔系"}:
        return "minor"
    if _is_double_major(candidate):
        return "double_major"
    return "none"


def _is_minor_request(request: EvaluationRequest) -> bool:
    return _secondary_kind(request.secondary_kind, request.program_type) == "minor"


def _is_double_request(request: EvaluationRequest) -> bool:
    return _secondary_kind(request.secondary_kind, request.program_type) == "double_major"


def _course_label(value: Any) -> str:
    """Normalize punctuation/spacing and numerals; never perform fuzzy matching."""

    text = _text(value)
    if not text:
        return ""
    return normalize_course_name(text).casefold()


def _safe_provenance(record: Any, *, requirement_id: str = "", scope: str = "") -> dict[str, Any]:
    """Keep official scalar audit handles and discard arbitrary record data."""

    if not isinstance(record, Mapping):
        return {
            "requirement_id": requirement_id,
            "scope": scope,
            "evidence_state": UNKNOWN,
            "automatic_decision": False,
        }
    result: dict[str, Any] = {
        "requirement_id": requirement_id,
        "scope": scope,
    }
    keys = (
        "id",
        "assertion_id",
        "source_assertion_id",
        "evidence_id",
        "record_id",
        "curriculum_id",
        "version",
        "curriculum_version",
        "admission_cohort",
        "program",
        "program_slug",
        "track",
        "track_slug",
        "waiver_requirement_version",
        "waiver_program_slug",
        "waiver_track_slug",
        "activity_membership_prefix",
        "source_type",
        "source_file",
        "source_url",
        "source_reference",
        "pages",
        "page",
        "pdf_page",
        "printed_page",
        "location",
        "table_location",
        "original_text",
        "original_clause",
        "claim",
        "raw_title",
        "evidence_state",
        "coverage_state",
        "extraction_method",
        "named_course_pool_state",
        "research_file",
        "verification_status",
        "automatic_decision",
        "policy_id",
        "policy_revision",
        "revision",
        "version",
        "curriculum_version",
        "program",
        "program_slug",
        "track",
        "track_slug",
        "waiver_requirement_version",
        "waiver_program_slug",
        "waiver_track_slug",
        "activity_membership_prefix",
        "role",
        "effective_term",
        "effective_start",
        "effective_end",
        "effective_interval",
        "applicability_state",
        "source_assertion_id",
        "assertion_id",
        "policy_source_reference",
        "scope_state",
        "applicability_basis",
        "manual_reason",
        "original_clause",
        "original_text",
    )
    for key in keys:
        value = record.get(key)
        if isinstance(value, (str, int, float, bool, Decimal)) and not isinstance(value, (bytes, bytearray)):
            result[key] = str(value) if isinstance(value, Decimal) else value
    evidence_state = _text(result.get("evidence_state")) or UNKNOWN
    coverage_state = _text(result.get("coverage_state")) or NONE
    result["evidence_state"] = evidence_state
    result["coverage_state"] = coverage_state
    explicit_automatic = result.get("automatic_decision")
    result["automatic_decision"] = (
        bool(explicit_automatic)
        if isinstance(explicit_automatic, bool)
        else evidence_state == VERIFIED and coverage_state == COMPLETE
    )
    return result


def _safe_curriculum(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    result: dict[str, Any] = {}
    for key in (
        "id",
        "curriculum_id",
        "kind",
        "type",
        "curriculum_kind",
        "version",
        "curriculum_version",
        "admission_cohort",
        "program",
        "program_slug",
        "track",
        "track_slug",
        "program_type",
        "total_required",
        "base_required",
        "other_required",
        "source_file",
        "source_url",
        "source_reference",
        "evidence_state",
        "coverage_state",
        "aggregate_status",
        "status",
        "pass_eligible",
        "legacy_planning",
        "research_file",
        "warnings",
        "blockers",
        "manual_review_reasons",
        "zero_credit_gate",
        "conflicted_course_names",
        "course_pools",
        "pools",
        "non_credit_requirements",
    ):
        value = record.get(key)
        if isinstance(value, (str, int, float, bool, Decimal)):
            result[key] = str(value) if isinstance(value, Decimal) else value
    thresholds = record.get("thresholds")
    if isinstance(thresholds, Mapping):
        result["thresholds"] = _safe_thresholds(thresholds)
    for pool_key in ("course_pools", "pools"):
        pools = _safe_course_pools(record.get(pool_key))
        if pools:
            result[pool_key] = pools
    catalog: list[dict[str, Any]] = []
    for row in record.get("course_catalog", ()) if isinstance(record.get("course_catalog", ()), Sequence) else ():
        if not isinstance(row, Mapping):
            continue
        safe: dict[str, Any] = {}
        for key in (
            "id",
            "requirement_id",
            "name",
            "display_name",
            "raw_title",
            "credits",
            "bucket",
            "kind",
            "requirement_type",
            "choice_group",
            "choice_rule",
            "component",
            "component_type",
            "component_label",
            "lecture_or_lab",
            "is_lab",
            "is_zero_credit",
            "zero_credit_gate",
            "required_count",
            "required_completions",
            "minimum_course_count",
            "maximum_course_count",
            "count_unique_attempts",
            "exclusive_only",
            "exclude_free_and_unallocated",
            "require_zero_credits",
            "required_hours",
            "hours_per_completion",
            "max_completions_per_term",
            "distinct_term_required",
            "distinct_activity_required",
            "title_base",
            "completion_statuses",
            "waiver_allowed",
            "waiver_evidence_required",
            "affects_credit_ledger",
            "scope_state",
            "applicability_basis",
            "track",
            "track_slug",
            "waiver_requirement_version",
            "waiver_program_slug",
            "waiver_track_slug",
            "curriculum_version",
            "evidence_state",
            "source_assertion_id",
            "assertion_id",
            "source_reference",
            "source_url",
            "source_file",
            "pdf_page",
            "printed_page",
            "page",
            "pages",
            "official_course_identity",
            "course_code",
            "official_course_code",
            "course_id",
            "program_approved_membership_ids",
            "approved_membership_ids",
            "program_approved_exemption_memberships",
            "program_approved_applicable_terms",
            "approved_applicable_terms",
            "recognition_basis",
            "offering_program_slug",
            "course_program_slug",
            "course_owner_program",
            "owner_program_slug",
            "department_program_slug",
            "offering_department",
            "department_unit",
            "official_department_unit",
            "course_department_unit",
            "offering_department_unit",
            "department_membership",
            "secondary_course_key",
            "candidate_key",
            "course_key",
            "math_secondary_composite_required",
            "department",
            "dept",
            "college",
            "college_slug",
            "category",
            "official_category",
            "membership_ids",
            "section",
            "named_course_pool_state",
            "eligible_course_names",
            "eligible_course_options",
            "pool_ids",
            "eligible_pool_ids",
            "overflow_routes",
            "accept_any",
            "waiver",
            "waiver_generates_credits",
            "allow_combined_lab_source",
            "source_assertion_id",
            "assertion_id",
            "research_file",
            "verification_status",
            "automatic_decision",
            "manual_reason",
            "original_clause",
            "original_text",
            "table_location",
            "policy_id",
            "policy_revision",
            "revision",
            "amount_semantics",
            "minimum_credits",
            "maximum_credits",
            "max_credits",
            "membership_id",
            "official_membership_id",
            "activity_membership_prefix",
            "waiver_authority_ids",
            "min_earned_credits_per_completion",
            "minimum_earned_credits_per_completion",
            "term_bound",
            "membership_version_required",
            "membership_program_required",
            "membership_track_required",
            "eligible_course_ids",
            "course_ids",
            "official_course_ids",
            "official_course_id",
            "eligible_terms",
            "terms",
            "effective_terms",
            "excluded_membership_id",
            "observed_requirement_ids",
            "observed_requirements",
            "excluded_from_free",
            "overflow_to_free",
            "overflow_to_free_elective",
        ):
            value = row.get(key)
            if isinstance(value, (str, int, float, bool, Decimal)):
                safe[key] = str(value) if isinstance(value, Decimal) else value
            elif key == "eligible_course_names" and isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                safe[key] = tuple(_text(item) for item in value if _text(item))
            elif key == "eligible_course_options" and isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                safe[key] = tuple(
                    {"name": _text(item.get("name")), "credits": _text(item.get("credits"))}
                    for item in value
                    if isinstance(item, Mapping) and _text(item.get("name"))
                )
            elif key in {"pool_ids", "eligible_pool_ids", "overflow_routes", "completion_statuses", "membership_ids", "subset_ids", "observed_requirement_ids", "observed_requirements", "eligible_course_ids", "course_ids", "official_course_ids", "eligible_terms", "terms", "effective_terms", "waiver_authority_ids", "program_approved_membership_ids", "approved_membership_ids", "program_approved_exemption_memberships", "program_approved_applicable_terms", "approved_applicable_terms"} and isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                safe[key] = tuple(_text(item) for item in value if _text(item))
        pool_evidence = _safe_pool_membership_evidence(
            row.get("pool_membership_evidence")
            or row.get("membership_evidence")
        )
        assertion_evidence = _safe_membership_assertions(
            row.get("membership_assertions"),
            default_source=_text(row.get("source_reference")),
        )
        if assertion_evidence:
            pool_evidence = tuple(dict.fromkeys((*pool_evidence, *assertion_evidence)))
        if pool_evidence:
            safe["pool_membership_evidence"] = pool_evidence
        subset_constraints = row.get("subset_constraints")
        if isinstance(subset_constraints, Mapping):
            subset_constraints = (subset_constraints,)
        if isinstance(subset_constraints, Sequence) and not isinstance(subset_constraints, (str, bytes, bytearray)):
            safe["subset_constraints"] = _safe_subset_constraints(subset_constraints)
        for nested_key in ("match", "official_activity_aliases", "applicability_source", "policy_source", "provenance", "policy"):
            nested = row.get(nested_key)
            if isinstance(nested, Mapping):
                safe[nested_key] = _safe_policy(nested) if nested_key in {"policy_source", "provenance", "policy"} else _safe_nested_non_credit(nested)
        catalog.append(safe)
    result["course_catalog"] = tuple(sorted(catalog, key=lambda item: (str(item.get("id", "")), str(item.get("name", "")))))
    raw_non_credit = record.get("non_credit_requirements", ())
    if isinstance(raw_non_credit, Sequence) and not isinstance(raw_non_credit, (str, bytes, bytearray)):
        result["non_credit_requirements"] = tuple(
            _safe_non_credit_requirement(item)
            for item in raw_non_credit
            if isinstance(item, Mapping)
        )
    for key in ("warnings", "manual_review_reasons"):
        value = record.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result[key] = tuple(_text(item) for item in value if _text(item))
    blockers = record.get("blockers")
    if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes, bytearray)):
        result["blockers"] = tuple(
            _safe_blocker(item) if isinstance(item, Mapping) else {"reason": _text(item)}
            for item in blockers
            if isinstance(item, Mapping) or _text(item)
        )
    for key in ("zero_credit_gate",):
        if isinstance(record.get(key), bool):
            result[key] = bool(record[key])
    for key in ("conflicted_course_names",):
        value = record.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result[key] = tuple(_text(item) for item in value if _text(item))
    result["citations"] = tuple(_safe_provenance(item) for item in record.get("citations", ()) if isinstance(item, Mapping))
    result["source_assertions"] = tuple(
        _safe_provenance({**dict(record), **dict(item)}, scope="curriculum_assertion")
        for item in record.get("source_assertions", record.get("assertions", ()))
        if isinstance(item, Mapping)
    )
    result["assertions"] = result["source_assertions"]
    return result


def _safe_thresholds(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = _text(key)
        if not key_text:
            continue
        if isinstance(item, Mapping):
            result[key_text] = _safe_thresholds(item)
        elif isinstance(item, (int, float, str, Decimal)) and not isinstance(item, bool):
            numeric = _number(item, Decimal("NaN"))
            if numeric.is_finite() and numeric >= 0:
                result[key_text] = str(numeric)
    return result


_POLICY_SCALAR_KEYS = frozenset(
    {
        "policy_id",
        "revision",
        "policy_revision",
        "selection_rule",
        "evidence_state",
        "coverage_state",
        "automatic_decision",
        "scope_state",
        "source_reference",
        "policy_source_reference",
        "source_url",
        "source_file",
        "research_file",
        "pdf_page",
        "printed_page",
        "pages",
        "original_clause",
        "allowed_scope",
        "category",
        "allow_external_departments",
        "minimum_science_college_credits",
        "exclude_allocated_attempts",
        "requires_official_course_catalog",
        "requires_official_named_course_pool",
        "zero_credit",
        "required_credits",
        "minimum_course_count",
        "maximum_course_count",
        "count_unique_attempts",
        "exclusive_only",
        "exclude_free_and_unallocated",
        "excluded_from_free",
        "overflow_to_free",
        "overflow_to_free_elective",
        "maximum_credits",
        "maximum_credits_by_student_type",
        "active_subset_maxima",
        "subset_maxima",
        "amount_semantics",
        "membership_id",
        "excluded_membership_id",
        "waiver_allowed",
        "manual_reason",
    }
)
_POLICY_LIST_KEYS = frozenset(
    {
        "curriculum_versions",
        "program_slugs",
        "track_slugs",
        "roles",
        "title_aliases",
        "membership_ids",
        "science_college_departments",
        "exclude_course_ids",
        "exclude_course_names",
        "exclude_course_types",
        "science_membership_fields",
        "exclude_pool_ids",
        "exclude_categories",
        "official_catalog_fields",
        "observed_requirement_ids",
        "observed_requirements",
        "excluded_membership_ids",
        "waiver_authority_ids",
        "excluded_membership_id",
        "required_membership_ids",
    }
)
_POLICY_PREDICATE_KEYS = frozenset(
    {
        "kind",
        "category",
        "minimum_credits",
        "minimum_course_count",
        "maximum_course_count",
        "count_unique_attempts",
        "exclusive_only",
        "exclude_free_and_unallocated",
        "excluded_from_free",
        "overflow_to_free",
        "overflow_to_free_elective",
        "maximum_credits",
        "maximum_credits_by_student_type",
        "active_subset_maxima",
        "subset_maxima",
        "student_type",
        "amount_semantics",
        "membership_id",
        "excluded_membership_id",
        "excluded_membership_ids",
        "observed_requirement_ids",
        "observed_requirements",
        "required_membership_ids",
        "candidate_aliases_are_not_authoritative",
        "allow_transcript_category_field",
        "official_category_fields",
        "science_membership_fields",
        "science_college_departments",
        "exclude_course_ids",
        "exclude_course_names",
        "exclude_course_types",
        "exclude_pool_ids",
        "exclude_categories",
        "official_catalog_fields",
    }
)

_SUBSET_CONSTRAINT_SCALAR_KEYS = (
    "constraint_id",
    "id",
    "membership_id",
    "subset_id",
    "pool_id",
    "student_type",
    "amount_semantics",
    "minimum_credits",
    "required_credits",
    "minimum_course_count",
    "maximum_course_count",
    "minimum",
    "maximum_credits",
    "max_credits",
    "maximum",
    "credit_cap",
    "source_reference",
    "evidence_reference",
    "policy_source_reference",
)
_SUBSET_CONSTRAINT_SEQUENCE_KEYS = (
    "observed_requirement_ids",
    "observed_requirements",
    "excluded_membership_ids",
    "excluded_membership_id",
)


def _safe_subset_constraints(value: Any) -> tuple[dict[str, Any], ...]:
    """Keep the server-owned fields needed to evaluate one subset rule."""

    if isinstance(value, Mapping):
        value = (value,)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        safe: dict[str, Any] = {}
        for key in _SUBSET_CONSTRAINT_SCALAR_KEYS:
            raw = item.get(key)
            if isinstance(raw, (str, int, float, bool, Decimal)) and not isinstance(raw, (bytes, bytearray)):
                safe[key] = str(raw) if isinstance(raw, Decimal) else raw
        for key in _SUBSET_CONSTRAINT_SEQUENCE_KEYS:
            raw = item.get(key)
            if isinstance(raw, str):
                raw = (raw,)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                safe[key] = tuple(_text(part) for part in raw if _text(part))
        if safe:
            result.append(safe)
    return tuple(result)


def _safe_policy(value: Any) -> dict[str, Any]:
    """Project the narrow official policy contract used by the core."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in _POLICY_SCALAR_KEYS:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool, Decimal)) and not isinstance(item, (bytes, bytearray)):
            result[key] = str(item) if isinstance(item, Decimal) else item
        elif key in {
            "maximum_credits_by_student_type",
            "active_subset_maxima",
            "subset_maxima",
        } and isinstance(item, Mapping):
            result[key] = _safe_nested_non_credit(item)
    applies = value.get("applies_to")
    if isinstance(applies, Mapping):
        result["applies_to"] = {
            key: tuple(_text(item) for item in applies.get(key, ()) if _text(item))
            for key in ("curriculum_versions", "program_slugs", "track_slugs", "roles")
            if isinstance(applies.get(key), Sequence) and not isinstance(applies.get(key), (str, bytes, bytearray))
        }
    predicate = value.get("predicate")
    if isinstance(predicate, Mapping):
        safe_predicate: dict[str, Any] = {}
        for key in _POLICY_PREDICATE_KEYS:
            item = predicate.get(key)
            if isinstance(item, (str, int, float, bool, Decimal)) and not isinstance(item, (bytes, bytearray)):
                safe_predicate[key] = str(item) if isinstance(item, Decimal) else item
            elif key == "maximum_credits_by_student_type" and isinstance(item, Mapping):
                safe_predicate[key] = _safe_nested_non_credit(item)
            elif key in _POLICY_LIST_KEYS and isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                safe_predicate[key] = tuple(_text(part) for part in item if _text(part))
        constraints = _safe_subset_constraints(predicate.get("subset_constraints"))
        if constraints:
            safe_predicate["subset_constraints"] = constraints
        result["predicate"] = safe_predicate
    constraints = _safe_subset_constraints(value.get("subset_constraints"))
    if constraints:
        result["subset_constraints"] = constraints
    policy_source = value.get("policy_source")
    if isinstance(policy_source, Mapping):
        result["policy_source"] = _safe_provenance(policy_source, scope="policy")
    return result


def _safe_nested_non_credit(value: Any) -> Any:
    """Keep only scalar/list evidence from a non-credit match descriptor."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _text(key)
            if not key_text:
                continue
            if isinstance(item, (str, int, float, bool, Decimal)) and not isinstance(item, (bytes, bytearray)):
                result[key_text] = str(item) if isinstance(item, Decimal) else item
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                result[key_text] = tuple(_text(part) for part in item if _text(part))
            elif isinstance(item, Mapping):
                nested = _safe_nested_non_credit(item)
                if nested:
                    result[key_text] = nested
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_text(item) for item in value if _text(item))
    return value if isinstance(value, (str, int, float, bool, Decimal)) else None


def _safe_pool_membership_evidence(value: Any) -> tuple[tuple[str, str, str, str], ...]:
    """Preserve each server-owned pool assertion, including its kind.

    ``kind`` distinguishes a positive catalog assertion from a trusted
    negative ``NOT_MEMBER`` assertion.  Dropping that fourth field at the
    curriculum boundary lets a later compiler mistake incomplete metadata
    for a negative fact, so it is retained alongside the id, state, and
    source reference.
    """

    if isinstance(value, Mapping):
        value = tuple(
            {"pool_id": key, **(dict(item) if isinstance(item, Mapping) else {"state": item})}
            for key, item in value.items()
        )
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    seen: dict[str, tuple[str, str, str, str]] = {}
    for item in value:
        pool_id = ""
        state = UNKNOWN
        source = ""
        kind = "catalog"
        if isinstance(item, Mapping):
            pool_id = _text(item.get("pool_id") or item.get("membership_id") or item.get("id"))
            state = _evidence(item.get("evidence_state") or item.get("state") or item.get("status"))
            source = _text(item.get("source_reference") or item.get("evidence_reference"))
            kind = _text(item.get("kind") or item.get("membership_kind")) or "catalog"
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            values = tuple(item)
            pool_id = _text(values[0]) if values else ""
            state = _evidence(values[1]) if len(values) > 1 else UNKNOWN
            source = _text(values[2]) if len(values) > 2 else ""
            kind = _text(values[3]) if len(values) > 3 else "catalog"
        if not pool_id:
            continue
        candidate = (pool_id, state, source, kind)
        previous = seen.get(pool_id)
        if previous is None or (previous[1] != VERIFIED and state == VERIFIED):
            seen[pool_id] = candidate
    return tuple(seen.values())


def _safe_membership_assertions(
    value: Any,
    *,
    default_source: str = "",
) -> tuple[tuple[str, str, str, str], ...]:
    """Project registry candidate membership assertions into pool evidence."""

    if isinstance(value, Mapping):
        value = tuple(value.values())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[tuple[str, str, str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        membership_id = _text(item.get("membership_id") or item.get("pool_id") or item.get("id"))
        if not membership_id:
            continue
        state = _evidence(item.get("evidence_state") or item.get("state") or item.get("status"))
        source = _text(
            item.get("source_reference")
            or item.get("evidence_reference")
            or item.get("policy_source_reference")
            or default_source
        )
        kind = _text(item.get("kind") or item.get("membership_kind"))
        if not kind:
            kind = "registry_negative" if state == NOT_MEMBER else "registry"
        result.append((membership_id, state, source, kind))
    return tuple(dict.fromkeys(result))


def _safe_non_credit_requirement(value: Mapping[str, Any]) -> dict[str, Any]:
    scalar_keys = (
        "id",
        "requirement_id",
        "name",
        "kind",
        "requirement_type",
        "required",
        "credits",
        "is_zero_credit",
        "zero_credit_gate",
        "required_count",
        "required_completions",
        "require_zero_credits",
        "required_hours",
        "hours_per_completion",
        "semesters_required",
        "max_completions_per_term",
        "distinct_term_required",
        "distinct_activity_required",
        "title_base",
        "evidence_state",
        "coverage_state",
        "automatic_decision",
        "scope_state",
        "applicability_basis",
        "waiver_allowed",
        "waiver",
        "waiver_evidence_required",
        "amount_semantics",
        "membership_id",
        "official_membership_id",
        "minimum_credits",
        "maximum_credits",
        "max_credits",
        "min_earned_credits_per_completion",
        "minimum_earned_credits_per_completion",
        "term_bound",
        "membership_version_required",
        "membership_program_required",
        "membership_track_required",
        "eligible_course_ids",
        "course_ids",
        "official_course_ids",
        "official_course_id",
        "eligible_terms",
        "terms",
        "effective_terms",
        "excluded_membership_id",
        "observed_requirement_ids",
        "observed_requirements",
        "affects_credit_ledger",
        "source_reference",
        "source_url",
        "source_file",
        "research_file",
        "pdf_page",
        "printed_page",
        "original_clause",
        "manual_reason",
        "policy_id",
        "policy_revision",
        "revision",
        "version",
        "curriculum_version",
        "program",
        "program_slug",
        "track",
        "track_slug",
        "waiver_requirement_version",
        "waiver_program_slug",
        "waiver_track_slug",
        "activity_membership_prefix",
        "role",
        "effective_term",
        "effective_start",
        "effective_end",
        "effective_interval",
        "applicability_state",
        "source_assertion_id",
        "assertion_id",
        "policy_source_reference",
    )
    sequence_keys = (
        "pool_ids",
        "eligible_pool_ids",
        "overflow_routes",
        "eligible_course_names",
        "eligible_course_ids",
        "course_ids",
        "official_course_ids",
        "eligible_terms",
        "terms",
        "effective_terms",
        "completion_statuses",
        "excluded_membership_ids",
        "waiver_authority_ids",
    )
    result: dict[str, Any] = {}
    for key in scalar_keys:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool, Decimal)) and not isinstance(item, (bytes, bytearray)):
            result[key] = str(item) if isinstance(item, Decimal) else item
    for key in sequence_keys:
        item = value.get(key)
        if isinstance(item, str):
            item = (item,)
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            result[key] = tuple(_text(part) for part in item if _text(part))
    for key in ("match", "official_activity_aliases", "applicability_source", "policy_source", "effective_interval", "applies_to", "predicate", "subset_constraints"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            result[key] = _safe_nested_non_credit(nested)
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
            result[key] = tuple(
                _safe_nested_non_credit(item)
                for item in nested
                if isinstance(item, Mapping)
            )
    provenance = value.get("provenance")
    if isinstance(provenance, Mapping):
        result["provenance"] = _safe_provenance(provenance, requirement_id=_text(value.get("requirement_id") or value.get("id")), scope="non_credit")
    return result


def _safe_course_pools(value: Any) -> tuple[dict[str, Any], ...]:
    """Project registry course pools without treating candidates as rules."""

    if isinstance(value, Mapping):
        entries = tuple(item for item in value.values() if isinstance(item, Mapping))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        entries = tuple(item for item in value if isinstance(item, Mapping))
    else:
        return ()
    result: list[dict[str, Any]] = []
    scalar_keys = (
        "id",
        "pool_id",
        "name",
        "year",
        "program",
        "program_slug",
        "scope",
        "bucket",
        "pool_name",
        "required_credits",
        "minimum_course_count",
        "maximum_course_count",
        "count_unique_attempts",
        "exclusive_only",
        "exclude_free_and_unallocated",
        "excluded_from_free",
        "overflow_to_free",
        "overflow_to_free_elective",
        "selection_rule",
        "evidence_state",
        "coverage_state",
        "automatic_decision",
        "scope_state",
        "source_reference",
        "source_url",
        "identity_fields",
        "allowed_components",
        "policy_id",
        "policy_revision",
        "revision",
        "policy_source",
        "candidate_only",
        "eligible_pool_ids",
        "overflow_routes",
        "membership_ids",
    )
    candidate_keys = ("candidate_courses", "candidates", "exact", "course_candidates")
    for entry in entries:
        safe: dict[str, Any] = {}
        for key in scalar_keys:
            item = entry.get(key)
            if isinstance(item, (str, int, float, bool, Decimal)):
                safe[key] = str(item) if isinstance(item, Decimal) else item
            elif key in {"identity_fields", "allowed_components", "eligible_pool_ids", "overflow_routes", "membership_ids"} and isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                safe[key] = tuple(_text(value) for value in item if _text(value))
        pool_evidence = _safe_pool_membership_evidence(
            entry.get("pool_membership_evidence")
            or entry.get("membership_evidence")
        )
        if pool_evidence:
            safe["pool_membership_evidence"] = pool_evidence
        candidates: list[dict[str, Any]] = []
        raw_candidates = next((entry.get(key) for key in candidate_keys if entry.get(key) is not None), ())
        if isinstance(raw_candidates, Mapping):
            raw_candidates = tuple(raw_candidates.values())
        if isinstance(raw_candidates, Sequence) and not isinstance(raw_candidates, (str, bytes, bytearray)):
            for candidate in raw_candidates:
                if not isinstance(candidate, Mapping):
                    continue
                candidate_metadata = candidate.get("course_metadata")
                candidate_metadata = candidate_metadata if isinstance(candidate_metadata, Mapping) else {}
                projection: dict[str, Any] = {}
                for key in (
                    "id",
                    "course_id",
                    "course_code",
                    "official_course_identity",
                    "name",
                    "course_name",
                    "credits",
                    "component",
                    "component_type",
                    "department",
                    "dept",
                    "section",
                    "term",
                    "academic_term",
                    "semester",
                    "curriculum_version",
                    "version",
                        "evidence_state",
                        "membership_ids",
                        "pool_memberships",
                        "subset_ids",
                        "college",
                    "college_slug",
                    "category",
                    "official_category",
                    "membership_source_reference",
                    "department_unit",
                    "official_department_unit",
                    "course_department_unit",
                    "offering_department_unit",
                    "department_membership",
                    "offering_program_slug",
                    "course_program_slug",
                    "course_owner_program",
                    "owner_program_slug",
                    "department_program_slug",
                    "offering_department",
                    "secondary_course_key",
                    "candidate_key",
                    "course_key",
                    "math_secondary_composite_required",
                    "program_approved_membership_ids",
                    "approved_membership_ids",
                    "program_approved_exemption_memberships",
                    "program_approved_applicable_terms",
                    "approved_applicable_terms",
                    "official_required",
                    "recognition_basis",
                    "excluded_from_free",
                    "overflow_to_free",
                    "overflow_to_free_elective",
                ):
                    item = candidate.get(key)
                    if item in (None, "") and key in {
                        "subset_ids",
                        "excluded_from_free",
                        "overflow_to_free",
                        "overflow_to_free_elective",
                    }:
                        item = candidate_metadata.get("subset_ids")
                        if key != "subset_ids":
                            item = candidate_metadata.get(key)
                    if isinstance(item, (str, int, float, bool, Decimal)):
                        projection[key] = str(item) if isinstance(item, Decimal) else item
                    elif key in {
                        "membership_ids",
                        "pool_memberships",
                        "subset_ids",
                        "program_approved_membership_ids",
                        "approved_membership_ids",
                        "program_approved_exemption_memberships",
                        "program_approved_applicable_terms",
                        "approved_applicable_terms",
                    } and isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                        projection[key] = tuple(_text(part) for part in item if _text(part))
                candidate_source = _text(
                    candidate.get("source_reference")
                    or candidate.get("membership_source_reference")
                    or entry.get("source_reference")
                )
                candidate_evidence = _safe_pool_membership_evidence(
                    candidate.get("pool_membership_evidence")
                    or candidate.get("membership_evidence")
                )
                candidate_evidence = tuple(
                    dict.fromkeys(
                        (
                            *candidate_evidence,
                            *_safe_membership_assertions(
                                candidate.get("membership_assertions")
                                or candidate_metadata.get("membership_assertions"),
                                default_source=candidate_source,
                            ),
                        )
                    )
                )
                if candidate_evidence:
                    projection["pool_membership_evidence"] = candidate_evidence
                if projection:
                    candidates.append(projection)
        safe["candidate_courses"] = tuple(candidates)
        policy = _safe_policy(entry.get("policy"))
        if policy:
            safe["policy"] = policy
        if safe:
            result.append(safe)
    return tuple(sorted(result, key=lambda item: (_text(item.get("pool_id") or item.get("id")), repr(item))))


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    """Privacy-safe, confirmed input for one deterministic evaluation.

    The dataclass intentionally has no fields for names, student numbers,
    passwords, cookies, PDF bytes, or evidence blobs.  ``from_mapping`` is a
    convenience adapter that only reads this allowlisted schema.
    """

    admission_cohort: str = ""
    primary_curriculum_id: str = ""
    confirmed_course_rows: tuple[NormalizedCourseRow, ...] = ()
    confirmed_course_fingerprint: str = ""
    transcript_confirmed: bool = False
    confirmation_state: str = ""
    # Compatibility spellings for adapters that already expose a confirmation
    # object.  They are normalized into the canonical fields below and are
    # never emitted as separate persistence fields.
    confirmed_rows: tuple[NormalizedCourseRow, ...] = ()
    confirmed_fingerprint: str = ""
    course_confirmation: CourseConfirmation | None = None
    input_confirmation_state: str | None = None
    program_type: str = "單主修"
    # Explicit secondary role; ``program_type`` remains a compatibility
    # spelling for existing single/double-major callers.
    secondary_kind: str | None = None
    target_curriculum_id: str | None = None
    target_curriculum_version_candidate: str | None = None
    # ``target_curriculum_version`` is retained as an explicit alias because
    # older callers use that wording.  It is never inferred from cohort.
    target_curriculum_version: str | None = None
    target_curriculum_version_id: str | None = None
    target_curriculum_year: str | int | None = None
    target_program: str | None = None
    target_track: str | None = None
    application_year: str | int | None = None
    application_semester: str | int | None = None
    application_term: str | None = None
    submitted_at: str | None = None
    # Opaque official application-record handle.  It is carried alongside
    # ``submitted_at`` so a date never becomes evidence by itself.
    application_event_evidence_id: str | None = None
    application_status: str | None = None
    school_approval_status: str | None = None
    formal_qualification_status: str | None = None
    formal_qualification_state: str | None = None
    formal_award_status: str | None = None
    # Opaque, session-scoped subject binding used only by trusted resolvers.
    # It is intentionally not emitted in ``as_dict`` or the DecisionSnapshot.
    subject_ref: str | None = field(default=None, repr=False)
    target_curriculum_evidence_id: str | None = None
    target_version_evidence_id: str | None = None
    rule_applicability_evidence_id: str | None = None
    rule_evidence_id: str | None = None
    department_decision_evidence_id: str | None = None
    school_approval_evidence_id: str | None = None
    registrar_registration_evidence_id: str | None = None
    registrar_evidence_id: str | None = None
    formal_qualification_evidence_id: str | None = None
    formal_award_evidence_id: str | None = None
    equivalency_evidence_ids: tuple[str, ...] = ()
    equivalency_binding_ids: tuple[str, ...] = ()
    official_evidence_ids: tuple[str, ...] = ()
    notice_evidence_ids: tuple[str, ...] = ()
    as_of: str | None = None
    search_limit: int = 10000
    # Only fixed, non-sensitive warning codes may cross the UI -> service
    # boundary.  Account fingerprints, scopes, and raw portal data never do.
    input_warning_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        rows = self.confirmed_course_rows
        confirmation = self.course_confirmation if isinstance(self.course_confirmation, CourseConfirmation) else None
        if not rows and self.confirmed_rows:
            rows = self.confirmed_rows
        if isinstance(rows, CourseConfirmation):
            confirmation = rows
            rows = confirmation.rows
        if confirmation is not None and not rows:
            rows = confirmation.rows
        if confirmation is not None:
            if not self.confirmed_course_fingerprint:
                object.__setattr__(self, "confirmed_course_fingerprint", confirmation.fingerprint)
            if not self.confirmation_state and not self.input_confirmation_state:
                object.__setattr__(self, "confirmation_state", confirmation.state.value)
        if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
            raise TypeError("confirmed_course_rows must contain NormalizedCourseRow values")
        if not all(isinstance(row, NormalizedCourseRow) for row in rows):
            raise TypeError("confirmed_course_rows must contain NormalizedCourseRow values")
        ordered_rows = tuple(sorted(rows, key=lambda row: tuple(_text(row.as_dict().get(key)) for key in sorted(row.as_dict()))))
        object.__setattr__(self, "confirmed_course_rows", ordered_rows)
        object.__setattr__(self, "confirmed_rows", ordered_rows)
        object.__setattr__(self, "admission_cohort", _text(self.admission_cohort))
        object.__setattr__(self, "primary_curriculum_id", _text(self.primary_curriculum_id))
        object.__setattr__(self, "program_type", _text(self.program_type) or "單主修")
        object.__setattr__(self, "secondary_kind", _secondary_kind(self.secondary_kind, self.program_type))
        fingerprint = _text(self.confirmed_course_fingerprint) or _text(self.confirmed_fingerprint)
        object.__setattr__(self, "confirmed_course_fingerprint", fingerprint)
        object.__setattr__(self, "confirmed_fingerprint", fingerprint)
        object.__setattr__(self, "transcript_confirmed", bool(self.transcript_confirmed))
        state = _text(self.confirmation_state or self.input_confirmation_state).upper() or ("CONFIRMED" if self.transcript_confirmed else "UNCONFIRMED")
        if state not in {item.value for item in ConfirmationState}:
            state = "UNCONFIRMED"
        if not self.transcript_confirmed:
            state = "UNCONFIRMED" if state == "CONFIRMED" else state
        # If an adapter supplied a CourseConfirmation object, all canonical
        # row/fingerprint/state fields must describe that exact object.  A
        # mismatch is stale input, never a second opportunity to assert
        # confirmation via the request booleans.
        confirmation_mismatch = False
        if confirmation is not None:
            current_rows_fingerprint = fingerprint_course_rows(ordered_rows)
            supplied_fingerprint = _text(self.confirmed_course_fingerprint)
            if confirmation.fingerprint != current_rows_fingerprint:
                confirmation_mismatch = True
            if supplied_fingerprint and supplied_fingerprint != confirmation.fingerprint:
                confirmation_mismatch = True
            if self.transcript_confirmed != (confirmation.state is ConfirmationState.CONFIRMED):
                confirmation_mismatch = True
            if confirmation_mismatch:
                object.__setattr__(self, "transcript_confirmed", False)
                state = "STALE"
        object.__setattr__(self, "confirmation_state", state)
        for key in (
            "target_curriculum_id",
            "target_curriculum_version_candidate",
            "target_curriculum_version",
            "target_program",
            "target_track",
            "target_curriculum_year",
            "application_term",
            "submitted_at",
            "application_status",
            "school_approval_status",
            "formal_qualification_status",
            "formal_award_status",
            "as_of",
            "subject_ref",
        ):
            value = getattr(self, key)
            object.__setattr__(self, key, _text(value) or None)
        for key in ("application_year", "application_semester"):
            value = getattr(self, key)
            object.__setattr__(self, key, _text(value) or None)
        for key in _EVIDENCE_FIELDS:
            value = getattr(self, key)
            object.__setattr__(self, key, _text(value) or None)
        aliases = {
            "target_curriculum_id": self.target_curriculum_version_id,
            "target_curriculum_evidence_id": self.target_version_evidence_id,
            "rule_applicability_evidence_id": self.rule_evidence_id,
            "department_decision_evidence_id": self.school_approval_evidence_id,
            "registrar_registration_evidence_id": self.registrar_evidence_id,
            "formal_qualification_status": self.formal_qualification_state,
        }
        for canonical, alias in aliases.items():
            if not getattr(self, canonical) and alias:
                object.__setattr__(self, canonical, _text(alias) or None)
        object.__setattr__(self, "target_curriculum_version_id", self.target_curriculum_id)
        object.__setattr__(self, "target_version_evidence_id", self.target_curriculum_evidence_id)
        object.__setattr__(self, "rule_evidence_id", self.rule_applicability_evidence_id)
        object.__setattr__(self, "school_approval_evidence_id", self.department_decision_evidence_id)
        object.__setattr__(self, "registrar_evidence_id", self.registrar_registration_evidence_id)
        object.__setattr__(self, "formal_qualification_state", self.formal_qualification_status)
        equivalency_ids = (*self.equivalency_evidence_ids, *self.equivalency_binding_ids)
        object.__setattr__(self, "equivalency_evidence_ids", _unique_text(equivalency_ids))
        object.__setattr__(self, "equivalency_binding_ids", self.equivalency_evidence_ids)
        object.__setattr__(self, "official_evidence_ids", _unique_text(self.official_evidence_ids))
        object.__setattr__(self, "notice_evidence_ids", _unique_text(self.notice_evidence_ids))
        warning_codes = []
        raw_warning_codes = self.input_warning_codes
        if isinstance(raw_warning_codes, Sequence) and not isinstance(raw_warning_codes, (str, bytes, bytearray)):
            for item in raw_warning_codes:
                code = _text(item)
                if code in INPUT_WARNING_CODES and code not in warning_codes:
                    warning_codes.append(code)
        object.__setattr__(self, "input_warning_codes", tuple(warning_codes))
        try:
            limit = int(self.search_limit)
        except (TypeError, ValueError, OverflowError):
            limit = 10000
        object.__setattr__(self, "search_limit", max(1, limit))

    @property
    def target_version(self) -> str | None:
        return self.target_curriculum_id or self.target_curriculum_version_candidate or self.target_curriculum_version

    @property
    def resolved_application_term(self) -> str | None:
        if self.application_term:
            return self.application_term
        if self.application_year and self.application_semester:
            return f"{self.application_year}-{self.application_semester}"
        return None

    @classmethod
    def from_confirmation(
        cls,
        confirmation: CourseConfirmation,
        *,
        admission_cohort: str,
        primary_curriculum_id: str,
        **kwargs: Any,
    ) -> EvaluationRequest:
        if not isinstance(confirmation, CourseConfirmation):
            raise TypeError("confirmation must be a CourseConfirmation")
        return cls(
            admission_cohort=admission_cohort,
            primary_curriculum_id=primary_curriculum_id,
            confirmed_course_rows=confirmation.rows,
            confirmed_course_fingerprint=confirmation.confirmed_fingerprint or confirmation.fingerprint,
            transcript_confirmed=confirmation.state is ConfirmationState.CONFIRMED,
            confirmation_state=confirmation.state.value,
            **kwargs,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EvaluationRequest:
        if not isinstance(value, Mapping):
            raise TypeError("evaluation request must be a mapping")
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        payload = {key: value[key] for key in allowed if key in value}
        if "target_curriculum_version_candidate" not in payload and "target_curriculum_version" in payload:
            payload["target_curriculum_version_candidate"] = payload["target_curriculum_version"]
        return cls(**payload)

    def as_dict(self) -> Mapping[str, Any]:
        rows = tuple(row.as_dict() for row in self.confirmed_course_rows)
        payload: dict[str, Any] = {
            "admission_cohort": self.admission_cohort,
            "primary_curriculum_id": self.primary_curriculum_id,
            "confirmed_course_rows": rows,
            "confirmed_course_fingerprint": self.confirmed_course_fingerprint,
            "transcript_confirmed": self.transcript_confirmed,
            "confirmation_state": self.confirmation_state,
            "program_type": self.program_type,
            "secondary_kind": self.secondary_kind,
            "target_curriculum_id": self.target_curriculum_id,
            "target_curriculum_version_candidate": self.target_curriculum_version_candidate,
            "target_curriculum_version": self.target_curriculum_version,
            "target_curriculum_year": self.target_curriculum_year,
            "target_program": self.target_program,
            "target_track": self.target_track,
            "application_year": self.application_year,
            "application_semester": self.application_semester,
            "application_term": self.resolved_application_term,
            "submitted_at": self.submitted_at,
            "application_status": self.application_status,
            "school_approval_status": self.school_approval_status,
            "formal_qualification_status": self.formal_qualification_status,
            "formal_award_status": self.formal_award_status,
            "input_warning_codes": self.input_warning_codes,
            "equivalency_evidence_ids": self.equivalency_evidence_ids,
            "official_evidence_ids": self.official_evidence_ids,
            "notice_evidence_ids": self.notice_evidence_ids,
            "as_of": self.as_of,
            "search_limit": self.search_limit,
        }
        for key in _EVIDENCE_FIELDS:
            payload[key] = getattr(self, key)
        return _freeze(payload)


def _released_rows(request: EvaluationRequest) -> tuple[NormalizedCourseRow, ...]:
    """Release exactly the current confirmed fingerprint, never raw input."""

    if not request.transcript_confirmed or request.confirmation_state != ConfirmationState.CONFIRMED.value:
        return ()
    if not request.confirmed_course_fingerprint:
        return ()
    if fingerprint_course_rows(request.confirmed_course_rows) != request.confirmed_course_fingerprint:
        return ()
    confirmation = request.course_confirmation
    if confirmation is None:
        confirmation = CourseConfirmation(
            rows=request.confirmed_course_rows,
            fingerprint=request.confirmed_course_fingerprint,
            state=ConfirmationState.CONFIRMED,
            confirmed_fingerprint=request.confirmed_course_fingerprint,
        )
    return release_formal_attempts(confirmation, request.confirmed_course_fingerprint)


def _context_request(request: EvaluationRequest, primary: Mapping[str, Any] | None, target: Mapping[str, Any] | None) -> dict[str, Any]:
    primary_program = _text((primary or {}).get("program_slug"))
    primary_track = _text((primary or {}).get("track_slug"))
    target_program = _text(request.target_program) or _text((target or {}).get("program_slug"))
    target_track = _text(request.target_track) or _text((target or {}).get("track_slug"))
    result: dict[str, Any] = {
        "admission_cohort": request.admission_cohort,
        "primary_curriculum_id": request.primary_curriculum_id,
        "primary_program": primary_program,
        "primary_track": primary_track,
        "program_type": request.program_type,
        "secondary_kind": request.secondary_kind,
        "target_program": target_program,
        "target_track": target_track,
        "application_term": request.resolved_application_term,
        "application_year": request.application_year,
        "application_semester": request.application_semester,
    }
    effective_target_version = request.target_version or _text((target or {}).get("curriculum_id"))
    if effective_target_version:
        result["target_curriculum_version"] = effective_target_version
    if request.target_curriculum_year:
        result["target_curriculum_year"] = request.target_curriculum_year
    if request.target_curriculum_evidence_id:
        result["target_version_evidence_reference"] = request.target_curriculum_evidence_id
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_rule_resolution(result: Mapping[str, Any]) -> dict[str, Any]:
    def dimension(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"status": UNKNOWN, "state": UNKNOWN, "reason": "規則維度無法解析。"}
        safe: dict[str, Any] = {}
        for key in ("status", "state", "value", "reason", "evidence_reference", "evidence_state", "coverage_state", "curriculum_id"):
            item = value.get(key)
            if isinstance(item, (str, int, float, bool, Decimal)) or item is None:
                safe[key] = str(item) if isinstance(item, Decimal) else item
        curriculum = _safe_curriculum(value.get("curriculum"))
        if curriculum is not None:
            safe["curriculum"] = curriculum
        return safe

    safe = {
        key: result.get(key)
        for key in (
            "status",
            "state",
            "resolved",
            "can_pass",
            "secondary_kind",
            "target_role",
            "blocker",
            "admission_cohort",
            "application_term",
            "target_program",
            "target_track",
            "primary_curriculum_id",
            "target_curriculum_id",
        )
        if isinstance(result.get(key), (str, int, float, bool)) or result.get(key) is None
    }
    safe["blocker_codes"] = tuple(_text(item) for item in result.get("blocker_codes", ()) if _text(item))
    safe["warnings"] = tuple(_text(item) for item in result.get("warnings", ()) if _text(item))
    dimensions = result.get("dimensions")
    safe["dimensions"] = {
        _text(key): dimension(item)
        for key, item in dimensions.items()
        if _text(key)
    } if isinstance(dimensions, Mapping) else {}
    safe["blockers"] = tuple(
        _safe_blocker(item)
        for item in result.get("blockers", ())
        if isinstance(item, Mapping)
    )
    safe["primary_curriculum"] = _safe_curriculum(result.get("primary_curriculum"))
    safe["target_curriculum"] = _safe_curriculum(result.get("target_curriculum"))
    return safe


def _safe_blocker(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value.get(key)
        for key in ("code", "dimension", "reason", "curriculum_id")
        if isinstance(value.get(key), (str, int, float, bool)) or value.get(key) is None
    }


def _safe_gate(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": UNKNOWN, "state": UNKNOWN, "reason": "官方申請證據無法解析。"}
    result: dict[str, Any] = {}
    for key in (
        "status",
        "state",
        "code",
        "reason",
        "value",
        "award_state",
        "qualification_state",
        "is_official",
        "effective_term",
        "curriculum_revision",
        "effective_interval",
        "application_event_evidence_id",
        "record_type",
    ):
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
    result["evidence_ids"] = tuple(_text(item) for item in value.get("evidence_ids", ()) if _text(item))
    result["provenance"] = tuple(_safe_provenance(item) for item in value.get("provenance", ()) if isinstance(item, Mapping))
    return result


def _safe_application(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"status": UNKNOWN, "state": UNKNOWN, "can_pass": False}
    safe = {key: result.get(key) for key in ("status", "state", "can_pass", "graduation_ready", "application_term", "target_program", "target_track", "submitted_at", "application_event_evidence_id", "as_of") if isinstance(result.get(key), (str, int, float, bool)) or result.get(key) is None}
    for key in (
        "notice",
        "university_window",
        "department_window",
        "submission",
        "rule_version",
        "rule_applicability",
        "department_decision",
        "department",
        "registrar_registration",
        "registration",
        "activity",
        "formal_qualification",
        "formal_award",
        "application_event",
    ):
        if key in result:
            safe[key] = _safe_gate(result[key])
    safe["gates"] = {str(key): _text(value) for key, value in result.get("gates", {}).items()} if isinstance(result.get("gates"), Mapping) else {}
    safe["blockers"] = tuple(_safe_blocker(item) for item in result.get("blockers", ()) if isinstance(item, Mapping))
    safe["evidence_ids"] = tuple(_text(item) for item in result.get("evidence_ids", ()) if _text(item))
    safe["provenance"] = tuple(_safe_provenance(item) for item in result.get("provenance", ()) if isinstance(item, Mapping))
    # Self-reported status is useful to explain a pending gate, but only the
    # resolver's official statuses above participate in a verdict.
    self_reported = result.get("self_reported")
    if isinstance(self_reported, Mapping):
        safe["self_reported"] = {
            key: item
            for key, item in self_reported.items()
            if key not in {"student_id", "subject_id", "subject_ref"} and isinstance(item, (str, int, float, bool))
        }
    return safe


def _curriculum_rows(record: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    rows = record.get("course_catalog", ()) if isinstance(record, Mapping) else ()
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in rows if isinstance(item, Mapping))


def _course_pool_entries(record: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Read registry pools as candidate identity data only."""

    if not isinstance(record, Mapping):
        return ()
    raw = record.get("course_pools", record.get("pools", ()))
    if isinstance(raw, Mapping):
        values = tuple(item for item in raw.values() if isinstance(item, Mapping))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        values = tuple(item for item in raw if isinstance(item, Mapping))
    else:
        return ()
    return values


_POLICY_SELECTION_RULES = frozenset({"official_category_policy", "official_open_elective_policy"})
_SUBSET_POLICY_RULES = frozenset({"official_department_elective_policy"})


def _policy_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    return tuple(dict.fromkeys(_text(item) for item in value if _text(item)))


def _policy_subset_constraints(policy: Mapping[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """Read explicit or active subset maxima from a server policy."""

    if not isinstance(policy, Mapping):
        return ()
    raw: list[Mapping[str, Any]] = []
    direct = policy.get("subset_constraints")
    if isinstance(direct, Mapping):
        direct = (direct,)
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes, bytearray)):
        raw.extend(item for item in direct if isinstance(item, Mapping))
    predicate = policy.get("predicate")
    if isinstance(predicate, Mapping):
        predicate_constraints = predicate.get("subset_constraints")
        if isinstance(predicate_constraints, Mapping):
            predicate_constraints = (predicate_constraints,)
        if isinstance(predicate_constraints, Sequence) and not isinstance(predicate_constraints, (str, bytes, bytearray)):
            raw.extend(item for item in predicate_constraints if isinstance(item, Mapping))
    constraints = list(_safe_subset_constraints(raw))

    def limit_key(value: Any) -> str:
        amount = _positive_number(value)
        return str(amount.normalize()) if amount > Decimal("0") else _text(value)

    seen = {
        (
            _text(item.get("membership_id") or item.get("subset_id") or item.get("pool_id")),
            _text(item.get("constraint_id") or item.get("id")),
            limit_key(item.get("maximum_credits") or item.get("max_credits") or item.get("maximum")),
        )
        for item in constraints
    }
    active_maxima = policy.get("active_subset_maxima")
    map_keys = ("active_subset_maxima",) if isinstance(active_maxima, Mapping) and active_maxima else ("subset_maxima",)
    for map_key in map_keys:
        maxima = policy.get(map_key)
        if not isinstance(maxima, Mapping):
            continue
        for membership_key, raw_limit in maxima.items():
            membership_id = _text(membership_key)
            if not membership_id:
                continue
            limits = raw_limit if isinstance(raw_limit, Mapping) else {"": raw_limit}
            for student_type, amount in limits.items():
                maximum = _positive_number(amount)
                if maximum <= Decimal("0"):
                    continue
                student_text = _text(student_type)
                constraint_id = f"{membership_id}:{student_text}:maximum" if student_text else f"{membership_id}:maximum"
                key = (membership_id, constraint_id, limit_key(maximum))
                if key in seen:
                    continue
                generated: dict[str, Any] = {
                    "constraint_id": constraint_id,
                    "membership_id": membership_id,
                    "subset_id": membership_id,
                    "maximum_credits": maximum,
                    "amount_semantics": "MAXIMUM",
                }
                if student_text:
                    generated["student_type"] = student_text
                constraints.append(generated)
                seen.add(key)
    return tuple(constraints)


def _policy_descriptor(record: Mapping[str, Any], pool: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate one registry policy before it can produce memberships."""

    selection_rule = _text(pool.get("selection_rule")).lower().replace("-", "_").replace(" ", "_")
    policy = pool.get("policy")
    subset_only = selection_rule in _SUBSET_POLICY_RULES
    if selection_rule not in _POLICY_SELECTION_RULES and not subset_only:
        return None
    if subset_only:
        policy = policy if isinstance(policy, Mapping) else {}
        constraints = _policy_subset_constraints(policy)
        pool_id = _text(pool.get("pool_id") or pool.get("id"))
        source_reference = _text(policy.get("source_reference") or pool.get("source_reference"))
        evidence_state = _evidence(policy.get("evidence_state") or pool.get("evidence_state"))
        coverage_state = _coverage(policy.get("coverage_state") or pool.get("coverage_state"))
        reasons: list[str] = []
        if not constraints:
            reasons.append("RULE_SUBSET_METADATA_MISSING")
        if evidence_state != VERIFIED or coverage_state != COMPLETE:
            reasons.append("RULE_POLICY_EVIDENCE_INCOMPLETE")
        if not source_reference:
            reasons.append("RULE_POLICY_SOURCE_MISSING")
        policy_source = policy.get("policy_source") if isinstance(policy.get("policy_source"), Mapping) else {}
        policy_source_reference = _text(
            policy.get("policy_source_reference")
            or policy_source.get("policy_source_reference")
            or policy_source.get("source_reference")
            or source_reference
        )
        return {
            "pool_id": pool_id,
            "selection_rule": selection_rule,
            "policy_state": VERIFIED if not reasons else UNKNOWN,
            "policy_reason": tuple(dict.fromkeys(reasons)),
            "policy_id": _text(policy.get("policy_id")),
            "revision": _text(policy.get("revision") or policy.get("policy_revision")),
            "predicate": {"subset_constraints": constraints},
            "subset_constraints": constraints,
            "source_reference": source_reference,
            "policy_source_reference": policy_source_reference,
            "source_url": _text(policy.get("source_url") or pool.get("source_url")),
            "policy": dict(policy),
            "evidence_state": evidence_state,
            "coverage_state": coverage_state,
            "automatic_decision": not reasons,
            "scope_state": VERIFIED if not reasons else UNKNOWN,
        }
    if not isinstance(policy, Mapping):
        return {
            "pool_id": _text(pool.get("pool_id") or pool.get("id")),
            "selection_rule": selection_rule,
            "policy_state": UNKNOWN,
            "policy_reason": "RULE_POLICY_METADATA_MISSING",
            "policy": {},
        }
    pool_id = _text(pool.get("pool_id") or pool.get("id"))
    predicate = policy.get("predicate")
    policy_subset_constraints = _policy_subset_constraints(policy)
    if policy_subset_constraints:
        predicate = dict(predicate) if isinstance(predicate, Mapping) else {}
        predicate["subset_constraints"] = policy_subset_constraints
    applies_to = policy.get("applies_to")
    version = _text(record.get("curriculum_version") or record.get("version") or record.get("admission_cohort"))
    program = _text(record.get("program_slug") or record.get("program"))
    track = _text(record.get("track_slug") or record.get("track")) or "department"
    role = _text(record.get("kind") or record.get("type"))
    scope_reasons: list[str] = []
    if not isinstance(applies_to, Mapping):
        scope_reasons.append("RULE_POLICY_SCOPE_UNVERIFIED")
    else:
        expected = (
            ("curriculum_versions", version),
            ("program_slugs", program),
            ("track_slugs", track),
            ("roles", role),
        )
        for key, wanted in expected:
            values = _policy_values(applies_to.get(key))
            if not values or wanted not in values:
                scope_reasons.append("RULE_POLICY_SCOPE_UNVERIFIED")
    evidence_state = _evidence(policy.get("evidence_state") or pool.get("evidence_state"))
    coverage_state = _coverage(policy.get("coverage_state") or pool.get("coverage_state"))
    automatic = policy.get("automatic_decision") is True
    if evidence_state != VERIFIED or coverage_state != COMPLETE or not automatic:
        scope_reasons.append("RULE_POLICY_EVIDENCE_INCOMPLETE")
    scope_state = _text(policy.get("scope_state")).upper()
    if scope_state and scope_state != VERIFIED:
        scope_reasons.append("RULE_POLICY_SCOPE_UNVERIFIED")
    if not _text(policy.get("policy_id")) or not _text(policy.get("revision")) or not isinstance(predicate, Mapping):
        scope_reasons.append("RULE_POLICY_METADATA_MISSING")
    source_reference = _text(policy.get("source_reference") or pool.get("source_reference"))
    policy_source = policy.get("policy_source") if isinstance(policy.get("policy_source"), Mapping) else {}
    policy_source_reference = _text(
        policy.get("policy_source_reference")
        or policy_source.get("policy_source_reference")
        or policy_source.get("source_reference")
    )
    if not source_reference:
        scope_reasons.append("RULE_POLICY_SOURCE_MISSING")
    policy_state = VERIFIED if not scope_reasons else UNKNOWN
    descriptor = {
        "pool_id": pool_id,
        "selection_rule": selection_rule,
        "policy_state": policy_state,
        "policy_reason": tuple(dict.fromkeys(scope_reasons)),
        "policy_id": _text(policy.get("policy_id")),
        "revision": _text(policy.get("revision") or policy.get("policy_revision")),
        "predicate": dict(predicate) if isinstance(predicate, Mapping) else {},
        "subset_constraints": policy_subset_constraints,
        "source_reference": source_reference,
        "policy_source_reference": policy_source_reference,
        "source_url": _text(policy.get("source_url") or pool.get("source_url")),
        "policy": dict(policy),
        "evidence_state": evidence_state,
        "coverage_state": coverage_state,
        "automatic_decision": automatic,
        "scope_state": scope_state or (VERIFIED if not scope_reasons else UNKNOWN),
    }
    return descriptor


def _policy_pool_descriptors(record: Mapping[str, Any] | None) -> tuple[dict[str, Any], ...]:
    if not isinstance(record, Mapping):
        return ()
    descriptors = [
        descriptor
        for pool in _course_pool_entries(record)
        if (descriptor := _policy_descriptor(record, pool)) is not None and descriptor.get("pool_id")
    ]
    return tuple(sorted(descriptors, key=lambda item: (_text(item.get("pool_id")), _text(item.get("policy_id")))))


def _course_pool_candidates(record: Mapping[str, Any] | None) -> tuple[dict[str, Any], ...]:
    """Flatten exact pool candidates without creating named requirements."""

    candidates: list[dict[str, Any]] = []
    for pool in _course_pool_entries(record):
        pool_id = _text(pool.get("pool_id") or pool.get("id") or pool.get("name"))
        if not pool_id:
            continue
        pool_evidence = _evidence(pool.get("evidence_state") or record.get("evidence_state"))
        identity_fields = pool.get("identity_fields", ())
        if isinstance(identity_fields, str):
            identity_fields = (identity_fields,)
        if not isinstance(identity_fields, Sequence):
            identity_fields = ()
        allowed_components = pool.get("allowed_components", ())
        if isinstance(allowed_components, str):
            allowed_components = (allowed_components,)
        if not isinstance(allowed_components, Sequence):
            allowed_components = ()
        normalized_allowed_components = {
            normalize_course_kind(item)
            for item in allowed_components
            if normalize_course_kind(item) != UNKNOWN
        }
        raw_candidates = next(
            (pool.get(key) for key in ("candidate_courses", "candidates", "exact", "course_candidates") if pool.get(key) is not None),
            (),
        )
        if isinstance(raw_candidates, Mapping):
            raw_candidates = tuple(raw_candidates.values())
        if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes, bytearray)):
            continue
        for index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, Mapping):
                continue
            course_metadata = candidate.get("course_metadata")
            course_metadata = course_metadata if isinstance(course_metadata, Mapping) else {}
            name = _text(candidate.get("course_name") or candidate.get("name") or candidate.get("title"))
            credits = _positive_number(candidate.get("credits", candidate.get("credit")))
            if not name or credits <= 0:
                continue
            identity = _text(
                candidate.get("official_course_identity")
                or candidate.get("course_id")
                or candidate.get("course_code")
                or candidate.get("id")
            )
            if not identity:
                identity = f"pool:{pool_id}:{_digest([name, str(credits), index])}"
            component = _text(candidate.get("component_type") or candidate.get("component") or candidate.get("lecture_or_lab"))
            normalized_component = normalize_course_kind(component)
            if normalized_allowed_components and normalized_component not in normalized_allowed_components:
                continue
            candidate_pool_ids = candidate.get("pool_ids", ())
            if isinstance(candidate_pool_ids, str):
                candidate_pool_ids = (candidate_pool_ids,)
            if not isinstance(candidate_pool_ids, Sequence):
                candidate_pool_ids = ()
            pool_ids = tuple(dict.fromkeys((_text(pool_id), *(_text(item) for item in candidate_pool_ids if _text(item)))))
            candidate_memberships = _policy_values(
                candidate.get("membership_ids")
                or candidate.get("pool_memberships")
                or pool.get("membership_ids")
                or pool.get("pool_memberships")
            )
            subset_memberships = _policy_values(
                candidate.get("subset_ids") or course_metadata.get("subset_ids")
            )
            candidate_memberships = tuple(dict.fromkeys((*candidate_memberships, *subset_memberships)))
            source_reference = _text(
                candidate.get("source_reference")
                or candidate.get("membership_source_reference")
                or course_metadata.get("source_reference")
                or course_metadata.get("membership_source_reference")
                or pool.get("source_reference")
            )
            candidate_pool_evidence = _safe_pool_membership_evidence(
                candidate.get("pool_membership_evidence")
                or candidate.get("membership_evidence")
            )
            candidate_pool_evidence = tuple(
                dict.fromkeys(
                    (
                        *candidate_pool_evidence,
                        *_safe_membership_assertions(
                            candidate.get("membership_assertions")
                            or course_metadata.get("membership_assertions"),
                            default_source=source_reference,
                        ),
                    )
                )
            )
            pool_membership_evidence = _safe_pool_membership_evidence(
                pool.get("pool_membership_evidence")
                or pool.get("membership_evidence")
            )
            if pool_membership_evidence:
                candidate_pool_evidence = tuple(
                    dict.fromkeys((*pool_membership_evidence, *candidate_pool_evidence))
                )
            if subset_memberships:
                subset_state = _evidence(candidate.get("evidence_state") or pool_evidence)
                subset_evidence = tuple(
                    (
                        membership_id,
                        subset_state,
                        source_reference,
                        "registry",
                    )
                    for membership_id in subset_memberships
                    if membership_id
                    and not any(existing[0] == membership_id for existing in candidate_pool_evidence)
                )
                candidate_pool_evidence = tuple(dict.fromkeys((*candidate_pool_evidence, *subset_evidence)))
            if not candidate_pool_evidence:
                candidate_pool_evidence = (
                    (
                        _text(pool_id),
                        _evidence(candidate.get("evidence_state") or pool_evidence),
                        source_reference,
                        "catalog",
                    ),
                )
            pool_policy = pool.get("policy") if isinstance(pool.get("policy"), Mapping) else {}
            candidate_excluded_from_free = (
                candidate.get("excluded_from_free") is True
                or course_metadata.get("excluded_from_free") is True
                or pool.get("excluded_from_free") is True
                or pool_policy.get("excluded_from_free") is True
            )
            candidate_overflow_to_free = (
                candidate.get("overflow_to_free") is True
                or course_metadata.get("overflow_to_free") is True
                or pool.get("overflow_to_free") is True
                or pool_policy.get("overflow_to_free") is True
            )
            candidate_overflow_to_free_elective = (
                candidate.get("overflow_to_free_elective") is True
                or course_metadata.get("overflow_to_free_elective") is True
                or pool.get("overflow_to_free_elective") is True
                or pool_policy.get("overflow_to_free_elective") is True
            )
            candidates.append(
                {
                    "requirement_id": "",
                    "scope": "pool_candidate",
                    "generic": False,
                    "course_name": name,
                    "course_id": identity,
                    "course_code": _text(candidate.get("course_code")),
                    "official_course_identity": identity,
                    "credits": credits,
                    "course_kind": component,
                    "component_type": component,
                    "department": _text(candidate.get("department") or candidate.get("dept")),
                    "section": _text(candidate.get("section") or candidate.get("track") or candidate.get("track_slug")),
                    "pool_ids": pool_ids,
                    "pool_evidence_state": _evidence(candidate.get("evidence_state") or pool_evidence),
                    "pool_membership_evidence": candidate_pool_evidence,
                    "membership_ids": candidate_memberships,
                    "membership_source_reference": _text(candidate.get("membership_source_reference") or source_reference),
                    "college": _text(candidate.get("college") or candidate.get("college_slug")),
                    "category": _text(candidate.get("official_category") or candidate.get("category")),
                    "department_unit": _text(candidate.get("department_unit") or course_metadata.get("department_unit")),
                    "official_department_unit": _text(candidate.get("official_department_unit") or course_metadata.get("official_department_unit")),
                    "course_department_unit": _text(candidate.get("course_department_unit") or course_metadata.get("course_department_unit")),
                    "offering_department_unit": _text(candidate.get("offering_department_unit") or course_metadata.get("offering_department_unit")),
                    "department_membership": _text(candidate.get("department_membership") or course_metadata.get("department_membership")),
                    "offering_program_slug": _text(candidate.get("offering_program_slug") or course_metadata.get("offering_program_slug")),
                    "course_program_slug": _text(candidate.get("course_program_slug") or course_metadata.get("course_program_slug")),
                    "course_owner_program": _text(candidate.get("course_owner_program") or course_metadata.get("course_owner_program")),
                    "owner_program_slug": _text(candidate.get("owner_program_slug") or course_metadata.get("owner_program_slug")),
                    "department_program_slug": _text(candidate.get("department_program_slug") or course_metadata.get("department_program_slug")),
                    "offering_department": _text(candidate.get("offering_department") or course_metadata.get("offering_department")),
                    "secondary_course_key": _text(candidate.get("secondary_course_key") or course_metadata.get("secondary_course_key")),
                    "candidate_key": _text(candidate.get("candidate_key") or course_metadata.get("candidate_key")),
                    "course_key": _text(candidate.get("course_key") or course_metadata.get("course_key")),
                    "math_secondary_composite_required": bool(candidate.get("math_secondary_composite_required") or course_metadata.get("math_secondary_composite_required")),
                    "program_approved_membership_ids": _policy_values(
                        candidate.get("program_approved_membership_ids")
                        or candidate.get("approved_membership_ids")
                        or candidate.get("program_approved_exemption_memberships")
                        or course_metadata.get("program_approved_membership_ids")
                    ),
                    "program_approved_applicable_terms": _policy_values(
                        candidate.get("program_approved_applicable_terms")
                        or candidate.get("approved_applicable_terms")
                        or course_metadata.get("program_approved_applicable_terms")
                    ),
                    "recognition_basis": _text(candidate.get("recognition_basis") or course_metadata.get("recognition_basis")),
                    "official_required": bool(candidate.get("official_required") or course_metadata.get("official_required")),
                    "excluded_from_free": candidate_excluded_from_free,
                    "overflow_to_free": candidate_overflow_to_free,
                    "overflow_to_free_elective": candidate_overflow_to_free_elective,
                    "pool_bucket": _text(pool.get("bucket") or pool.get("name")),
                    "pool_selection_rule": _text(pool.get("selection_rule")),
                    "term": _text(candidate.get("term") or candidate.get("academic_term") or candidate.get("semester")),
                    "curriculum_version": _text(
                        candidate.get("curriculum_version")
                        or candidate.get("version")
                        or pool.get("curriculum_version")
                        or pool.get("version")
                        or record.get("curriculum_version")
                        or record.get("version")
                    ),
                    "program_slug": _text(
                        candidate.get("program_slug")
                        or candidate.get("program")
                        or pool.get("program_slug")
                        or pool.get("program")
                        or record.get("program_slug")
                        or record.get("program")
                    ),
                    "track_slug": _text(
                        candidate.get("track_slug")
                        or candidate.get("track")
                        or pool.get("track_slug")
                        or pool.get("track")
                        or record.get("track_slug")
                        or record.get("track")
                    ),
                    "required_identity_dimensions": tuple(_text(item) for item in identity_fields if _text(item)),
                    "source_pool_id": pool_id,
                }
            )
    return tuple(candidates)


def _flatten_numeric_thresholds(value: Mapping[str, Any], prefix: str = "") -> Iterable[tuple[str, Decimal]]:
    for key in sorted(value):
        name = _text(key)
        if not name:
            continue
        full = f"{prefix}.{name}" if prefix else name
        item = value[key]
        if isinstance(item, Mapping):
            yield from _flatten_numeric_thresholds(item, full)
        elif isinstance(item, (int, float, str, Decimal)) and not isinstance(item, bool):
            amount = _positive_number(item)
            if amount > 0:
                yield full, amount


def _coverage(value: Any) -> str:
    text = _text(value).upper()
    return text if text in {COMPLETE, PARTIAL, NONE} else NONE


def _evidence(value: Any) -> str:
    text = _text(value).upper()
    return text if text in {VERIFIED, CONFLICTED, MISSING, MANUAL_REVIEW, NOT_MEMBER} else UNKNOWN


def _requirement_id(scope: str, curriculum_id: str, suffix: str) -> str:
    return f"{scope}:{curriculum_id}:{suffix}"


_EXECUTABLE_ROW_TYPES = frozenset(
    {
        "course",
        "named_course",
        "required_course",
        "required",
        "choice",
        "choice_course",
        "course_pool",
        "credit_quota",
        "quota",
        "aggregate",
        "credit_subset_gate",
        "credit_subset_requirement",
    }
)
_DERIVED_THRESHOLD_KEYS = frozenset({"total", "total_required", "graduation_total", "graduation_total_required"})


def _is_credit_subset_gate_row(row: Mapping[str, Any]) -> bool:
    """Return whether a registry row is a zero-consumption subset gate."""

    kind = _text(row.get("kind")).upper().replace("-", "_").replace(" ", "_")
    requirement_type = _text(row.get("requirement_type")).lower().replace("-", "_").replace(" ", "_")
    return kind in {"CREDIT_SUBSET_GATE", "CREDIT_SUBSET_REQUIREMENT"} or requirement_type in {
        "credit_subset_gate",
        "credit_subset_requirement",
    }


def _subset_gate_rows(record: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Read explicit subset gates from registry-owned requirement sections."""

    if not isinstance(record, Mapping):
        return ()
    sources: list[Any] = [record.get("subset_gate_requirements", ())]
    raw_non_credit = record.get("non_credit_requirements", ())
    if isinstance(raw_non_credit, Sequence) and not isinstance(raw_non_credit, (str, bytes, bytearray)):
        sources.append(raw_non_credit)
    catalog_ids = {
        _text(item.get("requirement_id") or item.get("id"))
        for item in _curriculum_rows(record)
        if _is_credit_subset_gate_row(item)
    }
    result: list[Mapping[str, Any]] = []
    seen = set(catalog_ids)
    for source in sources:
        if isinstance(source, Mapping):
            source = tuple(source.values())
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes, bytearray)):
            continue
        for item in source:
            if not isinstance(item, Mapping) or not _is_credit_subset_gate_row(item):
                continue
            identifier = _text(item.get("requirement_id") or item.get("id"))
            if identifier and identifier in seen:
                continue
            if identifier:
                seen.add(identifier)
            result.append(item)
    return tuple(result)


def _has_executable_row_semantics(row: Mapping[str, Any]) -> bool:
    """Require an explicit required/choice meaning before compiling a row."""

    requirement_type = _text(row.get("requirement_type")).lower().replace("-", "_").replace(" ", "_")
    if requirement_type in _EXECUTABLE_ROW_TYPES or _is_credit_subset_gate_row(row):
        return True
    # A checked-in source may use boolean/selection fields instead of the
    # normalized requirement_type.  A bucket or a name alone is intentionally
    # insufficient: CS department rows are candidate lists, not requirements.
    for key in (
        "required",
        "is_required",
        "required_course",
        "formal_requirement",
        "executable",
        "choice_rule",
        "selection_rule",
        "eligible_course_ids",
        "eligible_pool_ids",
        "accept_any",
    ):
        if row.get(key) not in (None, "", False, (), [], {}):
            return True
    return False


def _unresolved_threshold_quotas(
    record: Mapping[str, Any],
    *,
    scope: str,
    curriculum_id: str,
    record_coverage: str,
    record_evidence: str,
    existing_generic: bool,
) -> tuple[RequirementSpec, ...]:
    """Expose explicit aggregate claims without inventing a course pool.

    A threshold is only materialized when the registry has no explicit quota
    row for it.  It remains a non-allocatable UNKNOWN placeholder; derived
    total aliases are deliberately excluded.
    """

    if existing_generic or not isinstance(record.get("thresholds"), Mapping):
        return ()
    specs: list[RequirementSpec] = []
    seen_keys: set[str] = set()
    for key, raw_value in sorted(record["thresholds"].items(), key=lambda item: str(item[0])):
        key_text = _text(key).lower().replace("-", "_").replace(" ", "_")
        if not key_text or key_text in _DERIVED_THRESHOLD_KEYS:
            continue
        if key_text.endswith("_required") and key_text.removesuffix("_required") in {"base", "other"}:
            key_text = key_text.removesuffix("_required")
        if key_text in seen_keys:
            continue
        amount = _positive_number(raw_value)
        if amount <= 0 or isinstance(raw_value, Mapping):
            continue
        seen_keys.add(key_text)
        requirement_id = _requirement_id(scope, curriculum_id, f"quota:{key_text}")
        specs.append(
            RequirementSpec(
                requirement_id=requirement_id,
                name=f"未解析額度：{key_text}",
                credits_required=amount,
                eligible_course_ids=(),
                eligible_course_names=(),
                coverage_state=record_coverage,
                evidence_state=record_evidence,
                required=True,
                accept_any=False,
                bucket=key_text,
                kind="AGGREGATE",
                owner="PRIMARY" if scope == "primary" else "TARGET" if scope == "target" else _text(scope).upper(),
                domain=f"{scope}:{key_text}",
            )
        )
    return tuple(specs)


def _compile_requirements(
    record: Mapping[str, Any] | None,
    *,
    scope: str,
) -> tuple[tuple[RequirementSpec, ...], dict[str, dict[str, Any]], tuple[dict[str, Any], ...]]:
    """Compile executable leaf rows without flattening derived aggregates.

    Registry thresholds such as ``total`` and ``base`` remain available in
    the curriculum/provenance projections, but they are derived constraints,
    not independent consumers of transcript credits.  Only an exact named
    catalog row or an explicitly named incomplete quota becomes a requirement
    spec; the latter has no course pool and is deliberately UNKNOWN.
    """

    if not isinstance(record, Mapping):
        return (), {}, ()
    curriculum_id = _text(record.get("curriculum_id"))
    record_coverage = _coverage(record.get("coverage_state"))
    record_evidence = _evidence(record.get("evidence_state"))
    specs: list[RequirementSpec] = []
    metadata: dict[str, dict[str, Any]] = {}
    provenance: list[dict[str, Any]] = []
    seen: set[str] = set()
    overflow_to_free_sources: set[str] = set()
    # Server rows may use their compact source id in subset observations,
    # while RequirementSpec uses the scoped compiler id.  Build this map as
    # rows are compiled and resolve it only after all rows are known; ids that
    # are not present remain verbatim so the allocator fails closed.
    observed_requirement_aliases: dict[str, str] = {}
    pool_candidates = _course_pool_candidates(record)
    record_program = _text(record.get("program_slug") or record.get("program")).casefold()
    math_secondary_scope = (
        scope in {"target", "minor"}
        and record_program in {"math", "數學"}
    )
    policy_descriptors = _policy_pool_descriptors(record)
    policy_by_pool = {
        _text(item.get("pool_id")): item
        for item in policy_descriptors
        if _text(item.get("pool_id"))
    }
    pool_by_id = {
        _text(item.get("pool_id") or item.get("id")): item
        for item in _course_pool_entries(record)
        if _text(item.get("pool_id") or item.get("id"))
    }

    def server_flag(value: Mapping[str, Any] | None, key: str) -> bool:
        if not isinstance(value, Mapping):
            return False
        if value.get(key) is True:
            return True
        nested = value.get("policy")
        return isinstance(nested, Mapping) and nested.get(key) is True

    def ordered_texts(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            return ()
        return tuple(dict.fromkeys(_text(item) for item in value if _text(item)))

    def matching_pool_candidates(name: str, credits: Decimal, kind: str, department: str, section: str) -> tuple[Mapping[str, Any], ...]:
        matches: list[Mapping[str, Any]] = []
        for candidate in pool_candidates:
            if _course_label(candidate.get("course_name")) != _course_label(name):
                continue
            if _positive_number(candidate.get("credits")) != credits:
                continue
            candidate_kind = normalize_course_kind(candidate.get("course_kind") or candidate.get("component_type"))
            if kind and candidate_kind != normalize_course_kind(kind):
                continue
            if department and _text(candidate.get("department")) and _text(candidate.get("department")) != department:
                continue
            if section and _text(candidate.get("section")) and _text(candidate.get("section")) != section:
                continue
            matches.append(candidate)
        return tuple(matches)

    def enrich_policy_subset_constraints(
        constraints: Sequence[Mapping[str, Any]],
        *,
        requirement_id: str,
        source_reference: str,
    ) -> tuple[dict[str, Any], ...]:
        """Bind source policy observations to the compiled owner requirement."""

        enriched: list[dict[str, Any]] = []
        for raw_constraint in _safe_subset_constraints(constraints):
            constraint = dict(raw_constraint)
            membership_id = _text(
                constraint.get("membership_id")
                or constraint.get("subset_id")
                or constraint.get("pool_id")
            )
            if not membership_id:
                constraint_id = _text(constraint.get("constraint_id") or constraint.get("id"))
                if constraint_id:
                    membership_id = constraint_id.split(":", 1)[0]
            if membership_id:
                constraint["membership_id"] = membership_id
            # An explicit observation scope is authoritative registry data.
            # Keep each supplied id for the post-compile alias pass below;
            # only a constraint that omits the scope defaults to its owning
            # requirement.  Overwriting this list would erase legitimate
            # cross-requirement observations (and would turn an unknown
            # foreign id into a silently accepted owner alias).
            observed = constraint.get("observed_requirement_ids") or constraint.get("observed_requirements")
            if isinstance(observed, str):
                observed = (observed,)
            if not isinstance(observed, Sequence) or isinstance(observed, (bytes, bytearray)):
                observed = ()
            observed_ids = tuple(dict.fromkeys(_text(item) for item in observed if _text(item)))
            constraint["observed_requirement_ids"] = observed_ids or (requirement_id,)
            if source_reference and not _text(
                constraint.get("source_reference")
                or constraint.get("evidence_reference")
                or constraint.get("policy_source_reference")
            ):
                constraint["source_reference"] = source_reference
            enriched.append(constraint)
        return tuple(enriched)

    rows = (*_curriculum_rows(record), *_subset_gate_rows(record))
    for index, row in enumerate(rows):
        row_type = _text(row.get("requirement_type")).lower().replace("-", "_").replace(" ", "_")
        if row_type in {"conflict_candidate", "candidate_alias"}:
            # A conflict candidate is retained in the curriculum provenance,
            # but it is not a separate credit consumer.  The evaluation gate
            # below marks the overall minor UNKNOWN only when a transcript
            # actually uses that candidate.
            continue
        is_subset_gate = _is_credit_subset_gate_row(row)
        name = _text(row.get("name") or row.get("raw_title") or row.get("display_name"))
        credits = _positive_number(row.get("credits"))
        if not name and is_subset_gate:
            name = _text(row.get("requirement_id") or row.get("id"))
        if not name or (credits <= 0 and not is_subset_gate):
            continue
        if not _has_executable_row_semantics(row):
            # Preserve a planning candidate in provenance/registry output, but
            # never turn a bare department catalogue row into a required
            # deficit or a formal allocator consumer.
            continue
        raw_id = _text(row.get("requirement_id") or row.get("id"))
        suffix = raw_id or f"course:{_digest([name, str(credits), _text(row.get('bucket')), index])}"
        rid = _requirement_id(scope, curriculum_id, suffix)
        if rid in seen:
            continue
        seen.add(rid)
        for alias in (rid, raw_id, f"{scope}:{raw_id}" if raw_id else ""):
            alias_text = _text(alias)
            if alias_text:
                observed_requirement_aliases.setdefault(alias_text, rid)
        row_evidence = _evidence(row.get("evidence_state") or row.get("evidence") or record_evidence)
        row_coverage = _coverage(row.get("coverage_state") or record_coverage)
        raw_component = _text(row.get("component_type") or row.get("component") or row.get("lecture_or_lab"))
        normalized_component = normalize_course_kind(raw_component)
        # Registry aggregate/category rows commonly carry the literal
        # ``UNKNOWN`` marker because they have no lecture/lab restriction.
        # It must not become an exclusive UNKNOWN allowed kind: that would
        # reject every verified lecture attempt in a legal pool.  Explicit
        # lecture/lab values remain strict below.
        component = "" if normalized_component == UNKNOWN else normalized_component
        kind = component
        # ``credits`` is the executable minimum.  A registry-owned validated
        # maximum may allow the same exclusive pool to absorb additional
        # target credits (for example, Math secondary 3/8).  Only sanitized
        # server row fields are considered; request/course-row fields such as
        # ``credit_cap`` never reach this path.
        max_credits = credits
        for maximum_key in ("max_credits", "maximum_credits"):
            candidate_max = _number(row.get(maximum_key), Decimal("NaN"))
            if candidate_max.is_finite() and candidate_max >= credits:
                max_credits = candidate_max
                break
        generic = row_type in {"course_pool", "credit_quota", "quota", "aggregate"} or not _text(row.get("name"))
        if is_subset_gate:
            generic = False
        official_course_id = _text(
            row.get("course_code")
            or row.get("official_course_code")
            or row.get("official_course_identity")
            or row.get("course_id")
        )
        department = _text(row.get("department") or row.get("dept") or row.get("department_code"))
        section = _text(row.get("section") or row.get("track") or row.get("track_slug"))
        bucket = _text(row.get("bucket")) or scope
        owner = "PRIMARY" if scope == "primary" else "TARGET" if scope == "target" else _text(scope).upper()
        domain = _text(row.get("domain") or row.get("requirement_domain")) or f"{scope}:{bucket}"
        row_names = tuple(_text(item) for item in row.get("eligible_course_names", ()) if _text(item)) if isinstance(row.get("eligible_course_names"), Sequence) and not isinstance(row.get("eligible_course_names"), (str, bytes, bytearray)) else ()
        eligible_names = () if generic or is_subset_gate else row_names or (name,)
        accept_any = bool(row.get("accept_any")) if not generic and not is_subset_gate else False
        eligible_course_ids = (
            ()
            if generic or is_subset_gate or math_secondary_scope
            else ((official_course_id,) if official_course_id else ())
        )
        resolved_pool_candidate: Mapping[str, Any] | None = None
        if not generic:
            exact_pool_candidates = matching_pool_candidates(name, credits, kind, department, section)
            if official_course_id:
                exact_pool_candidates = tuple(
                    candidate
                    for candidate in exact_pool_candidates
                    if _text(candidate.get("course_id")) == official_course_id
                )
            if len(exact_pool_candidates) == 1:
                resolved_pool_candidate = exact_pool_candidates[0]
                if not eligible_course_ids and not math_secondary_scope:
                    eligible_course_ids = (_text(resolved_pool_candidate.get("course_id")),)
        catalog_course_id = official_course_id or (_text(eligible_course_ids[0]) if eligible_course_ids else "")
        eligible_pool_ids = () if is_subset_gate else ordered_texts(row.get("eligible_pool_ids"))
        overflow_routes = ordered_texts(row.get("overflow_routes"))
        row_pool_ids = () if is_subset_gate else ordered_texts(row.get("pool_ids"))
        applicable_policies = tuple(
            policy_by_pool[pool_id]
            for pool_id in eligible_pool_ids
            if pool_id in policy_by_pool
        )
        # Some adapters carry the policy directly on the quota row while
        # keeping the pool object minimal.  Preserve that contract only when
        # it names an approved policy rule; caller text never creates one.
        direct_policy = row.get("policy") if isinstance(row.get("policy"), Mapping) else None
        if direct_policy and not applicable_policies:
            direct_rule = _text(row.get("selection_rule") or direct_policy.get("selection_rule")).lower().replace("-", "_").replace(" ", "_")
            if direct_rule in _POLICY_SELECTION_RULES:
                applicable_policies = (
                    {
                        "pool_id": eligible_pool_ids[0] if eligible_pool_ids else "",
                        "selection_rule": direct_rule,
                        "policy_state": VERIFIED if direct_policy.get("automatic_decision") is True else UNKNOWN,
                        "policy_reason": (),
                        "policy_id": _text(direct_policy.get("policy_id")),
                        "revision": _text(direct_policy.get("revision") or direct_policy.get("policy_revision")),
                        "predicate": dict(direct_policy.get("predicate")) if isinstance(direct_policy.get("predicate"), Mapping) else {},
                        "source_reference": _text(direct_policy.get("source_reference")),
                        "source_url": _text(direct_policy.get("source_url")),
                        "policy": dict(direct_policy),
                        "evidence_state": _evidence(direct_policy.get("evidence_state")),
                        "coverage_state": _coverage(direct_policy.get("coverage_state")),
                        "automatic_decision": direct_policy.get("automatic_decision") is True,
                        "scope_state": _text(direct_policy.get("scope_state")) or UNKNOWN,
                    },
        )
        primary_policy = applicable_policies[0] if len(applicable_policies) == 1 else None
        pool_overflow_to_free = any(
            server_flag(pool_by_id.get(pool_id), "overflow_to_free")
            for pool_id in eligible_pool_ids
        )
        pool_overflow_to_free_elective = any(
            server_flag(pool_by_id.get(pool_id), "overflow_to_free_elective")
            for pool_id in eligible_pool_ids
        )
        policy_overflow_to_free = any(
            policy_meta.get("overflow_to_free") is True
            or server_flag(policy_meta.get("policy"), "overflow_to_free")
            for policy_meta in applicable_policies
        )
        policy_overflow_to_free_elective = any(
            policy_meta.get("overflow_to_free_elective") is True
            or server_flag(policy_meta.get("policy"), "overflow_to_free_elective")
            for policy_meta in applicable_policies
        )
        overflow_to_free_elective = (
            row.get("overflow_to_free_elective") is True
            or server_flag(direct_policy, "overflow_to_free_elective")
            or pool_overflow_to_free_elective
            or policy_overflow_to_free_elective
        )
        overflow_to_free = (
            row.get("overflow_to_free") is True
            or server_flag(direct_policy, "overflow_to_free")
            or pool_overflow_to_free
            or policy_overflow_to_free
            or overflow_to_free_elective
        )
        subset_constraints = ()
        raw_row_constraints = row.get("subset_constraints")
        if isinstance(raw_row_constraints, Mapping):
            raw_row_constraints = (raw_row_constraints,)
        if isinstance(raw_row_constraints, Sequence) and not isinstance(raw_row_constraints, (str, bytes, bytearray)):
            subset_constraints = tuple(item for item in raw_row_constraints if isinstance(item, Mapping))
        policy_subset_constraints: tuple[Mapping[str, Any], ...] = ()
        if primary_policy:
            policy_subset_constraints = _safe_subset_constraints(primary_policy.get("subset_constraints"))
            predicate = primary_policy.get("predicate")
            if not policy_subset_constraints and isinstance(predicate, Mapping):
                raw_constraints = predicate.get("subset_constraints", ())
                if isinstance(raw_constraints, Mapping):
                    raw_constraints = (raw_constraints,)
                if isinstance(raw_constraints, Sequence) and not isinstance(raw_constraints, (str, bytes, bytearray)):
                    policy_subset_constraints = tuple(item for item in raw_constraints if isinstance(item, Mapping))
            if not subset_constraints:
                subset_constraints = policy_subset_constraints
        policy_subset_source = _text(
            (primary_policy or {}).get("source_reference")
            or (primary_policy or {}).get("policy_source_reference")
            or row.get("source_reference")
            or record.get("source_reference")
        )
        if policy_subset_constraints or subset_constraints:
            subset_constraints = enrich_policy_subset_constraints(
                policy_subset_constraints or subset_constraints,
                requirement_id=rid,
                source_reference=policy_subset_source,
            )
        requirement_kind = (
            _text(row.get("kind") or row.get("requirement_type")).upper()
            if is_subset_gate
            else "AGGREGATE" if generic else _text(row.get("kind")) or "NAMED_COURSE"
        )
        applicability_state = _text(row.get("applicability_state") or row.get("scope_state")).upper()
        required = row.get("required", row.get("is_required", True)) is not False
        if applicability_state in {"NOT_APPLICABLE", "N/A", "NA"}:
            required = False
        spec = RequirementSpec(
            requirement_id=rid,
            name=name,
            credits_required=credits,
            max_credits=max_credits,
            eligible_course_ids=eligible_course_ids,
            eligible_pool_ids=eligible_pool_ids,
            overflow_routes=overflow_routes,
            eligible_course_names=eligible_names,
            allowed_course_kinds=(kind,) if kind else (),
            coverage_state=row_coverage,
            evidence_state=row_evidence,
            required=required,
            waiver=bool(row.get("waiver") or row.get("waiver_allowed")),
            accept_any=accept_any,
            bucket=bucket,
            kind=requirement_kind,
            owner=owner,
            domain=domain,
            curriculum_version=_text(record.get("curriculum_version") or record.get("version") or record.get("admission_cohort")),
            program_slug=_text(record.get("program_slug") or record.get("program")),
            track_slug=_text(record.get("track_slug") or record.get("track")),
            subset_constraints=subset_constraints,
        )
        specs.append(spec)
        if (
            overflow_to_free
            and scope == "primary"
            and "free" not in bucket.casefold().replace("-", "_")
        ):
            overflow_to_free_sources.add(rid)
        policy_predicate = _safe_policy({"predicate": (primary_policy or {}).get("predicate", {})}).get("predicate", {})
        if policy_subset_constraints:
            policy_predicate = dict(policy_predicate)
            policy_predicate["subset_constraints"] = subset_constraints
        meta = {
            "requirement_id": rid,
            "scope": scope,
            "curriculum_id": curriculum_id,
            "course_name": name,
            "course_id": catalog_course_id,
            "course_code": _text(row.get("course_code") or row.get("official_course_code")),
            "official_course_identity": _text(row.get("official_course_identity")) or catalog_course_id,
            "credits": credits,
            "max_credits": max_credits,
            "department": department,
            "section": section,
            "course_kind": kind,
            "evidence_state": row_evidence,
            "coverage_state": row_coverage,
            "owner": owner,
            "domain": domain,
            "required_identity_dimensions": tuple(
                key for key, value in (("course_code", official_course_id), ("course_kind", kind), ("department", department), ("section", section)) if value
            ),
            "bucket": spec.bucket,
            "generic": generic,
            "eligible_course_names": eligible_names,
            "accept_any": accept_any,
            "eligible_pool_ids": eligible_pool_ids,
            "overflow_routes": overflow_routes,
            "pool_ids": row_pool_ids
            or ordered_texts((resolved_pool_candidate or {}).get("pool_ids")),
            "pool_evidence_state": _evidence(
                (resolved_pool_candidate or {}).get("pool_evidence_state")
                or (resolved_pool_candidate or {}).get("evidence_state")
            )
            if resolved_pool_candidate
            else UNKNOWN,
            "pool_membership_evidence": tuple(
                _safe_pool_membership_evidence((resolved_pool_candidate or {}).get("pool_membership_evidence"))
            ) if resolved_pool_candidate else (),
            "membership_ids": ordered_texts((resolved_pool_candidate or {}).get("membership_ids")),
            "college": _text((resolved_pool_candidate or {}).get("college")),
            "department_unit": _text(
                row.get("department_unit")
                or (resolved_pool_candidate or {}).get("department_unit")
            ),
            "official_department_unit": _text(
                row.get("official_department_unit")
                or (resolved_pool_candidate or {}).get("official_department_unit")
            ),
            "course_department_unit": _text(
                row.get("course_department_unit")
                or (resolved_pool_candidate or {}).get("course_department_unit")
            ),
            "offering_department_unit": _text(
                row.get("offering_department_unit")
                or (resolved_pool_candidate or {}).get("offering_department_unit")
            ),
            "department_membership": _text(
                row.get("department_membership")
                or (resolved_pool_candidate or {}).get("department_membership")
            ),
            "offering_program_slug": _text(
                row.get("offering_program_slug")
                or (resolved_pool_candidate or {}).get("offering_program_slug")
            ),
            "course_program_slug": _text(
                row.get("course_program_slug")
                or (resolved_pool_candidate or {}).get("course_program_slug")
            ),
            "course_owner_program": _text(
                row.get("course_owner_program")
                or (resolved_pool_candidate or {}).get("course_owner_program")
            ),
            "owner_program_slug": _text(
                row.get("owner_program_slug")
                or (resolved_pool_candidate or {}).get("owner_program_slug")
            ),
            "department_program_slug": _text(
                row.get("department_program_slug")
                or (resolved_pool_candidate or {}).get("department_program_slug")
            ),
            "offering_department": _text(
                row.get("offering_department")
                or (resolved_pool_candidate or {}).get("offering_department")
            ),
            "secondary_course_key": _text(
                row.get("secondary_course_key")
                or (resolved_pool_candidate or {}).get("secondary_course_key")
            ),
            "candidate_key": _text(
                row.get("candidate_key")
                or (resolved_pool_candidate or {}).get("candidate_key")
            ),
            "course_key": _text(
                row.get("course_key")
                or (resolved_pool_candidate or {}).get("course_key")
            ),
            "math_secondary_composite_required": math_secondary_scope or bool(row.get("math_secondary_composite_required")),
            "program_approved_membership_ids": ordered_texts(
                row.get("program_approved_membership_ids")
                or row.get("approved_membership_ids")
                or row.get("program_approved_exemption_memberships")
                or (resolved_pool_candidate or {}).get("program_approved_membership_ids")
            ),
            "program_approved_applicable_terms": ordered_texts(
                row.get("program_approved_applicable_terms")
                or row.get("approved_applicable_terms")
                or (resolved_pool_candidate or {}).get("program_approved_applicable_terms")
            ),
            "recognition_basis": _text(
                row.get("recognition_basis")
                or (resolved_pool_candidate or {}).get("recognition_basis")
            ),
            "excluded_from_free": (
                row.get("excluded_from_free") is True
                or (resolved_pool_candidate or {}).get("excluded_from_free") is True
                or (isinstance(direct_policy, Mapping) and direct_policy.get("excluded_from_free") is True)
            ),
            "overflow_to_free": overflow_to_free,
            "overflow_to_free_elective": overflow_to_free_elective,
            "source_reference": _text(
                row.get("source_reference")
                or (resolved_pool_candidate or {}).get("source_reference")
            ),
            "choice_group": _text(row.get("choice_group")),
            "choice_rule": _text(row.get("choice_rule")),
            "waiver": bool(row.get("waiver") or row.get("waiver_allowed")),
            "waiver_generates_credits": bool(row.get("waiver_generates_credits")),
            "component_type": component,
            "policy_id": _text((primary_policy or {}).get("policy_id")),
            "policy_revision": _text((primary_policy or {}).get("revision")),
            "policy_source_reference": _text((primary_policy or {}).get("policy_source_reference")),
            "policy_rule": _text((primary_policy or {}).get("selection_rule")),
            "policy_state": _text((primary_policy or {}).get("policy_state")) or NOT_APPLICABLE,
            "policy_reason": tuple((primary_policy or {}).get("policy_reason", ())),
            "policy": _safe_policy((primary_policy or {}).get("policy")),
            "policy_predicate": policy_predicate,
            "subset_constraints": subset_constraints,
            "curriculum_version": spec.curriculum_version,
            "program_slug": spec.program_slug,
            "track_slug": spec.track_slug,
            "candidate_course_count": sum(
                1
                for candidate in pool_candidates
                if set(_text(item) for item in candidate.get("pool_ids", ())).intersection(eligible_pool_ids)
            ),
            "source": _safe_provenance({**dict(record), **dict(row)}, requirement_id=rid, scope=scope),
        }
        metadata[rid] = meta
        provenance.append(meta["source"])
    # ``course_pools`` are candidate identity evidence, never named
    # requirements.  Keep them in the private compiler metadata so transcript
    # rows without a PDF course number can still resolve by exact
    # handbook-year name/credit/component matching.
    for index, candidate in enumerate(pool_candidates):
        key = f"__pool_candidate__:{_text(candidate.get('course_id'))}:{index}"
        metadata.setdefault(key, dict(candidate))
    quota_specs = _unresolved_threshold_quotas(
        record,
        scope=scope,
        curriculum_id=curriculum_id,
        record_coverage=record_coverage,
        record_evidence=record_evidence,
        # Minor catalogues encode their own aggregate rows (including APC's
        # explicit unnamed quota).  Do not append registry threshold aliases
        # as extra credit consumers; that would double count the same rule.
        existing_generic=scope == "minor" or any(bool(item.get("generic")) for item in metadata.values()),
    )
    for spec in quota_specs:
        specs.append(spec)
        source = _safe_provenance(
            {
                **dict(record),
                "claim": f"{spec.name} {spec.credits_required} 學分；官方可執行課程池尚未建置。",
            },
            requirement_id=spec.requirement_id,
            scope=scope,
        )
        metadata[spec.requirement_id] = {
            "requirement_id": spec.requirement_id,
            "scope": scope,
            "curriculum_id": curriculum_id,
            "course_name": spec.name,
            "course_id": "",
            "course_code": "",
            "official_course_identity": "",
            "department": "",
            "section": "",
            "course_kind": "",
            "owner": spec.owner,
            "domain": spec.domain,
            "required_identity_dimensions": (),
            "bucket": spec.bucket,
            "generic": True,
            # Private compiler marker: only threshold placeholders created
            # above are eligible for the post-allocation
            # RULE_NOT_IMPLEMENTED compatibility projection.  Explicit
            # registry aggregate/pool rows have their own evidence and must
            # be decided by the allocator.
            "unresolved_catalog": True,
            "source": source,
        }
        provenance.append(source)
    def is_free_requirement(spec: RequirementSpec) -> bool:
        bucket_token = _text(spec.bucket).casefold().replace("-", "_").replace(" ", "_")
        if bucket_token in {"free", "free_elective", "free_total"}:
            return True
        requirement_token = spec.requirement_id.casefold()
        return requirement_token.endswith(":free_elective") or requirement_token.endswith(":free_total")

    # Some registry versions publish the one-way overflow flag on the
    # department quota/policy rather than materializing a route on the row.
    # Lower only that server-owned flag to a same-owner free requirement.  A
    # free-to-department reverse edge is never synthesized.
    specs_by_id = {spec.requirement_id: spec for spec in specs}
    free_targets = tuple(
        sorted(
            (spec for spec in specs if is_free_requirement(spec)),
            key=lambda spec: (
                0 if _text(spec.bucket).casefold() == "free_elective" else 1,
                spec.requirement_id,
            ),
        )
    )
    for source_id in sorted(overflow_to_free_sources):
        source = specs_by_id.get(source_id)
        if source is None or source.owner != "PRIMARY":
            continue
        target = next(
            (
                candidate
                for candidate in free_targets
                if candidate.requirement_id != source.requirement_id
                and candidate.owner == source.owner
            ),
            None,
        )
        if target is None or target.requirement_id in source.overflow_routes:
            continue
        routes = tuple(dict.fromkeys((*source.overflow_routes, target.requirement_id)))
        updated_source = replace(source, overflow_routes=routes)
        specs_by_id[source.requirement_id] = updated_source
        source_meta = metadata.get(source.requirement_id)
        if source_meta is not None:
            source_meta["overflow_routes"] = routes
    specs = [specs_by_id.get(spec.requirement_id, spec) for spec in specs]
    def canonical_observed_ids(
        constraints: Sequence[Mapping[str, Any]],
        *,
        owner_requirement_id: str,
    ) -> tuple[dict[str, Any], ...]:
        normalized: list[dict[str, Any]] = []
        for raw_constraint in constraints:
            if not isinstance(raw_constraint, Mapping):
                continue
            constraint = dict(raw_constraint)
            observed = constraint.get("observed_requirement_ids") or constraint.get("observed_requirements")
            if isinstance(observed, str):
                observed = (observed,)
            if not isinstance(observed, Sequence) or isinstance(observed, (bytes, bytearray)):
                observed = ()
            observed_ids = tuple(
                dict.fromkeys(
                    observed_requirement_aliases.get(_text(item), _text(item))
                    for item in observed
                    if _text(item)
                )
            )
            constraint["observed_requirement_ids"] = observed_ids or (owner_requirement_id,)
            normalized.append(constraint)
        return tuple(normalized)

    # Apply alias resolution to both executable specs and their private
    # metadata.  Unknown explicit ids are retained exactly as supplied so a
    # future/foreign observation cannot be mistaken for a current requirement.
    normalized_specs: list[RequirementSpec] = []
    for spec in specs:
        if spec.subset_constraints:
            constraints = canonical_observed_ids(
                spec.subset_constraints,
                owner_requirement_id=spec.requirement_id,
            )
            spec = replace(spec, subset_constraints=constraints)
            meta = metadata.get(spec.requirement_id)
            if meta is not None:
                meta["subset_constraints"] = constraints
                predicate = meta.get("policy_predicate")
                if isinstance(predicate, Mapping):
                    predicate = dict(predicate)
                    predicate["subset_constraints"] = constraints
                    meta["policy_predicate"] = predicate
        normalized_specs.append(spec)
    specs = normalized_specs
    compiled_specs = tuple(sorted(specs, key=lambda item: item.requirement_id))
    canonical_specs, route_issues = canonicalize_overflow_routes(compiled_specs)
    if route_issues:
        # Keep malformed raw routes on the affected spec so the allocator can
        # report the explicit rule error at the service boundary.  Valid
        # suffixes are still canonicalized for a stable snapshot view.
        invalid_source_ids = {
            requirement.requirement_id
            for requirement in compiled_specs
            if any(
                issue.startswith(f"OVERFLOW_ROUTE_{kind}:{requirement.requirement_id}")
                for kind in ("AMBIGUOUS", "MISSING", "SELF", "CROSS_OWNER", "CYCLE")
                for issue in route_issues
            )
        }
        specs = tuple(
            replace(canonical, overflow_routes=original.overflow_routes)
            if original.requirement_id in invalid_source_ids
            else canonical
            for original, canonical in zip(compiled_specs, canonical_specs)
        )
    else:
        specs = canonical_specs
    provenance = sorted(provenance, key=lambda item: (str(item.get("requirement_id", "")), str(item.get("assertion_id", ""))))
    return tuple(specs), metadata, tuple(provenance)


def _curriculum_provenance(record: Mapping[str, Any] | None, *, scope: str) -> tuple[dict[str, Any], ...]:
    """Project each checked-in rule assertion as a safe provenance row."""

    if not isinstance(record, Mapping):
        return ()
    curriculum_id = _text(record.get("curriculum_id"))
    assertions = record.get("source_assertions", record.get("assertions", ()))
    rows: list[dict[str, Any]] = []
    if isinstance(assertions, Sequence) and not isinstance(assertions, (str, bytes, bytearray)):
        for assertion in assertions:
            if not isinstance(assertion, Mapping):
                continue
            merged = {
                **dict(record),
                **dict(assertion),
                "curriculum_id": curriculum_id,
                "version": record.get("version"),
                "admission_cohort": record.get("admission_cohort") or record.get("version"),
            }
            rows.append(_safe_provenance(merged, scope=scope))
    if not rows:
        rows.append(_safe_provenance(record, scope=scope))
    return tuple(sorted(rows, key=lambda item: (str(item.get("assertion_id", item.get("id", ""))), str(item.get("curriculum_id", "")))))


def _is_math_secondary_descriptor(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("math_secondary_composite_required") is False:
        return False
    program = _text(value.get("program_slug") or value.get("program")).casefold()
    scope = _text(value.get("scope") or value.get("role")).casefold()
    return program in {"math", "數學"} and scope in {
        "target",
        "minor",
        "secondary",
        "secondary_target",
        "double_major_target",
        "minor_target",
    }


def _math_secondary_verified_memberships(
    evidence: PublicCourseEvidence,
    *,
    cohort: str,
) -> tuple[str, ...]:
    prefix = f"math-secondary:{cohort}:"
    return tuple(
        dict.fromkeys(
            _text(item)
            for item in evidence.verified_memberships
            if _text(item).startswith(prefix)
        )
    )


def _filter_math_secondary_pool_metadata(
    resolved_meta: Mapping[str, Any] | None,
    resolved_pool_ids: Sequence[str],
    *,
    composite_ids: Sequence[str],
) -> tuple[Mapping[str, Any] | None, tuple[str, ...]]:
    """Keep target attempts on the server-owned composite membership only."""

    allowed = set(_text(item) for item in composite_ids if _text(item))
    if not resolved_meta:
        # Public evidence cannot repair an unresolved handbook identity.  A
        # composite membership is the conjunction of the exact table row and
        # the official offering; without the unique server-owned row, adding
        # the public membership here would bypass that conjunction.
        return resolved_meta, ()
    filtered = dict(resolved_meta)
    raw_ids = tuple(_text(item) for item in resolved_pool_ids if _text(item))
    kept_ids = tuple(dict.fromkeys(item for item in (*raw_ids, *allowed) if item in allowed))
    filtered["pool_ids"] = kept_ids
    filtered["membership_ids"] = tuple(
        item for item in _policy_values(resolved_meta.get("membership_ids")) if item in allowed
    )
    raw_evidence = resolved_meta.get("pool_membership_evidence", ())
    if isinstance(raw_evidence, Sequence) and not isinstance(raw_evidence, (str, bytes, bytearray)):
        filtered["pool_membership_evidence"] = tuple(
            item
            for item in raw_evidence
            if isinstance(item, Sequence)
            and item
            and _text(item[0]) in allowed
        )
    else:
        filtered["pool_membership_evidence"] = ()
    return filtered, kept_ids


def _compile_attempts(
    rows: Sequence[NormalizedCourseRow],
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    completion_metadata: Sequence[Mapping[str, Any]] = (),
) -> tuple[tuple[CourseAttempt, ...], dict[str, dict[str, Any]]]:
    curriculum_category_tokens = frozenset(
        {
            "必",
            "必修",
            "系必修",
            "專業必修",
            "核心必修",
            "選",
            "選修",
            "系選修",
            "專業選修",
            "共同必修",
            "共同選修",
            "通識",
            "通識課程",
            "自由學分",
            "自由選修",
            "一般選修",
            "required",
            "required_course",
            "elective",
            "elective_course",
            "general_education",
            "free_elective",
        }
    )

    def kind_token(value: Any) -> str:
        return normalize_course_kind(value)

    def is_curriculum_category(value: Any) -> bool:
        """Recognize category labels without treating them as components.

        Transcript ``type`` fields often carry curriculum placement (for
        example ``系必修`` or ``共同選修``), while the registry's
        ``component_type`` carries lecture/lab semantics.  This allowlist is
        deliberately narrow: an empty or arbitrary value remains UNKNOWN.
        """

        raw = _text(value).strip()
        if not raw:
            return False
        compact = re.sub(r"[\s_\-()/（）]", "", raw).casefold()
        if compact in {
            re.sub(r"[\s_\-()/（）]", "", item).casefold()
            for item in curriculum_category_tokens
        }:
            return True
        return compact.endswith(("必修", "選修", "通識", "自由學分"))

    def row_component(value: Any) -> tuple[str, bool]:
        """Return (component kind, category-only marker) for a transcript row."""

        kind = kind_token(value)
        if kind != UNKNOWN:
            return kind, False
        # An AG102 row can omit both component and course code.  An absent
        # component is a missing identity dimension, while an explicit
        # category label is not a lecture/lab assertion.  The candidate still
        # has to be unique by exact name, credits, and official component.
        raw = _text(value)
        if not raw or raw.upper() in {"UNKNOWN", "UNSPECIFIED", "N/A", "NA"} or raw in {"未提供", "不詳"}:
            return "", True
        return "", is_curriculum_category(raw)

    completion_candidates: list[dict[str, Any]] = []
    for item in completion_metadata:
        if not isinstance(item, Mapping):
            continue
        completion_kind = _text(item.get("kind") or item.get("requirement_type"))
        completion_kind = completion_kind.upper().replace("-", "_").replace(" ", "_")
        if completion_kind != "OFFICIAL_LISTED_COURSE_COMPLETION":
            continue
        official_ids = _policy_values(
            item.get("official_course_ids")
            or item.get("eligible_course_ids")
            or item.get("course_ids")
            or item.get("official_course_id")
        )
        if not official_ids:
            continue
        completion_meta = dict(item)
        completion_meta["completion_only"] = True
        completion_meta.setdefault("course_id", official_ids[0])
        completion_meta.setdefault("course_code", official_ids[0])
        completion_meta.setdefault("official_course_identity", official_ids[0])
        completion_meta.setdefault("course_kind", "")
        completion_candidates.append(completion_meta)

    catalog_by_code: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    catalog_by_name: dict[tuple[str, Decimal], list[Mapping[str, Any]]] = defaultdict(list)
    indexed_metadata = (*metadata.values(), *completion_candidates)
    for meta in indexed_metadata:
        if meta.get("generic"):
            continue
        for key in ("course_code", "official_course_identity", "course_id"):
            value = _text(meta.get(key))
            if value:
                catalog_by_code[value].append(meta)
        name = _course_label(meta.get("course_name") or meta.get("name"))
        credits = _positive_number(meta.get("credits"))
        if name and (credits > 0 or meta.get("completion_only") is True):
            # Keep numeric credits as Decimal all the way through the
            # no-code index.  Stringifying makes equivalent 2, 2.0 and 2.00
            # values form different keys and hides an otherwise exact
            # handbook identity.
            catalog_by_name[(name, credits)].append(meta)
            # Existing explicit handbook aliases were previously ignored by
            # the no-code index. Keep the alias scoped to 113 Earth electives.
            if any(str(pool).startswith("pool:primary:113:earth:") for pool in meta.get("pool_ids", ())):
                from curriculum_registry import _RULES
                aliases = _RULES.get("shared", {}).get("course_aliases", {}).get("earth_life_common_electives", {})
                for canonical, names in aliases.items():
                    if name == _course_label(canonical):
                        for alias in names:
                            catalog_by_name[(_course_label(alias), credits)].append(
                                {**meta, "confirmed_lookup_alias": _course_label(alias)})
    policy_metadata = tuple(
        meta
        for meta in metadata.values()
        if meta.get("generic") and _text(meta.get("policy_rule")) in _POLICY_SELECTION_RULES
    )
    try:
        public_catalog = load_public_course_catalog()
    except (OSError, TypeError, ValueError):
        # A broken optional public bundle must leave the public property axis
        # unresolved; it must never make the transcript compiler fail open.
        public_catalog = None

    def public_pool_aliases() -> dict[str, tuple[str, ...]]:
        """Map stable public memberships to this record's actual pool IDs."""

        aliases: defaultdict[str, set[str]] = defaultdict(set)
        # Category memberships may alias a public category to a consuming
        # registry pool only when the pool itself carries an approved
        # ``official_category_policy``.  A candidate pool named
        # ``...:common_elective`` is a department/elective pool and must not
        # be inferred to mean the GE ``共同選修`` category merely because the
        # identifier has the same suffix.
        policy_category_memberships = {
            _course_label("國文類"): "university_compulsory",
            _course_label("英文類"): "university_compulsory",
            _course_label("共同必修"): "university_compulsory",
            _course_label("校定必修"): "university_compulsory",
            _course_label("共同選修"): "ge_common_elective",
            _course_label("common_elective"): "ge_common_elective",
            _course_label("ge_common_elective"): "ge_common_elective",
            _course_label("藝術與美感"): "ge_art",
            _course_label("藝術與美感領域"): "ge_art",
            _course_label("ge_art"): "ge_art",
            _course_label("人文與文化思考"): "ge_humanities",
            _course_label("人文與文化思考領域"): "ge_humanities",
            _course_label("ge_humanities"): "ge_humanities",
            _course_label("公民素養與社會探索"): "ge_civic",
            _course_label("公民素養與社會探索領域"): "ge_civic",
            _course_label("ge_civic"): "ge_civic",
            _course_label("自然、生命與科技"): "ge_nature",
            _course_label("自然、生命與科技領域"): "ge_nature",
            _course_label("ge_nature"): "ge_nature",
        }
        for meta in metadata.values():
            pool_ids = _policy_values(
                (
                    *(_policy_values(meta.get("pool_ids"))),
                    *(_policy_values(meta.get("eligible_pool_ids"))),
                    _text(meta.get("source_pool_id")),
                    _text(meta.get("pool_id")),
                )
            )
            if not pool_ids:
                continue
            memberships = set(_policy_values(meta.get("membership_ids")))
            if _text(meta.get("policy_rule")) == "official_category_policy":
                predicate = meta.get("policy_predicate")
                category = _course_label(predicate.get("category")) if isinstance(predicate, Mapping) else ""
                membership = policy_category_memberships.get(category)
                if membership:
                    aliases[membership].update(pool_ids)
            if _course_label(meta.get("pool_bucket")) == "physical_education":
                aliases["university_physical_education_completion"].update(pool_ids)
        return {key: tuple(sorted(values)) for key, values in aliases.items() if values}

    public_pool_membership_ids = public_pool_aliases()
    public_program_slug = next(
        (
            _text(meta.get("program_slug") or meta.get("program"))
            for meta in metadata.values()
            if _text(meta.get("program_slug") or meta.get("program"))
        ),
        "",
    )

    def policy_excluded(
        policy_meta: Mapping[str, Any],
        row: NormalizedCourseRow,
        resolved_meta: Mapping[str, Any] | None,
        public_evidence: PublicCourseEvidence,
    ) -> bool:
        predicate = policy_meta.get("policy_predicate")
        if not isinstance(predicate, Mapping):
            return False
        excluded_ids = _policy_values(predicate.get("exclude_course_ids"))
        excluded_names = {_course_label(item) for item in _policy_values(predicate.get("exclude_course_names"))}
        excluded_types = {_course_label(item) for item in _policy_values(predicate.get("exclude_course_types"))}
        excluded_pool_ids = set(_policy_values(predicate.get("exclude_pool_ids")))
        excluded_categories = {_course_label(item) for item in _policy_values(predicate.get("exclude_categories"))}
        resolved_ids = {
            _text(resolved_meta.get(key))
            for key in ("course_id", "course_code", "official_course_identity")
            if resolved_meta and _text(resolved_meta.get(key))
        }
        resolved_pool_ids = set(_policy_values((resolved_meta or {}).get("pool_ids")))
        # ``excluded_from_free`` is a server-owned candidate/policy fact.  It
        # survives the curriculum sanitizer and is checked before any public
        # category inference, so a verified handbook exclusion cannot be
        # reintroduced into the open-elective policy by an omitted transcript
        # course_type or a caller claim.
        if (resolved_meta or {}).get("excluded_from_free") is True:
            return True
        if excluded_ids and resolved_ids.intersection(excluded_ids):
            return True
        if excluded_pool_ids and resolved_pool_ids.intersection(excluded_pool_ids):
            return True
        if excluded_names and _course_label(row.course_name) in excluded_names:
            return True
        if excluded_types and _course_label(row.course_type) in excluded_types:
            return True
        official_category = (
            _course_label(public_evidence.official_category)
            if public_evidence.official_category_state == PUBLIC_VERIFIED
            else _course_label((resolved_meta or {}).get("category") or (resolved_meta or {}).get("official_category"))
        )
        return bool(excluded_categories and official_category in excluded_categories)

    def policy_membership_records(
        row: NormalizedCourseRow,
        status: str,
        row_credits: Decimal,
        resolved_meta: Mapping[str, Any] | None,
        resolved_pool_ids: Sequence[str],
        resolved_pool_state: str,
        public_evidence: PublicCourseEvidence,
    ) -> tuple[tuple[str, str, str, str], ...]:
        records: list[tuple[str, str, str, str]] = []
        seen: set[str] = set()
        explicit_pool_evidence: dict[str, tuple[str, str, str]] = {}
        raw_pool_evidence = (resolved_meta or {}).get("pool_membership_evidence", ())
        if isinstance(raw_pool_evidence, Sequence) and not isinstance(raw_pool_evidence, (str, bytes, bytearray)):
            for item in raw_pool_evidence:
                if isinstance(item, Mapping):
                    pool_key = _text(item.get("pool_id") or item.get("membership_id"))
                    state = _evidence(item.get("evidence_state") or item.get("state"))
                    source = _text(item.get("source_reference") or item.get("evidence_reference"))
                    kind = _text(item.get("kind") or item.get("membership_kind")) or "catalog"
                elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
                    values = tuple(item)
                    pool_key = _text(values[0]) if values else ""
                    state = _evidence(values[1]) if len(values) > 1 else UNKNOWN
                    source = _text(values[2]) if len(values) > 2 else ""
                    kind = _text(values[3]) if len(values) > 3 else "catalog"
                else:
                    continue
                if pool_key:
                    previous = explicit_pool_evidence.get(pool_key)
                    if previous is None or (previous[0] != VERIFIED and state == VERIFIED):
                        explicit_pool_evidence[pool_key] = (state, source, kind)
        if resolved_meta:
            meta_source = _text(resolved_meta.get("membership_source_reference") or resolved_meta.get("source_reference"))
            meta_state = _evidence(resolved_meta.get("pool_evidence_state") or resolved_meta.get("evidence_state"))
            for m_id in _policy_values(resolved_meta.get("membership_ids")):
                if m_id and meta_source:
                    explicit_pool_evidence[m_id] = (meta_state, meta_source, "catalog")
        for item in public_evidence.pool_membership_evidence:
            if not isinstance(item, Sequence) or len(item) < 4:
                continue
            pool_key = _text(item[0])
            if not pool_key:
                continue
            state = _evidence(item[1])
            source = _text(item[2])
            kind = _text(item[3]) or "public_catalog"
            previous = explicit_pool_evidence.get(pool_key)
            if previous is None or (previous[0] != VERIFIED and state == VERIFIED):
                explicit_pool_evidence[pool_key] = (state, source, kind)
        for pool_id in resolved_pool_ids:
            if pool_id and pool_id not in seen:
                state, source, kind = explicit_pool_evidence.get(
                    pool_id,
                    (resolved_pool_state, _text((resolved_meta or {}).get("source_reference")), "catalog"),
                )
                records.append((pool_id, state, source, kind))
                seen.add(pool_id)
        if resolved_meta:
            for membership_id in _policy_values(resolved_meta.get("membership_ids")):
                if membership_id not in seen:
                    records.append(
                        (
                            membership_id,
                            _evidence(resolved_meta.get("pool_evidence_state") or resolved_meta.get("evidence_state")),
                            _text(
                                resolved_meta.get("membership_source_reference")
                                or resolved_meta.get("source_reference")
                            ),
                            "catalog",
                        )
                    )
                    seen.add(membership_id)
        # Keep explicit membership assertions even when the asserted id is
        # not one of the candidate's consuming pool ids.  In particular, a
        # trusted ``NOT_MEMBER`` row is a real negative fact for a later
        # subset constraint; dropping it here would make the compiler infer
        # non-membership from mere absence.
        for membership_id, (state, source, kind) in explicit_pool_evidence.items():
            if membership_id and membership_id not in seen:
                records.append((membership_id, state, source, kind))
                seen.add(membership_id)
        # Public memberships are server-derived from exact term/course
        # listings.  They are merged only after handbook records so an
        # existing exact pool assertion remains authoritative for that same
        # id, while public aliases still expose category/IT/PE properties.
        for item in public_evidence.pool_membership_evidence:
            if not isinstance(item, Sequence) or len(item) < 4:
                continue
            membership_id = _text(item[0])
            if membership_id and membership_id not in seen:
                records.append(
                    (
                        membership_id,
                        _evidence(item[1]),
                        _text(item[2]),
                        _text(item[3]) or "public_catalog",
                    )
                )
                seen.add(membership_id)
        for policy_meta in policy_metadata:
            rule = _text(policy_meta.get("policy_rule"))
            policy_state = _text(policy_meta.get("policy_state"))
            policy_ids = _policy_values(policy_meta.get("pool_id") or policy_meta.get("eligible_pool_ids"))
            policy_id = policy_ids[0] if policy_ids else ""
            if not policy_id or policy_state != VERIFIED or status != PASS or row_credits <= 0:
                continue
            if policy_excluded(policy_meta, row, resolved_meta, public_evidence):
                continue
            predicate = policy_meta.get("policy_predicate")
            if rule == "official_category_policy":
                # A category policy is executable only through an official
                # exact candidate (or an explicitly enabled official category
                # field).  Transcript labels such as ``通識``/``選修`` are
                # candidate hints and never enough on their own.
                candidate_pool_ids = set(_text(item) for item in (resolved_meta or {}).get("pool_ids", ()))
                candidate_pool_ids.update(public_evidence.verified_memberships)
                explicitly_enabled = isinstance(predicate, Mapping) and predicate.get("allow_transcript_category_field") is True
                official_category = (
                    public_evidence.official_category
                    if public_evidence.official_category_state == PUBLIC_VERIFIED
                    else ""
                )
                if not official_category and explicitly_enabled:
                    # This branch is retained for reviewed legacy policies;
                    # normalized transcript rows do not carry a category
                    # field, so self-claims cannot reach it.
                    official_category = _text(row.as_dict().get("official_category"))
                expected_category = _text((predicate or {}).get("category")) if isinstance(predicate, Mapping) else ""
                if policy_id not in candidate_pool_ids and not (official_category and expected_category and official_category == expected_category):
                    continue
            elif rule != "official_open_elective_policy":
                continue
            if rule == "official_open_elective_policy":
                # GE/國英/PE public categories are authoritative exclusions
                # from free elective credit.  Do not let an exact public
                # identity or an omitted transcript course_type override that
                # server-derived negative fact.
                public_free_exclusion = any(
                    isinstance(item, Sequence)
                    and len(item) >= 4
                    and _text(item[0]) == "university_common_excluded_from_free"
                    and _evidence(item[1]) == VERIFIED
                    and _text(item[3]) == "public_catalog"
                    for item in public_evidence.pool_membership_evidence
                )
                if public_free_exclusion:
                    continue
            policy_source = _text(policy_meta.get("policy_source_reference") or policy_meta.get("policy_id"))
            if rule == "official_open_elective_policy":
                # The open-elective predicate is bounded by the official
                # catalog.  A transcript category label alone cannot prove
                # that a row is eligible (and a missing code/department is
                # common in the official transcript export).  Exact title,
                # credit, and component matching against an official catalog
                # candidate supplies that proof; otherwise retain an UNKNOWN
                # policy marker so the total is not silently treated as FAIL.
                catalog_identity = bool(
                    (
                        resolved_meta
                        and not resolved_meta.get("generic")
                        and _text(
                            resolved_meta.get("official_course_identity")
                            or resolved_meta.get("course_id")
                            or resolved_meta.get("course_code")
                        )
                    )
                    or public_evidence.public_identity_state == PUBLIC_VERIFIED
                )
                policy_state_for_row = VERIFIED if catalog_identity else UNKNOWN
            else:
                policy_state_for_row = VERIFIED
            records.append((policy_id, policy_state_for_row, policy_source, "policy"))
            seen.add(policy_id)
            if rule == "official_open_elective_policy" and isinstance(predicate, Mapping):
                constraint_values = predicate.get("subset_constraints", ())
                if isinstance(constraint_values, Mapping):
                    constraint_values = (constraint_values,)
                if isinstance(constraint_values, Sequence) and not isinstance(constraint_values, (str, bytes, bytearray)):
                    for constraint in constraint_values:
                        if not isinstance(constraint, Mapping):
                            continue
                        membership_id = _text(
                            constraint.get("membership_id")
                            or constraint.get("subset_id")
                            or constraint.get("pool_id")
                        )
                        if not membership_id or membership_id in seen:
                            continue
                        explicit_memberships = set(_policy_values((resolved_meta or {}).get("membership_ids")))
                        explicit_memberships.update(public_evidence.verified_memberships)
                        # Official catalog adapters may expose a normalized
                        # college/department membership instead of the final
                        # subset ID.  Accept only an explicit field named by
                        # the policy, never a fuzzy department/title guess.
                        science_fields = _policy_values(predicate.get("science_membership_fields"))
                        if not science_fields:
                            science_fields = ("college", "college_slug", "membership_ids")
                        for field_name in science_fields:
                            field_value = _text((resolved_meta or {}).get(field_name))
                            if field_value and field_value == membership_id:
                                explicit_memberships.add(membership_id)
                        # Candidate metadata may provide an official college
                        # membership.  Otherwise retain an UNKNOWN marker so
                        # free-total evidence is still usable while the
                        # subset remains a distinct unresolved question.
                        state = VERIFIED if membership_id in explicit_memberships else UNKNOWN
                        records.append(
                            (
                                membership_id,
                                state,
                                _text(
                                    constraint.get("source_reference")
                                    or constraint.get("evidence_reference")
                                    or constraint.get("policy_source_reference")
                                    or policy_meta.get("source_reference")
                                    or policy_meta.get("policy_source_reference")
                                ),
                                "policy",
                            )
                        )
                        seen.add(membership_id)
        return tuple(records)
    attempts: list[CourseAttempt] = []
    safe_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_dict = row.as_dict()
        row_section = _text(row_dict.get("section"))
        course_code = _text(row.course_code)
        course_name = _text(row.course_name)
        attempt_id = f"attempt:{_digest([course_code, course_name, row.term, str(row.credits), str(row.earned_credits), row.status, row.attempt_group])}"
        label = _course_label(course_name)
        identity = UNKNOWN
        row_kind, row_component_unspecified = row_component(row.course_type)
        row_credits = _positive_number(row.credits)
        code_candidates = (catalog_by_code.get(course_code, ()) if course_code else ()) or catalog_by_name.get((label, row_credits), ())
        matching_candidates: list[tuple[Mapping[str, Any], str]] = []
        candidate_by_key: dict[tuple[Any, ...], Mapping[str, Any]] = {}
        for meta in code_candidates:
            candidate_identity = _text(meta.get("official_course_identity") or meta.get("course_id") or meta.get("course_code"))
            candidate_key = (
                candidate_identity,
                _course_label(meta.get("course_name")),
                _positive_number(meta.get("credits")),
                kind_token(meta.get("course_kind") or meta.get("component_type")),
                _text(meta.get("department")),
                # A non-empty official identity is already scoped by the
                # registry.  Section labels may use a localized display
                # spelling in the named row, so merge those exact identities.
                # If no identity exists, retain section as an ambiguity
                # dimension instead of collapsing same-name candidates.
                "" if candidate_identity else _text(meta.get("section")),
            )
            if candidate_key in candidate_by_key:
                base = dict(candidate_by_key[candidate_key])
                for sequence_key in ("pool_ids", "membership_ids"):
                    merged_values = (
                        _policy_values(base.get(sequence_key))
                        + _policy_values(meta.get(sequence_key))
                    )
                    if merged_values:
                        base[sequence_key] = tuple(dict.fromkeys(merged_values))
                base_evidence = _evidence(base.get("pool_evidence_state") or base.get("evidence_state"))
                extra_evidence = _evidence(meta.get("pool_evidence_state") or meta.get("evidence_state"))
                if base_evidence != VERIFIED and extra_evidence == VERIFIED:
                    base["pool_evidence_state"] = VERIFIED
                for scalar_key in (
                    "term",
                    "curriculum_version",
                    "program_slug",
                    "track_slug",
                    "membership_source_reference",
                ):
                    if not _text(base.get(scalar_key)) and _text(meta.get(scalar_key)):
                        base[scalar_key] = meta[scalar_key]
                raw_base_evidence = base.get("pool_membership_evidence", ())
                raw_extra_evidence = meta.get("pool_membership_evidence", ())
                merged_evidence = tuple(
                    item
                    for item in (
                        *(raw_base_evidence if isinstance(raw_base_evidence, Sequence) and not isinstance(raw_base_evidence, (str, bytes, bytearray)) else ()),
                        *(raw_extra_evidence if isinstance(raw_extra_evidence, Sequence) and not isinstance(raw_extra_evidence, (str, bytes, bytearray)) else ()),
                    )
                )
                if merged_evidence:
                    base["pool_membership_evidence"] = merged_evidence
                candidate_by_key[candidate_key] = base
                continue
            candidate_by_key[candidate_key] = meta
        for meta in candidate_by_key.values():
            expected_kind = kind_token(meta.get("course_kind") or meta.get("component_type"))
            if expected_kind == UNKNOWN and meta.get("completion_only") is not True:
                continue
            meta_credits = _positive_number(meta.get("credits"))
            if meta_credits > 0 and meta_credits != row_credits:
                continue
            if not row_component_unspecified and (row_kind == UNKNOWN or expected_kind != row_kind):
                continue
            required_dimensions = set(meta.get("required_identity_dimensions", ()))
            candidate_term = _text(meta.get("term") or meta.get("academic_term") or meta.get("semester"))
            if candidate_term and candidate_term != _text(row.term):
                continue
            if "department" in required_dimensions and _text(row.department) and _text(meta.get("department")) != _text(row.department):
                continue
            # A raw course code does not establish a registry track/section.
            # The checked-in official identity, however, is already scoped by
            # that section; accepting it here does not infer anything from a
            # title or category label.  Other section-aware codes remain
            # unresolved until an adapter supplies section evidence.
            if "section" in required_dimensions and row_section and _text(meta.get("section")) != row_section:
                continue
            if (label and _course_label(meta.get("course_name") or meta.get("name")) != label
                    and meta.get("confirmed_lookup_alias") != label):
                continue
            matching_candidates.append((meta, expected_kind))
        matching_handbook_metadata = tuple(item[0] for item in matching_candidates)
        math_secondary_scope = any(
            _is_math_secondary_descriptor(meta)
            for meta in matching_handbook_metadata
        )
        public_row_program = (
            _text(matching_handbook_metadata[0].get("program_slug") or matching_handbook_metadata[0].get("program"))
            if matching_handbook_metadata
            else public_program_slug
        )
        if public_catalog is not None:
            try:
                public_evidence = resolve_public_evidence(
                    row_dict,
                    catalog=public_catalog,
                    pool_membership_ids=public_pool_membership_ids,
                    handbook_metadata=(
                        *matching_handbook_metadata,
                        *tuple(item for item in completion_metadata if isinstance(item, Mapping)),
                    ),
                    program_slug=public_row_program,
                )
            except (TypeError, ValueError):
                public_evidence = PublicCourseEvidence(reasons=("PUBLIC_CATALOG_UNAVAILABLE",))
        else:
            public_evidence = PublicCourseEvidence(reasons=("PUBLIC_CATALOG_UNAVAILABLE",))
        # Bridge AST inferred_category or bracket prefix tags for General Education
        inferred_cat = _text(row_dict.get("inferred_category"))
        if not inferred_cat:
            prefix_tag = _text(row_dict.get("prefix_tag"))
            if not prefix_tag:
                m_tag = re.match(r"^(\[[^\]]+\])", _text(row_dict.get("raw_name") or row_dict.get("course_name")))
                if m_tag:
                    prefix_tag = m_tag.group(1)
            if prefix_tag:
                if "自然" in prefix_tag or "科技" in prefix_tag:
                    inferred_cat = "自然、生命與科技領域"
                elif "藝術" in prefix_tag or "美感" in prefix_tag:
                    inferred_cat = "藝術與美感領域"
                elif "人文" in prefix_tag:
                    inferred_cat = "人文與文化思考領域"
                elif "公民" in prefix_tag or "社會" in prefix_tag:
                    inferred_cat = "公民素養與社會探索領域"
                elif "共同選修" in prefix_tag:
                    inferred_cat = "共同選修"
                elif "校共同" in prefix_tag or "校定必修" in prefix_tag:
                    inferred_cat = "校共同必修"

        category_to_membership = {
            "自然、生命與科技領域": "ge_nature",
            "自然、生命與科技": "ge_nature",
            "藝術與美感領域": "ge_art",
            "藝術與美感": "ge_art",
            "人文與文化思考領域": "ge_humanities",
            "人文與文化思考": "ge_humanities",
            "公民素養與社會探索領域": "ge_civic",
            "公民素養與社會探索": "ge_civic",
            "共同選修": "ge_common_elective",
            "通識共同選修": "ge_common_elective",
            "校共同必修": "university_compulsory",
            "校定必修": "university_compulsory",
        }
        ge_membership_key = category_to_membership.get(inferred_cat)
        target_ge_pools = public_pool_membership_ids.get(ge_membership_key, ()) if ge_membership_key else ()
        if ge_membership_key and target_ge_pools:
            new_verified = set(public_evidence.verified_memberships)
            new_verified.add(ge_membership_key)
            new_verified.add("university_common_excluded_from_free")
            new_verified.update(target_ge_pools)
            new_evidence = list(public_evidence.pool_membership_evidence)
            for pid in target_ge_pools:
                new_evidence.append((pid, VERIFIED, "transcript_inferred_tag", "public_catalog"))
            new_evidence.append((ge_membership_key, VERIFIED, "transcript_inferred_tag", "public_catalog"))
            new_evidence.append(("university_common_excluded_from_free", VERIFIED, "transcript_inferred_tag", "public_catalog"))
            public_evidence = replace(
                public_evidence,
                public_identity_state=PUBLIC_VERIFIED,
                official_category=inferred_cat,
                official_category_state=PUBLIC_VERIFIED,
                verified_memberships=tuple(dict.fromkeys(sorted(new_verified))),
                pool_membership_evidence=tuple(dict.fromkeys(new_evidence)),
            )
        if len(matching_candidates) > 1:
            distinct_kinds = {c[1] for c in matching_candidates}
            if len(distinct_kinds) == 1:
                req_candidates = [c for c in matching_candidates if c[0].get("requirement_id")]
                if req_candidates:
                    matching_candidates = [req_candidates[0]]
                else:
                    matching_candidates = [matching_candidates[0]]
        if len(matching_candidates) == 1:
            identity = VERIFIED
            resolved_kind = matching_candidates[0][1]
            resolved_meta = matching_candidates[0][0]
        elif len(matching_candidates) == 0 and len(candidate_by_key) == 0 and public_evidence.public_identity_state == PUBLIC_VERIFIED:
            identity = VERIFIED
            resolved_kind = row_kind if row_kind in {"LECTURE", "LAB", "COMBINED"} else "LECTURE"
            resolved_meta = None
        else:
            # Multiple exact candidates (especially candidates with different
            # components) are not safe to resolve by input order.
            resolved_kind = UNKNOWN
            resolved_meta = None
        status_map = {
            "COMPLETED": PASS,
            "IN_PROGRESS": "IN_PROGRESS",
            "FAILED": FAIL,
            "WITHDRAWN": FAIL,
            "NOT_TAKEN": "NOT_TAKEN",
            "WAIVED": WAIVER,
            "TRANSFERRED": UNKNOWN,
        }
        status = status_map.get(_text(row.status).upper(), UNKNOWN)
        if row_kind in {"LECTURE", "LAB", "COMBINED"}:
            course_kind = row_kind
        elif identity == VERIFIED and row_component_unspecified:
            course_kind = resolved_kind
        else:
            course_kind = UNKNOWN
        resolved_course_id = _text((resolved_meta or {}).get("course_id")) or (public_evidence.official_course_code if identity == VERIFIED else "") or course_code or course_name
        resolved_pool_ids = tuple(
            dict.fromkeys(
                _text(item)
                for item in (
                    *((resolved_meta or {}).get("pool_ids", ())),
                    *(public_evidence.verified_memberships if identity == VERIFIED else ()),
                    *(target_ge_pools if (ge_membership_key and identity == VERIFIED) else ()),
                )
                if _text(item)
            )
        )
        resolved_pool_state = (
            _evidence((resolved_meta or {}).get("pool_evidence_state") or (resolved_meta or {}).get("evidence_state"))
            if resolved_meta
            else (VERIFIED if resolved_pool_ids and identity == VERIFIED else UNKNOWN)
        )
        membership_meta = resolved_meta
        if math_secondary_scope:
            secondary_cohort = next(
                (
                    _text(meta.get("curriculum_version") or meta.get("version") or meta.get("cohort"))
                    for meta in matching_handbook_metadata
                    if _text(meta.get("curriculum_version") or meta.get("version") or meta.get("cohort"))
                ),
                "",
            )
            composite_ids = _math_secondary_verified_memberships(
                public_evidence,
                cohort=secondary_cohort,
            ) if secondary_cohort else ()
            membership_meta, resolved_pool_ids = _filter_math_secondary_pool_metadata(
                resolved_meta,
                resolved_pool_ids,
                composite_ids=composite_ids,
            )
            resolved_pool_state = VERIFIED if resolved_pool_ids else UNKNOWN
        membership_records = policy_membership_records(
            row,
            status,
            row_credits,
            membership_meta,
            resolved_pool_ids,
            resolved_pool_state,
            public_evidence,
        )
        attempt = CourseAttempt(
            attempt_id=attempt_id,
            course_id=resolved_course_id,
            course_name=course_name,
            credits=Decimal(str(row.credits)),
            earned_credits=Decimal(str(row.earned_credits)),
            academic_term=row.term,
            repeat_group_id=row.attempt_group or None,
            identity_status=identity,
            course_kind=course_kind,
            pool_memberships=resolved_pool_ids,
            pool_ids=resolved_pool_ids,
            pool_evidence_state=resolved_pool_state,
            pool_membership_evidence=membership_records,
            status=status,
            source_kind="CONFIRMED_TRANSCRIPT",
            grade=row.grade,
            grade_evidence_state=VERIFIED if _text(row.grade) else UNKNOWN,
            curriculum_version=_text((resolved_meta or {}).get("curriculum_version") or (resolved_meta or {}).get("version")),
            program_slug=_text((resolved_meta or {}).get("program_slug") or (resolved_meta or {}).get("program")),
            track_slug=_text((resolved_meta or {}).get("track_slug") or (resolved_meta or {}).get("track")),
        )
        attempts.append(attempt)
        safe_rows[attempt_id] = {
            key: row_dict[key]
            for key in row_dict
            if key in {"course_code", "course_name", "credits", "earned_credits", "status", "term", "grade", "course_type"}
        }
        safe_rows[attempt_id]["public_catalog"] = public_evidence.as_dict()
    attempts.sort(key=lambda item: (item.attempt_id, item.course_id, item.academic_term))
    return tuple(attempts), safe_rows


@dataclass(frozen=True, slots=True)
class NonCreditRequirementResult:
    """One administrative/non-credit completion gate.

    Non-credit gates are deliberately separate from ``AllocationResult``.
    They describe completion evidence and never create a credit portion or
    alter either side of the credit ledger.
    """

    requirement_id: str
    name: str
    kind: str
    status: str
    scope: str
    required_count: int
    completed_count: int
    completed_terms: tuple[str, ...] = ()
    in_progress_terms: tuple[str, ...] = ()
    matched_attempt_ids: tuple[str, ...] = ()
    waived: bool = False
    evidence: str = UNKNOWN
    coverage: str = NONE
    blockers: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    affects_credit_ledger: bool = False
    required_hours: Decimal = Decimal("0")
    completed_hours: Decimal = Decimal("0")
    min_earned_credits_per_completion: Decimal = Decimal("0")
    membership_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "scope": self.scope,
            "required_count": self.required_count,
            "completed_count": self.completed_count,
            "completed_terms": self.completed_terms,
            "in_progress_terms": self.in_progress_terms,
            "matched_attempt_ids": self.matched_attempt_ids,
            "waived": self.waived,
            "evidence": self.evidence,
            "evidence_state": self.evidence,
            "coverage": self.coverage,
            "coverage_state": self.coverage,
            "blockers": self.blockers,
            "provenance": _plain(self.provenance),
            "affects_credit_ledger": False,
            "required_hours": str(self.required_hours),
            "completed_hours": str(self.completed_hours),
            "min_earned_credits_per_completion": str(self.min_earned_credits_per_completion),
            "membership_id": self.membership_id,
        }


def _non_credit_rows(record: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Return explicit non-credit rows, with a narrow zero-credit fallback."""

    if not isinstance(record, Mapping):
        return ()
    raw_rows = record.get("non_credit_requirements", ())
    rows: list[Mapping[str, Any]] = [
        item for item in raw_rows
        if isinstance(item, Mapping) and not _is_credit_subset_gate_row(item)
    ] if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, (str, bytes, bytearray)) else []
    if not rows:
        catalog = record.get("course_catalog", ())
        if isinstance(catalog, Sequence) and not isinstance(catalog, (str, bytes, bytearray)):
            rows = [
                item
                for item in catalog
                if isinstance(item, Mapping)
                and not _is_credit_subset_gate_row(item)
                and (
                    bool(item.get("zero_credit_gate"))
                    or _text(item.get("requirement_type")).lower().replace("-", "_") == "non_credit"
                )
            ]
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        requirement_id = _text(row.get("requirement_id") or row.get("id")) or f"non_credit:{index + 1}"
        if requirement_id in seen:
            continue
        seen.add(requirement_id)
        result.append(row)
    return tuple(result)


def _non_credit_match_labels(row: Mapping[str, Any]) -> tuple[set[str], dict[str, str]]:
    """Build exact title labels and official activity aliases."""

    labels: set[str] = set()
    activities: dict[str, str] = {}
    match = row.get("match")
    if isinstance(match, Mapping):
        for key in ("exact_titles", "official_aliases"):
            for value in _policy_values(match.get(key)):
                normalized = _course_label(value)
                if normalized:
                    labels.add(normalized)
    for key in ("eligible_course_names", "eligible_course_options"):
        values = row.get(key)
        if isinstance(values, Mapping):
            values = tuple(values.values())
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
            for value in values:
                value_text = _text(value.get("name")) if isinstance(value, Mapping) else _text(value)
                normalized = _course_label(value_text)
                if normalized:
                    labels.add(normalized)
    for key in ("name", "raw_title", "title_base"):
        value = _course_label(row.get(key))
        if value and (
            key != "title_base"
            or _text(row.get("kind")).upper() == "DISTINCT_TERM_ITEM_COUNT"
            or _text(row.get("requirement_type")).lower().replace("-", "_") == "non_credit"
        ):
            labels.add(value)
    raw_aliases = row.get("official_activity_aliases")
    if isinstance(raw_aliases, Mapping):
        for key, descriptor in raw_aliases.items():
            canonical = _text(descriptor.get("canonical_id") if isinstance(descriptor, Mapping) else key) or _text(key)
            if not canonical:
                continue
            alias_values: Any = descriptor.get("aliases", ()) if isinstance(descriptor, Mapping) else ()
            aliases = (*_policy_values(alias_values), canonical, _text(key))
            for alias in aliases:
                normalized = _course_label(alias)
                if normalized:
                    labels.add(normalized)
                    activities[normalized] = canonical
    # The nested predicate can carry the same official alias contract.
    predicate = row.get("predicate")
    if isinstance(predicate, Mapping):
        predicate_projection = {
            key: predicate.get(key)
            for key in ("match", "official_activity_aliases", "name", "title_base")
            if predicate.get(key) is not None
        }
        if predicate_projection:
            nested_labels, nested_activities = _non_credit_match_labels(predicate_projection)
            labels.update(nested_labels)
            activities.update(nested_activities)
    return labels, activities


def _non_credit_waiver_matches(
    requirement_id: str,
    records: Sequence[Mapping[str, Any]] | None,
    *,
    requirement: Mapping[str, Any] | None = None,
    subject_ref: str | None = None,
) -> Mapping[str, Any] | None:
    """Accept only an exact, subject-bound, server-authorized waiver record."""

    expected_subject = _text(
        subject_ref if subject_ref is not None else (requirement or {}).get("subject_ref")
    )
    allowed_authorities = frozenset(
        _policy_values((requirement or {}).get("waiver_authority_ids"))
    )
    for record in records or ():
        if not isinstance(record, Mapping):
            continue
        record_type = _text(record.get("record_type") or record.get("type")).upper()
        target_id = _text(record.get("requirement_id") or record.get("target_requirement_id"))
        decision = _text(record.get("decision") or record.get("status")).upper()
        evidence_state = _evidence(record.get("evidence_state"))
        authority = _text(record.get("authority"))
        reference = _text(record.get("evidence_reference") or record.get("source_reference"))
        subject = _text(record.get("subject_ref") or record.get("subject_id"))
        expected_version = _text(
            (requirement or {}).get("waiver_requirement_version")
            or (requirement or {}).get("curriculum_version")
            or (requirement or {}).get("version")
            or (requirement or {}).get("requirement_version")
        )
        actual_version = _text(
            record.get("requirement_version")
            or record.get("curriculum_version")
            or record.get("version")
        )
        expected_program = _text(
            (requirement or {}).get("waiver_program_slug")
            or (requirement or {}).get("program_slug")
            or (requirement or {}).get("program")
        )
        actual_program = _text(record.get("program_slug") or record.get("program"))
        expected_track = _text(
            (requirement or {}).get("waiver_track_slug")
            or (requirement or {}).get("track_slug")
            or (requirement or {}).get("track")
        )
        actual_track = _text(record.get("track_slug") or record.get("track"))
        if (
            record_type == "NON_CREDIT_WAIVER_RECORD"
            and target_id == requirement_id
            and decision in {"APPROVED", "ACTIVE", "WAIVED"}
            and evidence_state == VERIFIED
            and authority in allowed_authorities
            and reference
            and expected_subject
            and subject == expected_subject
            and expected_version
            and actual_version == expected_version
            and expected_program
            and actual_program == expected_program
            and expected_track
            and actual_track == expected_track
        ):
            return record
    return None


def _non_credit_requirement_result(
    row: Mapping[str, Any],
    attempts: Sequence[CourseAttempt],
    *,
    scope: str,
    waiver_records: Sequence[Mapping[str, Any]] = (),
    subject_ref: str | None = None,
) -> NonCreditRequirementResult:
    requirement_id = _text(row.get("requirement_id") or row.get("id")) or f"non_credit:{_digest(row)}"
    name = _text(row.get("name") or row.get("raw_title") or row.get("display_name")) or requirement_id
    kind = _text(row.get("kind") or row.get("requirement_type")).upper() or "NON_CREDIT"
    evidence = _evidence(row.get("evidence_state") or row.get("evidence"))
    coverage = _coverage(row.get("coverage_state"))
    requirement_type = _text(row.get("requirement_type")).lower().replace("-", "_").replace(" ", "_")
    required_value = row.get("required_count", row.get("required_completions", row.get("semesters_required")))
    required_count = int(_positive_number(required_value)) if required_value not in (None, "") else 0
    if required_count <= 0 and requirement_type == "named_course" and bool(row.get("is_zero_credit", row.get("zero_credit_gate"))):
        required_count = 1
    membership_id = _text(row.get("membership_id") or row.get("official_membership_id"))
    minimum_earned = _positive_number(
        row.get("min_earned_credits_per_completion", row.get("minimum_earned_credits_per_completion"))
    )
    applicability_state = _text(row.get("applicability_state") or row.get("scope_state")).upper()
    if row.get("required") is False or applicability_state in {"NOT_APPLICABLE", "N/A", "NA"}:
        return NonCreditRequirementResult(
            requirement_id=requirement_id,
            name=name,
            kind=kind,
            status=NOT_APPLICABLE,
            scope=scope,
            required_count=required_count,
            completed_count=0,
            evidence=evidence,
            coverage=coverage,
            provenance=_safe_provenance(row, requirement_id=requirement_id, scope=scope),
            min_earned_credits_per_completion=minimum_earned,
            membership_id=membership_id,
        )
    blockers: list[str] = []
    if evidence != VERIFIED or coverage != COMPLETE:
        blockers.append(f"OFFICIAL_EVIDENCE_MISSING:{requirement_id}")
    scope_state = _text(row.get("scope_state")).upper()
    if scope_state and scope_state != VERIFIED:
        blockers.append(f"RULE_POLICY_SCOPE_UNVERIFIED:{requirement_id}")
    automatic = row.get("automatic_decision")
    if automatic is not True:
        blockers.append(f"OFFICIAL_EVIDENCE_MISSING:{requirement_id}:AUTOMATIC_DECISION")

    if required_count <= 0:
        blockers.append(f"RULE_NOT_IMPLEMENTED:{requirement_id}:REQUIRED_COUNT")

    required_hours = _positive_number(row.get("required_hours"))
    hours_per_completion = _positive_number(row.get("hours_per_completion"))
    if required_hours > 0 and hours_per_completion <= 0:
        blockers.append(f"RULE_NOT_IMPLEMENTED:{requirement_id}:HOURS_PER_COMPLETION")
    completion_values = row.get("completion_statuses", ("COMPLETED",))
    completion_statuses = { _text(item).upper() for item in _policy_values(completion_values) }
    if not completion_statuses:
        completion_statuses = {"COMPLETED"}
    direct_completion = kind.replace("-", "_").replace(" ", "_") == "OFFICIAL_LISTED_COURSE_COMPLETION"
    official_course_ids = _policy_values(
        row.get("eligible_course_ids")
        or row.get("course_ids")
        or row.get("official_course_ids")
        or row.get("official_course_id")
    )
    labels, activity_by_label = _non_credit_match_labels(row)
    if direct_completion:
        # The display name of an official completion gate is an administrative
        # label (for example, "資訊應用與設計"), not itself a course title.
        # Only explicit listed titles/IDs may narrow the gate.  With neither,
        # the server-owned membership below is the complete course-list
        # identity evidence.
        explicit_labels: set[str] = set()
        match = row.get("match")
        if isinstance(match, Mapping):
            for key in ("exact_titles", "official_aliases"):
                explicit_labels.update(
                    _course_label(item) for item in _policy_values(match.get(key)) if _course_label(item)
                )
        for key in ("eligible_course_names", "eligible_course_options"):
            values = row.get(key)
            if isinstance(values, Mapping):
                values = tuple(values.values())
            for item in _policy_values(values):
                label = _course_label(item.get("name")) if isinstance(item, Mapping) else _course_label(item)
                if label:
                    explicit_labels.add(label)
        labels = explicit_labels
        activity_by_label = {}
    if not labels and not (direct_completion and (official_course_ids or membership_id)):
        blockers.append(f"RULE_NOT_IMPLEMENTED:{requirement_id}:MATCH_DESCRIPTOR")

    waiver_record = _non_credit_waiver_matches(
        requirement_id,
        waiver_records,
        requirement=row,
        subject_ref=subject_ref,
    )
    if waiver_record is not None and bool(row.get("waiver_allowed")):
        provenance = _safe_provenance(
            {**dict(row), **dict(waiver_record)},
            requirement_id=requirement_id,
            scope=scope,
        )
        return NonCreditRequirementResult(
            requirement_id=requirement_id,
            name=name,
            kind=kind,
            status=PASS,
            scope=scope,
            required_count=required_count,
            completed_count=0,
            waived=True,
            evidence=VERIFIED,
            coverage=COMPLETE,
            provenance=provenance,
            required_hours=required_hours,
            min_earned_credits_per_completion=minimum_earned,
            membership_id=membership_id,
        )
    if waiver_record is not None and not bool(row.get("waiver_allowed")):
        blockers.append(f"MANUAL_DECISION_REQUIRED:{requirement_id}:WAIVER_NOT_ALLOWED")

    matched_completed: list[tuple[CourseAttempt, str]] = []
    matched_in_progress: list[CourseAttempt] = []
    missing_term = False
    unknown_candidates = 0
    # ``require_zero_credits`` is a server-owned predicate for listed
    # completion gates such as life/service learning.  It is independent
    # from the gate's own ``credits`` value: an administrative gate can
    # observe a zero-credit transcript row while an IT gate may require a
    # two-credit listed course.
    zero_credit_required = (
        row.get("require_zero_credits") is True
        or (
            not direct_completion
            and (
                bool(row.get("is_zero_credit"))
                or bool(row.get("zero_credit_gate"))
                or _positive_number(row.get("credits")) == 0
            )
        )
    )
    expected_terms = _policy_values(
        row.get("eligible_terms")
        or row.get("terms")
        or row.get("effective_terms")
        or row.get("term")
    )
    expected_version = _text(
        row.get("curriculum_version")
        or row.get("membership_version")
        or row.get("version")
    )
    legacy_version_bound = _text(row.get("term_bound")).upper() in {
        VERIFIED,
        "TRUE",
        "YES",
    }
    version_flag = row.get("membership_version_required")
    membership_version_required = (
        version_flag if isinstance(version_flag, bool) else legacy_version_bound
    )
    version_bound = membership_version_required
    expected_program = _text(row.get("program_slug") or row.get("program"))
    expected_track = _text(row.get("track_slug") or row.get("track"))
    program_flag = row.get("membership_program_required")
    track_flag = row.get("membership_track_required")
    membership_program_required = (
        program_flag if isinstance(program_flag, bool) else bool(expected_program)
    )
    membership_track_required = (
        track_flag if isinstance(track_flag, bool) else bool(expected_track)
    )
    activity_prefix = _text(row.get("activity_membership_prefix"))
    if not activity_prefix and (row.get("distinct_activity_required") is True or kind == "DISTINCT_TERM_ITEM_COUNT"):
        activity_prefix = "university_physical_education_activity:"
    for attempt in attempts:
        if zero_credit_required and attempt.credits != Decimal("0"):
            continue
        label = _course_label(attempt.course_name)
        membership_record = _pool_membership_record(attempt, membership_id) if direct_completion and membership_id else None
        activity = activity_by_label.get(label, "")
        if not activity and activity_prefix:
            for record in attempt.pool_membership_evidence:
                if not isinstance(record, Sequence) or len(record) < 4:
                    continue
                candidate_activity = _text(record[0])
                if (
                    candidate_activity.startswith(activity_prefix)
                    and _evidence(record[1]) == VERIFIED
                    and _text(record[2])
                    and _text(record[3]) not in {"legacy", ""}
                ):
                    activity = candidate_activity.removeprefix(activity_prefix)
                    break
        if direct_completion:
            identity_bound = bool(official_course_ids or labels)
            identity_hint = bool(
                (official_course_ids and attempt.course_id in official_course_ids)
                or (labels and label in labels)
                or membership_record is not None
            )
            if (
                identity_bound
                and identity_hint
                and attempt.status in {PASS, "IN_PROGRESS"}
                and attempt.identity_status != VERIFIED
            ):
                # This gate observes all confirmed completed attempts.  A row
                # whose official identity is unresolved may be on the listed
                # course list, so it must remain UNKNOWN rather than becoming
                # a definite deficit.
                unknown_candidates += 1
                blockers.append(f"OFFICIAL_EVIDENCE_MISSING:{requirement_id}:COURSE_IDENTITY")
                continue
            course_identity_matches = bool(
                (official_course_ids and attempt.course_id in official_course_ids)
                or (labels and label in labels)
                or (not official_course_ids and not labels and membership_record is not None)
            )
            if not course_identity_matches:
                continue
            if identity_bound and attempt.identity_status != VERIFIED:
                if attempt.status in {PASS, "IN_PROGRESS"}:
                    unknown_candidates += 1
                    blockers.append(f"OFFICIAL_EVIDENCE_MISSING:{requirement_id}:COURSE_IDENTITY")
                continue
            if membership_record is None:
                unknown_candidates += 1
                blockers.append(f"OFFICIAL_EVIDENCE_MISSING:{requirement_id}:MEMBERSHIP")
                continue
            membership_state, membership_source, membership_kind = membership_record
            if membership_state != VERIFIED or not membership_source or membership_kind == "legacy":
                unknown_candidates += 1
                blockers.append(f"OFFICIAL_EVIDENCE_MISSING:{requirement_id}:MEMBERSHIP")
                continue
            if version_bound:
                if expected_version and attempt.curriculum_version != expected_version:
                    unknown_candidates += 1
                    blockers.append(f"RULE_POLICY_SCOPE_UNVERIFIED:{requirement_id}:CURRICULUM_VERSION")
                    continue
                if not expected_version or not attempt.curriculum_version:
                    unknown_candidates += 1
                    blockers.append(f"RULE_POLICY_SCOPE_UNVERIFIED:{requirement_id}:CURRICULUM_VERSION")
                    continue
            if membership_program_required and (
                not expected_program or attempt.program_slug != expected_program
            ):
                unknown_candidates += 1
                blockers.append(f"RULE_POLICY_SCOPE_UNVERIFIED:{requirement_id}:PROGRAM")
                continue
            if membership_track_required and (
                not expected_track or attempt.track_slug != expected_track
            ):
                unknown_candidates += 1
                blockers.append(f"RULE_POLICY_SCOPE_UNVERIFIED:{requirement_id}:TRACK")
                continue
            if expected_terms and attempt.academic_term not in expected_terms:
                continue
        elif label not in labels and not activity:
            continue
        if attempt.status not in {PASS, "IN_PROGRESS"}:
            continue
        if not attempt.academic_term:
            missing_term = True
            continue
        if direct_completion and attempt.earned_credits < minimum_earned:
            continue
        if attempt.status == PASS and "COMPLETED" in completion_statuses:
            matched_completed.append((attempt, activity))
        elif attempt.status == "IN_PROGRESS":
            matched_in_progress.append(attempt)

    if missing_term:
        blockers.append(f"STUDENT_INPUT_MISSING:{requirement_id}:TERM")
    matched_ids: list[str] = []
    completed_terms: list[str] = []
    distinct_term = row.get("distinct_term_required") is True
    distinct_activity = row.get("distinct_activity_required") is True
    max_per_term_value = row.get("max_completions_per_term")
    max_per_term = int(_positive_number(max_per_term_value)) if max_per_term_value not in (None, "") else 0
    is_activity_gate = (
        kind == "DISTINCT_TERM_ITEM_COUNT"
        or distinct_term
        or distinct_activity
        or required_hours > 0
        or bool(row.get("official_activity_aliases"))
    )
    selected: list[tuple[CourseAttempt, str]] = []
    unknown_candidates += len(matched_in_progress)
    if is_activity_gate and distinct_term and distinct_activity:
        # Maximum matching over term/activity pairs prevents a repeated
        # activity or two rows from one term from being counted twice.
        edges_by_term: dict[str, list[tuple[str, CourseAttempt]]] = defaultdict(list)
        generic_by_term: set[str] = set()
        for attempt, activity in matched_completed:
            if activity:
                edges_by_term[attempt.academic_term].append((activity, attempt))
            else:
                generic_by_term.add(attempt.academic_term)
        match_by_activity: dict[str, tuple[str, CourseAttempt]] = {}

        def augment(term: str, seen_activities: set[str]) -> bool:
            for activity, attempt in edges_by_term.get(term, ()):
                if activity in seen_activities:
                    continue
                seen_activities.add(activity)
                existing = match_by_activity.get(activity)
                if existing is None or augment(existing[0], seen_activities):
                    match_by_activity[activity] = (term, attempt)
                    return True
            return False

        for term in sorted(edges_by_term):
            augment(term, set())
        selected = [(attempt, activity) for activity, (_term, attempt) in sorted(match_by_activity.items())]
        if generic_by_term:
            unknown_candidates += len(generic_by_term)
    elif is_activity_gate and distinct_term:
        by_term: dict[str, list[tuple[CourseAttempt, str]]] = defaultdict(list)
        for candidate in matched_completed:
            by_term[candidate[0].academic_term].append(candidate)
        for term in sorted(by_term):
            candidates = sorted(by_term[term], key=lambda item: (not bool(item[1]), item[0].attempt_id))
            selected.append(candidates[0])
            if len(candidates) > 1:
                unknown_candidates += max(0, len(candidates) - 1) if not any(item[1] for item in candidates) else 0
    elif is_activity_gate and distinct_activity:
        by_activity: dict[str, tuple[CourseAttempt, str]] = {}
        for candidate in matched_completed:
            if candidate[1] and candidate[1] not in by_activity:
                by_activity[candidate[1]] = candidate
            elif not candidate[1]:
                unknown_candidates += 1
        selected = list(by_activity.values())
    else:
        selected = list(matched_completed)
        if max_per_term > 0:
            counts: defaultdict[str, int] = defaultdict(int)
            limited: list[tuple[CourseAttempt, str]] = []
            for candidate in sorted(selected, key=lambda item: (item[0].academic_term, item[0].attempt_id)):
                term = candidate[0].academic_term
                if counts[term] >= max_per_term:
                    continue
                counts[term] += 1
                limited.append(candidate)
            selected = limited

    selected = sorted(selected, key=lambda item: (item[0].academic_term, item[0].attempt_id))
    for attempt, _activity in selected:
        matched_ids.append(attempt.attempt_id)
        completed_terms.append(attempt.academic_term)
    completed_count = len(selected)
    completed_hours = hours_per_completion * completed_count if hours_per_completion > 0 else Decimal("0")
    required_satisfied = completed_count >= required_count and (required_hours <= 0 or completed_hours >= required_hours)
    if required_satisfied and not any(code.startswith(("RULE_NOT_IMPLEMENTED", "OFFICIAL_EVIDENCE_MISSING", "RULE_POLICY_SCOPE_UNVERIFIED", "MANUAL_DECISION_REQUIRED")) for code in blockers):
        status = PASS
    elif required_satisfied and any(code.startswith("STUDENT_INPUT_MISSING") for code in blockers):
        status = UNKNOWN
    elif unknown_candidates > 0 or missing_term or any(code.startswith(("RULE_NOT_IMPLEMENTED", "OFFICIAL_EVIDENCE_MISSING", "RULE_POLICY_SCOPE_UNVERIFIED", "MANUAL_DECISION_REQUIRED")) for code in blockers):
        status = UNKNOWN
    else:
        status = FAIL
        blockers.append(f"NON_CREDIT_DEFICIT:{requirement_id}")
    provenance = _safe_provenance(row, requirement_id=requirement_id, scope=scope)
    return NonCreditRequirementResult(
        requirement_id=requirement_id,
        name=name,
        kind=kind,
        status=status,
        scope=scope,
        required_count=required_count,
        completed_count=completed_count,
        completed_terms=tuple(dict.fromkeys(completed_terms)),
        in_progress_terms=tuple(dict.fromkeys(sorted(item.academic_term for item in matched_in_progress if item.academic_term))),
        matched_attempt_ids=tuple(dict.fromkeys(matched_ids)),
        evidence=evidence,
        coverage=coverage,
        blockers=tuple(dict.fromkeys(blockers)),
        provenance=provenance,
        required_hours=required_hours,
        completed_hours=completed_hours,
        min_earned_credits_per_completion=minimum_earned,
        membership_id=membership_id,
    )


def _compile_non_credit_results(
    record: Mapping[str, Any] | None,
    attempts: Sequence[CourseAttempt],
    *,
    scope: str,
    waiver_records: Sequence[Mapping[str, Any]] = (),
    subject_ref: str | None = None,
) -> tuple[NonCreditRequirementResult, ...]:
    context = {
        key: record.get(key)
        for key in (
            "curriculum_version",
            "version",
            "program_slug",
            "program",
            "track_slug",
            "track",
        )
        if isinstance(record, Mapping) and record.get(key) not in (None, "")
    }
    return tuple(
        _non_credit_requirement_result(
            {**context, **dict(row)} if context else row,
            attempts,
            scope=scope,
            waiver_records=waiver_records,
            subject_ref=subject_ref,
        )
        for row in _non_credit_rows(record)
    )


def _non_credit_status(results: Sequence[NonCreditRequirementResult]) -> str:
    statuses = tuple(item.status for item in results if item.status != NOT_APPLICABLE)
    if FAIL in statuses:
        return FAIL
    if UNKNOWN in statuses:
        return UNKNOWN
    return PASS if statuses else NOT_APPLICABLE


def _resolve_non_credit_waivers(
    request: EvaluationRequest,
    evidence_resolver: Callable[[str], Any] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Resolve only official subject-bound non-credit waiver records."""

    if not callable(evidence_resolver) or not request.subject_ref:
        return ()
    records: list[Mapping[str, Any]] = []
    for evidence_id in request.official_evidence_ids:
        try:
            record = evidence_resolver(evidence_id)
        except Exception:
            continue
        if not isinstance(record, Mapping):
            continue
        if _text(record.get("record_type") or record.get("type")).upper() != "NON_CREDIT_WAIVER_RECORD":
            continue
        if _text(record.get("subject_ref") or record.get("subject_id")) != request.subject_ref:
            continue
        if _text(record.get("record_id") or record.get("evidence_id")) != evidence_id:
            continue
        records.append(record)
    return tuple(records)


def _source_attempt_id(value: Any, attempts: Sequence[CourseAttempt]) -> str | None:
    candidate = _text(value)
    if not candidate:
        return None
    for attempt in attempts:
        # A source course title is not a stable identity: two departments can
        # legitimately offer the same title.  Only exact released attempt or
        # course identifiers may be addressed by an opaque binding record.
        if candidate in {attempt.attempt_id, attempt.course_id}:
            return attempt.attempt_id
    return None


def _target_requirement_id(value: Any, requirements: Sequence[RequirementSpec], metadata: Mapping[str, Mapping[str, Any]]) -> str | None:
    candidate = _text(value)
    if not candidate:
        return None
    by_id = {item.requirement_id for item in requirements}
    if candidate in by_id:
        return candidate
    label = _course_label(candidate)
    matches = sorted(
        rid
        for rid, meta in metadata.items()
        if meta.get("requirement_id") and not meta.get("generic") and _course_label(meta.get("course_name")) == label
    )
    return matches[0] if len(matches) == 1 else None


def _binding_projection(binding_id: str, *, state: str = UNKNOWN, reason: str = "") -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "source_attempt_id": "",
        "target_requirement_id": "",
        "approved_credits": "0",
        "evidence_state": state,
        "authority": "",
        "evidence_reference": "",
        "direction": "",
        "shared": False,
        "decision": "PENDING",
        "allocation_kind": "",
        "source_requirement_id": "",
        "source_owner": "",
        "target_owner": "",
        "source_domain": "",
        "target_domain": "",
        "reason": reason,
    }


def _compile_bindings(
    evidence_ids: Sequence[str],
    *,
    attempts: Sequence[CourseAttempt],
    requirements: Sequence[RequirementSpec],
    metadata: Mapping[str, Mapping[str, Any]],
    evidence_resolver: Callable[[str], Any] | None,
    forbid_shared: bool = False,
) -> tuple[tuple[EquivalencyBinding, ...], tuple[dict[str, Any], ...], tuple[str, ...], tuple[str, ...]]:
    bindings: list[EquivalencyBinding] = []
    projections: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for binding_id in sorted(set(_text(item) for item in evidence_ids if _text(item))):
        record = None
        if callable(evidence_resolver):
            try:
                record = evidence_resolver(binding_id)
            except Exception:
                record = None
        record_type = _text(record.get("record_type") or record.get("type")).upper() if isinstance(record, Mapping) else ""
        valid = (
            isinstance(record, Mapping)
            and _text(record.get("record_id")) == binding_id
            and record_type in {
                "EQUIVALENCY_RECORD",
                "EQUIVALENCY_BINDING_RECORD",
                "COURSE_EQUIVALENCY_RECORD",
                "CREDIT_TRANSFER_RECORD",
                "SHARED_CREDIT_RECORD",
            }
        )
        if valid:
            # An equivalency record is not approval merely because it has an
            # official-looking type and evidence reference.  The source
            # authority must explicitly issue the APPROVED decision.
            valid = (
                _text(record.get("evidence_state")).upper() == VERIFIED
                and _text(record.get("decision")).upper() == "APPROVED"
            )
        authority = _text(record.get("authority")) if isinstance(record, Mapping) else ""
        reference = _text(record.get("evidence_reference") or record.get("source_reference")) if isinstance(record, Mapping) else ""
        valid = valid and bool(authority) and bool(reference)
        source = _source_attempt_id(
            record.get("source_attempt_id") or record.get("source_course_id") or record.get("source_course_code"),
            attempts,
        ) if isinstance(record, Mapping) else None
        target = _target_requirement_id(
            record.get("target_requirement_id") or record.get("target_course_id") or record.get("target_course_code") or record.get("target_course_name"),
            requirements,
            metadata,
        ) if isinstance(record, Mapping) else None
        if isinstance(record, Mapping):
            # An explicit zero is a real authority decision: never fall back
            # to a legacy ``credits`` alias when ``approved_credits`` is
            # present, or an unresolved record could mint an approved amount.
            amount_value = record["approved_credits"] if "approved_credits" in record else record.get("credits")
            amount = _positive_number(amount_value)
        else:
            amount = Decimal("0")
        if not source or not target or amount <= 0:
            valid = False
        source_requirement_id = _text(record.get("source_requirement_id")) if isinstance(record, Mapping) else ""
        direction = _text(record.get("direction") or record.get("ledger")) if isinstance(record, Mapping) else ""
        source_owner = _text(record.get("source_owner") or record.get("source_role")) if isinstance(record, Mapping) else ""
        target_owner = _text(record.get("target_owner") or record.get("target_role")) if isinstance(record, Mapping) else ""
        source_domain = _text(record.get("source_domain")) if isinstance(record, Mapping) else ""
        target_domain = _text(record.get("target_domain")) if isinstance(record, Mapping) else ""
        allocation_kind = _text(record.get("allocation_kind")) if isinstance(record, Mapping) else ""
        raw_shared = record.get("shared", False) if isinstance(record, Mapping) else False
        shared_input_is_boolean = not isinstance(record, Mapping) or "shared" not in record or raw_shared is True or raw_shared is False
        normalized_direction = direction.upper().replace("-", "_").replace(" ", "_")
        normalized_allocation_kind = allocation_kind.upper().replace("-", "_").replace(" ", "_")
        explicit_shared_marker = normalized_direction in {"PRIMARY_TO_TARGET", "TARGET_TO_PRIMARY"} or normalized_allocation_kind in {SHARED_SHADOW, "SHARED_REUSE"}
        shared = shared_input_is_boolean and (
            raw_shared is True
            or (raw_shared is False and explicit_shared_marker)
        )
        if not shared_input_is_boolean:
            # A malformed shared flag must not be rescued by a truthy
            # direction/kind.  Strip those hints before constructing the
            # binding, so the dataclass cannot accidentally re-enable sharing.
            direction = ""
            allocation_kind = ""
        if shared and forbid_shared:
            # A minor may not project a primary allocation through a shadow
            # ledger.  Preserve the opaque evidence ID as a pending audit row
            # but never hand the binding to the allocator.
            projections.append(_binding_projection(binding_id, reason="輔系不得使用雙主修 shared/shadow credit。"))
            blockers.append("MINOR_SHARED_CREDIT_FORBIDDEN")
            warnings.append("MINOR_SHARED_CREDIT_FORBIDDEN")
            bindings.append(
                EquivalencyBinding(
                    binding_id=binding_id,
                    source_attempt_id="",
                    target_requirement_id="",
                    evidence_state=UNKNOWN,
                    decision="PENDING",
                )
            )
            continue
        if shared:
            # Shared credit is a projection from one exact allocated source
            # requirement.  Never infer source scope, direction, owner or
            # domain from the current compiler metadata.
            source_requirement = next((item for item in requirements if item.requirement_id == source_requirement_id), None)
            target_requirement = next((item for item in requirements if item.requirement_id == target), None)
            explicit_target_id = _text(record.get("target_requirement_id")) if isinstance(record, Mapping) else ""
            expected_source_owner = _text(source_requirement.owner) if source_requirement else ""
            expected_target_owner = _text(target_requirement.owner) if target_requirement else ""
            expected_source_domain = _text(source_requirement.domain) if source_requirement else ""
            expected_target_domain = _text(target_requirement.domain) if target_requirement else ""
            shared = bool(
                valid
                and source_requirement_id
                and source_requirement is not None
                and target_requirement is not None
                and explicit_target_id == target
                and source_requirement_id != target
                and direction in {"PRIMARY_TO_TARGET", "TARGET_TO_PRIMARY"}
                and source_owner
                and target_owner
                and source_domain
                and target_domain
                and source_owner.upper() == expected_source_owner.upper()
                and target_owner.upper() == expected_target_owner.upper()
                and source_domain == expected_source_domain
                and target_domain == expected_target_domain
            )
            if not shared:
                valid = False
        if not valid:
            reason = "equivalency evidence is unresolved or does not match a released attempt/requirement"
            projections.append(_binding_projection(binding_id, reason=reason))
            blockers.append(f"EQUIVALENCY_UNRESOLVED:{binding_id}")
            warnings.append(f"UNVERIFIED_EQUIVALENCY:{binding_id}")
            bindings.append(
                EquivalencyBinding(
                    binding_id=binding_id,
                    source_attempt_id="",
                    target_requirement_id="",
                    evidence_state=UNKNOWN,
                    decision="PENDING",
                )
            )
            continue
        attempt = next(item for item in attempts if item.attempt_id == source)
        binding = EquivalencyBinding(
            binding_id=binding_id,
            source_attempt_id=source,
            target_requirement_id=target,
            approved_credits=amount,
            evidence_state=VERIFIED,
            authority=authority,
            evidence_reference=reference,
            direction=direction,
            shared=shared,
            source_course_id=attempt.course_id,
            target_course_id=_text(record.get("target_course_id") or record.get("target_course_code")),
            source_course_kind=attempt.course_kind,
            allocation_kind=allocation_kind,
            decision=_text(record.get("decision") or "PENDING"),
            scope=_text(record.get("scope")),
            source_requirement_id=source_requirement_id,
            source_owner=source_owner,
            target_owner=target_owner,
            source_domain=source_domain,
            target_domain=target_domain,
        )
        bindings.append(binding)
        projections.append(
            {
                "binding_id": binding.binding_id,
                "source_attempt_id": binding.source_attempt_id,
                "target_requirement_id": binding.target_requirement_id,
                "approved_credits": str(binding.approved_credits),
                "evidence_state": binding.evidence_state,
                "authority": binding.authority,
                "evidence_reference": binding.evidence_reference,
                "direction": binding.direction,
                "shared": binding.shared,
                "decision": binding.decision,
                "allocation_kind": binding.allocation_kind,
                "source_requirement_id": binding.source_requirement_id,
                "source_owner": binding.source_owner,
                "target_owner": binding.target_owner,
                "source_domain": binding.source_domain,
                "target_domain": binding.target_domain,
                "reason": "trusted opaque evidence resolved",
            }
        )
    return tuple(bindings), tuple(sorted(projections, key=lambda item: str(item.get("binding_id", "")))), tuple(blockers), tuple(warnings)


def _requirement_status(allocation: AllocationResult, requirement_ids: Sequence[str]) -> str:
    selected = [item for item in allocation.requirement_results if item.requirement_id in set(requirement_ids) and item.status != NOT_APPLICABLE]
    if not selected:
        return UNKNOWN
    if any(item.status == UNKNOWN for item in selected):
        return UNKNOWN
    if any(item.status == FAIL for item in selected):
        return FAIL
    return PASS


def _with_generic_unknown(allocation: AllocationResult, generic_ids: set[str]) -> AllocationResult:
    if not generic_ids:
        return allocation
    results: list[RequirementResult] = []
    blockers = list(allocation.blockers)
    generic_present = False
    for item in allocation.requirement_results:
        if item.requirement_id in generic_ids and item.status != NOT_APPLICABLE:
            generic_present = True
            code = f"RULE_NOT_IMPLEMENTED:{item.requirement_id}"
            legacy_code = f"REQUIREMENT_CATALOG_PARTIAL:{item.requirement_id}"
            for blocker in (code, legacy_code):
                if blocker not in blockers:
                    blockers.append(blocker)
            results.append(
                replace(
                    item,
                    status=UNKNOWN,
                    blockers=tuple(dict.fromkeys((*item.blockers, code, legacy_code))),
                )
            )
        else:
            results.append(item)
    if not generic_present:
        return allocation
    return replace(
        allocation,
        requirement_results=tuple(results),
        status=UNKNOWN,
        feasibility=UNKNOWN,
        feasible_witness=False,
        pass_eligible=False,
        blockers=tuple(blockers),
    )


def _combine_status(*statuses: str) -> str:
    statuses = tuple(status for status in statuses if status not in {NOT_APPLICABLE, ""})
    if FAIL in statuses:
        return FAIL
    if UNKNOWN in statuses:
        return UNKNOWN
    return PASS if statuses else UNKNOWN


def _safe_status(value: Any) -> str:
    normalized = _text(value).upper()
    return normalized if normalized in {PASS, FAIL, UNKNOWN, NOT_APPLICABLE} else UNKNOWN


def _self_report_status(value: Any) -> str:
    """Project a user claim for display without treating it as evidence."""
    # Keep this helper fail-closed even if a future caller reuses it.  The
    # human-readable claim belongs in ``claimed_state`` and can never become
    # an official PASS/FAIL decision without scoped registrar evidence.
    _ = value
    return UNKNOWN


def _self_report_claimed_state(value: Any) -> str:
    """Return a localized claim label that can never look like an official gate."""

    text = _text(value)
    if not text:
        return "使用者未提供申請狀態自述"
    normalized = text.upper().replace(" ", "")
    if any(token in normalized for token in ("REJECT", "DENY", "未核准", "未取得", "否")):
        return "使用者自述：未核准／未取得"
    if any(token in normalized for token in ("PASS", "APPROV", "已申請", "已核准", "已取得", "QUALIFIED", "GRANTED")):
        return "使用者自述：已申請／已核准"
    return f"使用者自述：{text}"


def _minor_coursework_blockers(
    target: Mapping[str, Any] | None,
    attempts: Sequence[CourseAttempt],
    allocation: AllocationResult,
) -> tuple[str, ...]:
    """Return only source-backed minor gates that allocation cannot encode."""

    if not isinstance(target, Mapping):
        return ()
    blockers: list[str] = []
    if bool(target.get("zero_credit_gate")):
        blockers.append("MINOR_ZERO_CREDIT_GATE_MANUAL_REVIEW")
    conflicted_names = {
        _course_label(item)
        for item in target.get("conflicted_course_names", ())
        if _course_label(item)
    }
    attempts_by_id = {attempt.attempt_id: attempt for attempt in attempts}
    conflict_used = any(
        _course_label(attempts_by_id.get(item.attempt_id).course_name) in conflicted_names
        and item.allocation_kind == "EXCLUSIVE"
        and _positive_number(item.credits) > 0
        for allocation_item in allocation.allocations
        for item in allocation_item.portions
        if attempts_by_id.get(item.attempt_id) is not None
    )
    if conflicted_names and conflict_used:
        blockers.append("MINOR_SOURCE_CONFLICT_USED")
    return tuple(dict.fromkeys(blockers))


def _statistics(
    attempts: Sequence[CourseAttempt],
    requirements: Sequence[RequirementSpec],
    allocation: AllocationResult,
    *,
    decisions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return build_statistics_v2(attempts, requirements, allocation, decisions=decisions)


def _evidence_projection(
    request: EvaluationRequest,
    *,
    rule_resolution: Mapping[str, Any],
    application: Mapping[str, Any] | None,
    bindings: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for item in request.official_evidence_ids:
        result.append({"evidence_id": item, "kind": "official", "resolution_state": "UNRESOLVED"})
    for evidence_field in _EVIDENCE_FIELDS:
        item = _text(getattr(request, evidence_field))
        if item:
            result.append({"evidence_id": item, "kind": evidence_field.removesuffix("_evidence_id"), "resolution_state": "UNRESOLVED"})
    for item in request.equivalency_evidence_ids:
        result.append({"evidence_id": item, "kind": "equivalency", "resolution_state": "RESOLVED" if any(binding.get("binding_id") == item and binding.get("evidence_state") == VERIFIED for binding in bindings) else "UNRESOLVED"})
    target_dimension = rule_resolution.get("dimensions", {}).get("target_curriculum_version", {}) if isinstance(rule_resolution.get("dimensions"), Mapping) else {}
    target_evidence = _text(target_dimension.get("evidence_reference"))
    if target_evidence:
        result.append({"evidence_id": target_evidence, "kind": "target_applicability", "resolution_state": "RESOLVED" if target_dimension.get("status") == "RESOLVED" else "UNRESOLVED"})
    if isinstance(application, Mapping):
        for item in application.get("evidence_ids", ()):
            evidence_id = _text(item)
            if evidence_id:
                result.append({"evidence_id": evidence_id, "kind": "application", "resolution_state": "RESOLVED"})
    unique = {(item["evidence_id"], item["kind"]): item for item in result}
    return tuple(unique[key] for key in sorted(unique))


def evaluate(
    request: EvaluationRequest,
    *,
    evidence_resolver: Callable[[str], Any] | None = None,
) -> DecisionSnapshot:
    """Evaluate one confirmed request and return the sole canonical snapshot."""

    if not isinstance(request, EvaluationRequest):
        raise TypeError("evaluate expects an EvaluationRequest")

    released_rows = _released_rows(request)
    input_state = request.confirmation_state
    input_blockers: list[str] = []
    if not released_rows and request.confirmed_course_rows:
        input_blockers.append("INPUT_CONFIRMATION_REQUIRED")
        input_state = "STALE" if request.transcript_confirmed and request.confirmation_state == ConfirmationState.CONFIRMED.value else input_state
    elif not request.transcript_confirmed or request.confirmation_state != ConfirmationState.CONFIRMED.value:
        input_blockers.append("INPUT_CONFIRMATION_REQUIRED")
    input_confirmation = {
        "state": input_state,
        "transcript_confirmed": request.transcript_confirmed,
        "fingerprint": request.confirmed_course_fingerprint,
        "confirmed_fingerprint": request.confirmed_course_fingerprint
        if released_rows or (not request.confirmed_course_rows and input_state == ConfirmationState.CONFIRMED.value)
        else None,
        "row_count": len(request.confirmed_course_rows),
        "released_row_count": len(released_rows),
        "cardinality_consistent": len(released_rows) <= len(request.confirmed_course_rows),
        "release_allowed": bool(
            released_rows
            or (
                request.transcript_confirmed
                and not request.confirmed_course_rows
                and request.confirmation_state == ConfirmationState.CONFIRMED.value
            )
        ),
    }

    primary: Mapping[str, Any] | None = None
    target: Mapping[str, Any] | None = None
    registry_blockers: list[str] = []
    try:
        if request.primary_curriculum_id:
            primary = get_curriculum(request.primary_curriculum_id)
        else:
            registry_blockers.append("PRIMARY_CURRICULUM_MISSING")
    except (KeyError, TypeError, ValueError):
        registry_blockers.append("PRIMARY_CURRICULUM_UNRESOLVED")
    secondary_kind = _secondary_kind(request.secondary_kind, request.program_type)
    is_minor = secondary_kind == "minor"
    is_double = secondary_kind == "double_major"
    target_candidate = request.target_version
    if not target_candidate and (is_minor or is_double):
        year = request.target_curriculum_year or request.admission_cohort
        prog = _program_slug(request.target_program)
        track = _track_slug(request.target_track, prog)
        if year and prog:
            candidate_id = (
                f"minor:{year}:{prog}:{track}" if (is_minor and track)
                else f"minor:{year}:{prog}" if is_minor
                else f"target:double_major:{year}:{prog}:{track}" if track
                else f"target:double_major:{year}:{prog}"
            )
            if candidate_id in list_curriculum_ids():
                target_candidate = candidate_id
    target_prefixes = ("minor:",) if is_minor else ("target:",) if is_double else ()
    if target_candidate and target_prefixes and target_candidate.startswith(target_prefixes):
        try:
            target = get_curriculum(target_candidate)
        except (KeyError, TypeError, ValueError):
            registry_blockers.append("TARGET_CURRICULUM_UNRESOLVED")
    context_request = _context_request(request, primary, target)
    try:
        raw_rule_resolution = resolve_rule_context(context_request, evidence_resolver=evidence_resolver)
    except Exception:
        raw_rule_resolution = {"status": UNKNOWN, "state": UNKNOWN, "can_pass": False, "blocker_codes": ["RULE_CONTEXT_UNRESOLVED"], "blockers": []}
    rule_resolution = _safe_rule_resolution(raw_rule_resolution)
    primary_from_context = raw_rule_resolution.get("primary_curriculum") if isinstance(raw_rule_resolution, Mapping) else None
    target_from_context = raw_rule_resolution.get("target_curriculum") if isinstance(raw_rule_resolution, Mapping) else None
    if isinstance(primary_from_context, Mapping):
        primary = primary_from_context
    if isinstance(target_from_context, Mapping):
        target = target_from_context
    if primary is None:
        registry_blockers.append("PRIMARY_CURRICULUM_UNRESOLVED")
    primary_specs, primary_meta, primary_provenance = _compile_requirements(primary, scope="primary")
    target_scope = "minor" if is_minor else "target"
    target_specs, target_meta, target_provenance = _compile_requirements(target, scope=target_scope) if (is_minor or is_double) else ((), {}, ())
    requirements = tuple(sorted((*primary_specs, *target_specs), key=lambda item: item.requirement_id))
    metadata = {**primary_meta, **target_meta}
    provenance = tuple(
        sorted(
            (
                *primary_provenance,
                *target_provenance,
                *_curriculum_provenance(primary, scope="primary"),
                *_curriculum_provenance(target, scope=target_scope),
            ),
            key=lambda item: (
                str(item.get("requirement_id", "")),
                str(item.get("assertion_id", item.get("id", ""))),
                str(item.get("scope", "")),
            ),
        )
    )
    completion_metadata: list[Mapping[str, Any]] = []
    for curriculum in (primary, target):
        safe_curriculum = _safe_curriculum(curriculum)
        if not isinstance(safe_curriculum, Mapping):
            continue
        raw_completion_rows = safe_curriculum.get("non_credit_requirements", ())
        if isinstance(raw_completion_rows, Sequence) and not isinstance(raw_completion_rows, (str, bytes, bytearray)):
            completion_metadata.extend(
                item for item in raw_completion_rows if isinstance(item, Mapping)
            )
    attempts, _safe_rows = _compile_attempts(
        released_rows,
        metadata,
        completion_metadata=tuple(completion_metadata),
    )
    from confirmed_rules import route_confirmed_attempts
    attempts = route_confirmed_attempts(request.primary_curriculum_id, attempts, requirements)
    non_credit_waivers = _resolve_non_credit_waivers(request, evidence_resolver)
    primary_non_credit_results = _compile_non_credit_results(
        primary,
        attempts,
        scope="primary",
        waiver_records=non_credit_waivers,
        subject_ref=request.subject_ref,
    )
    target_non_credit_results = _compile_non_credit_results(
        target,
        attempts,
        scope=target_scope,
        waiver_records=non_credit_waivers,
        subject_ref=request.subject_ref,
    ) if (is_minor or is_double) else ()
    primary_non_credit_status = _non_credit_status(primary_non_credit_results)
    target_non_credit_status = _non_credit_status(target_non_credit_results)
    binding_values = request.equivalency_evidence_ids
    bindings, binding_projections, binding_blockers, binding_warnings = _compile_bindings(
        binding_values,
        attempts=attempts,
        requirements=requirements,
        metadata=metadata,
        evidence_resolver=evidence_resolver,
        forbid_shared=is_minor,
    )
    minor_shared_blockers: list[str] = []
    if is_minor and any(binding.shared for binding in bindings):
        # A minor has no shared/shadow-credit path.  Keep the evidence row for
        # audit, but remove it from the allocator rather than letting a
        # malformed cross-curriculum binding create credit from nowhere.
        minor_shared_blockers.append("MINOR_SHARED_CREDIT_FORBIDDEN")
        binding_warnings = (*binding_warnings, "MINOR_SHARED_CREDIT_FORBIDDEN")
        bindings = tuple(binding for binding in bindings if not binding.shared)
        binding_projections = tuple(
            {
                **dict(item),
                "decision": "PENDING",
                "shared": False,
                "reason": "輔系不得使用雙主修 shared/shadow credit。",
            }
            if item.get("shared")
            else item
            for item in binding_projections
        )
    allocation = allocate_credits(attempts, requirements, bindings, search_limit=request.search_limit)
    generic_ids = {
        rid
        for rid, meta in metadata.items()
        if meta.get("unresolved_catalog") is True
    }
    allocation = _with_generic_unknown(allocation, generic_ids)

    application: Mapping[str, Any] | None = None
    formal_award: Mapping[str, Any] | None = None
    if is_double:
        app_request = {
            "application_term": request.resolved_application_term,
            "target_program": request.target_program or (target or {}).get("program_slug"),
            "target_track": request.target_track or (target or {}).get("track_slug"),
            "admission_cohort": request.admission_cohort,
            "application_status": request.application_status,
            "school_approval_status": request.school_approval_status,
            "formal_qualification_status": request.formal_qualification_status,
            "submitted_at": request.submitted_at,
            "application_event_evidence_id": request.application_event_evidence_id,
            "subject_ref": request.subject_ref,
        }
        try:
            application = resolve_application_case(
                app_request,
                notice_records=request.notice_evidence_ids or None,
                rule_applicability=request.rule_applicability_evidence_id,
                department_decision=request.department_decision_evidence_id,
                registrar_registration=request.registrar_registration_evidence_id,
                formal_qualification=request.formal_qualification_evidence_id,
                submitted_at=request.submitted_at,
                application_event_evidence_id=request.application_event_evidence_id,
                subject_ref=request.subject_ref,
                as_of=request.as_of,
                evidence_resolver=evidence_resolver,
            )
        except Exception:
            application = {"status": UNKNOWN, "state": UNKNOWN, "can_pass": False, "blockers": [{"code": "APPLICATION_RESOLUTION_UNKNOWN", "reason": "官方申請證據無法解析。"}]}
        try:
            formal_award = resolve_formal_award(
                request.formal_award_evidence_id,
                evidence_resolver=evidence_resolver,
                target_program=request.target_program or (target or {}).get("program_slug"),
                target_track=request.target_track or (target or {}).get("track_slug"),
                subject_ref=request.subject_ref,
            )
        except Exception:
            formal_award = {"status": UNKNOWN, "state": UNKNOWN, "award_state": UNKNOWN, "is_official": False}
    elif is_minor:
        minor_app_request = {
            "application_term": request.resolved_application_term,
            "target_program": request.target_program or (target or {}).get("program_slug"),
            "target_track": request.target_track or (target or {}).get("track_slug"),
            "application_status": request.application_status,
            "subject_ref": request.subject_ref,
        }
        try:
            application = resolve_minor_application_case(
                minor_app_request,
                department_approval=request.department_decision_evidence_id,
                registrar_registration=request.registrar_registration_evidence_id,
                formal_qualification=request.formal_qualification_evidence_id,
                evidence_resolver=evidence_resolver,
            )
        except Exception:
            application = {
                "status": UNKNOWN,
                "state": UNKNOWN,
                "can_pass": False,
                "blockers": [{"code": "MINOR_APPLICATION_RESOLUTION_UNKNOWN", "reason": "官方輔系申請證據無法解析。"}],
            }
        try:
            formal_award = resolve_minor_award(
                request.formal_award_evidence_id,
                evidence_resolver=evidence_resolver,
                target_program=request.target_program or (target or {}).get("program_slug"),
                target_track=request.target_track or (target or {}).get("track_slug"),
                subject_ref=request.subject_ref,
                application_term=request.resolved_application_term,
            )
        except Exception:
            formal_award = {"status": UNKNOWN, "state": UNKNOWN, "award_state": UNKNOWN, "is_official": False}

    safe_application = _safe_application(application)
    safe_formal_award = _safe_gate(formal_award) if formal_award is not None else {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "award_state": NOT_APPLICABLE, "is_official": False}
    rule_status = _text(raw_rule_resolution.get("status")) if isinstance(raw_rule_resolution, Mapping) else UNKNOWN
    primary_dimension = raw_rule_resolution.get("dimensions", {}).get("primary_curriculum", {}) if isinstance(raw_rule_resolution.get("dimensions"), Mapping) else {}
    target_dimension = raw_rule_resolution.get("dimensions", {}).get("target_curriculum_version", {}) if isinstance(raw_rule_resolution.get("dimensions"), Mapping) else {}
    primary_status = _requirement_status(allocation, [item.requirement_id for item in primary_specs])
    target_status = _requirement_status(allocation, [item.requirement_id for item in target_specs]) if target_specs else UNKNOWN
    primary_requirement_ids = {item.requirement_id for item in primary_specs}
    target_requirement_ids = {item.requirement_id for item in target_specs}
    subset_status_by_requirement = {
        _text(item.get("requirement_id")): _text(item.get("status"))
        for item in allocation.subset_results
        if isinstance(item, Mapping) and _text(item.get("requirement_id"))
    }

    def scope_allocation_uncertain(requirement_ids: set[str]) -> bool:
        if not requirement_ids:
            return False
        result_unknown = any(
            item.requirement_id in requirement_ids and item.status == UNKNOWN
            for item in allocation.requirement_results
        )
        subset_unknown = any(
            requirement_id in requirement_ids and status == UNKNOWN
            for requirement_id, status in subset_status_by_requirement.items()
        )
        return result_unknown or subset_unknown

    # Allocation ambiguity is a reportable route property.  It becomes a
    # blocker only when no complete witness exists; a verified witness may be
    # shown even when another equally valid assignment remains.
    search_unresolved = not allocation.search_complete and not allocation.feasible_witness
    primary_allocation_uncertain = search_unresolved or scope_allocation_uncertain(primary_requirement_ids)
    target_all_satisfied = bool(target_requirement_ids) and all(
        item.status == PASS for item in allocation.requirement_results if item.requirement_id in target_requirement_ids
    )
    target_allocation_uncertain = (
        scope_allocation_uncertain(target_requirement_ids)
        or (search_unresolved and not target_all_satisfied)
    )
    allocation_uncertain = (
        search_unresolved
        or primary_allocation_uncertain
        or target_allocation_uncertain
        or allocation.status == UNKNOWN
    )
    if not released_rows or input_blockers or registry_blockers or rule_status in {UNKNOWN, MISSING, MANUAL_REVIEW, CONFLICTED} or primary_dimension.get("status") != "RESOLVED":
        primary_status = UNKNOWN
    elif primary_allocation_uncertain:
        primary_status = UNKNOWN
    if primary_status != UNKNOWN:
        if primary_non_credit_status == FAIL:
            primary_status = FAIL
        elif primary_non_credit_status == UNKNOWN:
            primary_status = UNKNOWN
    primary_decision = {
        "status": primary_status,
        "state": primary_status,
        "can_pass": primary_status == PASS,
        "allocation_status": allocation.status,
        "requirement_ids": tuple(item.requirement_id for item in primary_specs),
        "non_credit_status": primary_non_credit_status,
        "non_credit_results": tuple(item.as_dict() for item in primary_non_credit_results),
        "subset_results": tuple(
            item for item in allocation.subset_results
            if isinstance(item, Mapping) and _text(item.get("requirement_id")) in primary_requirement_ids
        ),
        "reason": "主修課表、確認輸入與全域配置均可安全判定。" if primary_status == PASS else "主修規則、課程池、輸入確認或配置結果仍有未決條件。",
    }
    primary_total = _positive_number((primary or {}).get("total_required"))
    primary_version = _text(
        (primary or {}).get("curriculum_version")
        or (primary or {}).get("version")
    )
    primary_total_source = _text((primary or {}).get("source_reference"))
    primary_total_evidence = _evidence((primary or {}).get("evidence_state"))
    primary_total_coverage = _coverage((primary or {}).get("coverage_state"))
    if (
        isinstance(primary, Mapping)
        and primary_dimension.get("status") == "RESOLVED"
        and primary_version
        and primary_total > 0
        and primary_total_source
        and primary_total_evidence == VERIFIED
        and primary_total_coverage == COMPLETE
    ):
        primary_decision["total_credit_requirement"] = {
            "required_credits": primary_total,
            "curriculum_id": _text(primary.get("curriculum_id")),
            "curriculum_version": primary_version,
            "evidence_state": VERIFIED,
            "coverage_state": primary_total_coverage,
            "source_reference": primary_total_source,
            "authority": "PRIMARY_CURRICULUM_REGISTRY",
            "non_consuming": True,
        }
    application_self_report = {
        # A self-report is deliberately never a PASS/FAIL decision.  Keep
        # the claimed state separately so a UI can explain what the user
        # entered without rendering it as an official approval badge.
        "status": UNKNOWN,
        "state": UNKNOWN,
        "authoritative": False,
        "value": request.application_status,
        "claimed_state": _self_report_claimed_state(request.application_status),
        "reason": "這是使用者自述，不能升級任何官方資格或授予 gate。",
    }
    minor_application: dict[str, Any] = {
        "status": NOT_APPLICABLE,
        "state": NOT_APPLICABLE,
        "can_pass": False,
        "self_report": application_self_report,
        "reason": "目前規劃不是輔系。",
    }
    minor_decision: dict[str, Any] = {
        "status": NOT_APPLICABLE,
        "state": NOT_APPLICABLE,
        "can_pass": False,
        "reason": "目前規劃不是輔系。",
    }
    formal_minor_award: dict[str, Any] = {
        "status": NOT_APPLICABLE,
        "state": NOT_APPLICABLE,
        "can_pass": False,
        "award_state": NOT_APPLICABLE,
        "reason": "目前規劃不是輔系。",
    }
    if not (is_double or is_minor):
        double_decision: dict[str, Any] = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False, "reason": "目前規劃不是雙主修。"}
        formal_decision: dict[str, Any] = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False, "award_state": NOT_APPLICABLE, "reason": "目前規劃不是雙主修。"}
        department_approval = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False, "authoritative": True}
        registrar_registration = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False, "authoritative": True}
        formal_qualification = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False, "authoritative": True}
        target_coursework = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False}
        award_eligibility = {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False}
    elif is_minor:
        minor_rule_blockers = _minor_coursework_blockers(target, attempts, allocation)
        if (
            target_dimension.get("status") != "RESOLVED"
            or target_allocation_uncertain
            or input_blockers
            or minor_rule_blockers
        ):
            target_status = UNKNOWN
        elif target_non_credit_status == FAIL:
            target_status = FAIL
        elif target_non_credit_status == UNKNOWN:
            target_status = UNKNOWN
        app_status = _safe_status(safe_application.get("status"))
        department_status = _safe_status(safe_application.get("department_decision", {}).get("status"))
        registrar_status = _safe_status(safe_application.get("registration", {}).get("status"))
        qualification_status = _safe_status(safe_application.get("formal_qualification", {}).get("status"))
        department_approval = {
            **safe_application.get("department_decision", {}),
            "status": department_status,
            "state": department_status,
            "can_pass": department_status == PASS,
            "authoritative": True,
        }
        registrar_registration = {
            **safe_application.get("registration", {}),
            "status": registrar_status,
            "state": registrar_status,
            "can_pass": registrar_status == PASS,
            "authoritative": True,
        }
        formal_qualification = {
            **safe_application.get("formal_qualification", {}),
            "status": qualification_status,
            "state": qualification_status,
            "can_pass": qualification_status == PASS and safe_application.get("formal_qualification", {}).get("is_official") is True,
            "authoritative": True,
        }
        # ``minor_application_or_qualification`` only requires the official
        # department approval + registrar registration chain.  A self-report
        # and coursework completion are kept as independent dimensions.
        minor_application = {
            "status": _combine_status(department_status, registrar_status),
            "state": _combine_status(department_status, registrar_status),
            "can_pass": department_status == PASS and registrar_status == PASS,
            "self_report": application_self_report,
            "application": safe_application,
            "department_approval": department_approval,
            "registrar_registration": registrar_registration,
            "formal_qualification": formal_qualification,
            "requirement": "系所正式核准 + 教務處正式登錄；自述不具核准效力。",
            "reason": "系所核准與教務登錄均已核實。" if department_status == PASS and registrar_status == PASS else "輔系申請／正式修讀資格仍缺官方核准鏈。",
        }
        if minor_rule_blockers:
            minor_application["blockers"] = tuple(minor_rule_blockers)
        target_coursework = {
            "status": target_status,
            "state": target_status,
            "can_pass": target_status == PASS,
            "requirement_ids": tuple(item.requirement_id for item in target_specs),
            "non_credit_status": target_non_credit_status,
            "non_credit_results": tuple(item.as_dict() for item in target_non_credit_results),
            "subset_results": tuple(
                item for item in allocation.subset_results
                if isinstance(item, Mapping) and _text(item.get("requirement_id")) in target_requirement_ids
            ),
            "blockers": tuple(dict.fromkeys((*minor_rule_blockers, *(blocker for item in target_non_credit_results for blocker in item.blockers)))),
            "reason": "輔系目標課程配置可安全判定。" if target_status == PASS else "輔系課表、課程配置或年度 gate 仍需確認。",
        }
        award_eligibility_status = _combine_status(minor_application["status"], target_status)
        award_eligibility = {
            "status": award_eligibility_status,
            "state": award_eligibility_status,
            "can_pass": award_eligibility_status == PASS,
            "requires": ("minor_application_or_qualification", "minor_coursework_completion"),
            "reason": "輔系官方修讀資格與課程均已核實。" if award_eligibility_status == PASS else "輔系官方修讀資格或課程仍未全部核實。",
        }
        minor_decision_status = _combine_status(minor_application["status"], target_status)
        double_decision = {
            "status": NOT_APPLICABLE,
            "state": NOT_APPLICABLE,
            "can_pass": False,
            "reason": "目前規劃是輔系，雙主修決策不適用。",
        }
        formal_decision = {
            "status": NOT_APPLICABLE,
            "state": NOT_APPLICABLE,
            "can_pass": False,
            "award_state": NOT_APPLICABLE,
            "reason": "目前規劃是輔系，正式雙主修授予不適用。",
        }
        minor_decision = {
            "status": minor_decision_status,
            "state": minor_decision_status,
            "can_pass": minor_decision_status == PASS,
            "coursework_status": target_status,
            "non_credit_status": target_non_credit_status,
            "non_credit_results": tuple(item.as_dict() for item in target_non_credit_results),
            "application": minor_application,
            "requirement_ids": tuple(item.requirement_id for item in target_specs),
            "reason": "輔系課程與正式修讀資格均已核實。" if minor_decision_status == PASS else "輔系課程、年度規則或官方修讀資格仍需確認。",
        }
        formal_minor_status = _safe_status(safe_formal_award.get("status"))
        formal_minor_award = {
            "status": formal_minor_status,
            "state": formal_minor_status,
            "can_pass": formal_minor_status == PASS and safe_formal_award.get("is_official") is True,
            "award_state": safe_formal_award.get("award_state", UNKNOWN),
            "formal_award": safe_formal_award,
            "reason": "官方正式授予輔系紀錄已核實。" if formal_minor_status == PASS else "正式授予輔系需要獨立的教務處官方紀錄。",
        }
    else:
        if target_dimension.get("status") != "RESOLVED" or rule_status in {UNKNOWN, MISSING, MANUAL_REVIEW, CONFLICTED} or target_allocation_uncertain or input_blockers:
            target_status = UNKNOWN
        elif target_non_credit_status == FAIL:
            target_status = FAIL
        elif target_non_credit_status == UNKNOWN:
            target_status = UNKNOWN
        app_status = _safe_status(safe_application.get("status"))
        department_status = _safe_status(safe_application.get("department_decision", {}).get("status"))
        registrar_status = _safe_status(safe_application.get("registration", {}).get("status"))
        qualification_status = _safe_status(safe_application.get("formal_qualification", {}).get("status"))
        department_approval = {
            **safe_application.get("department_decision", {}),
            "status": department_status,
            "state": department_status,
            "can_pass": department_status == PASS,
            "authoritative": True,
        }
        registrar_registration = {
            **safe_application.get("registration", {}),
            "status": registrar_status,
            "state": registrar_status,
            "can_pass": registrar_status == PASS,
            "authoritative": True,
        }
        formal_qualification = {
            **safe_application.get("formal_qualification", {}),
            "status": qualification_status,
            "state": qualification_status,
            "can_pass": qualification_status == PASS and safe_application.get("formal_qualification", {}).get("is_official") is True,
            "authoritative": True,
        }
        target_coursework = {
            "status": target_status,
            "state": target_status,
            "can_pass": target_status == PASS,
            "requirement_ids": tuple(item.requirement_id for item in target_specs),
            "non_credit_status": target_non_credit_status,
            "non_credit_results": tuple(item.as_dict() for item in target_non_credit_results),
            "subset_results": tuple(
                item for item in allocation.subset_results
                if isinstance(item, Mapping) and _text(item.get("requirement_id")) in target_requirement_ids
            ),
            "reason": "雙主修目標課程配置可安全判定。" if target_status == PASS else "雙主修目標課表或課程配置仍需確認。",
        }
        eligibility_status = _combine_status(qualification_status, target_status, department_status, registrar_status)
        award_eligibility = {
            "status": eligibility_status,
            "state": eligibility_status,
            "can_pass": eligibility_status == PASS,
            "requires": ("formal_qualification", "target_coursework_completion", "department_approval", "registrar_registration"),
            "reason": "正式資格、目標課程與官方登錄 gate 均已核實。" if eligibility_status == PASS else "正式資格、目標課程或官方登錄 gate 尚未全部核實。",
        }
        double_status = _combine_status(target_status, app_status)
        double_decision = {
            "status": double_status,
            "state": double_status,
            "can_pass": double_status == PASS,
            "coursework_status": target_status,
            "non_credit_status": target_non_credit_status,
            "non_credit_results": tuple(item.as_dict() for item in target_non_credit_results),
            "application": safe_application,
            "requirement_ids": tuple(item.requirement_id for item in target_specs),
            "reason": "雙主修目標課表、課程配置與官方申請 gates 均已核實。" if double_status == PASS else "雙主修課表版本、申請核准或課程配置仍需確認。",
        }
        formal_status = _safe_status(safe_formal_award.get("status"))
        formal_decision = {
            "status": formal_status,
            "state": formal_status,
            "can_pass": formal_status == PASS and safe_formal_award.get("is_official") is True,
            "award_state": safe_formal_award.get("award_state", UNKNOWN),
            "formal_award": safe_formal_award,
            "reason": "官方正式授予紀錄已核實。" if formal_status == PASS else "正式授予雙主修需要獨立的教務處官方紀錄。",
        }
    secondary_decision = minor_decision if is_minor else double_decision
    overall_status = _combine_status(primary_status, secondary_decision["status"])
    if input_blockers or registry_blockers or binding_blockers or minor_shared_blockers or allocation_uncertain:
        overall_status = UNKNOWN
    decisions = {
        "primary_graduation": primary_decision,
        "double_major_qualification": double_decision,
        "formal_double_major_award": formal_decision,
        "application_self_report": application_self_report,
        "department_approval": department_approval,
        "registrar_registration": registrar_registration,
        "formal_qualification": formal_qualification,
        "target_coursework_completion": target_coursework,
        "minor_application_or_qualification": minor_application,
        "minor_coursework_completion": target_coursework if is_minor else {"status": NOT_APPLICABLE, "state": NOT_APPLICABLE, "can_pass": False, "reason": "目前規劃不是輔系。"},
        "formal_minor_award": formal_minor_award,
        "award_eligibility": award_eligibility,
        "formal_award": formal_minor_award if is_minor else formal_decision,
        "non_credit_results": {
            "primary": tuple(item.as_dict() for item in primary_non_credit_results),
            "target": tuple(item.as_dict() for item in target_non_credit_results),
        },
        "subset_results": tuple(item for item in allocation.subset_results),
        "overall": {
            "status": overall_status,
            "state": overall_status,
            "can_pass": overall_status == PASS,
            "reason": "所有適用決策均可安全判定。" if overall_status == PASS else "至少一項必要規則、輸入、配置或官方證據尚未安全判定。",
        },
    }
    context_blockers = []
    for code in raw_rule_resolution.get("blocker_codes", ()) if isinstance(raw_rule_resolution, Mapping) else ():
        code_text = _text(code)
        if code_text:
            context_blockers.append(f"RULE_CONTEXT:{code_text}")
    non_credit_blockers = tuple(
        blocker
        for result in (*primary_non_credit_results, *target_non_credit_results)
        for blocker in result.blockers
    )
    blockers = tuple(dict.fromkeys((*input_blockers, *registry_blockers, *context_blockers, *binding_blockers, *minor_shared_blockers, *non_credit_blockers, *(f"APPLICATION:{item.get('code') or item.get('status')}" for item in safe_application.get("blockers", ()) if isinstance(item, Mapping) and (item.get("code") or item.get("status"))), *allocation.blockers)))
    warnings = tuple(
        dict.fromkeys(
            (
                *request.input_warning_codes,
                *binding_warnings,
                *(_text(item) for item in raw_rule_resolution.get("warnings", ()) if _text(item)),
                *allocation.warnings,
            )
        )
    )
    statistics = _statistics(attempts, requirements, allocation, decisions=decisions)
    input_request = request.as_dict()
    safe_request = dict(input_request)
    # Request rows are already normalized, but they are available in the
    # allocation attempts as the canonical course view.  Keep the request
    # projection small enough for exports and exclude any caller extensions.
    safe_request["confirmed_course_rows"] = tuple(
        {
            key: item[key]
            for key in item
            if key in {"course_code", "course_name", "credits", "earned_credits", "status", "term", "grade", "course_type"}
        }
        for item in input_request.get("confirmed_course_rows", ())
        if isinstance(item, Mapping)
    )
    curriculum = {
        "primary": _safe_curriculum(primary),
        "target": _safe_curriculum(target),
    }
    safe_rule_provenance = tuple(provenance)
    evidence = _evidence_projection(request, rule_resolution=raw_rule_resolution, application=application, bindings=binding_projections)
    remediation = tuple(statistics.get("safe_remediation_directions", ()))
    initial = DecisionSnapshot(
        snapshot_id="",
        evaluated_at=request.as_of or "UNSPECIFIED",
        request=_freeze(safe_request),
        attempts=attempts,
        requirements=requirements,
        bindings=tuple(binding_projections),
        curriculum=_freeze(curriculum),
        rule_resolution=_freeze(rule_resolution),
        evidence=tuple(_freeze(item) for item in evidence),
        allocation=allocation,
        verdict=overall_status,
        blockers=blockers,
        warnings=warnings,
        schema_version=SERVICE_SCHEMA_VERSION,
        engine_version=ENGINE_VERSION,
        decisions=_freeze(decisions),
        rule_provenance=tuple(_freeze(item) for item in safe_rule_provenance),
        input_confirmation=_freeze(input_confirmation),
        search_complete=not allocation.search_exhausted,
        optimality=allocation.optimality,
        allocation_ambiguous=allocation.allocation_ambiguous,
        feasibility=allocation.feasibility,
        feasible_witness=allocation.feasible_witness,
        route_ambiguity=allocation.route_ambiguity,
        decision_ambiguity=allocation.decision_ambiguity,
        alternatives=allocation.alternative_allocations,
        statistics=_freeze(statistics),
        remediation_suggestions=remediation,
        non_credit_results=tuple(
            item.as_dict()
            for item in (*primary_non_credit_results, *target_non_credit_results)
        ),
        subset_results=allocation.subset_results,
    )
    canonical = _plain(initial.as_dict())
    canonical.pop("snapshot_id", None)
    snapshot_id = f"snapshot:{_digest(canonical)}"
    return replace(initial, snapshot_id=snapshot_id)


__all__ = [
    "ENGINE_VERSION",
    "SERVICE_SCHEMA_VERSION",
    "INPUT_WARNING_CODES",
    "EvaluationRequest",
    "NonCreditRequirementResult",
    "evaluate",
]
