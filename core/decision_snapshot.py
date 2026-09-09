"""Immutable, content-addressed decision snapshots.

This slice is deliberately a pure boundary around :mod:`allocation_engine`.
It normalizes the inputs used by the allocator, keeps only scalar request
context and opaque evidence identifiers, and projects injected resolver
records to a small provenance shape.  Resolver callables and their stores are
never retained in the returned object.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from allocation_engine import (
    NOT_APPLICABLE,
    UNKNOWN,
    AllocationResult,
    CourseAttempt,
    EquivalencyBinding,
    RequirementSpec,
    WaiverDecision,
    allocate_credits,
)

_SCALAR_TYPES = (str, int, float, bool, Decimal)
_SAFE_REQUEST_KEYS = {
    "admission_cohort",
    "application_term",
    "application_year",
    "application_semester",
    "target_curriculum_id",
    "primary_curriculum_id",
    "curriculum_id",
    "target_program",
    "target_track",
    "target_curriculum_version",
    "target_curriculum_version_candidate",
    "target_curriculum_year",
    "primary_program",
    "primary_track",
    "program",
    "program_type",
    "secondary_kind",
    "application_status",
    "submitted_at",
    "application_event_evidence_id",
    "school_approval_status",
    "formal_qualification_status",
    "formal_award_status",
    "input_warning_codes",
    "evaluated_at",
    "request_version",
}
_SAFE_INPUT_WARNING_CODES = frozenset()
_EVIDENCE_ID_KEYS = {
    "evidence_id",
    "evidence_record_id",
    "evidence_record_ids",
    "evidence_ids",
    "rule_evidence_id",
    "curriculum_evidence_id",
    "equivalency_evidence_ids",
    "target_curriculum_evidence_id",
    "department_decision_evidence_id",
    "registrar_registration_evidence_id",
    "formal_qualification_evidence_id",
    "formal_award_evidence_id",
    "application_event_evidence_id",
}
_SAFE_RECORD_KEYS = {
    "id",
    "evidence_id",
    "record_id",
    "curriculum_id",
    "kind",
    "type",
    "version",
    "program",
    "target_program",
    "track",
    "target_track",
    "admission_cohort",
    "application_term",
    "application_year",
    "application_semester",
    "scope",
    "evidence_state",
    "coverage_state",
    "authority",
    "source_reference",
    "source_type",
    "status",
    "decision",
    "reason",
    "course_catalog_coverage",
    "requirement_ids",
    "curriculum_revision",
    "effective_term",
    "effective_start",
    "effective_end",
}

_CONFIRMATION_MARKERS = {"FORMAL_RELEASE", "FORMAL_RELEASED", "CONFIRMED_TRANSCRIPT"}
_DIAGNOSTIC_ID_PREFIXES = (
    "DUPLICATE_BINDING_ID:",
    "DUPLICATE_WAIVER_DECISION_ID:",
    "EQUIVALENCY_UNRESOLVED:",
    "EVIDENCE_UNRESOLVED:",
    "UNVERIFIED_EQUIVALENCY:",
    "UNVERIFIED_WAIVER:",
    "SHARED_SCOPE_UNKNOWN:",
    "SHARED_SOURCE_USE_UNKNOWN:",
    "SHARED_SOURCE_CAP:",
    "SHARED_CAPACITY_UNKNOWN:",
    "SHARED_LEDGER_CAP:",
)


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float, bool)) else ""


def _freeze(value: Any) -> Any:
    """Recursively convert mutable containers to immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    """Return a JSON-friendly copy for hashing or a caller-facing view."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return deepcopy(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def statistics_digest(statistics: Mapping[str, Any]) -> str:
    """Hash the public statistics body with stable decimal representation."""

    def plain(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        if isinstance(value, Decimal):
            if not value.is_finite():
                return "0"
            if value == value.to_integral():
                return str(value.quantize(Decimal("1")))
            return format(value.normalize(), "f").rstrip("0").rstrip(".") or "0"
        return value

    body = {str(key): plain(value) for key, value in statistics.items() if str(key) != "statistics_digest"}
    return f"sha256:{_digest(body)}"


_PUBLIC_EVIDENCE_ID_KEYS = frozenset(
    {
        "evidence_id",
        "evidence_ids",
        "evidence_record_id",
        "evidence_record_ids",
        "evidence_reference",
        "decision_id",
        "record_id",
        "assertion_id",
        "source_assertion_id",
        "provenance_id",
        "rule_evidence_id",
        "curriculum_evidence_id",
        "target_curriculum_evidence_id",
        "target_version_evidence_id",
        "target_version_evidence_reference",
        "rule_applicability_evidence_id",
        "department_decision_evidence_id",
        "registrar_registration_evidence_id",
        "formal_qualification_evidence_id",
        "formal_award_evidence_id",
        "official_evidence_ids",
        "notice_evidence_ids",
        "equivalency_evidence_ids",
    }
)
_PUBLIC_BINDING_ID_KEYS = frozenset(
    {
        "binding_id",
        "binding_ids",
        "source_binding_id",
        "equivalency_binding_ids",
    }
)
_PUBLIC_IDENTITY_KEYS = frozenset(
    {
        # ASCII snake_case and compact/camel-case spellings commonly emitted
        # by transcript imports and portal adapters.
        "student_id",
        "studentid",
        "student_number",
        "studentnumber",
        "student_no",
        "studentno",
        "student_name",
        "studentname",
        "student_identifier",
        "studentidentifier",
        "student_uid",
        "studentuid",
        "subject_id",
        "subjectid",
        "subject_ref",
        "subjectref",
        # Generic account/login identifiers can be emitted by portal adapters
        # without the ``student_`` prefix.  They are never needed to explain
        # a course or requirement, so omit them at every public boundary.
        "uid",
        "user_id",
        "userid",
        "user_number",
        "usernumber",
        "user_no",
        "userno",
        "user_name",
        "username",
        "user_identifier",
        "useridentifier",
        "user_uid",
        "useruid",
        "account",
        "account_id",
        "accountid",
        "account_number",
        "accountnumber",
        "account_no",
        "accountno",
        "account_name",
        "accountname",
        "account_identifier",
        "accountidentifier",
        "account_uid",
        "accountuid",
        "login_id",
        "loginid",
        "login_name",
        "loginname",
        # These labels can occur in localized resolver records.
        "學號",
        "學生學號",
        "學生編號",
        "學生姓名",
        "姓名",
        "帳號",
        "帳號名稱",
        "使用者帳號",
        "使用者名稱",
    }
)
_PUBLIC_IDENTITY_CONTAINER_KEYS = frozenset(
    {
        # These are known identity-bearing containers.  Drop the container as
        # a unit instead of recursively retaining generic ``id``/``name``
        # members; course and requirement labels elsewhere remain auditable.
        "student",
        "student_info",
        "studentinfo",
        "student_record",
        "studentrecord",
        "subject",
        "subject_info",
        "subjectinfo",
        "account_info",
        "accountinfo",
        "user",
        "user_info",
        "userinfo",
        "profile",
        "identity",
    }
)

# Only reviewed, checked-in public citations may survive the snapshot privacy
# boundary verbatim.  A caller-provided URL is still an opaque evidence value:
# allowing an arbitrary HTTPS string would permit a student identifier or token
# to be smuggled into audit exports as a path/query component.
_PUBLIC_SOURCE_REFERENCES = frozenset(
    {
        "https://reg.utaipei.edu.tw/var/file/31/1031/attach/53/pta_84802_2981414_70686.pdf",
        "https://edu.utaipei.edu.tw/p/406-1056-97741%2Cr1.php?Lang=zh-tw",
        "https://edu.utaipei.edu.tw/p/406-1056-101280%2Cr1.php?Lang=zh-tw",
        "https://reg.utaipei.edu.tw/var/file/31/1031/attach/74/pta_106767_6992864_04492.pdf",
        "https://reg.utaipei.edu.tw/var/file/31/1031/attach/69/pta_112725_6380521_93841.pdf",
        "https://reg.utaipei.edu.tw/var/file/31/1031/attach/42/pta_129168_9101728_61974.pdf",
        "https://reg.utaipei.edu.tw/var/file/31/1031/attach/6/pta_141015_9521165_14743.pdf",
        "https://reg.utaipei.edu.tw/var/file/31/1031/attach/60/pta_164885_1896902_75647.pdf",
        "https://reg.utaipei.edu.tw/p/403-1031-511-1.php?Lang=zh-tw",
        "https://cs.utaipei.edu.tw/p/405-1081-116948%2Cc3219.php?Lang=zh-tw",
    }
)


def _public_opaque_id(value: Any, *, namespace: str) -> str:
    """Return a stable public handle without exposing an opaque caller ID."""

    text = _text(value)
    if not text:
        return ""
    prefix = f"{namespace}:sha256:"
    if text.startswith(prefix):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}{digest}"


def _public_source_reference(value: Any) -> Any:
    """Keep public web citations; hash private/opaque references."""

    if isinstance(value, Mapping):
        return _publicize_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return tuple(_public_source_reference(item) for item in value)
    text = _text(value)
    if not text:
        return ""
    if (
        text in _PUBLIC_SOURCE_REFERENCES
        or text.startswith("handbook:")
        or text.startswith("official:")
        or text.startswith("catalog:")
    ):
        return text
    return _public_opaque_id(text, namespace="evidence")


def _public_opaque_values(value: Any, *, namespace: str) -> Any:
    if isinstance(value, Mapping):
        return _publicize_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return tuple(_public_opaque_id(item, namespace=namespace) for item in value if _text(item))
    return _public_opaque_id(value, namespace=namespace)


def _publicize_mapping(
    value: Any,
    *,
    _provenance_context: bool = False,
    _binding_context: bool = False,
) -> Any:
    """Recursively publicize opaque identifiers in a JSON-shaped value.

    This is intentionally a projection helper, not an input validator.  The
    trusted resolver has already been called with the caller's opaque ID by
    the time this function runs.  Only identifiers and evidence references
    are replaced; course, requirement, curriculum, and human-readable text
    remain available for audit and remediation.
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.casefold()
            identity_key = "".join(char for char in normalized if char.isalnum())
            # A ``subject`` scalar can be an academic label, but a mapping
            # under any known identity container is account/person data.
            # Known student/account containers are otherwise always identity
            # payloads and must not cross the public Snapshot boundary.
            if identity_key in _PUBLIC_IDENTITY_CONTAINER_KEYS and (
                identity_key != "subject" or isinstance(item, Mapping)
            ):
                continue
            if normalized in _PUBLIC_IDENTITY_KEYS or identity_key in _PUBLIC_IDENTITY_KEYS:
                # Identity fields are omitted rather than hashed.  A stable
                # hash would still be a reusable student identifier in public
                # repr/export output and would violate the snapshot privacy
                # boundary.
                continue
            if normalized in _PUBLIC_BINDING_ID_KEYS or normalized.endswith("_binding_id") or normalized.endswith("_binding_ids"):
                result[key] = _public_opaque_values(item, namespace="binding")
            elif normalized == "source_reference":
                result[key] = _public_source_reference(item)
            elif (
                normalized in _PUBLIC_EVIDENCE_ID_KEYS
                or normalized.endswith("_evidence_id")
                or normalized.endswith("_evidence_ids")
                or normalized.endswith("_evidence_reference")
            ):
                result[key] = _public_opaque_values(item, namespace="evidence")
            elif normalized == "id" and _binding_context:
                result[key] = _public_opaque_id(item, namespace="binding")
            elif normalized == "id" and _provenance_context:
                result[key] = _public_opaque_id(item, namespace="evidence")
            else:
                child_provenance = _provenance_context or normalized in {"provenance", "rule_provenance", "evidence"}
                child_binding = _binding_context or normalized in {"binding", "bindings", "binding_assessments"}
                result[key] = _publicize_mapping(
                    item,
                    _provenance_context=child_provenance,
                    _binding_context=child_binding,
                )
        return result
    if isinstance(value, (list, tuple)):
        return tuple(
            _publicize_mapping(
                item,
                _provenance_context=_provenance_context,
                _binding_context=_binding_context,
            )
            for item in value
        )
    if isinstance(value, set):
        return tuple(
            sorted(
                (
                    _publicize_mapping(
                        item,
                        _provenance_context=_provenance_context,
                        _binding_context=_binding_context,
                    )
                    for item in value
                ),
                key=repr,
            )
        )
    return value


