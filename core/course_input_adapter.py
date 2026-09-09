"""Translate parser/crawler rows into the confirmation boundary.

The PDF and portal parsers pre-date :mod:`input_confirmation` and expose a
large, mutable mapping with presentation-only fields.  This module is the
only adapter between those parsers and the confirmation lifecycle.  It reads
an explicit allowlist of legacy scalar fields, emits only the exact course
row allowlist, and deliberately leaves incomplete or ambiguous rows unsafe.

No parser payload, PDF bytes, account, or credential is retained in the
returned objects.  A parser result is always ``PARSED`` (or
``UNCONFIRMED``); a caller must still use ``confirm_confirmation`` before
releasing attempts to the evaluation service.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from numbers import Real
from typing import Any

from allocation_engine import normalize_course_kind
from course_status import is_withdrawn_grade
from handbook_rules import normalize_course_name
from input_confirmation import (
    COURSE_FIELD_ALLOWLIST,
    ConfirmationState,
    CourseConfirmation,
    InputDiagnostic,
    NormalizationResult,
    normalize_course_rows,
)

_MISSING = object()
_STATUS_KEYS = ("eval_status", "status", "score", "grade")
_CREDIT_KEYS = ("total_credit", "credits", "credit", "學分")
_EARNED_KEYS = ("earned_credits", "posted_earned_credits", "transfer_earned_credits", "earned_credit")
_TERM_KEYS = ("term", "修課學期")
_YEAR_KEYS = ("academic_year", "year", "修課學年")
_SEMESTER_KEYS = ("semester", "學期")
_NAME_KEYS = ("course_name", "normalized_name", "clean_name", "name", "title", "raw_name", "科目名稱")
_CODE_KEYS = ("course_code", "code", "課程代碼", "課號")
_COMPONENT_TYPE_KEYS = ("component_type", "lecture_or_lab", "component")
_TYPE_KEYS = ("course_type", "type", "課程類別")
_DEPARTMENT_KEYS = ("department", "offering_department", "開課系所")


@dataclass(frozen=True, slots=True)
class CourseInputAdapterResult:
    """Safe adapter output used by the UI and tests.

    ``rows`` are plain allowlisted mappings suitable for a data editor;
    ``confirmation`` is the immutable lifecycle object.  ``normalization``
    contains only normalized scalar course values and safe diagnostics.
    """

    rows: tuple[Mapping[str, Any], ...]
    normalization: NormalizationResult
    confirmation: CourseConfirmation
    diagnostics: tuple[InputDiagnostic, ...] = ()

    @property
    def valid(self) -> bool:
        return self.confirmation.valid and not self.diagnostics

    @property
    def state(self) -> ConfirmationState:
        return self.confirmation.state

    @property
    def fingerprint(self) -> str:
        return self.confirmation.fingerprint

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": tuple(dict(row) for row in self.rows),
            "normalization": self.normalization.as_dict(),
            "confirmation": self.confirmation.as_dict(),
            "diagnostics": tuple(item.as_dict() for item in self.diagnostics),
            "valid": self.valid,
        }

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)


def _diagnostic(code: str, row_index: int | None = None, field: str | None = None) -> InputDiagnostic:
    messages = {
        "INPUT_NOT_ROWS": "課程資料格式無法辨識，請重新上傳或手動確認。",
        "ROW_NOT_MAPPING": "課程資料列格式無法辨識，請手動確認。",
        "MISSING_COURSE_NAME": "缺少課程名稱，不能建立可稽核的修課紀錄。",
        "MISSING_TERM": "缺少修課學期，不能建立可稽核的修課紀錄。",
        "INVALID_CREDITS": "學分資料無法辨識，不能自動採信。",
        "UNKNOWN_STATUS": "課程成績／狀態無法辨識，不能自動採信。",
        "TRANSFER_EARNED_CREDIT_UNVERIFIED": "抵免／抵認缺少校方登載的實得學分，不能產生學分。",
        "INVALID_DEFAULT_TERM": "預設修課學期格式無法辨識，不能自動補入。",
        "CONFLICTING_COMPONENT_METADATA": "課程講授／實驗標籤互相矛盾，請人工確認。",
    }
    return InputDiagnostic(
        code=code,
        message=messages.get(code, "資料無法安全採信，請人工確認。"),
        row_index=row_index,
        field=field,
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return str(value).strip()
    return ""


def _first(row: Mapping[Any, Any], keys: Sequence[str], default: Any = _MISSING) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return default


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal, Real)):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.strip())
        if not match:
            return None
        try:
            result = float(Decimal(match.group(0)))
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _has_value(row: Mapping[Any, Any], key: str) -> bool:
    return key in row and row[key] not in (None, "")


def _canonical_token(value: Any) -> str:
    return re.sub(r"\s+", "", _text(value)).casefold()


def _is_explicit_component(value: Any) -> bool:
    """Return whether a legacy type value explicitly names a component.

    Transcript ``type`` values commonly describe curriculum categories such as
    ``系必修`` or ``共同選修``.  Those labels are useful metadata, but they do
    not prove lecture/lab identity.  Only explicit component vocabulary is
    allowed to participate in the attempt-group identity.
    """

    return normalize_course_kind(value) != "UNKNOWN"


def _component_fields(row: Mapping[Any, Any]) -> tuple[str, str, bool]:
    """Return (safe display value, canonical kind, conflict marker).

    Legacy ``type`` is allowed to be a curriculum category.  It becomes a
    component signal only when its *whole normalized token* is a recognized
    lecture/lab/combined alias.  Explicit component fields and such a legacy
    signal must agree; otherwise the row is deliberately made unresolvable.
    """

    explicit_values = [
        _text(row[key])
        for key in _COMPONENT_TYPE_KEYS
        if _has_value(row, key)
    ]
    legacy_values = [
        _text(row[key])
        for key in _TYPE_KEYS
        if _has_value(row, key)
    ]
    explicit_kinds = [normalize_course_kind(value) for value in explicit_values]
    explicit_known = [kind for kind in explicit_kinds if kind != "UNKNOWN"]
    legacy_kinds = [normalize_course_kind(value) for value in legacy_values]
    legacy_known = [kind for kind in legacy_kinds if kind != "UNKNOWN"]
    if explicit_values:
        # Every explicitly component-shaped field must itself be a known
        # component and all supplied component signals must agree.  A value
        # such as ``component_type="系必修"`` is malformed metadata, not
        # permission to fall back to a category or a second field.
        explicit_set = set(explicit_known)
        if len(explicit_known) != len(explicit_values) or len(explicit_set) != 1:
            return "UNKNOWN", "UNKNOWN", True
        explicit_kind = explicit_known[0]
        if any(kind != explicit_kind for kind in legacy_known):
            return "UNKNOWN", "UNKNOWN", True
        return explicit_values[0], explicit_kind, False
    legacy_set = set(legacy_known)
    if len(legacy_set) > 1:
        return "UNKNOWN", "UNKNOWN", True
    if legacy_known:
        index = next(index for index, kind in enumerate(legacy_kinds) if kind != "UNKNOWN")
        return legacy_values[index], legacy_kinds[index], False
    if legacy_values:
        return legacy_values[0], "UNKNOWN", False
    return "", "UNKNOWN", False


def _status_and_earned(score: Any, credits: float, explicit_earned: Any = _MISSING) -> tuple[str, float, bool]:
    """Return canonical status, earned credits, and whether status is known."""

    token = _canonical_token(score)
    # Status words intentionally cover only unambiguous parser values.
    if token in {"p", "pass", "passed", "及格", "通過", "已修", "已完成", "修畢", "completed", "complete"}:
        return "COMPLETED", credits, True
    if token in {"未", "在修", "修習中", "修讀中", "修課中", "inprogress", "enrolled", "taking"}:
        return "IN_PROGRESS", 0.0, True
    if token in {"f", "fail", "failed", "不及格", "不通過"}:
        return "FAILED", 0.0, True
    if is_withdrawn_grade(score):
        return "WITHDRAWN", 0.0, True
    if token in {"免", "免修", "waived"}:
        return "WAIVED", 0.0, True
    if token in {"抵", "抵免", "抵認", "transfer", "transferred", "transfercredit"}:
        earned = _number(explicit_earned) if explicit_earned is not _MISSING else None
        return "TRANSFERRED", earned or 0.0, earned is not None

    # Numeric grades are interpreted only using the university's ordinary
    # pass boundary; the posted course credits remain the upper bound.
    numeric = _number(score)
    if numeric is not None and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", _text(score)):
        return ("COMPLETED", credits, True) if numeric >= 60 else ("FAILED", 0.0, True)
    return "UNKNOWN", 0.0, False


def _default_term_parts(default_term: Any) -> tuple[str, str] | None:
    text = _text(default_term)
    if not text:
        return None
    match = re.fullmatch(r"(\d{3,4})\s*[-/]\s*([12])", text)
    if not match:
        return None
    return match.group(1), match.group(2)


def _term_for(row: Mapping[Any, Any], semester: str | None, default_term: Any) -> tuple[str, str, str] | None:
    explicit_term = _first(row, _TERM_KEYS)
    year = _text(_first(row, _YEAR_KEYS, ""))
    explicit_semester = _text(_first(row, _SEMESTER_KEYS, ""))
    if semester:
        if not year:
            default_parts = _default_term_parts(default_term)
            if default_parts:
                year = default_parts[0]
        if year:
            return f"{year}-{semester}", year, semester
        # A single legacy row may carry a fully qualified term but no year.
        # It is safe only when it already identifies the same semester.
        if explicit_term and re.search(rf"(?:-|/)\s*{re.escape(semester)}\b", _text(explicit_term)):
            return _text(explicit_term), year, semester
        return None
    if explicit_term is not _MISSING and _text(explicit_term):
        term = _text(explicit_term)
        if explicit_semester in {"1", "2"}:
            return term, year, explicit_semester
        match = re.search(r"(?:-|/)\s*([12])\b", term)
        return term, year, match.group(1) if match else explicit_semester
    if year and explicit_semester in {"1", "2"}:
        return f"{year}-{explicit_semester}", year, explicit_semester
    default_parts = _default_term_parts(default_term)
    if default_parts:
        return f"{default_parts[0]}-{default_parts[1]}", default_parts[0], default_parts[1]
    return None


def _attempt_group(code: str, name: str, course_type: str) -> str:
    identity = code or name
    if not identity:
        return ""
    # Keep an explicitly stated component in the identity.  Curriculum
    # category labels (必修／選修／通識／共同選修, etc.) are not components and
    # must not create a fake lecture/lab distinction.
    component = normalize_course_kind(course_type)
    return f"{identity}|component:{component}" if component != "UNKNOWN" else identity


def _legacy_row_to_attempts(
    row: Mapping[Any, Any],
    row_index: int,
    *,
    default_term: Any = None,
) -> tuple[list[dict[str, Any]], list[InputDiagnostic]]:
    diagnostics: list[InputDiagnostic] = []
    name = _text(_first(row, _NAME_KEYS, ""))
    code = _text(_first(row, _CODE_KEYS, ""))
    if not name and not code:
        diagnostics.append(_diagnostic("MISSING_COURSE_NAME", row_index, "course_name"))
        return [], diagnostics

    course_type, _component_kind, component_conflict = _component_fields(row)
    if component_conflict:
        diagnostics.append(_diagnostic("CONFLICTING_COMPONENT_METADATA", row_index, "course_type"))
    department = _text(_first(row, _DEPARTMENT_KEYS, ""))
    raw_name_val = _text(row.get("raw_name") or row.get("raw_title") or name)
    prefix_tag_val = _text(row.get("prefix_tag") or row.get("bracket_tag") or "")
    if not prefix_tag_val and raw_name_val:
        tag_m = re.match(r"^(\[[^\]]+\])", raw_name_val)
        if tag_m:
            prefix_tag_val = tag_m.group(1)
    clean_name_val = _text(row.get("clean_name") or row.get("clean_title") or "")
    if not clean_name_val:
        clean_name_val = raw_name_val[len(prefix_tag_val):].strip() if prefix_tag_val else raw_name_val
    normalized_name_val = _text(row.get("normalized_name") or row.get("normalized_title") or "")
    if not normalized_name_val:
        normalized_name_val = normalize_course_name(clean_name_val or name)
    inferred_category_val = _text(row.get("inferred_category") or "")
    if not inferred_category_val and prefix_tag_val:
        if "自然" in prefix_tag_val:
            inferred_category_val = "自然、生命與科技領域"
        elif "藝術" in prefix_tag_val:
            inferred_category_val = "藝術與美感領域"
        elif "人文" in prefix_tag_val:
            inferred_category_val = "人文與文化思考領域"
        elif "公民" in prefix_tag_val:
            inferred_category_val = "公民素養與社會探索領域"
        elif "共同選修" in prefix_tag_val:
            inferred_category_val = "共同選修"
        elif "校共同" in prefix_tag_val or "校定必修" in prefix_tag_val:
            inferred_category_val = "校共同必修"
        elif "系必修" in prefix_tag_val or "系定必修" in prefix_tag_val:
            inferred_category_val = "專業必修"
        elif "系選修" in prefix_tag_val or "系定選修" in prefix_tag_val:
            inferred_category_val = "專業選修"

    resolved_name = normalized_name_val or clean_name_val or name
    group = _attempt_group(code, resolved_name, course_type)
    attempts: list[dict[str, Any]] = []

    semester_specs = (
        ("1", "sem1_credit", "sem1_score", "sem1_earned_credit"),
        ("2", "sem2_credit", "sem2_score", "sem2_earned_credit"),
    )
    has_semester_data = any(_has_value(row, credit_key) for _, credit_key, _, _ in semester_specs)
    if has_semester_data:
        for semester, credit_key, score_key, earned_key in semester_specs:
            if not _has_value(row, credit_key):
                continue
            credit = _number(row[credit_key])
            if credit is None:
                diagnostics.append(_diagnostic("INVALID_CREDITS", row_index, credit_key))
                continue
            term_info = _term_for(row, semester, default_term)
            if term_info is None:
                diagnostics.append(_diagnostic("MISSING_TERM", row_index, "term"))
                continue
            term, academic_year, resolved_semester = term_info
            score = row.get(score_key, _first(row, _STATUS_KEYS, ""))
            explicit_earned = row.get(earned_key, _MISSING)
            if explicit_earned is _MISSING:
                explicit_earned = _first(row, _EARNED_KEYS, _MISSING)
            status, earned, known = _status_and_earned(score, credit, explicit_earned)
            if not known:
                diagnostics.append(_diagnostic("UNKNOWN_STATUS", row_index, score_key))
            if status == "TRANSFERRED" and explicit_earned is _MISSING:
                diagnostics.append(_diagnostic("TRANSFER_EARNED_CREDIT_UNVERIFIED", row_index, "earned_credits"))
            attempts.append(
                {
                    "course_code": code,
                    "course_name": resolved_name,
                    "credits": credit,
                    "earned_credits": earned,
                    "status": status,
                    "term": term,
                    "academic_year": academic_year,
                    "semester": resolved_semester,
                    "grade": _text(score),
                    "attempt_group": group,
                    "department": department,
                    "course_type": course_type,
                    "raw_name": raw_name_val,
                    "prefix_tag": prefix_tag_val,
                    "clean_name": clean_name_val,
                    "normalized_name": normalized_name_val,
                    "inferred_category": inferred_category_val,
                }
            )
        return attempts, diagnostics

    credit_value = _first(row, _CREDIT_KEYS, _MISSING)
    if credit_value is _MISSING and bool(row.get("is_zero_credit", False)):
        # This is an explicit parser assertion, not a default for missing
        # data.  It preserves a genuine zero-credit course as zero.
        credit_value = 0.0
    if credit_value is _MISSING:
        diagnostics.append(_diagnostic("INVALID_CREDITS", row_index, "credits"))
        return [], diagnostics
    credit = _number(credit_value)
    if credit is None:
        diagnostics.append(_diagnostic("INVALID_CREDITS", row_index, "credits"))
        return [], diagnostics
    term_info = _term_for(row, None, default_term)
    if term_info is None:
        diagnostics.append(_diagnostic("MISSING_TERM", row_index, "term"))
        return [], diagnostics
    term, academic_year, semester = term_info
    score = _first(row, _STATUS_KEYS, "")
    # A printed withdrawal grade is decisive even if a legacy parser left
    # a generic UNKNOWN/COMPLETED status alongside it.
    for grade_key in ("grade", "score"):
        if is_withdrawn_grade(row.get(grade_key)):
            score = row[grade_key]
            break
    explicit_earned = _first(row, _EARNED_KEYS, _MISSING)
    status, earned, known = _status_and_earned(score, credit, explicit_earned)
    if not known:
        diagnostics.append(_diagnostic("UNKNOWN_STATUS", row_index, "status"))
    if status == "TRANSFERRED" and explicit_earned is _MISSING:
        diagnostics.append(_diagnostic("TRANSFER_EARNED_CREDIT_UNVERIFIED", row_index, "earned_credits"))
    attempts.append(
        {
            "course_code": code,
            "course_name": resolved_name,
            "credits": credit,
            "earned_credits": earned,
            "status": status,
            "term": term,
            "academic_year": academic_year,
            "semester": semester,
            "grade": _text(score),
            "attempt_group": group,
            "department": department,
            "course_type": course_type,
            "raw_name": raw_name_val,
            "prefix_tag": prefix_tag_val,
            "clean_name": clean_name_val,
            "normalized_name": normalized_name_val,
            "inferred_category": inferred_category_val,
        }
    )
    return attempts, diagnostics


def expand_legacy_course_rows(
    records: Iterable[Mapping[Any, Any]],
    *,
    default_term: str | None = None,
    source_kind: str = "parser",
) -> tuple[dict[str, Any], ...]:
    """Expand safe legacy rows to exact confirmation-boundary mappings.

    ``source_kind`` is accepted for callers that distinguish PDF and portal
    input, but it is intentionally not emitted into a course row.  Source
    provenance belongs to the surrounding confirmation state, not to a
    guessed course field.
    """

    del source_kind  # provenance must not cross the row allowlist
    if isinstance(records, (str, bytes, bytearray)):
        return ()
    try:
        iterator = iter(records)
    except TypeError:
        return ()
    rows: list[dict[str, Any]] = []
    for row in iterator:
        if not isinstance(row, Mapping):
            continue
        attempts, _ = _legacy_row_to_attempts(row, len(rows), default_term=default_term)
        rows.extend(attempts)
    return tuple({key: value for key, value in attempt.items() if key in COURSE_FIELD_ALLOWLIST} for attempt in rows)


def adapt_legacy_result(
    records: Iterable[Mapping[Any, Any]],
    *,
    default_term: str | None = None,
    source_kind: str = "parser",
) -> CourseInputAdapterResult:
    """Adapt parser rows and create a fresh, never-formal confirmation."""

    diagnostics: list[InputDiagnostic] = []
    if isinstance(records, (str, bytes, bytearray)):
        diagnostics.append(_diagnostic("INPUT_NOT_ROWS"))
        raw_rows: tuple[dict[str, Any], ...] = ()
    else:
        try:
            iterator = iter(records)
        except TypeError:
            iterator = iter(())
            diagnostics.append(_diagnostic("INPUT_NOT_ROWS"))
        raw_attempts: list[dict[str, Any]] = []
        for row_index, row in enumerate(iterator):
            if not isinstance(row, Mapping):
                diagnostics.append(_diagnostic("ROW_NOT_MAPPING", row_index))
                continue
            attempts, row_diagnostics = _legacy_row_to_attempts(row, row_index, default_term=default_term)
            raw_attempts.extend(attempts)
            diagnostics.extend(row_diagnostics)
        raw_rows = tuple(
            {key: value for key, value in attempt.items() if key in COURSE_FIELD_ALLOWLIST}
            for attempt in raw_attempts
        )

    normalization = normalize_course_rows(raw_rows)
    diagnostics.extend(normalization.diagnostics)
    state = ConfirmationState.PARSED if not diagnostics else ConfirmationState.UNCONFIRMED
    confirmation = CourseConfirmation(
        rows=normalization.rows,
        fingerprint=normalization.fingerprint,
        state=state,
        diagnostics=tuple(diagnostics),
    )
    safe_rows = tuple(row.as_dict() for row in normalization.rows)
    return CourseInputAdapterResult(
        rows=safe_rows,
        normalization=normalization,
        confirmation=confirmation,
        diagnostics=tuple(diagnostics),
    )


def adapt_legacy_rows(
    records: Iterable[Mapping[Any, Any]],
    *,
    default_term: str | None = None,
    source_kind: str = "parser",
) -> CourseConfirmation:
    """Return a fresh parsed/unconfirmed lifecycle object for legacy rows."""

    return adapt_legacy_result(records, default_term=default_term, source_kind=source_kind).confirmation


def parser_rows_to_confirmation(
    records: Iterable[Mapping[Any, Any]],
    *,
    default_term: str | None = None,
    source_kind: str = "parser",
) -> CourseConfirmation:
    """Explicit alias documenting the parser-to-confirmation boundary."""

    return adapt_legacy_rows(records, default_term=default_term, source_kind=source_kind)


legacy_rows_to_confirmation = parser_rows_to_confirmation
adapt_parser_rows = parser_rows_to_confirmation


__all__ = [
    "CourseInputAdapterResult",
    "expand_legacy_course_rows",
    "adapt_legacy_result",
    "adapt_legacy_rows",
    "parser_rows_to_confirmation",
    "legacy_rows_to_confirmation",
    "adapt_parser_rows",
]
