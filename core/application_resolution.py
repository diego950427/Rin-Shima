"""Fail-closed application-event and formal-award resolution for UTaipei.

This module deliberately keeps application timing separate from curriculum
version resolution.  The notice fixtures are a read-only transcription of the
official evidence matrix in ``research/double_major_timing_rules.md``; missing
or conflicting source evidence remains ``UNKNOWN`` instead of being inferred
from neighbouring terms.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, time
from types import MappingProxyType
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

VERIFIED = "VERIFIED"
CONFLICTED = "CONFLICTED"
MISSING = "MISSING"

RULE_VERSION_UNKNOWN = "RULE_VERSION_UNKNOWN"
APPLICATION_EVENT_UNKNOWN = "APPLICATION_EVENT_UNKNOWN"
NOTICE_WINDOW_UNKNOWN = "NOTICE_WINDOW_UNKNOWN"
DEPARTMENT_DECISION_UNKNOWN = "DEPARTMENT_DECISION_UNKNOWN"
REGISTRATION_UNKNOWN = "REGISTRATION_UNKNOWN"
EFFECTIVE_TERM_UNKNOWN = "EFFECTIVE_TERM_UNKNOWN"
SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
FORMAL_AWARD_RECORD = "FORMAL_AWARD_RECORD"
FORMAL_QUALIFICATION_RECORD = "FORMAL_QUALIFICATION_RECORD"
APPLICATION_EVENT_RECORD = "APPLICATION_EVENT_RECORD"
GRANTED = "GRANTED"
ACTIVE = "ACTIVE"

_UNIVERSITY_NOTICE_URLS = {
    "111-1": "https://reg.utaipei.edu.tw/var/file/31/1031/attach/53/pta_84802_2981414_70686.pdf",
    "111-2": "https://edu.utaipei.edu.tw/p/406-1056-97741%2Cr1.php?Lang=zh-tw",
    "112-1": "https://edu.utaipei.edu.tw/p/406-1056-101280%2Cr1.php?Lang=zh-tw",
    "112-2": "https://reg.utaipei.edu.tw/var/file/31/1031/attach/74/pta_106767_6992864_04492.pdf",
    "113-1": "https://reg.utaipei.edu.tw/var/file/31/1031/attach/69/pta_112725_6380521_93841.pdf",
    "114-1": "https://reg.utaipei.edu.tw/var/file/31/1031/attach/42/pta_129168_9101728_61974.pdf",
    "114-2": "https://reg.utaipei.edu.tw/var/file/31/1031/attach/6/pta_141015_9521165_14743.pdf",
    "115-1": "https://reg.utaipei.edu.tw/var/file/31/1031/attach/60/pta_164885_1896902_75647.pdf",
}

_CS_DEPARTMENT_URL = "https://cs.utaipei.edu.tw/p/405-1081-116948%2Cc3219.php?Lang=zh-tw"


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return deepcopy(value)


def _university_notice(term: str, start: str, end: str, *, evidence_state: str = VERIFIED, evidence_id: str | None = None, assertions: list[dict[str, Any]] | None = None, eligibility_state: str = VERIFIED) -> dict[str, Any]:
    evidence_id = evidence_id or f"notice:university:{term}"
    return {
        "scope": "university",
        "application_term": term,
        "evidence_state": evidence_state,
        "evidence_id": evidence_id,
        "authority": "UTaipei 教務處／註冊組",
        "source_reference": _UNIVERSITY_NOTICE_URLS[term],
        "window": {"start": start, "end": end, "end_precision": "date"} if evidence_state == VERIFIED else None,
        "eligibility": {
            "evidence_state": eligibility_state,
            "evidence_id": f"eligibility:university:{term}",
            "authority": "UTaipei 教務處／註冊組",
            "source_reference": _UNIVERSITY_NOTICE_URLS[term],
            "assertions": assertions or [],
        },
    }


_NOTICE_FIXTURES = _freeze(
    {
        "university": {
            "111-1": _university_notice(
                "111-1",
                "111-03-14",
                "111-03-18",
                eligibility_state=CONFLICTED,
                assertions=[
                    {"evidence_id": "eligibility:111-1:year-level-a", "claim": "公告載明現為一下至三下"},
                    {"evidence_id": "eligibility:111-1:year-level-b", "claim": "現行辦法文字為二年級起至最後一年第一學期"},
                ],
            ),
            "111-2": _university_notice("111-2", "111-10-03", "111-10-07"),
            "112-1": _university_notice("112-1", "112-03-06", "112-03-10"),
            "112-2": _university_notice("112-2", "112-10-02", "112-10-06"),
            "113-1": _university_notice("113-1", "113-03-11", "113-03-15"),
            "113-2": {
                "scope": "university",
                "application_term": "113-2",
                "evidence_state": MISSING,
                "evidence_id": "notice:university:113-2:missing",
                "authority": "UTaipei 教務處／註冊組",
                "source_reference": "https://reg.utaipei.edu.tw/p/403-1031-511-1.php?Lang=zh-tw",
                "window": None,
                "eligibility": {"evidence_state": MISSING, "evidence_id": "eligibility:university:113-2:missing", "authority": "UTaipei 教務處／註冊組", "source_reference": "https://reg.utaipei.edu.tw/p/403-1031-511-1.php?Lang=zh-tw", "assertions": []},
            },
            "114-1": _university_notice("114-1", "114-03-10", "114-03-14"),
            "114-2": {
                "scope": "university",
                "application_term": "114-2",
                "evidence_state": CONFLICTED,
                "evidence_id": "notice:university:114-2:conflict",
                "authority": "UTaipei 教務處／註冊組",
                "source_reference": _UNIVERSITY_NOTICE_URLS["114-2"],
                "window": None,
                "assertions": [{"evidence_id": "notice:114-2:pdf", "claim": "PDF 內文窗口"}, {"evidence_id": "notice:114-2:page-title", "claim": "公告頁標題學期標示不一致"}],
                "eligibility": {"evidence_state": CONFLICTED, "evidence_id": "eligibility:university:114-2:conflict", "authority": "UTaipei 教務處／註冊組", "source_reference": _UNIVERSITY_NOTICE_URLS["114-2"], "assertions": [{"evidence_id": "notice:114-2:pdf", "claim": "PDF 內文窗口"}, {"evidence_id": "notice:114-2:page-title", "claim": "公告頁標題學期標示不一致"}]},
            },
            "115-1": _university_notice("115-1", "115-03-16", "115-03-20"),
            "115-2": {
                "scope": "university",
                "application_term": "115-2",
                "evidence_state": MISSING,
                "evidence_id": "notice:university:115-2:missing",
                "authority": "UTaipei 教務處／註冊組",
                "source_reference": "https://reg.utaipei.edu.tw/p/403-1031-511-1.php?Lang=zh-tw",
                "window": None,
                "eligibility": {"evidence_state": MISSING, "evidence_id": "eligibility:university:115-2:missing", "authority": "UTaipei 教務處／註冊組", "source_reference": "https://reg.utaipei.edu.tw/p/403-1031-511-1.php?Lang=zh-tw", "assertions": []},
            },
        },
        "department": {
            "cs": {
                "113-2": {
                    "scope": "department",
                    "application_term": "113-2",
                    "target_program": "cs",
                    "evidence_state": VERIFIED,
                    "evidence_id": "notice:department:cs:113-2",
                    "authority": "UTaipei 資訊科學系",
                    "source_reference": _CS_DEPARTMENT_URL,
                    "window": {"start": "113-09-30", "end": "113-10-08", "end_precision": "date", "note": "原 10/04 17:00 窗口因颱風延至 10/08；公告未核實延後截止時間。"},
                }
            }
        },
    }
)


def _normalize_term(value: Any) -> str | None:
    text = str(value or "").strip().replace("學年度", "").replace("學年", "")
    match = re.search(r"(?<!\d)(11[1-5])\s*[-_/－—]?\s*([12])(?!\d)", text)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def _normalize_program(value: Any) -> str | None:
    text = str(value or "").strip().replace("（", "(").replace("）", ")")
    compact = text.replace(" ", "").replace("　", "").lower()
    if compact in {"cs", "資科", "資科系", "資訊科學", "資訊科學系"} or "資科" in text or "資訊科學" in text:
        return "cs"
    if compact in {"math", "數學", "數學系"} or "數學" in text:
        return "math"
    if compact in {"apc", "物化", "物化系", "應用物理暨化學系"} or "物化" in text or "應用物理" in text or "應用化學" in text:
        return "apc"
    if compact in {"earth", "地生", "地球環境", "生命科學"} or "地生" in text or "地球環境" in text or "生命科學" in text:
        return "earth"
    return None


def _status_for_notice(record: dict[str, Any] | None) -> str:
    if not record or record.get("evidence_state") in {MISSING, CONFLICTED}:
        return UNKNOWN
    if (
        record.get("evidence_state") != VERIFIED
        or not record.get("window")
        or not _record_id(record)
        or not str(record.get("authority") or "").strip()
        or not _record_reference(record)
    ):
        return UNKNOWN
    return PASS


def _status_for_evidence_assertion(record: dict[str, Any] | None) -> str:
    """Validate a non-window evidence assertion without inventing a date."""

    if not isinstance(record, dict) or record.get("evidence_state") != VERIFIED:
        return UNKNOWN
    if not _record_id(record) or not str(record.get("authority") or "").strip() or not _record_reference(record):
        return UNKNOWN
    return PASS


def _evidence_summary(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {"evidence_ids": [], "provenance": []}
    evidence_ids = []
    for value in (record.get("evidence_id"), *(item.get("evidence_id") for item in record.get("assertions", []) if isinstance(item, dict))):
        if value and value not in evidence_ids:
            evidence_ids.append(value)
    provenance = []
    if record.get("evidence_id"):
        provenance.append({"evidence_id": record.get("evidence_id"), "scope": record.get("scope"), "authority": record.get("authority"), "source_reference": record.get("source_reference")})
    for item in record.get("assertions", []):
        if isinstance(item, dict) and item.get("evidence_id"):
            provenance.append({"evidence_id": item.get("evidence_id"), "scope": record.get("scope"), "authority": record.get("authority"), "source_reference": record.get("source_reference"), "claim": item.get("claim", "")})
    return {"evidence_ids": evidence_ids, "provenance": provenance}


def _notice_resolution(
    term: str | None,
    target_program: str | None,
    notice_records: Any = None,
    evidence_resolver: Any = None,
) -> dict[str, Any]:
    university_record = _thaw(_NOTICE_FIXTURES["university"].get(term)) if term else None
    department_record = None
    if term and target_program:
        department_record = _thaw(_NOTICE_FIXTURES["department"].get(target_program, {}).get(term))
    custom_university, custom_department = _resolve_custom_notice_records(notice_records, term, target_program, evidence_resolver)
    if custom_university is not None:
        university_record = custom_university
    if custom_department is not None:
        department_record = custom_department
    university_status = _status_for_notice(university_record)
    department_status = _status_for_notice(department_record) if department_record is not None else NOT_APPLICABLE
    eligibility_record = (university_record or {}).get("eligibility")
    eligibility_status = _status_for_evidence_assertion(eligibility_record)
    evidence = {"evidence_ids": [], "provenance": []}
    for record in (university_record, department_record):
        summary = _evidence_summary(record)
        for item in summary["evidence_ids"]:
            if item not in evidence["evidence_ids"]:
                evidence["evidence_ids"].append(item)
        evidence["provenance"].extend(summary["provenance"])
    status_values = [university_status, eligibility_status]
    if department_status != NOT_APPLICABLE:
        status_values.append(department_status)
    overall = FAIL if FAIL in status_values else UNKNOWN if UNKNOWN in status_values else PASS
    return {
        "application_term": term,
        "target_program": target_program,
        "status": overall,
        "university_window": {
            "scope": "university",
            "status": university_status,
            "state": university_record.get("evidence_state") if university_record else MISSING,
            "window": deepcopy((university_record or {}).get("window")),
            **_evidence_summary(university_record),
        },
        "department_window": {
            "scope": "department" if department_record is not None else None,
            "status": department_status,
            "state": department_record.get("evidence_state") if department_record else NOT_APPLICABLE,
            "window": deepcopy((department_record or {}).get("window")),
            **_evidence_summary(department_record),
        },
        "eligibility": {
            "status": eligibility_status,
            "state": eligibility_record.get("evidence_state") if eligibility_record else MISSING,
            **_evidence_summary(eligibility_record),
        },
        **evidence,
    }


def get_notice_resolution(application_term: Any, target_program: Any = None) -> dict[str, Any]:
    """Return read-only-by-copy notice evidence for one known application term."""

    term = _normalize_term(application_term)
    program = _normalize_program(target_program) if target_program is not None else None
    return _notice_resolution(term, program)


def _record_value(record: Any, *keys: str) -> Any:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _safe_text(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return ""


def _record_id(record: Any) -> str:
    """Return one stable evidence identifier for safe provenance output."""

    if not isinstance(record, dict):
        return ""
    for key in ("record_id", "evidence_id", "id", "reference_id"):
        value = _safe_text(record.get(key))
        if value:
            return value
    return ""


def _record_identity_matches(record: Any, evidence_id: Any) -> bool:
    requested = str(evidence_id or "").strip()
    # Treat the first canonical identifier as the record's identity.  If a
    # resolver returns conflicting aliases, accepting any matching alias would
    # make an ID-mismatch look valid at the trust boundary.
    return bool(requested) and _record_id(record) == requested


def _record_type(record: Any) -> str:
    return str(_record_value(record, "record_type", "evidence_type", "type") or "").strip().upper()


def _record_reference(record: Any) -> str:
    value = _record_value(record, "evidence_reference", "source_reference", "source_url", "evidence_url")
    if value in (None, ""):
        source = record.get("source") if isinstance(record, dict) else None
        if isinstance(source, dict):
            value = _record_value(source, "reference", "url", "id")
        elif isinstance(source, str):
            value = source
    return _safe_text(value)


def _provenance(record: Any, *, scope: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    return {
        "evidence_id": _record_id(record),
        "scope": scope or record.get("scope", ""),
        "authority": _safe_text(record.get("authority")),
        "source_reference": _record_reference(record),
        "application_term": _normalize_term(record.get("application_term")) or record.get("application_term", ""),
        "target_program": _normalize_program(record.get("target_program")) or _safe_text(record.get("target_program")),
    }


def _evidence_for_record(record: Any, *, scope: str | None = None) -> dict[str, Any]:
    evidence_id = _record_id(record)
    return {
        "evidence_ids": [evidence_id] if evidence_id else [],
        "provenance": [_provenance(record, scope=scope)] if evidence_id else [],
    }


def _safe_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    safe = {
        key: value[key]
        for key in ("start", "end", "end_precision", "note")
        if key in value and isinstance(value[key], (str, int, float, bool, type(None)))
    }
    return safe or None


def _safe_assertions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [
        {
            key: item[key]
            for key in ("evidence_id", "claim", "pages", "page")
            if key in item and isinstance(item[key], (str, int, float, bool, type(None)))
        }
        for item in value
        if isinstance(item, dict) and item.get("evidence_id")
    ]


def _parse_instant(value: Any, *, end_of_day: bool = False) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max if end_of_day else time.min)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        roc_match = re.match(r"^(11[1-5])[-/]([0-9]{1,2})[-/]([0-9]{1,2})(?:[ T](.*))?$", text)
        if roc_match:
            text = f"{int(roc_match.group(1)) + 1911:04d}-{int(roc_match.group(2)):02d}-{int(roc_match.group(3)):02d}"
            if roc_match.group(4):
                text += f"T{roc_match.group(4)}"
        text = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for pattern in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
            if end_of_day:
                parsed = datetime.combine(parsed.date(), time.max)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    if end_of_day and parsed.time() == time.min:
        parsed = datetime.combine(parsed.date(), time.max)
    return parsed


def _window_contains(window: Any, submitted_at: Any) -> bool | None:
    if not isinstance(window, dict):
        return None
    start = _parse_instant(window.get("start"))
    end = _parse_instant(window.get("end"), end_of_day=True)
    submitted = _parse_instant(submitted_at)
    if not start or not end or not submitted:
        return None
    return start <= submitted <= end


def _normalize_track(value: Any, program: str | None = None) -> str | None:
    text = str(value or "").strip().replace("（", "(").replace("）", ")")
    compact = text.replace(" ", "").replace("　", "").lower()
    if "化學" in text or compact in {"chemistry", "chemical"}:
        return "chemistry" if program in (None, "apc") else None
    if "物理" in text or compact in {"physics", "physical"}:
        return "physics" if program in (None, "apc") else None
    if "地球環境" in text or compact in {"earth_environment", "environment"}:
        return "earth_environment" if program in (None, "earth") else None
    if "生命科學" in text or compact in {"life_science", "life"}:
        return "life_science" if program in (None, "earth") else None
    return None


def _target_program_and_track(value: Any) -> tuple[str | None, str | None]:
    program = _normalize_program(value)
    track = _normalize_track(value, program)
    return program, track


def _record_matches_event(record: dict[str, Any], term: str | None, program: str | None, track: str | None) -> bool:
    # Application event matching is intentionally independent of any later
    # effective/registration term.  A record with only an effective term is
    # not evidence that it belongs to this application event.
    record_term = _normalize_term(_record_value(record, "application_term"))
    if record_term is None:
        record_year = str(_record_value(record, "application_year") or "").strip()
        record_semester = str(_record_value(record, "application_semester", "application_term_semester") or "").strip()
        if record_year and record_semester:
            record_term = _normalize_term(f"{record_year}-{record_semester}")
    if term and (not record_term or record_term != term):
        return False
    record_program = _normalize_program(_record_value(record, "target_program", "target_dept", "program"))
    if program and (not record_program or record_program != program):
        return False
    record_track = _normalize_track(_record_value(record, "target_track", "track"), record_program or program)
    if track:
        if not record_track or record_track != track:
            return False
    elif _record_value(record, "target_track", "track") not in (None, ""):
        return False
    return True


def _official_record(record: Any, *, term: str | None = None, program: str | None = None, track: str | None = None) -> bool:
    if not isinstance(record, dict) or record.get("evidence_state") != VERIFIED:
        return False
    if not _record_id(record) or not str(record.get("authority") or "").strip() or not _record_reference(record):
        return False
    return _record_matches_event(record, term, program, track)


def _resolve_server_record(
    evidence_id: Any,
    evidence_resolver: Any,
    *,
    record_types: set[str],
    term: str | None = None,
    program: str | None = None,
    track: str | None = None,
    student_id: str | None = None,
    subject_ref: str | None = None,
    require_subject: bool = False,
    require_application_term: bool = False,
    require_program: bool = False,
) -> dict[str, Any] | None:
    """Resolve one opaque ID at the server-owned evidence boundary.

    Callers never supply the record itself.  The resolver is allowed to
    return a record only for the exact requested ID; all dimensions are then
    checked against the current request before the record can affect a gate.
    """

    requested_id = str(evidence_id or "").strip() if isinstance(evidence_id, str) else ""
    if not requested_id or not callable(evidence_resolver):
        return None
    try:
        record = evidence_resolver(requested_id)
    except Exception:
        return None
    if not isinstance(record, dict) or not _record_identity_matches(record, requested_id):
        return None
    if _record_type(record) not in record_types:
        return None
    if not _official_record(record, term=term, program=program, track=track):
        return None
    if require_application_term:
        application_term = _normalize_term(_record_value(record, "application_term"))
        if application_term is None:
            record_year = _safe_text(_record_value(record, "application_year"))
            record_semester = _safe_text(_record_value(record, "application_semester", "application_term_semester"))
            application_term = _normalize_term(f"{record_year}-{record_semester}") if record_year and record_semester else None
        if application_term is None:
            return None
    if require_program and _normalize_program(_record_value(record, "target_program", "target_dept", "program")) is None:
        return None
    if require_subject:
        if subject_ref:
            record_subject_ref = _record_value(record, "subject_ref", "subject_token", "opaque_subject_ref")
            if not record_subject_ref or str(record_subject_ref).strip() != str(subject_ref).strip():
                return None
        else:
            subject_id = _record_value(record, "student_id", "subject_id", "student_number", "student_no")
            if not str(subject_id or "").strip():
                return None
            if not student_id or str(subject_id).strip() != str(student_id).strip():
                return None
    # Work with a private copy so a resolver cannot mutate a caller-owned
    # object after validation.  The raw record is never copied into output.
    try:
        return deepcopy(record)
    except Exception:
        return None


def _aggregate_status(statuses: list[str]) -> str:
    if FAIL in statuses:
        return FAIL
    if UNKNOWN in statuses:
        return UNKNOWN
    return PASS


def _gate(status: str, *, code: str = "", reason: str = "", value: Any = None, record: Any = None, scope: str | None = None) -> dict[str, Any]:
    result = {
        "status": status,
        "state": status,
        "code": code,
        "reason": reason,
        "value": value,
        **_evidence_for_record(record, scope=scope),
    }
    return result


def _safe_self_report(self_reported: Any) -> dict[str, Any]:
    if not isinstance(self_reported, dict):
        return {}
    allowed = {
        "application_term",
        "target_program",
        "target_track",
        "student_id",
        "subject_id",
        "subject_ref",
        "submitted_at",
        "application_event_evidence_id",
        "target_curriculum_id",
        "target_curriculum_version",
        "curriculum_revision",
        "admission_cohort",
        "current_year_level",
        "transfer_student",
        "normal_study_years",
    }
    return {
        key: value
        for key, value in self_reported.items()
        if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
    }


def _public_self_report(self_reported: Any) -> dict[str, Any]:
    """Project self-reported context without exposing student identity."""

    return {
        key: value
        for key, value in _safe_self_report(self_reported).items()
        if key not in {"student_id", "subject_id", "subject_ref"}
    }


def _resolve_custom_notice_records(
    notice_records: Any,
    term: str | None,
    program: str | None,
    evidence_resolver: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve optional notice IDs without trusting request-owned records.

    Built-in fixtures are selected separately and remain authoritative for the
    known matrix.  Legacy dictionaries/lists of dictionaries are deliberately
    ignored; only opaque IDs looked up through the injected resolver may add a
    custom notice.
    """

    if isinstance(notice_records, str):
        identifiers = [notice_records]
    elif isinstance(notice_records, (list, tuple)) and all(isinstance(item, str) for item in notice_records):
        identifiers = list(notice_records)
    else:
        identifiers = []
    university_candidates = []
    department_candidates = []
    for evidence_id in identifiers:
        record = _resolve_server_record(
            evidence_id,
            evidence_resolver,
            record_types={"UNIVERSITY_NOTICE_RECORD", "DEPARTMENT_NOTICE_RECORD"},
            term=term,
        )
        if record is None:
            continue
        scope = str(record.get("scope") or "").strip().lower()
        raw_program = _normalize_program(record.get("target_program"))
        record_type = _record_type(record)
        if scope == "university" and record_type == "UNIVERSITY_NOTICE_RECORD":
            if raw_program and program and raw_program != program:
                continue
            university_candidates.append(_safe_custom_notice(record, "university", term, None))
        elif scope == "department" and program and raw_program == program and record_type == "DEPARTMENT_NOTICE_RECORD":
            department_candidates.append(_safe_custom_notice(record, "department", term, raw_program))

    def combine(candidates):
        if not candidates:
            return None
        first = candidates[0]
        if any(item.get("evidence_state") in {MISSING, CONFLICTED} for item in candidates):
            first["evidence_state"] = CONFLICTED if any(item.get("evidence_state") == CONFLICTED for item in candidates) else MISSING
            first["window"] = None
            return first
        windows = {repr(item.get("window")) for item in candidates}
        if len(windows) > 1:
            first["evidence_state"] = CONFLICTED
            first["window"] = None
            first["assertions"] = _safe_assertions(
                [
                    {"evidence_id": item.get("evidence_id"), "claim": "同一 scope 有多個不一致窗口"}
                    for item in candidates
                ]
            )
        return first

    return combine(university_candidates), combine(department_candidates)