def _optimality_is_uncertain(value: Any) -> bool:
    """Return whether an optimality label cannot support a formal PASS."""

    # ``OPTIMAL`` is the only label emitted by a complete, unique search.
    # Unknown/future labels must not silently become formal PASS values.
    return _text(value).casefold() != "optimal"


def _safe_diagnostic(value: Any) -> str:
    """Mask opaque evidence/binding identifiers in public diagnostics."""

    text = _text(value)
    for prefix in _DIAGNOSTIC_ID_PREFIXES:
        if text.startswith(prefix):
            if ":evidence:" in text:
                return text
            head, _, tail = text.rpartition(":")
            if not tail:
                return text
            return f"{head}:evidence:{hashlib.sha256(tail.encode('utf-8')).hexdigest()[:12]}"
    return text


def _values(value: Any) -> tuple[Any, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, Mapping):
        # A mapping is an individual record, not an iterable of its keys.
        return (value,)
    if isinstance(value, (str, bytes, bytearray)):
        return (value,)
    if isinstance(value, Sequence) or isinstance(value, set):
        return tuple(value)
    return (value,)


def _safe_confirmation_projection(
    value: Any,
    *,
    attempt_count: int | None = None,
) -> tuple[MappingProxyType, bool]:
    """Validate/project the exact input-confirmation release contract.

    Accepted producers (currently ``graduation_service``) must provide a
    confirmed state, equal non-empty current/confirmed fingerprints, and
    either the explicit ``formal_release_success`` marker or the service's
    ``release_allowed=True`` plus a positive released-row count.  Everything
    else is treated as unconfirmed and omitted from the returned projection.
    """

    if not isinstance(value, Mapping):
        return _freeze(
            {
                "state": "UNCONFIRMED",
                "current_fingerprint": "",
                "fingerprint": "",
                "confirmed_fingerprint": "",
                "formal_release_success": False,
                "formal_release_marker": "",
                "release_allowed": False,
                "row_count": 0,
                "released_row_count": 0,
                "cardinality_consistent": False,
            }
        ), False
    state = _text(value.get("state") or value.get("confirmation_state")).upper()
    current = _text(value.get("current_fingerprint") or value.get("fingerprint"))
    confirmed = _text(value.get("confirmed_fingerprint"))
    marker = _text(value.get("formal_release_marker") or value.get("release_marker")).upper()
    formal_success = value.get("formal_release_success") is True
    release_allowed = value.get("release_allowed") is True
    counts_valid = True
    try:
        released_count = int(value.get("released_row_count", 0))
        row_count = int(value.get("row_count", 0))
    except (TypeError, ValueError, OverflowError):
        released_count = row_count = 0
        counts_valid = False
    counts_valid = counts_valid and row_count >= 0 and released_count >= 0 and released_count <= row_count
    if attempt_count is not None:
        try:
            normalized_attempt_count = int(attempt_count)
        except (TypeError, ValueError, OverflowError):
            normalized_attempt_count = -1
        # A formally released attempt is the only input that may reach the
        # allocator.  Keep the release boundary closed if its declared count
        # disagrees with the actual released attempt objects, including the
        # otherwise-valid 1-row/0-attempt case.
        counts_valid = counts_valid and normalized_attempt_count == released_count
    if value.get("cardinality_consistent") is False:
        counts_valid = False
    marker_success = marker in _CONFIRMATION_MARKERS
    marker = marker if marker_success else ""
    # The service deliberately allows a confirmed empty transcript (there is
    # no formal row to release in that case).  It remains safe because the
    # caller still has to provide the exact confirmation state/fingerprints
    # and the explicit release flag; no attempts are exposed for an empty
    # row-set.
    release_success = counts_valid and (formal_success or (release_allowed and (released_count > 0 or row_count == 0)))
    valid = bool(
        state == "CONFIRMED"
        and current
        and confirmed
        and current == confirmed
        # A marker is evidence of the producer's release step, not a waiver
        # of the count contract.  It must never make malformed cardinality
        # look formally confirmed.
        and counts_valid
        and (release_success or marker_success)
    )
    projection = {
        "state": state or "UNCONFIRMED",
        "current_fingerprint": current,
        "fingerprint": current,
        "confirmed_fingerprint": confirmed,
        "formal_release_success": bool(release_success),
        "formal_release_marker": marker,
        "release_allowed": bool(release_allowed),
        "row_count": max(0, row_count),
        "released_row_count": max(0, released_count),
        "cardinality_consistent": counts_valid,
    }
    if not valid:
        projection["state"] = "UNCONFIRMED"
        projection["formal_release_success"] = False
    return _freeze(projection), valid


