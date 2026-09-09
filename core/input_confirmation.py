"""Framework-independent transcript input confirmation and privacy boundary.

This module is deliberately small and conservative.  Parsers and UI code may
produce arbitrary mappings, but only the explicitly listed scalar course
fields cross this boundary.  A normalized row is immutable, and formal course
attempts are released only after the exact normalized content has been
confirmed by the caller.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from numbers import Integral, Real
from typing import Any

# These are the only fields accepted from an input row.  Identity/equivalency
# evidence belongs to the rule-resolution boundary and is intentionally not
# allowed to arrive as an opaque nested blob here.
COURSE_FIELD_ALLOWLIST = frozenset(
    {
        "course_code",
        "course_name",
        "credits",
        "earned_credits",
        "status",
        "term",
        "academic_year",
        "semester",
        "grade",
        "attempt_group",
        "department",
        "course_type",
        "raw_name",
        "prefix_tag",
        "clean_name",
        "normalized_name",
        "inferred_category",
    }
)

# Field names which are always rejected rather than merely ignored.  The set
# includes common English and Chinese spellings seen in uploads and scrapers.
FORBIDDEN_INPUT_FIELDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "cookie",
        "cookies",
        "authorization",
        "authorization_header",
        "auth",
        "token",
        "access_token",
        "refresh_token",
        "session",
        "session_cookie",
        "session_id",
        "session_token",
        "raw",
        "raw_blob",
        "raw_bytes",
        "raw_pdf",
        "raw_record",
        "pdf",
        "pdf_bytes",
        "transcript",
        "student_id",
        "student_name",
        "name_of_student",
        "學號",
        "姓名",
    }
)


class ConfirmationState(str, Enum):
    """Lifecycle state of parsed and user-confirmed course rows."""

    PARSED = "PARSED"
    CONFIRMED = "CONFIRMED"
    STALE = "STALE"
    UNCONFIRMED = "UNCONFIRMED"


class SafeErrorCode(str, Enum):
    """Stable, non-sensitive error categories suitable for user-facing UI."""

    INPUT_INVALID = "INPUT_INVALID"
    INPUT_UNREADABLE = "INPUT_UNREADABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    GENERAL_FAILURE = "GENERAL_FAILURE"


@dataclass(frozen=True)
class SafeError:
    """A fixed safe error response; exception text is never retained."""

    code: SafeErrorCode
    message: str


@dataclass(frozen=True)
class InputDiagnostic:
    """A safe diagnostic containing no caller-provided values."""

    code: str
    message: str
    row_index: int | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "row_index": self.row_index,
            "field": self.field,
        }


@dataclass(frozen=True)
class NormalizedCourseRow:
    """The immutable scalar course representation used by later analysis."""

    course_code: str
    course_name: str
    credits: float
    earned_credits: float
    status: str
    term: str
    academic_year: str = ""
    semester: str = ""
    grade: str = ""
    attempt_group: str = ""
    department: str = ""
    course_type: str = ""
    raw_name: str = ""
    prefix_tag: str = ""
    clean_name: str = ""
    normalized_name: str = ""
    inferred_category: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a plain safe copy for adapters and presentation code."""

        data = {
            "course_code": self.course_code,
            "course_name": self.course_name,
            "credits": self.credits,
            "earned_credits": self.earned_credits,
            "status": self.status,
            "term": self.term,
            "academic_year": self.academic_year,
            "semester": self.semester,
            "grade": self.grade,
            "attempt_group": self.attempt_group,
            "department": self.department,
            "course_type": self.course_type,
        }
        for field in ("raw_name", "prefix_tag", "clean_name", "normalized_name", "inferred_category"):
            val = getattr(self, field, "")
            if val:
                data[field] = val
        return data


@dataclass(frozen=True)
class NormalizationResult:
    """Safe normalized rows plus explicit reasons they cannot be trusted."""

    rows: tuple[NormalizedCourseRow, ...]
    diagnostics: tuple[InputDiagnostic, ...]
    fingerprint: str

    @property
    def valid(self) -> bool:
        return not self.diagnostics

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.as_dict() for row in self.rows],
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "fingerprint": self.fingerprint,
            "valid": self.valid,
        }