def _safe_custom_notice(record: dict[str, Any], scope: str, term: str | None, program: str | None) -> dict[str, Any]:
    """Keep only the small, auditable notice projection used by the planner."""

    eligibility = record.get("eligibility")
    if isinstance(eligibility, dict):
        safe_eligibility = {
            "evidence_state": eligibility.get("evidence_state"),
            "evidence_id": _record_id(eligibility),
            "authority": _safe_text(eligibility.get("authority")),
            "source_reference": _record_reference(eligibility),
            "assertions": _safe_assertions(eligibility.get("assertions", [])),
        }
    else:
        safe_eligibility = {"evidence_state": MISSING, "assertions": []}
    return {
        "scope": scope,
        "application_term": term,
        "target_program": program,
        "evidence_state": record.get("evidence_state") if record.get("evidence_state") in {VERIFIED, CONFLICTED, MISSING} else None,
        "evidence_id": _record_id(record),
        "authority": _safe_text(record.get("authority")),
        "source_reference": _record_reference(record),
        "window": _safe_window(record.get("window")),
        "eligibility": safe_eligibility,
        "assertions": _safe_assertions(record.get("assertions", [])),
    }


def _resolve_submission(notice: dict[str, Any], submitted_at: Any) -> dict[str, Any]:
    department_window = notice.get("department_window", {})
    university_window = notice.get("university_window", {})
    selected_window = department_window if department_window.get("status") != NOT_APPLICABLE else university_window
    if submitted_at in (None, ""):
        return _gate(
            UNKNOWN,
            code=SUBMISSION_UNKNOWN,
            reason="缺少 submitted_at；不能判斷是否在官方申請窗口內。",
        )
    if selected_window.get("status") != PASS:
        return _gate(
            UNKNOWN,
            code=NOTICE_WINDOW_UNKNOWN,
            reason="適用的官方申請窗口缺少可核實的單一日期範圍。",
        )
    inside = _window_contains(selected_window.get("window"), submitted_at)
    if inside is None:
        return _gate(
            UNKNOWN,
            code=SUBMISSION_UNKNOWN,
            reason="submitted_at 格式或官方窗口精度不足，不能安全判斷。",
        )
    if not inside:
        return _gate(
            FAIL,
            code="SUBMISSION_OUTSIDE_WINDOW",
            reason="submitted_at 位於已核實官方申請窗口之外。",
            value=submitted_at,
            record=None,
            scope=selected_window.get("scope"),
        )
    return _gate(PASS, value=submitted_at, reason="submitted_at 位於適用的已核實官方申請窗口內。")