def _attempt(value: Any) -> CourseAttempt | None:
    if isinstance(value, CourseAttempt):
        return value
    if not isinstance(value, Mapping):
        return None
    return CourseAttempt(
        attempt_id=value.get("attempt_id", value.get("id", "")),
        course_id=value.get("course_id", value.get("course_code", value.get("code", value.get("name", "")))),
        course_name=value.get("course_name", value.get("name", value.get("title", ""))),
        credits=value.get("credits", value.get("credit", value.get("total_credit", 0))),
        earned_credits=value.get("earned_credits", value.get("completed_credit")),
        academic_term=value.get("academic_term", value.get("term", value.get("semester", ""))),
        repeat_group_id=value.get("repeat_group_id", value.get("repeat_group")),
        # Mapping inputs are an untrusted import boundary; identity must be
        # explicitly established by the caller before a course can pass.
        identity_status=value.get("identity_status", value.get("course_identity_status", "UNKNOWN")),
        course_kind=value.get("course_kind", value.get("component_type", UNKNOWN)),
        pool_memberships=value.get("pool_memberships", value.get("pools", ())),
        status=value.get("status", "PASS"),
        source_kind=value.get("source_kind", "TRANSCRIPT"),
        grade=value.get("grade", ""),
        grade_evidence_state=value.get("grade_evidence_state", value.get("grade_trust", UNKNOWN)),
        effective_attempt=value.get("effective_attempt", value.get("is_effective_attempt", False)),
        repeat_selection_evidence_state=value.get(
            "repeat_selection_evidence_state",
            value.get("effective_attempt_evidence_state", value.get("repeat_selection_state", UNKNOWN)),
        ),
        pool_ids=value.get("pool_ids", ()),
        pool_evidence_state=value.get("pool_evidence_state", value.get("pool_evidence", UNKNOWN)),
        pool_membership_evidence=value.get(
            "pool_membership_evidence",
            value.get("membership_evidence", value.get("pool_evidence_by_id", ())),
        ),
    )


def _requirement(value: Any) -> RequirementSpec | None:
    if isinstance(value, RequirementSpec):
        return value
    if not isinstance(value, Mapping):
        return None
    return RequirementSpec(
        requirement_id=value.get("requirement_id", value.get("id", "")),
        name=value.get("name", value.get("title", "")),
        credits_required=value.get("credits_required", value.get("credits", value.get("required_credits", 0))),
        max_credits=value.get("max_credits", value.get("credit_cap")),
        eligible_course_ids=value.get("eligible_course_ids", value.get("course_ids", ())),
        eligible_pool_ids=value.get("eligible_pool_ids", value.get("pool_ids", ())),
        overflow_routes=value.get("overflow_routes", ()),
        allowed_course_kinds=value.get("allowed_course_kinds", value.get("allowed_kinds", ())),
        course_kind=value.get("course_kind"),
        # Do not infer official coverage/provenance from a bare dictionary.
        coverage_state=value.get("coverage_state", value.get("coverage", "NONE")),
        evidence_state=value.get("evidence_state", value.get("evidence", "UNKNOWN")),
        required=value.get("required", True),
        repeatable=value.get("repeatable", False),
        repeat_policy=value.get("repeat_policy", "BEST_ATTEMPT_ONLY"),
        waiver=value.get("waiver", False),
        eligible_course_names=value.get("eligible_course_names", ()),
        accept_any=value.get("accept_any", False),
        bucket=value.get("bucket", ""),
        kind=value.get("kind", ""),
        owner=value.get("owner", value.get("role", value.get("curriculum_role", ""))),
        domain=value.get("domain", value.get("requirement_domain", "")),
        subset_constraints=value.get("subset_constraints", value.get("subsets", ())),
    )


def _binding(value: Any) -> EquivalencyBinding | None:
    if isinstance(value, EquivalencyBinding):
        return value
    if not isinstance(value, Mapping):
        return None
    return EquivalencyBinding(
        binding_id=value.get("binding_id", value.get("id", "")),
        source_attempt_id=value.get("source_attempt_id", value.get("attempt_id", "")),
        target_requirement_id=value.get("target_requirement_id", value.get("requirement_id", "")),
        approved_credits=value.get("approved_credits", value.get("credits", 0)),
        evidence_state=value.get("evidence_state", "UNKNOWN"),
        authority=value.get("authority", ""),
        evidence_reference=value.get("evidence_reference", value.get("source_reference", "")),
        direction=value.get("direction", ""),
        shared=value.get("shared", False),
        source_course_id=value.get("source_course_id", ""),
        target_course_id=value.get("target_course_id", ""),
        source_course_kind=value.get("source_course_kind", ""),
        allocation_kind=value.get("allocation_kind", ""),
        decision=value.get("decision", "PENDING"),
        scope=value.get("scope", ""),
        source_requirement_id=value.get("source_requirement_id", value.get("source_requirement", "")),
        source_domain=value.get("source_domain", ""),
        target_domain=value.get("target_domain", ""),
        source_owner=value.get("source_owner", ""),
        target_owner=value.get("target_owner", ""),
    )