@dataclass(frozen=True)
class CourseConfirmation:
    """Immutable parsed/confirmed state for one edited set of course rows."""

    rows: tuple[NormalizedCourseRow, ...]
    fingerprint: str
    state: ConfirmationState
    diagnostics: tuple[InputDiagnostic, ...] = ()
    confirmed_fingerprint: str | None = None

    @property
    def valid(self) -> bool:
        return not self.diagnostics

    def as_dict(self) -> dict[str, Any]:
        # This is intentionally a safe projection.  It contains normalized
        # course data for the user's own analysis, but never the original row
        # mappings or parser payload.
        return {
            "rows": [row.as_dict() for row in self.rows],
            "fingerprint": self.fingerprint,
            "state": self.state.value,
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "confirmed_fingerprint": self.confirmed_fingerprint,
            "valid": self.valid,
        }


_STATUS_ALIASES = {
    "completed": "COMPLETED",
    "complete": "COMPLETED",
    "passed": "COMPLETED",
    "pass": "COMPLETED",
    "已修": "COMPLETED",
    "已完成": "COMPLETED",
    "修畢": "COMPLETED",
    "及格": "COMPLETED",
    "通過": "COMPLETED",
    "in_progress": "IN_PROGRESS",
    "in progress": "IN_PROGRESS",
    "enrolled": "IN_PROGRESS",
    "taking": "IN_PROGRESS",
    "修習中": "IN_PROGRESS",
    "修讀中": "IN_PROGRESS",
    "修課中": "IN_PROGRESS",
    "failed": "FAILED",
    "fail": "FAILED",
    "不及格": "FAILED",
    "不通過": "FAILED",
    "withdrawn": "WITHDRAWN",
    "退": "WITHDRAWN",
    "退選": "WITHDRAWN",
    "已退選": "WITHDRAWN",
    "停修": "WITHDRAWN",
    "撤選": "WITHDRAWN",
    "not_taken": "NOT_TAKEN",
    "not taken": "NOT_TAKEN",
    "未修": "NOT_TAKEN",
    "未選": "NOT_TAKEN",
    "waived": "WAIVED",
    "免修": "WAIVED",
    "transferred": "TRANSFERRED",
    "transfer_credit": "TRANSFERRED",
    "transfer": "TRANSFERRED",
    "抵免": "TRANSFERRED",
    "抵認": "TRANSFERRED",
}

_DIAGNOSTIC_MESSAGES = {
    "INPUT_NOT_ROWS": "課程資料格式無法辨識，請重新上傳或手動確認。",
    "ROW_NOT_MAPPING": "課程資料列格式無法辨識，請手動確認。",
    "UNKNOWN_FIELD": "資料含有未允許的欄位，請移除後重新確認。",
    "FORBIDDEN_FIELD": "資料含有不可匯入分析的敏感欄位。",
    "BLANK_COURSE": "課程代碼或課程名稱不可空白。",
    "INVALID_TEXT": "課程文字欄位格式無法辨識，請手動確認。",
    "INVALID_CREDITS": "學分必須是有限且不為負數的數值。",
    "MISSING_EARNED_CREDITS": "缺少實得學分語義，不能自動採信。",
    "INVALID_EARNED_CREDITS": "實得學分必須是有限且不為負數的數值。",
    "EARNED_CREDITS_EXCEED_CREDITS": "實得學分不可大於課程學分。",
    "INVALID_STATUS": "課程狀態無法辨識，不能自動採信。",
    "STATUS_EARNED_CREDITS_CONFLICT": "課程狀態與實得學分互相矛盾，需人工確認。",
    "MISSING_TERM": "缺少修課學期，不能建立可稽核的修課紀錄。",
    "DUPLICATE_EXACT_ROW": "發現完全重複的課程紀錄，不能自動採信。",
    "CONFIRMATION_FINGERPRINT_MISMATCH": "確認內容已變更，請重新檢視並確認。",
}