def _term_ordinal(value: Any) -> int | None:
    term = _normalize_term(value)
    if not term:
        return None
    year, semester = term.split("-", 1)
    return int(year) * 2 + int(semester)


def _record_curriculum_revision(record: Mapping[str, Any]) -> str:
    value = _record_value(
        record,
        "curriculum_revision",
        "curriculum_revision_id",
        "curriculum_version_id",
        "target_curriculum_version_id",
        "curriculum_version",
        "target_curriculum_version",
        "curriculum_id",
    )
    return _safe_text(value)


def _effective_interval_matches(record: Mapping[str, Any], term: str | None, submitted_at: Any = None) -> bool:
    """Validate an explicit effective interval without inferring dates."""

    interval = record.get("effective_interval")
    if not isinstance(interval, dict):
        interval = {}
    start = _record_value(interval, "start", "from", "effective_start", "start_term", "from_term")
    end = _record_value(interval, "end", "to", "effective_end", "end_term", "to_term")
    if start in (None, ""):
        start = _record_value(record, "effective_start", "effective_from", "effective_from_term", "valid_from")
    if end in (None, ""):
        end = _record_value(record, "effective_end", "effective_to", "effective_to_term", "valid_to")
    start_term = _term_ordinal(start)
    end_term = _term_ordinal(end)
    target_term = _term_ordinal(term)
    if start_term is not None or end_term is not None:
        if start_term is None or end_term is None or target_term is None or start_term > end_term:
            return False
        return start_term <= target_term <= end_term
    effective_term = _normalize_term(_record_value(record, "effective_term", "applicable_term"))
    if effective_term:
        return bool(term and effective_term == term)
    # Date intervals need an explicit submitted_at to establish a date-level
    # claim.  A meeting/decision date by itself is never used as effective.
    if start in (None, "") or end in (None, ""):
        return False
    start_instant = _parse_instant(start)
    end_instant = _parse_instant(end, end_of_day=True)
    if not start_instant or not end_instant or start_instant > end_instant:
        return False
    if submitted_at in (None, ""):
        return True
    return _window_contains({"start": start, "end": end}, submitted_at) is True