def _waiver_decision(value: Any) -> WaiverDecision | None:
    if isinstance(value, WaiverDecision):
        return value
    if not isinstance(value, Mapping):
        return None
    return WaiverDecision(
        decision_id=value.get("decision_id", value.get("id", value.get("evidence_id", ""))),
        target_requirement_id=value.get("target_requirement_id", value.get("requirement_id", value.get("target_id", ""))),
        evidence_state=value.get("evidence_state", value.get("status", "UNKNOWN")),
        authority=value.get("authority", ""),
        evidence_reference=value.get("evidence_reference", value.get("source_reference", value.get("evidence_id", ""))),
        decision=value.get("decision", "PENDING"),
    )


def _normalized_inputs(request: Mapping[str, Any]) -> tuple[tuple[CourseAttempt, ...], tuple[RequirementSpec, ...], tuple[EquivalencyBinding, ...]]:
    attempts_value = request.get("attempts", request.get("course_attempts", request.get("course_records", ())))
    requirements_value = request.get("requirements", request.get("requirement_specs", ()))
    bindings_value = request.get("bindings", request.get("equivalencies", ()))
    attempts = tuple(sorted((item for value in _values(attempts_value) if (item := _attempt(value)) is not None), key=lambda item: (item.attempt_id, item.course_id, item.academic_term)))
    requirements = tuple(sorted((item for value in _values(requirements_value) if (item := _requirement(value)) is not None), key=lambda item: item.requirement_id))
    bindings = tuple(sorted((item for value in _values(bindings_value) if (item := _binding(value)) is not None), key=lambda item: item.binding_id))
    return attempts, requirements, bindings


def _normalized_waiver_decisions(request: Mapping[str, Any]) -> tuple[WaiverDecision, ...]:
    values = request.get("waiver_decisions", request.get("waivers", ()))
    decisions = tuple(item for value in _values(values) if (item := _waiver_decision(value)) is not None)
    return tuple(sorted(decisions, key=lambda item: item.decision_id))


def _request_evidence_ids(request: Mapping[str, Any]) -> tuple[str, ...]:
    context = request.get("context")
    source_maps = [request]
    if isinstance(context, Mapping):
        source_maps.append(context)
    evidence_ids: set[str] = set()
    for source in source_maps:
        for key in _EVIDENCE_ID_KEYS:
            for candidate in _values(source.get(key)):
                text = _text(candidate)
                if text:
                    evidence_ids.add(text)
    return tuple(sorted(evidence_ids))


def _safe_request(request: Mapping[str, Any], evaluated_at: str) -> MappingProxyType:
    """Keep scalar context and opaque evidence IDs only."""

    context = request.get("context")
    source_maps = [request]
    if isinstance(context, Mapping):
        source_maps.append(context)
    result: dict[str, Any] = {}
    for source in source_maps:
        for key in _SAFE_REQUEST_KEYS:
            value = source.get(key)
            if key == "input_warning_codes":
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                    result[key] = tuple(
                        code for item in value if (code := _text(item)) in _SAFE_INPUT_WARNING_CODES
                    )
            elif isinstance(value, _SCALAR_TYPES) and not isinstance(value, (bytes, bytearray)):
                result[key] = str(value) if isinstance(value, Decimal) else value
    result["evaluated_at"] = evaluated_at

    result["evidence_record_ids"] = tuple(
        _public_opaque_id(item, namespace="evidence") for item in _request_evidence_ids(request)
    )
    return _freeze(_publicize_mapping(result))


def _record_mapping(record: Any) -> Mapping[str, Any] | None:
    return record if isinstance(record, Mapping) else None


def _safe_record(record: Any, *, record_id: str, record_kind: str) -> MappingProxyType:
    """Project a resolver record; never retain arbitrary nested evidence."""

    mapping = _record_mapping(record)
    result: dict[str, Any] = {
        "record_id": _public_opaque_id(record_id, namespace="evidence"),
        "record_kind": record_kind,
    }
    if mapping is None:
        result["resolution_state"] = "UNRESOLVED"
        return _freeze(result)
    for key in _SAFE_RECORD_KEYS:
        value = mapping.get(key)
        if isinstance(value, _SCALAR_TYPES):
            result[key] = str(value) if isinstance(value, Decimal) else value
        elif key == "requirement_ids":
            result[key] = tuple(sorted(text for item in _values(value) if (text := _text(item))))
    evidence_id = _text(mapping.get("evidence_id") or mapping.get("record_id") or record_id) or record_id
    result["evidence_id"] = _public_opaque_id(evidence_id, namespace="evidence")
    result["resolution_state"] = "RESOLVED"
    return _freeze(_publicize_mapping(result))


def _resolve_record(resolver: Callable[[str], Any] | None, record_id: str) -> Any:
    if resolver is None:
        return None
    try:
        return resolver(record_id)
    except Exception:  # Resolver failures are an unresolved boundary, not a caller-visible crash.
        return None


def _curriculum_projection(request: Mapping[str, Any], resolver: Callable[[str], Any] | None) -> tuple[MappingProxyType, MappingProxyType, tuple[str, ...]]:
    ids: list[tuple[str, str]] = []
    for role, keys in (
        ("primary", ("primary_curriculum_id", "curriculum_id")),
        ("target", ("target_curriculum_id",)),
    ):
        for key in keys:
            value = _text(request.get(key))
            if value:
                ids.append((role, value))
                break
    curricula: dict[str, Any] = {}
    resolution: dict[str, Any] = {}
    blockers: list[str] = []
    for role, curriculum_id in ids:
        record = _resolve_record(resolver, curriculum_id)
        projection = _safe_record(record, record_id=curriculum_id, record_kind=role)
        curricula[role] = projection
        resolution[role] = {
            "status": "RESOLVED" if projection.get("resolution_state") == "RESOLVED" else "UNKNOWN",
            "curriculum_id": curriculum_id,
            "evidence_id": projection.get("evidence_id", curriculum_id),
        }
        if projection.get("resolution_state") != "RESOLVED":
            blockers.append(f"CURRICULUM_UNRESOLVED:{curriculum_id}")
    return _freeze(curricula), _freeze(resolution), tuple(blockers)


