"""Deterministic, evidence-aware credit allocation primitives.

The allocator is intentionally independent from the existing graduation
engine.  It operates on immutable value objects and returns a complete,
auditable result: exclusive allocations conserve transcript credits, while
approved shared-credit mappings are represented separately as shadow rows.
Unknown evidence is never promoted to a passing result.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

# Feasibility is deliberately separate from the allocator's PASS/FAIL
# headline.  A bounded search can retain a verified witness even when it
# cannot establish global optimality, while a search with no witness must not
# invent either a failure or a minimum.
FEASIBLE = "FEASIBLE"
INFEASIBLE = "INFEASIBLE"

VERIFIED = "VERIFIED"
CONFLICTED = "CONFLICTED"
MISSING = "MISSING"
MANUAL_REVIEW = "MANUAL_REVIEW"
# A membership row may carry an explicit, server-owned negative assertion.
# Absence of a row is intentionally different: it remains unresolved for
# subset accounting until an adapter supplies this state with an auditable
# negative source.
NOT_MEMBER = "NOT_MEMBER"

_TRUSTED_NEGATIVE_MEMBERSHIP_KINDS = frozenset(
    {
        "official_negative",
        "public_catalog_negative",
        "registry_negative",
        "server_owned_negative",
    }
)

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
NONE = "NONE"

LECTURE = "LECTURE"
LAB = "LAB"
COMBINED = "COMBINED"
WAIVER = "WAIVER"

EXCLUSIVE = "EXCLUSIVE"
SHARED_SHADOW = "SHARED_SHADOW"
SHARED_BLOCKED = "SHARED_BLOCKED"

BEST_ATTEMPT_ONLY = "BEST_ATTEMPT_ONLY"
REPEATABLE = "REPEATABLE"

PRIMARY_TO_TARGET = "PRIMARY_TO_TARGET"
TARGET_TO_PRIMARY = "TARGET_TO_PRIMARY"
SHARED_LIMIT = Decimal("6")

_ZERO = Decimal("0")
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


def _decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value)) if value not in (None, "") else default
        except (InvalidOperation, TypeError, ValueError):
            return default
    if not result.is_finite():
        return default
    return result


def _nonnegative_decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    return max(_ZERO, _decimal(value, default))


def _is_valid_nonnegative_decimal(value: Any) -> bool:
    """Return whether a required-credit input is a finite non-negative number."""

    if value is None or value == "":
        return False
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return result.is_finite() and result >= _ZERO


def _nonnegative_integer(value: Any) -> int | None:
    """Parse a finite, non-negative integer without accepting booleans."""

    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result < _ZERO or result != result.to_integral_value():
        return None
    return int(result)


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float, bool)) else ""


def _norm_status(value: Any) -> str:
    text = _text(value).upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "PASS": PASS,
        "PASSED": PASS,
        "COMPLETE": PASS,
        "COMPLETED": PASS,
        "IN_PROGRESS": "IN_PROGRESS",
        "CURRENT": "IN_PROGRESS",
        "TAKING": "IN_PROGRESS",
        "FAIL": FAIL,
        "FAILED": FAIL,
        "WITHDRAWN": FAIL,
        "WAIVER": WAIVER,
        "WAIVED": WAIVER,
        "UNKNOWN": UNKNOWN,
        "MANUAL_REVIEW": MANUAL_REVIEW,
        "CONFLICTED": CONFLICTED,
        "MISSING": MISSING,
    }
    return aliases.get(text, text or UNKNOWN)


def _norm_evidence_state(value: Any) -> str:
    text = _text(value).upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "NOT_IN_POOL": NOT_MEMBER,
        "EXPLICIT_NOT_MEMBER": NOT_MEMBER,
        "NON_MEMBER": NOT_MEMBER,
    }
    text = aliases.get(text, text)
    return text if text in {VERIFIED, CONFLICTED, MISSING, MANUAL_REVIEW, NOT_MEMBER} else UNKNOWN


def _norm_membership_kind(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_") or "explicit"


def _norm_coverage(value: Any) -> str:
    text = _text(value).upper().replace("-", "_").replace(" ", "_")
    return text if text in {COMPLETE, PARTIAL, NONE} else NONE


_COMPONENT_TOKEN_RE = re.compile(r"[\s_\-/\\()（）]+")
_COMPONENT_ALIASES = {
    LECTURE: frozenset(
        {
            "lecture",
            "theory",
            "theoretical",
            "講授",
            "講授課",
            "理論",
            "理論課",
            "授課",
            "學科",
        }
    ),
    LAB: frozenset(
        {
            "lab",
            "laboratory",
            "practical",
            "實驗",
            "實驗課",
            "實習",
            "實習課",
            "實作",
        }
    ),
    COMBINED: frozenset(
        {
            "combined",
            "lecturelab",
            "lectureandlab",
            "lecturelaboratory",
            "講授實驗",
            "講授與實驗",
            "合併",
            "綜合",
        }
    ),
}


def _component_token(value: Any) -> str:
    """Normalize a component label without substring interpretation."""

    return _COMPONENT_TOKEN_RE.sub("", _text(value).casefold())


def _norm_course_kind(value: Any) -> str:
    token = _component_token(value)
    for kind, aliases in _COMPONENT_ALIASES.items():
        if token in aliases:
            return kind
    # Missing or unrecognised component metadata cannot be safely treated as
    # a lecture: doing so could make a lecture satisfy a lab requirement.
    return UNKNOWN


def normalize_course_kind(value: Any) -> str:
    """Public shared component-token normalizer for input adapters."""

    return _norm_course_kind(value)


def _norm_repeat_policy(value: Any, repeatable: bool = False) -> str:
    if repeatable:
        return REPEATABLE
    text = _text(value).upper().replace("-", "_").replace(" ", "_")
    return REPEATABLE if text in {REPEATABLE, "ALLOW_REPEAT", "REPEATABLE_ATTEMPTS"} else BEST_ATTEMPT_ONLY


def _norm_direction(value: Any) -> str:
    text = _text(value).upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "PRIMARY_TO_TARGET": PRIMARY_TO_TARGET,
        "PRIMARY2TARGET": PRIMARY_TO_TARGET,
        "HOME_TO_TARGET": PRIMARY_TO_TARGET,
        "TARGET_TO_PRIMARY": TARGET_TO_PRIMARY,
        "TARGET2PRIMARY": TARGET_TO_PRIMARY,
        "SECONDARY_TO_PRIMARY": TARGET_TO_PRIMARY,
    }
    return aliases.get(text, text if text in {PRIMARY_TO_TARGET, TARGET_TO_PRIMARY} else "")


def _tuple_text(values: Any, *, preserve_order: bool = False) -> tuple[str, ...]:
    if values in (None, ""):
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        return ()
    unique = {_text(item) for item in values}
    unique.discard("")
    return tuple(item for item in (values if preserve_order else sorted(unique)) if _text(item) in unique) if preserve_order else tuple(sorted(unique))


def _freeze_contract_value(value: Any) -> Any:
    """Freeze a small JSON-shaped contract value at a value-object boundary."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_contract_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_contract_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted((_freeze_contract_value(item) for item in value), key=repr))
    return value


def _membership_evidence_records(value: Any) -> tuple[tuple[str, str, str, str], ...]:
    """Normalize per-membership evidence without trusting caller mappings.

    The fourth field distinguishes an explicitly produced policy membership
    from the legacy global pool marker.  It is intentionally internal: the
    public projection exposes the first three auditable fields only.
    """

    if isinstance(value, Mapping):
        value = tuple(
            {"pool_id": key, "evidence_state": item}
            if not isinstance(item, Mapping)
            else {"pool_id": key, **dict(item)}
            for key, item in value.items()
        )
    if isinstance(value, str) or not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    records: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for item in value:
        pool_id = ""
        state: Any = UNKNOWN
        source = ""
        kind = "explicit"
        if isinstance(item, Mapping):
            pool_id = _text(item.get("pool_id") or item.get("membership_id") or item.get("id"))
            state = item.get("evidence_state", item.get("state", item.get("status", UNKNOWN)))
            source = _text(item.get("source_reference") or item.get("evidence_reference"))
            kind = _norm_membership_kind(item.get("membership_kind") or item.get("kind"))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            values = tuple(item)
            if values:
                pool_id = _text(values[0])
                state = values[1] if len(values) > 1 else UNKNOWN
                source = _text(values[2]) if len(values) > 2 else ""
                kind = _norm_membership_kind(values[3]) if len(values) > 3 else "explicit"
        if not pool_id or pool_id in seen:
            continue
        seen.add(pool_id)
        records.append((pool_id, _norm_evidence_state(state), source, kind or "explicit"))
    return tuple(records)


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _safe_diagnostic(value: Any) -> str:
    """Mask opaque evidence/binding references in public diagnostics."""

    text = _text(value)
    for prefix in _DIAGNOSTIC_ID_PREFIXES:
        if text.startswith(prefix):
            if ":evidence:" in text:
                return text
            head, _, tail = text.rpartition(":")
            if tail:
                return f"{head}:evidence:{hashlib.sha256(tail.encode('utf-8')).hexdigest()[:12]}"
    return text


@dataclass(frozen=True, slots=True)
class CourseAttempt:
    """One immutable transcript attempt; no student identity is stored."""

    attempt_id: str
    course_id: str
    course_name: str
    credits: Decimal
    earned_credits: Decimal | None = None
    academic_term: str = ""
    repeat_group_id: str | None = None
    identity_status: str = VERIFIED
    course_kind: str = LECTURE
    pool_memberships: tuple[str, ...] = ()
    status: str = PASS
    source_kind: str = "TRANSCRIPT"
    name: str = ""
    term: str = ""
    grade: str = ""
    # Missing posted-grade evidence is unresolved.  Legacy call sites that
    # intentionally exercise a verified repeat rule must opt in explicitly.
    grade_evidence_state: str = UNKNOWN
    # A source-owned effective-attempt/repeat-selection rule may explicitly
    # identify the one winner for a fixed repeat group.  The flag alone is
    # never trusted; it must carry VERIFIED selection evidence.
    effective_attempt: bool = False
    repeat_selection_evidence_state: str = UNKNOWN
    # ``pool_ids`` is the registry-facing spelling.  ``pool_memberships`` is
    # retained for existing allocator callers; both are normalized to the
    # same immutable value.  The evidence marker prevents a raw transcript
    # mapping from self-assigning an official course pool.
    pool_ids: tuple[str, ...] = ()
    pool_evidence_state: str = VERIFIED
    # A policy can establish one membership while another (for example the
    # science-college subset of free electives) remains unresolved.  Keep
    # those states independent instead of collapsing them into one global
    # marker.  Each normalized item is ``(pool_id, state, source, kind)``.
    pool_membership_evidence: tuple[Any, ...] = ()
    # Optional catalog scope used by official term/version-bound completion
    # gates.  Ordinary transcript attempts may leave these empty.
    curriculum_version: str = ""
    program_slug: str = ""
    track_slug: str = ""

    def __post_init__(self):
        course_name = _text(self.course_name) or _text(self.name) or _text(self.course_id)
        course_id = _text(self.course_id) or course_name
        credits = _nonnegative_decimal(self.credits)
        status = _norm_status(self.status)
        earned = _nonnegative_decimal(self.earned_credits, credits if status == PASS else _ZERO)
        earned = min(credits, earned)
        academic_term = _text(self.academic_term) or _text(self.term)
        repeat_group = _text(self.repeat_group_id) or None
        attempt_id = _text(self.attempt_id)
        if not attempt_id:
            attempt_id = f"attempt:{_stable_digest([course_id, course_name, academic_term, str(credits), repeat_group or ''])}"
        object.__setattr__(self, "attempt_id", attempt_id)
        object.__setattr__(self, "course_id", course_id)
        object.__setattr__(self, "course_name", course_name)
        object.__setattr__(self, "name", course_name)
        object.__setattr__(self, "credits", credits)
        object.__setattr__(self, "earned_credits", earned)
        object.__setattr__(self, "academic_term", academic_term)
        object.__setattr__(self, "term", academic_term)
        object.__setattr__(self, "repeat_group_id", repeat_group)
        object.__setattr__(self, "identity_status", _norm_evidence_state(self.identity_status))
        object.__setattr__(self, "course_kind", _norm_course_kind(self.course_kind))
        pool_memberships = self.pool_memberships or self.pool_ids
        pool_memberships = _tuple_text(pool_memberships)
        pool_evidence_state = _norm_evidence_state(self.pool_evidence_state)
        records = list(_membership_evidence_records(self.pool_membership_evidence))
        seen_records = {item[0] for item in records}
        # Legacy typed attempts with one global state remain source
        # compatible.  Explicit records take precedence for their own pool.
        for pool_id in pool_memberships:
            if pool_id not in seen_records:
                records.append((pool_id, pool_evidence_state, "", "legacy"))
        for pool_id, _state, _source, _kind in records:
            if pool_id not in pool_memberships:
                pool_memberships = (*pool_memberships, pool_id)
        pool_memberships = _tuple_text(pool_memberships)
        object.__setattr__(self, "pool_memberships", pool_memberships)
        object.__setattr__(self, "pool_ids", pool_memberships)
        object.__setattr__(self, "pool_evidence_state", pool_evidence_state)
        object.__setattr__(self, "pool_membership_evidence", tuple(records))
        object.__setattr__(self, "curriculum_version", _text(self.curriculum_version))
        object.__setattr__(self, "program_slug", _text(self.program_slug))
        object.__setattr__(self, "track_slug", _text(self.track_slug))
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_kind", _text(self.source_kind) or "TRANSCRIPT")
        object.__setattr__(self, "grade", _text(self.grade))
        object.__setattr__(self, "grade_evidence_state", _norm_evidence_state(self.grade_evidence_state))
        object.__setattr__(self, "effective_attempt", bool(self.effective_attempt))
        object.__setattr__(self, "repeat_selection_evidence_state", _norm_evidence_state(self.repeat_selection_evidence_state))

    @property
    def available_credits(self) -> Decimal:
        return self.earned_credits if self.status == PASS else _ZERO


@dataclass(frozen=True, slots=True)
class RequirementSpec:
    """One exact requirement or bounded credit bucket."""

    requirement_id: str
    name: str = ""
    credits_required: Decimal = _ZERO
    max_credits: Decimal | None = None
    eligible_course_ids: tuple[str, ...] = ()
    eligible_pool_ids: tuple[str, ...] = ()
    overflow_routes: tuple[str, ...] = ()
    allowed_course_kinds: tuple[str, ...] = ()
    course_kind: str | None = None
    coverage_state: str = COMPLETE
    evidence_state: str = VERIFIED
    required: bool = True
    repeatable: bool = False
    repeat_policy: str = BEST_ATTEMPT_ONLY
    waiver: bool = False
    eligible_course_names: tuple[str, ...] = ()
    accept_any: bool = False
    bucket: str = ""
    required_credits: Decimal | None = None
    credit_cap: Decimal | None = None
    course_ids: tuple[str, ...] = ()
    pool_ids: tuple[str, ...] = ()
    allowed_kinds: tuple[str, ...] = ()
    coverage: str = ""
    evidence: str = ""
    kind: str = ""
    # Explicit ownership/domain metadata is required when a shared binding
    # projects credit between curricula.  The aliases keep adapters that use
    # ``role`` or ``curriculum_role`` source-compatible.
    owner: str = ""
    domain: str = ""
    role: str = ""
    curriculum_role: str = ""
    requirement_domain: str = ""
    # Optional source scope carried by service-compiled requirements.  It is
    # used to bind zero-credit subset-gate waivers to the exact curriculum
    # revision without changing the ordinary allocator contract.
    curriculum_version: str = ""
    program_slug: str = ""
    track_slug: str = ""
    # Constraints over the same EXCLUSIVE portions assigned to this
    # requirement.  They never create a second credit consumer.
    subset_constraints: tuple[Mapping[str, Any], ...] = ()
    # Keep validation evidence after the numeric fields are normalized.  This
    # prevents invalid input such as ``-3`` or ``"unknown"`` from becoming a
    # zero-credit requirement that can accidentally pass.
    credits_required_valid: bool = field(default=True, init=False)

    def __post_init__(self):
        requirement_id = _text(self.requirement_id)
        name = _text(self.name) or _text(self.bucket) or requirement_id
        raw_required = self.required_credits if self.required_credits is not None else self.credits_required
        required_valid = _is_valid_nonnegative_decimal(raw_required)
        required = _nonnegative_decimal(raw_required)
        cap_value = self.credit_cap if self.credit_cap is not None else self.max_credits
        cap = _nonnegative_decimal(cap_value, required) if cap_value is not None else required
        cap = max(required, cap)
        course_ids = self.eligible_course_ids or self.course_ids
        pool_ids = self.eligible_pool_ids or self.pool_ids
        allowed_kinds = self.allowed_course_kinds or self.allowed_kinds
        if self.course_kind:
            allowed_kinds = (_norm_course_kind(self.course_kind),)
        normalized_kinds = tuple(_norm_course_kind(item) for item in _tuple_text(allowed_kinds))
        object.__setattr__(self, "requirement_id", requirement_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "credits_required", required)
        object.__setattr__(self, "required_credits", required)
        object.__setattr__(self, "credits_required_valid", required_valid)
        object.__setattr__(self, "max_credits", cap)
        object.__setattr__(self, "credit_cap", cap)
        object.__setattr__(self, "eligible_course_ids", _tuple_text(course_ids))
        object.__setattr__(self, "course_ids", _tuple_text(course_ids))
        object.__setattr__(self, "eligible_pool_ids", _tuple_text(pool_ids))
        object.__setattr__(self, "pool_ids", _tuple_text(pool_ids))
        object.__setattr__(self, "eligible_course_names", _tuple_text(self.eligible_course_names))
        object.__setattr__(self, "overflow_routes", _tuple_text(self.overflow_routes, preserve_order=True))
        object.__setattr__(self, "allowed_course_kinds", normalized_kinds)
        object.__setattr__(self, "allowed_kinds", normalized_kinds)
        object.__setattr__(self, "course_kind", _norm_course_kind(self.course_kind) if self.course_kind else None)
        object.__setattr__(self, "coverage_state", _norm_coverage(self.coverage or self.coverage_state))
        object.__setattr__(self, "evidence_state", _norm_evidence_state(self.evidence or self.evidence_state))
        object.__setattr__(self, "repeat_policy", _norm_repeat_policy(self.repeat_policy, self.repeatable))
        object.__setattr__(self, "repeatable", self.repeat_policy == REPEATABLE or bool(self.repeatable))
        object.__setattr__(self, "waiver", bool(self.waiver) or _text(self.kind).upper() == WAIVER)
        object.__setattr__(self, "bucket", _text(self.bucket) or requirement_id)
        object.__setattr__(self, "kind", _text(self.kind))
        owner = _text(self.owner or self.role or self.curriculum_role).upper().replace("-", "_").replace(" ", "_")
        domain = _text(self.domain or self.requirement_domain)
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "role", owner)
        object.__setattr__(self, "curriculum_role", owner)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "requirement_domain", domain)
        object.__setattr__(self, "curriculum_version", _text(self.curriculum_version))
        object.__setattr__(self, "program_slug", _text(self.program_slug))
        object.__setattr__(self, "track_slug", _text(self.track_slug))
        raw_constraints = self.subset_constraints
        if isinstance(raw_constraints, Mapping):
            raw_constraints = (raw_constraints,)
        if not isinstance(raw_constraints, Sequence) or isinstance(raw_constraints, (str, bytes, bytearray)):
            raw_constraints = ()
        constraints = tuple(
            _freeze_contract_value(item)
            for item in raw_constraints
            if isinstance(item, Mapping)
        )
        object.__setattr__(self, "subset_constraints", constraints)