def _resolve_application_event(
    evidence_id: Any,
    *,
    term: str | None,
    program: str | None,
    track: str | None,
    subject_ref: str | None,
    student_id: str | None,
    curriculum_revision: Any = None,
    submitted_at: Any = None,
    evidence_resolver: Any,
) -> dict[str, Any]:
    if not term or not program or not (subject_ref or student_id):
        return _gate(UNKNOWN, code=APPLICATION_EVENT_UNKNOWN, reason="缺少申請事件的 subject、學期或目標系所 binding。")
    record = _resolve_server_record(
        evidence_id,
        evidence_resolver,
        record_types={
            APPLICATION_EVENT_RECORD,
            "DOUBLE_MAJOR_APPLICATION_RECORD",
            "DOUBLE_MAJOR_APPLICATION_EVENT",
            "APPLICATION_RECORD",
        },
        term=term,
        program=program,
        track=track,
        student_id=student_id if not subject_ref else None,
        subject_ref=subject_ref,
        require_subject=True,
        require_application_term=True,
        require_program=True,
    )
    if record is None:
        return _gate(UNKNOWN, code=APPLICATION_EVENT_UNKNOWN, reason="缺少與目前 subject、申請學期、系所及組別相符的正式申請紀錄。")
    revision = _record_curriculum_revision(record)
    expected_revision = _safe_text(curriculum_revision)
    if not revision:
        return _gate(UNKNOWN, code=APPLICATION_EVENT_UNKNOWN, reason="正式申請紀錄缺少 curriculum revision；rule_version 顯示字串不能補足適用性。")
    if expected_revision and expected_revision not in {revision, _normalize_term(revision)}:
        return _gate(UNKNOWN, code=APPLICATION_EVENT_UNKNOWN, value=revision, reason="正式申請紀錄的 curriculum revision 與目前請求不一致。")
    if not _effective_interval_matches(record, term, submitted_at):
        return _gate(UNKNOWN, code=APPLICATION_EVENT_UNKNOWN, value=revision, reason="正式申請紀錄缺少或不符合明示的 effective interval。")
    result = _gate(PASS, code="APPLICATION_EVENT_VERIFIED", value=revision, record=record, scope="application_event", reason="正式申請紀錄已核實 subject、學期、系所、修訂及生效區間。")
    result.update({"curriculum_revision": revision, "record_type": _record_type(record)})
    if isinstance(record.get("effective_interval"), dict):
        result["effective_interval"] = _safe_window(record.get("effective_interval"))
    return result