_TEXT_FIELDS = frozenset(
    {
        "course_code",
        "course_name",
        "term",
        "academic_year",
        "semester",
        "grade",
        "attempt_group",
        "department",
        "course_type",
        "raw_name",
        "prefix_tag",
        "clean_name",
        "normalized_name",
        "inferred_category",
    }
)


def _diagnostic(code: str, row_index: int | None = None, field: str | None = None) -> InputDiagnostic:
    return InputDiagnostic(
        code=code,
        message=_DIAGNOSTIC_MESSAGES.get(code, "資料無法安全採信，請人工確認。"),
        row_index=row_index,
        field=field,
    )


def _is_forbidden_key(key: object) -> bool:
    return isinstance(key, str) and key.casefold() in FORBIDDEN_INPUT_FIELDS


def _safe_scalar_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Integral) and not isinstance(value, bool):
        return str(value)
    return None


def _safe_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (Integral, Real, Decimal)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    # Keep ordinary credit values stable in JSON and comparisons without
    # retaining an arbitrary numeric subclass supplied by a caller.
    return int(number) if number.is_integer() else number


def _canonical_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().casefold()
    return _STATUS_ALIASES.get(cleaned)


def _fingerprint_payload(rows: Iterable[NormalizedCourseRow]) -> list[dict[str, Any]]:
    payload = [row.as_dict() for row in rows]
    return sorted(
        payload,
        key=lambda item: tuple(item.get(field, "") for field in sorted(COURSE_FIELD_ALLOWLIST)),
    )


def fingerprint_course_rows(rows: Iterable[NormalizedCourseRow]) -> str:
    """Hash normalized rows in canonical order, without inspecting raw maps."""

    payload = _fingerprint_payload(rows)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_one(row: Mapping[object, object], row_index: int) -> tuple[NormalizedCourseRow, list[InputDiagnostic]]:
    diagnostics: list[InputDiagnostic] = []

    # Inspect keys only.  Values belonging to unknown/restricted fields are
    # never read, represented, or copied.
    present: dict[str, object] = {}
    for key in row.keys():
        if not isinstance(key, str):
            diagnostics.append(_diagnostic("UNKNOWN_FIELD", row_index, "<non-text>"))
            continue
        key_folded = key.casefold()
        if key_folded in FORBIDDEN_INPUT_FIELDS:
            diagnostics.append(_diagnostic("FORBIDDEN_FIELD", row_index, "<forbidden>"))
            continue
        if key not in COURSE_FIELD_ALLOWLIST:
            diagnostics.append(_diagnostic("UNKNOWN_FIELD", row_index, "<unknown>"))
            continue
        present[key] = row[key]

    def text_field(field: str, *, required: bool = False) -> str:
        if field not in present:
            return ""
        value = _safe_scalar_text(present[field])
        if value is None:
            diagnostics.append(_diagnostic("INVALID_TEXT", row_index, field))
            return ""
        if required and not value:
            diagnostics.append(_diagnostic("BLANK_COURSE", row_index, field))
        return value

    course_code = text_field("course_code")
    course_name = text_field("course_name")
    if not course_code and not course_name:
        diagnostics.append(_diagnostic("BLANK_COURSE", row_index, "course"))

    credits = _safe_number(present.get("credits")) if "credits" in present else None
    if credits is None:
        diagnostics.append(_diagnostic("INVALID_CREDITS", row_index, "credits"))

    if "earned_credits" not in present or present["earned_credits"] is None:
        earned_credits = 0
        diagnostics.append(_diagnostic("MISSING_EARNED_CREDITS", row_index, "earned_credits"))
    else:
        earned_credits = _safe_number(present["earned_credits"])
        if earned_credits is None:
            earned_credits = 0
            diagnostics.append(_diagnostic("INVALID_EARNED_CREDITS", row_index, "earned_credits"))

    if credits is not None and earned_credits is not None and earned_credits > credits:
        diagnostics.append(_diagnostic("EARNED_CREDITS_EXCEED_CREDITS", row_index, "earned_credits"))

    status = _canonical_status(present.get("status")) if "status" in present else None
    if status is None:
        diagnostics.append(_diagnostic("INVALID_STATUS", row_index, "status"))
        status = "UNKNOWN"
    elif status in {"FAILED", "WITHDRAWN", "NOT_TAKEN", "WAIVED", "IN_PROGRESS"} and earned_credits:
        diagnostics.append(_diagnostic("STATUS_EARNED_CREDITS_CONFLICT", row_index, "earned_credits"))

    term = text_field("term")
    if not term:
        diagnostics.append(_diagnostic("MISSING_TERM", row_index, "term"))

    values = {
        field: text_field(field)
        for field in _TEXT_FIELDS
        if field not in {"course_code", "course_name", "term"}
    }
    normalized = NormalizedCourseRow(
        course_code=course_code,
        course_name=course_name,
        credits=credits if credits is not None else 0,
        earned_credits=earned_credits if earned_credits is not None else 0,
        status=status,
        term=term,
        academic_year=values["academic_year"],
        semester=values["semester"],
        grade=values["grade"],
        attempt_group=values["attempt_group"],
        department=values["department"],
        course_type=values["course_type"],
        raw_name=values["raw_name"],
        prefix_tag=values["prefix_tag"],
        clean_name=values["clean_name"],
        normalized_name=values["normalized_name"],
        inferred_category=values["inferred_category"],
    )
    return normalized, diagnostics