@dataclass(frozen=True, slots=True)
class EquivalencyBinding:
    """An exact, auditable source-attempt to requirement decision."""

    binding_id: str
    source_attempt_id: str
    target_requirement_id: str
    approved_credits: Decimal = _ZERO
    # A binding is an externally asserted decision.  It must carry explicit
    # evidence; a typed constructor with omitted fields is not approval.
    evidence_state: str = UNKNOWN
    authority: str = ""
    evidence_reference: str = ""
    direction: str = ""
    shared: bool = False
    source_course_id: str = ""
    target_course_id: str = ""
    source_course_kind: str = ""
    allocation_kind: str = ""
    credits: Decimal | None = None
    decision: str = "PENDING"
    scope: str = ""
    # Shared credit must name the exact source requirement that owns the
    # exclusive allocation.  Domain/owner hints are optional aliases, but if
    # supplied they must agree with the source/target requirement metadata.
    source_requirement_id: str = ""
    source_domain: str = ""
    target_domain: str = ""
    source_owner: str = ""
    target_owner: str = ""

    def __post_init__(self):
        binding_id = _text(self.binding_id)
        source_id = _text(self.source_attempt_id)
        target_id = _text(self.target_requirement_id)
        direction = _norm_direction(self.direction)
        allocation_kind = _text(self.allocation_kind).upper().replace("-", "_")
        # ``shared`` is an input trust boundary.  Python truthiness would
        # treat values such as ``"false"`` and ``1`` as approval.  Only a
        # literal boolean can control the flag; a literal ``False`` may still
        # be accompanied by an explicit shared direction/kind, preserving the
        # established shorthand for an intentionally shared mapping.
        explicit_shared_marker = direction in {PRIMARY_TO_TARGET, TARGET_TO_PRIMARY} or allocation_kind in {SHARED_SHADOW, "SHARED_REUSE"}
        shared = self.shared is True or (self.shared is False and explicit_shared_marker)
        if not binding_id:
            binding_id = f"binding:{_stable_digest([source_id, target_id, str(self.approved_credits), direction])}"
        amount = _nonnegative_decimal(self.credits if self.credits is not None else self.approved_credits)
        object.__setattr__(self, "binding_id", binding_id)
        object.__setattr__(self, "source_attempt_id", source_id)
        object.__setattr__(self, "target_requirement_id", target_id)
        object.__setattr__(self, "approved_credits", amount)
        object.__setattr__(self, "credits", amount)
        object.__setattr__(self, "evidence_state", _norm_evidence_state(self.evidence_state))
        object.__setattr__(self, "authority", _text(self.authority))
        object.__setattr__(self, "evidence_reference", _text(self.evidence_reference))
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "shared", shared)
        object.__setattr__(self, "source_course_id", _text(self.source_course_id))
        object.__setattr__(self, "target_course_id", _text(self.target_course_id))
        object.__setattr__(self, "source_course_kind", _norm_course_kind(self.source_course_kind) if self.source_course_kind else "")
        object.__setattr__(self, "allocation_kind", allocation_kind or (SHARED_SHADOW if shared else EXCLUSIVE))
        object.__setattr__(self, "decision", _text(self.decision).upper() or "PENDING")
        object.__setattr__(self, "scope", _text(self.scope))
        object.__setattr__(self, "source_requirement_id", _text(self.source_requirement_id))
        object.__setattr__(self, "source_domain", _text(self.source_domain))
        object.__setattr__(self, "target_domain", _text(self.target_domain))
        object.__setattr__(self, "source_owner", _text(self.source_owner).upper().replace("-", "_").replace(" ", "_"))
        object.__setattr__(self, "target_owner", _text(self.target_owner).upper().replace("-", "_").replace(" ", "_"))


@dataclass(frozen=True, slots=True)
class WaiverDecision:
    """An explicit, verified student-specific waiver decision.

    ``RequirementSpec.waiver`` only describes that a requirement *may* have a
    waiver path.  It is never itself evidence that the student received one.
    This record is the separate proof required to satisfy that path and does
    not carry any earned credit.
    """

    decision_id: str
    target_requirement_id: str
    evidence_state: str = UNKNOWN
    authority: str = ""
    evidence_reference: str = ""
    decision: str = "PENDING"
    # Subset-gate waivers are subject/version-bound.  These additive fields
    # remain empty for the legacy generic waiver contract.
    subject_ref: str = ""
    curriculum_version: str = ""
    requirement_version: str = ""
    program_slug: str = ""
    track_slug: str = ""

    def __post_init__(self):
        decision_id = _text(self.decision_id)
        target = _text(self.target_requirement_id)
        if not decision_id:
            decision_id = f"waiver:{_stable_digest([target, self.authority, self.evidence_reference])}"
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "target_requirement_id", target)
        object.__setattr__(self, "evidence_state", _norm_evidence_state(self.evidence_state))
        object.__setattr__(self, "authority", _text(self.authority))
        object.__setattr__(self, "evidence_reference", _text(self.evidence_reference))
        object.__setattr__(self, "decision", _text(self.decision).upper() or "PENDING")
        object.__setattr__(self, "subject_ref", _text(self.subject_ref))
        object.__setattr__(self, "curriculum_version", _text(self.curriculum_version))
        object.__setattr__(self, "requirement_version", _text(self.requirement_version))
        object.__setattr__(self, "program_slug", _text(self.program_slug))
        object.__setattr__(self, "track_slug", _text(self.track_slug))


@dataclass(frozen=True, slots=True)
class CreditPortion:
    attempt_id: str
    requirement_id: str
    credits: Decimal
    allocation_kind: str = EXCLUSIVE
    direction: str = ""
    binding_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "attempt_id", _text(self.attempt_id))
        object.__setattr__(self, "requirement_id", _text(self.requirement_id))
        object.__setattr__(self, "credits", _nonnegative_decimal(self.credits))
        object.__setattr__(self, "allocation_kind", _text(self.allocation_kind).upper() or EXCLUSIVE)
        object.__setattr__(self, "direction", _norm_direction(self.direction))
        object.__setattr__(self, "binding_id", _text(self.binding_id))

    @property
    def kind(self) -> str:
        return self.allocation_kind


@dataclass(frozen=True, slots=True)
class AttemptAllocation:
    attempt_id: str
    source_credits: Decimal
    portions: tuple[CreditPortion, ...] = ()
    unallocated_credits: Decimal = _ZERO

    def __post_init__(self):
        portions = tuple(sorted(self.portions, key=lambda item: (item.requirement_id, item.allocation_kind, item.binding_id)))
        source = _nonnegative_decimal(self.source_credits)
        unallocated = _nonnegative_decimal(self.unallocated_credits)
        object.__setattr__(self, "attempt_id", _text(self.attempt_id))
        object.__setattr__(self, "source_credits", source)
        object.__setattr__(self, "portions", portions)
        object.__setattr__(self, "unallocated_credits", unallocated)


@dataclass(frozen=True, slots=True)
class RequirementResult:
    requirement_id: str
    status: str
    required_credits: Decimal
    exclusive_credits: Decimal
    shared_shadow_credits: Decimal
    effective_credits: Decimal
    deficit: Decimal
    coverage_state: str
    evidence_state: str
    waived: bool = False
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SharedLedger:
    direction: str
    used_credits: Decimal
    limit_credits: Decimal = SHARED_LIMIT
    blocked_credits: Decimal = _ZERO
    binding_ids: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "direction", _norm_direction(self.direction))
        object.__setattr__(self, "used_credits", _nonnegative_decimal(self.used_credits))
        object.__setattr__(self, "limit_credits", _nonnegative_decimal(self.limit_credits, SHARED_LIMIT))
        object.__setattr__(self, "blocked_credits", _nonnegative_decimal(self.blocked_credits))
        object.__setattr__(self, "binding_ids", _tuple_text(self.binding_ids))


@dataclass(frozen=True, slots=True)
class BindingAssessment:
    binding_id: str
    status: str
    reason: str
    direction: str = ""
    approved_credits: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class WaiverAssessment:
    decision_id: str
    target_requirement_id: str
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class AllocationResult:
    status: str
    allocations: tuple[AttemptAllocation, ...]
    requirement_results: tuple[RequirementResult, ...]
    source_earned_credits: Decimal
    recognized_credits: Decimal
    unallocated_credits: Decimal
    credit_conservation: bool
    shadow_allocations: tuple[CreditPortion, ...] = ()
    shared_ledgers: tuple[SharedLedger, ...] = ()
    binding_assessments: tuple[BindingAssessment, ...] = ()
    waiver_assessments: tuple[WaiverAssessment, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    search_exhausted: bool = False
    pass_eligible: bool = False
    nodes_searched: int = 0
    objective: tuple[Any, ...] = ()
    # Every distinct best-score assignment is retained as an immutable
    # signature.  A signature is deliberately smaller than a second full
    # result, but still identifies the course-to-requirement routes that make
    # the branch meaningfully different.
    alternative_allocations: tuple[tuple[Any, ...], ...] = ()
    allocation_ambiguous: bool = False
    # Independent search/decision metadata.  These fields are additive and
    # let consumers display a feasible witness without claiming uniqueness or
    # global optimality.
    feasibility: str = ""
    search_complete: bool | None = None
    optimality: str = ""
    route_ambiguity: bool = False
    decision_ambiguity: bool = False
    feasible_witness: bool | None = None
    # Observational policy-subset outcomes.  These rows share the parent
    # requirement's exclusive portions and never alter the credit ledger.
    subset_results: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        status = _text(self.status) or UNKNOWN
        # These fields are trust-boundary metadata, not ordinary truthy
        # configuration.  A string such as ``"false"`` or an integer zero
        # must never certify a safe allocation.  Invalid values are retained
        # only as conservative unsafe states below.
        conservation_is_bool = self.credit_conservation is True or self.credit_conservation is False
        search_exhausted_is_bool = self.search_exhausted is True or self.search_exhausted is False
        pass_eligible_is_bool = self.pass_eligible is True or self.pass_eligible is False
        allocation_ambiguous_is_bool = self.allocation_ambiguous is True or self.allocation_ambiguous is False
        route_ambiguity_is_bool = self.route_ambiguity is True or self.route_ambiguity is False
        decision_ambiguity_is_bool = self.decision_ambiguity is True or self.decision_ambiguity is False
        search_complete_is_bool = self.search_complete is None or self.search_complete is True or self.search_complete is False
        feasible_witness_is_bool = self.feasible_witness is None or self.feasible_witness is True or self.feasible_witness is False
        allocations = tuple(self.allocations)
        source_earned_credits = _nonnegative_decimal(self.source_earned_credits)
        recognized_credits = _nonnegative_decimal(self.recognized_credits)
        unallocated_credits = _nonnegative_decimal(self.unallocated_credits)

        # Recompute conservation from the immutable allocation rows instead
        # of trusting headline totals.  A malformed row can otherwise claim
        # three source credits while spending six in a single EXCLUSIVE
        # portion.  Rows are one-to-one with source attempts; duplicate IDs or
        # portions belonging to another attempt are structural mismatches.
        attempt_ids = [item.attempt_id for item in allocations]
        rows_have_unique_ids = len(attempt_ids) == len(set(attempt_ids)) and all(attempt_ids)
        rows_balanced = True
        row_source_total = _ZERO
        row_exclusive_total = _ZERO
        row_unallocated_total = _ZERO
        for allocation in allocations:
            source = _nonnegative_decimal(allocation.source_credits)
            unallocated = _nonnegative_decimal(allocation.unallocated_credits)
            exclusive = _ZERO
            for portion in allocation.portions:
                if portion.attempt_id != allocation.attempt_id:
                    rows_balanced = False
                if portion.allocation_kind == EXCLUSIVE:
                    exclusive += _nonnegative_decimal(portion.credits)
                elif _nonnegative_decimal(portion.credits) > _ZERO:
                    # Waivers are valid only as zero-credit markers.  Shared
                    # shadows and every other non-exclusive kind belong in
                    # ``shadow_allocations`` or an audit record, never inside
                    # a source allocation row.
                    rows_balanced = False
            if exclusive + unallocated != source:
                rows_balanced = False
            row_source_total += source
            row_exclusive_total += exclusive
            row_unallocated_total += unallocated
        allocation_conservation = (
            rows_have_unique_ids
            and rows_balanced
            and row_source_total == source_earned_credits
            and row_exclusive_total == recognized_credits
            and row_unallocated_total == unallocated_credits
        )
        credit_conservation = self.credit_conservation is True and allocation_conservation
        search_exhausted = self.search_exhausted is not False
        allocation_ambiguous = self.allocation_ambiguous is not False
        metadata_invalid = not all(
            (
                conservation_is_bool,
                search_exhausted_is_bool,
                pass_eligible_is_bool,
                allocation_ambiguous_is_bool,
                route_ambiguity_is_bool,
                decision_ambiguity_is_bool,
                search_complete_is_bool,
                feasible_witness_is_bool,
            )
        )
        search_complete = self.search_complete is not False and not search_exhausted
        if self.search_complete is not None and self.search_complete is not True:
            search_complete = False
        feasible_witness = (
            self.feasible_witness
            if self.feasible_witness is True or self.feasible_witness is False
            else status == PASS
            and credit_conservation
            and not search_exhausted
            and (self.search_complete is None or self.search_complete is True)
        )
        feasibility = _text(self.feasibility).upper()
        if feasibility not in {FEASIBLE, INFEASIBLE, UNKNOWN}:
            feasibility = FEASIBLE if feasible_witness else UNKNOWN if not search_complete else INFEASIBLE if status == FAIL else UNKNOWN
        if feasible_witness:
            feasibility = FEASIBLE
        optimality = _text(self.optimality).upper()
        if optimality not in {"OPTIMAL", "NON_UNIQUE", "BOUNDED_NOT_COMPLETE", "UNKNOWN"}:
            optimality = "BOUNDED_NOT_COMPLETE" if not search_complete else "NON_UNIQUE" if allocation_ambiguous else "OPTIMAL"
        if not search_complete and optimality == "OPTIMAL":
            optimality = "BOUNDED_NOT_COMPLETE"
        if status == PASS and (
            not credit_conservation
            or (search_exhausted and not feasible_witness)
            or (not feasible_witness and feasibility != FEASIBLE)
            or metadata_invalid
        ):
            status = UNKNOWN
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "allocations", allocations)
        object.__setattr__(self, "requirement_results", tuple(sorted(self.requirement_results, key=lambda item: item.requirement_id)))
        object.__setattr__(self, "source_earned_credits", source_earned_credits)
        object.__setattr__(self, "recognized_credits", recognized_credits)
        object.__setattr__(self, "unallocated_credits", unallocated_credits)
        object.__setattr__(self, "shadow_allocations", tuple(self.shadow_allocations))
        object.__setattr__(self, "shared_ledgers", tuple(sorted(self.shared_ledgers, key=lambda item: item.direction)))
        object.__setattr__(self, "binding_assessments", tuple(sorted(self.binding_assessments, key=lambda item: item.binding_id)))
        object.__setattr__(self, "waiver_assessments", tuple(sorted(self.waiver_assessments, key=lambda item: item.decision_id)))
        blockers = [_safe_diagnostic(item) for item in self.blockers if _text(item)]
        if not credit_conservation and "CREDIT_CONSERVATION_FAILED" not in blockers:
            blockers.append("CREDIT_CONSERVATION_FAILED")
        if metadata_invalid and "ALLOCATION_METADATA_INVALID" not in blockers:
            blockers.append("ALLOCATION_METADATA_INVALID")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))
        object.__setattr__(self, "warnings", tuple(dict.fromkeys(_safe_diagnostic(item) for item in self.warnings if _text(item))))
        alternatives = tuple(sorted({tuple(item) for item in self.alternative_allocations}, key=repr))
        object.__setattr__(self, "alternative_allocations", alternatives)
        object.__setattr__(self, "credit_conservation", credit_conservation)
        object.__setattr__(self, "search_exhausted", search_exhausted)
        object.__setattr__(self, "allocation_ambiguous", allocation_ambiguous)
        object.__setattr__(self, "search_complete", search_complete)
        object.__setattr__(self, "feasible_witness", bool(feasible_witness) if feasible_witness_is_bool else False)
        object.__setattr__(self, "feasibility", feasibility)
        object.__setattr__(self, "optimality", optimality)
        object.__setattr__(self, "route_ambiguity", self.route_ambiguity is True)
        object.__setattr__(self, "decision_ambiguity", self.decision_ambiguity is True)
        object.__setattr__(self, "subset_results", tuple(_freeze_contract_value(item) for item in self.subset_results if isinstance(item, Mapping)))
        object.__setattr__(
            self,
            "pass_eligible",
            self.pass_eligible is True
            and self.status == PASS
            and self.credit_conservation is True
            and (self.search_exhausted is False or self.feasible_witness is True)
            and self.feasible_witness is True,
        )

    @property
    def can_pass(self) -> bool:
        return self.pass_eligible

    @property
    def effective_recognized_credits(self) -> Decimal:
        return self.recognized_credits + sum((item.credits for item in self.shadow_allocations), _ZERO)

    def allocation_for(self, requirement_id: str) -> tuple[CreditPortion, ...]:
        return tuple(
            portion
            for allocation in self.allocations
            for portion in allocation.portions
            if portion.requirement_id == requirement_id
        )

    def requirement_for(self, requirement_id: str) -> RequirementResult | None:
        return next((item for item in self.requirement_results if item.requirement_id == requirement_id), None)

    def shared_ledger(self, direction: str) -> SharedLedger | None:
        normalized = _norm_direction(direction)
        return next((item for item in self.shared_ledgers if item.direction == normalized), None)

    @property
    def alternatives(self) -> tuple[tuple[Any, ...], ...]:
        """Compatibility/readability alias for the best-route signatures."""

        return self.alternative_allocations