def _resolve_rule_version(
    evidence_id: Any,
    term: str | None,
    program: str | None,
    track: str | None,
    evidence_resolver: Any,
    *,
    curriculum_revision: Any = None,
    submitted_at: Any = None,
) -> dict[str, Any]:
    if not term or not program:
        return _gate(UNKNOWN, code=RULE_VERSION_UNKNOWN, reason="缺少申請學期或目標系所；不能核對 rule version applicability 的適用範圍。")
    record = _resolve_server_record(
        evidence_id,
        evidence_resolver,
        record_types={"RULE_APPLICABILITY_RECORD"},
        term=term,
        program=program,
        track=track,
        require_program=True,
    )
    if record is None:
        return _gate(UNKNOWN, code=RULE_VERSION_UNKNOWN, reason="缺少或未核實的官方 rule version applicability evidence。")
    rule_version = _record_value(record, "rule_version", "version")
    if not isinstance(rule_version, (str, int, float, bool)) or not str(rule_version or "").strip():
        return _gate(UNKNOWN, code=RULE_VERSION_UNKNOWN, reason="官方 rule applicability record 缺少 rule_version。")
    revision = _record_curriculum_revision(record)
    if not revision:
        return _gate(UNKNOWN, code=RULE_VERSION_UNKNOWN, value=rule_version, reason="rule_version 顯示字串不能單獨證明 curriculum applicability；缺少 revision。")
    expected_revision = _safe_text(curriculum_revision)
    if expected_revision and expected_revision not in {revision, _normalize_term(revision)}:
        return _gate(UNKNOWN, code=RULE_VERSION_UNKNOWN, value=rule_version, reason="官方 rule applicability 的 curriculum revision 與請求不一致。")
    if not _effective_interval_matches(record, term, submitted_at):
        return _gate(UNKNOWN, code=RULE_VERSION_UNKNOWN, value=rule_version, reason="官方 rule applicability 缺少或不符合明示的 effective interval。")
    result = _gate(PASS, value=rule_version, record=record, scope="rule_applicability")
    result.update({"curriculum_revision": revision})
    return result


def _resolve_department_decision(
    evidence_id: Any,
    term: str | None,
    program: str | None,
    track: str | None,
    student_id: str | None,
    evidence_resolver: Any,
    *,
    subject_ref: str | None = None,
) -> dict[str, Any]:
    if not term or not program:
        return _gate(UNKNOWN, code=DEPARTMENT_DECISION_UNKNOWN, reason="缺少申請學期或目標系所；不能核對系所正式核准紀錄。")
    record = _resolve_server_record(
        evidence_id,
        evidence_resolver,
        record_types={"DEPARTMENT_DECISION_RECORD"},
        term=term,
        program=program,
        track=track,
        student_id=student_id,
        subject_ref=subject_ref,
        require_subject=True,
        require_program=True,
    )
    if record is None:
        return _gate(UNKNOWN, code=DEPARTMENT_DECISION_UNKNOWN, reason="缺少系所正式核准紀錄；self-reported approval 不具官方效力。")
    decision = str(_record_value(record, "decision", "department_decision", "status") or "").strip().upper()
    if decision in {"REJECTED", "DENIED", "NOT_APPROVED"}:
        return _gate(FAIL, code="DEPARTMENT_REJECTED", value=decision, record=record, scope="department_decision", reason="系所正式紀錄拒絕雙主修申請。")
    if decision != "APPROVED":
        return _gate(UNKNOWN, code=DEPARTMENT_DECISION_UNKNOWN, value=decision, record=record, scope="department_decision", reason="系所紀錄沒有明確 APPROVED 決定。")
    return _gate(PASS, code="DEPARTMENT_APPROVED", value=decision, record=record, scope="department_decision", reason="系所正式紀錄已核准。")