def normalize_course_rows(records: Iterable[Mapping[object, object]]) -> NormalizationResult:
    """Normalize parser/editor rows and report every unsafe condition.

    The function never copies unknown values.  Invalid rows are represented by
    safe placeholders only so that the caller can show diagnostics; callers
    must check ``valid`` before treating ``rows`` as formal attempts.
    """

    diagnostics: list[InputDiagnostic] = []
    normalized_rows: list[NormalizedCourseRow] = []

    if isinstance(records, (str, bytes, bytearray)):
        diagnostics.append(_diagnostic("INPUT_NOT_ROWS"))
    else:
        try:
            iterator = iter(records)
        except TypeError:
            iterator = iter(())
            diagnostics.append(_diagnostic("INPUT_NOT_ROWS"))

        for row_index, row in enumerate(iterator):
            if not isinstance(row, Mapping):
                diagnostics.append(_diagnostic("ROW_NOT_MAPPING", row_index))
                continue
            normalized, row_diagnostics = _normalize_one(row, row_index)
            normalized_rows.append(normalized)
            diagnostics.extend(row_diagnostics)

    seen: set[tuple[Any, ...]] = set()
    for row_index, row in enumerate(normalized_rows):
        identity = tuple(row.as_dict().get(field, "") for field in sorted(COURSE_FIELD_ALLOWLIST))
        if identity in seen:
            diagnostics.append(_diagnostic("DUPLICATE_EXACT_ROW", row_index, "course"))
        seen.add(identity)

    rows = tuple(normalized_rows)
    return NormalizationResult(rows=rows, diagnostics=tuple(diagnostics), fingerprint=fingerprint_course_rows(rows))


def start_confirmation(records: Iterable[Mapping[object, object]]) -> CourseConfirmation:
    """Create a parsed state; unsafe input starts as UNCONFIRMED."""

    result = normalize_course_rows(records)
    state = ConfirmationState.PARSED if result.valid else ConfirmationState.UNCONFIRMED
    return CourseConfirmation(
        rows=result.rows,
        fingerprint=result.fingerprint,
        state=state,
        diagnostics=result.diagnostics,
    )


def confirm_confirmation(confirmation: CourseConfirmation, confirmation_fingerprint: object) -> CourseConfirmation:
    """Confirm only when the caller explicitly confirms the current hash."""

    if (
        confirmation.valid
        and isinstance(confirmation_fingerprint, str)
        and confirmation_fingerprint == confirmation.fingerprint
    ):
        return CourseConfirmation(
            rows=confirmation.rows,
            fingerprint=confirmation.fingerprint,
            state=ConfirmationState.CONFIRMED,
            diagnostics=confirmation.diagnostics,
            confirmed_fingerprint=confirmation.fingerprint,
        )

    diagnostics = tuple(confirmation.diagnostics) + (
        _diagnostic("CONFIRMATION_FINGERPRINT_MISMATCH"),
    )
    return CourseConfirmation(
        rows=confirmation.rows,
        fingerprint=confirmation.fingerprint,
        state=ConfirmationState.UNCONFIRMED,
        diagnostics=diagnostics,
        confirmed_fingerprint=None,
    )