def _as_attempt(value: Any) -> CourseAttempt | None:
    if isinstance(value, CourseAttempt):
        return value
    if not isinstance(value, Mapping):
        return None
    return CourseAttempt(
        attempt_id=_text(value.get("attempt_id") or value.get("id")),
        course_id=_text(value.get("course_id") or value.get("course_code") or value.get("code") or value.get("name")),
        course_name=_text(value.get("course_name") or value.get("name") or value.get("title")),
        credits=value.get("credits", value.get("credit", value.get("total_credit", 0))),
        earned_credits=value.get("earned_credits", value.get("completed_credit")),
        academic_term=value.get("academic_term", value.get("term", value.get("semester", ""))),
        repeat_group_id=value.get("repeat_group_id", value.get("repeat_group")),
        # A plain mapping is an untrusted boundary object.  Callers must
        # carry an explicit identity decision before a direct match may pass.
        identity_status=value.get("identity_status", value.get("course_identity_status", UNKNOWN)),
        course_kind=value.get("course_kind", value.get("component_type", value.get("kind", UNKNOWN))),
        pool_memberships=value.get("pool_memberships", value.get("pool_membership", value.get("pools", value.get("eligible_pools", ())))),
        status=value.get("status", PASS if value.get("is_completed", True) else "IN_PROGRESS"),
        source_kind=value.get("source_kind", "TRANSCRIPT"),
        grade=value.get("grade", ""),
        grade_evidence_state=value.get("grade_evidence_state", value.get("grade_trust", UNKNOWN)),
        effective_attempt=value.get("effective_attempt", value.get("is_effective_attempt", False)),
        repeat_selection_evidence_state=value.get(
            "repeat_selection_evidence_state",
            value.get("effective_attempt_evidence_state", value.get("repeat_selection_state", UNKNOWN)),
        ),
        pool_ids=value.get("pool_ids", ()),
        # A caller-owned mapping cannot establish registry ownership merely
        # by naming a pool.  Service-produced attempts set this explicitly
        # after a unique official catalog match.
        pool_evidence_state=value.get("pool_evidence_state", value.get("pool_evidence", UNKNOWN)),
        pool_membership_evidence=value.get(
            "pool_membership_evidence",
            value.get("membership_evidence", value.get("pool_evidence_by_id", ())),
        ),
        curriculum_version=value.get("curriculum_version", value.get("version", "")),
        program_slug=value.get("program_slug", value.get("program", "")),
        track_slug=value.get("track_slug", value.get("track", "")),
    )


def _as_requirement(value: Any) -> RequirementSpec | None:
    if isinstance(value, RequirementSpec):
        return value
    if not isinstance(value, Mapping):
        return None
    return RequirementSpec(
        requirement_id=value.get("requirement_id", value.get("id", value.get("key", ""))),
        name=value.get("name", value.get("label", value.get("title", ""))),
        credits_required=value.get("credits_required", value.get("required_credits", value.get("credits", value.get("credit", 0)))),
        max_credits=value.get("max_credits", value.get("credit_cap")),
        eligible_course_ids=value.get("eligible_course_ids", value.get("course_ids", value.get("course_id", ()))),
        eligible_pool_ids=value.get("eligible_pool_ids", value.get("pool_ids", value.get("pool_id", ()))),
        eligible_course_names=value.get("eligible_course_names", value.get("course_names", ())),
        overflow_routes=value.get("overflow_routes", value.get("overflow", ())),
        allowed_course_kinds=value.get("allowed_course_kinds", value.get("allowed_kinds", ())),
        course_kind=value.get("course_kind"),
        # A mapping has no provenance by itself.  Keep it fail-closed unless
        # the caller explicitly supplies both coverage and evidence state.
        coverage_state=value.get("coverage_state", value.get("coverage", NONE)),
        evidence_state=value.get("evidence_state", value.get("evidence", UNKNOWN)),
        required=value.get("required", True),
        repeatable=value.get("repeatable", value.get("repeatable_attempts", False)),
        repeat_policy=value.get("repeat_policy", BEST_ATTEMPT_ONLY),
        waiver=value.get("waiver", False),
        accept_any=value.get("accept_any", False),
        bucket=value.get("bucket", ""),
        kind=value.get("kind", ""),
        owner=value.get("owner", value.get("role", value.get("curriculum_role", ""))),
        domain=value.get("domain", value.get("requirement_domain", "")),
        curriculum_version=value.get("curriculum_version", value.get("version", "")),
        program_slug=value.get("program_slug", value.get("program", "")),
        track_slug=value.get("track_slug", value.get("track", "")),
        subset_constraints=value.get("subset_constraints", value.get("subsets", ())),
    )


def _as_binding(value: Any) -> EquivalencyBinding | None:
    if isinstance(value, EquivalencyBinding):
        return value
    if not isinstance(value, Mapping):
        return None
    return EquivalencyBinding(
        binding_id=value.get("binding_id", value.get("id", value.get("record_id", ""))),
        source_attempt_id=value.get("source_attempt_id", value.get("source_id", value.get("attempt_id", ""))),
        target_requirement_id=value.get("target_requirement_id", value.get("requirement_id", value.get("target_id", ""))),
        approved_credits=value.get("approved_credits", value.get("credits", value.get("credit", 0))),
        evidence_state=value.get("evidence_state", value.get("status", UNKNOWN)),
        authority=value.get("authority", ""),
        evidence_reference=value.get("evidence_reference", value.get("source_reference", value.get("evidence_id", ""))),
        direction=value.get("direction", value.get("ledger", "")),
        shared=value.get("shared", False),
        source_course_id=value.get("source_course_id", value.get("source_course_code", "")),
        target_course_id=value.get("target_course_id", value.get("target_course_code", "")),
        source_course_kind=value.get("source_course_kind", value.get("course_kind", "")),
        allocation_kind=value.get("allocation_kind", value.get("kind", "")),
        decision=value.get("decision", "PENDING"),
        scope=value.get("scope", ""),
        source_requirement_id=value.get("source_requirement_id", value.get("source_requirement", "")),
        source_domain=value.get("source_domain", ""),
        target_domain=value.get("target_domain", ""),
        source_owner=value.get("source_owner", ""),
        target_owner=value.get("target_owner", ""),
    )


def _attempt_quality(attempt: CourseAttempt) -> tuple[Any, ...]:
    status_rank = {PASS: 4, "IN_PROGRESS": 3, WAIVER: 2, UNKNOWN: 1, FAIL: 0}.get(attempt.status, 0)
    return (
        status_rank,
        attempt.available_credits,
        attempt.earned_credits,
        attempt.credits,
        attempt.academic_term,
        attempt.course_id,
        attempt.course_name,
        attempt.attempt_id,
    )


def _normalize_attempts(
    values: Any,
    *,
    with_issues: bool = False,
) -> tuple[CourseAttempt, ...] | tuple[tuple[CourseAttempt, ...], tuple[str, ...]]:
    if values is None or isinstance(values, (str, bytes)):
        values = (values,) if values is not None else ()
    elif isinstance(values, Mapping):
        values = (values,)
    attempts = [item for value in values or () if (item := _as_attempt(value)) is not None]
    grouped: dict[str, list[CourseAttempt]] = {}
    for item in attempts:
        grouped.setdefault(item.attempt_id, []).append(item)
    issues: list[str] = []
    selected: list[CourseAttempt] = []
    for attempt_id, items in grouped.items():
        fingerprints = {_input_fingerprint(item) for item in items}
        if len(fingerprints) > 1:
            # Retain a deterministic representative for inspection, but do
            # not silently choose between conflicting records.
            issues.append(f"DUPLICATE_ATTEMPT_ID:{attempt_id}")
        selected.append(max(items, key=_attempt_quality))
    normalized = tuple(sorted(selected, key=lambda item: (item.attempt_id, item.course_id, item.academic_term)))
    return (normalized, tuple(sorted(set(issues)))) if with_issues else normalized


def _input_values(values: Any) -> tuple[Any, ...]:
    if values is None or isinstance(values, (str, bytes)):
        return (values,) if values is not None else ()
    if isinstance(values, Mapping):
        return (values,)
    return tuple(values or ()) if isinstance(values, Sequence | set) else (values,)