def _resolve_registration(
    evidence_id: Any,
    term: str | None,
    program: str | None,
    track: str | None,
    student_id: str | None,
    evidence_resolver: Any,
    *,
    subject_ref: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not term or not program:
        unknown = _gate(UNKNOWN, code=REGISTRATION_UNKNOWN, reason="缺少申請學期或目標系所；不能核對教務處正式登錄紀錄。")
        return unknown, _gate(UNKNOWN, code=REGISTRATION_UNKNOWN, reason="缺少申請事件或目標系所，不能進入 ACTIVE。")
    record = _resolve_server_record(
        evidence_id,
        evidence_resolver,
        record_types={"REGISTRAR_REGISTRATION_RECORD"},
        term=term,
        program=program,
        track=track,
        student_id=student_id,
        subject_ref=subject_ref,
        require_subject=True,
        require_program=True,
    )
    if record is None:
        unknown = _gate(UNKNOWN, code=REGISTRATION_UNKNOWN, reason="缺少教務處正式登錄紀錄；self-reported registration 不具官方效力。")
        return unknown, _gate(UNKNOWN, code=REGISTRATION_UNKNOWN, reason="缺少 registrar registration，不能進入 ACTIVE。")
    registration_state = str(_record_value(record, "registration_status", "status", "decision") or "").strip().upper()
    if registration_state in {"REJECTED", "DENIED", "NOT_REGISTERED"}:
        failed = _gate(FAIL, code="REGISTRATION_REJECTED", value=registration_state, record=record, scope="registrar_registration", reason="教務處正式紀錄未登錄雙主修。")
        return failed, failed.copy()
    if registration_state != "REGISTERED":
        unknown = _gate(UNKNOWN, code=REGISTRATION_UNKNOWN, value=registration_state, record=record, scope="registrar_registration", reason="教務處紀錄沒有明確 REGISTERED 狀態。")
        return unknown, _gate(UNKNOWN, code=REGISTRATION_UNKNOWN, reason="未確認 REGISTERED，不能進入 ACTIVE。")
    registration = _gate(PASS, code="REGISTRAR_REGISTERED", value=registration_state, record=record, scope="registrar_registration", reason="教務處正式紀錄已 REGISTERED。")
    effective_term = _normalize_term(_record_value(record, "effective_term", "registration_effective_term"))
    if not effective_term:
        return registration, _gate(UNKNOWN, code=EFFECTIVE_TERM_UNKNOWN, value=effective_term, record=record, scope="registrar_registration", reason="REGISTERED 紀錄缺少 effective_term；不能判定 ACTIVE。")
    active = _gate(PASS, code="ACTIVE", value=effective_term, record=record, scope="registrar_registration", reason="REGISTERED 且有官方 effective_term，可進入 ACTIVE。")
    active["state"] = ACTIVE
    active["effective_term"] = effective_term
    return registration, active


def resolve_application_case(
    self_reported: Any,
    *,
    notice_records: Any = None,
    rule_applicability: Any = None,
    department_decision: Any = None,
    registrar_registration: Any = None,
    formal_qualification: Any = None,
    submitted_at: Any = None,
    application_event_evidence_id: Any = None,
    curriculum_revision: Any = None,
    subject_ref: Any = None,
    as_of: Any = None,
    evidence_resolver: Any = None,
) -> dict[str, Any]:
    """Resolve an application timeline using official evidence only.

    ``self_reported`` supplies scalar context and cannot upgrade any gate.
    Evidence arguments are opaque record IDs; the server-owned resolver is the
    only path by which official records can enter this calculation.
    """

    # Normalize the request projection before interpreting any dimensions so
    # nested caller-owned blobs cannot be coerced through ``str(...)`` into a
    # program, track, or event value.
    self_reported = _safe_self_report(self_reported)
    term = _normalize_term(self_reported.get("application_term"))
    target_program, target_track = _target_program_and_track(self_reported.get("target_program"))
    explicit_track = _normalize_track(self_reported.get("target_track"), target_program)
    if explicit_track:
        target_track = explicit_track
    student_id = _record_value(self_reported, "student_id", "subject_id")
    student_id = str(student_id).strip() if student_id not in (None, "") else None
    resolved_subject_ref = _safe_text(subject_ref) or _safe_text(self_reported.get("subject_ref")) or None
    resolved_submitted_at = submitted_at if submitted_at not in (None, "") else self_reported.get("submitted_at")
    resolved_event_id = application_event_evidence_id if application_event_evidence_id not in (None, "") else self_reported.get("application_event_evidence_id")
    resolved_revision = curriculum_revision if curriculum_revision not in (None, "") else _record_value(
        self_reported,
        "curriculum_revision",
        "target_curriculum_version",
        "target_curriculum_id",
    )
    notice = _notice_resolution(term, target_program, notice_records, evidence_resolver)
    submission = _resolve_submission(notice, resolved_submitted_at)
    rule_gate = _resolve_rule_version(
        rule_applicability,
        term,
        target_program,
        target_track,
        evidence_resolver,
        curriculum_revision=resolved_revision,
        submitted_at=resolved_submitted_at,
    )
    application_event = (
        _resolve_application_event(
            resolved_event_id,
            term=term,
            program=target_program,
            track=target_track,
            subject_ref=resolved_subject_ref,
            student_id=student_id,
            curriculum_revision=resolved_revision,
            submitted_at=resolved_submitted_at,
            evidence_resolver=evidence_resolver,
        )
        if resolved_event_id
        else _gate(NOT_APPLICABLE, reason="未提供獨立 application event evidence；不以 rule_version 字串代替。")
    )
    department_gate = _resolve_department_decision(
        department_decision,
        term,
        target_program,
        target_track,
        student_id,
        evidence_resolver,
        subject_ref=resolved_subject_ref,
    )
    registration, activity = _resolve_registration(
        registrar_registration,
        term,
        target_program,
        target_track,
        student_id,
        evidence_resolver,
        subject_ref=resolved_subject_ref,
    )
    qualification_gate = resolve_formal_qualification(
        formal_qualification,
        evidence_resolver=evidence_resolver,
        subject_ref=resolved_subject_ref,
        student_id=student_id if not resolved_subject_ref else None,
        application_term=term,
        target_program=target_program,
        target_track=target_track,
    )

    required_gates = [
        notice.get("university_window", {}).get("status", UNKNOWN),
        notice.get("eligibility", {}).get("status", UNKNOWN),
        notice.get("department_window", {}).get("status", NOT_APPLICABLE),
        submission.get("status", UNKNOWN),
        rule_gate.get("status", UNKNOWN),
        department_gate.get("status", UNKNOWN),
        registration.get("status", UNKNOWN),
        activity.get("status", UNKNOWN),
    ]
    if resolved_event_id:
        required_gates.append(application_event.get("status", UNKNOWN))
    overall = _aggregate_status(required_gates)
    blockers = []
    for name, gate in (
        ("university_window", notice.get("university_window", {})),
        ("eligibility", notice.get("eligibility", {})),
        ("department_window", notice.get("department_window", {})),
        ("submission", submission),
        ("rule_version", rule_gate),
        ("department_decision", department_gate),
        ("registration", registration),
        ("activity", activity),
        ("application_event", application_event),
        ("formal_qualification", qualification_gate),
    ):
        if gate.get("status") in {FAIL, UNKNOWN}:
            blockers.append({"code": gate.get("code") or gate.get("status"), "dimension": name, "reason": gate.get("reason", "")})

    evidence_ids = []
    provenance = []
    for item in (
        notice.get("provenance", []),
        rule_gate.get("provenance", []),
        department_gate.get("provenance", []),
        registration.get("provenance", []),
        activity.get("provenance", []),
        application_event.get("provenance", []),
        qualification_gate.get("provenance", []),
    ):
        for record in item:
            evidence_id = record.get("evidence_id")
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            if record not in provenance:
                provenance.append(record)

    return {
        "status": overall,
        "state": overall,
        "can_pass": overall == PASS,
        "graduation_ready": overall == PASS,
        "application_term": term,
        "target_program": target_program,
        "target_track": target_track,
        "as_of": as_of if isinstance(as_of, (str, int, float, bool, type(None))) else None,
        "self_reported": _public_self_report(self_reported),
        "notice": notice,
        "university_window": notice.get("university_window"),
        "department_window": notice.get("department_window"),
        "submission": submission,
        "submitted_at": resolved_submitted_at if isinstance(resolved_submitted_at, (str, int, float, bool, type(None))) else None,
        "application_event": application_event,
        "application_event_evidence_id": _safe_text(resolved_event_id) or None,
        "rule_version": rule_gate,
        "rule_applicability": rule_gate,
        "department_decision": department_gate,
        "department": department_gate,
        "registrar_registration": registration,
        "registration": registration,
        "activity": activity,
        "formal_qualification": qualification_gate,
        "formal_award": resolve_formal_award(None, evidence_resolver=evidence_resolver),
        "gates": {
            "university_window": notice.get("university_window", {}).get("status", UNKNOWN),
            "eligibility": notice.get("eligibility", {}).get("status", UNKNOWN),
            "department_window": notice.get("department_window", {}).get("status", NOT_APPLICABLE),
            "submission": submission.get("status", UNKNOWN),
            "rule_version": rule_gate.get("status", UNKNOWN),
            "department_decision": department_gate.get("status", UNKNOWN),
            "registration": registration.get("status", UNKNOWN),
            "activity": activity.get("status", UNKNOWN),
            "application_event": application_event.get("status", NOT_APPLICABLE),
            "formal_qualification": qualification_gate.get("status", UNKNOWN),
        },
        "blockers": blockers,
        "evidence_ids": evidence_ids,
        "provenance": provenance,
    }


def resolve_formal_qualification(
    opaque_evidence_id: Any = None,
    *,
    evidence_resolver: Any = None,
    subject_ref: Any = None,
    student_id: Any = None,
    application_term: Any = None,
    target_program: Any = None,
    target_track: Any = None,
) -> dict[str, Any]:
    """Resolve the official double-major qualification independently.

    This is intentionally separate from application timing and coursework.
    A self-report can describe what the student believes, but only a trusted
    server record bound to the opaque subject reference can make this gate
    PASS.  The legacy ``student_id`` argument is retained for compatibility;
    callers using the production service should provide ``subject_ref``.
    """

    requested_id = _safe_text(opaque_evidence_id) if isinstance(opaque_evidence_id, str) else ""
    term = _normalize_term(application_term)
    program = _normalize_program(target_program) if target_program is not None else None
    track = _normalize_track(target_track, program) if target_track is not None else None
    ref = _safe_text(subject_ref) or None
    legacy_student = _safe_text(student_id) or None
    if not requested_id or not program or not (ref or legacy_student):
        return _gate(
            UNKNOWN,
            code="FORMAL_QUALIFICATION_UNKNOWN",
            reason="缺少可核實的正式雙主修資格證據或 opaque subject binding。",
        ) | {"qualification_state": UNKNOWN, "is_official": False}
    record = _resolve_server_record(
        requested_id,
        evidence_resolver,
        record_types={FORMAL_QUALIFICATION_RECORD, "DOUBLE_MAJOR_QUALIFICATION_RECORD", "QUALIFICATION_RECORD"},
        term=term,
        program=program,
        track=track,
        student_id=legacy_student if not ref else None,
        subject_ref=ref,
        require_subject=True,
        # A trusted subject-bound current qualification may be the only
        # surviving evidence after an already-approved application.  When a
        # request supplies a term, still require exact event binding; when it
        # does not, do not manufacture a historical submission date.
        require_application_term=bool(term),
        require_program=True,
    )
    if record is None:
        return _gate(
            UNKNOWN,
            code="FORMAL_QUALIFICATION_UNKNOWN",
            reason="沒有與目前學期、目標系所及 subject binding 相符的官方資格紀錄。",
        ) | {"qualification_state": UNKNOWN, "is_official": False}
    state = str(_record_value(record, "qualification_status", "qualification_state", "status", "decision") or "").strip().upper()
    if state in {"ACTIVE", "QUALIFIED", "APPROVED", "GRANTED", "ELIGIBLE", "REGISTERED"}:
        result = _gate(
            PASS,
            code="FORMAL_QUALIFICATION_VERIFIED",
            value=state,
            reason="官方正式雙主修資格紀錄已核實。",
            record=record,
            scope="formal_qualification",
        )
        result.update({"qualification_state": state, "is_official": True, "record_type": _record_type(record)})
        return result
    if state in {"REJECTED", "DENIED", "NOT_QUALIFIED", "NOT_ELIGIBLE"}:
        result = _gate(
            FAIL,
            code="FORMAL_QUALIFICATION_NOT_GRANTED",
            value=state,
            reason="官方正式紀錄顯示未取得雙主修資格。",
            record=record,
            scope="formal_qualification",
        )
        result.update({"qualification_state": state, "is_official": True, "record_type": _record_type(record)})
        return result
    return _gate(
        UNKNOWN,
        code="FORMAL_QUALIFICATION_UNKNOWN",
        value=state or None,
        reason="官方資格紀錄沒有明確的授權狀態。",
        record=record,
        scope="formal_qualification",
    ) | {"qualification_state": UNKNOWN, "is_official": False}


def resolve_formal_award(
    opaque_evidence_id: Any = None,
    *,
    evidence_resolver: Any = None,
    student_id: Any = None,
    subject_ref: Any = None,
    award_kind: Any = None,
    target_program: Any = None,
    target_track: Any = None,
    evidence_record: Any = None,
) -> dict[str, Any]:
    """Resolve an independent formal award from a server-owned record ID.

    A raw dictionary is intentionally rejected, including dictionaries that
    happen to look like a verified official record.  This keeps an award
    decision independent from planner/user-owned data.
    """

    # ``evidence_record`` is retained only as a safe legacy spelling.  A raw
    # dict supplied through it follows the same rejection path as any other
    # caller-owned record; a scalar ID may still be resolved server-side.
    if evidence_record is not None:
        if opaque_evidence_id not in (None, ""):
            return _gate(UNKNOWN, code="FORMAL_AWARD_UNKNOWN", reason="同時提供兩個授予證據輸入；不能安全判定。") | {"award_state": UNKNOWN, "is_official": False}
        opaque_evidence_id = evidence_record
    requested_id = str(opaque_evidence_id or "").strip() if isinstance(opaque_evidence_id, str) else ""
    expected_program = _normalize_program(target_program) if target_program is not None else None
    expected_track = _normalize_track(target_track, expected_program) if target_track is not None else None
    expected_student = str(student_id).strip() if student_id not in (None, "") else None
    expected_subject_ref = _safe_text(subject_ref) or None
    record = _resolve_server_record(
        requested_id,
        evidence_resolver,
        record_types={FORMAL_AWARD_RECORD},
        program=expected_program,
        track=expected_track,
        student_id=expected_student,
        subject_ref=expected_subject_ref,
        require_subject=True,
        require_application_term=True,
        require_program=True,
    )
    award_state = str(_record_value(record, "award_state", "award_status", "decision", "status") or "").strip().upper()
    if record is None:
        return _gate(UNKNOWN, code="FORMAL_AWARD_UNKNOWN", reason="沒有可核實的官方 FORMAL_AWARD_RECORD；規劃結果不能代替正式授予紀錄。") | {"award_state": UNKNOWN, "is_official": False}
    if award_kind not in (None, ""):
        record_kind = str(_record_value(record, "award_kind", "kind") or "").strip()
        if not record_kind or record_kind != str(award_kind).strip():
            return _gate(UNKNOWN, code="FORMAL_AWARD_UNKNOWN", value=award_state or None, reason="正式授予紀錄的 award_kind 與請求不一致。", record=record, scope="formal_award") | {"award_state": UNKNOWN, "is_official": False}
    if award_state == GRANTED:
        result = _gate(PASS, code="FORMAL_AWARD_GRANTED", value=GRANTED, reason="官方正式授予紀錄已核實。", record=record, scope="formal_award")
        result.update({"award_state": GRANTED, "is_official": True, "record_type": FORMAL_AWARD_RECORD})
        return result
    if award_state in {"REVOKED", "DENIED", "NOT_GRANTED"}:
        result = _gate(FAIL, code="FORMAL_AWARD_NOT_GRANTED", value=award_state, reason="官方正式紀錄顯示未授予或已撤銷。", record=record, scope="formal_award")
        result.update({"award_state": award_state, "is_official": True, "record_type": FORMAL_AWARD_RECORD})
        return result
    return _gate(UNKNOWN, code="FORMAL_AWARD_UNKNOWN", value=award_state or None, reason="官方正式紀錄沒有明確授予狀態。", record=record, scope="formal_award") | {"award_state": UNKNOWN, "is_official": False}


def _resolve_minor_record_gate(
    evidence_id: Any,
    *,
    evidence_resolver: Any,
    record_types: set[str],
    term: str | None,
    program: str | None,
    track: str | None,
    subject_ref: str | None,
    state_keys: tuple[str, ...],
    accepted_states: set[str],
    rejected_states: set[str],
    pass_code: str,
    unknown_code: str,
    reject_code: str,
    scope: str,
    pass_reason: str,
    missing_reason: str,
    reject_reason: str,
) -> dict[str, Any]:
    """Resolve an opaque minor evidence ID with the existing trust boundary.

    Minor approvals and registrations are not double-major records.  They do
    share the same server-owned ID, event, programme, authority and source
    checks, but deliberately do not accept request-owned record dictionaries.
    Every minor application gate is bound to the opaque subject reference from
    the current request.  A department or registrar record that has no subject
    binding (or belongs to another subject) cannot authorize this request.
    """

    resolved_subject_ref = _safe_text(subject_ref) or None
    if not resolved_subject_ref or not term or not program:
        return _gate(UNKNOWN, code=unknown_code, reason=missing_reason) | {
            "qualification_state": UNKNOWN,
            "is_official": False,
        }
    record = _resolve_server_record(
        evidence_id,
        evidence_resolver,
        record_types=record_types,
        term=term,
        program=program,
        track=track,
        subject_ref=resolved_subject_ref,
        require_subject=True,
        require_application_term=True,
        require_program=True,
    )
    if record is None:
        return _gate(UNKNOWN, code=unknown_code, reason=missing_reason) | {
            "qualification_state": UNKNOWN,
            "is_official": False,
        }
    value = str(_record_value(record, *state_keys) or "").strip().upper()
    record_type = _record_type(record)
    if value in accepted_states:
        result = _gate(PASS, code=pass_code, value=value, record=record, scope=scope, reason=pass_reason)
        result.update({"qualification_state": value, "is_official": True, "record_type": record_type})
        return result
    if value in rejected_states:
        result = _gate(FAIL, code=reject_code, value=value, record=record, scope=scope, reason=reject_reason)
        result.update({"qualification_state": value, "is_official": True, "record_type": record_type})
        return result
    result = _gate(UNKNOWN, code=unknown_code, value=value or None, record=record, scope=scope, reason=missing_reason)
    result.update({"qualification_state": UNKNOWN, "is_official": False, "record_type": record_type})
    return result


def resolve_minor_application_case(
    self_reported: Any,
    *,
    department_approval: Any = None,
    registrar_registration: Any = None,
    formal_qualification: Any = None,
    evidence_resolver: Any = None,
) -> dict[str, Any]:
    """Resolve the official qualification chain for a minor target.

    ``self_reported`` is context only.  Every official gate still requires an
    opaque ID resolved by ``evidence_resolver``; a claim such as ``已申請`` can
    therefore never become a PASS by itself.
    """

    context = _safe_self_report(self_reported)
    term = _normalize_term(context.get("application_term"))
    target_program, target_track = _target_program_and_track(context.get("target_program"))
    explicit_track = _normalize_track(context.get("target_track"), target_program)
    if explicit_track:
        target_track = explicit_track
    subject_ref = _safe_text(context.get("subject_ref")) or None
    approval = _resolve_minor_record_gate(
        department_approval,
        evidence_resolver=evidence_resolver,
        record_types={"MINOR_APPROVAL", "MINOR_DEPARTMENT_APPROVAL", "MINOR_APPROVAL_RECORD"},
        term=term,
        program=target_program,
        track=target_track,
        subject_ref=subject_ref,
        state_keys=("decision", "approval_status"),
        accepted_states={"APPROVED"},
        rejected_states={"REJECTED", "DENIED", "NOT_APPROVED"},
        pass_code="MINOR_DEPARTMENT_APPROVED",
        unknown_code="MINOR_DEPARTMENT_APPROVAL_UNKNOWN",
        reject_code="MINOR_DEPARTMENT_REJECTED",
        scope="minor_department_approval",
        pass_reason="系所正式輔系核准紀錄已核實。",
        missing_reason="缺少與目前申請事件及目標系所相符的系所輔系核准紀錄。",
        reject_reason="系所正式紀錄顯示輔系申請未核准。",
    )
    registration = _resolve_minor_record_gate(
        registrar_registration,
        evidence_resolver=evidence_resolver,
        record_types={"MINOR_REGISTRATION", "MINOR_REGISTRAR_REGISTRATION", "MINOR_REGISTRATION_RECORD"},
        term=term,
        program=target_program,
        track=target_track,
        subject_ref=subject_ref,
        state_keys=("registration_status",),
        accepted_states={"REGISTERED"},
        rejected_states={"REJECTED", "DENIED", "NOT_REGISTERED"},
        pass_code="MINOR_REGISTRAR_REGISTERED",
        unknown_code="MINOR_REGISTRATION_UNKNOWN",
        reject_code="MINOR_REGISTRATION_REJECTED",
        scope="minor_registrar_registration",
        pass_reason="教務處正式輔系登錄紀錄已核實。",
        missing_reason="缺少與目前申請事件及目標系所相符的教務處輔系登錄紀錄。",
        reject_reason="教務處正式紀錄顯示輔系未登錄。",
    )
    qualification = _resolve_minor_record_gate(
        formal_qualification,
        evidence_resolver=evidence_resolver,
        record_types={"MINOR_QUALIFICATION", "MINOR_FORMAL_QUALIFICATION", "MINOR_QUALIFICATION_RECORD"},
        term=term,
        program=target_program,
        track=target_track,
        subject_ref=subject_ref,
        state_keys=("qualification_status", "qualification_state"),
        accepted_states={"QUALIFIED"},
        rejected_states={"REJECTED", "DENIED", "NOT_QUALIFIED", "NOT_ELIGIBLE"},
        pass_code="MINOR_QUALIFICATION_VERIFIED",
        unknown_code="MINOR_QUALIFICATION_UNKNOWN",
        reject_code="MINOR_QUALIFICATION_NOT_GRANTED",
        scope="minor_formal_qualification",
        pass_reason="官方正式輔系資格紀錄已核實。",
        missing_reason="沒有與目前申請事件及目標系所相符的官方輔系資格紀錄。",
        reject_reason="官方正式紀錄顯示未取得輔系資格。",
    )
    statuses = [approval.get("status", UNKNOWN), registration.get("status", UNKNOWN)]
    overall = _aggregate_status(statuses)
    blockers = []
    for name, gate in (("department_approval", approval), ("registrar_registration", registration), ("formal_qualification", qualification)):
        if gate.get("status") in {FAIL, UNKNOWN}:
            blockers.append({"code": gate.get("code") or gate.get("status"), "dimension": name, "reason": gate.get("reason", "")})
    evidence_ids = []
    provenance = []
    for gate in (approval, registration, qualification):
        for evidence_id in gate.get("evidence_ids", ()):
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        provenance.extend(gate.get("provenance", ()))
    return {
        "status": overall,
        "state": overall,
        "can_pass": overall == PASS,
        "application_term": term,
        "target_program": target_program,
        "target_track": target_track,
        "self_reported": _public_self_report(context),
        "department_decision": approval,
        "department": approval,
        "registrar_registration": registration,
        "registration": registration,
        "formal_qualification": qualification,
        "gates": {
            "department_approval": approval.get("status", UNKNOWN),
            "registrar_registration": registration.get("status", UNKNOWN),
            "formal_qualification": qualification.get("status", UNKNOWN),
        },
        "blockers": blockers,
        "evidence_ids": evidence_ids,
        "provenance": provenance,
    }


def resolve_minor_award(
    opaque_evidence_id: Any = None,
    *,
    evidence_resolver: Any = None,
    target_program: Any = None,
    target_track: Any = None,
    subject_ref: Any = None,
    application_term: Any = None,
) -> dict[str, Any]:
    """Resolve an independent official ``MINOR_AWARD`` record.

    Formal minor awarding is a student- and application-event-specific fact.
    A record type, course completion, or an unbound official-looking row is not
    sufficient evidence of that fact.
    """

    program = _normalize_program(target_program) if target_program is not None else None
    track = _normalize_track(target_track, program) if target_track is not None else None
    term = _normalize_term(application_term)
    ref = _safe_text(subject_ref) or None
    if not program or not term or not ref:
        return _gate(UNKNOWN, code="MINOR_AWARD_UNKNOWN", reason="輔系授予證據缺少目前學生、目標系所或申請學期綁定。") | {
            "award_state": UNKNOWN,
            "is_official": False,
        }
    record = _resolve_server_record(
        opaque_evidence_id,
        evidence_resolver,
        record_types={"MINOR_AWARD", "MINOR_FORMAL_AWARD", "MINOR_AWARD_RECORD"},
        term=term,
        program=program,
        track=track,
        subject_ref=ref,
        require_subject=True,
        require_application_term=True,
        require_program=True,
    )
    if record is None:
        return _gate(UNKNOWN, code="MINOR_AWARD_UNKNOWN", reason="沒有可核實的官方 MINOR_AWARD_RECORD；課程完成不能代替正式授予紀錄。") | {
            "award_state": UNKNOWN,
            "is_official": False,
        }
    award_state = str(_record_value(record, "award_state", "award_status", "decision", "status") or "").strip().upper()
    if award_state in {GRANTED, "AWARDED"}:
        result = _gate(PASS, code="MINOR_AWARD_GRANTED", value=award_state, record=record, scope="minor_formal_award", reason="官方正式授予輔系紀錄已核實。")
        result.update({"award_state": award_state, "is_official": True, "record_type": _record_type(record)})
        return result
    if award_state in {"REVOKED", "DENIED", "NOT_GRANTED", "NOT_AWARDED", "REJECTED"}:
        result = _gate(FAIL, code="MINOR_AWARD_NOT_GRANTED", value=award_state, record=record, scope="minor_formal_award", reason="官方正式紀錄顯示未授予或已撤銷輔系。")
        result.update({"award_state": award_state, "is_official": True, "record_type": _record_type(record)})
        return result
    result = _gate(UNKNOWN, code="MINOR_AWARD_UNKNOWN", value=award_state or None, record=record, scope="minor_formal_award", reason="官方正式輔系授予紀錄沒有明確狀態。")
    result.update({"award_state": UNKNOWN, "is_official": False, "record_type": _record_type(record)})
    return result


__all__ = [
    "APPLICATION_EVENT_RECORD",
    "APPLICATION_EVENT_UNKNOWN",
    "ACTIVE",
    "CONFLICTED",
    "DEPARTMENT_DECISION_UNKNOWN",
    "EFFECTIVE_TERM_UNKNOWN",
    "FAIL",
    "FORMAL_AWARD_RECORD",
    "FORMAL_QUALIFICATION_RECORD",
    "GRANTED",
    "MISSING",
    "NOT_APPLICABLE",
    "PASS",
    "REGISTRATION_UNKNOWN",
    "RULE_VERSION_UNKNOWN",
    "SUBMISSION_UNKNOWN",
    "UNKNOWN",
    "get_notice_resolution",
    "resolve_minor_application_case",
    "resolve_minor_award",
    "resolve_application_case",
    "resolve_formal_qualification",
    "resolve_formal_award",
]