def _evidence_projection(request: Mapping[str, Any], resolver: Callable[[str], Any] | None) -> tuple[tuple[MappingProxyType, ...], tuple[str, ...]]:
    # Resolve the caller's raw opaque IDs before public projection.  The
    # resulting record and every exposed handle are hashed by
    # ``_safe_record``/``DecisionSnapshot`` below.
    evidence_ids = _request_evidence_ids(request)
    projections: list[MappingProxyType] = []
    blockers: list[str] = []
    for evidence_id in evidence_ids:
        record = _resolve_record(resolver, evidence_id)
        projection = _safe_record(record, record_id=evidence_id, record_kind="evidence")
        projections.append(projection)
        if projection.get("resolution_state") != "RESOLVED":
            blockers.append(f"EVIDENCE_UNRESOLVED:{evidence_id}")
    return tuple(sorted(projections, key=lambda item: str(item.get("evidence_id", item.get("record_id", ""))))), tuple(blockers)


def _attempt_plain(item: CourseAttempt) -> dict[str, Any]:
    return {
        "attempt_id": item.attempt_id,
        "course_id": item.course_id,
        "course_name": item.course_name,
        "credits": str(item.credits),
        "earned_credits": str(item.earned_credits),
        "available_credits": str(item.available_credits),
        "academic_term": item.academic_term,
        "repeat_group_id": item.repeat_group_id,
        "identity_status": item.identity_status,
        "course_kind": item.course_kind,
        "pool_memberships": item.pool_memberships,
        "pool_ids": item.pool_ids,
        "pool_evidence_state": item.pool_evidence_state,
        "pool_membership_evidence": tuple(
            {
                "pool_id": record[0],
                "evidence_state": record[1],
                "source_reference": record[2],
            }
            for record in item.pool_membership_evidence
        ),
        "status": item.status,
        "source_kind": item.source_kind,
        "grade": item.grade,
        "grade_evidence_state": item.grade_evidence_state,
        "effective_attempt": item.effective_attempt,
        "repeat_selection_evidence_state": item.repeat_selection_evidence_state,
    }


def _requirement_plain(item: RequirementSpec) -> dict[str, Any]:
    return {
        "requirement_id": item.requirement_id,
        "name": item.name,
        "credits_required": str(item.credits_required),
        "max_credits": str(item.max_credits),
        "eligible_course_ids": item.eligible_course_ids,
        "eligible_pool_ids": item.eligible_pool_ids,
        "eligible_course_names": item.eligible_course_names,
        "overflow_routes": item.overflow_routes,
        "allowed_course_kinds": item.allowed_course_kinds,
        "coverage_state": item.coverage_state,
        "evidence_state": item.evidence_state,
        "required": item.required,
        "repeat_policy": item.repeat_policy,
        "repeatable": item.repeatable,
        "waiver": item.waiver,
        "accept_any": item.accept_any,
        "bucket": item.bucket,
        "owner": item.owner,
        "domain": item.domain,
        "subset_constraints": item.subset_constraints,
    }


def _binding_plain(item: EquivalencyBinding) -> dict[str, Any]:
    # Keep only scalar audit handles and official-reference fields; arbitrary
    # nested evidence is never copied into a snapshot.
    return {
        "binding_id": _public_opaque_id(item.binding_id, namespace="binding"),
        "source_attempt_id": item.source_attempt_id,
        "target_requirement_id": item.target_requirement_id,
        "approved_credits": str(item.approved_credits),
        "evidence_state": item.evidence_state,
        "authority": item.authority,
        "evidence_reference": _public_opaque_id(item.evidence_reference, namespace="evidence"),
        "direction": item.direction,
        "shared": item.shared,
        "source_course_id": item.source_course_id,
        "target_course_id": item.target_course_id,
        "source_course_kind": item.source_course_kind,
        "allocation_kind": item.allocation_kind,
        "decision": item.decision,
        "scope": item.scope,
        "source_requirement_id": item.source_requirement_id,
        "source_domain": item.source_domain,
        "target_domain": item.target_domain,
        "source_owner": item.source_owner,
        "target_owner": item.target_owner,
    }


def _waiver_plain(item: WaiverDecision) -> dict[str, Any]:
    return {
        "decision_id": _public_opaque_id(item.decision_id, namespace="evidence"),
        "target_requirement_id": item.target_requirement_id,
        "evidence_state": item.evidence_state,
        "authority": item.authority,
        "evidence_reference": _public_opaque_id(item.evidence_reference, namespace="evidence"),
        "decision": item.decision,
    }


def _portion_plain(portion: Any) -> dict[str, Any]:
    return {
        "attempt_id": _text(portion.attempt_id),
        "requirement_id": _text(portion.requirement_id),
        "credits": str(portion.credits),
        "allocation_kind": _text(portion.allocation_kind),
        "direction": _text(portion.direction),
        "binding_id": _public_opaque_id(portion.binding_id, namespace="binding"),
        "is_shared_shadow": _text(portion.allocation_kind) == "SHARED_SHADOW",
    }


def _attempt_allocation_plain(item: Any) -> dict[str, Any]:
    portions = tuple(_portion_plain(portion) for portion in item.portions)
    positive = tuple(portion for portion in item.portions if portion.credits > Decimal("0"))
    return {
        "attempt_id": _text(item.attempt_id),
        "source_credits": str(item.source_credits),
        "used_credits": str(sum((portion.credits for portion in item.portions if portion.allocation_kind == "EXCLUSIVE"), Decimal("0"))),
        "portions": portions,
        "unallocated_credits": str(item.unallocated_credits),
        "candidate_requirement_ids": tuple(sorted({str(portion["requirement_id"]) for portion in portions})),
        "allocation_reason": (
            "WAIVER_DECISION"
            if any(portion["allocation_kind"] == "WAIVER" for portion in portions)
            else "EXCLUSIVE_REQUIREMENT_ROUTE"
            if positive
            else "NO_SAFE_REQUIREMENT_ROUTE"
        ),
    }


def _requirement_result_plain(item: Any, name: str = "") -> dict[str, Any]:
    res = {
        "requirement_id": _text(item.requirement_id),
        "status": _text(item.status),
        "required_credits": str(item.required_credits),
        "exclusive_credits": str(item.exclusive_credits),
        "shared_shadow_credits": str(item.shared_shadow_credits),
        "effective_credits": str(item.effective_credits),
        "deficit": str(item.deficit),
        "coverage_state": _text(item.coverage_state),
        "evidence_state": _text(item.evidence_state),
        "waived": bool(item.waived),
        "blockers": tuple(_text(blocker) for blocker in item.blockers),
    }
    req_name = name or _text(getattr(item, "name", ""))
    if req_name:
        res["name"] = req_name
    return res


def _shared_ledger_plain(item: Any) -> dict[str, Any]:
    return {
        "direction": _text(item.direction),
        "used_credits": str(item.used_credits),
        "limit_credits": str(item.limit_credits),
        "blocked_credits": str(item.blocked_credits),
        "binding_ids": tuple(_public_opaque_id(binding_id, namespace="binding") for binding_id in item.binding_ids),
    }


def _binding_assessment_plain(item: Any) -> dict[str, Any]:
    return {
        "binding_id": _public_opaque_id(item.binding_id, namespace="binding"),
        "status": _text(item.status),
        "reason": _text(item.reason),
        "direction": _text(item.direction),
        "approved_credits": str(item.approved_credits),
    }


def _waiver_assessment_plain(item: Any) -> dict[str, Any]:
    return {
        "decision_id": _public_opaque_id(item.decision_id, namespace="evidence"),
        "target_requirement_id": _text(item.target_requirement_id),
        "status": _text(item.status),
        "reason": _text(item.reason),
    }