def _canonical_input(value: Any) -> Any:
    """Canonicalize all fields for duplicate/conflict detection in memory."""

    if isinstance(value, Mapping):
        return {str(key): _canonical_input(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_input(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonical_input(item) for item in value), key=repr)
    if isinstance(value, (bytes, bytearray)):
        # Keep only a digest, never the raw bytes, even in diagnostics.
        return {"bytes_sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {item.name: _canonical_input(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _input_fingerprint(value: Any) -> str:
    return _stable_digest(_canonical_input(value))


def _normalize_requirements(
    values: Any,
    *,
    with_issues: bool = False,
) -> tuple[RequirementSpec, ...] | tuple[tuple[RequirementSpec, ...], tuple[str, ...]]:
    if values is None or isinstance(values, (str, bytes)):
        values = (values,) if values is not None else ()
    elif isinstance(values, Mapping):
        values = (values,)
    entries: dict[str, list[tuple[RequirementSpec, str]]] = {}
    issues: list[str] = []
    for value in values or ():
        item = _as_requirement(value)
        if item is not None and item.requirement_id:
            entries.setdefault(item.requirement_id, []).append((item, _input_fingerprint(value)))
            if not item.credits_required_valid:
                issues.append(f"INVALID_REQUIREMENT_CREDITS:{item.requirement_id}")
    unique: dict[str, RequirementSpec] = {}
    for requirement_id, candidates in entries.items():
        fingerprints = {fingerprint for _item, fingerprint in candidates}
        if len(candidates) > 1 and len(fingerprints) > 1:
            issues.append(f"DUPLICATE_REQUIREMENT_ID:{requirement_id}")
        # Pick a stable representative even for conflicting input order.
        unique[requirement_id] = min(candidates, key=lambda pair: pair[1])[0]
    normalized = tuple(sorted(unique.values(), key=lambda item: item.requirement_id))
    return (normalized, tuple(sorted(set(issues)))) if with_issues else normalized


def _route_owner(requirement: RequirementSpec) -> str:
    """Return the explicit owner used to constrain overflow routes."""

    return _text(requirement.owner or requirement.role or requirement.curriculum_role).upper().replace("-", "_").replace(" ", "_")


def canonicalize_overflow_routes(
    requirements: Sequence[RequirementSpec],
) -> tuple[tuple[RequirementSpec, ...], tuple[str, ...]]:
    """Resolve same-owner overflow suffixes and report malformed routes.

    Registry rows may use either the full requirement ID or a suffix such as
    ``elective``.  A suffix is accepted only when it resolves to one target;
    missing, cross-owner, self-referential, and cyclic routes remain explicit
    rule errors.  The returned specs contain canonical full IDs so every
    consumer displays the same route candidate.
    """

    normalized = tuple(requirements)
    by_id = {item.requirement_id: item for item in normalized}
    resolved: dict[str, list[str]] = {}
    issues: list[str] = []
    for requirement in normalized:
        routes: list[str] = []
        for raw_route in requirement.overflow_routes:
            route = _text(raw_route)
            if not route:
                continue
            target = by_id.get(route)
            if target is None:
                suffix_matches = [
                    candidate
                    for candidate in normalized
                    if candidate.requirement_id.endswith(f":{route}")
                ]
                if len(suffix_matches) == 1:
                    target = suffix_matches[0]
                elif len(suffix_matches) > 1:
                    issues.append(f"OVERFLOW_ROUTE_AMBIGUOUS:{requirement.requirement_id}:{route}")
                else:
                    issues.append(f"OVERFLOW_ROUTE_MISSING:{requirement.requirement_id}:{route}")
            if target is None:
                continue
            if target.requirement_id == requirement.requirement_id:
                issues.append(f"OVERFLOW_ROUTE_SELF:{requirement.requirement_id}")
                continue
            source_owner = _route_owner(requirement)
            target_owner = _route_owner(target)
            if source_owner and target_owner and source_owner != target_owner:
                issues.append(f"OVERFLOW_ROUTE_CROSS_OWNER:{requirement.requirement_id}:{target.requirement_id}")
                continue
            routes.append(target.requirement_id)
        resolved[requirement.requirement_id] = list(dict.fromkeys(routes))

    graph = {key: tuple(value) for key, value in resolved.items()}
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_nodes: set[str] = set()

    def visit(node: str, stack: tuple[str, ...] = ()) -> None:
        if node in visiting:
            cycle_nodes.update(stack[stack.index(node) :] if node in stack else (node,))
            return
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, ()):
            visit(child, (*stack, node))
        visiting.discard(node)
        visited.add(node)

    for node in graph:
        visit(node)
    issues.extend(f"OVERFLOW_ROUTE_CYCLE:{node}" for node in sorted(cycle_nodes))
    issue_set = tuple(sorted(set(issues)))
    result = tuple(
        replace(requirement, overflow_routes=tuple(resolved.get(requirement.requirement_id, ())))
        for requirement in normalized
    )
    return result, issue_set


def _normalize_bindings(
    values: Any,
    *,
    with_issues: bool = False,
) -> tuple[EquivalencyBinding, ...] | tuple[tuple[EquivalencyBinding, ...], tuple[str, ...]]:
    if values is None or isinstance(values, (str, bytes)):
        values = (values,) if values is not None else ()
    elif isinstance(values, Mapping):
        values = (values,)
    entries: dict[str, list[tuple[EquivalencyBinding, str]]] = {}
    for value in values or ():
        item = _as_binding(value)
        if item is not None and item.binding_id:
            entries.setdefault(item.binding_id, []).append((item, _input_fingerprint(value)))
    issues: list[str] = []
    unique: dict[str, EquivalencyBinding] = {}
    for binding_id, candidates in entries.items():
        fingerprints = {fingerprint for _item, fingerprint in candidates}
        if len(candidates) > 1 and len(fingerprints) > 1:
            issues.append(f"DUPLICATE_BINDING_ID:{binding_id}")
        unique[binding_id] = min(candidates, key=lambda pair: pair[1])[0]
    normalized = tuple(sorted(unique.values(), key=lambda item: item.binding_id))
    return (normalized, tuple(sorted(set(issues)))) if with_issues else normalized


def _as_waiver_decision(value: Any) -> WaiverDecision | None:
    if isinstance(value, WaiverDecision):
        return value
    if not isinstance(value, Mapping):
        return None
    return WaiverDecision(
        decision_id=value.get("decision_id", value.get("id", value.get("evidence_id", ""))),
        target_requirement_id=value.get("target_requirement_id", value.get("requirement_id", value.get("target_id", ""))),
        evidence_state=value.get("evidence_state", value.get("status", UNKNOWN)),
        authority=value.get("authority", ""),
        evidence_reference=value.get("evidence_reference", value.get("source_reference", value.get("evidence_id", ""))),
        decision=value.get("decision", "PENDING"),
        subject_ref=value.get("subject_ref", value.get("subject_id", "")),
        curriculum_version=value.get("curriculum_version", value.get("version", "")),
        requirement_version=value.get("requirement_version", value.get("required_version", "")),
        program_slug=value.get("program_slug", value.get("program", "")),
        track_slug=value.get("track_slug", value.get("track", "")),
    )


def _normalize_waiver_decisions(
    values: Any,
    *,
    with_issues: bool = False,
) -> tuple[WaiverDecision, ...] | tuple[tuple[WaiverDecision, ...], tuple[str, ...]]:
    entries: dict[str, list[tuple[WaiverDecision, str]]] = {}
    for value in _input_values(values):
        item = _as_waiver_decision(value)
        if item is not None and item.decision_id:
            entries.setdefault(item.decision_id, []).append((item, _input_fingerprint(value)))
    unique: dict[str, WaiverDecision] = {}
    issues: list[str] = []
    for decision_id, candidates in entries.items():
        fingerprints = {fingerprint for _item, fingerprint in candidates}
        if len(fingerprints) > 1:
            issues.append(f"DUPLICATE_WAIVER_DECISION_ID:{decision_id}")
        unique[decision_id] = min(candidates, key=lambda pair: pair[1])[0]
    normalized = tuple(sorted(unique.values(), key=lambda item: item.decision_id))
    return (normalized, tuple(sorted(set(issues)))) if with_issues else normalized


def _is_subset_gate_requirement(requirement: RequirementSpec | None) -> bool:
    if requirement is None:
        return False
    kind = _text(requirement.kind).upper().replace("-", "_").replace(" ", "_")
    return kind in {"CREDIT_SUBSET_GATE", "CREDIT_SUBSET_REQUIREMENT"}


def _validate_waiver_decisions(
    decisions: tuple[WaiverDecision, ...],
    requirements: tuple[RequirementSpec, ...],
) -> tuple[set[str], tuple[WaiverAssessment, ...], set[str], list[str]]:
    """Return approved waiver targets plus deterministic audit assessments."""

    requirements_by_id = {item.requirement_id: item for item in requirements}
    approved_targets: set[str] = set()
    unknown_targets: set[str] = set()
    assessments: list[WaiverAssessment] = []
    warnings: list[str] = []
    target_decisions: dict[str, list[WaiverDecision]] = {}
    for decision in decisions:
        target_decisions.setdefault(decision.target_requirement_id, []).append(decision)
    for decision in decisions:
        requirement = requirements_by_id.get(decision.target_requirement_id)
        valid = bool(requirement and requirement.waiver)
        reason = ""
        if requirement is None:
            reason = "waiver target requirement is missing"
        elif not requirement.waiver:
            reason = "target requirement has no waiver path"
        elif decision.evidence_state != VERIFIED or decision.decision != "APPROVED":
            valid = False
            reason = "waiver evidence is not VERIFIED/APPROVED"
        elif not decision.authority or not decision.evidence_reference:
            valid = False
            reason = "waiver authority or evidence reference is missing"
        elif len(target_decisions.get(decision.target_requirement_id, ())) > 1:
            valid = False
            reason = "multiple waiver decisions target the same requirement"
        elif _is_subset_gate_requirement(requirement):
            actual_version = decision.requirement_version or decision.curriculum_version
            expected_version = requirement.curriculum_version
            if not decision.subject_ref:
                valid = False
                reason = "subset-gate waiver subject binding is missing"
            elif expected_version and actual_version != expected_version:
                valid = False
                reason = "subset-gate waiver curriculum version does not match"
            elif not actual_version:
                valid = False
                reason = "subset-gate waiver curriculum version is missing"
            elif requirement.program_slug and decision.program_slug and decision.program_slug != requirement.program_slug:
                valid = False
                reason = "subset-gate waiver program does not match"
            elif requirement.track_slug and decision.track_slug and decision.track_slug != requirement.track_slug:
                valid = False
                reason = "subset-gate waiver track does not match"
        if valid:
            approved_targets.add(decision.target_requirement_id)
            assessments.append(WaiverAssessment(decision.decision_id, decision.target_requirement_id, PASS, "verified approved waiver decision"))
        else:
            if decision.target_requirement_id:
                unknown_targets.add(decision.target_requirement_id)
            assessments.append(WaiverAssessment(decision.decision_id, decision.target_requirement_id, UNKNOWN, reason or "waiver decision is unresolved"))
            warnings.append(f"UNVERIFIED_WAIVER:{decision.decision_id}")
    for requirement in requirements:
        if requirement.waiver and requirement.requirement_id not in approved_targets:
            unknown_targets.add(requirement.requirement_id)
    return approved_targets, tuple(assessments), unknown_targets, warnings


def _course_kind_allowed(attempt: CourseAttempt, requirement: RequirementSpec) -> bool | None:
    allowed = requirement.allowed_course_kinds
    if requirement.course_kind == LAB or LAB in allowed:
        return attempt.course_kind == LAB
    if allowed and attempt.course_kind not in allowed:
        return False
    if attempt.course_kind not in {LECTURE, COMBINED, LAB}:
        return None
    return True


def _pool_membership_record(attempt: CourseAttempt, membership_id: str) -> tuple[str, str, str] | None:
    """Return ``(state, source_reference, kind)`` for one pool membership."""

    wanted = _text(membership_id)
    if not wanted:
        return None
    for pool_id, state, source_reference, kind in attempt.pool_membership_evidence:
        if pool_id == wanted:
            return state, source_reference, kind
    if wanted in attempt.pool_memberships:
        return attempt.pool_evidence_state, "", "legacy"
    return None


def _is_trusted_negative_membership(record: tuple[str, str, str] | None) -> bool:
    """Return whether a membership record is an auditable negative assertion.

    A missing target row, or a legacy/global pool marker, cannot establish
    that a course is outside a capped pool.  Only the explicit negative
    record kinds emitted by a server-owned catalog/registry adapter may do so.
    """

    if record is None:
        return False
    state, source_reference, kind = record
    return (
        state == NOT_MEMBER
        and bool(_text(source_reference))
        and _norm_membership_kind(kind) in _TRUSTED_NEGATIVE_MEMBERSHIP_KINDS
    )


def _is_trusted_verified_membership(record: tuple[str, str, str] | None) -> bool:
    """Return whether a positive membership has auditable source evidence."""

    if record is None:
        return False
    state, source_reference, kind = record
    return (
        state == VERIFIED
        and bool(_text(source_reference))
        and _norm_membership_kind(kind) != "legacy"
    )


def _membership_record_is_uncertain(record: tuple[str, str, str] | None) -> bool:
    """Return whether an existing membership record remains unresolved."""

    if record is None:
        return False
    state = record[0]
    return state not in {VERIFIED, CONFLICTED, NOT_MEMBER} or (
        state == NOT_MEMBER and not _is_trusted_negative_membership(record)
    )


def _direct_match(attempt: CourseAttempt, requirement: RequirementSpec) -> bool | None:
    pool_records = tuple(
        record
        for pool_id in requirement.eligible_pool_ids
        if (record := _pool_membership_record(attempt, pool_id)) is not None
    )
    # Public offering evidence can prove an aggregate pool independently of
    # named handbook identity.  It never promotes a named-course match.
    public_pool_quota = _text(requirement.kind).upper() in {"AGGREGATE", "COURSE_POOL", "QUOTA"}
    authoritative_pool = any(
        state == VERIFIED and bool(_text(source)) and (
            record_kind == "policy" or (record_kind == "public_catalog" and public_pool_quota)
        )
        for state, source, record_kind in pool_records
    )
    pool_match = any(
        state == VERIFIED and attempt.identity_status == VERIFIED
        for state, _source, _kind in pool_records
    ) or authoritative_pool
    kind = _course_kind_allowed(attempt, requirement)
    # Official open/category policies are scoped predicates, not named
    # lecture/lab requirements.  They may accept a confirmed transcript row
    # whose component is absent (as in the official AG102 export), provided
    # the policy membership itself is VERIFIED.  Named routes retain the
    # strict component identity check.
    if kind is False:
        return False
    pool_unknown = any(_membership_record_is_uncertain(record) for record in pool_records)
    formal_exact = attempt.course_id in requirement.eligible_course_ids or pool_match or requirement.accept_any
    # Keep same-name rows visible as planning candidates, but never let title
    # equality establish a formal identity or consume graduation credit.
    if not formal_exact:
        if pool_unknown:
            return None
        if attempt.course_name in requirement.eligible_course_names:
            if kind is None and not authoritative_pool:
                return None
            # Exact title equality is usable only after the service/adapter
            # has already established a formal, uniquely matched identity.
            return True if attempt.identity_status == VERIFIED and attempt.pool_evidence_state == VERIFIED else None
        return False
    if kind is None and not authoritative_pool:
        return None
    if pool_match:
        return True
    if attempt.identity_status != VERIFIED:
        return None
    return True


def _binding_is_verified(binding: EquivalencyBinding, attempt: CourseAttempt, requirement: RequirementSpec) -> tuple[bool, str]:
    if not binding.binding_id or not binding.source_attempt_id or not binding.target_requirement_id:
        return False, "binding identity is incomplete"
    if binding.evidence_state != VERIFIED or binding.decision != "APPROVED":
        return False, "equivalency evidence is not VERIFIED/APPROVED"
    if not binding.authority or not binding.evidence_reference:
        return False, "equivalency authority or evidence reference is missing"
    if binding.source_attempt_id != attempt.attempt_id or binding.target_requirement_id != requirement.requirement_id:
        return False, "source attempt or target requirement does not match"
    if binding.source_course_id and binding.source_course_id != attempt.course_id:
        return False, "source course identity does not match"
    if binding.target_course_id and requirement.eligible_course_ids and binding.target_course_id not in requirement.eligible_course_ids:
        return False, "target course identity does not match"
    if binding.source_course_kind and binding.source_course_kind != attempt.course_kind:
        return False, "source course component kind does not match"
    kind = _course_kind_allowed(attempt, requirement)
    if kind is not True:
        return False, "lecture/combined attempt cannot satisfy LAB requirement"
    if binding.approved_credits <= _ZERO:
        return False, "approved equivalency credits are zero"
    return True, ""


def _norm_owner(value: Any) -> str:
    text = _text(value).upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "PRIMARY": "PRIMARY",
        "HOME": "PRIMARY",
        "MAJOR": "PRIMARY",
        "SOURCE": "PRIMARY",
        "主修": "PRIMARY",
        "TARGET": "TARGET",
        "SECONDARY": "TARGET",
        "DOUBLE_MAJOR": "TARGET",
        "DOUBLEMAJOR": "TARGET",
        "雙主修": "TARGET",
    }
    return aliases.get(text, text)


def _shared_binding_scope(
    binding: EquivalencyBinding,
    attempt: CourseAttempt | None,
    source_requirement: RequirementSpec | None,
    target_requirement: RequirementSpec | None,
    exclusive_by_attempt_requirement: Mapping[tuple[str, str], Decimal],
) -> tuple[bool, str]:
    """Validate source ownership before a shared shadow is projected."""

    if attempt is None or source_requirement is None or target_requirement is None:
        return False, "shared source/target requirement is missing"
    if not binding.source_requirement_id:
        return False, "shared binding source requirement scope is missing"
    if source_requirement.requirement_id == target_requirement.requirement_id:
        return False, "shared binding cannot target its source requirement"
    if not binding.direction:
        return False, "shared binding direction is missing"
    source_owner = _norm_owner(source_requirement.owner or source_requirement.role or source_requirement.curriculum_role)
    target_owner = _norm_owner(target_requirement.owner or target_requirement.role or target_requirement.curriculum_role)
    expected = (
        ("PRIMARY", "TARGET")
        if binding.direction == PRIMARY_TO_TARGET
        else ("TARGET", "PRIMARY")
        if binding.direction == TARGET_TO_PRIMARY
        else None
    )
    if expected is None:
        return False, "shared binding direction is unsupported"
    if (source_owner, target_owner) != expected:
        return False, "shared binding ownership does not match direction"
    if not binding.source_owner or not binding.target_owner or not binding.source_domain or not binding.target_domain:
        return False, "shared binding owner/domain scope is incomplete"
    if _norm_owner(binding.source_owner) != source_owner:
        return False, "shared binding source owner does not match requirement"
    if _norm_owner(binding.target_owner) != target_owner:
        return False, "shared binding target owner does not match requirement"
    source_domain = _text(source_requirement.domain or source_requirement.requirement_domain)
    target_domain = _text(target_requirement.domain or target_requirement.requirement_domain)
    if not source_domain or not target_domain:
        return False, "shared requirement ownership/domain metadata is incomplete"
    if binding.source_domain != source_domain:
        return False, "shared binding source domain does not match requirement"
    if binding.target_domain != target_domain:
        return False, "shared binding target domain does not match requirement"
    if exclusive_by_attempt_requirement.get((attempt.attempt_id, source_requirement.requirement_id), _ZERO) <= _ZERO:
        return False, "shared source requirement has no positive exclusive allocation"
    return True, ""


def _repeat_filter(attempts: tuple[CourseAttempt, ...], requirements: tuple[RequirementSpec, ...]) -> tuple[CourseAttempt, ...]:
    """Return deduplicated attempts without choosing a repeat winner early.

    Repeat policy is a property of the *route* an attempt takes, not merely a
    property of the input requirement set.  Filtering a whole repeat group
    up-front loses legal repeatable-only allocations and, conversely, keeping
    every attempt without a route guard lets one group straddle fixed and
    repeatable requirements.  The search therefore keeps all distinct
    attempts and enforces the group mode while visiting each route; the final
    result projects only the legally active attempts.

    ``_normalize_attempts`` has already collapsed duplicate parser rows, so
    this function is intentionally a stable no-op kept as a named boundary
    for callers/tests that relied on the old helper.
    """

    del requirements
    return tuple(sorted(attempts, key=lambda item: (item.attempt_id, item.course_id, item.academic_term)))