def edit_confirmation(
    confirmation: CourseConfirmation,
    records: Iterable[Mapping[object, object]],
) -> CourseConfirmation:
    """Replace edited rows and mark a previously confirmed hash stale."""

    result = normalize_course_rows(records)
    if not result.valid:
        state = ConfirmationState.UNCONFIRMED
        confirmed_fingerprint = None
    elif (
        confirmation.state is ConfirmationState.CONFIRMED
        and confirmation.confirmed_fingerprint == result.fingerprint
        and confirmation.fingerprint == result.fingerprint
    ):
        state = ConfirmationState.CONFIRMED
        confirmed_fingerprint = confirmation.confirmed_fingerprint
    elif confirmation.state is ConfirmationState.CONFIRMED:
        state = ConfirmationState.STALE
        confirmed_fingerprint = confirmation.confirmed_fingerprint
    else:
        state = ConfirmationState.PARSED
        confirmed_fingerprint = None

    return CourseConfirmation(
        rows=result.rows,
        fingerprint=result.fingerprint,
        state=state,
        diagnostics=result.diagnostics,
        confirmed_fingerprint=confirmed_fingerprint,
    )


def release_formal_attempts(
    confirmation: CourseConfirmation,
    confirmation_fingerprint: object,
) -> tuple[NormalizedCourseRow, ...]:
    """Release no rows unless state and both confirmation hashes agree."""

    if not (
        confirmation.state is ConfirmationState.CONFIRMED
        and confirmation.valid
        and isinstance(confirmation_fingerprint, str)
        and confirmation.fingerprint == confirmation_fingerprint
        and confirmation.confirmed_fingerprint == confirmation_fingerprint
    ):
        return ()
    return confirmation.rows


def mask_student_id(value: object) -> str:
    """Return a short masked identifier without coercing arbitrary objects."""

    if not isinstance(value, str):
        return "••••"
    value = value.strip()
    if not value:
        return "••••"
    if len(value) <= 4:
        return "•" * len(value)
    return f"{value[:2]}••••{value[-2:]}"


def mask_person_name(value: object) -> str:
    """Mask a name while retaining at most its first visible character."""

    if not isinstance(value, str):
        return "＊＊"
    value = value.strip()
    if not value:
        return "＊＊"
    return value[0] + ("＊" * (len(value) - 1)) if len(value) > 1 else "＊"


def classify_user_error(error: BaseException | object) -> SafeError:
    """Classify an exception without retaining or echoing its message."""

    if isinstance(error, UnicodeError):
        return SafeError(SafeErrorCode.INPUT_UNREADABLE, "檔案內容無法讀取，請確認格式後重試。")
    if isinstance(error, (ValueError, TypeError)):
        return SafeError(SafeErrorCode.INPUT_INVALID, "輸入資料格式不正確，請檢查後重試。")
    if isinstance(error, TimeoutError):
        return SafeError(SafeErrorCode.UPSTREAM_TIMEOUT, "校務系統回應逾時，請稍後重試。")
    if isinstance(error, PermissionError):
        return SafeError(SafeErrorCode.PERMISSION_DENIED, "校務系統拒絕此次操作，請重新登入後重試。")
    return SafeError(SafeErrorCode.GENERAL_FAILURE, "分析暫時無法完成，請稍後重試或改用手動匯入。")


__all__ = [
    "COURSE_FIELD_ALLOWLIST",
    "FORBIDDEN_INPUT_FIELDS",
    "ConfirmationState",
    "SafeErrorCode",
    "SafeError",
    "InputDiagnostic",
    "NormalizedCourseRow",
    "NormalizationResult",
    "CourseConfirmation",
    "normalize_course_rows",
    "fingerprint_course_rows",
    "start_confirmation",
    "confirm_confirmation",
    "edit_confirmation",
    "release_formal_attempts",
    "mask_student_id",
    "mask_person_name",
    "classify_user_error",
]