def _alternative_plain(signature: Any) -> Any:
    """Keep alternative route metadata scalar and recursively immutable."""

    if isinstance(signature, (list, tuple)):
        return tuple(_alternative_plain(item) for item in signature)
    return str(signature)


def _allocation_plain(allocation: AllocationResult, requirements: Sequence[Any] = ()) -> dict[str, Any]:
    req_names = {
        _text(getattr(r, "requirement_id", None) or getattr(r, "id", None)): _text(getattr(r, "name", None))
        for r in requirements
    }
    return {
        "status": allocation.status,
        "source_earned_credits": str(allocation.source_earned_credits),
        "recognized_credits": str(allocation.recognized_credits),
        "effective_recognized_credits": str(allocation.effective_recognized_credits),
        "unallocated_credits": str(allocation.unallocated_credits),
        "credit_conservation": bool(allocation.credit_conservation),
        "allocations": tuple(_attempt_allocation_plain(item) for item in allocation.allocations),
        "requirement_results": tuple(
            _requirement_result_plain(item, req_names.get(_text(item.requirement_id), ""))
            for item in allocation.requirement_results
        ),
        "shadow_allocations": tuple(_portion_plain(item) for item in allocation.shadow_allocations),
        "shared_ledgers": tuple(_shared_ledger_plain(item) for item in allocation.shared_ledgers),
        "binding_assessments": tuple(_binding_assessment_plain(item) for item in allocation.binding_assessments),
        "waiver_assessments": tuple(_waiver_assessment_plain(item) for item in allocation.waiver_assessments),
        "blockers": tuple(_safe_diagnostic(item) for item in allocation.blockers),
        "warnings": tuple(_safe_diagnostic(item) for item in allocation.warnings),
        "search_exhausted": bool(allocation.search_exhausted),
        "search_complete": bool(allocation.search_complete),
        "feasibility": allocation.feasibility,
        "feasible_witness": bool(allocation.feasible_witness),
        "optimality": allocation.optimality,
        "route_ambiguity": bool(allocation.route_ambiguity),
        "decision_ambiguity": bool(allocation.decision_ambiguity),
        "pass_eligible": bool(allocation.pass_eligible),
        "nodes_searched": int(allocation.nodes_searched),
        "objective": tuple(str(item) if isinstance(item, Decimal) else item for item in allocation.objective),
        "allocation_ambiguous": bool(allocation.allocation_ambiguous),
        "alternative_allocations": tuple(_alternative_plain(item) for item in allocation.alternative_allocations),
        "subset_results": tuple(_thaw(item) for item in allocation.subset_results),
    }


def _publicize_allocation(allocation: AllocationResult) -> AllocationResult:
    """Replace opaque binding/waiver IDs on the immutable allocation object."""

    def portion(item: Any) -> Any:
        return replace(item, binding_id=_public_opaque_id(item.binding_id, namespace="binding"))

    def attempt_allocation(item: Any) -> Any:
        return replace(item, portions=tuple(portion(value) for value in item.portions))

    def ledger(item: Any) -> Any:
        return replace(
            item,
            binding_ids=tuple(_public_opaque_id(value, namespace="binding") for value in item.binding_ids),
        )

    def binding_assessment(item: Any) -> Any:
        return replace(item, binding_id=_public_opaque_id(item.binding_id, namespace="binding"))

    def waiver_assessment(item: Any) -> Any:
        return replace(item, decision_id=_public_opaque_id(item.decision_id, namespace="evidence"))

    return replace(
        allocation,
        allocations=tuple(attempt_allocation(item) for item in allocation.allocations),
        shadow_allocations=tuple(portion(item) for item in allocation.shadow_allocations),
        shared_ledgers=tuple(ledger(item) for item in allocation.shared_ledgers),
        binding_assessments=tuple(binding_assessment(item) for item in allocation.binding_assessments),
        waiver_assessments=tuple(waiver_assessment(item) for item in allocation.waiver_assessments),
    )