def _maximum_subset_capacity(
    attempt: CourseAttempt,
    requirement: RequirementSpec,
    requirements_by_id: Mapping[str, RequirementSpec],
    amounts: Mapping[str, Decimal],
    *,
    selected_allocations: Sequence[AttemptAllocation] = (),
    attempts_by_id: Mapping[str, CourseAttempt] | None = None,
) -> Decimal | None:
    """Return a safe per-option cap imposed by a verified subset maximum.

    Maximum subset rules are evaluated over the selected exclusive ledger, so
    the allocator must expose a partial option when a course would otherwise
    overshoot a cap.  Only a verified target membership is capped here.  An
    unknown membership remains observable as UNKNOWN in the subset evaluator;
    treating it as internal at search time would silently turn evidence
    uncertainty into a pass.
    """

    del amounts  # The cap is based on selected membership evidence, not totals.
    capacities: list[Decimal] = []
    selected = tuple(selected_allocations or ())
    attempt_lookup = attempts_by_id or {}

    def values(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            return ()
        return tuple(dict.fromkeys(_text(item) for item in value if _text(item)))

    for owner in requirements_by_id.values():
        for constraint in owner.subset_constraints:
            membership_id = _text(constraint.get("membership_id") or constraint.get("pool_id"))
            if not membership_id:
                continue
            amount_semantics = _text(constraint.get("amount_semantics")).upper().replace("-", "_").replace(" ", "_")
            maximum_value = next(
                (
                    constraint.get(key)
                    for key in ("maximum_credits", "max_credits", "maximum", "credit_cap")
                    if constraint.get(key) not in (None, "")
                ),
                None,
            )
            if amount_semantics == "MAXIMUM" and maximum_value in (None, ""):
                maximum_value = next(
                    (
                        constraint.get(key)
                        for key in ("minimum_credits", "required_credits", "minimum")
                        if constraint.get(key) not in (None, "")
                    ),
                    None,
                )
            if maximum_value in (None, "") or not _is_valid_nonnegative_decimal(maximum_value):
                continue
            observed_ids = values(
                constraint.get("observed_requirement_ids", constraint.get("observed_requirements", ()))
            ) or (owner.requirement_id,)
            if requirement.requirement_id not in observed_ids:
                continue
            record = _pool_membership_record(attempt, membership_id)
            if record is None or record[0] != VERIFIED:
                continue
            excluded_ids = values(
                constraint.get(
                    "excluded_membership_ids",
                    constraint.get("excluded_membership_id", constraint.get("excluded_membership", ())),
                )
            )
            if any(
                (exemption := _pool_membership_record(attempt, excluded_id)) is not None
                and exemption[0] == VERIFIED
                for excluded_id in excluded_ids
            ):
                continue
            # ``amounts`` is a requirement ledger and cannot distinguish an
            # external course from an internal one.  Count only already
            # selected EXCLUSIVE portions whose source carries VERIFIED
            # membership in this constraint; each observed requirement is
            # therefore measured once, with exemptions removed per attempt.
            observed_amount = _ZERO
            for allocation in selected:
                source_attempt = attempt_lookup.get(allocation.attempt_id)
                if source_attempt is None:
                    continue
                source_record = _pool_membership_record(source_attempt, membership_id)
                if source_record is None or source_record[0] != VERIFIED:
                    continue
                if any(
                    (exemption := _pool_membership_record(source_attempt, excluded_id)) is not None
                    and exemption[0] == VERIFIED
                    for excluded_id in excluded_ids
                ):
                    continue
                observed_amount += sum(
                    (
                        portion.credits
                        for portion in allocation.portions
                        if portion.allocation_kind == EXCLUSIVE
                        and portion.credits > _ZERO
                        and portion.requirement_id in observed_ids
                    ),
                    _ZERO,
                )
            capacities.append(max(_ZERO, _nonnegative_decimal(maximum_value) - observed_amount))
    return min(capacities) if capacities else None


def _option_portions(
    attempt: CourseAttempt,
    requirement: RequirementSpec,
    requirements_by_id: Mapping[str, RequirementSpec],
    amounts: Mapping[str, Decimal],
    binding: EquivalencyBinding | None = None,
    *,
    respect_subset_maximum: bool = True,
    selected_allocations: Sequence[AttemptAllocation] = (),
    attempts_by_id: Mapping[str, CourseAttempt] | None = None,
) -> tuple[tuple[CreditPortion, ...], Decimal] | None:
    available = attempt.available_credits
    if attempt.status == WAIVER:
        return (CreditPortion(attempt.attempt_id, requirement.requirement_id, _ZERO, WAIVER),), _ZERO
    if available <= _ZERO:
        return None
    current = amounts.get(requirement.requirement_id, _ZERO)
    capacity = max(_ZERO, requirement.max_credits - current)
    if respect_subset_maximum:
        subset_capacity = _maximum_subset_capacity(
            attempt,
            requirement,
            requirements_by_id,
            amounts,
            selected_allocations=selected_allocations,
            attempts_by_id=attempts_by_id,
        )
        if subset_capacity is not None:
            capacity = min(capacity, subset_capacity)
    if capacity <= _ZERO:
        return None
    binding_cap = binding.approved_credits if binding is not None else available
    if binding is not None and binding_cap <= _ZERO:
        return None
    amount = min(available, capacity, binding_cap)
    portions = [
        CreditPortion(
            attempt.attempt_id,
            requirement.requirement_id,
            amount,
            EXCLUSIVE,
            binding_id=binding.binding_id if binding is not None else "",
        )
    ]
    residual = available - amount
    if residual > _ZERO:
        for route_id in requirement.overflow_routes:
            route = requirements_by_id.get(route_id)
            if route is None:
                continue
            if _course_kind_allowed(attempt, route) is not True:
                continue
            route_amounts = _apply_portions(amounts, portions)
            route_capacity = max(
                _ZERO,
                route.max_credits - route_amounts.get(route.requirement_id, _ZERO),
            )
            if respect_subset_maximum:
                pending = AttemptAllocation(
                    attempt.attempt_id,
                    attempt.available_credits,
                    tuple(portions),
                    _ZERO,
                )
                route_subset_capacity = _maximum_subset_capacity(
                    attempt,
                    route,
                    requirements_by_id,
                    route_amounts,
                    selected_allocations=(*selected_allocations, pending),
                    attempts_by_id=attempts_by_id,
                )
                if route_subset_capacity is not None:
                    route_capacity = min(route_capacity, route_subset_capacity)
            if route_capacity <= _ZERO:
                continue
            route_amount = min(residual, route_capacity)
            if route_amount > _ZERO:
                portions.append(CreditPortion(attempt.attempt_id, route.requirement_id, route_amount, EXCLUSIVE))
                residual -= route_amount
            if residual <= _ZERO:
                break
    return tuple(portions), residual


def _apply_portions(amounts: Mapping[str, Decimal], portions: Sequence[CreditPortion]) -> dict[str, Decimal]:
    updated = dict(amounts)
    for portion in portions:
        if portion.allocation_kind != EXCLUSIVE:
            continue
        updated[portion.requirement_id] = updated.get(portion.requirement_id, _ZERO) + portion.credits
    return updated


def _signature(choices: Sequence[AttemptAllocation]) -> tuple[Any, ...]:
    return tuple(
        (
            choice.attempt_id,
            tuple(
                (
                    portion.requirement_id,
                    str(portion.credits),
                    portion.allocation_kind,
                    portion.direction,
                )
                for portion in choice.portions
            ),
            str(choice.unallocated_credits),
        )
        for choice in choices
    )


def _positive_portions(
    portions: Sequence[CreditPortion],
) -> tuple[CreditPortion, ...]:
    return tuple(item for item in portions if item.allocation_kind == EXCLUSIVE and item.credits > _ZERO)


def _repeat_mode_for_portions(
    portions: Sequence[CreditPortion],
    requirements_by_id: Mapping[str, RequirementSpec],
) -> str | None:
    """Return the repeat mode represented by a positive route.

    An option can contain an explicit overflow route.  If any part of that
    route lands in a fixed requirement, the whole option is fixed for repeat
    accounting; this prevents a single attempt from straddling fixed and
    repeatable requirements through an overflow edge.
    """

    positive = _positive_portions(portions)
    if not positive:
        return None
    modes = {
        requirement.repeat_policy
        for item in positive
        if (requirement := requirements_by_id.get(item.requirement_id)) is not None
    }
    if BEST_ATTEMPT_ONLY in modes and REPEATABLE in modes:
        # One retake route may not straddle fixed and repeatable policy
        # classes through an overflow edge.
        return "MIXED"
    if BEST_ATTEMPT_ONLY in modes:
        return BEST_ATTEMPT_ONLY
    return REPEATABLE


def _repeat_option_allowed(
    attempt: CourseAttempt,
    portions: Sequence[CreditPortion],
    requirements_by_id: Mapping[str, RequirementSpec],
    repeat_states: Mapping[str, tuple[str, str | None]],
    *,
    repeat_selection_winners: Mapping[str, str] | None = None,
    repeat_selection_unknown_groups: set[str] | None = None,
) -> bool:
    group_id = attempt.repeat_group_id
    mode = _repeat_mode_for_portions(portions, requirements_by_id)
    if mode == "MIXED":
        return False
    if not group_id or mode is None:
        return True
    repeat_selection_winners = repeat_selection_winners or {}
    repeat_selection_unknown_groups = repeat_selection_unknown_groups or set()
    if mode == BEST_ATTEMPT_ONLY:
        if group_id in repeat_selection_unknown_groups:
            return False
        selected_winner = repeat_selection_winners.get(group_id)
        if selected_winner is not None and selected_winner != attempt.attempt_id:
            return False
    previous = repeat_states.get(group_id)
    if previous is None:
        return True
    previous_mode, previous_attempt_id = previous
    if mode == REPEATABLE:
        # Once a group has contributed to a fixed requirement, another
        # attempt in that group cannot be counted by a repeatable bucket.
        return previous_mode == REPEATABLE
    # BEST_ATTEMPT_ONLY allows one and only one source attempt for the group.
    return previous_mode == BEST_ATTEMPT_ONLY and previous_attempt_id == attempt.attempt_id


def _repeat_evidence_unknown(
    attempts: tuple[CourseAttempt, ...],
    choices: Sequence[AttemptAllocation],
    requirements_by_id: Mapping[str, RequirementSpec],
) -> set[str]:
    """Return fixed-repeat requirements whose selected winner lacks evidence.

    Numeric credits alone are not enough to choose a legally effective repeat
    winner.  The production adapter records the posted grade evidence state;
    a missing/unverified state therefore makes every affected requirement
    unresolved without inventing a grade conversion.
    """

    attempts_by_id = {item.attempt_id: item for item in attempts}
    unknown: set[str] = set()
    for choice in choices:
        attempt = attempts_by_id.get(choice.attempt_id)
        if attempt is None or not attempt.repeat_group_id:
            continue
        if _repeat_mode_for_portions(choice.portions, requirements_by_id) != BEST_ATTEMPT_ONLY:
            continue
        if attempt.grade_evidence_state == VERIFIED or (
            attempt.effective_attempt and attempt.repeat_selection_evidence_state == VERIFIED
        ):
            continue
        unknown.update(
            portion.requirement_id
            for portion in _positive_portions(choice.portions)
        )
    return unknown


def _repeat_selection_state(
    attempts: tuple[CourseAttempt, ...],
    requirements: tuple[RequirementSpec, ...],
) -> tuple[dict[str, str], set[str]]:
    """Resolve explicit fixed-repeat winners without ordering heuristics.

    Returns ``(winner_by_group, unresolved_groups)``.  A group with multiple
    positive attempts is unresolved unless exactly one attempt is explicitly
    marked effective with VERIFIED selection evidence.  Repeatable-only
    requirements do not need a winner and are intentionally ignored here.
    """

    if not any(item.repeat_policy == BEST_ATTEMPT_ONLY for item in requirements):
        return {}, set()
    grouped: dict[str, list[CourseAttempt]] = {}
    for item in attempts:
        if item.repeat_group_id and item.available_credits > _ZERO:
            grouped.setdefault(item.repeat_group_id, []).append(item)
    winners: dict[str, str] = {}
    unresolved: set[str] = set()
    for group_id, items in grouped.items():
        if len(items) <= 1:
            continue
        explicit = [
            item
            for item in items
            if item.effective_attempt and item.repeat_selection_evidence_state == VERIFIED
        ]
        if len(explicit) == 1:
            winners[group_id] = explicit[0].attempt_id
        else:
            unresolved.add(group_id)
    return winners, unresolved


def _active_attempt_ids(
    attempts: tuple[CourseAttempt, ...],
    choices: Sequence[AttemptAllocation],
    requirements_by_id: Mapping[str, RequirementSpec],
    *,
    repeat_selection_winners: Mapping[str, str] | None = None,
    repeat_selection_unknown_groups: set[str] | None = None,
) -> set[str]:
    """Determine which attempts contribute legally earned source credit.

    A fixed repeat group contributes the one attempt that was actually routed
    to a fixed requirement.  A repeatable-only group may contribute every
    attempt.  A group with no selected route falls back to its deterministic
    best attempt, matching the usual best-attempt transcript rule.
    """

    attempts_by_group: dict[str, list[CourseAttempt]] = {}
    active: set[str] = set()
    for attempt in attempts:
        if attempt.repeat_group_id:
            attempts_by_group.setdefault(attempt.repeat_group_id, []).append(attempt)
        else:
            active.add(attempt.attempt_id)

    selected_by_group: dict[str, list[tuple[str, str]]] = {}
    for choice in choices:
        attempt = next((item for item in attempts if item.attempt_id == choice.attempt_id), None)
        if attempt is None or not attempt.repeat_group_id:
            continue
        mode = _repeat_mode_for_portions(choice.portions, requirements_by_id)
        if mode is not None:
            selected_by_group.setdefault(attempt.repeat_group_id, []).append((mode, attempt.attempt_id))

    repeat_selection_winners = repeat_selection_winners or {}
    repeat_selection_unknown_groups = repeat_selection_unknown_groups or set()
    for group_id, grouped_attempts in attempts_by_group.items():
        selected = selected_by_group.get(group_id, [])
        if any(mode == BEST_ATTEMPT_ONLY for mode, _attempt_id in selected):
            # The DFS guard ensures this set contains at most one ID.  Keep a
            # deterministic fallback in case a caller constructs a result by
            # hand or a future route violates that invariant.
            fixed_ids = sorted({attempt_id for mode, attempt_id in selected if mode == BEST_ATTEMPT_ONLY})
            active.add(fixed_ids[0] if fixed_ids else max(grouped_attempts, key=_attempt_quality).attempt_id)
        elif any(mode == REPEATABLE for mode, _attempt_id in selected):
            active.update(item.attempt_id for item in grouped_attempts)
        elif group_id in repeat_selection_unknown_groups:
            # Keep every released attempt in the conservation ledger while
            # the fixed-repeat route itself remains unresolved.  No winner is
            # silently selected merely to make the totals look consistent.
            active.update(item.attempt_id for item in grouped_attempts)
        elif group_id in repeat_selection_winners:
            active.add(repeat_selection_winners[group_id])
        else:
            active.add(max(grouped_attempts, key=_attempt_quality).attempt_id)
    return active


def _legalize_choices(
    attempts: tuple[CourseAttempt, ...],
    choices: Sequence[AttemptAllocation],
    requirements_by_id: Mapping[str, RequirementSpec],
    *,
    repeat_selection_winners: Mapping[str, str] | None = None,
    repeat_selection_unknown_groups: set[str] | None = None,
) -> tuple[AttemptAllocation, ...]:
    active_ids = _active_attempt_ids(
        attempts,
        choices,
        requirements_by_id,
        repeat_selection_winners=repeat_selection_winners,
        repeat_selection_unknown_groups=repeat_selection_unknown_groups,
    )
    attempts_by_id = {item.attempt_id: item for item in attempts}
    legalized: list[AttemptAllocation] = []
    for choice in choices:
        if choice.attempt_id not in active_ids:
            continue
        source = attempts_by_id[choice.attempt_id].available_credits
        used = sum((item.credits for item in choice.portions if item.allocation_kind == EXCLUSIVE), _ZERO)
        legalized.append(AttemptAllocation(choice.attempt_id, source, choice.portions, max(_ZERO, source - used)))
    return tuple(legalized)


def _shared_projection(
    attempts: tuple[CourseAttempt, ...],
    requirements: tuple[RequirementSpec, ...],
    bindings: tuple[EquivalencyBinding, ...],
    exclusive_amounts: Mapping[str, Decimal],
    exclusive_allocations: Sequence[AttemptAllocation] = (),
) -> tuple[tuple[CreditPortion, ...], tuple[SharedLedger, ...], tuple[BindingAssessment, ...], set[str], list[str]]:
    attempts_by_id = {item.attempt_id: item for item in attempts}
    requirements_by_id = {item.requirement_id: item for item in requirements}
    exclusive_by_attempt: dict[str, Decimal] = {}
    exclusive_by_attempt_requirement: dict[tuple[str, str], Decimal] = {}
    for allocation in exclusive_allocations:
        amount = _ZERO
        for portion in allocation.portions:
            if portion.allocation_kind != EXCLUSIVE or portion.credits <= _ZERO:
                continue
            amount += portion.credits
            key = (allocation.attempt_id, portion.requirement_id)
            exclusive_by_attempt_requirement[key] = exclusive_by_attempt_requirement.get(key, _ZERO) + portion.credits
        if amount > _ZERO:
            exclusive_by_attempt[allocation.attempt_id] = exclusive_by_attempt.get(allocation.attempt_id, _ZERO) + amount
    shared_by_attempt: dict[str, Decimal] = {}
    ledgers: dict[str, Decimal] = {PRIMARY_TO_TARGET: _ZERO, TARGET_TO_PRIMARY: _ZERO}
    ledger_binding_ids: dict[str, list[str]] = {PRIMARY_TO_TARGET: [], TARGET_TO_PRIMARY: []}
    blocked: dict[str, Decimal] = {PRIMARY_TO_TARGET: _ZERO, TARGET_TO_PRIMARY: _ZERO}
    shadows: list[CreditPortion] = []
    assessments: list[BindingAssessment] = []
    unknown_requirements: set[str] = set()
    warnings: list[str] = []
    for binding in bindings:
        if not binding.shared:
            # Exclusive bindings were already validated while constructing the
            # ordinary candidate set; they do not consume a shared ledger.
            continue
        attempt = attempts_by_id.get(binding.source_attempt_id)
        requirement = requirements_by_id.get(binding.target_requirement_id)
        direction = binding.direction or (PRIMARY_TO_TARGET if binding.shared else "")
        valid = bool(attempt and requirement and direction in {PRIMARY_TO_TARGET, TARGET_TO_PRIMARY})
        reason = ""
        if not valid:
            reason = "binding source/target/direction is not resolvable"
        elif binding.evidence_state != VERIFIED or binding.decision != "APPROVED":
            valid = False
            reason = "equivalency evidence is not VERIFIED/APPROVED"
        elif not binding.authority or not binding.evidence_reference:
            valid = False
            reason = "equivalency authority or evidence reference is missing"
        elif binding.approved_credits <= _ZERO:
            valid = False
            reason = "approved equivalency credits are zero"
        elif attempt is None or requirement is None:
            valid = False
            reason = "binding source attempt or target requirement is missing"
        else:
            valid, reason = _binding_is_verified(binding, attempt, requirement)
        if not valid:
            assessments.append(BindingAssessment(binding.binding_id, UNKNOWN, reason, direction, binding.approved_credits))
            if requirement is not None:
                unknown_requirements.add(requirement.requirement_id)
            warnings.append(f"UNVERIFIED_EQUIVALENCY:{binding.binding_id}")
            continue
        source_requirement = requirements_by_id.get(binding.source_requirement_id)
        scoped, scope_reason = _shared_binding_scope(
            binding,
            attempt,
            source_requirement,
            requirement,
            exclusive_by_attempt_requirement,
        )
        if not scoped:
            assessments.append(BindingAssessment(binding.binding_id, UNKNOWN, scope_reason, direction, binding.approved_credits))
            if requirement is not None:
                unknown_requirements.add(requirement.requirement_id)
            warnings.append(f"SHARED_SCOPE_UNKNOWN:{binding.binding_id}")
            if "no positive exclusive allocation" in scope_reason:
                # Retain the specific source-cap diagnostic for callers that
                # relied on the earlier shared-source warning.
                warnings.append(f"SHARED_SOURCE_USE_UNKNOWN:{binding.binding_id}")
            continue
        remaining_ledger = max(_ZERO, SHARED_LIMIT - ledgers[direction])
        # Shared credit is a shadow of a real exclusive allocation.  It may
        # never be minted from an otherwise unallocated transcript attempt,
        # and multiple mappings must share the same source capacity.
        source_exclusive = exclusive_by_attempt.get(attempt.attempt_id, _ZERO)
        source_remaining = max(_ZERO, source_exclusive - shared_by_attempt.get(attempt.attempt_id, _ZERO))
        if source_remaining <= _ZERO:
            assessments.append(
                BindingAssessment(
                    binding.binding_id,
                    UNKNOWN,
                    "shared source attempt has no positive exclusive allocation",
                    direction,
                    binding.approved_credits,
                )
            )
            unknown_requirements.add(requirement.requirement_id)
            warnings.append(f"SHARED_SOURCE_USE_UNKNOWN:{binding.binding_id}")
            if source_exclusive > _ZERO:
                warnings.append(f"SHARED_SOURCE_CAP:{attempt.attempt_id}")
            continue
        requirement_capacity = max(_ZERO, requirement.max_credits - exclusive_amounts.get(requirement.requirement_id, _ZERO) - sum((item.credits for item in shadows if item.requirement_id == requirement.requirement_id), _ZERO))
        amount = min(binding.approved_credits, source_remaining, remaining_ledger, requirement_capacity)
        if amount > _ZERO:
            shadows.append(CreditPortion(attempt.attempt_id, requirement.requirement_id, amount, SHARED_SHADOW, direction, binding.binding_id))
            ledgers[direction] += amount
            shared_by_attempt[attempt.attempt_id] = shared_by_attempt.get(attempt.attempt_id, _ZERO) + amount
            ledger_binding_ids[direction].append(binding.binding_id)
            assessments.append(BindingAssessment(binding.binding_id, PASS, "exact VERIFIED shared binding", direction, amount))
        else:
            assessments.append(BindingAssessment(binding.binding_id, UNKNOWN, "shared credit has no available capacity", direction, binding.approved_credits))
            unknown_requirements.add(requirement.requirement_id)
            warnings.append(f"SHARED_CAPACITY_UNKNOWN:{binding.binding_id}")
        residual = binding.approved_credits - amount
        if residual > _ZERO:
            blocked[direction] += residual
            if source_remaining < remaining_ledger and source_remaining <= requirement_capacity:
                warnings.append(f"SHARED_SOURCE_CAP:{attempt.attempt_id}")
            if remaining_ledger <= source_remaining and remaining_ledger <= requirement_capacity:
                warnings.append(f"SHARED_LEDGER_CAP:{direction}:{binding.binding_id}")
            if requirement_capacity <= source_remaining and requirement_capacity <= remaining_ledger:
                warnings.append(f"SHARED_CAPACITY_UNKNOWN:{binding.binding_id}")
            unknown_requirements.add(requirement.requirement_id)
    ledgers_result = tuple(
        SharedLedger(direction, ledgers[direction], SHARED_LIMIT, blocked[direction], tuple(ledger_binding_ids[direction]))
        for direction in (PRIMARY_TO_TARGET, TARGET_TO_PRIMARY)
    )
    return tuple(sorted(shadows, key=lambda item: (item.direction, item.binding_id, item.requirement_id))), ledgers_result, tuple(assessments), unknown_requirements, warnings


def _subset_constraint_evaluations(
    requirements: tuple[RequirementSpec, ...],
    attempts: Sequence[CourseAttempt] | None,
    exclusive_allocations: Sequence[AttemptAllocation] | None,
) -> tuple[tuple[Mapping[str, Any], ...], dict[str, str], dict[str, tuple[str, ...]]]:
    """Evaluate constraints over already selected EXCLUSIVE portions.

    A subset is observational: it never adds a credit portion or changes the
    source ledger.  Unknown membership evidence is retained per constraint so
    a verified free-total route can remain visible while its science subset
    is still unresolved.
    """

    if attempts is None or exclusive_allocations is None:
        return (), {}, {}
    attempts_by_id = {item.attempt_id: item for item in attempts}
    requirement_ids = {item.requirement_id for item in requirements}
    results: list[Mapping[str, Any]] = []
    statuses: dict[str, str] = {}
    blockers_by_requirement: dict[str, tuple[str, ...]] = {}

    def values(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            return ()
        return tuple(dict.fromkeys(_text(item) for item in value if _text(item)))

    def first_value(constraint: Mapping[str, Any], keys: Sequence[str]) -> tuple[Any, bool]:
        for key in keys:
            if key in constraint and constraint.get(key) not in (None, ""):
                return constraint.get(key), True
        return None, False

    for requirement in requirements:
        constraints = requirement.subset_constraints
        if not constraints:
            continue
        if not requirement.required:
            for index, constraint in enumerate(constraints):
                constraint_id = _text(constraint.get("constraint_id") or constraint.get("id")) or f"subset:{index + 1}"
                membership_id = _text(constraint.get("membership_id") or constraint.get("pool_id"))
                observed_ids = values(
                    constraint.get("observed_requirement_ids", constraint.get("observed_requirements", ()))
                )
                observed_scope = tuple(sorted(observed_ids or (requirement.requirement_id,)))
                source_reference = _text(
                    constraint.get("source_reference")
                    or constraint.get("evidence_reference")
                    or constraint.get("policy_source_reference")
                )
                results.append(
                    {
                        "requirement_id": requirement.requirement_id,
                        "constraint_id": constraint_id,
                        "membership_id": membership_id,
                        "required_credits": _ZERO,
                        "minimum_credits": _ZERO,
                        "maximum_credits": None,
                        "minimum_course_count": None,
                        "maximum_course_count": None,
                        "verified_course_count": 0,
                        "unknown_candidate_course_count": 0,
                        "amount_semantics": "NOT_APPLICABLE",
                        "observed_requirement_ids": observed_scope,
                        "excluded_membership_ids": (),
                        "excluded_membership_id": "",
                        "verified_credits": _ZERO,
                        "unknown_candidate_credits": _ZERO,
                        "exempted_credits": _ZERO,
                        "status": NOT_APPLICABLE,
                        "matched_attempt_ids": (),
                        "source_reference": source_reference,
                    }
                )
            statuses[requirement.requirement_id] = NOT_APPLICABLE
            continue
        requirement_statuses: list[str] = []
        requirement_blockers: list[str] = []
        for index, constraint in enumerate(constraints):
            constraint_id = _text(constraint.get("constraint_id") or constraint.get("id")) or f"subset:{index + 1}"
            membership_id = _text(constraint.get("membership_id") or constraint.get("pool_id"))
            minimum_value, minimum_present = first_value(
                constraint,
                ("minimum_credits", "required_credits", "minimum"),
            )
            maximum_value, maximum_present = first_value(
                constraint,
                ("maximum_credits", "max_credits", "maximum", "credit_cap"),
            )
            count_value, count_present = first_value(
                constraint,
                ("minimum_course_count",),
            )
            maximum_count_value, maximum_count_present = first_value(
                constraint,
                ("maximum_course_count", "max_course_count", "course_count_cap"),
            )
            amount_semantics = _text(constraint.get("amount_semantics")).upper().replace("-", "_").replace(" ", "_")
            # A maximum-only constraint is a valid observational cap.  A
            # legacy ``required_credits`` field remains a minimum unless the
            # source explicitly labels the constraint as MAXIMUM.
            if amount_semantics == "MAXIMUM" and not maximum_present and minimum_present:
                maximum_value, maximum_present = minimum_value, True
                minimum_value, minimum_present = None, False
            minimum_valid = not minimum_present or _is_valid_nonnegative_decimal(minimum_value)
            maximum_valid = not maximum_present or _is_valid_nonnegative_decimal(maximum_value)
            minimum_course_count = _nonnegative_integer(count_value) if count_present else None
            maximum_course_count = _nonnegative_integer(maximum_count_value) if maximum_count_present else None
            count_valid = (
                (not count_present or minimum_course_count is not None)
                and (not maximum_count_present or maximum_course_count is not None)
            )
            minimum = _nonnegative_decimal(minimum_value) if minimum_present else _ZERO
            maximum = _nonnegative_decimal(maximum_value) if maximum_present else None
            if maximum is not None and minimum > maximum:
                maximum_valid = False
            observed_ids = values(
                constraint.get("observed_requirement_ids", constraint.get("observed_requirements", ()))
            )
            observed_scope = frozenset(observed_ids or (requirement.requirement_id,))
            unknown_observed_ids = tuple(sorted(set(observed_scope).difference(requirement_ids)))
            excluded_ids = values(
                constraint.get(
                    "excluded_membership_ids",
                    constraint.get("excluded_membership_id", constraint.get("excluded_membership", ())),
                )
            )
            valid_shape = (
                bool(membership_id)
                and (minimum_present or maximum_present or count_present or maximum_count_present)
                and minimum_valid
                and maximum_valid
                and count_valid
            )
            matched_ids: list[str] = []
            verified_amount = _ZERO
            unknown_amount = _ZERO
            exempted_amount = _ZERO
            verified_course_count = 0
            unknown_course_count = 0
            relevant_portions: list[tuple[CourseAttempt, CreditPortion]] = []
            seen_portions: set[tuple[Any, ...]] = set()
            for allocation in exclusive_allocations:
                attempt = attempts_by_id.get(allocation.attempt_id)
                if attempt is None:
                    continue
                for portion_index, portion in enumerate(allocation.portions):
                    if portion.allocation_kind != EXCLUSIVE or portion.credits <= _ZERO or portion.requirement_id not in observed_scope:
                        continue
                    # The same EXCLUSIVE portion is observed once even when a
                    # source registry accidentally repeats an observed ID.
                    portion_key = (
                        allocation.attempt_id,
                        portion_index,
                        portion.requirement_id,
                        str(portion.credits),
                        portion.binding_id,
                    )
                    if portion_key in seen_portions:
                        continue
                    seen_portions.add(portion_key)
                    relevant_portions.append((attempt, portion))

            counted_attempt_ids: set[str] = set()
            for attempt, portion in relevant_portions:
                record = _pool_membership_record(attempt, membership_id)
                exemption_records = tuple(
                    _pool_membership_record(attempt, excluded_id)
                    for excluded_id in excluded_ids
                )
                exemption_verified = any(item is not None and item[0] == VERIFIED for item in exemption_records)
                exemption_unknown = any(
                    _membership_record_is_uncertain(item)
                    for item in exemption_records
                )
                if (
                    (count_present or maximum_count_present)
                    and attempt.attempt_id not in counted_attempt_ids
                    and attempt.status == PASS
                    and attempt.available_credits > _ZERO
                ):
                    counted_attempt_ids.add(attempt.attempt_id)
                    if _is_trusted_verified_membership(record):
                        verified_course_count += 1
                        matched_ids.append(attempt.attempt_id)
                    elif not _is_trusted_negative_membership(record):
                        # Missing, unresolved, conflicted, or unverifiable
                        # membership evidence cannot prove a course count.
                        unknown_course_count += 1
                        matched_ids.append(attempt.attempt_id)
                if maximum_present:
                    if exemption_verified:
                        exempted_amount += portion.credits
                    elif record is not None and record[0] == VERIFIED and not exemption_unknown:
                        verified_amount += portion.credits
                        matched_ids.append(attempt.attempt_id)
                    elif _is_trusted_negative_membership(record) and not exemption_unknown:
                        # An explicit server-owned negative is known to be
                        # outside the capped pool and contributes no amount.
                        pass
                    else:
                        # Missing membership is unresolved.  A different
                        # VERIFIED pool, identity, or legacy global marker is
                        # not a negative assertion for this target pool.
                        unknown_amount += portion.credits
                        matched_ids.append(attempt.attempt_id)
                if minimum_present:
                    if record is not None and record[0] == VERIFIED:
                        # The minimum and maximum observe the same portion;
                        # this amount is intentionally not added twice.
                        if not maximum_present:
                            verified_amount += portion.credits
                        matched_ids.append(attempt.attempt_id)
                    elif _is_trusted_negative_membership(record):
                        # A known non-member does not contribute to a minimum.
                        pass
                    elif record is None:
                        # Absence of a classification is unresolved for a
                        # minimum subset whenever the source carried an
                        # official pool or the constraint observes another
                        # requirement.  It is never proof of non-membership.
                        if membership_id in requirement.eligible_pool_ids or attempt.pool_memberships or observed_ids:
                            unknown_amount += portion.credits
                            matched_ids.append(attempt.attempt_id)
                    elif record[0] not in {CONFLICTED}:
                        unknown_amount += portion.credits
                        matched_ids.append(attempt.attempt_id)

            # If both bounds are present, ``verified_amount`` was computed by
            # the maximum branch and is also the verified minimum amount.
            # Recompute the minimum view only when the constraint is minimum
            # only, keeping one observed ledger amount for the result.
            if minimum_present and maximum_present:
                verified_minimum = _ZERO
                unknown_minimum = _ZERO
                for attempt, portion in relevant_portions:
                    record = _pool_membership_record(attempt, membership_id)
                    if record is not None and record[0] == VERIFIED:
                        verified_minimum += portion.credits
                    elif _is_trusted_negative_membership(record):
                        pass
                    elif record is None:
                        if membership_id in requirement.eligible_pool_ids or attempt.pool_memberships or observed_ids:
                            unknown_minimum += portion.credits
                    elif record[0] not in {CONFLICTED}:
                        unknown_minimum += portion.credits
                minimum_verified_amount = verified_minimum
                minimum_unknown_amount = unknown_minimum
            else:
                minimum_verified_amount = verified_amount
                minimum_unknown_amount = unknown_amount

            if not valid_shape or unknown_observed_ids:
                state = UNKNOWN
                requirement_blockers.append(f"SUBSET_CONSTRAINT_INVALID:{requirement.requirement_id}:{constraint_id}")
            else:
                minimum_state = PASS
                if minimum_present:
                    if minimum_verified_amount >= minimum:
                        minimum_state = PASS
                    elif minimum_verified_amount + minimum_unknown_amount >= minimum or minimum_unknown_amount > _ZERO:
                        minimum_state = UNKNOWN
                    else:
                        minimum_state = FAIL
                        requirement_blockers.append(f"SUBSET_CONSTRAINT_DEFICIT:{requirement.requirement_id}:{constraint_id}")
                maximum_state = PASS
                if maximum_present:
                    if verified_amount > (maximum or _ZERO):
                        maximum_state = FAIL if unknown_amount <= _ZERO else UNKNOWN
                    elif verified_amount + unknown_amount > (maximum or _ZERO):
                        maximum_state = UNKNOWN
                    else:
                        maximum_state = PASS
                    if maximum_state == FAIL:
                        requirement_blockers.append(f"SUBSET_CONSTRAINT_EXCESS:{requirement.requirement_id}:{constraint_id}")
                    elif maximum_state == UNKNOWN:
                        requirement_blockers.append(f"SUBSET_CONSTRAINT_EVIDENCE_UNKNOWN:{requirement.requirement_id}:{constraint_id}")
                count_state = PASS
                if count_present:
                    if verified_course_count >= (minimum_course_count or 0):
                        count_state = PASS
                    elif verified_course_count + unknown_course_count >= (minimum_course_count or 0):
                        count_state = UNKNOWN
                    else:
                        count_state = FAIL
                        requirement_blockers.append(
                            f"SUBSET_CONSTRAINT_COUNT_DEFICIT:{requirement.requirement_id}:{constraint_id}"
                        )
                maximum_count_state = PASS
                if maximum_count_present:
                    maximum_count = maximum_course_count or 0
                    if verified_course_count > maximum_count:
                        maximum_count_state = FAIL
                        requirement_blockers.append(
                            f"SUBSET_CONSTRAINT_COUNT_EXCESS:{requirement.requirement_id}:{constraint_id}"
                        )
                    elif verified_course_count + unknown_course_count > maximum_count:
                        maximum_count_state = UNKNOWN
                        requirement_blockers.append(
                            f"SUBSET_CONSTRAINT_COUNT_EVIDENCE_UNKNOWN:{requirement.requirement_id}:{constraint_id}"
                        )
                if FAIL in {minimum_state, maximum_state, count_state, maximum_count_state}:
                    state = FAIL
                elif UNKNOWN in {minimum_state, maximum_state, count_state, maximum_count_state}:
                    state = UNKNOWN
                else:
                    state = PASS
                if state == UNKNOWN and not any(
                    blocker.endswith(f":{constraint_id}")
                    for blocker in requirement_blockers
                ):
                    requirement_blockers.append(f"SUBSET_CONSTRAINT_EVIDENCE_UNKNOWN:{requirement.requirement_id}:{constraint_id}")
            requirement_statuses.append(state)
            source_reference = _text(
                constraint.get("source_reference")
                or constraint.get("evidence_reference")
                or constraint.get("policy_source_reference")
            )
            results.append(
                {
                    "requirement_id": requirement.requirement_id,
                    "constraint_id": constraint_id,
                    "membership_id": membership_id,
                    "required_credits": minimum,
                    "minimum_credits": minimum if minimum_present else None,
                    "maximum_credits": maximum,
                    "minimum_course_count": minimum_course_count,
                    "maximum_course_count": maximum_course_count,
                    "verified_course_count": verified_course_count,
                    "unknown_candidate_course_count": unknown_course_count,
                    "amount_semantics": amount_semantics or ("MAXIMUM" if maximum_present and not minimum_present else "MINIMUM"),
                    "observed_requirement_ids": tuple(sorted(observed_scope)),
                    "excluded_membership_ids": excluded_ids,
                    "excluded_membership_id": excluded_ids[0] if len(excluded_ids) == 1 else "",
                    "verified_credits": verified_amount,
                    "unknown_candidate_credits": unknown_amount,
                    "exempted_credits": exempted_amount,
                    "status": state,
                    "matched_attempt_ids": tuple(sorted(set(matched_ids))),
                    "source_reference": source_reference,
                }
            )
        if FAIL in requirement_statuses:
            statuses[requirement.requirement_id] = FAIL
        elif UNKNOWN in requirement_statuses:
            statuses[requirement.requirement_id] = UNKNOWN
        else:
            statuses[requirement.requirement_id] = PASS
        if requirement_blockers:
            blockers_by_requirement[requirement.requirement_id] = tuple(dict.fromkeys(requirement_blockers))
    return tuple(results), statuses, blockers_by_requirement


def _evaluate_requirements(
    requirements: tuple[RequirementSpec, ...],
    exclusive_amounts: Mapping[str, Decimal],
    shadows: Sequence[CreditPortion],
    unknown_requirements: set[str],
    waived_requirements: set[str] | None = None,
    *,
    potential_unknown_requirements: set[str] | None = None,
    attempts: Sequence[CourseAttempt] | None = None,
    exclusive_allocations: Sequence[AttemptAllocation] | None = None,
) -> tuple[tuple[RequirementResult, ...], str, list[str]]:
    waived_requirements = waived_requirements or set()
    requirements_by_id = {item.requirement_id: item for item in requirements}
    shared_amounts: dict[str, Decimal] = {}
    for portion in shadows:
        shared_amounts[portion.requirement_id] = shared_amounts.get(portion.requirement_id, _ZERO) + portion.credits
    _subset_results, subset_statuses, subset_blockers_by_requirement = _subset_constraint_evaluations(
        requirements,
        attempts,
        exclusive_allocations,
    )
    results: list[RequirementResult] = []
    blockers: list[str] = []
    for requirement in requirements:
        exclusive = exclusive_amounts.get(requirement.requirement_id, _ZERO)
        shared = shared_amounts.get(requirement.requirement_id, _ZERO)
        effective = exclusive + shared
        deficit = max(_ZERO, requirement.credits_required - effective)
        # ``RequirementSpec.waiver`` only declares that a waiver is possible;
        # it is not student-specific evidence.  Only an independently
        # verified WaiverDecision can make this requirement waived.
        waived = requirement.requirement_id in waived_requirements
        if waived:
            # A waiver is a visible decision, not earned credit.  It can
            # satisfy the requirement only when the requirement evidence is
            # itself complete and verified.
            deficit = _ZERO
        requirement_blockers: list[str] = list(subset_blockers_by_requirement.get(requirement.requirement_id, ()))
        waiver_unresolved = requirement.waiver and requirement.requirement_id in unknown_requirements and not waived
        subset_status = subset_statuses.get(requirement.requirement_id)
        if waived and _is_subset_gate_requirement(requirement):
            requirement_blockers = []
        if not requirement.required:
            status = NOT_APPLICABLE
        elif _is_subset_gate_requirement(requirement) and waived:
            # A formal, subject/version-bound waiver satisfies this gate only;
            # it never creates an earned-credit portion for the observed GE
            # requirements.
            status = PASS
            deficit = _ZERO
        elif _is_subset_gate_requirement(requirement) and subset_status == FAIL:
            status = FAIL
            deficit = _ZERO
        elif _is_subset_gate_requirement(requirement) and subset_status == UNKNOWN:
            status = UNKNOWN
            deficit = _ZERO
        elif _is_subset_gate_requirement(requirement) and subset_status == PASS:
            if requirement.coverage_state == COMPLETE and requirement.evidence_state == VERIFIED:
                status = PASS
                deficit = _ZERO
            else:
                status = UNKNOWN
                deficit = _ZERO
                requirement_blockers.append(f"REQUIREMENT_COVERAGE_UNKNOWN:{requirement.requirement_id}")
        elif requirement.credits_required == _ZERO and not waived:
            status = UNKNOWN
            requirement_blockers.append(
                f"ZERO_CREDIT_GATE_EVIDENCE_REQUIRED:{requirement.requirement_id}"
            )
        elif deficit <= _ZERO:
            # A curriculum waiver flag is not evidence by itself.  If the
            # student has ordinary earned credit covering the requirement,
            # that route remains usable; only a zero-credit waiver path needs
            # an approved waiver decision.
            evidence_unknown = requirement.requirement_id in unknown_requirements and not (
                waiver_unresolved and effective > _ZERO
            )
            if requirement.coverage_state == COMPLETE and requirement.evidence_state == VERIFIED and not evidence_unknown:
                status = PASS
            else:
                status = UNKNOWN
                requirement_blockers.append(f"REQUIREMENT_COVERAGE_UNKNOWN:{requirement.requirement_id}")
        elif requirement.requirement_id in unknown_requirements or requirement.requirement_id in (potential_unknown_requirements or ()):
            status = UNKNOWN
            requirement_blockers.append(
                f"WAIVER_DECISION_REQUIRED:{requirement.requirement_id}"
                if waiver_unresolved
                else f"REQUIREMENT_EVIDENCE_UNKNOWN:{requirement.requirement_id}"
            )
        else:
            status = FAIL
            requirement_blockers.append(f"REQUIREMENT_DEFICIT:{requirement.requirement_id}")
        blockers.extend(requirement_blockers)
        results.append(
            RequirementResult(
                requirement.requirement_id,
                status,
                requirement.credits_required,
                exclusive,
                shared,
                effective,
                deficit,
                requirement.coverage_state,
                requirement.evidence_state,
                waived,
                tuple(requirement_blockers),
            )
        )
    statuses = [item.status for item in results if item.status != NOT_APPLICABLE]
    statuses.extend(
        status
        for requirement_id, status in subset_statuses.items()
        if status != NOT_APPLICABLE
        and not (
            requirement_id in waived_requirements
            and _is_subset_gate_requirement(requirements_by_id.get(requirement_id))
        )
    )
    status = FAIL if FAIL in statuses else UNKNOWN if UNKNOWN in statuses else PASS
    return tuple(results), status, blockers


def allocate_credits(
    attempts: Any,
    requirements: Any,
    bindings: Any = (),
    *,
    search_limit: int = 10000,
    waiver_decisions: Any = (),
) -> AllocationResult:
    """Allocate immutable transcript attempts to exact requirements.

    The search is deterministic and bounded.  Exhaustion without a complete
    verified witness remains UNKNOWN; a validated witness proves feasibility
    even when optimization has not visited every alternative.
    """

    normalized_requirements, requirement_issues = _normalize_requirements(requirements, with_issues=True)
    normalized_requirements, route_issues = canonicalize_overflow_routes(normalized_requirements)
    requirement_issues = tuple(sorted(set((*requirement_issues, *route_issues))))
    normalized_attempts, attempt_issues = _normalize_attempts(attempts, with_issues=True)
    normalized_attempts = _repeat_filter(normalized_attempts, normalized_requirements)
    normalized_bindings, binding_issues = _normalize_bindings(bindings, with_issues=True)
    normalized_waiver_decisions, waiver_issues = _normalize_waiver_decisions(waiver_decisions, with_issues=True)
    waived_requirements, waiver_assessments, waiver_unknown, waiver_warnings = _validate_waiver_decisions(
        normalized_waiver_decisions,
        normalized_requirements,
    )
    if waiver_issues:
        conflicting_waiver_ids = {
            issue.split(":", 1)[1]
            for issue in waiver_issues
            if issue.startswith("DUPLICATE_WAIVER_DECISION_ID:")
        }
        waiver_unknown.update(
            decision.target_requirement_id
            for decision in normalized_waiver_decisions
            if decision.decision_id in conflicting_waiver_ids and decision.target_requirement_id
        )
    input_issues = tuple((*requirement_issues, *binding_issues, *attempt_issues, *waiver_issues))
    requirements_by_id = {item.requirement_id: item for item in normalized_requirements}
    repeat_selection_winners, repeat_selection_unknown_groups = _repeat_selection_state(
        normalized_attempts,
        normalized_requirements,
    )
    valid_exclusive_bindings: dict[str, list[EquivalencyBinding]] = {}
    pending_binding_requirements: set[str] = set()
    binding_assessments_seed: list[BindingAssessment] = []
    attempts_by_id = {item.attempt_id: item for item in normalized_attempts}
    for binding in normalized_bindings:
        if binding.shared:
            continue
        attempt = attempts_by_id.get(binding.source_attempt_id)
        requirement = requirements_by_id.get(binding.target_requirement_id)
        if attempt is None or requirement is None:
            pending_binding_requirements.add(binding.target_requirement_id)
            binding_assessments_seed.append(BindingAssessment(binding.binding_id, UNKNOWN, "binding source/target is missing", binding.direction, binding.approved_credits))
            continue
        valid, reason = _binding_is_verified(binding, attempt, requirement)
        if valid and attempt.available_credits <= _ZERO:
            valid = False
            reason = "source attempt has no positive earned credit"
        if valid:
            valid_exclusive_bindings.setdefault(attempt.attempt_id, []).append(binding)
            binding_assessments_seed.append(BindingAssessment(binding.binding_id, PASS, "exact VERIFIED exclusive binding", binding.direction, binding.approved_credits))
        else:
            pending_binding_requirements.add(requirement.requirement_id)
            binding_assessments_seed.append(BindingAssessment(binding.binding_id, UNKNOWN, reason, binding.direction, binding.approved_credits))

    candidate_requirements: dict[str, set[str]] = {}
    unknown_candidates: set[str] = set(pending_binding_requirements)
    potential_candidate_unknown: set[str] = set()
    unknown_candidates.update(
        item.requirement_id for item in normalized_requirements if not item.credits_required_valid
    )
    if attempt_issues:
        # A conflicting attempt ID means the course identity/credits are not
        # stable enough for any requirement to be reported as a formal pass.
        unknown_candidates.update(item.requirement_id for item in normalized_requirements)
    for attempt in normalized_attempts:
        candidate_requirements[attempt.attempt_id] = {
            binding.target_requirement_id for binding in valid_exclusive_bindings.get(attempt.attempt_id, ())
        }
        for requirement in normalized_requirements:
            if attempt.status == WAIVER and requirement.waiver:
                candidate_requirements[attempt.attempt_id].add(requirement.requirement_id)
                continue
            if any(
                binding.target_requirement_id == requirement.requirement_id
                for binding in valid_exclusive_bindings.get(attempt.attempt_id, ())
            ):
                # An explicit equivalency record is the authoritative route
                # for this pair; do not silently replace it with a bare
                # direct requirement ID and lose its credit cap/audit handle.
                continue
            direct = _direct_match(attempt, requirement)
            if direct is True:
                if (
                    attempt.repeat_group_id in repeat_selection_unknown_groups
                    and requirement.repeat_policy == BEST_ATTEMPT_ONLY
                ):
                    unknown_candidates.add(requirement.requirement_id)
                candidate_requirements[attempt.attempt_id].add(requirement.requirement_id)
            elif direct is None:
                # This unselected route cannot invalidate sufficient verified
                # credits, but prevents declaring a remaining deficit final.
                potential_candidate_unknown.add(requirement.requirement_id)

    # Explore constrained courses first, then useful allocations before skips.
    # Bounds still distinguish an actual complete witness from no-witness
    # search exhaustion; changing order does not create evidence or credits.
    search_attempts = tuple(sorted(
        normalized_attempts,
        key=lambda item: (len(candidate_requirements.get(item.attempt_id, ())), item.attempt_id),
    ))
    candidate_counts = {
        requirement.requirement_id: sum(
            requirement.requirement_id in candidate_requirements.get(item.attempt_id, ())
            for item in normalized_attempts
        )
        for requirement in normalized_requirements
    }
    limit = max(1, int(search_limit)) if isinstance(search_limit, (int, float, Decimal)) else 10000
    nodes = 0
    exhausted = False
    best_choices: list[AttemptAllocation] | None = None
    best_amounts: dict[str, Decimal] | None = None
    best_score: tuple[Any, ...] | None = None
    best_signature: tuple[Any, ...] | None = None
    best_signatures: set[tuple[Any, ...]] = set()
    witness_choices: tuple[AttemptAllocation, ...] | None = None
    witness_amounts: dict[str, Decimal] | None = None
    witness_signature: tuple[Any, ...] | None = None
    # Prefer the deterministic best grade/credit attempt when a fixed repeat
    # group has more than one numerically viable route.  Equal-quality
    # attempts intentionally retain their tie so the caller can decide if
    # the records are truly interchangeable.
    repeat_quality_ranks: dict[str, int] = {}
    grouped_for_quality: dict[str, list[CourseAttempt]] = {}
    for item in normalized_attempts:
        if item.repeat_group_id:
            grouped_for_quality.setdefault(item.repeat_group_id, []).append(item)
    for grouped in grouped_for_quality.values():
        qualities = sorted({_attempt_quality(item) for item in grouped}, reverse=True)
        rank_by_quality = {quality: len(qualities) - index for index, quality in enumerate(qualities)}
        for item in grouped:
            repeat_quality_ranks[item.attempt_id] = rank_by_quality[_attempt_quality(item)]

    def score(choices: Sequence[AttemptAllocation], amounts: Mapping[str, Decimal]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        legalized = _legalize_choices(
            normalized_attempts,
            choices,
            requirements_by_id,
            repeat_selection_winners=repeat_selection_winners,
            repeat_selection_unknown_groups=repeat_selection_unknown_groups,
        )
        shadows, _ledgers, _assessments, shared_unknown, _warnings = _shared_projection(
            normalized_attempts,
            normalized_requirements,
            normalized_bindings,
            amounts,
            legalized,
        )
        unknown = set(unknown_candidates) | shared_unknown
        unknown |= _repeat_evidence_unknown(normalized_attempts, legalized, requirements_by_id)
        results, status, _blockers = _evaluate_requirements(
            normalized_requirements,
            amounts,
            shadows,
            unknown | waiver_unknown,
            waived_requirements,
            potential_unknown_requirements=potential_candidate_unknown,
            attempts=normalized_attempts,
            exclusive_allocations=legalized,
        )
        pass_count = sum(item.status == PASS for item in results)
        unknown_count = sum(item.status == UNKNOWN for item in results)
        deficit = sum((item.deficit for item in results), _ZERO)
        recognized = sum((portion.credits for choice in legalized for portion in choice.portions if portion.allocation_kind == EXCLUSIVE), _ZERO)
        repeat_quality = sum(
            repeat_quality_ranks.get(choice.attempt_id, 0)
            for choice in legalized
            if _repeat_mode_for_portions(choice.portions, requirements_by_id) == BEST_ATTEMPT_ONLY
        )
        primary_status = 1 if status == PASS else 0
        # A definite academic deficit is more informative than an unresolved
        # coverage flag: when the same branch is numerically short, retain
        # FAIL instead of choosing an empty/unknown branch merely because it
        # has fewer UNKNOWN requirement rows.  Coverage still prevents PASS
        # once the numeric gate is met.
        return (primary_status, pass_count, -deficit, -unknown_count, repeat_quality, recognized), _signature(legalized)

    def complete_witness(
        choices: Sequence[AttemptAllocation],
        amounts: Mapping[str, Decimal],
    ) -> tuple[bool, tuple[AttemptAllocation, ...]]:
        """Check whether a branch is a complete, conservative PASS witness."""

        legalized = _legalize_choices(
            normalized_attempts,
            choices,
            requirements_by_id,
            repeat_selection_winners=repeat_selection_winners,
            repeat_selection_unknown_groups=repeat_selection_unknown_groups,
        )
        shadows, _ledgers, _assessments, shared_unknown, _warnings = _shared_projection(
            normalized_attempts,
            normalized_requirements,
            normalized_bindings,
            amounts,
            legalized,
        )
        unknown = set(unknown_candidates) | shared_unknown
        unknown |= _repeat_evidence_unknown(normalized_attempts, legalized, requirements_by_id)
        results, branch_status, _blockers = _evaluate_requirements(
            normalized_requirements,
            amounts,
            shadows,
            unknown | waiver_unknown,
            waived_requirements,
            potential_unknown_requirements=potential_candidate_unknown,
            attempts=normalized_attempts,
            exclusive_allocations=legalized,
        )
        if branch_status != PASS or input_issues:
            return False, legalized
        active_ids = _active_attempt_ids(
            normalized_attempts,
            legalized,
            requirements_by_id,
            repeat_selection_winners=repeat_selection_winners,
            repeat_selection_unknown_groups=repeat_selection_unknown_groups,
        )
        source = sum((item.available_credits for item in normalized_attempts if item.attempt_id in active_ids), _ZERO)
        recognized = sum(
            (portion.credits for item in legalized for portion in item.portions if portion.allocation_kind == EXCLUSIVE),
            _ZERO,
        )
        unallocated = sum((item.unallocated_credits for item in legalized), _ZERO)
        return bool(results) and recognized + unallocated == source, legalized

    def visit(
        index: int,
        amounts: dict[str, Decimal],
        choices: list[AttemptAllocation],
        repeat_states: dict[str, tuple[str, str | None]],
    ):
        nonlocal nodes, exhausted, best_choices, best_amounts, best_score, best_signature, best_signatures
        nonlocal witness_choices, witness_amounts, witness_signature
        if nodes >= limit:
            exhausted = True
            return
        nodes += 1
        if index >= len(search_attempts):
            current_score, current_signature = score(choices, amounts)
            is_witness, legalized_witness = complete_witness(choices, amounts)
            if is_witness and (
                witness_signature is None or current_signature < witness_signature
            ):
                witness_signature = current_signature
                witness_choices = legalized_witness
                witness_amounts = dict(amounts)
            if best_score is None or current_score > best_score:
                best_score = current_score
                best_signature = current_signature
                best_choices = list(choices)
                best_amounts = dict(amounts)
                best_signatures = {current_signature}
            elif current_score == best_score:
                best_signatures.add(current_signature)
                if current_signature < (best_signature or ()):
                    best_signature = current_signature
                    best_choices = list(choices)
                    best_amounts = dict(amounts)
            return
        attempt = search_attempts[index]
        options: list[tuple[tuple[CreditPortion, ...], Decimal]] = [((), attempt.available_credits)]
        for requirement_id in sorted(
            candidate_requirements.get(attempt.attempt_id, ()),
            key=lambda rid: (
                _text(requirements_by_id[rid].kind).upper() == "AGGREGATE",
                candidate_counts.get(rid, 0),
                rid,
            ),
        ):
            requirement = requirements_by_id.get(requirement_id)
            if requirement is None:
                continue
            # A verified equivalency binding is the authoritative route for
            # this attempt/requirement pair.  Never add a bare direct option
            # that could spend more than ``approved_credits`` or lose the
            # binding audit identity.
            if any(
                binding.target_requirement_id == requirement_id
                for binding in valid_exclusive_bindings.get(attempt.attempt_id, ())
            ):
                continue
            option = _option_portions(
                attempt,
                requirement,
                requirements_by_id,
                amounts,
                selected_allocations=choices,
                attempts_by_id=attempts_by_id,
            )
            if option is not None:
                options.append(option)
                # Keep an explicit full-credit branch for diagnostics.  A
                # capped branch may be the feasible witness when replacement
                # courses exist, while the uncapped branch still exposes a
                # verified maximum excess when no complete replacement is
                # possible.
                uncapped = _option_portions(
                    attempt,
                    requirement,
                    requirements_by_id,
                    amounts,
                    respect_subset_maximum=False,
                    selected_allocations=choices,
                    attempts_by_id=attempts_by_id,
                )
                if uncapped is not None and uncapped != option:
                    options.append(uncapped)
        # Explicit exact bindings are candidates even when the transcript
        # identity itself is not otherwise known.
        for binding in sorted(valid_exclusive_bindings.get(attempt.attempt_id, ()), key=lambda item: item.binding_id):
            requirement = requirements_by_id.get(binding.target_requirement_id)
            option = (
                _option_portions(
                    attempt,
                    requirement,
                    requirements_by_id,
                    amounts,
                    binding,
                    selected_allocations=choices,
                    attempts_by_id=attempts_by_id,
                )
                if requirement
                else None
            )
            if option is not None and option not in options:
                options.append(option)
        # A waiver is an explicit, auditable requirement decision.  Keep its
        # zero-credit portion visible even though the ordinary skip branch
        # has the same numeric score.
        if attempt.status == WAIVER and len(options) > 1:
            options = options[1:]
        elif len(options) > 1:
            options = options[1:] + options[:1]
        for portions, residual in options:
            if not _repeat_option_allowed(
                attempt,
                portions,
                requirements_by_id,
                repeat_states,
                repeat_selection_winners=repeat_selection_winners,
                repeat_selection_unknown_groups=repeat_selection_unknown_groups,
            ):
                continue
            group_id = attempt.repeat_group_id
            repeat_mode = _repeat_mode_for_portions(portions, requirements_by_id)
            previous_state = repeat_states.get(group_id) if group_id else None
            if group_id and repeat_mode is not None and previous_state is None:
                repeat_states[group_id] = (repeat_mode, attempt.attempt_id if repeat_mode == BEST_ATTEMPT_ONLY else None)
            updated = _apply_portions(amounts, portions)
            choices.append(AttemptAllocation(attempt.attempt_id, attempt.available_credits, portions, residual))
            visit(index + 1, updated, choices, repeat_states)
            choices.pop()
            if group_id and repeat_mode is not None and previous_state is None:
                repeat_states.pop(group_id, None)

    # Large transcripts have many interchangeable pool routes.  First build
    # one demand-aware candidate so static DFS ordering cannot hide an easy
    # feasible allocation behind thousands of less useful pool combinations.
    # This is only a seed: it uses the normal option/repeat guards and must
    # pass the complete witness validator, including all subset constraints.
    if len(search_attempts) > 24 and not input_issues:
        seed_amounts: dict[str, Decimal] = {}
        seed_choices: list[AttemptAllocation] = []
        seed_repeat_states: dict[str, tuple[str, str | None]] = {}
        remaining = {item.attempt_id: item for item in search_attempts}
        while remaining:
            unmet = {
                item.requirement_id: item
                for item in normalized_requirements
                if seed_amounts.get(item.requirement_id, _ZERO) < item.credits_required
                and item.requirement_id not in waived_requirements
            }
            ranked_routes = []
            for rid, requirement in unmet.items():
                eligible = []
                for aid, item in remaining.items():
                    if rid not in candidate_requirements.get(aid, ()):
                        continue
                    bindings_for_pair = [
                        binding for binding in valid_exclusive_bindings.get(aid, ())
                        if binding.target_requirement_id == rid
                    ]
                    for binding in bindings_for_pair or [None]:
                        option = _option_portions(
                            item, requirement, requirements_by_id, seed_amounts, binding,
                            selected_allocations=seed_choices, attempts_by_id=attempts_by_id,
                        )
                        if option is None or not _repeat_option_allowed(
                            item, option[0], requirements_by_id, seed_repeat_states,
                            repeat_selection_winners=repeat_selection_winners,
                            repeat_selection_unknown_groups=repeat_selection_unknown_groups,
                        ):
                            continue
                        contribution = sum((p.credits for p in option[0] if p.requirement_id == rid), _ZERO)
                        if contribution > _ZERO:
                            alternatives = len(candidate_requirements.get(aid, set()) & unmet.keys())
                            eligible.append((alternatives, aid, option, contribution))
                            break
                if not eligible:
                    continue
                deficit = requirement.credits_required - seed_amounts.get(rid, _ZERO)
                slack = sum((entry[3] for entry in eligible), _ZERO) - deficit
                is_pool = _text(requirement.kind).upper() in {"AGGREGATE", "COURSE_POOL", "QUOTA"}
                ranked_routes.append((is_pool, slack, rid, eligible))
            if not ranked_routes:
                break
            _, _, _, eligible = min(ranked_routes, key=lambda entry: entry[:3])
            # A seed visits each attempt once.  Prefer an exact fit (or a
            # legal overflow route) before consuming a larger course whose
            # residual would be stranded outside another needed pool.
            _, aid, (portions, residual), _ = min(eligible, key=lambda entry: (entry[2][1], *entry[:2]))
            item = remaining.pop(aid)
            seed_choices.append(AttemptAllocation(aid, item.available_credits, portions, residual))
            seed_amounts = _apply_portions(seed_amounts, portions)
            repeat_mode = _repeat_mode_for_portions(portions, requirements_by_id)
            if item.repeat_group_id and repeat_mode is not None and item.repeat_group_id not in seed_repeat_states:
                seed_repeat_states[item.repeat_group_id] = (
                    repeat_mode, aid if repeat_mode == BEST_ATTEMPT_ONLY else None,
                )
        seed_choices.extend(
            AttemptAllocation(item.attempt_id, item.available_credits, (), item.available_credits)
            for item in remaining.values()
        )
        is_witness, legalized_seed = complete_witness(seed_choices, seed_amounts)
        if is_witness:
            witness_choices = legalized_seed
            witness_amounts = dict(seed_amounts)
            witness_signature = _signature(legalized_seed)

    visit(0, {}, [], {})
    if best_choices is None:
        best_choices = [AttemptAllocation(item.attempt_id, item.available_credits, (), item.available_credits) for item in normalized_attempts]
        best_amounts = {}
        best_score = (0, 0, 0, _ZERO, 0, _ZERO)

    # A feasible witness is the useful result even when a bounded search has
    # not exhausted every alternative.  It proves feasibility while leaving
    # optimality explicitly bounded below.
    if witness_choices is not None and witness_amounts is not None:
        best_choices = list(witness_choices)
        best_amounts = dict(witness_amounts)

    assert best_amounts is not None
    legalized_choices = _legalize_choices(
        normalized_attempts,
        best_choices,
        requirements_by_id,
        repeat_selection_winners=repeat_selection_winners,
        repeat_selection_unknown_groups=repeat_selection_unknown_groups,
    )
    shadows, ledgers, binding_assessments, shared_unknown, shared_warnings = _shared_projection(
        normalized_attempts,
        normalized_requirements,
        normalized_bindings,
        best_amounts,
        legalized_choices,
    )
    requirement_unknown = set(unknown_candidates) | shared_unknown
    requirement_unknown |= _repeat_evidence_unknown(normalized_attempts, legalized_choices, requirements_by_id)
    requirement_results, status, blockers = _evaluate_requirements(
        normalized_requirements,
        best_amounts,
        shadows,
        requirement_unknown | waiver_unknown,
        waived_requirements,
        potential_unknown_requirements=potential_candidate_unknown,
        attempts=normalized_attempts,
        exclusive_allocations=legalized_choices,
    )
    subset_results, _subset_statuses, _subset_blockers = _subset_constraint_evaluations(
        normalized_requirements,
        normalized_attempts,
        legalized_choices,
    )
    all_binding_assessments = tuple(binding_assessments_seed) + tuple(binding_assessments)
    warnings = list(waiver_warnings) + list(shared_warnings)
    blockers.extend(input_issues)
    if not normalized_attempts and not normalized_requirements:
        blockers.append("EMPTY_INPUT")
    if not normalized_requirements:
        blockers.append("REQUIREMENTS_MISSING")
    if input_issues or not normalized_requirements or not normalized_attempts and not normalized_requirements:
        status = UNKNOWN
    if exhausted:
        if witness_choices is None:
            status = UNKNOWN
            blockers.append("SEARCH_EXHAUSTED")
        else:
            warnings.append("SEARCH_EXHAUSTED_AFTER_FEASIBLE_WITNESS")
    if any(item.status == UNKNOWN for item in all_binding_assessments):
        warnings.extend(f"UNVERIFIED_EQUIVALENCY:{item.binding_id}" for item in all_binding_assessments if item.status == UNKNOWN)
    alternatives = tuple(sorted(best_signatures, key=repr))
    allocation_ambiguous = len(alternatives) > 1
    if allocation_ambiguous:
        # Multiple complete assignments are legal evidence of feasibility;
        # ambiguity is retained as metadata and never becomes a false failure.
        if witness_choices is None:
            status = UNKNOWN
            blockers.append("ALLOCATION_AMBIGUOUS")
        warnings.append(f"ALLOCATION_ALTERNATIVES:{len(alternatives)}")
    elif shared_unknown and status == FAIL:
        # A verified-looking shared route with no usable source allocation is
        # unresolved evidence, not a definite academic failure.
        status = UNKNOWN
    active_ids = _active_attempt_ids(
        normalized_attempts,
        legalized_choices,
        requirements_by_id,
        repeat_selection_winners=repeat_selection_winners,
        repeat_selection_unknown_groups=repeat_selection_unknown_groups,
    )
    source_earned = sum((item.available_credits for item in normalized_attempts if item.attempt_id in active_ids), _ZERO)
    recognized = sum((portion.credits for choice in legalized_choices for portion in choice.portions if portion.allocation_kind == EXCLUSIVE), _ZERO)
    unallocated = sum((choice.unallocated_credits for choice in legalized_choices), _ZERO)
    conservation = recognized + unallocated == source_earned
    if not conservation:
        blockers.append("CREDIT_CONSERVATION_FAILED")
        status = UNKNOWN
    feasible_witness = witness_choices is not None and conservation and not input_issues
    search_complete = not exhausted
    feasibility = FEASIBLE if feasible_witness else INFEASIBLE if search_complete and status == FAIL else UNKNOWN
    route_targets = {
        route_id
        for requirement in normalized_requirements
        for route_id in requirement.overflow_routes
    }
    route_ambiguity = allocation_ambiguous and any(
        portion[0] in route_targets
        for signature in alternatives
        for choice in signature
        if isinstance(choice, tuple) and len(choice) > 1
        for portion in choice[1]
        if isinstance(portion, tuple) and portion
    )
    return AllocationResult(
        status=status,
        allocations=legalized_choices,
        requirement_results=requirement_results,
        source_earned_credits=source_earned,
        recognized_credits=recognized,
        unallocated_credits=unallocated,
        credit_conservation=conservation,
        shadow_allocations=shadows,
        shared_ledgers=ledgers,
        binding_assessments=all_binding_assessments,
        waiver_assessments=waiver_assessments,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        search_exhausted=exhausted,
        pass_eligible=status == PASS and feasible_witness,
        nodes_searched=nodes,
        objective=best_score or (),
        alternative_allocations=alternatives,
        allocation_ambiguous=allocation_ambiguous,
        feasibility=feasibility,
        search_complete=search_complete,
        optimality="BOUNDED_NOT_COMPLETE" if exhausted else "NON_UNIQUE" if allocation_ambiguous else "OPTIMAL",
        route_ambiguity=route_ambiguity,
        decision_ambiguity=False,
        feasible_witness=feasible_witness,
        subset_results=subset_results,
    )


__all__ = [
    "AllocationResult",
    "AttemptAllocation",
    "BEST_ATTEMPT_ONLY",
    "BindingAssessment",
    "COMBINED",
    "COMPLETE",
    "CONFLICTED",
    "CreditPortion",
    "CourseAttempt",
    "EquivalencyBinding",
    "EXCLUSIVE",
    "FEASIBLE",
    "FAIL",
    "INFEASIBLE",
    "LAB",
    "LECTURE",
    "MANUAL_REVIEW",
    "MISSING",
    "NONE",
    "NOT_MEMBER",
    "NOT_APPLICABLE",
    "PARTIAL",
    "PASS",
    "PRIMARY_TO_TARGET",
    "REPEATABLE",
    "RequirementResult",
    "RequirementSpec",
    "SHARED_BLOCKED",
    "SHARED_SHADOW",
    "SHARED_LIMIT",
    "SharedLedger",
    "TARGET_TO_PRIMARY",
    "UNKNOWN",
    "VERIFIED",
    "WAIVER",
    "WaiverAssessment",
    "WaiverDecision",
    "allocate_credits",
    "canonicalize_overflow_routes",
    "normalize_course_kind",
]