def _force_unknown_allocation(allocation: AllocationResult) -> AllocationResult:
    """Remove formal route data when the input release contract is unsafe."""

    requirement_results = tuple(
        replace(
            item,
            status=UNKNOWN if item.status != NOT_APPLICABLE else NOT_APPLICABLE,
            exclusive_credits=Decimal("0"),
            shared_shadow_credits=Decimal("0"),
            effective_credits=Decimal("0"),
            deficit=item.required_credits if item.status != NOT_APPLICABLE else Decimal("0"),
            waived=False,
            blockers=("INPUT_UNCONFIRMED",) if item.status != NOT_APPLICABLE else item.blockers,
        )
        for item in allocation.requirement_results
    )
    return replace(
        allocation,
        status="UNKNOWN",
        allocations=(),
        requirement_results=requirement_results,
        source_earned_credits=Decimal("0"),
        recognized_credits=Decimal("0"),
        unallocated_credits=Decimal("0"),
        credit_conservation=True,
        shadow_allocations=(),
        shared_ledgers=(),
        objective=(),
        alternative_allocations=(),
        allocation_ambiguous=False,
        feasibility=UNKNOWN,
        search_complete=False,
        optimality="UNKNOWN",
        route_ambiguity=False,
        decision_ambiguity=False,
        feasible_witness=False,
        blockers=tuple(dict.fromkeys((*allocation.blockers, "INPUT_UNCONFIRMED"))),
        pass_eligible=False,
    )


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """Deeply immutable output shared by later UI/report consumers."""

    snapshot_id: str
    evaluated_at: str
    request: Mapping[str, Any]
    attempts: tuple[CourseAttempt, ...]
    requirements: tuple[RequirementSpec, ...]
    bindings: tuple[Mapping[str, Any], ...]
    curriculum: Mapping[str, Any]
    rule_resolution: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...]
    allocation: AllocationResult
    verdict: str
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    # These fields are additive so existing allocator/snapshot callers keep
    # working.  The production service uses them as the canonical contract
    # for UI, charts, and exports; none of the fields retain resolver objects
    # or caller-owned raw records.
    schema_version: str = "decision-snapshot.v2"
    engine_version: str = "allocation-engine.v1"
    decisions: Mapping[str, Any] = field(default_factory=dict)
    rule_provenance: tuple[Mapping[str, Any], ...] = ()
    input_confirmation: Mapping[str, Any] = field(default_factory=dict)
    search_complete: bool | None = None
    optimality: str = ""
    allocation_ambiguous: bool | None = None
    feasibility: str = ""
    feasible_witness: bool | None = None
    route_ambiguity: bool | None = None
    decision_ambiguity: bool | None = None
    alternatives: tuple[Any, ...] = ()
    statistics: Mapping[str, Any] = field(default_factory=dict)
    remediation_suggestions: tuple[str, ...] = ()
    waiver_decisions: tuple[Mapping[str, Any], ...] = ()
    # Administrative/non-credit gates and policy subsets are observational
    # outputs.  They are carried separately from the credit ledger so a
    # renderer cannot accidentally add either category to recognized credits.
    non_credit_results: tuple[Mapping[str, Any], ...] = ()
    subset_results: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        evaluated_at = _text(self.evaluated_at)
        object.__setattr__(self, "snapshot_id", _text(self.snapshot_id))
        object.__setattr__(self, "evaluated_at", evaluated_at)
        request = self.request if isinstance(self.request, Mapping) else {}
        object.__setattr__(self, "request", _safe_request(request, evaluated_at))
        safe_confirmation, confirmation_valid = _safe_confirmation_projection(
            self.input_confirmation,
            attempt_count=len(self.attempts),
        )
        allocation = self.allocation if confirmation_valid else _force_unknown_allocation(self.allocation)
        # Internal allocation uses the resolver's opaque binding/waiver IDs so
        # caps and source matching remain exact.  Public snapshot consumers
        # receive stable hashes only; this is done before storing the object
        # itself so ``repr(snapshot)`` is safe as well as ``as_dict()``.
        allocation = _publicize_allocation(allocation)
        # The allocation object is part of the immutable snapshot repr as
        # well as its JSON projection.  Sanitize diagnostic references at the
        # object boundary so neither view leaks opaque evidence IDs.
        allocation = replace(
            allocation,
            blockers=tuple(_safe_diagnostic(item) for item in allocation.blockers),
            warnings=tuple(_safe_diagnostic(item) for item in allocation.warnings),
        )
        search_complete = (
            not allocation.search_exhausted
            if self.search_complete is None
            else self.search_complete is True
        )
        optimality = _text(self.optimality)
        if not optimality:
            optimality = "BOUNDED_NOT_COMPLETE" if not search_complete else "OPTIMAL"
        allocation_ambiguous = (
            allocation.allocation_ambiguous
            if self.allocation_ambiguous is None
            # An explicit override is trusted only when it is the literal
            # boolean False.  Truthy strings/numbers are malformed metadata,
            # not proof that the allocation is unique.
            else self.allocation_ambiguous is not False
        )
        metadata_blockers = []
        if self.feasible_witness is None:
            # An explicit snapshot override of ``search_complete=False``
            # cannot inherit the implicit witness that AllocationResult
            # derives for a fully searched legacy fixture.  A bounded
            # allocator result carries its own ``search_complete=False`` and
            # explicit ``feasible_witness=True``, so that verified witness is
            # still preserved here.
            feasible_witness = allocation.feasible_witness is True and not (
                self.search_complete is False and allocation.search_complete is True
            )
        else:
            feasible_witness = self.feasible_witness is True
        metadata_invalid = any(
            getattr(self, key) is not None
            and getattr(self, key) is not True
            and getattr(self, key) is not False
            for key in (
                "search_complete",
                "allocation_ambiguous",
                "feasible_witness",
                "route_ambiguity",
                "decision_ambiguity",
            )
        )
        feasibility = _text(self.feasibility).upper() or allocation.feasibility
        if feasibility not in {"FEASIBLE", "INFEASIBLE", UNKNOWN}:
            feasibility = UNKNOWN
            metadata_invalid = True
        route_ambiguity = allocation.route_ambiguity if self.route_ambiguity is None else self.route_ambiguity is True
        decision_ambiguity = allocation.decision_ambiguity if self.decision_ambiguity is None else self.decision_ambiguity is True
        if not search_complete and not feasible_witness:
            metadata_blockers.append("SEARCH_INCOMPLETE")
        if _optimality_is_uncertain(optimality) and not feasible_witness:
            metadata_blockers.append("OPTIMALITY_UNCERTAIN")
        if metadata_invalid:
            metadata_blockers.append("ALLOCATION_METADATA_INVALID")
        attempts = tuple(self.attempts) if confirmation_valid else ()
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "requirements", tuple(self.requirements))
        object.__setattr__(
            self,
            "bindings",
            tuple(_freeze(_publicize_mapping(item, _binding_context=True)) for item in self.bindings),
        )
        object.__setattr__(self, "curriculum", _freeze(_publicize_mapping(self.curriculum)))
        object.__setattr__(self, "rule_resolution", _freeze(_publicize_mapping(self.rule_resolution)))
        object.__setattr__(
            self,
            "evidence",
            tuple(_freeze(_publicize_mapping(item, _provenance_context=True)) for item in self.evidence),
        )
        object.__setattr__(self, "allocation", allocation)
        verdict = _text(self.verdict) or "UNKNOWN"
        blockers = tuple(
            dict.fromkeys(
                (
                    *(_text(item) for item in self.blockers if _text(item)),
                    *(_text(item) for item in allocation.blockers if _text(item)),
                    *metadata_blockers,
                )
            )
        )
        if not confirmation_valid:
            verdict = "UNKNOWN"
            blockers = tuple(dict.fromkeys((*blockers, "INPUT_UNCONFIRMED")))
        if verdict == "PASS" and (
            blockers
            or allocation.blockers
            or allocation.status != "PASS"
            or not allocation.pass_eligible
        ):
            verdict = UNKNOWN
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "blockers", tuple(_safe_diagnostic(item) for item in blockers))
        object.__setattr__(self, "warnings", tuple(_safe_diagnostic(item) for item in dict.fromkeys(_text(item) for item in self.warnings if _text(item))))
        object.__setattr__(self, "schema_version", _text(self.schema_version) or "decision-snapshot.v2")
        object.__setattr__(self, "engine_version", _text(self.engine_version) or "allocation-engine.v1")
        decisions = _publicize_mapping(self.decisions)
        if isinstance(decisions, Mapping) and "overall" in decisions:
            overall = decisions.get("overall")
            if isinstance(overall, Mapping):
                decisions = {
                    **decisions,
                    "overall": {
                        **overall,
                        "status": verdict,
                        "state": verdict,
                        "can_pass": verdict == "PASS",
                    },
                }
            else:
                decisions = {**decisions, "overall": verdict}
        object.__setattr__(self, "decisions", _freeze(decisions))
        object.__setattr__(
            self,
            "rule_provenance",
            tuple(_freeze(_publicize_mapping(item, _provenance_context=True)) for item in self.rule_provenance),
        )
        object.__setattr__(self, "input_confirmation", safe_confirmation)
        waiver_projections = []
        for item in self.waiver_decisions:
            decision = _waiver_decision(item)
            if decision is not None:
                waiver_projections.append(_waiver_plain(decision))
        object.__setattr__(self, "waiver_decisions", tuple(_freeze(_publicize_mapping(item)) for item in waiver_projections))
        object.__setattr__(self, "search_complete", search_complete)
        object.__setattr__(self, "optimality", optimality)
        object.__setattr__(self, "allocation_ambiguous", bool(allocation_ambiguous))
        object.__setattr__(self, "feasibility", feasibility)
        object.__setattr__(self, "feasible_witness", bool(feasible_witness))
        object.__setattr__(self, "route_ambiguity", bool(route_ambiguity))
        object.__setattr__(self, "decision_ambiguity", bool(decision_ambiguity))
        alternatives = () if not confirmation_valid else (self.alternatives or allocation.alternative_allocations)
        object.__setattr__(
            self,
            "alternatives",
            tuple(_freeze(_publicize_mapping(item)) for item in alternatives),
        )
        statistics = _thaw(_publicize_mapping(self.statistics))
        # Public evidence references change the body. Rehash only a body whose
        # incoming digest was valid; preserve corrupt input for fail-closed
        # projection validation instead of accidentally repairing its checksum.
        if isinstance(self.statistics, Mapping) and self.statistics.get("statistics_digest") == statistics_digest(self.statistics):
            statistics = {**statistics, "statistics_digest": statistics_digest(statistics)}
        object.__setattr__(self, "statistics", _freeze(statistics))
        object.__setattr__(self, "remediation_suggestions", tuple(_text(item) for item in self.remediation_suggestions if _text(item)))
        object.__setattr__(
            self,
            "non_credit_results",
            tuple(_freeze(_publicize_mapping(item)) for item in self.non_credit_results if isinstance(item, Mapping)),
        )
        object.__setattr__(
            self,
            "subset_results",
            tuple(_freeze(_publicize_mapping(item)) for item in self.subset_results if isinstance(item, Mapping)),
        )

    @property
    def graduation_verdict(self) -> str:
        return self.verdict

    @property
    def can_pass(self) -> bool:
        return self.verdict == "PASS" and self.allocation.pass_eligible and not self.blockers

    def as_dict(self) -> Mapping[str, Any]:
        """Return the complete read-only projection shared by all consumers.

        This is intentionally rebuilt from the frozen value objects instead
        of exposing ``AllocationResult.__dict__``.  The projection contains
        every numeric/route decision needed by UI, charts, and exporters,
        while retaining no resolver callbacks, transcript bytes, credentials,
        or caller-provided identity fields.
        """

        return _freeze(
            {
                "snapshot_id": self.snapshot_id,
                "evaluated_at": self.evaluated_at,
                "request": _thaw(self.request),
                "attempts": tuple(_attempt_plain(item) for item in self.attempts),
                "requirements": tuple(_requirement_plain(item) for item in self.requirements),
                "bindings": _thaw(self.bindings),
                "curriculum": _thaw(self.curriculum),
                "rule_resolution": _thaw(self.rule_resolution),
                "evidence": _thaw(self.evidence),
                "verdict": self.verdict,
                "blockers": self.blockers,
                "warnings": self.warnings,
                "schema_version": self.schema_version,
                "engine_version": self.engine_version,
                "decisions": _thaw(self.decisions),
                "rule_provenance": _thaw(self.rule_provenance),
                "input_confirmation": _thaw(self.input_confirmation),
                "waiver_decisions": _thaw(self.waiver_decisions),
                "search_complete": self.search_complete,
                "optimality": self.optimality,
                "allocation_ambiguous": self.allocation_ambiguous,
                "feasibility": self.feasibility,
                "feasible_witness": self.feasible_witness,
                "route_ambiguity": self.route_ambiguity,
                "decision_ambiguity": self.decision_ambiguity,
                "alternatives": _thaw(self.alternatives),
                "statistics": _thaw(self.statistics),
                "remediation_suggestions": self.remediation_suggestions,
                "non_credit_results": _thaw(self.non_credit_results),
                "subset_results": _thaw(self.subset_results),
                "allocation": _allocation_plain(self.allocation, self.requirements),
                # Keep the old scalar alias for existing adapters while the
                # nested allocation object becomes the canonical payload.
                "allocation_status": self.allocation.status,
            }
        )


def build_decision_snapshot(
    request: Mapping[str, Any] | None,
    *,
    curriculum_resolver: Callable[[str], Any] | None = None,
    evidence_resolver: Callable[[str], Any] | None = None,
    evaluated_at: str | None = None,
) -> DecisionSnapshot:
    """Build one deterministic snapshot without retaining boundary objects.

    ``request`` is intentionally a mapping of planner inputs rather than a
    trust container.  Resolver records are addressed only by opaque IDs and
    are projected to safe provenance fields before the snapshot is returned.
    """

    source = request if isinstance(request, Mapping) else {}
    at = _text(evaluated_at) or _text(source.get("evaluated_at")) or "UNSPECIFIED"
    attempts_all, requirements, bindings = _normalized_inputs(source)
    waiver_decisions = _normalized_waiver_decisions(source)
    confirmation_value = source.get("input_confirmation", source.get("confirmation"))
    safe_confirmation, confirmation_valid = _safe_confirmation_projection(
        confirmation_value,
        attempt_count=len(attempts_all),
    )
    # Formal course attempts are released only after the exact confirmation
    # projection has passed the boundary contract.  Requirements and their
    # provenance remain visible for remediation, but no unconfirmed attempt
    # can enter the allocator or the snapshot projection.
    attempts = attempts_all if confirmation_valid else ()
    safe_request = _safe_request(source, at)
    curriculum, rule_resolution, curriculum_blockers = _curriculum_projection(source, curriculum_resolver)
    evidence, evidence_blockers = _evidence_projection(source, evidence_resolver)
    allocation = allocate_credits(
        attempts,
        requirements,
        bindings,
        search_limit=source.get("search_limit", 10000),
        waiver_decisions=waiver_decisions,
    )
    missing_input_blockers = ("REQUIREMENTS_MISSING",) if not requirements else ()
    confirmation_blockers = () if confirmation_valid else ("INPUT_UNCONFIRMED",)
    blockers = tuple(dict.fromkeys((*missing_input_blockers, *confirmation_blockers, *curriculum_blockers, *evidence_blockers, *allocation.blockers)))
    verdict = allocation.status if confirmation_valid else "UNKNOWN"
    if blockers and verdict == "PASS":
        verdict = "UNKNOWN"
    initial = DecisionSnapshot(
        snapshot_id="",
        evaluated_at=at,
        request=safe_request,
        attempts=attempts,
        requirements=requirements,
        bindings=tuple(_binding_plain(item) for item in bindings),
        curriculum=curriculum,
        rule_resolution=rule_resolution,
        evidence=evidence,
        allocation=allocation,
        verdict=verdict,
        blockers=blockers,
        warnings=allocation.warnings,
        input_confirmation=safe_confirmation,
        search_complete=source.get("search_complete") if isinstance(source.get("search_complete"), bool) else None,
        optimality=_text(source.get("optimality")),
        allocation_ambiguous=source.get("allocation_ambiguous") if isinstance(source.get("allocation_ambiguous"), bool) else None,
        feasibility=_text(source.get("feasibility")),
        feasible_witness=source.get("feasible_witness") if isinstance(source.get("feasible_witness"), bool) else None,
        route_ambiguity=source.get("route_ambiguity") if isinstance(source.get("route_ambiguity"), bool) else None,
        decision_ambiguity=source.get("decision_ambiguity") if isinstance(source.get("decision_ambiguity"), bool) else None,
        waiver_decisions=tuple(_waiver_plain(item) for item in waiver_decisions),
    )
    # Construct the content address from the exact frozen projection so the
    # ID covers every field consumed by UI, charts and exports, including the
    # fail-closed confirmation state.
    canonical = _thaw(initial.as_dict())
    canonical.pop("snapshot_id", None)
    snapshot_id = f"snapshot:{_digest(canonical)}"
    return replace(initial, snapshot_id=snapshot_id)


__all__ = ["DecisionSnapshot", "build_decision_snapshot"]
