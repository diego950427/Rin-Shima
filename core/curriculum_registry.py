"""Versioned curriculum metadata and fail-closed rule resolution.

This module is deliberately small and data-oriented.  It is the boundary
between a student's selected identity (admission cohort, primary programme and
double-major target) and the older aggregate rule helpers.  In particular, a
target curriculum is never inferred from an admission cohort or an
application term.  A target version must be selected explicitly and be backed
by a scoped applicability assertion or an evidence reference.

The registry keeps two kinds of evidence separate:

* ``evidence_state`` describes what the official source says
  (``VERIFIED``, ``CONFLICTED`` or ``MISSING``).
* ``coverage_state`` describes how much of that source has been transcribed
  into course-level data (``COMPLETE``, ``PARTIAL`` or ``NONE``).

The returned dictionaries are copies, so report rendering and integrations
cannot mutate the process-wide registry.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

# Public states.  Keep these strings stable because reports and exports use
# them as audit values.
VERIFIED = "VERIFIED"
CONFLICTED = "CONFLICTED"
MISSING = "MISSING"
MANUAL_REVIEW = "MANUAL_REVIEW"

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
COVERAGE_NONE = "NONE"

RESOLVED = "RESOLVED"
NOT_APPLICABLE = "NOT_APPLICABLE"

_SUPPORTED_COHORTS = ("111", "112", "113", "114", "115")
_DOUBLE_MAJOR = "雙主修"

_ROOT = os.path.dirname(os.path.abspath(__file__))
_RULES_PATH = os.path.join(_ROOT, "rules_config.json")


def _cleanup_legacy_contaminated_handbooks() -> None:
    try:
        data_handbooks_dir = os.path.join(_ROOT, "data", "handbooks")
        if os.path.isdir(data_handbooks_dir):
            for name in os.listdir(data_handbooks_dir):
                if "_應用化學組_" in name and "應用物理暨化學系" not in name:
                    target_path = os.path.join(data_handbooks_dir, name)
                    try:
                        os.remove(target_path)
                    except OSError:
                        pass
    except Exception:
        pass


# UT-checker integration is read-only; never delete files during module import.

_HANDBOOK_URLS = {
    "111": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MemN4TDNCMFlWODVNREkyTWw4eE56STBPREZmT0RBNU5qY3VjR1Jt&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "112": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MelV5TDNCMFlWOHhNRFF3T1RKZk16YzNORGc0TVY4ek16ZzJOaTV3WkdZPQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "113": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MemcxTDNCMFlWOHhNell5TmpoZk16TXhNREE0TVY4ek9UY3lNUzV3WkdZPQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "114": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MemN5TDNCMFlWOHhNemN4TWpSZk5UQTVNalkzTWw4ek9UY3lNUzV3WkdZPQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "115": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9Memt3TDNCMFlWOHhOelExTnpKZk1qTXlNak0yWHpjNE5qSXhMbkJrWmc9PQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
}
_DOUBLE_MAJOR_RULE_URL = "https://reg.utaipei.edu.tw/var/file/31/1031/img/926/316980591.pdf"

_PROGRAMS = {
    "地生": "earth",
    "地球環境": "earth",
    "地球環境暨生物資源學系": "earth",
    "生命科學": "earth",
    "生命科學系": "earth",
    "物化": "apc",
    "物理化學": "apc",
    "應用物理": "apc",
    "電子物理": "apc",
    "應用化學": "apc",
    "化學": "apc",
    "資科": "cs",
    "資訊科學": "cs",
    "資訊科學系": "cs",
    "數學": "math",
    "數學系": "math",
    "數據科學與數學": "math",
    "數據科學與數學系": "math",
}
_PROGRAM_DISPLAY = {"earth": "地生", "apc": "物化", "cs": "資科", "math": "數學"}
_TRACKS = {
    "earth_environment": {"地球環境", "地球環境系", "地球環境暨生物資源學系", "地生（地球環境）", "地生-地球環境"},
    "life_science": {"生命科學", "生命科學系", "地生（生命科學）", "地生-生命科學"},
    "physics": {"物理組", "電子物理", "電子物理組", "物化系物理組", "物化（電子物理）", "物化-電子物理"},
    "chemistry": {"化學組", "應用化學", "應用化學組", "物化系化學組", "物化（應用化學）", "物化-應用化學"},
    "math_scientific_computing": {"數學與科學計算", "數學與科學計算領域", "math_scientific_computing"},
    "data_science": {"數據科學", "數據科學領域", "data_science"},
    "math_education": {"數學教育", "數學教育領域", "math_education"},
}
_TRACK_DISPLAY = {
    "earth_environment": "地球環境",
    "life_science": "生命科學",
    "physics": "電子物理",
    "chemistry": "應用化學",
    "math_scientific_computing": "數學與科學計算",
    "data_science": "數據科學",
    "math_education": "數學教育",
}
_PROGRAM_SLUGS = frozenset({"earth", "apc", "cs", "math"})
_MATH_PRIMARY_TRACKS = {
    "111": (),
    "112": (),
    "113": ("math_scientific_computing", "data_science", "math_education"),
    "114": ("math_scientific_computing", "data_science", "math_education"),
    "115": ("math_scientific_computing", "data_science"),
}
_TRACK_SLUGS = frozenset(
    {
        "earth_environment",
        "life_science",
        "physics",
        "chemistry",
        "math_scientific_computing",
        "data_science",
        "math_education",
    }
)
_TRACK_PROGRAMS = {
    "earth_environment": "earth",
    "life_science": "earth",
    "physics": "apc",
    "chemistry": "apc",
    "math_scientific_computing": "math",
    "data_science": "math",
    "math_education": "math",
}

# Course-pool identifiers deliberately include the handbook cohort and the
# selected role.  A transcript row from one handbook therefore cannot satisfy
# a similarly named course in another handbook merely because the title is the
# same.  The registrar export used by this project does not contain course
# codes or opening departments, so the identity contract carried by each pool
# is explicit about the fields that are actually available.
_POOL_ID_SCHEMA = "pool:v1"
_POOL_IDENTITY_FIELDS = (
    "curriculum_version",
    "program_slug",
    "track_slug",
    "raw_title",
    "credits",
    "lecture_or_lab",
)


def _scope_slug(kind: str) -> str:
    if kind == "primary":
        return "primary"
    if kind in {_MINOR_TARGET_ROLE, "minor", "minor_target"}:
        return "minor"
    return "double_major"


def _pool_token(value: Any) -> str:
    text = _normalize_text(value)
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", text).strip("_").lower()
    return text or "department"


def _pool_id(kind: str, cohort: str, program: str, track: str | None, bucket: str) -> str:
    """Return a stable, year-scoped course-pool identifier."""

    return ":".join(
        (
            "pool",
            _scope_slug(kind),
            str(cohort),
            str(program),
            _pool_token(track or "department"),
            _pool_token(bucket),
        )
    )


def _pool_source(source: Mapping[str, Any], pool_id: str) -> dict[str, Any]:
    """Copy source metadata while giving the pool its own auditable ref."""

    source_reference = f"{source.get('source_reference', 'handbook')}:pool:{pool_id.rsplit(':', 1)[-1]}"
    return {
        "source_type": "official_handbook",
        "research_file": source.get("research_file", ""),
        "source_pdf_sha256": source.get("source_pdf_sha256", ""),
        "source_catalog_file": source.get("source_catalog_file", ""),
        "source_url": source.get("source_url", ""),
        "source_file": source.get("source_file", ""),
        "pdf_page": source.get("pdf_page", "未標示"),
        "printed_page": source.get("printed_page", "未標示"),
        "pages": source.get("pages", "未標示"),
        "table_location": source.get("table_location", "官方課程表"),
        "source_reference": source_reference,
        "original_clause": source.get("original_clause", ""),
    }


def _pool_course_entry(
    cohort: str,
    program: str,
    track: str | None,
    pool_id: str,
    name: str,
    credits: int | float,
    source: Mapping[str, Any],
    *,
    evidence_state: str = VERIFIED,
    course_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a candidate entry; candidates never become requirements alone."""

    raw_title = str(name)
    slug = _pool_token(raw_title)
    component = "lab" if "實驗" in raw_title else "lecture"
    pool_source = _pool_source(source, pool_id)
    metadata = deepcopy(dict(course_metadata)) if isinstance(course_metadata, Mapping) else {}
    # A source-backed catalog may pin each candidate to a distinct printed
    # row.  Keep that row citation when present; the pool-level fallback is
    # retained for legacy candidate maps that only provide pool provenance.
    resolved_source = deepcopy(pool_source)
    for key in (
        "research_file",
        "source_pdf_sha256",
        "source_catalog_file",
        "source_url",
        "source_file",
        "pdf_page",
        "printed_page",
        "pages",
        "table_location",
        "source_reference",
        "original_clause",
    ):
        if metadata.get(key) not in (None, ""):
            resolved_source[key] = deepcopy(metadata[key])
    row_reference = str(
        resolved_source.get("source_reference")
        or f"{pool_source['source_reference']}:course:{slug}"
    )
    original_clause = str(
        resolved_source.get("original_clause")
        or f"{raw_title} {float(credits):g} 學分；列於官方課程池。"
    )
    entry = {
        "name": raw_title,
        "raw_title": raw_title,
        "credits": float(credits),
        "component": component,
        "component_type": component,
        "lecture_or_lab": component,
        "is_lab": component == "lab",
        "curriculum_version": cohort,
        "program_slug": program,
        "track_slug": track or "department",
        "official_course_identity": f"{cohort}:{program}:{track or 'department'}:{slug}",
        "pool_ids": (pool_id,),
        "candidate_only": True,
        "requirement_type": "candidate_course",
        "evidence_state": evidence_state,
        "verification_status": evidence_state,
        "source_reference": row_reference,
        "source_url": resolved_source["source_url"],
        "source_file": resolved_source["source_file"],
        "source_pdf_sha256": resolved_source.get("source_pdf_sha256", ""),
        "source_catalog_file": resolved_source.get("source_catalog_file", ""),
        "research_file": resolved_source["research_file"],
        "pdf_page": resolved_source["pdf_page"],
        "printed_page": resolved_source["printed_page"],
        "pages": resolved_source["pages"],
        "table_location": resolved_source["table_location"],
        "original_clause": original_clause,
        "source": {**resolved_source, "source_reference": row_reference, "raw_title": raw_title},
    }
    if metadata:
        entry["course_metadata"] = metadata
        entry["source"]["course_metadata"] = deepcopy(metadata)
        # Preserve server-owned subset/evidence claims at the candidate
        # boundary.  These fields describe the printed pool membership; they
        # never turn a candidate into a required course or accept a student
        # supplied department assertion.
        for key in (
            "canonical_name",
            "course_alias_of",
            "official_course_identity",
            "source_track_slug",
            "source_track",
            "source_program_slug",
            "requires_server_owned_offering",
            "secondary_course_key",
            "course_key",
            "math_secondary_composite_required",
            "subset_ids",
            "membership_ids",
            "membership_assertions",
            "pool_membership_evidence",
            "observed_requirement_ids",
            "source_domains",
            "source_row",
            "source_table",
            "original_note",
            "membership_source_reference",
        ):
            if key in metadata:
                entry[key] = deepcopy(metadata[key])
    # Only verified, department-scoped candidates from the official 理學院
    # handbook can assert the college membership.  Keep the assertion tied to
    # the same handbook pool provenance; university GE/PE pools, free-choice
    # pools, and unresolved candidates deliberately remain unlabelled.
    pool_bucket = pool_id.rsplit(":", 1)[-1]
    science_department_buckets = {
        "earth": {
            "common_compulsory",
            "domain_required",
            "common_elective",
            "domain_elective",
            "department_professional",
            "common_alternative_1",
            "common_alternative_2",
            "common_alternative_3",
        },
        "apc": {
            "apc_common_compulsory",
            "apc_cross_track_lab_candidates",
            "apc_track_compulsory",
            "apc_track_elective",
        },
        "cs": {
            "cs_department_required",
            "cs_department_elective",
            "cs_elective_alpha",
            "cs_elective_beta",
        },
        "math": {
            "math_common_compulsory",
            "math_domain_required",
            "math_department_elective",
        },
    }
    official_college_handbook = (
        str(pool_source.get("source_file") or "").startswith("3-理學院")
        and pool_source["source_type"] == "official_handbook"
    )
    if (
        program in science_department_buckets
        and evidence_state == VERIFIED
        and pool_bucket in science_department_buckets[program]
        and official_college_handbook
    ):
        existing_memberships = tuple(entry.get("membership_ids", ()))
        membership_ids = tuple(
            dict.fromkeys(("science_college", *existing_memberships))
        )
        entry.update(
            {
                "college": "理學院",
                "membership_ids": membership_ids,
            }
        )
        entry["source"].update(
            {
                "college": "理學院",
                "membership_ids": membership_ids,
            }
        )
    return entry


def _pool_record(
    *,
    kind: str,
    cohort: str,
    program: str,
    track: str | None,
    bucket: str,
    label: str,
    source: Mapping[str, Any],
    candidates: Mapping[str, int | float] | None = None,
    candidate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    required_credits: int | float | None = None,
    evidence_state: str = VERIFIED,
    coverage_state: str = COMPLETE,
    selection_rule: str = "exact_title_credit_component",
    policy: Mapping[str, Any] | None = None,
    overflow_routes: tuple[str, ...] = (),
    manual_reason: str = "",
) -> dict[str, Any]:
    """Create one auditable pool policy and its non-required candidates."""

    pool_id = _pool_id(kind, cohort, program, track, bucket)
    course_entries = tuple(
        _pool_course_entry(
            cohort,
            program,
            track,
            pool_id,
            name,
            credits,
            source,
            evidence_state=evidence_state,
            course_metadata=(candidate_metadata or {}).get(name),
        )
        for name, credits in (candidates or {}).items()
    )
    pool_source = _pool_source(source, pool_id)
    policy_data = deepcopy(dict(policy or {}))
    # Policy-only pools are compiled by the core policy gate.  Keep the
    # policy identity and its scope beside the ordinary pool provenance so a
    # policy cannot silently apply to another cohort, programme, role, or
    # track.  Exact named-course pools may omit these fields.
    policy_source = policy_data.get("policy_source")
    policy_source = deepcopy(policy_source) if isinstance(policy_source, Mapping) else None
    policy_id = policy_data.get("policy_id")
    policy_revision = policy_data.get("revision")
    applies_to = policy_data.get("applies_to")
    predicate = policy_data.get("predicate")
    policy_evidence_state = policy_data.get("evidence_state", evidence_state)
    policy_coverage_state = policy_data.get("coverage_state", coverage_state)
    policy_automatic_decision = policy_data.get("automatic_decision")
    if policy_automatic_decision is None:
        policy_automatic_decision = (
            policy_evidence_state == VERIFIED
            and policy_coverage_state == COMPLETE
            and not manual_reason
        )
    requirement_minimum_credits = policy_data.get("requirement_minimum_credits")
    if requirement_minimum_credits is None and required_credits is not None:
        requirement_minimum_credits = float(required_credits)
    subset_maxima = policy_data.get("subset_maxima")
    subset_maxima = deepcopy(dict(subset_maxima)) if isinstance(subset_maxima, Mapping) else {}
    return {
        "id": pool_id,
        "pool_id": pool_id,
        "schema": _POOL_ID_SCHEMA,
        "scope": _scope_slug(kind),
        "kind": kind,
        "curriculum_version": cohort,
        "cohort": cohort,
        "program_slug": program,
        "track_slug": track or "department",
        "label": label,
        "bucket": bucket,
        "required_credits": float(required_credits) if required_credits is not None else None,
        "requirement_minimum_credits": (
            float(requirement_minimum_credits)
            if requirement_minimum_credits is not None
            else None
        ),
        "subset_maxima": subset_maxima,
        "candidate_only": True,
        "eligible_pool_ids": (pool_id,),
        "overflow_routes": tuple(overflow_routes),
        "selection_rule": selection_rule,
        "identity_fields": _POOL_IDENTITY_FIELDS,
        "allow_user_claimed_department": False,
        "allowed_components": ("lecture", "lab"),
        "evidence_state": evidence_state,
        "verification_status": evidence_state,
        "coverage_state": coverage_state,
        "automation_sufficiency": "COMPLETE" if coverage_state == COMPLETE and evidence_state == VERIFIED else "PARTIAL",
        "manual_reason": manual_reason,
        "source_reference": pool_source["source_reference"],
        "source_url": pool_source["source_url"],
        "source_file": pool_source["source_file"],
        "research_file": pool_source["research_file"],
        "pdf_page": pool_source["pdf_page"],
        "printed_page": pool_source["printed_page"],
        "pages": pool_source["pages"],
        "table_location": pool_source["table_location"],
        "original_clause": pool_source["original_clause"] or f"{label}課程池。",
        "policy": policy_data,
        "policy_id": policy_id,
        "policy_revision": policy_revision,
        "applies_to": deepcopy(applies_to),
        "predicate": deepcopy(predicate),
        "policy_evidence_state": policy_evidence_state,
        "policy_coverage_state": policy_coverage_state,
        "policy_automatic_decision": bool(policy_automatic_decision),
        "policy_source": policy_source,
        "candidate_courses": course_entries,
        "courses": course_entries,
    }

# ``minor`` is a distinct target role.  It deliberately does not reuse the
# double-major catalogue or its shared-credit semantics.  These are the only
# five target programme scopes supported by the checked-in research matrix;
# no course is inferred from a neighbouring handbook year.
_MINOR_TARGET_ROLE = "minor_target"
_MINOR_RESEARCH_FILE = "research/minor_program_matrix_111_115.md"
_MINOR_PROGRAMS = ("earth", "apc", "cs", "math")
_MINOR_APC_TRACKS = ("physics", "chemistry")

_MINOR_SOURCE_PAGES: dict[tuple[str, str, str | None], tuple[Any, Any, str]] = {
    # ``(pdf page, printed page, matrix section)``
    **{
        (year, "apc", track): (
            {"111": 10, "112": 10, "113": 11, "114": "11–12", "115": 12}[year]
            if track == "physics"
            else {"111": 19, "112": 19, "113": 23, "114": 23, "115": 24}[year],
            {"111": 9, "112": 9, "113": 10, "114": "10–11", "115": 11}[year]
            if track == "physics"
            else {"111": 18, "112": 18, "113": 22, "114": 22, "115": 23}[year],
            f"5.1 APC {year} {track}",
        )
        for year in _SUPPORTED_COHORTS
        for track in _MINOR_APC_TRACKS
    },
    **{
        (year, "earth", None): (
            {"111": 39, "112": 39, "113": 45, "114": 47, "115": 55}[year],
            {"111": 38, "112": 38, "113": 44, "114": 46, "115": 54}[year],
            f"5.2 地生 {year}",
        )
        for year in _SUPPORTED_COHORTS
    },
    **{
        (year, "cs", None): (
            {"111": 121, "112": 116, "113": 111, "114": 116, "115": 127}[year],
            {"111": 120, "112": 115, "113": 110, "114": 115, "115": 126}[year],
            f"5.3 資科 {year}",
        )
        for year in _SUPPORTED_COHORTS
    },
    **{
        (year, "math", None): (
            {"111": "77–79", "112": "72–74", "113": "74–76", "114": "77–79", "115": "88–90"}[year],
            {"111": "76–78", "112": "71–73", "113": "73–75", "114": "76–78", "115": "87–89"}[year],
            f"5.4 數學／數據科學與數學 {year}",
        )
        for year in _SUPPORTED_COHORTS
    },
}

_MINOR_COURSE_KIND = "lecture"


def _table_cell_clause(name: str, credits: int | float) -> str:
    """Return only the transcribed course cell, never a synthesized sentence."""

    return f"{name} {credits:g} 學分"


def _minor_source(year: str, program: str, track: str | None) -> dict[str, Any]:
    try:
        pdf_page, printed_page, section = _MINOR_SOURCE_PAGES[(year, program, track)]
    except KeyError:
        # Earth secondary tables are department-scoped even when the
        # historical double-major id keeps the selected Earth track.  Use
        # only the same cohort/program department source in that case.
        if program != "earth":
            raise
        pdf_page, printed_page, section = _MINOR_SOURCE_PAGES[(year, program, None)]
    course_pdf_page = pdf_page
    if program == "earth":
        course_pdf_page = {"111": 32, "112": 32, "113": 38, "114": 40, "115": 48}[year]
    elif program == "cs":
        course_pdf_page = {"111": 121, "112": 116, "113": 111, "114": 116, "115": 127}[year]
    elif program == "math":
        course_pdf_page = {"111": "77–79", "112": "72–74", "113": "74–76", "114": "77–79", "115": "88–90"}[year]
    elif program == "apc":
        course_pdf_page = pdf_page
    official_reference = f"handbook:{year}:pdf:{course_pdf_page}:table:{program}:{track or 'department'}"
    source = {
        "research_file": _MINOR_RESEARCH_FILE,
        "source_url": _HANDBOOK_URLS[year],
        "source_file": _source_file(year),
        "pdf_page": pdf_page,
        "printed_page": printed_page,
        "pages": f"PDF p.{pdf_page}（印刷 p.{printed_page}）",
        "section": section,
        # A research matrix is supporting provenance, never the official
        # row identity.  Course rows append ``:row:<slug>`` below.
        "source_reference": official_reference,
    }
    if program == "earth":
        course_pdf_page = {"111": 32, "112": 32, "113": 38, "114": 40, "115": 48}[year]
        course_printed_page = {"111": 31, "112": 31, "113": 37, "114": 39, "115": 47}[year]
        source.update(
            {
                "course_pdf_page": course_pdf_page,
                "course_printed_page": course_printed_page,
                "course_pages": f"PDF p.{course_pdf_page}（印刷 p.{course_printed_page}）",
                "course_table_location": f"{section}；共同必修逐課表",
                "source_reference": f"handbook:{year}:pdf:{course_pdf_page}:table:{program}:department",
            }
        )
    elif program == "cs":
        source.update(
            {
                "course_pdf_page": course_pdf_page,
                "course_printed_page": {"111": 120, "112": 115, "113": 110, "114": 115, "115": 126}[year],
                "course_pages": f"PDF p.{course_pdf_page}（印刷 p.{ {'111': 120, '112': 115, '113': 110, '114': 115, '115': 126}[year] }）",
                "course_table_location": f"{section}；輔系課程列項",
                "source_reference": f"handbook:{year}:pdf:{course_pdf_page}:table:{program}:department",
            }
        )
    elif program == "math":
        source.update(
            {
                "course_pdf_page": course_pdf_page,
                "course_printed_page": {"111": "76–78", "112": "71–73", "113": "73–75", "114": "76–78", "115": "87–89"}[year],
                "course_pages": f"PDF pp.{course_pdf_page}（印刷 pp.{ {'111': '76–78', '112': '71–73', '113': '73–75', '114': '76–78', '115': '87–89'}[year] }）",
                "course_table_location": f"{section}；輔系課程列項",
                "source_reference": f"handbook:{year}:pdf:{course_pdf_page}:table:{program}:department",
            }
        )
    elif program == "apc":
        source.update(
            {
                "course_pdf_page": pdf_page,
                "course_printed_page": printed_page,
                "course_pages": source["pages"],
                "course_table_location": f"{section}；輔系課程列項",
                "source_reference": f"handbook:{year}:pdf:{pdf_page}:table:{program}:{track or 'department'}",
            }
        )
    return source


def _minor_row(
    year: str,
    program: str,
    track: str | None,
    slug: str,
    name: str,
    credits: int | float,
    *,
    component: str = _MINOR_COURSE_KIND,
    eligible_names: tuple[str, ...] | None = None,
    eligible_options: tuple[tuple[str, int | float], ...] | None = None,
    requirement_type: str = "named_course",
    choice_group: str | None = None,
    choice_rule: str = "exact_course_or_approved_equivalency",
    accept_any: bool = False,
    waiver: bool = False,
    zero_credit: bool = False,
    evidence_state: str = VERIFIED,
    coverage_state: str = COMPLETE,
    original_clause: str = "",
    manual_reason: str = "",
    pool_ids: tuple[str, ...] = (),
    eligible_pool_ids: tuple[str, ...] = (),
    overflow_routes: tuple[str, ...] = (),
) -> dict[str, Any]:
    if requirement_type == "credit_quota":
        # A quota describes an amount consumed from a candidate pool.  Its
        # candidates may be lectures or labs, so the row itself must not be
        # mistaken for either course component.
        component = "quota"
    source = _minor_source(year, program, track)
    row_pdf_page = source.get("course_pdf_page", source["pdf_page"])
    row_printed_page = source.get("course_printed_page", source["printed_page"])
    row_pages = source.get("course_pages", source["pages"])
    row_table_location = source.get("course_table_location", source["section"])
    row_id = f"minor.{year}.{program}.{track or 'department'}.{slug}"
    source_reference = f"{source['source_reference']}:row:{slug}"
    names = eligible_names if eligible_names is not None else (name,)
    # Keep fixed named rows and explicit choice/quota rows in different
    # pools.  A named row's pool is identity evidence for that exact row; it
    # is never an eligibility route for a separate credit quota.  Choice
    # groups also get independent pools so (for example) a software choice
    # cannot consume the same candidate as the remaining elective quota.
    default_pool_id = _pool_id("minor", year, program, track, "required")
    if requirement_type in {"choice", "course_pool", "credit_quota"}:
        if requirement_type == "choice":
            pool_bucket = f"choice_{choice_group or slug}"
        elif requirement_type == "course_pool":
            pool_bucket = "elective"
        else:
            pool_bucket = "quota"
        default_pool_id = _pool_id("minor", year, program, track, pool_bucket)
        resolved_pool_ids = tuple(pool_ids)
        resolved_eligible_pool_ids = tuple(eligible_pool_ids) or (default_pool_id,)
    elif requirement_type in {"missing_named_course", "conflict_candidate"}:
        resolved_pool_ids = tuple(pool_ids)
        resolved_eligible_pool_ids = tuple(eligible_pool_ids)
    else:
        resolved_pool_ids = tuple(pool_ids) or (default_pool_id,)
        resolved_eligible_pool_ids = tuple(eligible_pool_ids)
    automatic = evidence_state == VERIFIED and coverage_state == COMPLETE and not manual_reason
    result = {
        "id": row_id,
        "requirement_id": row_id,
        "name": name,
        "raw_title": name,
        "credits": float(credits),
        "bucket": "minor_required" if requirement_type != "course_pool" else "minor_elective",
        "kind": "MINOR_COURSE" if requirement_type != "credit_quota" else "MINOR_QUOTA",
        "requirement_type": requirement_type,
        "choice_group": choice_group,
        "choice_rule": choice_rule,
        "track": _TRACK_DISPLAY.get(track) if track else None,
        "eligible_course_names": tuple(names),
        "eligible_course_options": tuple(
            {"name": option_name, "credits": float(option_credits)}
            for option_name, option_credits in (eligible_options or ())
        ),
        "accept_any": accept_any,
        # ``pool_ids`` identify where this exact named course may be counted;
        # ``eligible_pool_ids`` belong only to an explicit choice/quota row.
        "pool_ids": resolved_pool_ids,
        "eligible_pool_ids": resolved_eligible_pool_ids,
        "overflow_routes": tuple(overflow_routes),
        "candidate_only": False,
        "pool_requirement": requirement_type in {"choice", "course_pool", "credit_quota"},
        "required_credits": float(credits)
        if requirement_type in {"choice", "course_pool", "credit_quota"}
        else None,
        "waiver": waiver,
        "waiver_generates_credits": False,
        "component": component,
        "component_type": component,
        "component_label": "實驗" if component == "lab" else "講授" if component == "lecture" else component,
        "lecture_or_lab": component,
        "is_lab": component == "lab",
        "is_zero_credit": zero_credit,
        "allow_combined_lab_source": False,
        "evidence": evidence_state,
        "evidence_state": evidence_state,
        "coverage_state": coverage_state,
        "verification_status": evidence_state,
        "automation_sufficiency": "COMPLETE" if automatic else "PARTIAL",
        "automatic_decision": automatic,
        "manual_reason": manual_reason,
        "manual_review_reason": manual_reason,
        "program_slug": program,
        "track_slug": track or "department",
        "curriculum_version": year,
        "source_assertion_id": row_id,
        "assertion_id": row_id,
        "source_reference": source_reference,
        "source_url": source["source_url"],
        "source_file": source["source_file"],
        "research_file": source["research_file"],
        "pdf_page": row_pdf_page,
        "printed_page": row_printed_page,
        "page": row_pdf_page,
        "pages": row_pages,
        "table_location": row_table_location,
        "original_clause": original_clause,
        "original_text": original_clause,
        "source": {
            "file": source["source_file"],
            "source_file": source["source_file"],
            "url": source["source_url"],
            "source_url": source["source_url"],
            "pages": row_pages,
            "pdf_page": row_pdf_page,
            "printed_page": row_printed_page,
            "source_reference": source_reference,
            "original_clause": original_clause,
            "curriculum_version": year,
        },
        "provenance": {
            "assertion_id": row_id,
            "source_type": "official_handbook",
            "research_file": source["research_file"],
            "source_url": source["source_url"],
            "source_file": source["source_file"],
            "pdf_page": row_pdf_page,
            "printed_page": row_printed_page,
            "pages": row_pages,
            "source_reference": source_reference,
            "table_location": row_table_location,
            "original_clause": original_clause,
            "evidence_state": evidence_state,
            "verification_status": evidence_state,
            "automatic_decision": automatic,
            "manual_reason": manual_reason,
            "manual_review_reason": manual_reason,
        },
    }
    return result


def _secondary_catalog(
    cohort: str,
    program: str,
    role: str,
    track: str | None = None,
) -> dict[str, Any]:
    """Return the year-scoped secondary catalogue contract.

    Secondary tables are kept under their owning handbook section in
    ``rules_config.json``.  This accessor deliberately fails closed when a
    section is absent; it must never borrow a neighbouring cohort's rows.
    """

    section_key = {
        "earth": "earth_life_major",
        "apc": "apc_rules",
        "cs": "cs_rules",
        "math": "math_rules",
    }[program]
    handbook = _RULES.get("handbooks", {}).get(str(cohort), {})
    section = handbook.get(section_key, {}) if isinstance(handbook, Mapping) else {}
    catalog = section.get("secondary_catalog", {}) if isinstance(section, Mapping) else {}
    if program == "apc":
        if track:
            catalog = catalog.get(str(track), {}) if isinstance(catalog, Mapping) else {}
        source = catalog.get("source", {}) if isinstance(catalog, Mapping) else {}
        selected = catalog.get(str(role), {}) if isinstance(catalog, Mapping) else {}
    else:
        catalog = catalog if isinstance(catalog, Mapping) else {}
        source = catalog.get("source", {}) if isinstance(catalog, Mapping) else {}
        selected = catalog.get(str(role), {}) if isinstance(catalog, Mapping) else {}
    if not isinstance(selected, Mapping):
        return {}
    result = deepcopy(dict(selected))
    if isinstance(source, Mapping):
        result.setdefault("source", deepcopy(dict(source)))
        for key, value in source.items():
            result.setdefault(key, deepcopy(value))
    return result


def _secondary_source(
    cohort: str,
    program: str,
    track: str | None,
    role: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one source contract for a secondary table and its pool."""

    source = contract.get("source", {}) if isinstance(contract, Mapping) else {}
    source = deepcopy(dict(source)) if isinstance(source, Mapping) else {}
    try:
        fallback = _minor_source(cohort, program, track)
    except KeyError:
        # Earth double-major targets are exposed through the historical
        # ``earth_environment`` target id, while the secondary source table
        # is department-scoped.  Fall back only to the same cohort/program;
        # never borrow a neighbouring cohort or a different programme.
        fallback = _minor_source(cohort, program, None)
    for key, fallback_key in (
        ("source_file", "source_file"),
        ("source_url", "source_url"),
        ("research_file", "research_file"),
        ("pdf_page", "pdf_page"),
        ("printed_page", "printed_page"),
        ("pages", "pages"),
        ("table_location", "section"),
        ("source_reference", "source_reference"),
    ):
        if source.get(key) in (None, ""):
            source[key] = fallback.get(fallback_key, "")
    source.setdefault("source_type", "official_handbook")
    source.setdefault("curriculum_version", cohort)
    source.setdefault("program_slug", program)
    source.setdefault("track_slug", track or "department")
    source.setdefault("original_clause", "")
    source.setdefault("source_pdf_sha256", contract.get("source_pdf_sha256", ""))
    source.setdefault("source_catalog_file", contract.get("source_catalog_file", ""))
    source["role"] = role
    source["source_type"] = "official_handbook"
    # Keep the aliases consumed by older source/export code in sync.
    source["url"] = source.get("source_url", "")
    source["file"] = source.get("source_file", "")
    return source


def _secondary_item_source(
    source: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    name: str,
    credits: int | float,
) -> dict[str, Any]:
    """Overlay an exact source-table row on a secondary source contract."""

    result = deepcopy(dict(source))
    for key in (
        "source_reference",
        "pdf_page",
        "printed_page",
        "pages",
        "table_location",
        "original_clause",
        "source_pdf_sha256",
        "source_catalog_file",
    ):
        if item.get(key) not in (None, ""):
            result[key] = deepcopy(item[key])
    if item.get("source_table") not in (None, ""):
        result["table_location"] = deepcopy(item["source_table"])
    result["source_reference"] = str(
        result.get("source_reference")
        or f"{source.get('source_reference', 'handbook')}:row:{_pool_token(name)}"
    )
    result["original_clause"] = str(
        result.get("original_clause")
        or item.get("original_clause")
        or f"{name} {float(credits):g} 學分；列於官方次修課程表。"
    )
    result["url"] = result.get("source_url", "")
    result["file"] = result.get("source_file", "")
    return result


def _apply_secondary_item(
    row: dict[str, Any],
    item: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    role: str,
    program: str,
    cohort: str,
    track: str | None,
    candidate_names: Sequence[str] = (),
) -> dict[str, Any]:
    """Attach source-row, membership and composite-catalog metadata to a row."""

    name = str(item.get("name") or row.get("name") or "").strip()
    credits = float(item.get("credits", row.get("credits", 0)) or 0)
    row_source = _secondary_item_source(source, item, name=name, credits=credits)
    row_reference = row_source["source_reference"]
    slug = _pool_token(name)
    membership_ids = item.get("membership_ids", ())
    if isinstance(membership_ids, str):
        membership_ids = (membership_ids,)
    elif not isinstance(membership_ids, Sequence):
        membership_ids = ()
    membership_ids = tuple(str(value) for value in membership_ids if str(value).strip())
    if program == "math":
        # Core resolves Math secondary rows through the stable, normalized
        # course key; the eligibility membership is the non-consuming union
        # observed by the total gate.  Keep source row keys separately.
        membership_ids = (
            f"math-secondary:{cohort}:{_pool_token(name)}",
            f"math-secondary:{cohort}:eligible-target-credit",
        )
    if not membership_ids:
        membership_ids = (f"{program}-secondary:{cohort}:{role}:{slug}",)
    item_metadata = {
        key: deepcopy(value)
        for key, value in item.items()
        if key not in {"name", "credits", "options"}
    }
    item_metadata.setdefault("membership_ids", membership_ids)
    item_metadata.setdefault("membership_source_reference", row_reference)
    if item.get("membership_assertions") is not None:
        item_metadata["membership_assertions"] = deepcopy(item["membership_assertions"])
    if item.get("pool_membership_evidence") is not None:
        item_metadata["pool_membership_evidence"] = deepcopy(item["pool_membership_evidence"])
    item_metadata.setdefault("observed_requirement_ids", (row.get("requirement_id") or row.get("id"),))
    row["source_reference"] = row_reference
    row["source_url"] = row_source.get("source_url", "")
    row["source_file"] = row_source.get("source_file", "")
    row["research_file"] = row_source.get("research_file", "")
    row["pdf_page"] = row_source.get("pdf_page", "未標示")
    row["printed_page"] = row_source.get("printed_page", "未標示")
    row["page"] = row["pdf_page"]
    row["pages"] = row_source.get("pages", "未標示")
    row["table_location"] = row_source.get("table_location", "官方次修課程表")
    row["original_clause"] = row_source.get("original_clause", "")
    row["original_text"] = row["original_clause"]
    row["source_pdf_sha256"] = row_source.get("source_pdf_sha256", "")
    row["source_catalog_file"] = row_source.get("source_catalog_file", "")
    row["source"] = {
        **deepcopy(dict(row.get("source") or {})),
        **row_source,
        "source_reference": row_reference,
        "raw_title": name,
    }
    provenance = dict(row.get("provenance") or {})
    provenance.update(
        {
            "source_type": "official_handbook",
            "source_reference": row_reference,
            "source_pdf_sha256": row_source.get("source_pdf_sha256", ""),
            "source_catalog_file": row_source.get("source_catalog_file", ""),
            "raw_title": name,
            "original_clause": row["original_clause"],
            "membership_ids": membership_ids,
            "membership_source_reference": row_reference,
            "evidence_state": row.get("evidence_state", VERIFIED),
            "verification_status": row.get("verification_status", VERIFIED),
            "coverage_state": row.get("coverage_state", COMPLETE),
        }
    )
    for key in (
        "source_row",
        "source_table",
        "source_track",
        "source_track_slug",
        "source_category",
        "original_course_name",
        "revision_evidence",
        "membership_assertions",
        "pool_membership_evidence",
        "observed_requirement_ids",
        "requires_server_owned_offering",
        "math_secondary_composite_required",
        "secondary_course_key",
    ):
        if key in item_metadata:
            row[key] = deepcopy(item_metadata[key])
            provenance[key] = deepcopy(item_metadata[key])
    row["membership_ids"] = membership_ids
    row["membership_source_reference"] = row_reference
    row["provenance"] = provenance
    # Candidate pools are projected from the requirement rows.  Preserve
    # per-course metadata here so an Earth union or Math composite candidate
    # cannot lose its source track or official-membership contract.
    names = tuple(dict.fromkeys(str(value) for value in candidate_names if str(value).strip()))
    if not names:
        options = item.get("options", ())
        if isinstance(options, Mapping):
            names = tuple(str(value) for value in options if str(value).strip())
        elif isinstance(options, Sequence) and not isinstance(options, (str, bytes, bytearray)):
            names = tuple(
                str(option.get("name"))
                for option in options
                if isinstance(option, Mapping) and option.get("name")
            )
        if not names and row.get("requirement_type") in {"course_pool", "credit_quota"}:
            names = tuple(str(value) for value in row.get("eligible_course_names", ()) if str(value).strip())
    candidate_metadata: dict[str, dict[str, Any]] = {}
    for candidate_name in names:
        candidate_metadata[candidate_name] = deepcopy(item_metadata)
        candidate_metadata[candidate_name]["membership_ids"] = membership_ids
        candidate_metadata[candidate_name]["membership_source_reference"] = row_reference
    if candidate_metadata:
        row["candidate_metadata"] = candidate_metadata
    return row


def _secondary_target_row(
    cohort: str,
    program: str,
    track: str | None,
    item: Mapping[str, Any],
    *,
    role: str = "double_major",
    bucket: str,
    pool_ids: tuple[str, ...] = (),
    eligible_pool_ids: tuple[str, ...] = (),
    requirement_type: str = "named_course",
    choice_group: str | None = None,
    choice_rule: str = "exact_course_or_approved_equivalency",
    candidate_names: Sequence[str] = (),
    candidate_options: Sequence[tuple[str, int | float]] = (),
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a target named/quota row without reusing a primary requirement ID."""

    name = str(item.get("name") or "").strip()
    credits = float(item.get("credits", 0) or 0)
    slug = str(item.get("slug") or _pool_token(name))
    row = _minor_row(
        cohort,
        program,
        track,
        slug,
        name,
        credits,
        component=str(item.get("component") or ("lab" if "實驗" in name else "lecture")),
        eligible_names=tuple(candidate_names) if candidate_names else ((name,) if requirement_type == "named_course" else ()),
        eligible_options=tuple(candidate_options) if candidate_options else None,
        requirement_type=requirement_type,
        choice_group=choice_group,
        choice_rule=choice_rule,
        pool_ids=pool_ids,
        eligible_pool_ids=eligible_pool_ids,
        coverage_state=COMPLETE,
        evidence_state=VERIFIED,
        original_clause=str(item.get("original_clause") or f"{name} {credits:g} 學分。"),
    )
    target_track = track or "department"
    row_id = f"{program}.dm.{cohort}.{target_track}.{slug}"
    row.update(
        {
            "id": row_id,
            "requirement_id": row_id,
            "source_assertion_id": row_id,
            "assertion_id": row_id,
            "kind": "course" if requirement_type == "named_course" else "quota" if requirement_type == "credit_quota" else "choice",
            "bucket": bucket,
            "track_slug": target_track,
            "track": track,
            "program_slug": program,
            "cohort": cohort,
            "curriculum_version": cohort,
            "pool_requirement": requirement_type in {"choice", "course_pool", "credit_quota"},
            "required_credits": credits if requirement_type in {"choice", "course_pool", "credit_quota"} else None,
            "official_course_identity": f"{cohort}:{program}:{target_track}:{slug}",
            "manual_reason": "",
            "manual_review_reason": "",
            "automation_sufficiency": "COMPLETE",
            "automatic_decision": True,
            "verification_status": VERIFIED,
            "coverage_state": COMPLETE,
        }
    )
    if isinstance(row.get("provenance"), dict):
        row["provenance"]["assertion_id"] = row_id
    return _apply_secondary_item(
        row,
        item,
        source,
        role=role,
        program=program,
        cohort=cohort,
        track=track,
        candidate_names=candidate_names or ((name,) if requirement_type == "named_course" else ()),
    )


def _secondary_items(contract: Mapping[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    """Return source rows from one role without coercing aggregate values."""

    values = contract.get(key, ()) if isinstance(contract, Mapping) else ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    return tuple(
        deepcopy(dict(item))
        for item in values
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    )


def _secondary_item_pairs(items: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, float], ...]:
    """Build stable name/credit options while retaining the first source row."""

    pairs: list[tuple[str, float]] = []
    seen: set[str] = set()
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        try:
            credits = float(item.get("credits", 0) or 0)
        except (TypeError, ValueError):
            continue
        if credits <= 0:
            continue
        seen.add(name)
        pairs.append((name, credits))
    return tuple(pairs)


def _secondary_candidate_metadata(
    cohort: str,
    program: str,
    role: str,
    track: str | None,
    source: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    membership_id: str | None = None,
    membership_assertion: str | None = None,
    source_category: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Normalize per-candidate metadata for a source-backed secondary pool."""

    result: dict[str, dict[str, Any]] = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        metadata = deepcopy(dict(item))
        row_source = _secondary_item_source(
            source,
            item,
            name=name,
            credits=float(item.get("credits", 0) or 0),
        )
        metadata.setdefault("source_reference", row_source["source_reference"])
        metadata.setdefault("source_pdf_sha256", row_source.get("source_pdf_sha256", ""))
        metadata.setdefault("source_catalog_file", row_source.get("source_catalog_file", ""))
        metadata.setdefault("membership_source_reference", row_source["source_reference"])
        if membership_id:
            metadata.setdefault("membership_ids", (membership_id,))
        elif not metadata.get("membership_ids"):
            metadata["membership_ids"] = (
                f"{program}-secondary:{cohort}:{role}:{_pool_token(name)}",
            )
        if program == "math":
            metadata["membership_ids"] = (
                f"math-secondary:{cohort}:{_pool_token(name)}",
                f"math-secondary:{cohort}:eligible-target-credit",
            )
        if membership_assertion:
            metadata.setdefault("membership_assertions", (membership_assertion,))
        if source_category:
            metadata.setdefault("source_category", source_category)
        metadata.setdefault("source_program_slug", program)
        metadata.setdefault("source_track_slug", track or "department")
        metadata.setdefault("observed_requirement_ids", ())
        result[name] = metadata
    return result


def _secondary_pool_row(
    row: dict[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    cohort: str,
    program: str,
    role: str,
    track: str | None,
    source: Mapping[str, Any],
    membership_id: str | None = None,
    membership_assertion: str | None = None,
    source_category: str | None = None,
) -> dict[str, Any]:
    """Attach exact source candidates to an already-built quota/choice row."""

    pairs = _secondary_item_pairs(items)
    row["eligible_course_names"] = tuple(name for name, _credits in pairs)
    row["eligible_course_options"] = tuple(
        {"name": name, "credits": credits} for name, credits in pairs
    )
    row["candidate_metadata"] = _secondary_candidate_metadata(
        cohort,
        program,
        role,
        track,
        source,
        items,
        membership_id=membership_id,
        membership_assertion=membership_assertion,
        source_category=source_category,
    )
    row["candidate_only"] = False
    row["pool_requirement"] = True
    row["coverage_state"] = COMPLETE
    row["evidence_state"] = VERIFIED
    row["verification_status"] = VERIFIED
    row["automation_sufficiency"] = "COMPLETE"
    row["automatic_decision"] = True
    row["manual_reason"] = ""
    row["manual_review_reason"] = ""
    return row


def _secondary_aggregate_item(
    name: str,
    credits: int | float,
    *,
    source_reference: str,
    original_clause: str,
    source_table: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "credits": float(credits),
        "component": "quota",
        "source_reference": source_reference,
        "original_clause": original_clause,
        "source_table": source_table,
    }


def _math_secondary_catalog_rows(
    cohort: str,
    role: str,
    track: str | None = None,
) -> list[dict[str, Any]]:
    """Build Math secondary rows from the exact year-scoped source contract."""

    contract = _secondary_catalog(cohort, "math", role)
    source = _secondary_source(cohort, "math", None, role, contract)
    rows: list[dict[str, Any]] = []
    required_items = _secondary_items(contract, "required")
    for item in required_items:
        name = str(item["name"])
        credits = float(item.get("credits", 0) or 0)
        if role == "double_major":
            row = _secondary_target_row(
                cohort,
                "math",
                None,
                item,
                bucket="required",
                pool_ids=(_pool_id("double_major_target", cohort, "math", None, "required"),),
                requirement_type="named_course",
                source=source,
            )
        else:
            row = _minor_row(
                cohort,
                "math",
                None,
                str(item.get("slug") or _pool_token(name)),
                name,
                credits,
                requirement_type="named_course",
                waiver=bool(item.get("waiver_allowed", False)),
                coverage_state=COMPLETE,
                original_clause=str(item.get("original_clause") or f"{name} {credits:g} 學分。"),
            )
            _apply_secondary_item(
                row,
                item,
                source,
                role=role,
                program="math",
                cohort=cohort,
                track=None,
                candidate_names=(name,),
            )
        rows.append(row)

    elective_items = _secondary_items(contract, "electives")
    software = contract.get("software_choice", {})
    software_names = {
        str(value).strip()
        for value in (software.get("members", ()) if isinstance(software, Mapping) else ())
        if str(value).strip()
    }
    min_key = "elective_minimum"
    max_key = "elective_maximum"
    elective_minimum = float(contract.get(min_key, 0) or 0)
    elective_maximum = float(contract.get(max_key, 0) or 0)
    if role == "double_major":
        pool_id = _pool_id("double_major_target", cohort, "math", None, "elective")
        aggregate_item = _secondary_aggregate_item(
            "數學表列選修",
            elective_minimum,
            source_reference=f"{source.get('source_reference', 'handbook')}:row:elective_pool",
            original_clause=f"表列選修至少 {elective_minimum:g} 學分，最多 {elective_maximum:g} 學分。",
            source_table="double_major_elective",
        )
        row = _secondary_target_row(
            cohort,
            "math",
            None,
            aggregate_item,
            bucket="elective",
            eligible_pool_ids=(pool_id,),
            requirement_type="credit_quota",
            choice_rule="official_math_secondary_elective_pool",
            candidate_names=tuple(name for name, _credits in _secondary_item_pairs(elective_items)),
            candidate_options=_secondary_item_pairs(elective_items),
            source=source,
        )
    else:
        pool_id = _pool_id("minor_target", cohort, "math", None, "elective")
        aggregate_item = _secondary_aggregate_item(
            "數學表列選修",
            elective_minimum,
            source_reference=f"{source.get('source_reference', 'handbook')}:row:elective_pool",
            original_clause=f"表列選修至少 {elective_minimum:g} 學分，最多 {elective_maximum:g} 學分。",
            source_table="minor_elective",
        )
        row = _minor_row(
            cohort,
            "math",
            None,
            "elective_pool",
            "數學表列選修",
            elective_minimum,
            requirement_type="credit_quota",
            eligible_pool_ids=(pool_id,),
            choice_rule="official_math_secondary_elective_pool",
            eligible_names=tuple(name for name, _credits in _secondary_item_pairs(elective_items)),
            eligible_options=_secondary_item_pairs(elective_items),
            coverage_state=COMPLETE,
            original_clause=aggregate_item["original_clause"],
        )
        _apply_secondary_item(
            row,
            aggregate_item,
            source,
            role=role,
            program="math",
            cohort=cohort,
            track=None,
            candidate_names=tuple(name for name, _credits in _secondary_item_pairs(elective_items)),
        )
    # Required rows consume their own named pool; the elective quota is the
    # sole credit consumer for the elective candidate set.  Software choices
    # remain metadata on the same pool, so they cannot double-consume credits.
    row["required_credits"] = float(row.get("credits", 0) or 0)
    row["requirement_minimum_credits"] = elective_minimum
    row["max_credits"] = elective_maximum
    row["subset_maxima"] = {
        "math_software": {
            "max_courses": int(software.get("max_courses", 1)) if isinstance(software, Mapping) else 1,
            "max_credits": float(software.get("max_credits", 3) or 3) if isinstance(software, Mapping) else 3.0,
            "members": tuple(sorted(software_names)),
        }
    }
    row["choice_group"] = "math_secondary_elective"
    row["choice_rule"] = "official_math_secondary_elective_pool; software_choice_max_3"
    # The software courses are part of the same official elective pool.  The
    # one-course/three-credit software limit is a subset constraint on this
    # pool; dropping those rows here would make the documented choice
    # impossible and would silently shrink the candidate catalogue.
    all_candidate_items = elective_items
    _secondary_pool_row(
        row,
        all_candidate_items,
        cohort=cohort,
        program="math",
        role=role,
        track=None,
        source=source,
        source_category="math_secondary_elective",
    )

    # Keep the two source-backed subset observations on this one elective
    # quota.  They observe already allocated EXCLUSIVE portions and never
    # create another credit consumer.  The service adapter may canonicalize
    # the raw requirement IDs; retain the registry IDs here so the evidence
    # remains auditable at the source boundary.
    elective_requirement_id = str(row.get("requirement_id") or row.get("id"))
    observed_requirement_ids = tuple(
        str(item.get("requirement_id") or item.get("id"))
        for item in (*rows, row)
        if str(item.get("requirement_id") or item.get("id"))
    )
    source_reference = str(source.get("source_reference") or f"handbook:{cohort}:math:secondary")
    software_membership_id = f"math-secondary:{cohort}:software"
    total_minimum = 20.0 if role == "minor" else 40.0
    row["subset_constraints"] = (
        {
            "constraint_id": f"math-secondary:{cohort}:{role}:software-max",
            "membership_id": software_membership_id,
            "amount_semantics": "MAXIMUM",
            "maximum_credits": float(
                software.get("max_credits", 3) if isinstance(software, Mapping) else 3
            ),
            "maximum_course_count": int(
                software.get("max_courses", 1) if isinstance(software, Mapping) else 1
            ),
            "observed_requirement_ids": (elective_requirement_id,),
            "source_reference": f"{source_reference}:row:software_choice",
        },
        {
            "constraint_id": f"math-secondary:{cohort}:{role}:total",
            "membership_id": f"math-secondary:{cohort}:eligible-target-credit",
            "amount_semantics": "MINIMUM",
            "minimum_credits": total_minimum,
            "observed_requirement_ids": observed_requirement_ids,
            "source_reference": f"{source_reference}:row:total{int(total_minimum)}",
        },
    )

    # The printed secondary table establishes the software subset, while the
    # official offering evidence establishes each candidate's target credit
    # membership.  Record an explicit positive/negative assertion for every
    # observed candidate so two software courses cannot be treated as one
    # course, and an ordinary elective cannot silently count toward the cap.
    candidate_metadata = row.get("candidate_metadata")
    if isinstance(candidate_metadata, Mapping):
        for candidate_name, metadata in candidate_metadata.items():
            if not isinstance(metadata, dict):
                continue
            candidate_source = str(metadata.get("source_reference") or source_reference)
            is_software = str(candidate_name) in software_names
            software_assertion = {
                "membership_id": software_membership_id,
                "state": VERIFIED if is_software else "NOT_MEMBER",
                "kind": "official_handbook_membership" if is_software else "registry_negative",
                "source_reference": f"{candidate_source}:software_subset",
                "observed_requirement_ids": (elective_requirement_id,),
            }
            prior_assertions = metadata.get("membership_assertions", ())
            if isinstance(prior_assertions, (str, bytes, bytearray)):
                prior_assertions = (prior_assertions,)
            elif not isinstance(prior_assertions, Sequence):
                prior_assertions = ()
            metadata["membership_assertions"] = tuple(
                (*prior_assertions, software_assertion)
            )
            if is_software:
                prior_membership_ids = metadata.get("membership_ids", ())
                if isinstance(prior_membership_ids, (str, bytes, bytearray)):
                    prior_membership_ids = (prior_membership_ids,)
                elif not isinstance(prior_membership_ids, Sequence):
                    prior_membership_ids = ()
                metadata["membership_ids"] = tuple(
                    dict.fromkeys((*prior_membership_ids, software_membership_id))
                )
            prior_evidence = metadata.get("pool_membership_evidence", ())
            if isinstance(prior_evidence, (str, bytes, bytearray)) or not isinstance(
                prior_evidence, Sequence
            ):
                prior_evidence = ()
            metadata["pool_membership_evidence"] = tuple(
                (*prior_evidence, {
                    "membership_id": software_membership_id,
                    "state": VERIFIED if is_software else "NOT_MEMBER",
                    "kind": "official_handbook_membership" if is_software else "registry_negative",
                    "source_reference": f"{candidate_source}:software_subset",
                })
            )
    rows.append(row)
    return rows


def _earth_secondary_catalog_rows(
    cohort: str,
    role: str,
    track: str | None = None,
) -> list[dict[str, Any]]:
    contract = _secondary_catalog(cohort, "earth", role)
    source = _secondary_source(cohort, "earth", None, role, contract)
    rows: list[dict[str, Any]] = []
    base_items = _secondary_items(contract, "required" if role == "minor" else "base")
    for index, item in enumerate(base_items):
        name = str(item["name"])
        credits = float(item.get("credits", 0) or 0)
        requirement_type = str(item.get("requirement_type") or "named_course")
        raw_options = item.get("options", ())
        option_pairs = tuple(
            (str(option.get("name")), float(option.get("credits", credits) or credits))
            for option in raw_options
            if isinstance(option, Mapping) and str(option.get("name") or "").strip()
        ) if isinstance(raw_options, Sequence) and not isinstance(raw_options, (str, bytes, bytearray)) else ()
        eligible_names = tuple(option_name for option_name, _option_credits in option_pairs) or (name,)
        if role == "minor":
            row = _minor_row(
                cohort,
                "earth",
                None,
                str(item.get("slug") or f"common_{index}"),
                name,
                credits,
                component=str(item.get("component") or ("lab" if "實驗" in name else "lecture")),
                eligible_names=eligible_names,
                eligible_options=option_pairs or None,
                requirement_type=requirement_type,
                choice_group=str(item.get("choice_group") or "") or None,
                choice_rule=str(item.get("choice_rule") or "擇一"),
                coverage_state=COMPLETE,
                original_clause=str(item.get("original_clause") or f"{name} {credits:g} 學分。"),
            )
            _apply_secondary_item(
                row,
                item,
                source,
                role=role,
                program="earth",
                cohort=cohort,
                track=None,
                candidate_names=eligible_names,
            )
        else:
            target_pool = _pool_id("double_major_target", cohort, "earth", track, "base")
            row = _secondary_target_row(
                cohort,
                "earth",
                track,
                item,
                bucket="base",
                pool_ids=(target_pool,) if requirement_type == "named_course" else (),
                eligible_pool_ids=(target_pool,) if requirement_type != "named_course" else (),
                requirement_type=requirement_type,
                choice_group=str(item.get("choice_group") or "") or None,
                choice_rule=str(item.get("choice_rule") or "擇一"),
                candidate_names=eligible_names if requirement_type != "named_course" else (),
                candidate_options=option_pairs,
                source=source,
            )
        rows.append(row)
    if role == "double_major":
        elective_items = _secondary_items(contract, "domain_electives")
        pool_id = _pool_id("double_major_target", cohort, "earth", track, "domain_elective")
        pairs = _secondary_item_pairs(elective_items)
        aggregate_item = _secondary_aggregate_item(
            "兩領域專業選修",
            float(contract.get("other_minimum", 16) or 16),
        source_reference=f"{source.get('source_reference', 'handbook')}:row:domain_elective",
            original_clause="地球環境與生命科學兩領域專業選修合計至少 16 學分。",
            source_table="earth_environment_and_life_science.domain_elective",
        )
        row = _secondary_target_row(
            cohort,
            "earth",
            track,
            aggregate_item,
            bucket="domain_elective",
            eligible_pool_ids=(pool_id,),
            requirement_type="credit_quota",
            choice_rule="earth_union_of_both_domain_elective_pools",
            candidate_names=tuple(name for name, _credits in pairs),
            candidate_options=pairs,
            source=source,
        )
        row["required_credits"] = float(contract.get("other_minimum", 16) or 16)
        row["requirement_minimum_credits"] = float(contract.get("other_minimum", 16) or 16)
        row["secondary_domain_scope"] = "earth_environment_and_life_science"
        _secondary_pool_row(
            row,
            elective_items,
            cohort=cohort,
            program="earth",
            role=role,
            track=track,
            source=source,
            membership_id=f"earth-secondary:{cohort}:domain-elective",
            source_category="domain_elective",
        )
        for name, metadata in row.get("candidate_metadata", {}).items():
            source_track = str(metadata.get("source_track") or metadata.get("source_track_slug") or "")
            if source_track:
                metadata["source_track_slug"] = source_track
                metadata["membership_ids"] = (
                    f"earth-secondary:{cohort}:domain-elective",
                    f"earth-secondary:{cohort}:{source_track}:domain-elective",
                )
                metadata["observed_requirement_ids"] = (
                    f"earth.primary.{cohort}.{source_track}.domain_elective",
                )
        rows.append(row)
    return rows


def _cs_secondary_catalog_rows(cohort: str, role: str) -> list[dict[str, Any]]:
    contract = _secondary_catalog(cohort, "cs", role)
    source = _secondary_source(cohort, "cs", None, role, contract)
    rows: list[dict[str, Any]] = []
    required_items = _secondary_items(contract, "required")
    for item in required_items:
        name = str(item["name"])
        credits = float(item.get("credits", 0) or 0)
        if role == "minor":
            row = _minor_row(
                cohort,
                "cs",
                None,
                str(item.get("slug") or _pool_token(name)),
                name,
                credits,
                coverage_state=COMPLETE,
                original_clause=str(item.get("original_clause") or f"{name} {credits:g} 學分。"),
            )
            _apply_secondary_item(
                row,
                item,
                source,
                role=role,
                program="cs",
                cohort=cohort,
                track=None,
                candidate_names=(name,),
            )
        else:
            row = _secondary_target_row(
                cohort,
                "cs",
                None,
                item,
                bucket="required",
                pool_ids=(_pool_id("double_major_target", cohort, "cs", None, "required_named"),),
                source=source,
            )
        rows.append(row)
    other_items = _secondary_items(contract, "other")
    other_minimum = float(contract.get("other_minimum", 0) or 0)
    row_name = "資科系其他開設課程" if role == "minor" else "資科雙主修其他開設課程"
    aggregate_item = _secondary_aggregate_item(
        row_name,
        other_minimum,
        source_reference=f"{source.get('source_reference', 'handbook')}:row:other",
        original_clause=f"官方資科系其他開設課程至少 {other_minimum:g} 學分。",
        source_table="department_courses",
    )
    if role == "minor":
        pool_id = _pool_id("minor_target", cohort, "cs", None, "quota")
        row = _minor_row(
            cohort,
            "cs",
            None,
            "other_cs_offerings",
            row_name,
            other_minimum,
            requirement_type="credit_quota",
            eligible_pool_ids=(pool_id,),
            choice_rule="server_owned_official_cs_offering_without_named_overlap",
            coverage_state=COMPLETE,
            original_clause=aggregate_item["original_clause"],
        )
        _apply_secondary_item(
            row,
            aggregate_item,
            source,
            role=role,
            program="cs",
            cohort=cohort,
            track=None,
            candidate_names=tuple(name for name, _credits in _secondary_item_pairs(other_items)),
        )
    else:
        pool_id = _pool_id("double_major_target", cohort, "cs", None, "other_required")
        row = _secondary_target_row(
            cohort,
            "cs",
            None,
            aggregate_item,
            bucket="other_required",
            eligible_pool_ids=(pool_id,),
            requirement_type="credit_quota",
            choice_rule="server_owned_official_cs_offering_without_named_overlap",
            candidate_names=tuple(name for name, _credits in _secondary_item_pairs(other_items)),
            candidate_options=_secondary_item_pairs(other_items),
            source=source,
        )
    row["required_credits"] = other_minimum
    row["requirement_minimum_credits"] = other_minimum
    row["requires_server_owned_offering"] = True
    _secondary_pool_row(
        row,
        other_items,
        cohort=cohort,
        program="cs",
        role=role,
        track=None,
        source=source,
        membership_id=f"cs-secondary:{cohort}:other",
        membership_assertion=f"cs.secondary.{cohort}.official_department_offering",
        source_category="department_courses",
    )
    row["requires_server_owned_offering"] = True
    rows.append(row)
    return rows


def _apc_secondary_catalog_rows(cohort: str, track: str, role: str) -> list[dict[str, Any]]:
    contract = _secondary_catalog(cohort, "apc", role, track)
    # The APC accessor is track-aware in the config; _secondary_catalog has
    # already selected the role but the source remains the exact track page.
    source = _secondary_source(cohort, "apc", track, role, contract)
    rows: list[dict[str, Any]] = []
    base_items = _secondary_items(contract, "base")
    base_pool = _pool_id(
        "minor_target" if role == "minor" else "double_major_target",
        cohort,
        "apc",
        track,
        "base",
    )
    for index, item in enumerate(base_items):
        name = str(item["name"])
        credits = float(item.get("credits", 0) or 0)
        if role == "minor":
            row = _minor_row(
                cohort,
                "apc",
                track,
                str(item.get("slug") or f"base_{index}"),
                name,
                credits,
                component=str(item.get("component") or ("lab" if "實驗" in name else "lecture")),
                coverage_state=COMPLETE,
                original_clause=str(item.get("original_clause") or f"{name} {credits:g} 學分。"),
            )
            _apply_secondary_item(
                row,
                item,
                source,
                role=role,
                program="apc",
                cohort=cohort,
                track=track,
                candidate_names=(name,),
            )
        else:
            row = _secondary_target_row(
                cohort,
                "apc",
                track,
                item,
                bucket="base",
                pool_ids=(base_pool,),
                source=source,
            )
        rows.append(row)
    remainder_items = _secondary_items(contract, "remainder")
    remainder_minimum = float(contract.get("remainder_minimum", 0) or 0)
    if remainder_minimum > 0:
        pairs = _secondary_item_pairs(remainder_items)
        name = "同組主修必修剩餘課程"
        aggregate_item = _secondary_aggregate_item(
            name,
            remainder_minimum,
            source_reference=f"{source.get('source_reference', 'handbook')}:row:remainder_required",
            original_clause=f"同組主修必修剩餘課程至少 {remainder_minimum:g} 學分。",
            source_table="primary_track_compulsory_remainder",
        )
        pool_bucket = "remainder_required"
        pool_id = _pool_id(
            "minor_target" if role == "minor" else "double_major_target",
            cohort,
            "apc",
            track,
            pool_bucket,
        )
        if role == "minor":
            row = _minor_row(
                cohort,
                "apc",
                track,
                "remainder_required",
                name,
                remainder_minimum,
                requirement_type="credit_quota",
                eligible_pool_ids=(pool_id,),
                eligible_names=tuple(item_name for item_name, _credits in pairs),
                eligible_options=pairs,
                choice_rule="same_track_primary_required_remainder",
                coverage_state=COMPLETE,
                original_clause=aggregate_item["original_clause"],
            )
            _apply_secondary_item(
                row,
                aggregate_item,
                source,
                role=role,
                program="apc",
                cohort=cohort,
                track=track,
                candidate_names=tuple(item_name for item_name, _credits in pairs),
            )
        else:
            row = _secondary_target_row(
                cohort,
                "apc",
                track,
                aggregate_item,
                bucket="remainder_required",
                eligible_pool_ids=(pool_id,),
                requirement_type="credit_quota",
                choice_rule="same_track_primary_required_remainder",
                candidate_names=tuple(item_name for item_name, _credits in pairs),
                candidate_options=pairs,
                source=source,
            )
        row["required_credits"] = remainder_minimum
        row["requirement_minimum_credits"] = remainder_minimum
        row["source_track_slug"] = track
        _secondary_pool_row(
            row,
            remainder_items,
            cohort=cohort,
            program="apc",
            role=role,
            track=track,
            source=source,
            membership_id=f"apc-secondary:{cohort}:{track}:primary-required-remainder",
            membership_assertion=f"apc.primary.{cohort}.{track}.track_required",
            source_category="primary_track_compulsory_remainder",
        )
        row["source_track_slug"] = track
        rows.append(row)
    return rows

# The APC chemistry double-major table is one of the few target tables that
# can be transcribed safely from the checked-in handbook page images.  Keep
# this list separate from ``rules_config.json``: that file is an older
# aggregate/primary-rule source and must not make a neighbouring cohort's
# rows appear in this target catalogue.
_APC_CHEMISTRY_DM_ROWS = {
    "111": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "112": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "113": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "114": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "115": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("微積分(一)", 3.0, "lecture", "calculus_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
        ("微積分(二)", 3.0, "lecture", "calculus_2"),
    ),
}
_APC_PHYSICS_DM_ROWS = {
    "111": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "112": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "113": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "114": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "lab", "chemistry_lab_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "lab", "chemistry_lab_2"),
    ),
    "115": (
        ("普通物理學(一)", 3.0, "lecture", "physics_1"),
        ("普通化學(一)", 3.0, "lecture", "chemistry_1"),
        ("普通物理實驗(一)", 1.0, "lab", "physics_lab_1"),
        ("微積分(一)", 3.0, "lecture", "calculus_1"),
        ("普通物理學(二)", 3.0, "lecture", "physics_2"),
        ("普通化學(二)", 3.0, "lecture", "chemistry_2"),
        ("普通物理實驗(二)", 1.0, "lab", "physics_lab_2"),
        ("微積分(二)", 3.0, "lecture", "calculus_2"),
    ),
}
_APC_CHEMISTRY_DM_PAGE = {
    "111": {"pdf_page": 19, "printed_page": 18},
    "112": {"pdf_page": 19, "printed_page": 18},
    "113": {"pdf_page": 24, "printed_page": 23},
    "114": {"pdf_page": 24, "printed_page": 23},
    "115": {"pdf_page": 25, "printed_page": 24},
}
_APC_PHYSICS_DM_PAGE = {
    "111": {"pdf_page": 10, "printed_page": 9},
    "112": {"pdf_page": 10, "printed_page": 9},
    "113": {"pdf_page": 11, "printed_page": 10},
    "114": {"pdf_page": "11–12", "printed_page": "10–11"},
    "115": {"pdf_page": "12–13", "printed_page": "11–12"},
}
_SLUG_ALIASES = {
    "earth": "earth",
    "地生": "earth",
    "earth_environment": "earth_environment",
    "environment": "earth_environment",
    "地球環境": "earth_environment",
    "life_science": "life_science",
    "life": "life_science",
    "生命科學": "life_science",
    "apc": "apc",
    "物化": "apc",
    "physics": "physics",
    "電子物理": "physics",
    "chemistry": "chemistry",
    "應用化學": "chemistry",
    "cs": "cs",
    "資科": "cs",
    "math": "math",
    "數學": "math",
    "math_scientific_computing": "math_scientific_computing",
    "data_science": "data_science",
    "math_education": "math_education",
}


def _load_rules() -> dict[str, Any]:
    try:
        with open(_RULES_PATH, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


_RULES = _load_rules()


def _source_file(cohort: str) -> str:
    return "3-理學院.pdf" if cohort == "111" else f"3-理學院 ({cohort}).pdf"


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().replace("（", "(").replace("）", ")")


def _normalize_cohort(value: Any) -> str | None:
    text = _normalize_text(value).replace("學年度", "").replace("學年", "")
    match = re.search(r"(?<!\d)(11[1-5])(?!\d)", text)
    return match.group(1) if match else None


def _program_slug(value: Any) -> str | None:
    text = _normalize_text(value)
    if text in _PROGRAMS:
        return _PROGRAMS[text]
    compact = text.replace(" ", "").replace("　", "")
    if "地球環境" in compact or compact.startswith("地生") or "生命科學" in compact:
        return "earth"
    if "資科" in compact or "資訊科學" in compact:
        return "cs"
    if "數學" in compact:
        return "math"
    if "物化" in compact or "應用化學" in compact or "應用物理" in compact or "電子物理" in compact:
        return "apc"
    alias = _SLUG_ALIASES.get(compact.lower())
    # Track slugs are not programs.  Keeping this distinction here prevents
    # a later default from turning ``chemistry`` into APC physics (or an
    # Earth track into a different primary curriculum).
    return alias if alias in _PROGRAM_SLUGS else None


def _track_slug(value: Any, program: str | None = None) -> str | None:
    text = _normalize_text(value)
    compact = text.replace(" ", "").replace("　", "")
    canonical = compact.lower()
    if canonical in _TRACK_SLUGS:
        candidate = canonical
        if program is None or _TRACK_PROGRAMS[candidate] == program:
            return candidate
        return None
    for slug, aliases in _TRACKS.items():
        if compact in {item.replace(" ", "").replace("　", "") for item in aliases}:
            if program is None or _TRACK_PROGRAMS[slug] == program:
                return slug
            return None
    if "地球環境" in compact:
        candidate = "earth_environment"
        return candidate if program in (None, _TRACK_PROGRAMS[candidate]) else None
    if "生命科學" in compact:
        candidate = "life_science"
        return candidate if program in (None, _TRACK_PROGRAMS[candidate]) else None
    if "物理" in compact or "電子物理" in compact:
        candidate = "physics"
        return candidate if program in (None, _TRACK_PROGRAMS[candidate]) else None
    if "化學" in compact or "應用化學" in compact:
        candidate = "chemistry"
        return candidate if program in (None, _TRACK_PROGRAMS[candidate]) else None
    if program == "earth" and not compact:
        return "earth_environment"
    return None


def _canonical_id(kind: str, cohort: str, program: str, track: str | None = None) -> str:
    if kind == "primary":
        prefix = "primary"
    elif kind in {_MINOR_TARGET_ROLE, "minor", "minor_target"}:
        prefix = "minor"
    else:
        prefix = "target:double_major"
    parts = [prefix, cohort, program]
    if kind in {_MINOR_TARGET_ROLE, "minor", "minor_target"}:
        # Minor scopes are department-level for Earth/CS/Math and track-level
        # only for APC.  ``department`` is part of the stable public ID.
        parts.append(track or "department")
    elif track and (
        program in {"earth", "apc"}
        or (
            program == "math"
            and kind == "primary"
            and track in _MATH_PRIMARY_TRACKS.get(str(cohort), ())
        )
    ):
        parts.append(track)
    return ":".join(parts)


def _citation(cohort: str, pages: str, label: str, *, url: str | None = None) -> dict[str, str]:
    return {
        "file": _source_file(cohort),
        "pages": pages,
        "label": label,
        "url": url or _HANDBOOK_URLS.get(cohort, ""),
    }


def _apc_target_rows(cohort: str, track: str) -> tuple[tuple[str, float, str, str], ...]:
    if track == "chemistry":
        return _APC_CHEMISTRY_DM_ROWS[cohort]
    if track == "physics":
        return _APC_PHYSICS_DM_ROWS[cohort]
    raise KeyError(f"unsupported APC target track: {track}")


def _apc_target_page(cohort: str, track: str) -> dict[str, Any]:
    pages = _APC_CHEMISTRY_DM_PAGE if track == "chemistry" else _APC_PHYSICS_DM_PAGE
    page = pages[cohort]
    return {
        **page,
        "pages": f"PDF p.{page['pdf_page']}（印刷 p.{page['printed_page']}）",
        "source_reference": f"handbook:{cohort}:pdf:{page['pdf_page']}",
    }


def _apc_chemistry_page(cohort: str) -> dict[str, Any]:
    return _apc_target_page(cohort, "chemistry")


def _apc_chemistry_row_assertion_id(cohort: str, slug: str) -> str:
    return f"apc.dm.{cohort}.chemistry.{slug}"


def _apc_target_catalog_assertions(cohort: str, track: str) -> list[dict[str, Any]]:
    """Return row-level assertions for a readable APC DM page.

    The footer's generic ``其餘必修`` amount is an official aggregate, but the
    page does not provide a complete named course pool in the local evidence.
    Keep that missing semantic as an assertion instead of treating a guessed
    primary catalogue as the target's course list.
    """

    contract = _secondary_catalog(cohort, "apc", "double_major", track)
    page = _apc_target_page(cohort, track)
    track_label = "電子物理組" if track == "physics" else "應用化學組"
    label = f"{cohort} 學年度物化系{track_label}雙主修表"
    base_items = _secondary_items(contract, "base")
    remainder = float(contract.get("other_minimum", 20 if cohort == "115" else 24) or 0)
    assertions = [
        _assertion(
            f"apc.dm.{cohort}.{track}.{_pool_token(item['name'])}",
            cohort,
            page["pages"],
            f"雙主修列項：{item['name']}",
            float(item.get("credits", 0) or 0),
            label=label,
        )
        for item in base_items
    ]
    assertions.append(
        _assertion(
            f"apc.dm.{cohort}.{track}.other{int(remainder)}",
            cohort,
            page["pages"],
            "同組主修必修剩餘額度",
            remainder,
            label=label,
        )
    )
    return assertions

    page = _apc_target_page(cohort, track)
    track_label = "電子物理組" if track == "physics" else "應用化學組"
    label = f"{cohort} 學年度物化系{track_label}雙主修表"
    assertions: list[dict[str, Any]] = []
    for name, credits, _component, slug in _apc_target_rows(cohort, track):
        assertions.append(
            _assertion(
                f"apc.dm.{cohort}.{track}.{slug}",
                cohort,
                page["pages"],
                f"雙主修列項：{name}",
                credits,
                label=label,
            )
        )
    other_credits = 20 if cohort == "115" else 24
    other_claim = (
        "表尾額外目標必修額度"
        if cohort in {"111", "112"} and track == "chemistry"
        else "表尾其餘必修課程應修畢"
    )
    # The 111/112 chemistry page explicitly gives the additional 24-credit
    # target requirement.  Its named course catalogue is still incomplete,
    # but the aggregate itself is no longer a source conflict.
    other_evidence = VERIFIED
    assertions.append(
        _assertion(
            f"apc.dm.{cohort}.{track}.other_catalog",
            cohort,
            page["pages"],
            "其餘必修課程完整命名與選擇規則",
            "not_transcribed",
            evidence_state=MISSING,
            label=label,
        )
    )
    # Keep the aggregate amount available as an independent official claim;
    # this assertion is also used by the quota row provenance below.
    assertions.append(
        _assertion(
            f"apc.dm.{cohort}.other{other_credits}",
            cohort,
            page["pages"],
            other_claim,
            other_credits,
            evidence_state=other_evidence,
            label=label,
        )
    )
    return assertions


def _apc_chemistry_catalog_assertions(cohort: str) -> list[dict[str, Any]]:
    return _apc_target_catalog_assertions(cohort, "chemistry")


def _apc_target_catalog(cohort: str, track: str) -> list[dict[str, Any]]:
    """Build exact visible APC double-major rows for one cohort/track.

        This intentionally returns the eight named rows plus one generic footer
    quota.  The quota is not expanded with 115 primary/track courses because
    that would be a false claim about the target page's missing choice pool.
    """

    # The source-backed secondary contract now contains the complete base
    # rows and same-track remainder pool for every cohort/track.  Keep the
    # legacy implementation below for reference, but route all callers to
    # the scoped catalogue so no target falls back to an aggregate-only row.
    return _apc_secondary_catalog_rows(cohort, track, "double_major")

    page = _apc_target_page(cohort, track)
    track_label = "電子物理組" if track == "physics" else "應用化學組"
    source = _citation(cohort, page["pages"], f"物化系{track_label}雙主修課程表")
    base_pool_id = _pool_id("double_major_target", cohort, "apc", track, "base")
    other_pool_id = _pool_id("double_major_target", cohort, "apc", track, "other_required")
    rows: list[dict[str, Any]] = []
    for name, credits, component, slug in _apc_target_rows(cohort, track):
        requirement_id = f"apc.dm.{cohort}.{track}.{slug}"
        source_reference = f"{page['source_reference']}:row:{slug}"
        original_clause = f"{track_label}雙主修列項：{name} {credits:g} 學分。"
        provenance = {
            "assertion_id": requirement_id,
            "source_type": "official_handbook",
            "source_file": source["file"],
            "source_url": source["url"],
            "research_file": "research/apc_cs_handbook_matrix_111_115.md",
            "pdf_page": page["pdf_page"],
            "printed_page": page["printed_page"],
            "source_reference": source_reference,
            "table_location": source["label"],
            "raw_title": name,
            "claim": original_clause,
            "original_clause": original_clause,
            "evidence_state": VERIFIED,
            "verification_status": VERIFIED,
            "coverage_state": PARTIAL,
            "automation_sufficiency": PARTIAL,
            "automatic_decision": False,
            "manual_reason": "雙主修其餘必修完整命名課程池與個案核准仍需人工確認。",
            "manual_review_reason": "雙主修其餘必修完整命名課程池與個案核准仍需人工確認。",
            "extraction_method": "handbook_text_and_visual_crosscheck",
        }
        rows.append(
            {
                "id": requirement_id,
                "requirement_id": requirement_id,
                "name": name,
                "raw_title": name,
                "credits": float(credits),
                "bucket": "base",
                "kind": "course",
                "requirement_type": "named_course",
                "choice_group": None,
                "choice_rule": "exact_course_or_approved_equivalency",
                "track": track,
                "track_slug": track,
                "track_name": track_label,
                "cohort": cohort,
                "curriculum_version": cohort,
                "component": component,
                "component_type": component,
                "component_label": "講授" if component == "lecture" else "實驗",
                "lecture_or_lab": component,
                "is_lab": component == "lab",
                "is_zero_credit": False,
                "allow_combined_lab_source": False,
                "pool_ids": (base_pool_id,),
                "eligible_pool_ids": (),
                "overflow_routes": (),
                "candidate_only": False,
                "pool_requirement": False,
                "evidence": VERIFIED,
                "evidence_state": VERIFIED,
                "assertion_id": requirement_id,
                "source_assertion_id": requirement_id,
                "source_reference": source_reference,
                "source_url": source["url"],
                "source_file": source["file"],
                "pdf_page": page["pdf_page"],
                "printed_page": page["printed_page"],
                "page": page["pdf_page"],
                "pages": page["pages"],
                "source": deepcopy(source),
                "provenance": provenance,
                "official_course_identity": f"{cohort}:apc:{track}:{slug}",
                "research_file": "research/apc_cs_handbook_matrix_111_115.md",
                "table_location": source["label"],
                "original_clause": original_clause,
                "original_text": original_clause,
                "verification_status": VERIFIED,
                "coverage_state": PARTIAL,
                "automation_sufficiency": PARTIAL,
                "automatic_decision": False,
                "manual_reason": "雙主修其餘必修完整命名課程池與個案核准仍需人工確認。",
                "manual_review_reason": "雙主修其餘必修完整命名課程池與個案核准仍需人工確認。",
            }
        )

    other_credits = 20.0 if cohort == "115" else 24.0
    quota_slug = "other_required"
    quota_requirement_id = f"apc.dm.{cohort}.{track}.{quota_slug}"
    quota_assertion_id = f"apc.dm.{cohort}.{track}.other{int(other_credits)}"
    quota_reference = f"{page['source_reference']}:footer:{quota_slug}"
    quota_is_additional_target = cohort in {"111", "112"} and track == "chemistry"
    quota_title = "額外目標必修課程" if quota_is_additional_target else "其餘必修課程"
    quota_original_clause = f"{track_label}雙主修表尾：{quota_title} {int(other_credits)} 學分。"
    quota_evidence = VERIFIED
    quota_manual_reason = (
        "官方頁面已核對 16＋24＝40 的 aggregate；24 學分是額外目標必修額度，完整命名目錄與核准 mapping 仍需人工確認。"
        if quota_is_additional_target
        else "官方頁面未在本地證據中提供其餘必修的完整命名課程池。"
    )
    quota_provenance = {
        "assertion_id": quota_assertion_id,
        "source_type": "official_handbook",
        "source_file": source["file"],
        "source_url": source["url"],
        "research_file": "research/apc_cs_handbook_matrix_111_115.md",
        "pdf_page": page["pdf_page"],
        "printed_page": page["printed_page"],
        "source_reference": quota_reference,
        "table_location": source["label"],
        "raw_title": "其餘必修課程",
        "claim": quota_original_clause,
        "original_clause": quota_original_clause,
        "evidence_state": quota_evidence,
        "verification_status": quota_evidence,
        "coverage_state": PARTIAL,
        "automation_sufficiency": PARTIAL,
        "automatic_decision": False,
        "manual_reason": quota_manual_reason,
        "manual_review_reason": quota_manual_reason,
        "extraction_method": "handbook_text_and_visual_crosscheck",
        "named_course_pool": "not_transcribed",
    }
    rows.append(
        {
            "id": quota_requirement_id,
            "requirement_id": quota_requirement_id,
            "name": quota_title,
            "display_name": f"{track_label}{quota_title}",
            "raw_title": quota_title,
            "credits": other_credits,
            "bucket": "other_required",
            "kind": "quota",
            "requirement_type": "credit_quota",
            "choice_group": "other_required_pool",
            "choice_rule": "department_approved_named_course_pool",
            "track": track,
            "track_slug": track,
            "track_name": track_label,
            "cohort": cohort,
            "curriculum_version": cohort,
            "component": "quota",
            "component_type": "quota",
            "component_label": "其餘必修額度",
            "lecture_or_lab": "quota",
            "is_lab": False,
            "is_zero_credit": False,
            "allow_combined_lab_source": False,
            "pool_ids": (),
            "eligible_pool_ids": (other_pool_id,),
            "overflow_routes": (),
            "candidate_only": False,
            "pool_requirement": True,
            "evidence": quota_evidence,
            "evidence_state": quota_evidence,
            "assertion_id": quota_assertion_id,
            "source_assertion_id": quota_assertion_id,
            "source_reference": quota_reference,
            "source_url": source["url"],
            "source_file": source["file"],
            "pdf_page": page["pdf_page"],
            "printed_page": page["printed_page"],
            "page": page["pdf_page"],
            "pages": page["pages"],
            "source": deepcopy(source),
            "provenance": quota_provenance,
            "official_course_identity": f"{cohort}:apc:{track}:{quota_slug}",
            "catalog": [],
            "named_course_pool_state": PARTIAL,
            "research_file": "research/apc_cs_handbook_matrix_111_115.md",
            "table_location": source["label"],
            "original_clause": quota_original_clause,
            "original_text": quota_original_clause,
            "verification_status": quota_evidence,
            "coverage_state": PARTIAL,
            "automation_sufficiency": PARTIAL,
            "automatic_decision": False,
            "manual_reason": quota_manual_reason,
            "manual_review_reason": quota_manual_reason,
            "missing_semantics": [
                "官方頁面未在本地證據中提供可執行的其餘必修完整課名清單。",
                "其餘必修的選擇規則與系所核准條件仍需人工確認。",
            ],
        }
    )
    return rows


def _apc_chemistry_catalog(cohort: str) -> list[dict[str, Any]]:
    return _apc_target_catalog(cohort, "chemistry")


def _assertion(
    assertion_id: str,
    cohort: str,
    pages: str,
    claim: str,
    value: Any,
    *,
    evidence_state: str = VERIFIED,
    label: str = "學生手冊",
    url: str | None = None,
    pdf_page: str | None = None,
    printed_page: str | None = None,
    original_clause: str | None = None,
    source_reference: str | None = None,
    automatic_decision: bool | None = None,
    manual_reason: str | None = None,
    coverage_state: str | None = None,
    conflict_group: str | None = None,
    blocks_decision: bool | None = None,
) -> dict[str, Any]:
    source = _citation(cohort, pages, label, url=url)
    resolved_pdf_page = str(pdf_page or pages).strip()
    resolved_printed_page = str(printed_page or "未標示").strip()
    resolved_coverage = coverage_state or (COMPLETE if evidence_state == VERIFIED else PARTIAL)
    resolved_automatic = (
        automatic_decision
        if automatic_decision is not None
        else evidence_state == VERIFIED and resolved_coverage == COMPLETE
    )
    resolved_manual_reason = manual_reason or (
        "官方證據不足或衝突，需人工確認。" if not resolved_automatic else ""
    )
    resolved_blocks_decision = (
        blocks_decision
        if blocks_decision is not None
        else evidence_state == CONFLICTED
    )
    resolved_original_clause = original_clause or claim
    resolved_reference = source_reference or f"handbook:{cohort}:pdf:{resolved_pdf_page}:assertion:{assertion_id}"
    source.update(
        {
            "curriculum_version": cohort,
            "pdf_page": resolved_pdf_page,
            "printed_page": resolved_printed_page,
            "source_reference": resolved_reference,
            "original_clause": resolved_original_clause,
            "verification_status": evidence_state,
            "coverage_state": resolved_coverage,
            "automation_sufficiency": "COMPLETE" if resolved_automatic else "PARTIAL" if evidence_state == VERIFIED else "NONE",
            "automatic_decision": resolved_automatic,
            "manual_reason": resolved_manual_reason,
            "blocks_decision": resolved_blocks_decision,
        }
    )
    if conflict_group:
        source["conflict_group"] = conflict_group
    return {
        "id": assertion_id,
        "assertion_id": assertion_id,
        "claim": claim,
        "value": value,
        "evidence_state": evidence_state,
        "source": source,
        # Flat copies make the evidence easy to consume in CSV/JSON exports.
        "source_url": source["url"],
        "source_file": source["file"],
        "pages": pages,
        "page": pages,
        "pdf_page": resolved_pdf_page,
        "printed_page": resolved_printed_page,
        "table_location": label,
        "source_reference": resolved_reference,
        "original_clause": resolved_original_clause,
        "original_text": resolved_original_clause,
        "curriculum_version": cohort,
        "conflict_group": conflict_group,
        "verification_status": evidence_state,
        "coverage_state": resolved_coverage,
        "automation_sufficiency": "COMPLETE" if resolved_automatic else "PARTIAL" if evidence_state == VERIFIED else "NONE",
        "automatic_decision": resolved_automatic,
        "manual_reason": resolved_manual_reason,
        "manual_review_reason": resolved_manual_reason,
        "blocks_decision": resolved_blocks_decision,
    }


def _base_thresholds(program: str, cohort: str, track: str | None) -> dict[str, Any]:
    """Return independently transcribed aggregate thresholds."""

    if program == "earth":
        domain_elective = 22 if cohort == "111" else 20
        return {
            "total": 128,
            "university_common": 28,
            "major_total": 85,
            "major_common": 24,
            "domain_required": 14,
            "domain_elective": domain_elective,
            "other_elective": 13 if cohort == "111" else 27,
            "free": 15,
        }
    if program == "apc":
        is_chemistry = track == "chemistry"
        if cohort == "115":
            return {"total": 128, "university_common": 28, "major_common": 18, "track_required": 42, "elective": 25, "free": 15}
        return {
            "total": 128,
            "university_common": 28,
            "major_common": 16,
            "track_required": 45 if is_chemistry else 44,
            "elective": 24 if is_chemistry else 25,
            "free": 15,
        }
    if program == "cs":
        return {
            "total": 128,
            "university_common": 28,
            "department_required": 31,
            "department_elective": 54,
            "department_alpha_required": 32,
            "department_beta_required": 22,
            "beta_minimum_course_count": 1,
            "elective_rollup_group_id": "cs_primary_elective_54",
            "elective_rollup_required_credits": 54,
            "free": 15,
            "common_split": {"compulsory": 10, "category": 16, "elective": 2},
        }
    if program == "math":
        # 111/112 use a 36-credit common block and a 64-credit department
        # elective block.  The 15-credit alpha rule is a subset of that
        # block; it is deliberately not added to the 128-credit total.
        if cohort in {"111", "112"}:
            return {
                "total": 128,
                "university_common": 28,
                "program_common": 36,
                "program_elective": 64,
                "program_elective_by_student_type": {"non_teacher": 64, "teacher": 44},
                "active_student_type": "non_teacher",
                "department_elective_minimum": 64,
                "department_alpha_minimum": 15,
                "free": 15,
                "free_requirement_minimum": None,
                "free_subset_maxima": {
                    "external_department_or_school_professional": {
                        "non_teacher": 15,
                        "teacher": 11,
                    }
                },
                "department_subset_maxima": {
                    "external_department_or_school_professional": {"non_teacher": 15}
                },
                "free_amount_semantics": "MAXIMUM",
                "free_max": 15,
            }
        domain_required_by_track = {
            "math_scientific_computing": 7,
            "data_science": 6,
            "math_education": 3,
        }
        domain_required = domain_required_by_track.get(track or "math_scientific_computing", 7)
        return {
            "total": 128,
            "university_common": 28,
            "program_common": 20,
            "domain_required": domain_required,
            "domain_required_by_track": domain_required_by_track,
            "program_elective": 65,
            "department_elective_minimum": 65 - domain_required,
            "professional_total": 65,
            "active_student_type": "non_teacher",
            "free": 15,
            "free_requirement_minimum": 15,
            "free_subset_maxima": {
                "external_department_or_school_professional": {
                    "non_teacher": 15,
                    "teacher": 15,
                }
            },
            "free_amount_semantics": "MINIMUM",
        }
    if cohort in {"111", "112"}:
        return {
            "total": 128,
            "university_common": 28,
            "program_common": 36,
            "program_elective": 64,
            "program_elective_by_student_type": {
                "non_teacher": 64,
                "teacher": 44,
            },
            "free": 15,
            "free_requirement_minimum": None,
            "free_subset_maxima": {
                "external_department_or_school_professional": {
                    "non_teacher": 15,
                    "teacher": 11,
                }
            },
            "free_amount_semantics": "MAXIMUM",
            "free_max": 15,
        }
    if cohort == "115":
        return {
            "total": 128,
            "university_common": 28,
            "program_common": 20,
            "domain_required": 7,
            "program_elective": 65,
            "free": 15,
            "free_requirement_minimum": 15,
            "free_subset_maxima": {
                "external_department_or_school_professional": {
                    "non_teacher": 15,
                    "teacher": 15,
                }
            },
            "free_amount_semantics": "MINIMUM",
        }
    return {
        "total": 128,
        "university_common": 28,
        "program_common": 20,
        "domain_required": 7,
        "program_elective": 65,
        "free": 15,
        "free_requirement_minimum": 15,
        "free_subset_maxima": {
            "external_department_or_school_professional": {
                "non_teacher": 15,
                "teacher": 15,
            }
        },
        "free_amount_semantics": "MINIMUM",
    }


def _double_thresholds(program: str, cohort: str, track: str | None) -> dict[str, Any]:
    """Return the separate 40-credit double-major aggregate schema.

    These values describe the target curriculum only.  They intentionally do
    not inherit the primary 128-credit graduation thresholds.  Math 111/112
    records three jointly satisfiable minimum constraints (total 40, required
    minimum 21, and elective minimum 18); the visible bucket values remain
    evidence while the untranscribed course allocation stays PARTIAL.
    """

    if program == "earth":
        base, other = 24, 16
    elif program == "cs":
        base, other = 15, 25
    elif program == "math":
        if cohort in {"111", "112"}:
            base, other = 21, 18
        else:
            base, other = 14, 26
    elif cohort == "115":
        base, other = 20, 20
    else:
        base, other = 16, 24
    return {
        "schema": "double_major_40",
        "total": 40,
        "total_required": 40,
        "base": base,
        "other": other,
        "base_required": base,
        "other_required": other,
    }


def _primary_conflict_assertions(program: str, cohort: str) -> list[dict[str, Any]]:
    if program == "math" and cohort == "114":
        return [
            _assertion("math.primary.114.common20", cohort, "PDF p.67", "系共同必修架構", 20, label="數學系課程架構"),
        ]
    if program == "cs" and cohort == "114":
        conflict_group = "cs.primary.114.common-course-split"
        manual_reason = "PDF p.110 是摘要說明；共同課程分配依中央通識手冊與 PDF p.108 表格的 10／16／2 定案。"
        return [
            _assertion(
                "cs.primary.114.split10-16-2",
                cohort,
                "PDF p.108",
                "共同課程分配",
                "10/16/2",
                label="資科系學分規劃表",
                pdf_page="108",
                printed_page="107",
                original_clause="共同課程分配：10／16／2。",
            ),
            _assertion(
                "cs.primary.114.split8-16-4",
                cohort,
                "PDF p.110",
                "共同課程分配",
                "8/16/4",
                label="資科系課程說明",
                pdf_page="110",
                printed_page="109",
                original_clause="共同課程分配：8／16／4。",
                evidence_state=CONFLICTED,
                automatic_decision=False,
                manual_reason=manual_reason,
                coverage_state=PARTIAL,
                conflict_group=conflict_group,
                blocks_decision=False,
            ),
        ]
    if program == "cs" and cohort == "115":
        return [
            _assertion(
                "cs.primary.115.diagram15",
                cohort,
                "PDF p.118",
                "自由選修額度",
                "at-least-15",
                label="資科系課程架構圖",
                original_clause="課程架構圖另列自由選修至少 15 學分。",
                blocks_decision=False,
            ),
            _assertion("cs.primary.115.table28", cohort, "PDF p.119", "校共同課程總額", 28, label="資科系學分規劃表"),
        ]
    return []


def _primary_assertions(program: str, cohort: str, track: str | None) -> list[dict[str, Any]]:
    pages = {
        "earth": {"111": "PDF p.26", "112": "PDF p.26", "113": "PDF p.31", "114": "PDF p.32", "115": "PDF p.40"},
        "apc": {"111": "PDF p.3", "112": "PDF p.3", "113": "PDF p.3", "114": "PDF p.3", "115": "PDF p.3"},
        "math": {"111": "PDF p.67", "112": "PDF p.62", "113": "PDF p.67", "114": "PDF p.67", "115": "PDF p.78"},
        "cs": {"111": "PDF p.113", "112": "PDF p.108", "113": "PDF p.103", "114": "PDF p.108", "115": "PDF p.119"},
    }
    assertions = [
        _assertion(
            f"{program}.primary.{cohort}.total",
            cohort,
            pages[program][cohort],
            "主修總畢業學分",
            128,
            label=f"{cohort} 學年度{_PROGRAM_DISPLAY[program]}主修門檻",
        )
    ]
    assertions.extend(_primary_conflict_assertions(program, cohort))
    return assertions


def _double_assertions(program: str, cohort: str, track: str | None) -> tuple[list[dict[str, Any]], str, str, str]:
    """Return assertions, evidence state, coverage and warning for a target."""

    # Secondary catalogues are now transcribed from the year-scoped contracts
    # below.  Keep aggregate assertions small and independent from the
    # candidate pools; a pool candidate is eligible evidence, never an extra
    # credit consumer.  The older branches remain below for historical
    # compatibility with callers that inspect this module, but all registry
    # construction follows this complete path.
    if program == "math":
        if cohort in {"111", "112"}:
            pages = "PDF pp.80–82" if cohort == "111" else "PDF pp.75–77"
            required = 21
            elective = 18
        elif cohort == "113":
            pages = "PDF pp.77–79"
            required = 14
            elective = 26
        else:
            pages = "PDF pp.80–82" if cohort == "114" else "PDF pp.91–93"
            required = 14
            elective = 26
        assertions = [
            _assertion(f"math.dm.{cohort}.total40", cohort, pages, "雙主修總額", 40, label="數學系雙主修表"),
            _assertion(f"math.dm.{cohort}.required{required}", cohort, pages, "雙主修必修最低額度", required, label="數學系雙主修表"),
            _assertion(f"math.dm.{cohort}.elective{elective}", cohort, pages, "雙主修選修最低額度", elective, label="數學系雙主修表"),
        ]
        if cohort == "113":
            assertions.extend(
                (
                    _assertion(
                        "math.dm.113.elective.high_calculus_1",
                        cohort,
                        pages,
                        "高等微積分(一)正式選修列項",
                        4,
                        label="數學系雙主修表",
                    ),
                    _assertion(
                        "math.dm.113.elective.algebra_1",
                        cohort,
                        pages,
                        "代數學(一)正式選修列項",
                        3,
                        label="數學系雙主修表",
                    ),
                )
            )
        return assertions, VERIFIED, COMPLETE, ""
    if program == "earth":
        pages = {"111": "PDF p.39", "112": "PDF p.39", "113": "PDF p.45", "114": "PDF p.47", "115": "PDF p.55"}[cohort]
        assertions = [
            _assertion(f"earth.dm.{cohort}.total40", cohort, pages, "地生雙主修總額", 40, label="地生系雙主修表"),
            _assertion(f"earth.dm.{cohort}.base24", cohort, pages, "共同必修", 24, label="地生系雙主修表"),
            _assertion(f"earth.dm.{cohort}.other16", cohort, pages, "兩領域專業選修", 16, label="地生系雙主修表"),
        ]
        return assertions, VERIFIED, COMPLETE, ""
    if program == "apc":
        page_map = _APC_PHYSICS_DM_PAGE if track == "physics" else _APC_CHEMISTRY_DM_PAGE
        page = page_map[cohort]
        pages = f"PDF p.{page['pdf_page']}"
        base, other = (20, 20) if cohort == "115" else (16, 24)
        track_label = "電子物理組" if track == "physics" else "應用化學組"
        assertions = [
            _assertion(f"apc.dm.{cohort}.base{base}", cohort, pages, "物化雙主修基礎列項", base, label=f"物化系{track_label}雙主修表"),
            _assertion(f"apc.dm.{cohort}.other{other}", cohort, pages, "物化系同組主修必修剩餘額度", other, label=f"物化系{track_label}雙主修表"),
        ]
        return assertions, VERIFIED, COMPLETE, ""
    if program == "cs":
        pages = {"111": "PDF pp.121–122", "112": "PDF pp.116–117", "113": "PDF pp.111–112", "114": "PDF pp.116–117", "115": "PDF p.127"}[cohort]
        assertions = [
            _assertion(f"cs.dm.{cohort}.required15", cohort, pages, "資科雙主修命名必修", 15, label="資科系雙主修表"),
            _assertion(f"cs.dm.{cohort}.other25", cohort, pages, "資科雙主修其他課程", 25, label="資科系雙主修表"),
        ]
        return assertions, VERIFIED, COMPLETE, ""

    if program == "math" and cohort in {"111", "112"}:
        pages = "PDF pp.80–82" if cohort == "111" else "PDF pp.75–77"
        assertions = [
            _assertion(f"math.dm.{cohort}.total40", cohort, pages, "雙主修總額", 40, label="數學系雙主修表"),
            _assertion(f"math.dm.{cohort}.required21", cohort, pages, "雙主修必修列項合計", 21, label="數學系雙主修表"),
            _assertion(f"math.dm.{cohort}.elective18", cohort, pages, "雙主修選修下限", 18, label="數學系雙主修表"),
        ]
        return (
            assertions,
            VERIFIED,
            PARTIAL,
            "總額40、必修最低21、選修最低18是共同最低約束；選修可修19以上以達總額40，逐課目錄尚未完整建置。",
        )
    if program == "math" and cohort == "113":
        pages = "PDF pp.77–79"
        conflict_group = "math.dm.113.required-course-revision"
        assertions = [
            _assertion("math.dm.113.header14", cohort, pages, "雙主修表頭必修", 14, label="數學系雙主修表"),
            _assertion(
                "math.dm.113.visible21",
                cohort,
                pages,
                "雙主修原表可見必修列項合計",
                21,
                label="數學系雙主修表",
                evidence_state=CONFLICTED,
                coverage_state=PARTIAL,
                automatic_decision=False,
                manual_reason="同頁修訂將高等微積分(一)與代數學(一)自必修列移至正式選修列；保留原列合計作來源稽核，不阻擋總額判定。",
                conflict_group=conflict_group,
                blocks_decision=False,
            ),
            _assertion("math.dm.113.elective26", cohort, pages, "雙主修選修下限", 26, label="數學系雙主修表"),
            _assertion(
                "math.dm.113.elective.high_calculus_1",
                cohort,
                pages,
                "高等微積分(一)正式選修列項",
                4,
                label="數學系雙主修表",
                original_clause="高等微積分(一) 4 學分於同頁修訂後選修表列出。",
            ),
            _assertion(
                "math.dm.113.elective.algebra_1",
                cohort,
                pages,
                "代數學(一)正式選修列項",
                3,
                label="數學系雙主修表",
                original_clause="代數學(一) 3 學分於同頁修訂後選修表列出。",
            ),
        ]
        return (
            assertions,
            VERIFIED,
            PARTIAL,
            "雙主修總額40、必修14、選修至少26已依同頁修訂核對；高等微積分(一)與代數學(一)移入正式選修池，完整逐課配置仍需人工複核。",
        )
    if program == "math" and cohort in {"114", "115"}:
        pages = "PDF pp.80–82" if cohort == "114" else "PDF pp.91–93"
        assertions = [
            _assertion(f"math.dm.{cohort}.total40", cohort, pages, "雙主修總額", 40, label="數學系雙主修表"),
            _assertion(f"math.dm.{cohort}.required14", cohort, pages, "雙主修必修", 14, label="數學系雙主修表"),
            _assertion(f"math.dm.{cohort}.elective26", cohort, pages, "雙主修選修下限", 26, label="數學系雙主修表"),
        ]
        return assertions, VERIFIED, PARTIAL, "aggregate 14＋至少26 可核對，但逐課目錄尚未完整建置，需人工複核。"
    if program == "apc" and track == "chemistry" and cohort in {"111", "112"}:
        pages = "PDF p.19"
        assertions = [
            _assertion(f"apc.dm.{cohort}.visible16", cohort, pages, "應用化學雙主修可見基礎列項", 16, label="物化系雙主修表"),
            _assertion(
                f"apc.dm.{cohort}.footer24",
                cohort,
                pages,
                "表尾額外目標必修額度",
                24,
                label="物化系雙主修表",
                original_clause="表尾額外目標必修 24 學分；與可見基礎列項16合計40。",
            ),
            _assertion(
                f"apc.dm.{cohort}.footer-semantics",
                cohort,
                pages,
                "表尾額度用途",
                "additional_required",
                label="物化系雙主修表",
                original_clause="表尾24學分為雙主修額外目標必修額度。",
            ),
        ]
        row_assertions = _apc_target_catalog_assertions(cohort, track)
        existing_ids = {item["id"] for item in assertions}
        assertions.extend(item for item in row_assertions if item["id"] not in existing_ids)
        return (
            assertions,
            VERIFIED,
            PARTIAL,
            "可見基礎16與額外目標必修24已核對為雙主修40；完整目標必修命名目錄與核准 mapping 仍需人工確認。",
        )
    if program == "cs" and cohort == "115":
        pages = "PDF p.127"
        assertions = [
            _assertion("cs.dm.115.aggregate15", cohort, pages, "雙主修必修 aggregate", 15, label="資科系雙主修表"),
            _assertion("cs.dm.115.aggregate25", cohort, pages, "雙主修其他課程 aggregate", 25, label="資科系雙主修表"),
            _assertion("cs.dm.115.visible-named", cohort, pages, "可見命名基礎課列項", "incomplete", evidence_state=MISSING, label="資科系雙主修表"),
        ]
        return assertions, VERIFIED, PARTIAL, "aggregate 15＋25 可核對，但命名基礎課程表不完整，需人工核對。"
    if program == "earth":
        pages = {"111": "PDF p.39", "112": "PDF p.39", "113": "PDF p.45", "114": "PDF p.47", "115": "PDF p.55"}[cohort]
        assertions = [
            _assertion(f"earth.dm.{cohort}.total40", cohort, pages, "地生雙主修總額", 40, label="地生系雙主修表"),
            _assertion(f"earth.dm.{cohort}.base24", cohort, pages, "共同必修", 24, label="地生系雙主修表"),
            _assertion(f"earth.dm.{cohort}.other16", cohort, pages, "專業選修", 16, label="地生系雙主修表"),
        ]
        return assertions, VERIFIED, PARTIAL, "雙主修頁只有 aggregate 結構，未提供可安全逐課配置的完整目錄。"
    if program == "apc":
        page_map = _APC_PHYSICS_DM_PAGE if track == "physics" else _APC_CHEMISTRY_DM_PAGE
        page = page_map[cohort]
        pages = f"PDF p.{page['pdf_page']}"
        track_label = "電子物理組" if track == "physics" else "應用化學組"
        if cohort == "115":
            base, other = 20, 20
        else:
            base, other = 16, 24
        assertions = [
            _assertion(f"apc.dm.{cohort}.base{base}", cohort, pages, "物化雙主修基礎列項", base, label=f"物化系{track_label}雙主修表"),
            _assertion(f"apc.dm.{cohort}.other{other}", cohort, pages, "物化雙主修其餘必修", other, label=f"物化系{track_label}雙主修表"),
        ]
        if track == "chemistry" or (track == "physics" and cohort == "115"):
            # Preserve the aggregate assertions above while exposing the
            # exact visible target rows and the missing named-pool semantic.
            # The returned evidence state stays VERIFIED because the official
            # claims are readable; coverage remains PARTIAL below.
            row_assertions = _apc_target_catalog_assertions(cohort, track)
            existing_ids = {item["id"] for item in assertions}
            assertions.extend(item for item in row_assertions if item["id"] not in existing_ids)
            track_label = "電子物理組" if track == "physics" else "應用化學組"
            warning = (
                f"可見八項（含微積分(一)、微積分(二)）與其餘必修20 aggregate 已核對；{track_label}官方其餘必修完整命名目錄與選擇規則尚未轉錄，"
                "coverage=PARTIAL，需人工複核。"
                if cohort == "115"
                else f"可見八項與其餘必修24 aggregate 已核對；{track_label}官方其餘必修完整命名目錄與選擇規則尚未轉錄，coverage=PARTIAL，需人工複核。"
            )
            return assertions, VERIFIED, PARTIAL, warning
        return assertions, VERIFIED, PARTIAL, "雙主修 aggregate 可核對，但逐課目錄尚未完整建置。"
    # CS 111–114 have readable aggregate rows, but only the older configured
    # rule table can be used for exact names.  Keep the evidence independent.
    pages = {"111": "PDF pp.121–122", "112": "PDF pp.116–117", "113": "PDF pp.111–112", "114": "PDF pp.116–117", "115": "PDF p.127"}[cohort]
    assertions = [
        _assertion(f"cs.dm.{cohort}.required15", cohort, pages, "資科雙主修必修", 15, label="資科系雙主修表"),
        _assertion(f"cs.dm.{cohort}.other25", cohort, pages, "資科雙主修其他課程", 25, label="資科系雙主修表"),
    ]
    return assertions, VERIFIED, PARTIAL, "aggregate 15＋25 可核對，但 required/other 與申請核准語義尚未完整建置。"


def _primary_evidence(cohort: str, program: str) -> dict[str, Any]:
    """Return the source contract for a primary handbook section.

    New 111/115 records carry section-level evidence in ``rules_config.json``;
    older records receive conservative legacy defaults.  A missing section
    never inherits a neighbouring cohort's citation or course rows.
    """

    handbook = _RULES.get("handbooks", {}).get(cohort, {})
    meta = handbook.get("_meta", {}) if isinstance(handbook, dict) else {}
    section_key = {
        "earth": "earth_life_major",
        "apc": "apc_rules",
        "math": "math_rules",
        "cs": "cs_rules",
    }[program]
    section = handbook.get(section_key, {}) if isinstance(handbook, dict) else {}
    evidence = section.get("evidence", {}) if isinstance(section, dict) else {}
    if not isinstance(evidence, dict):
        evidence = {}
    pages = meta.get("source_pages", {}).get(
        "earth_life" if program == "earth" else program,
        "官方學生手冊",
    )
    source_url = evidence.get("source_url") or meta.get("source_url") or _HANDBOOK_URLS.get(cohort, "")
    return {
        "curriculum_version": cohort,
        "research_file": evidence.get("research_file", "research/math_earth_handbook_matrix_111_115.md" if program in {"earth", "math"} else "research/apc_cs_handbook_matrix_111_115.md"),
        "source_reference": evidence.get("source_reference", f"handbook:{cohort}:primary:{program}"),
        "source_url": source_url,
        "source_file": meta.get("source_file", _source_file(cohort)),
        "pdf_page": evidence.get("pdf_page", pages),
        "printed_page": evidence.get("printed_page", "未標示"),
        "pages": pages,
        "table_location": evidence.get("table_location", f"{_PROGRAM_DISPLAY[program]} 主修課程表"),
        "evidence_state": evidence.get("evidence_state", meta.get("evidence_state", VERIFIED)),
        "coverage_state": evidence.get("coverage_state", meta.get("coverage_state", PARTIAL)),
        "automation_sufficiency": evidence.get("automation_sufficiency", meta.get("automation_sufficiency", PARTIAL)),
        "original_clause": evidence.get("original_clause", ""),
        "manual_reason": evidence.get("manual_review_reason", meta.get("manual_review_reason", "")),
    }


def _primary_section_source(
    source: Mapping[str, Any],
    section: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply a source-backed page/coverage override to one primary section."""

    if not isinstance(section, Mapping):
        return deepcopy(dict(source))
    scoped = deepcopy(dict(source))
    for key in (
        "research_file",
        "source_reference",
        "source_url",
        "source_file",
        "pdf_page",
        "printed_page",
        "pages",
        "table_location",
        "original_clause",
        "evidence_state",
        "coverage_state",
        "automation_sufficiency",
        "automatic_decision",
        "manual_reason",
        "manual_review_reason",
    ):
        if key in section and section[key] not in (None, ""):
            scoped[key] = deepcopy(section[key])
    return scoped


def _policy_source(
    cohort: str,
    policy_key: str,
    fallback: Mapping[str, Any],
    *,
    program: str | None = None,
    track: str | None = None,
) -> dict[str, Any]:
    """Return a source contract for a policy-only pool.

    Shared university policies are sourced from the year-specific official
    general-education manual.  The free-elective rule is instead stated in
    each primary programme section, so it uses that section's source while
    retaining a distinct policy reference.  The returned object deliberately
    keeps both the normalized source keys used by the registry and the
    ``url``/``file`` aliases used by older exporters.
    """

    shared = _RULES.get("shared", {})
    config = shared.get(policy_key, {}) if isinstance(shared, Mapping) else {}
    config = config if isinstance(config, Mapping) else {}
    contract: Mapping[str, Any] = {}
    evidence_by_cohort = config.get("evidence_by_cohort", {})
    if isinstance(evidence_by_cohort, Mapping):
        item = evidence_by_cohort.get(str(cohort), {})
        if isinstance(item, Mapping):
            # ``free_elective`` is a programme/track-scoped policy.  Keep the
            # shared cohort entry as a compact source family, then overlay the
            # selected track contract so a policy can never inherit another
            # programme's page, amount semantics, or subset rule.
            if policy_key == "free_elective" and program:
                programme_contract = item.get(program, {})
                if isinstance(programme_contract, Mapping):
                    merged: dict[str, Any] = {
                        key: deepcopy(value)
                        for key, value in programme_contract.items()
                        if key != "tracks"
                    }
                    tracks = programme_contract.get("tracks", {})
                    selected_track = track or "department"
                    if isinstance(tracks, Mapping):
                        # Math 113+ exposes canonical domain tracks while the
                        # handbook's free-elective clause is shared by the
                        # department section.  Reuse that department source
                        # only when a selected track has no dedicated clause;
                        # Earth/APC track-specific contracts still win.
                        track_contract = (
                            tracks.get(selected_track)
                            or tracks.get("default")
                            or tracks.get("department", {})
                        )
                        if isinstance(track_contract, Mapping):
                            merged.update(deepcopy(dict(track_contract)))
                    contract = merged
            else:
                contract = item

    if contract:
        source_reference = str(contract.get("source_reference") or fallback.get("source_reference") or "")
        source_file = str(contract.get("source_file") or fallback.get("source_file") or "")
        source_url = str(contract.get("source_url") or fallback.get("source_url") or "")
        pdf_page = contract.get("pdf_page") or fallback.get("pdf_page") or "未標示"
        printed_page = contract.get("printed_page") or fallback.get("printed_page") or "未標示"
        pages = contract.get("pages") or f"{pdf_page}（印刷 {printed_page}）"
        table_location = contract.get("table_location") or config.get("table_location") or "官方政策條款"
        research_file = contract.get("research_file") or "research/university_common_policy_111_115.md"
    else:
        source_reference = str(fallback.get("source_reference") or "")
        source_file = str(fallback.get("source_file") or "")
        source_url = str(fallback.get("source_url") or "")
        pdf_page = fallback.get("pdf_page") or "未標示"
        printed_page = fallback.get("printed_page") or "未標示"
        pages = fallback.get("pages") or f"{pdf_page}（印刷 {printed_page}）"
        table_location = fallback.get("table_location") or "官方主修課程表"
        research_file = fallback.get("research_file") or ""

    if policy_key == "free_elective":
        source_reference = f"{source_reference}:policy:free_total"
        table_location = f"{table_location}；自由選修政策"
        research_file = str(
            contract.get("research_file")
            or fallback.get("research_file")
            or "research/math_earth_handbook_matrix_111_115.md"
        )
        source_type = str(contract.get("source_type") or "official_primary_handbook_section")
    else:
        source_type = str(contract.get("source_type") or "official_general_education_manual") if contract else "official_policy"

    result = {
        "source_type": source_type,
        "research_file": research_file,
        "source_url": source_url,
        "url": source_url,
        "source_file": source_file,
        "file": source_file,
        "pdf_page": pdf_page,
        "printed_page": printed_page,
        "pages": pages,
        "table_location": table_location,
        "source_reference": source_reference,
        "curriculum_version": str(cohort),
        "program_slug": program,
        "track_slug": track or "department",
        "original_clause": str(
            contract.get("original_clause")
            or config.get("policy_clause")
            or fallback.get("original_clause")
            or ""
        ),
    }
    for key in (
        "manual_embedded",
        "historical_handbook_embedded",
        "applicability_state",
        "applicability_note",
        "policy_source_reference",
        "verified_fields",
        "evidence_state",
        "coverage_state",
        "automation_sufficiency",
        "manual_reason",
        "manual_review_reason",
        "amount_semantics",
        "total_req",
        "requirement_minimum_credits",
        "subset_maxima",
        "maximum_credits",
        "maximum_credits_by_student_type",
        "minimum_science_college_credits",
        "science_subset_state",
        "allow_external_departments",
        "requires_official_course_catalog",
        "exclude_allocated_attempts",
        "source_contract",
        "rule_components",
        "supplemental_source",
        "compulsory_total",
        "compulsory_courses",
        "courses",
        "category_min_each",
        "common_elective_min",
        "flex_credits",
        "requirement_id",
        "kind",
        "requirement_type",
        "required_completions",
        "min_earned_credits_per_completion",
        "membership_id",
        "affects_credit_ledger",
        "waiver_allowed",
        "waiver_authority_ids",
        "effective_cohorts",
        "scope_state",
        "catalog_source",
        "automatic_decision",
    ):
        if key in contract:
            result[key] = deepcopy(contract[key])
    return result


def _university_common_contract(cohort: str) -> dict[str, Any]:
    """Return the year-scoped common-course compile contract.

    The shared defaults describe the later 10-credit language structure.  The
    111 handbook is an explicit exception (8 credits plus a two-credit flex
    route), so both pool and row builders must read the same cohort contract
    instead of silently inheriting that default.
    """

    shared = _RULES.get("shared", {}).get("university_common", {})
    if not isinstance(shared, Mapping):
        return {}
    evidence_by_cohort = shared.get("evidence_by_cohort", {})
    contract = evidence_by_cohort.get(str(cohort), {}) if isinstance(evidence_by_cohort, Mapping) else {}
    contract = deepcopy(dict(contract)) if isinstance(contract, Mapping) else {}
    compulsory = shared.get("compulsory", {})
    compulsory = compulsory if isinstance(compulsory, Mapping) else {}
    fallback_courses = compulsory.get("courses", {})
    fallback_courses = fallback_courses if isinstance(fallback_courses, Mapping) else {}
    courses = contract.get("compulsory_courses") or contract.get("courses") or fallback_courses
    courses = dict(courses) if isinstance(courses, Mapping) else {}
    contract["courses"] = deepcopy(courses)
    contract["compulsory_courses"] = deepcopy(courses)
    contract["compulsory_total"] = contract.get(
        "compulsory_total",
        sum(float(value) for value in courses.values()),
    )
    ge = shared.get("ge_categories", {})
    ge = ge if isinstance(ge, Mapping) else {}
    contract["category_min_each"] = contract.get(
        "category_min_each",
        ge.get("per_category_req", 4),
    )
    contract["common_elective_min"] = contract.get(
        "common_elective_min",
        shared.get("ge_common_elective_req", 2),
    )
    contract["flex_credits"] = contract.get("flex_credits", 0)
    return contract


def _primary_course_row(
    cohort: str,
    program: str,
    track: str | None,
    name: str,
    credits: int | float,
    bucket: str,
    source: Mapping[str, Any],
    *,
    kind: str = "PRIMARY_COURSE",
    requirement_type: str = "named_course",
    choice_group: str | None = None,
    choice_rule: str | None = "exact_course_or_approved_equivalency",
    eligible_names: tuple[str, ...] | None = None,
    eligible_options: tuple[tuple[str, int | float], ...] | None = None,
    pool_ids: tuple[str, ...] = (),
    eligible_pool_ids: tuple[str, ...] = (),
    overflow_routes: tuple[str, ...] = (),
    coverage_state: str | None = None,
    automatic_decision: bool | None = None,
) -> dict[str, Any]:
    """Build one auditable primary course row from a source-backed map."""

    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(name)).strip("_").lower() or "course"
    track_slug = track or "department"
    row_id = f"{program}.primary.{cohort}.{track_slug}.{slug}"
    component = "lab" if "實驗" in str(name) else "lecture"
    source_reference = f"{source['source_reference']}:row:{slug}"
    original_clause = source.get("original_clause") or f"{name} {float(credits):g} 學分。"
    evidence_state = source.get("evidence_state", VERIFIED)
    row_coverage = coverage_state or source.get("coverage_state", PARTIAL)
    row_automatic = (
        automatic_decision
        if automatic_decision is not None
        else evidence_state == VERIFIED and row_coverage == COMPLETE
    )
    names = eligible_names if eligible_names is not None else (name,)
    provenance = {
        "assertion_id": row_id,
        "source_type": "official_handbook",
        "research_file": source["research_file"],
        "source_url": source["source_url"],
        "source_file": source["source_file"],
        "pdf_page": source["pdf_page"],
        "printed_page": source["printed_page"],
        "pages": source["pages"],
        "table_location": source["table_location"],
        "source_reference": source_reference,
        "raw_title": name,
        "original_clause": original_clause,
        "evidence_state": evidence_state,
        "verification_status": evidence_state,
        "automation_sufficiency": "COMPLETE" if row_automatic else "PARTIAL",
        "automatic_decision": row_automatic,
        "manual_reason": source.get("manual_reason", ""),
        "manual_review_reason": source.get("manual_reason", ""),
    }
    return {
        "id": row_id,
        "requirement_id": row_id,
        "name": name,
        "raw_title": name,
        "credits": float(credits),
        "bucket": bucket,
        "kind": kind,
        "requirement_type": requirement_type,
        "choice_group": choice_group,
        "choice_rule": choice_rule,
        "track": _TRACK_DISPLAY.get(track) if track else None,
        "track_slug": track_slug,
        "eligible_course_names": tuple(names),
        "eligible_course_options": tuple(
            {"name": option_name, "credits": float(option_credits)}
            for option_name, option_credits in (eligible_options or ((name, credits),))
        ),
        "pool_ids": tuple(pool_ids),
        "eligible_pool_ids": tuple(eligible_pool_ids),
        "overflow_routes": tuple(overflow_routes),
        "candidate_only": False,
        "pool_requirement": requirement_type in {"choice", "course_pool", "credit_quota"},
        "waiver": False,
        "waiver_generates_credits": False,
        "component": component,
        "component_type": component,
        "component_label": "實驗" if component == "lab" else "講授",
        "lecture_or_lab": component,
        "is_lab": component == "lab",
        "is_zero_credit": float(credits) == 0,
        "allow_combined_lab_source": False,
        "evidence": evidence_state,
        "evidence_state": evidence_state,
        "coverage_state": row_coverage,
        "verification_status": evidence_state,
        "automation_sufficiency": "COMPLETE" if row_automatic else "PARTIAL",
        "automatic_decision": row_automatic,
        "manual_reason": source.get("manual_reason", ""),
        "manual_review_reason": source.get("manual_reason", ""),
        "program_slug": program,
        "curriculum_version": cohort,
        "cohort": cohort,
        "source_assertion_id": row_id,
        "assertion_id": row_id,
        "source_reference": source_reference,
        "source_url": source["source_url"],
        "source_file": source["source_file"],
        "research_file": source["research_file"],
        "pdf_page": source["pdf_page"],
        "printed_page": source["printed_page"],
        "page": source["pdf_page"],
        "pages": source["pages"],
        "table_location": source["table_location"],
        "original_clause": original_clause,
        "original_text": original_clause,
        "provenance": provenance,
        "official_course_identity": f"{cohort}:{program}:{track_slug}:{slug}",
    }


def _cs_target_quota_catalog(cohort: str) -> list[dict[str, Any]]:
    """Expose only the official CS double-major aggregates as generic rows."""

    return _cs_secondary_catalog_rows(cohort, "double_major")

    pages = {
        "111": ("121", "120"),
        "112": ("116", "115"),
        "113": ("111", "110"),
        "114": ("116", "115"),
        "115": ("127", "126"),
    }
    pdf_page, printed_page = pages[cohort]
    label = f"{cohort} 學年度資科系雙主修 aggregate 表"
    source = _citation(cohort, f"PDF p.{pdf_page}（印刷 p.{printed_page}）", label)
    required_pool_id = _pool_id("double_major_target", cohort, "cs", None, "required_named")
    other_pool_id = _pool_id("double_major_target", cohort, "cs", None, "other_required")
    rows: list[dict[str, Any]] = []
    for slug, name, credits, clause in (
        ("required15", "資科雙主修命名必修 aggregate", 15.0, "資科雙主修必修 15 學分。"),
        ("other25", "資科雙主修其他課程 aggregate", 25.0, "資科雙主修其他課程 25 學分。"),
    ):
        requirement_id = f"cs.dm.{cohort}.{slug}"
        source_reference = f"handbook:{cohort}:pdf:{pdf_page}:row:{slug}"
        manual_reason = "官方只核對到 aggregate 額度，命名課程池與個案核准仍需人工確認。"
        row_source = {
            **deepcopy(source),
            "curriculum_version": cohort,
            "pdf_page": pdf_page,
            "printed_page": printed_page,
            "source_reference": source_reference,
            "original_clause": clause,
            "verification_status": VERIFIED,
            "coverage_state": PARTIAL,
            "automation_sufficiency": PARTIAL,
            "automatic_decision": False,
            "manual_reason": manual_reason,
        }
        provenance = {
            "assertion_id": requirement_id,
            "source_type": "official_handbook",
            "research_file": "research/apc_cs_handbook_matrix_111_115.md",
            "source_url": source["url"],
            "source_file": source["file"],
            "pdf_page": pdf_page,
            "printed_page": printed_page,
            "pages": source["pages"],
            "source_reference": source_reference,
            "table_location": label,
            "original_clause": clause,
            "evidence_state": VERIFIED,
            "verification_status": VERIFIED,
            "coverage_state": PARTIAL,
            "automation_sufficiency": PARTIAL,
            "automatic_decision": False,
            "manual_reason": manual_reason,
            "manual_review_reason": manual_reason,
        }
        rows.append(
            {
                "id": requirement_id,
                "requirement_id": requirement_id,
                "name": name,
                "display_name": name,
                "raw_title": name,
                "credits": credits,
                "bucket": "required" if slug == "required15" else "other_required",
                "kind": "quota",
                "requirement_type": "credit_quota",
                "choice_group": "cs_double_major_aggregate",
                "choice_rule": "official_aggregate_only",
                "track": None,
                "track_slug": "department",
                "cohort": cohort,
                "curriculum_version": cohort,
                "component": "quota",
                "component_type": "quota",
                "component_label": "aggregate 額度",
                "lecture_or_lab": "quota",
                "is_lab": False,
                "is_zero_credit": False,
                "allow_combined_lab_source": False,
                "eligible_course_names": (),
                "eligible_course_options": (),
                "accept_any": False,
                "pool_ids": (),
                "eligible_pool_ids": (required_pool_id if slug == "required15" else other_pool_id,),
                "overflow_routes": (),
                "candidate_only": False,
                "pool_requirement": True,
                "waiver": False,
                "waiver_generates_credits": False,
                "evidence": VERIFIED,
                "evidence_state": VERIFIED,
                "verification_status": VERIFIED,
                "coverage_state": PARTIAL,
                "automation_sufficiency": PARTIAL,
                "automatic_decision": False,
                "manual_reason": manual_reason,
                "manual_review_reason": manual_reason,
                "assertion_id": requirement_id,
                "source_assertion_id": requirement_id,
                "source_reference": source_reference,
                "source_url": source["url"],
                "source_file": source["file"],
                "research_file": "research/apc_cs_handbook_matrix_111_115.md",
                "pdf_page": pdf_page,
                "printed_page": printed_page,
                "page": pdf_page,
                "pages": source["pages"],
                "table_location": label,
                "original_clause": clause,
                "original_text": clause,
                "source": row_source,
                "provenance": provenance,
                "official_course_identity": f"{cohort}:cs:double_major:{slug}",
                "catalog": [],
                "named_course_pool_state": PARTIAL,
            }
        )
    return rows


def _primary_pool_requirement_row(
    cohort: str,
    program: str,
    track: str | None,
    pool: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    suffix: str,
    requirement_type: str = "course_pool",
    choice_group: str | None = None,
    choice_rule: str | None = "at_least_credits_from_pool",
    overflow_routes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Turn one explicit pool quota into an executable requirement row.

    The candidate courses remain under ``course_pools``.  This row is the
    credit consumer and is the only primary row that receives
    ``eligible_pool_ids``.  Named compulsory rows use ``pool_ids`` instead,
    so a candidate from a broad pool cannot replace a named course.
    """

    pool_id = str(pool["id"])
    row_id = f"{program}.primary.{cohort}.{track or 'department'}.pool.{_pool_token(suffix)}"
    label = str(pool.get("label") or suffix)
    credits = float(pool.get("required_credits") or 0)
    candidates = tuple(pool.get("candidate_courses") or ())
    eligible_names = tuple(
        str(item.get("name"))
        for item in candidates
        if isinstance(item, Mapping) and item.get("name")
    )
    eligible_options = tuple(
        (
            str(item.get("name")),
            float(item.get("credits", 0)),
        )
        for item in candidates
        if isinstance(item, Mapping) and item.get("name") and float(item.get("credits", 0)) > 0
    )
    row = _primary_course_row(
        cohort,
        program,
        track,
        label,
        credits,
        str(pool.get("bucket") or suffix),
        source,
        kind="quota" if requirement_type != "choice" else "choice",
        requirement_type=requirement_type,
        choice_group=choice_group,
        choice_rule=choice_rule,
        eligible_names=eligible_names,
        eligible_options=eligible_options,
        pool_ids=(),
        eligible_pool_ids=(pool_id,),
        overflow_routes=overflow_routes,
        coverage_state=str(pool.get("coverage_state") or PARTIAL),
        automatic_decision=(
            str(pool.get("evidence_state")) == VERIFIED
            and str(pool.get("coverage_state")) == COMPLETE
            and not pool.get("manual_reason")
        ),
    )
    row.update(
        {
            "id": row_id,
            "requirement_id": row_id,
            "name": label,
            "raw_title": label,
            "display_name": label,
            "kind": "choice" if requirement_type == "choice" else "quota",
            "requirement_type": requirement_type,
            "component": "quota",
            "component_type": "quota",
            "component_label": "選課池額度",
            "lecture_or_lab": "quota",
            "is_lab": False,
            "official_course_identity": f"{cohort}:{program}:{track or 'department'}:pool:{_pool_token(suffix)}",
            "source_assertion_id": row_id,
            "assertion_id": row_id,
            "required_credits": credits,
            "pool_ids": (),
            "eligible_pool_ids": (pool_id,),
            "overflow_routes": tuple(overflow_routes),
            "candidate_only": False,
            "pool_requirement": True,
            "named_course_pool_state": "COMPLETE" if str(pool.get("coverage_state")) == COMPLETE else PARTIAL,
            "policy_id": pool.get("policy_id"),
            "policy_revision": pool.get("policy_revision"),
            "applies_to": deepcopy(pool.get("applies_to")),
            "predicate": deepcopy(pool.get("predicate")),
            "policy_source": deepcopy(pool.get("policy_source")),
        }
    )
    provenance = dict(row.get("provenance") or {})
    provenance.update(
        {
            "assertion_id": row_id,
            "pool_id": pool_id,
            "required_credits": credits,
            "candidate_only": False,
            "original_clause": pool.get("original_clause") or f"{label}至少 {credits:g} 學分。",
        }
    )
    if pool.get("policy"):
        row["policy"] = deepcopy(pool["policy"])
        row["provenance"]["policy"] = deepcopy(pool["policy"])
    if pool.get("policy_source"):
        row["provenance"]["policy_source"] = deepcopy(pool["policy_source"])
    row["provenance"] = provenance
    row["original_clause"] = pool.get("original_clause") or f"{label}至少 {credits:g} 學分。"
    row["original_text"] = row["original_clause"]
    row["source_reference"] = pool.get("source_reference") or row["source_reference"]
    row["source_url"] = pool.get("source_url") or row["source_url"]
    row["source_file"] = pool.get("source_file") or row["source_file"]
    row["research_file"] = pool.get("research_file") or row["research_file"]
    row["pdf_page"] = pool.get("pdf_page") or row["pdf_page"]
    row["printed_page"] = pool.get("printed_page") or row["printed_page"]
    row["page"] = row["pdf_page"]
    row["pages"] = pool.get("pages") or row["pages"]
    row["table_location"] = pool.get("table_location") or row["table_location"]
    row["manual_reason"] = str(pool.get("manual_reason") or "")
    row["manual_review_reason"] = row["manual_reason"]
    row["evidence"] = pool.get("evidence_state", row.get("evidence_state", VERIFIED))
    row["evidence_state"] = pool.get("evidence_state", row.get("evidence_state", VERIFIED))
    row["verification_status"] = row["evidence_state"]
    row["automation_sufficiency"] = "COMPLETE" if row.get("automatic_decision") else "PARTIAL"
    row["source"] = {
        "file": row["source_file"],
        "source_file": row["source_file"],
        "url": row["source_url"],
        "source_url": row["source_url"],
        "pages": row["pages"],
        "pdf_page": row["pdf_page"],
        "printed_page": row["printed_page"],
        "source_reference": row["source_reference"],
        "original_clause": row["original_clause"],
        "curriculum_version": cohort,
        "pool_id": pool_id,
    }
    row["provenance"].update(row["source"])
    return row


def _math_primary_catalog(cohort: str) -> dict[str, Any]:
    """Return the independently transcribed Math primary catalogue contract."""

    handbook = _RULES.get("handbooks", {}).get(str(cohort), {})
    rules = handbook.get("math_rules", {}) if isinstance(handbook, Mapping) else {}
    catalog = rules.get("primary_catalog", {}) if isinstance(rules, Mapping) else {}
    return deepcopy(dict(catalog)) if isinstance(catalog, Mapping) else {}


def _math_course_map(value: Any) -> dict[str, int | float]:
    """Normalize one source course map while retaining source row credits."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for name, credits in value.items():
        if name in (None, "") or credits in (None, ""):
            continue
        try:
            numeric = float(credits)
        except (TypeError, ValueError):
            continue
        result[str(name)] = int(numeric) if numeric.is_integer() else numeric
    return result


def _math_expand_courses(
    courses: Mapping[str, int | float],
    aliases: Any,
) -> tuple[dict[str, int | float], dict[str, dict[str, Any]]]:
    """Add explicitly printed alternate names without changing requirements."""

    expanded = dict(courses)
    metadata: dict[str, dict[str, Any]] = {}
    if not isinstance(aliases, Mapping):
        return expanded, metadata
    for canonical, values in aliases.items():
        canonical_name = str(canonical)
        if canonical_name not in courses or not isinstance(values, (list, tuple)):
            continue
        for alias in values:
            alias_name = str(alias or "").strip()
            if not alias_name or alias_name in expanded:
                continue
            expanded[alias_name] = courses[canonical_name]
            metadata[alias_name] = {
                "canonical_name": canonical_name,
                "course_alias_of": canonical_name,
            }
    return expanded, metadata


def _math_catalog_source(
    source: Mapping[str, Any],
    catalog: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    sources = catalog.get("sources", {})
    section = sources.get(key, {}) if isinstance(sources, Mapping) else {}
    return _primary_section_source(source, section if isinstance(section, Mapping) else None)


def _cs_primary_catalog(cohort: str) -> dict[str, Any]:
    """Return the year-scoped CS primary catalogue contract.

    The catalogue is deliberately separate from ``department_courses``:
    that older map is also used by minor/target compatibility paths and does
    not carry the primary page's alpha/beta membership evidence.  Primary
    compilation therefore reads only the source-backed catalogue when it is
    present, with the old map retained as a conservative fallback.
    """

    handbook = _RULES.get("handbooks", {}).get(str(cohort), {})
    rules = handbook.get("cs_rules", {}) if isinstance(handbook, Mapping) else {}
    catalog = rules.get("primary_catalog", {}) if isinstance(rules, Mapping) else {}
    return deepcopy(dict(catalog)) if isinstance(catalog, Mapping) else {}


def _cs_catalog_source(
    source: Mapping[str, Any],
    catalog: Mapping[str, Any],
    key: str,
) -> dict[str, Any]:
    """Resolve one CS primary category to its official page contract."""

    sources = catalog.get("sources", {})
    section = sources.get(key, {}) if isinstance(sources, Mapping) else {}
    return _primary_section_source(source, section if isinstance(section, Mapping) else None)


def _cs_course_source(
    category_source: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay one exact CS handbook row on its category source contract."""

    scoped = deepcopy(dict(category_source))
    source_reference = str(row.get("source_reference") or "")
    pdf_page = row.get("pdf_page")
    if pdf_page not in (None, ""):
        scoped["pdf_page"] = pdf_page
        scoped["printed_page"] = row.get("printed_page") or pdf_page
        scoped["pages"] = f"PDF p.{pdf_page}"
    if source_reference:
        scoped["source_reference"] = source_reference
    source_table = str(row.get("source_table") or "").strip()
    if source_table:
        scoped["table_location"] = source_table
    name = str(row.get("course_name") or row.get("name") or "").strip()
    credits = row.get("credits", 0)
    note = str(row.get("original_note") or "").strip()
    scoped["original_clause"] = (
        f"{name} {float(credits):g} 學分；{note}"
        if note
        else f"{name} {float(credits):g} 學分；列於資科系主修課程表。"
    )
    return scoped


def _cs_catalog_rows(catalog: Mapping[str, Any], category: str) -> tuple[dict[str, Any], ...]:
    """Normalize draft-backed CS rows without inventing course identities."""

    raw_rows = catalog.get(category, ())
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes, bytearray)):
        return ()
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("course_name") or raw.get("name") or "").strip()
        if not name or raw.get("credits") in (None, ""):
            continue
        try:
            credits = float(raw["credits"])
        except (TypeError, ValueError):
            continue
        if credits <= 0:
            continue
        row = deepcopy(dict(raw))
        row["course_name"] = name
        row["credits"] = int(credits) if credits.is_integer() else credits
        domains = row.get("domains", ())
        row["domains"] = tuple(
            str(item).strip().lower()
            for item in domains
            if str(item).strip().lower() in {"common", "software", "network"}
        ) if isinstance(domains, Sequence) and not isinstance(domains, (str, bytes, bytearray)) else ()
        rows.append(row)
    return tuple(rows)


def _cs_candidate_data(
    catalog: Mapping[str, Any],
    cohort: str,
    category: str,
    requirement_id: str,
) -> tuple[dict[str, int | float], dict[str, dict[str, Any]]]:
    """Build exact candidate rows and server-owned membership evidence."""

    rows = _cs_catalog_rows(catalog, category)
    membership_id = f"cs_elective_{category}:{cohort}"
    candidates: dict[str, int | float] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["course_name"])
        credits = row["credits"]
        candidates[name] = credits
        domains = tuple(row.get("domains", ())) if category == "beta" else ()
        domain_ids = tuple(f"cs_beta_domain:{cohort}:{domain}" for domain in domains)
        memberships = (membership_id, *domain_ids)
        observed_ids = (requirement_id, *domain_ids)
        source_reference = str(row.get("source_reference") or "")
        note = str(row.get("original_note") or "").strip()
        metadata[name] = {
            "membership_ids": memberships,
            "subset_ids": memberships,
            "membership_source_reference": source_reference,
            "observed_requirement_ids": observed_ids,
            "source_domains": domains,
            "source_row": row.get("source_row"),
            "source_table": row.get("source_table"),
            "original_note": note,
            "pdf_page": row.get("pdf_page"),
            "source_reference": source_reference,
            "original_clause": (
                f"{name} {float(credits):g} 學分；{note}"
                if note
                else f"{name} {float(credits):g} 學分；列於資科系{category}類課程表。"
            ),
            "pool_membership_evidence": tuple(
                {
                    "membership_id": item,
                    "state": VERIFIED,
                    "source_reference": source_reference,
                    "kind": "official_handbook_membership",
                }
                for item in memberships
            ),
            "membership_assertions": tuple(
                {
                    "membership_id": item,
                    "state": VERIFIED,
                    "observed_requirement_ids": (requirement_id,),
                    "source_reference": source_reference,
                }
                for item in memberships
            ),
        }
    return candidates, metadata


def _math_domain_courses(
    catalog: Mapping[str, Any],
    domain: str,
    *,
    include_required: bool = True,
    include_electives: bool = True,
) -> tuple[dict[str, int | float], dict[str, dict[str, Any]]]:
    domains = catalog.get("domains", {})
    spec = domains.get(domain, {}) if isinstance(domains, Mapping) else {}
    if not isinstance(spec, Mapping):
        return {}, {}
    courses: dict[str, int | float] = {}
    if include_required:
        courses.update(_math_course_map(spec.get("required")))
    if include_electives:
        courses.update(_math_course_map(spec.get("electives")))
    return _math_expand_courses(courses, spec.get("aliases"))


def _math_department_candidates(
    catalog: Mapping[str, Any],
    cohort: str,
    selected_track: str | None,
) -> tuple[dict[str, int | float], dict[str, dict[str, Any]]]:
    """Build the year-specific professional candidate pool.

    The source tables list every domain and the ``其他`` rows.  A selected
    113+ domain's named required rows are removed from this broad elective
    pool so one transcript attempt cannot satisfy both the named requirement
    and the 65-credit professional total.
    """

    candidates: dict[str, int | float] = {}
    metadata: dict[str, dict[str, Any]] = {}
    domains = catalog.get("domains", {})
    selected_required: set[str] = set()
    if selected_track and isinstance(domains, Mapping):
        selected = domains.get(selected_track, {})
        if isinstance(selected, Mapping):
            selected_required = set(_math_course_map(selected.get("required")))
    if isinstance(domains, Mapping):
        for domain, spec in domains.items():
            domain_courses, domain_metadata = _math_domain_courses(catalog, str(domain))
            for name, credits in domain_courses.items():
                if name in selected_required:
                    continue
                candidates.setdefault(name, credits)
                if name in domain_metadata:
                    metadata.setdefault(name, {}).update(domain_metadata[name])
            if isinstance(spec, Mapping):
                for name in spec.get("alpha", ()) if isinstance(spec.get("alpha"), (list, tuple)) else ():
                    if name in candidates:
                        metadata.setdefault(name, {}).setdefault("subset_ids", []).append(f"math_alpha:{cohort}")
    other, other_metadata = _math_expand_courses(
        _math_course_map(catalog.get("other")),
        catalog.get("other_aliases"),
    )
    for name, credits in other.items():
        if name in selected_required:
            continue
        candidates.setdefault(name, credits)
        if name in other_metadata:
            metadata.setdefault(name, {}).update(other_metadata[name])
    requirement_ids = {
        "alpha": f"math_alpha:{cohort}",
        "external": f"external_department_or_school_professional:{cohort}",
    }
    for name in candidates:
        item = metadata.setdefault(name, {})
        memberships = item.setdefault("membership_assertions", [])
        memberships.append(
            {
                "membership_id": "external_department_or_school_professional",
                "state": "NOT_MEMBER",
                "observed_requirement_ids": (requirement_ids["external"],),
            }
        )
    return candidates, metadata


def _primary_pool_bundle(
    cohort: str,
    program: str,
    track: str | None,
    source: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Build scoped pool policies and quota rows for one primary curriculum."""

    handbook = _RULES.get("handbooks", {}).get(cohort, {})
    handbook = handbook if isinstance(handbook, Mapping) else {}
    shared = _RULES.get("shared", {}).get("university_common", {})
    shared = shared if isinstance(shared, Mapping) else {}
    university_contract = _university_common_contract(cohort)
    pools: dict[str, dict[str, Any]] = {}
    requirements: list[dict[str, Any]] = []
    university_policy_source = _policy_source(
        cohort,
        "university_common",
        source,
        program=program,
        track=track,
    )
    free_policy_source = _policy_source(
        cohort,
        "free_elective",
        source,
        program=program,
        track=track,
    )

    def policy_descriptor(
        policy_id: str,
        predicate: Mapping[str, Any],
        policy_source: Mapping[str, Any],
        *,
        automatic_decision: bool = True,
    ) -> dict[str, Any]:
        """Create the stable policy fields consumed by the policy adapter."""

        evidence_state = str(policy_source.get("evidence_state") or VERIFIED)
        coverage_state = str(policy_source.get("coverage_state") or COMPLETE)
        source_automatic = policy_source.get("automatic_decision")
        if source_automatic is False:
            automatic_decision = False

        return {
            "policy_id": policy_id,
            "revision": f"{cohort}.1",
            "applies_to": {
                "curriculum_versions": (cohort,),
                "program_slugs": (program,),
                "track_slugs": (track or "department",),
                "roles": ("primary",),
            },
            "predicate": deepcopy(dict(predicate)),
            "evidence_state": evidence_state,
            "coverage_state": coverage_state,
            "automatic_decision": automatic_decision,
            "source_reference": policy_source.get("source_reference", ""),
            "source_url": policy_source.get("source_url", ""),
            "source_file": policy_source.get("source_file", ""),
            "pdf_page": policy_source.get("pdf_page", "未標示"),
            "printed_page": policy_source.get("printed_page", "未標示"),
            "original_clause": policy_source.get("original_clause", ""),
            "policy_source": deepcopy(dict(policy_source)),
        }

    def add(
        bucket: str,
        label: str,
        candidates: Mapping[str, int | float] | None = None,
        *,
        source_override: Mapping[str, Any] | None = None,
        candidate_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        required_credits: int | float | None = None,
        selection_rule: str = "exact_title_credit_component",
        policy: Mapping[str, Any] | None = None,
        coverage_state: str = COMPLETE,
        evidence_state: str = VERIFIED,
        manual_reason: str = "",
        requirement: bool = False,
        requirement_type: str = "course_pool",
        choice_group: str | None = None,
        choice_rule: str | None = "at_least_credits_from_pool",
        overflow_buckets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        pool = _pool_record(
            kind="primary",
            cohort=cohort,
            program=program,
            track=track,
            bucket=bucket,
            label=label,
            source=source_override or source,
            candidates=candidates,
            candidate_metadata=candidate_metadata,
            required_credits=required_credits,
            evidence_state=evidence_state,
            coverage_state=coverage_state,
            selection_rule=selection_rule,
            policy=policy,
            manual_reason=manual_reason,
        )
        pools[pool["id"]] = pool
        if requirement and required_credits is not None and float(required_credits) > 0:
            overflow_routes = tuple(
                f"primary:{_canonical_id('primary', cohort, program, track)}:{program}.primary.{cohort}.{track or 'department'}.pool.{_pool_token(item)}"
                for item in overflow_buckets
            )
            requirements.append(
                _primary_pool_requirement_row(
                    cohort,
                    program,
                    track,
                    pool,
                    source,
                    suffix=bucket,
                    requirement_type=requirement_type,
                    choice_group=choice_group,
                    choice_rule=choice_rule,
                    overflow_routes=overflow_routes,
                )
            )
        return pool

    compulsory = shared.get("compulsory", {}) if isinstance(shared, Mapping) else {}
    compulsory = compulsory if isinstance(compulsory, Mapping) else {}
    compulsory_courses = university_contract.get("compulsory_courses", {})
    add(
        "university_compulsory",
        "校共同必修",
        compulsory_courses if isinstance(compulsory_courses, Mapping) else {},
        required_credits=university_contract.get("compulsory_total", compulsory.get("total_req", 10)),
        policy={
            "allowed_scope": "university_common",
            "exact_title_and_credits": True,
            "excluded_from_free": True,
        },
    )
    ge = shared.get("ge_categories", {}) if isinstance(shared, Mapping) else {}
    categories = ge.get("categories", {}) if isinstance(ge, Mapping) else {}
    per_category = university_contract.get(
        "category_min_each",
        ge.get("per_category_req", 4) if isinstance(ge, Mapping) else 4,
    )
    if isinstance(categories, Mapping):
        for category, aliases in categories.items():
            add(
                f"ge_{category}",
                f"通識分類：{category}",
                {},
                required_credits=per_category,
                selection_rule="official_category_policy",
                policy={
                    **policy_descriptor(
                        f"university_common.ge_category.{cohort}.{_pool_token(category)}",
                        {
                            "kind": "official_category_membership",
                            "category": category,
                            "minimum_credits": float(per_category),
                            "candidate_aliases_are_not_authoritative": True,
                        },
                        university_policy_source,
                    ),
                    "category": category,
                    "title_aliases": tuple(aliases) if isinstance(aliases, (list, tuple)) else (),
                    "allowed_scope": "university_common",
                    "excluded_from_free": True,
                },
            )
    add(
        "ge_common_elective",
        "通識共同選修",
        {},
        required_credits=university_contract.get(
            "common_elective_min",
            shared.get("ge_common_elective_req", 2) if isinstance(shared, Mapping) else 2,
        ),
        selection_rule="official_category_policy",
        policy={
            **policy_descriptor(
                f"university_common.ge_common_elective.{cohort}",
                {
                    "kind": "official_category_membership",
                    "category": "common_elective",
                    "minimum_credits": float(
                        university_contract.get(
                            "common_elective_min",
                            shared.get("ge_common_elective_req", 2) if isinstance(shared, Mapping) else 2,
                        )
                    ),
                    "candidate_aliases_are_not_authoritative": True,
                },
                university_policy_source,
            ),
            "allowed_scope": "university_common",
            "category": "common_elective",
            "excluded_from_free": True,
        },
    )
    free_amount = float(free_policy_source.get("total_req") or 15)
    free_amount_semantics = str(free_policy_source.get("amount_semantics") or "MINIMUM").upper()
    raw_requirement_minimum = free_policy_source.get("requirement_minimum_credits")
    if raw_requirement_minimum not in (None, ""):
        free_requirement_minimum = float(raw_requirement_minimum)
    elif free_amount_semantics not in {"MAXIMUM", "SUBSET_MAXIMUM"}:
        free_requirement_minimum = free_amount
    else:
        # 111/112 use the 15-credit value only for the external-professional
        # subset; it is not an independent free-elective minimum.
        free_requirement_minimum = None
    raw_subset_maxima = free_policy_source.get("subset_maxima")
    free_subset_maxima = (
        deepcopy(dict(raw_subset_maxima))
        if isinstance(raw_subset_maxima, Mapping)
        else {}
    )
    # The 113–115 Math handbooks state the same 15-credit external
    # professional cap even when the shared policy payload predates the
    # per-program subset field.  Keep the source payload untouched in the
    # audit copy, while making the selected non-teacher policy executable.
    if program == "math" and cohort not in {"111", "112"}:
        free_subset_maxima.setdefault("external_department_or_school_professional", {})[
            "non_teacher"
        ] = 15
    free_is_legacy_maximum = (
        free_requirement_minimum is None
        and free_amount_semantics == "MAXIMUM"
        and not free_subset_maxima
    )
    free_science_minimum = float(free_policy_source.get("minimum_science_college_credits") or 0)
    free_science_state = str(free_policy_source.get("science_subset_state") or "NOT_STATED").upper()
    free_predicate: dict[str, Any] = {
        "kind": "exclusive_open_elective",
        "amount_semantics": free_amount_semantics,
        "candidate_aliases_are_not_authoritative": True,
    }
    if free_requirement_minimum is not None:
        free_predicate["minimum_credits"] = free_requirement_minimum
    elif free_is_legacy_maximum:
        free_predicate["maximum_credits"] = free_amount
        maximum_by_student_type = free_policy_source.get("maximum_credits_by_student_type")
        if isinstance(maximum_by_student_type, Mapping):
            free_predicate["maximum_credits_by_student_type"] = deepcopy(dict(maximum_by_student_type))
    if free_subset_maxima:
        free_predicate["subset_maxima"] = deepcopy(free_subset_maxima)
        subset_constraints: list[dict[str, Any]] = []
        active_student_type = "non_teacher" if program == "math" else None
        for subset_id, limits in free_subset_maxima.items():
            if not isinstance(limits, Mapping):
                continue
            for student_type, maximum in limits.items():
                if maximum in (None, ""):
                    continue
                if active_student_type and str(student_type) != active_student_type:
                    continue
                subset_constraints.append(
                    {
                        "constraint_id": f"{subset_id}:{student_type}:maximum",
                        "subset_id": str(subset_id),
                        "student_type": str(student_type),
                        "maximum_credits": float(maximum),
                    }
                )
        if subset_constraints:
            free_predicate["subset_constraints"] = subset_constraints
        if active_student_type:
            free_predicate["student_type"] = active_student_type
    if free_science_minimum > 0 and free_science_state == VERIFIED:
        free_predicate.setdefault("subset_constraints", []).append(
            {
                "constraint_id": "science_college_minimum",
                "membership_id": "science_college",
                "minimum_credits": free_science_minimum,
            }
        )
    # CS 甲類 rows are named requirements, so a matching attempt cannot be
    # reused to satisfy the open-free pool.  ``exclude_pool_ids`` is the
    # executable identity guard used by the allocator; the explicit
    # membership value remains available to the policy/subset adapters and
    # makes the source contract auditable without trusting student input.
    if program == "cs":
        alpha_membership_id = f"cs_elective_alpha:{cohort}"
        alpha_pool_id = _pool_id("primary", cohort, program, track, "cs_elective_alpha")
        free_predicate["excluded_membership_ids"] = (alpha_membership_id,)
        free_predicate["exclude_pool_ids"] = (alpha_pool_id,)
    free_policy = {
        **policy_descriptor(
            f"university_common.free_total.{cohort}",
            free_predicate,
            free_policy_source,
        ),
        "allowed_scope": "official_course_catalog",
        "amount_semantics": free_amount_semantics,
        "total_req": free_amount,
        "requirement_minimum_credits": free_requirement_minimum,
        "subset_maxima": deepcopy(free_subset_maxima),
        "active_subset_maxima": (
            {
                subset_id: {"non_teacher": limits.get("non_teacher")}
                for subset_id, limits in free_subset_maxima.items()
                if isinstance(limits, Mapping) and limits.get("non_teacher") not in (None, "")
            }
            if program == "math"
            else {}
        ),
        "allow_external_departments": bool(free_policy_source.get("allow_external_departments", True)),
        "minimum_science_college_credits": free_science_minimum,
        "science_subset_state": free_science_state,
        "exclude_allocated_attempts": bool(free_policy_source.get("exclude_allocated_attempts", True)),
        "requires_official_course_catalog": bool(free_policy_source.get("requires_official_course_catalog", True)),
    }
    if program == "cs":
        free_policy.update(
            {
                "excluded_membership_ids": (f"cs_elective_alpha:{cohort}",),
                "exclude_pool_ids": (_pool_id("primary", cohort, program, track, "cs_elective_alpha"),),
            }
        )
    add(
        "free_elective",
        "自由選修",
        {},
        required_credits=free_requirement_minimum,
        selection_rule="official_open_elective_policy",
        policy=free_policy,
        requirement=free_requirement_minimum is not None,
    )

    if program == "earth":
        earth = handbook.get("earth_life_major", {})
        earth = earth if isinstance(earth, Mapping) else {}
        common = earth.get("common_compulsory", {})
        common = common if isinstance(common, Mapping) else {}
        common_courses = common.get("courses", {})
        add(
            "common_compulsory",
            "地生共同必修",
            common_courses if isinstance(common_courses, Mapping) else {},
            required_credits=common.get("total_req", 24),
            policy={"allowed_scope": "earth_common", "exact_title_and_credits": True},
        )
        domain_label = "地球環境" if track == "earth_environment" else "生命科學"
        domains = earth.get("domains", {})
        selected_domain = domains.get(domain_label, {}) if isinstance(domains, Mapping) else {}
        selected_domain = selected_domain if isinstance(selected_domain, Mapping) else {}
        domain_compulsory = selected_domain.get("compulsory", {})
        domain_electives = selected_domain.get("electives", {})
        add(
            "domain_required",
            f"{domain_label}專業領域必修",
            domain_compulsory if isinstance(domain_compulsory, Mapping) else {},
            required_credits=14,
            policy={"allowed_scope": "earth_domain", "domain": domain_label, "exact_title_and_credits": True},
        )
        common_electives = earth.get("common_electives", {})
        common_electives = common_electives if isinstance(common_electives, Mapping) else {}
        common_elective_requirement = 12 if cohort == "111" else None
        domain_elective_credits = 22 if cohort == "111" else 20
        other_credits = 13 if cohort == "111" else 27
        add(
            "common_elective",
            "地生共同選修",
            common_electives,
            required_credits=common_elective_requirement,
            policy={"allowed_scope": "earth_common_elective", "exact_title_and_credits": True},
            requirement=common_elective_requirement is not None,
            overflow_buckets=("department_professional",),
        )
        add(
            "domain_elective",
            f"{domain_label}專業領域選修",
            domain_electives if isinstance(domain_electives, Mapping) else {},
            required_credits=domain_elective_credits,
            policy={"allowed_scope": "earth_domain_elective", "domain": domain_label, "exact_title_and_credits": True},
            requirement=True,
            overflow_buckets=("department_professional",),
        )
        all_professional: dict[str, int | float] = {}
        for domain_data in (domains.values() if isinstance(domains, Mapping) else ()):
            if not isinstance(domain_data, Mapping):
                continue
            for field in ("compulsory", "electives"):
                values = domain_data.get(field, {})
                if isinstance(values, Mapping):
                    all_professional.update(values)
        all_professional.update(common_electives)
        add(
            "department_professional",
            "地生系內／全系專業課程",
            all_professional,
            required_credits=other_credits,
            policy={
                "allowed_scope": "earth_department_professional",
                "department": "地球環境暨生物資源學系",
                "exclude_non_professional": True,
                "exact_title_and_credits": True,
            },
            requirement=True,
        )
        alternatives = earth.get("common_alternatives", ())
        if isinstance(alternatives, (list, tuple)):
            for index, alternative in enumerate(alternatives, start=1):
                if not isinstance(alternative, Mapping):
                    continue
                options = alternative.get("options", {})
                if not isinstance(options, Mapping):
                    continue
                pool = add(
                    f"common_alternative_{index}",
                    str(alternative.get("label") or f"共同必修選擇 {index}"),
                    options,
                    required_credits=alternative.get("required_credits", 0),
                    selection_rule="one_of_exact_title_credit",
                    policy={"allowed_scope": "earth_common", "choice_count": 1},
                    coverage_state=COMPLETE,
                    requirement=True,
                    requirement_type="choice",
                    choice_group=f"earth_common_alternative_{index}",
                    choice_rule="one_of",
                )
                # The row above is a pool requirement only when the source
                # declares a positive amount; keep the candidate pool even
                # when malformed/empty input is encountered.
                del pool
    elif program == "apc":
        apc = handbook.get("apc_rules", {})
        apc = apc if isinstance(apc, Mapping) else {}
        common_catalog = apc.get("common_catalog")
        if not isinstance(common_catalog, Mapping):
            common_catalog = {
                **(
                    apc.get("basic_core", {})
                    if isinstance(apc.get("basic_core", {}), Mapping)
                    else {}
                ),
                **(
                    apc.get("shared_other_required", {})
                    if isinstance(apc.get("shared_other_required", {}), Mapping)
                    else {}
                ),
            }
        common_by_track = apc.get("common_required_by_track", {})
        common_by_track = common_by_track if isinstance(common_by_track, Mapping) else {}
        division_label = "物理組" if track == "physics" else "化學組"
        required_common = common_by_track.get(division_label)
        if not isinstance(required_common, Mapping):
            required_common = common_catalog
        common_evidence = _primary_section_source(
            source,
            apc.get("common_catalog_evidence"),
        )
        required_credits = sum(float(value) for value in required_common.values())
        add(
            "apc_common_compulsory",
            "物化系共同必修",
            required_common,
            source_override=common_evidence,
            required_credits=required_credits,
            policy={"allowed_scope": "apc_common", "exact_title_and_credits": True},
        )
        # Keep the other group's experiments as auditable candidate evidence,
        # but outside the selected common requirement.  They can only be
        # considered later by the official open-free policy.
        cross_track_candidates = {
            name: credits
            for name, credits in common_catalog.items()
            if name not in required_common
        }
        if cross_track_candidates:
            add(
                "apc_cross_track_lab_candidates",
                "物化系跨組實驗候選（不可直接列入本組必修）",
                cross_track_candidates,
                source_override=common_evidence,
                policy={
                    "allowed_scope": "apc_cross_track_lab_candidate",
                    "eligible_for_free_elective_policy": True,
                    "track_required": division_label,
                    "exact_title_and_credits": True,
                },
            )
        division = apc.get("divisions", {}).get(division_label, {}) if isinstance(apc.get("divisions", {}), Mapping) else {}
        compulsory_values = division.get("compulsory", {}) if isinstance(division, Mapping) else {}
        required_evidence_by_track = apc.get("required_evidence_by_track", {})
        required_evidence_by_track = (
            required_evidence_by_track
            if isinstance(required_evidence_by_track, Mapping)
            else {}
        )
        add(
            "apc_track_compulsory",
            f"物化{division_label}專業必修",
            compulsory_values if isinstance(compulsory_values, Mapping) else {},
            source_override=_primary_section_source(
                source,
                required_evidence_by_track.get(division_label),
            ),
            required_credits=sum(float(value) for value in compulsory_values.values()) if isinstance(compulsory_values, Mapping) else 0,
            policy={"allowed_scope": "apc_track", "track": division_label, "exact_title_and_credits": True},
        )
        elective_catalogs = apc.get("primary_electives", {})
        elective_catalogs = elective_catalogs if isinstance(elective_catalogs, Mapping) else {}
        elective_spec = elective_catalogs.get(division_label, {})
        elective_spec = elective_spec if isinstance(elective_spec, Mapping) else {}
        elective_courses = elective_spec.get("courses", {})
        elective_courses = elective_courses if isinstance(elective_courses, Mapping) else {}
        elective_evidence = elective_spec.get("evidence", {})
        elective_evidence = elective_evidence if isinstance(elective_evidence, Mapping) else {}
        elective_metadata = elective_spec.get("course_metadata", {})
        elective_metadata = elective_metadata if isinstance(elective_metadata, Mapping) else {}
        elective_state = str(elective_evidence.get("evidence_state") or VERIFIED)
        elective_coverage = str(elective_evidence.get("coverage_state") or COMPLETE)
        elective_reason = str(
            elective_evidence.get("manual_reason")
            or elective_evidence.get("manual_review_reason")
            or ""
        )
        add(
            "apc_track_elective",
            f"物化{division_label}專業選修",
            elective_courses,
            source_override=_primary_section_source(source, elective_evidence),
            candidate_metadata=elective_metadata,
            required_credits=25 if cohort == "115" else 24 if track == "chemistry" else 25,
            selection_rule="official_department_elective_policy",
            policy={"allowed_scope": "apc_track_elective", "track": division_label, "department_approval_required": True},
            evidence_state=elective_state,
            coverage_state=elective_coverage,
            manual_reason=elective_reason,
            requirement=True,
        )
    elif program == "cs":
        cs = handbook.get("cs_rules", {})
        cs = cs if isinstance(cs, Mapping) else {}
        primary_catalog = _cs_primary_catalog(cohort)
        configured_required = primary_catalog.get("named_core") if primary_catalog else cs.get("primary_named_core")
        required = (
            dict(configured_required)
            if isinstance(configured_required, Mapping)
            else {
                "計算機概論": 3,
                "Java程式設計": 3,
                "離散數學": 3,
                "C程式設計": 3,
                "資料結構": 3,
                "數位電子學": 3,
                "線性代數": 3,
                "演算法": 3,
                "數位系統設計": 3,
                "作業系統": 3,
                "資訊專題(I)": 1,
            }
        )
        required_source = (
            _cs_catalog_source(source, primary_catalog, "required")
            if primary_catalog
            else source
        )
        add(
            "cs_department_required",
            "資科系專業必修",
            required,
            source_override=required_source,
            required_credits=31,
            policy={"allowed_scope": "cs_required", "exact_title_and_credits": True},
        )

        # The 54-credit elective block is composed of 12 named alpha rows
        # (32 credits) plus one beta remainder (22 credits).  Keep alpha as
        # a candidate pool only: its individual named rows are emitted by
        # _course_catalog below, so a missing alpha course cannot be replaced
        # by another beta course or by the free-elective pool.
        alpha_requirement_id = f"cs.primary.{cohort}.alpha"
        alpha_candidates, alpha_metadata = _cs_candidate_data(
            primary_catalog,
            cohort,
            "alpha",
            alpha_requirement_id,
        )
        alpha_source = (
            _cs_catalog_source(source, primary_catalog, "alpha")
            if primary_catalog
            else source
        )
        alpha_pool = add(
            "cs_elective_alpha",
            "資科系甲類專業選修（指定課程候選）",
            alpha_candidates,
            source_override=alpha_source,
            candidate_metadata=alpha_metadata,
            selection_rule="exact_title_credit_component",
            policy={
                "allowed_scope": "cs_elective_alpha",
                "membership_id": f"cs_elective_alpha:{cohort}",
                "required_named_courses": True,
                "excluded_from_free": True,
                "exact_title_and_credits": True,
                "rollup_group_id": "cs_primary_elective_54",
                "rollup_required_credits": 54,
            },
            coverage_state=COMPLETE,
        )
        alpha_pool.update(
            {
                "membership_id": f"cs_elective_alpha:{cohort}",
                "membership_ids": (f"cs_elective_alpha:{cohort}",),
                "excluded_from_free": True,
                "rollup_group_id": "cs_primary_elective_54",
                "rollup_required_credits": 54,
            }
        )

        beta_requirement_id = f"cs.primary.{cohort}.beta_remainder"
        beta_candidates, beta_metadata = _cs_candidate_data(
            primary_catalog,
            cohort,
            "beta",
            beta_requirement_id,
        )
        beta_source = (
            _cs_catalog_source(source, primary_catalog, "beta")
            if primary_catalog
            else source
        )
        beta_domains = primary_catalog.get("beta_domains", {}) if primary_catalog else {}
        beta_domains = beta_domains if isinstance(beta_domains, Mapping) else {}
        beta_constraints: list[dict[str, Any]] = []
        for domain, domain_spec in beta_domains.items():
            if not isinstance(domain_spec, Mapping):
                continue
            membership_id = str(
                domain_spec.get("membership_id")
                or f"cs_beta_domain:{cohort}:{domain}"
            )
            observed_ids = (
                beta_requirement_id,
                f"primary:primary:{cohort}:cs:{beta_requirement_id}",
            )
            beta_constraints.append(
                {
                    "constraint_id": membership_id,
                    "membership_id": membership_id,
                    "subset_id": membership_id,
                    "minimum_course_count": 1,
                    "observed_requirement_ids": observed_ids,
                    "source_reference": beta_source.get("source_reference", ""),
                    "evidence_state": VERIFIED,
                }
            )
        beta_membership_id = f"cs_elective_beta:{cohort}"
        beta_predicate = {
            "kind": "exclusive_cs_beta_remainder",
            "membership_id": beta_membership_id,
            "required_credits": 22,
            "minimum_credits": 22,
            "observed_requirement_ids": (
                beta_requirement_id,
                f"primary:primary:{cohort}:cs:{beta_requirement_id}",
            ),
            "subset_constraints": beta_constraints,
            "minimum_course_count": 1,
            "count_unique_attempts": True,
            "exclusive_only": True,
            "exclude_free_and_unallocated": True,
            "overflow_to_free": True,
            "rollup_group_id": "cs_primary_elective_54",
            "rollup_required_credits": 54,
        }
        beta_policy = {
            **policy_descriptor(
                f"cs.primary.beta_remainder.{cohort}",
                beta_predicate,
                beta_source,
            ),
            "allowed_scope": "cs_elective_beta",
            "membership_id": beta_membership_id,
            "membership_ids": (beta_membership_id,),
            "required_credits": 22,
            "minimum_course_count": 1,
            "subset_constraints": beta_constraints,
            "exclusive": True,
            "overflow_to_free": True,
            "rollup_group_id": "cs_primary_elective_54",
            "rollup_required_credits": 54,
            "exact_title_and_credits": True,
        }
        beta_pool = add(
            "cs_elective_beta",
            "資科系乙類專業選修（領域候選）",
            beta_candidates,
            source_override=beta_source,
            candidate_metadata=beta_metadata,
            required_credits=22,
            selection_rule="official_category_elective_policy",
            policy=beta_policy,
            coverage_state=COMPLETE,
            requirement=True,
            overflow_buckets=("free_elective",),
        )
        beta_pool.update(
            {
                "membership_id": beta_membership_id,
                "membership_ids": (beta_membership_id,),
                "minimum_course_count": 1,
                "subset_constraints": deepcopy(beta_constraints),
                "rollup_group_id": "cs_primary_elective_54",
                "rollup_required_credits": 54,
            }
        )
        if requirements:
            # ``add`` derives the pool row ID from the bucket.  Publish a
            # short stable requirement ID for the Core subset adapter while
            # retaining the pool ID and all source provenance.
            remainder = requirements[-1]
            old_requirement_id = remainder.get("requirement_id")
            remainder["id"] = beta_requirement_id
            remainder["requirement_id"] = beta_requirement_id
            remainder["source_assertion_id"] = beta_requirement_id
            remainder["assertion_id"] = beta_requirement_id
            remainder["official_course_identity"] = f"{cohort}:cs:department:pool:beta_remainder"
            remainder["observed_requirement_ids"] = (
                beta_requirement_id,
                f"primary:primary:{cohort}:cs:{beta_requirement_id}",
            )
            # Keep the subset contract on the executable row as well as on
            # the pool policy.  The current compiler preserves row-level
            # constraints even when a custom selection rule is not a public
            # policy descriptor; Core can therefore enforce the domain
            # witnesses without inventing another credit consumer.
            remainder["membership_id"] = beta_membership_id
            remainder["membership_ids"] = (beta_membership_id,)
            remainder["minimum_course_count"] = 1
            remainder["subset_constraints"] = deepcopy(beta_constraints)
            remainder["exclusive"] = True
            remainder["overflow_to_free"] = True
            remainder["rollup_group_id"] = "cs_primary_elective_54"
            remainder["rollup_required_credits"] = 54
            remainder.setdefault("provenance", {})["requirement_id"] = beta_requirement_id
            remainder["provenance"]["legacy_requirement_id"] = old_requirement_id
    else:
        math_rules = handbook.get("math_rules", {})
        math_rules = math_rules if isinstance(math_rules, Mapping) else {}
        primary_catalog = _math_primary_catalog(cohort)
        if primary_catalog:
            common_math = _math_course_map(primary_catalog.get("common_required"))
            if not common_math:
                common_math = _math_course_map(math_rules.get("common_compulsory"))
            common_required_credits = sum(float(value) for value in common_math.values())
            common_math, common_metadata = _math_expand_courses(
                common_math,
                primary_catalog.get("common_aliases"),
            )
            add(
                "math_common_compulsory",
                "數學系共同必修",
                common_math,
                source_override=_math_catalog_source(source, primary_catalog, "common"),
                candidate_metadata=common_metadata,
                required_credits=common_required_credits,
                policy={
                    "allowed_scope": "math_common",
                    "student_type": "non_teacher",
                    "exact_title_and_credits": True,
                },
            )

            selected_domain = track if track in set(_MATH_PRIMARY_TRACKS.get(cohort, ())) else None
            selected_required: dict[str, int | float] = {}
            selected_source = _math_catalog_source(source, primary_catalog, selected_domain or "professional")
            if selected_domain:
                domain_spec = (
                    primary_catalog.get("domains", {}).get(selected_domain, {})
                    if isinstance(primary_catalog.get("domains", {}), Mapping)
                    else {}
                )
                if isinstance(domain_spec, Mapping):
                    selected_required = _math_course_map(domain_spec.get("required"))
                add(
                    "math_domain_required",
                    f"數學系{_TRACK_DISPLAY.get(selected_domain, selected_domain)}領域必修",
                    selected_required,
                    source_override=selected_source,
                    required_credits=sum(float(value) for value in selected_required.values()),
                    policy={
                        "allowed_scope": "math_domain",
                        "domain": selected_domain,
                        "student_type": "non_teacher",
                        "membership_id": f"math_domain:{cohort}:{selected_domain}",
                        "observed_requirement_ids": (f"math.primary.{cohort}.{selected_domain}.required",),
                        "exact_title_and_credits": True,
                    },
                    # The selected domain's named rows are the authoritative
                    # required consumers.  Keep this pool for candidate and
                    # provenance discovery, but do not emit a second quota
                    # consumer for the same domain credits.
                    requirement=False,
                )
            else:
                # 111/112 have no selectable domain requirement.  Keep an
                # empty named bucket for stable pool discovery without
                # inventing an extra requirement or unioning all domains.
                add(
                    "math_domain_required",
                    "數學系專業領域課程（非獨立必修）",
                    {},
                    source_override=_math_catalog_source(source, primary_catalog, "professional"),
                    policy={
                        "allowed_scope": "math_domain_catalog",
                        "student_type": "non_teacher",
                        "candidate_only": True,
                    },
                )

            department_courses, department_metadata = _math_department_candidates(
                primary_catalog,
                cohort,
                selected_domain,
            )
            department_requirement_id = (
                f"{program}.primary.{cohort}.{track or 'department'}.pool.math_department_elective"
            )
            subset_constraints: list[dict[str, Any]] = []
            alpha_minimum = primary_catalog.get("alpha_minimum_credits")
            if cohort in {"111", "112"} and alpha_minimum not in (None, "", 0):
                alpha_id = f"math_alpha:{cohort}"
                subset_constraints.append(
                    {
                        "constraint_id": f"{alpha_id}:minimum",
                        "membership_id": alpha_id,
                        "subset_id": alpha_id,
                        "minimum_credits": float(alpha_minimum),
                        "observed_requirement_ids": (department_requirement_id,),
                    }
                )
            if cohort in {"111", "112"}:
                subset_constraints.append(
                    {
                        "constraint_id": f"external_department_or_school_professional:{cohort}:maximum",
                        "membership_id": "external_department_or_school_professional",
                        "subset_id": "external_department_or_school_professional",
                        "maximum_credits": 15.0,
                        "student_type": "non_teacher",
                        "observed_requirement_ids": (department_requirement_id,),
                    }
                )
            department_minimum = primary_catalog.get("department_elective_minimum_by_track", {}).get(
                selected_domain or "department",
                64 if cohort in {"111", "112"} else max(0, 65 - int(sum(selected_required.values()))),
            )
            department_policy = {
                "allowed_scope": "math_department",
                # Modern handbooks allow the same eligible departmental
                # elective to supply the remaining free-elective credits.
                # The allocator checks destination eligibility separately.
                "overflow_to_free": cohort not in {"111", "112"},
                "student_type": "non_teacher",
                "membership_id": f"math_department:{cohort}",
                "observed_requirement_ids": (f"math.primary.{cohort}.department_elective",),
                "department_approval_required": True,
                "exact_title_and_credits": True,
            }
            if subset_constraints:
                department_policy["subset_constraints"] = subset_constraints
            add(
                "math_department_elective",
                "數學系專業選修",
                department_courses,
                source_override=_math_catalog_source(source, primary_catalog, "professional"),
                candidate_metadata=department_metadata,
                required_credits=department_minimum,
                selection_rule="official_department_elective_policy",
                policy=department_policy,
                coverage_state=COMPLETE,
                requirement=True,
            )
        else:
            # Conservative compatibility path for a malformed/legacy
            # handbook config.  Normal checked-in Math cohorts all carry the
            # source-backed primary_catalog above.
            common_math = math_rules.get("common_compulsory", {})
            add(
                "math_common_compulsory",
                "數學系共同必修",
                common_math if isinstance(common_math, Mapping) else {},
                required_credits=sum(float(value) for value in common_math.values()) if isinstance(common_math, Mapping) else 0,
                policy={"allowed_scope": "math_common", "exact_title_and_credits": True},
            )
            add(
                "math_domain_required",
                "數學系專業領域必修",
                {},
                policy={"allowed_scope": "math_domain", "exact_title_and_credits": True},
            )
            add(
                "math_department_elective",
                "數學系專業選修",
                {},
                required_credits=64 if cohort in {"111", "112"} else 65,
                selection_rule="official_department_elective_policy",
                policy={"allowed_scope": "math_department", "department_approval_required": True},
                coverage_state=PARTIAL,
                manual_reason="手冊明列選修額度與分類限制；本地逐課候選池仍需依該年度表格補齊。",
                requirement=True,
            )

    return pools, requirements


def _primary_zero_credit_source(
    cohort: str,
    program: str,
    series: str,
    source: Mapping[str, Any],
    *,
    original_clause: str,
) -> dict[str, Any] | None:
    """Return the year/program-scoped source for a primary zero-credit gate.

    The handbook tables describe an aggregate series, while the public course
    catalogue supplies the term-bound course membership later.  Keep those
    two facts separate here: this source object records only the reviewed
    primary-table evidence and never invents an offering identity.
    """

    shared = _RULES.get("shared", {})
    policy = shared.get("primary_zero_credit", {}) if isinstance(shared, Mapping) else {}
    if not isinstance(policy, Mapping):
        return None
    series_contract = policy.get(series, {})
    if not isinstance(series_contract, Mapping):
        return None
    evidence_by_cohort = series_contract.get("evidence_by_cohort", {})
    cohort_contract = (
        evidence_by_cohort.get(str(cohort), {})
        if isinstance(evidence_by_cohort, Mapping)
        else {}
    )
    evidence = cohort_contract.get(program, {}) if isinstance(cohort_contract, Mapping) else {}
    if not isinstance(evidence, Mapping) or not evidence or bool(evidence.get("omitted")):
        # An omitted or deleted source row is provenance, not a required=false
        # placeholder.  The caller therefore emits no active requirement.
        return None

    research_by_program = policy.get("research_file_by_program", {})
    research_file = (
        research_by_program.get(program)
        if isinstance(research_by_program, Mapping)
        else None
    ) or source.get("research_file", "")
    pdf_page = str(evidence.get("pdf_page") or "未標示")
    printed_page = str(evidence.get("printed_page") or "未標示")
    pages = str(evidence.get("pages") or pdf_page)
    page_token = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", pdf_page).strip("-").lower()
    source_reference = str(
        evidence.get("source_reference")
        or f"handbook:{cohort}:primary:{program}:{series}:pdf:{page_token or 'unmarked'}"
    )
    source_file = str(evidence.get("source_file") or source.get("source_file") or _source_file(cohort))
    source_url = str(evidence.get("source_url") or source.get("source_url") or _HANDBOOK_URLS.get(cohort, ""))
    result = deepcopy(dict(source))
    result.update(
        {
            "source_type": str(policy.get("source_type") or "official_primary_handbook_section"),
            "research_file": str(research_file),
            "source_url": source_url,
            "url": source_url,
            "source_file": source_file,
            "file": source_file,
            "pdf_page": pdf_page,
            "printed_page": printed_page,
            "pages": pages,
            "table_location": str(evidence.get("table_location") or "官方主修課程表"),
            "source_reference": source_reference,
            "original_clause": str(evidence.get("original_clause") or original_clause),
            "evidence_state": str(evidence.get("evidence_state") or VERIFIED),
            "verification_status": str(evidence.get("evidence_state") or VERIFIED),
            "coverage_state": str(evidence.get("coverage_state") or COMPLETE),
            "automation_sufficiency": str(evidence.get("automation_sufficiency") or COMPLETE),
            "automatic_decision": bool(evidence.get("automatic_decision", True)),
            "manual_reason": str(evidence.get("manual_reason") or ""),
            "manual_review_reason": str(evidence.get("manual_review_reason") or evidence.get("manual_reason") or ""),
        }
    )
    if evidence.get("amendment_action"):
        result["amendment_action"] = str(evidence["amendment_action"])
    return result


def _primary_zero_credit_descriptor(
    cohort: str,
    program: str,
    track: str | None,
    series: str,
    source: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Build one source-backed primary non-credit series descriptor.

    ``applicable_*`` describes the selected registry record.  The membership
    flags stay false because a server-owned GE/department course listing may
    not carry the student's programme or track as offering metadata.
    """

    shared = _RULES.get("shared", {})
    policy = shared.get("primary_zero_credit", {}) if isinstance(shared, Mapping) else {}
    contract = policy.get(series, {}) if isinstance(policy, Mapping) else {}
    if not isinstance(contract, Mapping):
        return None
    name = str(contract.get("name") or ("大學生活學習與輔導" if series == "life_guidance" else "服務學習"))
    track_slug = track if program in {"earth", "apc"} else "department"
    if series == "life_guidance" and program == "cs":
        eligible_names = tuple(f"{name} Part {index}" for index in range(1, 9))
    else:
        eligible_names = (name,)
    hours_by_program = contract.get("hours_by_program", {})
    hours = hours_by_program.get(program, {}) if isinstance(hours_by_program, Mapping) else {}
    hours = hours if isinstance(hours, Mapping) else {}
    if series == "life_guidance":
        if program == "cs":
            clause = "各學期「大學生活學習與輔導 Part 1–8」為 0 學分，第一至四年共 8 學期。"
        else:
            clause = "各學期「大學生活學習與輔導」為 0 學分，第一至四年共 8 學期。"
    elif hours:
        clause = "二年級兩學期「服務學習」各 0 學分；每學期至少 24 小時，其中至少 12 小時為公共服務。"
    else:
        clause = "二年級兩學期「服務學習」各 0 學分，須完成兩個不同學期。"
    scoped_source = _primary_zero_credit_source(
        cohort,
        program,
        series,
        source,
        original_clause=clause,
    )
    if scoped_source is None:
        return None

    required_count = int(contract.get("required_count") or 0)
    requirement_id = f"{program}.primary.{cohort}.{track_slug}.{series}"
    membership_id = f"{contract.get('membership_id_prefix', f'university_primary_{series}:')}{cohort}:{program}:{track_slug}"
    raw_course_ids = contract.get("official_course_ids", ())
    if isinstance(raw_course_ids, str):
        official_course_ids = (raw_course_ids,)
    elif isinstance(raw_course_ids, Sequence):
        official_course_ids = tuple(str(item) for item in raw_course_ids if str(item))
    else:
        official_course_ids = ()
    applies_to = {
        "curriculum_versions": (cohort,),
        "program_slugs": (program,),
        "track_slugs": (track_slug,),
        "roles": ("primary",),
    }
    predicate: dict[str, Any] = {
        "kind": str(contract.get("kind") or "OFFICIAL_LISTED_COURSE_COMPLETION"),
        "requirement_type": str(contract.get("requirement_type") or "non_credit_course_series"),
        "required_count": required_count,
        "required_completions": required_count,
        "distinct_term_required": bool(contract.get("distinct_term_required", True)),
        "max_completions_per_term": int(contract.get("max_completions_per_term") or 1),
        "require_zero_credits": bool(contract.get("require_zero_credits", True)),
        "completion_statuses": tuple(str(item) for item in contract.get("completion_statuses", ("COMPLETED",))),
        "membership_id": membership_id,
        "membership_only": True,
        "exact_titles": eligible_names,
        "official_course_ids": official_course_ids,
    }
    row: dict[str, Any] = {
        "id": requirement_id,
        "requirement_id": requirement_id,
        "name": name,
        "display_name": name,
        "raw_title": name,
        "credits": 0.0,
        "required_credits": 0.0,
        "bucket": series,
        "kind": str(contract.get("kind") or "OFFICIAL_LISTED_COURSE_COMPLETION"),
        "requirement_type": str(contract.get("requirement_type") or "non_credit_course_series"),
        "component": "non_credit",
        "component_type": "non_credit",
        "component_label": "非學分門檻",
        "lecture_or_lab": "non_credit",
        "is_zero_credit": True,
        "zero_credit_gate": True,
        "require_zero_credits": bool(contract.get("require_zero_credits", True)),
        "required": True,
        "required_count": required_count,
        "required_completions": required_count,
        "distinct_term_required": bool(contract.get("distinct_term_required", True)),
        "max_completions_per_term": int(contract.get("max_completions_per_term") or 1),
        "completion_statuses": tuple(str(item) for item in contract.get("completion_statuses", ("COMPLETED",))),
        "membership_id": membership_id,
        "official_membership_id": membership_id,
        "membership_ids": (membership_id,),
        "term_bound": "VERIFIED",
        "membership_program_required": False,
        "membership_track_required": False,
        "membership_version_required": False,
        "applicable_curriculum_version": cohort,
        "applicable_program_slug": program,
        "applicable_track_slug": track_slug,
        "applicability_state": "VERIFIED",
        "applicability_basis": "YEAR_SCOPED_PRIMARY_HANDBOOK_SECTION",
        "applies_to": applies_to,
        "scope_state": "VERIFIED",
        "evidence_state": scoped_source["evidence_state"],
        "coverage_state": scoped_source["coverage_state"],
        "verification_status": scoped_source["verification_status"],
        "automation_sufficiency": scoped_source["automation_sufficiency"],
        "automatic_decision": scoped_source["automatic_decision"],
        "affects_credit_ledger": bool(contract.get("affects_credit_ledger", False)),
        "excluded_from_free": True,
        "waiver_allowed": False,
        "waiver_evidence_required": True,
        "candidate_only": False,
        "pool_requirement": False,
        "pool_ids": (),
        "eligible_pool_ids": (),
        "overflow_routes": (),
        "eligible_course_names": eligible_names,
        "eligible_course_options": tuple({"name": item, "credits": 0.0} for item in eligible_names),
        "eligible_course_ids": official_course_ids,
        "course_ids": official_course_ids,
        "official_course_ids": official_course_ids,
        "match": {
            "official_membership_only": True,
            "exact_titles": eligible_names,
            "official_aliases": (),
        },
        "predicate": predicate,
        "policy_id": requirement_id,
        "policy_revision": f"{cohort}.1",
        "policy_source": deepcopy(scoped_source),
        "source_assertion_id": requirement_id,
        "assertion_id": requirement_id,
        "source_reference": f"{scoped_source['source_reference']}:row:{series}",
        "source_url": scoped_source["source_url"],
        "source_file": scoped_source["source_file"],
        "research_file": scoped_source["research_file"],
        "pdf_page": scoped_source["pdf_page"],
        "printed_page": scoped_source["printed_page"],
        "page": scoped_source["pdf_page"],
        "pages": scoped_source["pages"],
        "table_location": scoped_source["table_location"],
        "original_clause": scoped_source["original_clause"],
        "original_text": scoped_source["original_clause"],
    }
    if hours:
        for key in (
            "required_hours",
            "hours_per_completion",
            "minimum_public_service_hours_per_completion",
            "hours_evidence_semantics",
        ):
            if hours.get(key) not in (None, ""):
                row[key] = deepcopy(hours[key])
                predicate[key] = deepcopy(hours[key])
    row["source"] = {
        "source_type": scoped_source["source_type"],
        "file": scoped_source["source_file"],
        "source_file": scoped_source["source_file"],
        "url": scoped_source["source_url"],
        "source_url": scoped_source["source_url"],
        "pages": scoped_source["pages"],
        "pdf_page": scoped_source["pdf_page"],
        "printed_page": scoped_source["printed_page"],
        "source_reference": row["source_reference"],
        "original_clause": row["original_clause"],
        "curriculum_version": cohort,
        "program_slug": program,
        "track_slug": track_slug,
        "requirement_id": requirement_id,
    }
    provenance = {
        **deepcopy(scoped_source),
        "assertion_id": requirement_id,
        "requirement_id": requirement_id,
        "source_reference": row["source_reference"],
        "membership_id": membership_id,
        "official_membership_id": membership_id,
        "membership_ids": (membership_id,),
        "official_course_ids": official_course_ids,
        "eligible_course_ids": official_course_ids,
        "applicable_curriculum_version": cohort,
        "applicable_program_slug": program,
        "applicable_track_slug": track_slug,
        "scope_state": "VERIFIED",
        "require_zero_credits": row["require_zero_credits"],
        "required_count": required_count,
        "distinct_term_required": row["distinct_term_required"],
        "max_completions_per_term": row["max_completions_per_term"],
        "affects_credit_ledger": row["affects_credit_ledger"],
        "excluded_from_free": True,
        "predicate": deepcopy(predicate),
    }
    if hours:
        for key in (
            "required_hours",
            "hours_per_completion",
            "minimum_public_service_hours_per_completion",
            "hours_evidence_semantics",
        ):
            if key in row:
                provenance[key] = deepcopy(row[key])
    row["provenance"] = provenance
    if scoped_source.get("amendment_action"):
        row["amendment_action"] = scoped_source["amendment_action"]
        provenance["amendment_action"] = scoped_source["amendment_action"]
    return row


def _primary_university_requirement_rows(
    cohort: str,
    program: str,
    track: str | None,
    pools: dict[str, dict[str, Any]],
    source: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return shared university rows and the non-credit gates.

    ``course_catalog`` used to contain only department rows.  That made a
    COMPLETE Earth record look complete while its five language courses and
    general-education allocation were still invisible to the registry
    compiler.  Keep the exact language rows and policy-only category quotas
    in the same auditable row shape as department requirements.  Physical
    education remains the first non-credit row for compatibility; the
    source-backed life-guidance and service-learning series follow it and
    cannot consume graduation credits.
    """

    rows: list[dict[str, Any]] = []
    university_contract = _university_common_contract(cohort)
    compulsory_pool_id = _pool_id("primary", cohort, program, track, "university_compulsory")
    compulsory_courses = university_contract.get("compulsory_courses", {})
    for name, credits in compulsory_courses.items() if isinstance(compulsory_courses, Mapping) else ():
        row = _primary_course_row(
            cohort,
            program,
            track,
            str(name),
            credits,
            "university_compulsory",
            source,
            pool_ids=(compulsory_pool_id,),
            coverage_state=COMPLETE,
        )
        row["excluded_from_free"] = True
        row.setdefault("provenance", {})["excluded_from_free"] = True
        rows.append(row)

    flex_credits = float(university_contract.get("flex_credits") or 0)
    track_slug = track or "department"
    flex_requirement_id = (
        f"{program}.primary.{cohort}.{track_slug}.pool.ge_flex"
        if flex_credits > 0
        else ""
    )
    ge_source_rows: list[dict[str, Any]] = []
    ge_pool_ids: list[str] = []
    for pool_id, pool in pools.items():
        bucket = str(pool.get("bucket") or "")
        if not (bucket.startswith("ge_") or bucket == "ge_common_elective"):
            continue
        required_credits = pool.get("required_credits")
        if required_credits is None or float(required_credits) <= 0:
            continue
        ge_pool_ids.append(str(pool_id))
        ge_row = _primary_pool_requirement_row(
            cohort,
            program,
            track,
            pool,
            source,
            suffix=bucket,
            requirement_type="course_pool",
            choice_rule="official_category_policy",
            overflow_routes=(flex_requirement_id,) if flex_requirement_id else (),
        )
        ge_row["excluded_from_free"] = True
        ge_row.setdefault("provenance", {})["excluded_from_free"] = True
        if isinstance(ge_row.get("policy"), Mapping):
            ge_row["policy"]["excluded_from_free"] = True
        ge_source_rows.append(ge_row)

    rows.extend(ge_source_rows)
    if flex_requirement_id:
        # The flex requirement is a single exclusive credit consumer.  It
        # reuses the five official GE source pools directly; no sixth shadow
        # pool or extra 28-credit consumer is created.
        base_pool = pools[ge_pool_ids[-1]] if ge_pool_ids and ge_pool_ids[-1] in pools else None
        if base_pool is not None:
            flex_row = _primary_pool_requirement_row(
                cohort,
                program,
                track,
                base_pool,
                source,
                suffix="ge_flex",
                requirement_type="course_pool",
                choice_rule="exclusive_university_common_flex",
            )
            university_policy_source = _policy_source(
                cohort,
                "university_common",
                source,
                program=program,
                track=track,
            )
            flex_policy_id = f"university_common.ge_flex.{cohort}"
            flex_applies_to = {
                "curriculum_versions": (cohort,),
                "program_slugs": (program,),
                "track_slugs": (track_slug,),
                "roles": ("primary",),
            }
            flex_predicate = {
                "kind": "exclusive_university_common_flex",
                "minimum_credits": flex_credits,
                "source_pool_ids": tuple(ge_pool_ids),
                "eligible_pool_ids": tuple(ge_pool_ids),
                "overflow_only": True,
                "excluded_from_free": True,
            }
            flex_policy = {
                "policy_id": flex_policy_id,
                "revision": f"{cohort}.1",
                "applies_to": flex_applies_to,
                "predicate": flex_predicate,
                "evidence_state": university_policy_source.get("evidence_state", VERIFIED),
                "coverage_state": university_policy_source.get("coverage_state", COMPLETE),
                "automatic_decision": university_policy_source.get("automatic_decision", True),
                "scope_state": "VERIFIED",
                "source_reference": university_policy_source.get("source_reference", ""),
                "source_url": university_policy_source.get("source_url", ""),
                "source_file": university_policy_source.get("source_file", ""),
                "pdf_page": university_policy_source.get("pdf_page", "未標示"),
                "printed_page": university_policy_source.get("printed_page", "未標示"),
                "original_clause": university_policy_source.get("original_clause", ""),
                "policy_source": deepcopy(university_policy_source),
                "allowed_scope": "university_common",
                "exclusive": True,
                "excluded_from_free": True,
            }
            flex_row.update(
                {
                    "id": flex_requirement_id,
                    "requirement_id": flex_requirement_id,
                    "name": "通識彈性補足",
                    "display_name": "通識彈性補足",
                    "raw_title": "通識彈性補足",
                    "credits": flex_credits,
                    "required_credits": flex_credits,
                    "bucket": "ge_flex",
                    "kind": "quota",
                    "requirement_type": "course_pool",
                    "choice_rule": "exclusive_university_common_flex",
                    "eligible_course_names": (),
                    "eligible_course_options": (),
                    "pool_ids": (),
                    "eligible_pool_ids": tuple(ge_pool_ids),
                    "overflow_routes": (),
                    "pool_requirement": True,
                    "candidate_only": False,
                    "excluded_from_free": True,
                    "official_course_identity": f"{cohort}:{program}:{track_slug}:pool:ge_flex",
                    "policy_id": flex_policy_id,
                    "policy_revision": f"{cohort}.1",
                    "applies_to": flex_applies_to,
                    "predicate": flex_predicate,
                    "policy_source": deepcopy(university_policy_source),
                    "policy": flex_policy,
                    "named_course_pool_state": "COMPLETE",
                    "original_clause": f"111學年度通識彈性補足 {flex_credits:g} 學分；僅接收五個通識來源池的超額。",
                    "original_text": f"111學年度通識彈性補足 {flex_credits:g} 學分；僅接收五個通識來源池的超額。",
                }
            )
            flex_provenance = dict(flex_row.get("provenance") or {})
            flex_provenance.update(
                {
                    "assertion_id": flex_requirement_id,
                    "pool_ids": tuple(ge_pool_ids),
                    "eligible_pool_ids": tuple(ge_pool_ids),
                    "excluded_from_free": True,
                    "policy": deepcopy(flex_policy),
                    "policy_source": deepcopy(university_policy_source),
                    "original_clause": flex_row["original_clause"],
                }
            )
            flex_row["provenance"] = flex_provenance
            rows.append(flex_row)

    # Information application/design is a formal non-credit completion gate.
    # The public-course adapter supplies the term-specific membership; this
    # registry row supplies the student's applicability scope and never
    # invents offering-level program, track, or version metadata.
    it_policy = _RULES.get("shared", {}).get("information_technology", {})
    it_policy = it_policy if isinstance(it_policy, Mapping) else {}
    it_contracts = it_policy.get("evidence_by_cohort", {})
    it_contract = it_contracts.get(str(cohort), {}) if isinstance(it_contracts, Mapping) else {}
    it_contract = it_contract if isinstance(it_contract, Mapping) else {}
    it_source = _policy_source(
        cohort,
        "information_technology",
        source,
        program=program,
        track=track,
    )
    it_requirement_id = str(
        it_contract.get("requirement_id")
        or it_policy.get("requirement_id")
        or "university.it_application_design"
    )
    it_required = program != "cs"
    it_scope_state = "VERIFIED" if it_required else NOT_APPLICABLE
    it_policy_id = f"{it_requirement_id}.{cohort}"
    it_membership_program_required = bool(
        it_contract.get(
            "membership_program_required",
            it_policy.get("membership_program_required", False),
        )
    )
    it_membership_track_required = bool(
        it_contract.get(
            "membership_track_required",
            it_policy.get("membership_track_required", False),
        )
    )
    it_membership_version_required = bool(
        it_contract.get(
            "membership_version_required",
            it_policy.get("membership_version_required", False),
        )
    )
    it_row = _primary_course_row(
        cohort,
        program,
        track,
        str(it_contract.get("name") or it_policy.get("name") or "資訊應用與設計"),
        0,
        "information_technology",
        it_source,
        kind=str(it_contract.get("kind") or it_policy.get("kind") or "OFFICIAL_LISTED_COURSE_COMPLETION"),
        requirement_type="non_credit",
        eligible_names=(),
        eligible_options=(),
        coverage_state=str(it_contract.get("coverage_state") or it_policy.get("coverage_state") or COMPLETE),
        automatic_decision=bool(
            it_contract.get(
                "automatic_decision",
                it_policy.get("automatic_decision", True),
            )
        ),
    )
    it_row.update(
        {
            "id": it_requirement_id,
            "requirement_id": it_requirement_id,
            "name": str(it_contract.get("name") or it_policy.get("name") or "資訊應用與設計"),
            "display_name": str(it_contract.get("name") or it_policy.get("name") or "資訊應用與設計"),
            "raw_title": str(it_contract.get("name") or it_policy.get("name") or "資訊應用與設計"),
            "credits": 0.0,
            "required_credits": 0.0,
            "bucket": "information_technology",
            "kind": str(it_contract.get("kind") or it_policy.get("kind") or "OFFICIAL_LISTED_COURSE_COMPLETION"),
            "requirement_type": "non_credit",
            "component": "non_credit",
            "component_type": "non_credit",
            "component_label": "非學分門檻",
            "lecture_or_lab": "non_credit",
            "is_zero_credit": True,
            "zero_credit_gate": True,
            "eligible_course_names": (),
            "eligible_course_options": (),
            "pool_ids": (),
            "eligible_pool_ids": (),
            "overflow_routes": (),
            "pool_requirement": False,
            "candidate_only": False,
            "required": it_required,
            "required_count": int(it_contract.get("required_completions", it_policy.get("required_completions", 1))),
            "required_completions": int(it_contract.get("required_completions", it_policy.get("required_completions", 1))),
            "min_earned_credits_per_completion": float(
                it_contract.get(
                    "min_earned_credits_per_completion",
                    it_policy.get("min_earned_credits_per_completion", 2),
                )
            ),
            "membership_id": str(
                it_contract.get("membership_id")
                or it_policy.get("membership_id")
                or "university_it_direct_completion"
            ),
            "official_membership_id": str(
                it_contract.get("membership_id")
                or it_policy.get("membership_id")
                or "university_it_direct_completion"
            ),
            "membership_ids": (
                str(
                    it_contract.get("membership_id")
                    or it_policy.get("membership_id")
                    or "university_it_direct_completion"
                ),
            ),
            "completion_statuses": ("COMPLETED",),
            "term_bound": "VERIFIED",
            # These are requirement applicability fields.  The course-list
            # membership is term-bound, but a GE offering need not carry the
            # student's primary program, track, or curriculum version.
            "membership_program_required": it_membership_program_required,
            "membership_track_required": it_membership_track_required,
            "membership_version_required": it_membership_version_required,
            "program_slug": program,
            "track_slug": track_slug,
            "curriculum_version": cohort,
            "program": program,
            "track": _TRACK_DISPLAY.get(track) if track else None,
            "waiver_requirement_version": cohort,
            "waiver_program_slug": program,
            "waiver_track_slug": track_slug,
            "policy_id": it_policy_id,
            "policy_revision": f"{cohort}.1",
            "applies_to": {
                "curriculum_versions": (cohort,),
                "program_slugs": (program,),
                "track_slugs": (track_slug,),
                "roles": ("primary",),
            },
            "predicate": {
                "kind": "OFFICIAL_LISTED_COURSE_COMPLETION",
                "required_completions": int(
                    it_contract.get("required_completions", it_policy.get("required_completions", 1))
                ),
                "min_earned_credits_per_completion": float(
                    it_contract.get(
                        "min_earned_credits_per_completion",
                        it_policy.get("min_earned_credits_per_completion", 2),
                    )
                ),
                "membership_id": str(
                    it_contract.get("membership_id")
                    or it_policy.get("membership_id")
                    or "university_it_direct_completion"
                ),
                "membership_only": True,
            },
            "evidence_state": str(it_contract.get("evidence_state") or it_policy.get("evidence_state") or VERIFIED),
            "coverage_state": str(it_contract.get("coverage_state") or it_policy.get("coverage_state") or COMPLETE),
            "automatic_decision": bool(
                it_contract.get("automatic_decision", it_policy.get("automatic_decision", True))
            ),
            "scope_state": str(it_contract.get("scope_state") or "VERIFIED") if it_required else NOT_APPLICABLE,
            "applicability_state": it_scope_state,
            "applicability_basis": str(it_contract.get("applicability_state") or "YEAR_SCOPED_OFFICIAL_POLICY"),
            "applicability_source": deepcopy(it_source),
            "waiver_allowed": bool(it_contract.get("waiver_allowed", it_policy.get("waiver_allowed", True))),
            "waiver_evidence_required": True,
            "waiver_authority_ids": tuple(
                str(item)
                for item in (
                    it_contract.get(
                        "waiver_authority_ids",
                        it_policy.get("waiver_authority_ids", ()),
                    )
                    or ()
                )
                if str(item)
            ),
            "affects_credit_ledger": bool(
                it_contract.get("affects_credit_ledger", it_policy.get("affects_credit_ledger", False))
            ),
            "excluded_from_free": True,
            "match": {"official_membership_only": True, "exact_titles": (), "official_aliases": ()},
            "policy_source": deepcopy(it_source),
            "source_reference": f"{it_source['source_reference']}:row:information_technology",
            "source_url": it_source["source_url"],
            "source_file": it_source["source_file"],
            "research_file": it_source["research_file"],
            "pdf_page": it_source["pdf_page"],
            "printed_page": it_source["printed_page"],
            "pages": it_source["pages"],
            "table_location": it_source["table_location"],
            "original_clause": it_source["original_clause"] or it_policy.get("policy_clause", ""),
            "original_text": it_source["original_clause"] or it_policy.get("policy_clause", ""),
        }
    )
    it_row["source_assertion_id"] = it_requirement_id
    it_row["assertion_id"] = it_requirement_id
    it_row["official_course_identity"] = f"{cohort}:university:information_technology"
    it_row["provenance"] = {
        **dict(it_row.get("provenance") or {}),
        "assertion_id": it_requirement_id,
        "source_type": it_source.get("source_type", "official_general_education_policy"),
        "source_reference": it_row["source_reference"],
        "source_url": it_row["source_url"],
        "source_file": it_row["source_file"],
        "research_file": it_row["research_file"],
        "pdf_page": it_row["pdf_page"],
        "printed_page": it_row["printed_page"],
        "pages": it_row["pages"],
        "table_location": it_row["table_location"],
        "original_clause": it_row["original_clause"],
        "evidence_state": it_row["evidence_state"],
        "verification_status": it_row["evidence_state"],
        "coverage_state": it_row["coverage_state"],
        "automatic_decision": it_row["automatic_decision"],
        "scope_state": it_row["scope_state"],
        "curriculum_version": cohort,
        "program_slug": program,
        "track_slug": track_slug,
        "membership_id": it_row["membership_id"],
        "waiver_authority_ids": it_row["waiver_authority_ids"],
        "affects_credit_ledger": it_row["affects_credit_ledger"],
        "excluded_from_free": True,
        "policy_source": deepcopy(it_source),
    }
    zero_credit_rows: list[dict[str, Any]] = []
    for series in ("life_guidance", "service_learning"):
        # Explicit user-confirmed 113 Earth/Life correction, 2026-09-08.
        if cohort == "113" and program == "earth" and series == "service_learning":
            continue
        descriptor = _primary_zero_credit_descriptor(
            cohort,
            program,
            track,
            series,
            source,
        )
        if descriptor is not None:
            zero_credit_rows.append(descriptor)
    pe_pool_id = _pool_id("primary", cohort, program, track, "physical_education")
    pe_policy = _RULES.get("shared", {}).get("physical_education", {})
    pe_policy = pe_policy if isinstance(pe_policy, Mapping) else {}
    pe_contract = pe_policy.get("evidence_by_cohort", {}).get(cohort, {})
    pe_contract = pe_contract if isinstance(pe_contract, Mapping) else {}
    # Historical handbooks share the four-course/zero-credit rule, while the
    # later general-education manual adds the 8-hour, one-per-term, and
    # non-repeating activity constraints.  Read each year's contract instead
    # of applying the current shared defaults to every cohort.
    pe_semesters = int(pe_contract.get("semesters_required", pe_policy.get("semesters_required", 0)))
    pe_hours = float(pe_contract.get("required_hours", pe_policy.get("hours_required", 0)) or 0)
    pe_courses = int(pe_contract.get("courses_required", pe_policy.get("courses_required", 0)))
    pe_hours_per_completion = float(
        pe_contract.get(
            "hours_per_completion",
            pe_hours / pe_courses if pe_courses else 0,
        )
        or 0
    )
    raw_pe_max_per_term = pe_contract.get(
        "per_semester_limit",
        pe_policy.get("per_semester_limit"),
    )
    pe_max_per_term = (
        int(raw_pe_max_per_term)
        if raw_pe_max_per_term not in (None, "")
        else None
    )
    pe_distinct_term = bool(
        pe_contract.get("distinct_term_required", pe_policy.get("distinct_term_required", True))
    )
    pe_distinct_activity = bool(
        pe_contract.get(
            "distinct_activity_required",
            pe_policy.get("distinct_activity_required", False),
        )
    )
    pe_aliases = deepcopy(pe_policy.get("activity_aliases", {}))
    pe_membership_id = str(
        pe_contract.get("membership_id")
        or pe_policy.get("membership_id")
        or "university_physical_education_completion"
    )
    pe_activity_prefix = str(
        pe_contract.get("activity_membership_prefix")
        or pe_policy.get("activity_membership_prefix")
        or "university_physical_education_activity:"
    )
    pe_excluded_from_free = bool(
        pe_contract.get("excluded_from_free", pe_policy.get("excluded_from_free", True))
    )
    pe_policy_source = _policy_source(
        cohort,
        "physical_education",
        source,
        program=program,
        track=track,
    )
    pe_scope_state = str(pe_contract.get("applicability_state") or "UNSPECIFIED").upper()
    pe_scope_verified = pe_scope_state not in {
        "REQUIRES_HISTORICAL_CONFIRMATION",
        "NOT_STATED",
        "UNSPECIFIED",
    }
    pe_evidence_state = str(pe_contract.get("evidence_state") or VERIFIED)
    pe_coverage_state = str(pe_contract.get("coverage_state") or COMPLETE)
    pe_automatic = pe_scope_verified and pe_evidence_state == VERIFIED and pe_coverage_state == COMPLETE
    pe_policy_id = f"university_common.physical_education.{cohort}"
    pe_policy_descriptor = {
        "policy_id": pe_policy_id,
        "revision": f"{cohort}.1",
        "applies_to": {
            "curriculum_versions": (cohort,),
            "program_slugs": (program,),
            "track_slugs": (track or "department",),
            "roles": ("primary",),
        },
        "predicate": {
            "kind": "DISTINCT_TERM_ITEM_COUNT",
            "required_completions": pe_courses,
            "required_hours": pe_hours,
            "hours_per_completion": pe_hours_per_completion,
            "max_completions_per_term": pe_max_per_term,
            "distinct_term_required": pe_distinct_term,
            "distinct_activity_required": pe_distinct_activity,
            "title_base": "體育",
            "official_activity_aliases": pe_aliases,
            "membership_id": pe_membership_id,
            "activity_membership_prefix": pe_activity_prefix,
            "excluded_from_free": pe_excluded_from_free,
        },
        "evidence_state": pe_evidence_state,
        "coverage_state": pe_coverage_state,
        "automatic_decision": pe_automatic,
        "scope_state": "VERIFIED" if pe_scope_verified else "RULE_POLICY_SCOPE_UNVERIFIED",
        "source_reference": pe_policy_source["source_reference"],
        "source_url": pe_policy_source["source_url"],
        "source_file": pe_policy_source["source_file"],
        "pdf_page": pe_policy_source["pdf_page"],
        "printed_page": pe_policy_source["printed_page"],
        "original_clause": pe_policy_source["original_clause"]
        or pe_policy.get("policy_clause", ""),
        "policy_source": pe_policy_source,
        "official_activity_aliases": pe_aliases,
        "applicability_basis": pe_contract.get("applicability_state", "UNSPECIFIED"),
    }
    pe_pool = _pool_record(
        kind="primary",
        cohort=cohort,
        program=program,
        track=track,
        bucket="physical_education",
        label="體育學期門檻",
        source=source,
        required_credits=0,
        selection_rule="semester_count",
        policy={
            **pe_policy_descriptor,
            "allowed_scope": "university_common",
            "semesters_required": pe_semesters,
            "required_completions": pe_courses,
            "required_hours": pe_hours,
            "hours_per_completion": pe_hours_per_completion,
            "max_completions_per_term": pe_max_per_term,
            "distinct_term_required": pe_distinct_term,
            "distinct_activity_required": pe_distinct_activity,
            "official_activity_aliases": pe_aliases,
            "membership_id": pe_membership_id,
            "activity_membership_prefix": pe_activity_prefix,
            "excluded_from_free": pe_excluded_from_free,
            "zero_credit": True,
        },
    )
    pools[pe_pool_id] = pe_pool
    pe_row = _primary_course_row(
        cohort,
        program,
        track,
        "體育",
        0,
        "physical_education",
        source,
        kind="DISTINCT_TERM_ITEM_COUNT",
        requirement_type="non_credit",
        coverage_state=pe_coverage_state,
        automatic_decision=pe_automatic,
    )
    pe_row.update(
        {
            "pool_ids": (pe_pool_id,),
            "eligible_pool_ids": (),
            "candidate_only": False,
            "pool_requirement": False,
            "is_zero_credit": True,
            "zero_credit_gate": True,
            "semesters_required": pe_semesters,
            "required_count": pe_courses,
            "required_completions": pe_courses,
            "required_hours": pe_hours,
            "hours_per_completion": pe_hours_per_completion,
            "max_completions_per_term": pe_max_per_term,
            "distinct_term_required": pe_distinct_term,
            "distinct_activity_required": pe_distinct_activity,
            "policy_id": pe_policy_id,
            "policy_revision": f"{cohort}.1",
            "applies_to": pe_policy_descriptor["applies_to"],
            "predicate": pe_policy_descriptor["predicate"],
            "policy_source": deepcopy(pe_policy_source),
            "title_base": "體育",
            "official_activity_aliases": pe_aliases,
            "membership_id": pe_membership_id,
            "official_membership_id": pe_membership_id,
            "activity_membership_prefix": pe_activity_prefix,
            "excluded_from_free": pe_excluded_from_free,
            "match": {
                "exact_titles": ("體育",),
                "official_aliases": tuple(
                    alias
                    for item in pe_aliases.values()
                    if isinstance(item, Mapping)
                    for alias in item.get("aliases", ())
                ),
            },
            "completion_statuses": ("COMPLETED",),
            "waiver_allowed": True,
            "waiver_evidence_required": True,
            "affects_credit_ledger": False,
            "applicability_basis": pe_contract.get("applicability_state", "UNSPECIFIED"),
            "applicability_source": deepcopy(pe_policy_source),
            "scope_state": "VERIFIED" if pe_scope_verified else "RULE_POLICY_SCOPE_UNVERIFIED",
            "component": "non_credit",
            "component_type": "non_credit",
            "lecture_or_lab": "non_credit",
            "source_reference": f"{pe_policy_source['source_reference']}:row:physical_education",
            "source_url": pe_policy_source["source_url"],
            "source_file": pe_policy_source["source_file"],
            "research_file": pe_policy_source["research_file"],
            "pdf_page": pe_policy_source["pdf_page"],
            "printed_page": pe_policy_source["printed_page"],
            "pages": pe_policy_source["pages"],
            "table_location": pe_policy_source["table_location"],
            "original_clause": pe_policy_source["original_clause"]
            or pe_policy.get("policy_clause", ""),
            "original_text": pe_policy_source["original_clause"]
            or pe_policy.get("policy_clause", ""),
        }
    )
    pe_row["page"] = pe_row["pdf_page"]
    pe_row["source"] = {
        "source_type": pe_policy_source.get("source_type", "official_policy"),
        "file": pe_row["source_file"],
        "source_file": pe_row["source_file"],
        "url": pe_row["source_url"],
        "source_url": pe_row["source_url"],
        "pages": pe_row["pages"],
        "pdf_page": pe_row["pdf_page"],
        "printed_page": pe_row["printed_page"],
        "source_reference": pe_row["source_reference"],
        "original_clause": pe_row["original_clause"],
        "curriculum_version": cohort,
        "program_slug": program,
        "track_slug": track or "department",
        "policy_id": pe_policy_id,
    }
    pe_row["provenance"] = {
        **dict(pe_row.get("provenance") or {}),
        "source_type": pe_policy_source.get("source_type", "official_policy"),
        "research_file": pe_row["research_file"],
        "source_url": pe_row["source_url"],
        "source_file": pe_row["source_file"],
        "pdf_page": pe_row["pdf_page"],
        "printed_page": pe_row["printed_page"],
        "pages": pe_row["pages"],
        "table_location": pe_row["table_location"],
        "source_reference": pe_row["source_reference"],
        "original_clause": pe_row["original_clause"],
        "evidence_state": pe_evidence_state,
        "verification_status": pe_evidence_state,
        "coverage_state": pe_coverage_state,
        "automation_sufficiency": "COMPLETE" if pe_automatic else "PARTIAL",
        "automatic_decision": pe_automatic,
        "zero_credit_gate": True,
        "semesters_required": pe_semesters,
        "required_count": pe_courses,
        "required_completions": pe_courses,
        "required_hours": pe_hours,
        "hours_per_completion": pe_hours_per_completion,
        "max_completions_per_term": pe_max_per_term,
        "distinct_term_required": pe_distinct_term,
        "distinct_activity_required": pe_distinct_activity,
        "official_activity_aliases": pe_aliases,
        "membership_id": pe_membership_id,
        "official_membership_id": pe_membership_id,
        "activity_membership_prefix": pe_activity_prefix,
        "excluded_from_free": pe_excluded_from_free,
        "policy_id": pe_policy_id,
        "policy_revision": f"{cohort}.1",
        "applies_to": pe_policy_descriptor["applies_to"],
        "predicate": pe_policy_descriptor["predicate"],
        "policy_source": deepcopy(pe_policy_source),
        "scope_state": pe_policy_descriptor["scope_state"],
    }
    return rows, [pe_row, it_row, *zero_credit_rows]


def _course_catalog(program: str, cohort: str, kind: str, track: str | None, coverage: str) -> list[dict[str, Any]]:
    """Build source-scoped catalogs without silently borrowing another year."""

    if coverage == COVERAGE_NONE:
        return []
    if kind == "double_major_target" and program == "apc" and track in {"physics", "chemistry"}:
        return _apc_target_catalog(cohort, track)
    if kind == "double_major_target" and program == "cs":
        return _cs_target_quota_catalog(cohort)
    if kind == "double_major_target" and program == "earth":
        return _earth_secondary_catalog_rows(cohort, "double_major", track)
    if kind == "double_major_target" and program == "math":
        return _math_secondary_catalog_rows(cohort, "double_major", track)
    if kind == "double_major_target":
        return []
    handbook = _RULES.get("handbooks", {}).get(cohort, {})
    if not isinstance(handbook, dict):
        return []
    source = _primary_evidence(cohort, program)
    pools, pool_requirements = _primary_pool_bundle(cohort, program, track, source)
    university_rows, _ = _primary_university_requirement_rows(
        cohort,
        program,
        track,
        pools,
        source,
    )

    def pool_for(bucket: str) -> tuple[str, ...]:
        pool_id = _pool_id("primary", cohort, program, track, bucket)
        return (pool_id,) if pool_id in pools else ()

    # Shared university requirements are explicit registry rows.  Their
    # category rows are quotas with policy-only pools; their candidate list is
    # intentionally empty because the handbook gives category rules rather
    # than a fixed title catalogue.
    rows: list[dict[str, Any]] = list(university_rows)
    if program == "earth":
        major = handbook.get("earth_life_major", {})
        common = major.get("common_compulsory", {}) if isinstance(major, dict) else {}
        common_courses = common.get("courses", {}) if isinstance(common, dict) else {}
        for name, credits in common_courses.items() if isinstance(common_courses, dict) else ():
            # Life guidance and service learning are emitted as dedicated
            # aggregate gates below.  Keeping their historical 0-credit
            # department rows here would duplicate the same gate and expose
            # them as ordinary course requirements to credit allocation.
            if float(credits or 0) == 0:
                continue
            rows.append(
                _primary_course_row(
                    cohort,
                    program,
                    track,
                    name,
                    credits,
                    "common_compulsory",
                    source,
                    pool_ids=pool_for("common_compulsory"),
                    coverage_state=COMPLETE,
                )
            )
        domains = major.get("domains", {}) if isinstance(major, dict) else {}
        domain_label = "地球環境" if track == "earth_environment" else "生命科學"
        domain_data = domains.get(domain_label, {}) if isinstance(domains, dict) else {}
        domain_courses = domain_data.get("compulsory", {}) if isinstance(domain_data, dict) else {}
        for name, credits in domain_courses.items() if isinstance(domain_courses, dict) else ():
            rows.append(
                _primary_course_row(
                    cohort,
                    program,
                    track,
                    name,
                    credits,
                    f"{domain_label}:compulsory",
                    source,
                    pool_ids=pool_for("domain_required"),
                    coverage_state=COMPLETE,
                )
            )
        # Alternatives are one requirement with a choice pool.  Expanding
        # each title into its own row would incorrectly require both options.
        rows.extend(
            item
            for item in pool_requirements
            if item.get("requirement_type") == "choice"
        )
    elif program == "apc":
        apc = handbook.get("apc_rules", {})
        apc = apc if isinstance(apc, Mapping) else {}
        common_catalog = apc.get("common_catalog")
        if not isinstance(common_catalog, Mapping):
            common_catalog = {
                **(
                    apc.get("basic_core", {})
                    if isinstance(apc.get("basic_core", {}), Mapping)
                    else {}
                ),
                **(
                    apc.get("shared_other_required", {})
                    if isinstance(apc.get("shared_other_required", {}), Mapping)
                    else {}
                ),
            }
        common_by_track = apc.get("common_required_by_track", {})
        common_by_track = common_by_track if isinstance(common_by_track, Mapping) else {}
        division_label = "物理組" if track == "physics" else "化學組"
        required_common = common_by_track.get(division_label)
        if not isinstance(required_common, Mapping):
            required_common = common_catalog
        common_source = _primary_section_source(
            source,
            apc.get("common_catalog_evidence"),
        )
        seen: set[str] = set()
        for name, credits in required_common.items():
            seen.add(name)
            rows.append(
                _primary_course_row(
                    cohort,
                    program,
                    track,
                    name,
                    credits,
                    "apc_common",
                    common_source,
                    pool_ids=pool_for("apc_common_compulsory"),
                    coverage_state=COMPLETE,
                )
            )
        division = apc.get("divisions", {}).get(
            "物理組" if track == "physics" else "化學組" if track == "chemistry" else "",
            {},
        )
        required_evidence_by_track = apc.get("required_evidence_by_track", {})
        required_evidence_by_track = (
            required_evidence_by_track
            if isinstance(required_evidence_by_track, Mapping)
            else {}
        )
        track_source = _primary_section_source(
            source,
            required_evidence_by_track.get(division_label),
        )
        for name, credits in division.get("compulsory", {}).items() if isinstance(division, dict) else ():
            if name not in seen:
                rows.append(
                    _primary_course_row(
                        cohort,
                        program,
                        track,
                        name,
                        credits,
                        "apc_track_compulsory",
                        track_source,
                        pool_ids=pool_for("apc_track_compulsory"),
                        coverage_state=COMPLETE,
                    )
                )
    elif program == "cs":
        primary_catalog = _cs_primary_catalog(cohort)
        configured_required = primary_catalog.get("named_core") if primary_catalog else None
        required = (
            dict(configured_required)
            if isinstance(configured_required, Mapping)
            else {
                "計算機概論": 3,
                "Java程式設計": 3,
                "離散數學": 3,
                "C程式設計": 3,
                "資料結構": 3,
                "數位電子學": 3,
                "線性代數": 3,
                "演算法": 3,
                "數位系統設計": 3,
                "作業系統": 3,
                "資訊專題(I)": 1,
            }
        )
        required_source = (
            _cs_catalog_source(source, primary_catalog, "required")
            if primary_catalog
            else source
        )
        for name, credits in required.items():
            rows.append(
                _primary_course_row(
                    cohort,
                    program,
                    track,
                    name,
                    credits,
                    "cs_required",
                    required_source,
                    pool_ids=pool_for("cs_department_required"),
                    coverage_state=COMPLETE,
                )
            )
        alpha_pool_ids = pool_for("cs_elective_alpha")
        alpha_source = (
            _cs_catalog_source(source, primary_catalog, "alpha")
            if primary_catalog
            else source
        )
        alpha_rows = _cs_catalog_rows(primary_catalog, "alpha")
        alpha_membership_id = f"cs_elective_alpha:{cohort}"
        for item in alpha_rows:
            name = str(item["course_name"])
            credits = item["credits"]
            row_source = _cs_course_source(alpha_source, item)
            row = _primary_course_row(
                cohort,
                program,
                track,
                name,
                credits,
                "cs_alpha_required",
                row_source,
                pool_ids=alpha_pool_ids,
                coverage_state=COMPLETE,
            )
            row.update(
                {
                    "membership_id": alpha_membership_id,
                    "membership_ids": (alpha_membership_id,),
                    "subset_ids": (alpha_membership_id,),
                    "excluded_from_free": True,
                    "rollup_group_id": "cs_primary_elective_54",
                    "rollup_required_credits": 54,
                }
            )
            row.setdefault("provenance", {}).update(
                {
                    "membership_id": alpha_membership_id,
                    "membership_ids": (alpha_membership_id,),
                    "excluded_from_free": True,
                    "rollup_group_id": "cs_primary_elective_54",
                    "rollup_required_credits": 54,
                }
            )
            rows.append(row)
    elif program == "math":
        math_rules = handbook.get("math_rules", {})
        primary_catalog = _math_primary_catalog(cohort)
        common_math = _math_course_map(primary_catalog.get("common_required"))
        if not common_math:
            common_math = _math_course_map(math_rules.get("common_compulsory"))
        common_source = _math_catalog_source(source, primary_catalog, "common") if primary_catalog else source
        common_aliases = primary_catalog.get("common_aliases", {}) if isinstance(primary_catalog, Mapping) else {}
        for name, credits in common_math.items():
            aliases = common_aliases.get(name, ()) if isinstance(common_aliases, Mapping) else ()
            eligible_names = (name, *tuple(str(item) for item in aliases if item))
            rows.append(
                _primary_course_row(
                    cohort,
                    program,
                    track,
                    name,
                    credits,
                    "math_common_compulsory",
                    common_source,
                    eligible_names=eligible_names,
                    pool_ids=pool_for("math_common_compulsory"),
                    coverage_state=COMPLETE,
                )
            )
        selected_domain = track if track in set(_MATH_PRIMARY_TRACKS.get(cohort, ())) else None
        if selected_domain and primary_catalog:
            domains = primary_catalog.get("domains", {})
            selected_spec = domains.get(selected_domain, {}) if isinstance(domains, Mapping) else {}
            selected_required = _math_course_map(
                selected_spec.get("required") if isinstance(selected_spec, Mapping) else {}
            )
            aliases = selected_spec.get("aliases", {}) if isinstance(selected_spec, Mapping) else {}
            required_source = _math_catalog_source(source, primary_catalog, selected_domain)
            for name, credits in selected_required.items():
                variants = aliases.get(name, ()) if isinstance(aliases, Mapping) else ()
                eligible_names = (name, *tuple(str(item) for item in variants if item))
                rows.append(
                    _primary_course_row(
                        cohort,
                        program,
                        track,
                        name,
                        credits,
                        f"{selected_domain}:required",
                        required_source,
                        eligible_names=eligible_names,
                        pool_ids=pool_for("math_domain_required"),
                        coverage_state=COMPLETE,
                    )
                )
    # Add explicit quota/choice rows after named requirements.  Every row has
    # a scoped eligible_pool_ids tuple; the candidate catalogue stays inside
    # the pool policy and is never compiled as a list of mandatory courses.
    rows.extend(
        item
        for item in pool_requirements
        if item.get("requirement_type") != "choice" or program != "earth"
    )
    return rows


def _minor_catalog(cohort: str, program: str, track: str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the exact minor rows transcribed in the research matrix.

    The matrix intentionally does not provide official course codes.  Rows
    therefore expose names/components as candidate evidence only; the
    service can formalize a route only after a confirmed transcript identity
    or an explicit equivalency record is supplied.
    """

    rows: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "manual_review_reasons": [],
        "conflicted_course_names": (),
        "zero_credit_gate": False,
    }
    if program == "apc":
        rows = _apc_secondary_catalog_rows(cohort, track or "physics", "minor")
        metadata.update(
            {
                "secondary_catalog_complete": True,
                "fixed_base_credits": 20 if cohort == "115" else 16,
                "remaining_required_credits": 0 if cohort == "115" else 4,
                "track_experiment": track,
            }
        )
        return rows, metadata
    if program == "earth":
        rows = _earth_secondary_catalog_rows(cohort, "minor")
        metadata.update(
            {
                "secondary_catalog_complete": True,
                "credit_total": 24,
                "zero_credit_gate": False,
            }
        )
        return rows, metadata
    if program == "cs":
        rows = _cs_secondary_catalog_rows(cohort, "minor")
        metadata.update(
            {
                "secondary_catalog_complete": True,
                "mandatory_credits": 6,
                "other_course_credits": 14,
            }
        )
        return rows, metadata
    if program == "math":
        contract = _secondary_catalog(cohort, "math", "minor")
        rows = _math_secondary_catalog_rows(cohort, "minor")
        metadata.update(
            {
                "secondary_catalog_complete": True,
                "calculus_credits": 8,
                "elective_credits": float(contract.get("elective_minimum", 12) or 12),
                "elective_maximum_credits": float(contract.get("elective_maximum", 20) or 20),
                "math_secondary_composite_required": True,
                "software_choice": deepcopy(contract.get("software_choice", {})),
            }
        )
        if cohort == "113":
            # Keep the handbook amendment as provenance while the operative
            # candidate row uses the current three-credit value.  This is
            # metadata for audit/review only; it is not a second requirement.
            metadata["credit_revision_history"] = {
                "數學導論": {
                    "current_credits": 3,
                    "previous_credits": 4,
                    "revision_state": "CURRENT_VALUE_VERIFIED",
                    "source_reference": "handbook:113:math:minor:p.74",
                }
            }
        return rows, metadata
    if program == "apc":
        base_rows = (
            ("普通物理學(一)", 3, "lecture", "ordinary_physics_1"),
            ("普通物理實驗(一)", 1, "lab", "ordinary_physics_lab_1"),
            ("普通化學(一)", 3, "lecture", "ordinary_chemistry_1"),
            ("普通化學實驗(一)", 1, "lab", "ordinary_chemistry_lab_1"),
            ("普通物理學(二)", 3, "lecture", "ordinary_physics_2"),
            ("普通物理實驗(二)", 1, "lab", "ordinary_physics_lab_2"),
            ("普通化學(二)", 3, "lecture", "ordinary_chemistry_2"),
            ("普通化學實驗(二)", 1, "lab", "ordinary_chemistry_lab_2"),
        )
        if cohort in {"111", "112", "113", "114"}:
            # Both tracks share the eight named rows in the source page.  The
            # additional four credits are deliberately an unnamed quota.
            for name, credits, component, slug in base_rows:
                rows.append(
                    _minor_row(
                        cohort,
                        program,
                        track,
                        slug,
                        name,
                        credits,
                        component=component,
                        # The named eight rows are transcribed, but the
                        # separate unnamed four-credit quota keeps the
                        # overall 111–114 decision manual.
                        coverage_state=COMPLETE,
                        original_clause=_table_cell_clause(name, credits),
                    )
                )
            rows.append(
                _minor_row(
                    cohort,
                    program,
                    track,
                    "unnamed_required_4",
                    "未具名必修課程（需目標系書面確認）",
                    4,
                    requirement_type="credit_quota",
                    eligible_names=(),
                    coverage_state=PARTIAL,
                    evidence_state=VERIFIED,
                    original_clause="其餘／必修課程應修畢 4 學分（課名未具名）",
                    manual_reason="官方輔系頁只列 4 學分額度，沒有可執行的課名與選擇規則。",
                )
            )
            metadata["manual_review_reasons"].append("APC 111–114 額外 4 學分未具名，整體不得自動完成。")
            return rows, metadata

        # The 115 page is track-specific: the experiment component is not
        # interchangeable with the other component.
        lab_name = "普通物理實驗" if track == "physics" else "普通化學實驗"
        lab_component = "physics" if track == "physics" else "chemistry"
        rows = [
            _minor_row(cohort, program, track, "ordinary_physics_1", "普通物理學(一)", 3, original_clause=_table_cell_clause("普通物理學(一)", 3)),
            _minor_row(cohort, program, track, "ordinary_chemistry_1", "普通化學(一)", 3, original_clause=_table_cell_clause("普通化學(一)", 3)),
            _minor_row(cohort, program, track, "experiment_1", f"{lab_name}(一)", 1, component="lab", original_clause=_table_cell_clause(f"{lab_name}(一)", 1)),
            _minor_row(cohort, program, track, "calculus_1", "微積分(一)", 3, original_clause=_table_cell_clause("微積分(一)", 3)),
            _minor_row(cohort, program, track, "ordinary_physics_2", "普通物理學(二)", 3, original_clause=_table_cell_clause("普通物理學(二)", 3)),
            _minor_row(cohort, program, track, "ordinary_chemistry_2", "普通化學(二)", 3, original_clause=_table_cell_clause("普通化學(二)", 3)),
            _minor_row(cohort, program, track, "experiment_2", f"{lab_name}(二)", 1, component="lab", original_clause=_table_cell_clause(f"{lab_name}(二)", 1)),
            _minor_row(cohort, program, track, "calculus_2", "微積分(二)", 3, original_clause=_table_cell_clause("微積分(二)", 3)),
        ]
        metadata["track_experiment"] = lab_component
        return rows, metadata

    if program == "earth":
        common = (
            ("普通生物學(一)", 3, "biology_1"),
            ("普通生物學(二)", 3, "biology_2"),
            ("普通生物學實驗", 1, "biology_lab"),
            ("地球科學實驗", 1, "earth_science_lab"),
            ("地球科學(一)", 3, "earth_science_1"),
            ("地球科學(二)", 3, "earth_science_2"),
            ("資料處理與分析", 3, "data_processing"),
            ("基礎生態學", 3, "basic_ecology"),
            ("環境影響評估", 2, "environmental_impact"),
        )
        for name, credits, slug in common:
            rows.append(
                _minor_row(
                    cohort,
                    program,
                    None,
                    slug,
                    name,
                    credits,
                    original_clause=_table_cell_clause(name, credits),
                    # The named common rows are individually transcribed and
                    # verified.  The curriculum can still be PARTIAL because
                    # the zero-credit applicability gate and year-specific
                    # choice quota need a separate manual decision.
                    coverage_state=COMPLETE,
                )
            )
        if cohort in {"111", "112"}:
            rows.extend(
                (
                    _minor_row(
                        cohort,
                        program,
                        None,
                        "project_1_choice",
                        "專題研究／專業實習(一)",
                        1,
                        eligible_names=("專題研究(一)", "專業實習(一)"),
                        requirement_type="choice",
                        choice_group="earth_project_1",
                        choice_rule="二選一",
                        original_clause="專題研究(一)／專業實習(一) 二選一，1 學分",
                        coverage_state=COMPLETE,
                    ),
                    _minor_row(
                        cohort,
                        program,
                        None,
                        "project_2_choice",
                        "專題研究／專業實習(二)",
                        1,
                        eligible_names=("專題研究(二)", "專業實習(二)"),
                        requirement_type="choice",
                        choice_group="earth_project_2",
                        choice_rule="二選一",
                        original_clause="專題研究(二)／專業實習(二) 二選一，1 學分",
                        coverage_state=COMPLETE,
                    ),
                )
            )
        elif cohort == "113":
            rows.append(
                _minor_row(
                    cohort,
                    program,
                    None,
                    "project_choice",
                    "專題研究／專業實習",
                    2,
                    eligible_names=("專題研究", "專業實習"),
                    requirement_type="choice",
                    choice_group="earth_project",
                    choice_rule="二選一",
                    original_clause="專題研究／專業實習 二選一，2 學分",
                    coverage_state=COMPLETE,
                )
            )
        else:
            rows.append(
                _minor_row(
                    cohort,
                    program,
                    None,
                    "seminar",
                    "書報討論",
                    2,
                    original_clause="書報討論 2 學分",
                    coverage_state=COMPLETE,
                )
            )
        zero_names = ("大學生活學習與輔導",) if cohort == "115" else ("大學生活學習與輔導", "服務學習學年課")
        for index, name in enumerate(zero_names, start=1):
            rows.append(
                _minor_row(
                    cohort,
                    program,
                    None,
                    f"zero_credit_{index}",
                    name,
                    0,
                    zero_credit=True,
                    original_clause=f"{name} 0 學分",
                    coverage_state=PARTIAL,
                    manual_reason="0 學分課程是否屬輔系非學分完成 gate，官方輔系頁未說明。",
                )
            )
        metadata["zero_credit_gate"] = True
        metadata["manual_review_reasons"].append("共同必修中的 0 學分項目是否適用輔系，需人工確認。")
        return rows, metadata

    if program == "cs":
        rows = [
            _minor_row(cohort, program, None, "intro", "計算機概論", 3, original_clause=_table_cell_clause("計算機概論", 3), coverage_state=COMPLETE),
        ]
        if cohort == "115":
            # The 115 secondary page verifies the aggregate 6+14 structure,
            # but the locally checked image does not safely identify the
            # second named 3-credit course.  Keep the missing claim explicit;
            # do not copy the older C-programming title into this cohort.
            rows.append(
                _minor_row(
                    cohort,
                    program,
                    None,
                    "missing_named_core",
                    "資科系輔系第二項命名必修（官方列項未完整辨識）",
                    3,
                    requirement_type="missing_named_course",
                    coverage_state=PARTIAL,
                    evidence_state=MISSING,
                    original_clause="輔系必修 aggregate 6 學分；第二個命名列項未能辨識",
                    manual_reason="不得以 C 程式設計或其他年度名稱補猜 115 輔系第二項必修，需回看官方頁面或系所核准。",
                )
            )
            metadata["manual_review_reasons"].append("115 資科輔系第二項命名必修未能由官方頁面影像安全辨識。")
        else:
            rows.append(
                _minor_row(cohort, program, None, "c_programming", "C 程式設計", 3, original_clause=_table_cell_clause("C 程式設計", 3), coverage_state=COMPLETE)
            )
        rows.append(
            _minor_row(
                cohort,
                program,
                None,
                "other_cs_offerings_14",
                "本系其他開設課程（需開課單位證據）",
                14,
                requirement_type="credit_quota",
                eligible_names=(),
                coverage_state=PARTIAL,
                original_clause="本系其他開設課程至少 14 學分",
                manual_reason="其他 14 學分須有資訊科學系開課單位及個案核准證據，不能只靠課名或全域 mapping。",
            ),
        )
        metadata["manual_review_reasons"].append("資科其他 14 學分需要開課單位與必要替代核准證據。")
        return rows, metadata

    # Math / Data Science and Mathematics.  Keep each year independent.
    # The research matrix gives a complete, independently scoped named list
    # for every 111–115 minor table.  Keep each year exact: never manufacture
    # a course by taking a union with a neighbouring handbook version.
    math_elective_options_by_year: dict[str, tuple[tuple[str, int], ...]] = {
        "111": (
            ("線性代數(一)", 3), ("線性代數(二)", 3), ("基礎數學", 3),
            ("數學軟體應用與實作(A)", 3), ("計算機概論", 3), ("基礎統計學", 3), ("數論", 3),
            ("C語言程式設計", 3), ("數學軟體應用與實作(B)", 3), ("數學軟體應用與實作(C)", 3),
            ("統計套裝軟體之應用", 3), ("數學導論", 4), ("數學教育概論", 3),
            ("數學遊戲教學設計與實務", 3), ("高等微積分(一)", 4), ("高等微積分(二)", 4),
            ("代數學(一)", 3), ("代數學(二)", 3), ("機率論", 3), ("統計學", 3), ("幾何學", 3),
            ("微分方程(一)", 3), ("高等線性代數", 3), ("微分方程(二)", 3), ("數值分析(一)", 3),
            ("離散數學", 3), ("數學課程研究", 3), ("兒童數學概念發展", 3), ("資訊科技融入數學教學", 3),
            ("視覺化資料分析", 3), ("資料探勘", 3), ("統計程式語言", 3), ("財務數學", 3),
            ("迴歸分析", 3), ("時間序列", 3), ("應用統計方法(一)", 3), ("數理統計(一)", 3),
            ("數理統計(二)", 3), ("實變數函數論", 3), ("複變數函數論", 3), ("拓樸學", 3),
            ("數學教學與評量", 3), ("數學教育專題", 3),
        ),
        "112": (
            ("線性代數(一)", 3), ("線性代數(二)", 3), ("基礎數學", 3),
            ("數學軟體應用與實作(A)", 3), ("計算機概論", 3), ("基礎統計學", 3), ("數論", 3),
            ("C語言程式設計", 3), ("數學軟體應用與實作(B)", 3), ("數學軟體應用與實作(C)", 3),
            ("統計套裝軟體之應用", 3), ("數學導論", 4), ("數學教育概論", 3),
            ("數學遊戲教學設計與實務", 3), ("高等微積分(一)", 4), ("高等微積分(二)", 4),
            ("代數學(一)", 3), ("代數學(二)", 3), ("機率論", 3), ("統計學", 3), ("幾何學", 3),
            ("微分方程(一)", 3), ("高等線性代數", 3), ("微分方程(二)", 3), ("數值分析(一)", 3),
            ("離散數學", 3), ("數學課程研究", 3), ("兒童數學概念發展", 3), ("資訊科技融入數學教學", 3),
            ("視覺化資料分析", 3), ("資料探勘", 3), ("統計程式語言", 3), ("財務數學", 3),
            ("迴歸分析", 3), ("時間序列", 3), ("應用統計方法(一)", 3), ("數理統計(一)", 3),
            ("數理統計(二)", 3), ("實變數函數論", 3), ("複變數函數論", 3), ("拓樸學", 3),
            ("數學教學與評量", 3), ("數學教育專題", 3),
        ),
        "113": (
            ("線性代數(一)", 3), ("線性代數(二)", 3), ("集合與邏輯", 3), ("資訊科學與科學計算", 3),
            ("統計與生活", 3), ("數論", 3), ("C語言程式設計", 3), ("Matlab程式設計", 3),
            ("Python程式設計", 3), ("統計套裝軟體之應用", 3), ("數學導論", 3), ("數學教育概論", 3),
            ("數學遊戲教學設計與實務", 3), ("高等微積分(一)", 4), ("高等微積分(二)", 4),
            ("代數學(一)", 3), ("代數學(二)", 3), ("機率論", 3), ("統計學(一)", 3),
            ("統計學", 3), ("統計學(二)", 3), ("幾何學", 3), ("微分方程(一)", 3),
            ("高等線性代數", 3), ("微分方程(二)", 3), ("數值分析(一)", 3), ("離散數學", 3),
            ("數學課程研究", 3), ("兒童數學概念發展", 3), ("數學概念發展", 3), ("資訊科技融入數學教學", 3),
            ("視覺化資料分析", 3), ("資料探勘", 3), ("統計程式語言", 3), ("程式設計與資料庫", 3),
            ("財務數學", 3), ("迴歸分析", 3), ("時間序列", 3), ("應用統計方法(一)", 3),
            ("數理統計(一)", 3), ("數理統計(二)", 3), ("貝氏統計", 3), ("實變數函數論", 3),
            ("複變數函數論", 3), ("拓樸學", 3), ("數學教學與評量", 3), ("數學教學與評量研究", 3),
            ("數學教育專題", 3),
        ),
        "114": (
            ("線性代數(一)", 3), ("線性代數(二)", 3), ("集合與邏輯", 3), ("資訊科學與科學計算", 3),
            ("統計與生活", 3), ("數論", 3), ("Python程式設計", 3), ("Matlab程式設計", 3),
            ("統計套裝軟體之應用", 3), ("數學導論", 3), ("數學教育概論", 3),
            ("數學遊戲教學設計與實務", 3), ("高等微積分(一)", 4), ("高等微積分(二)", 4),
            ("代數學(一)", 3), ("代數學(二)", 3), ("統計學(一)", 3), ("統計學(二)", 3),
            ("微分方程", 3), ("高等線性代數", 3), ("數值分析", 3), ("離散數學", 3),
            ("數學課程研究", 3), ("數學概念發展", 3), ("資訊科技融入數學教學", 3),
            ("視覺化資料分析", 3), ("資料探勘", 3), ("統計程式語言", 3), ("財務數學", 3),
            ("迴歸分析", 3), ("時間序列", 3), ("應用統計", 3), ("貝氏數據分析導論", 3),
            ("實變數函數論", 3), ("複變數函數論", 3), ("數學教學與評量研究", 3), ("數學教育專題研究", 3),
        ),
        "115": (
            ("線性代數(一)", 3), ("線性代數(二)", 3), ("集合與邏輯", 3), ("統計與生活", 3),
            ("資訊科學與科學計算", 3), ("數論", 3), ("Python程式設計", 3), ("Matlab程式設計", 3),
            ("統計套裝軟體之應用", 3), ("數學導論", 3), ("數學教育概論", 3),
            ("數學遊戲數位設計與實務", 3), ("高等微積分(一)", 4), ("高等微積分(二)", 4),
            ("代數學(一)", 3), ("代數學(二)", 3), ("統計學(一)", 3), ("統計學(二)", 3),
            ("微分方程", 3), ("高等線性代數", 3), ("數值分析", 3), ("離散數學", 3),
            ("數學課程研究", 3), ("數學概念發展", 3), ("資訊科技融入數學教學", 3),
            ("視覺化資料分析", 3), ("資料探勘", 3), ("統計程式語言", 3), ("財務數學", 3),
            ("迴歸分析", 3), ("時間序列", 3), ("應用統計", 3), ("貝氏數據分析導論", 3),
            ("實變數函數論", 3), ("複變數函數論", 3), ("數學教學與評量研究", 3), ("數學教育專題研究", 3),
        ),
    }
    elective_options = math_elective_options_by_year[cohort]
    software = tuple(name for name, _credits in elective_options if name in {
        "數學軟體應用與實作(A)", "數學軟體應用與實作(B)", "數學軟體應用與實作(C)",
        "Matlab程式設計", "Python程式設計",
    })
    elective_names = tuple(name for name, _credits in elective_options)
    pool_coverage = COMPLETE
    pool_reason = ""
    rows = [
        _minor_row(
            cohort,
            program,
            None,
            "calculus_1",
            "微積分(一)",
            4,
            waiver=True,
            original_clause="微積分(一) 4 學分",
            coverage_state=COMPLETE,
        ),
        _minor_row(
            cohort,
            program,
            None,
            "calculus_2",
            "微積分(二)",
            4,
            waiver=True,
            original_clause="微積分(二) 4 學分",
            coverage_state=COMPLETE,
        ),
        _minor_row(
            cohort,
            program,
            None,
            "software_one_of",
            "數學軟體課程（擇一）",
            3,
            eligible_names=software,
            eligible_options=tuple(option for option in elective_options if option[0] in software),
            requirement_type="choice",
            choice_group="math_software",
            choice_rule="擇一，最多 3 學分",
            original_clause=f"數學軟體課程 {'／'.join(software)} 僅擇一採計 3 學分",
            coverage_state=COMPLETE,
        ),
        _minor_row(
            cohort,
            program,
            None,
            "other_electives_9",
            "其他表列選修（不含軟體擇一）",
            9,
            eligible_names=tuple(name for name in elective_names if name not in software),
            eligible_options=tuple(option for option in elective_options if option[0] not in software),
            requirement_type="course_pool",
            choice_group="math_electives",
            choice_rule="本系表列選修補足至少 12 學分",
            original_clause="表列選修至少 12 學分；軟體課程群僅擇一 3 學分",
            coverage_state=pool_coverage,
            manual_reason=pool_reason,
        ),
    ]
    if cohort == "113":
        # The current 113 handbook value is 3 credits.  Keep the previous
        # four-credit value as revision provenance rather than exposing a
        # same-title conflict candidate that can never be allocated.
        metadata["credit_revision_history"] = {
            "數學導論": {
                "current_credits": 3,
                "previous_credits": 4,
                "revision_state": "CURRENT_VALUE_VERIFIED",
                "source_reference": "handbook:113:math:minor:p.74",
            }
        }
    return rows, metadata


def _minor_assertions(cohort: str, program: str, track: str | None, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    source = _minor_source(cohort, program, track)
    label = f"{cohort} 學年度{_PROGRAM_DISPLAY[program]}輔系規則"
    if program == "apc":
        total = 20
        base = 20 if cohort == "115" else 16
        remainder = 0 if cohort == "115" else 4
        assertions = [
            _assertion(
                f"minor.{cohort}.apc.total20",
                cohort,
                source["pages"],
                "物化輔系指定課程總計 20 學分",
                total,
                label=label,
                url=source["source_url"],
            ),
            _assertion(
                f"minor.{cohort}.apc.base{base}",
                cohort,
                source["pages"],
                "物化輔系基礎列項",
                base,
                label=label,
                url=source["source_url"],
            ),
        ]
        if remainder:
            assertions.append(
                _assertion(
                    f"minor.{cohort}.apc.remainder{remainder}",
                    cohort,
                    source["pages"],
                    "同組主修必修剩餘額度",
                    remainder,
                    label=label,
                    url=source["source_url"],
                )
            )
        return _enrich_minor_assertions(assertions, source, label, metadata)
    if program == "earth":
        assertions = [
            _assertion(
                f"minor.{cohort}.earth.total24",
                cohort,
                source["pages"],
                "地生輔系共同必修 24 學分",
                24,
                label=label,
                url=source["source_url"],
            )
        ]
        return _enrich_minor_assertions(assertions, source, label, metadata)
    if program == "cs":
        assertions = [
            _assertion(
                f"minor.{cohort}.cs.total20",
                cohort,
                source["pages"],
                "計算機概論 3 + C 程式設計 3 + 資科其他開設課程至少 14 = 20",
                20,
                label=label,
                url=source["source_url"],
            )
        ]
        return _enrich_minor_assertions(assertions, source, label, metadata)
    if program == "math":
        assertions = [
            _assertion(
                f"minor.{cohort}.math.total20",
                cohort,
                source["pages"],
                "微積分 8 + 表列選修至少 12 = 20",
                20,
                label=label,
                url=source["source_url"],
            )
        ]
        return _enrich_minor_assertions(assertions, source, label, metadata)
    if program == "apc":
        total = 20
        claim = "輔系指定課程總計 20 學分"
        assertions = [_assertion(f"minor.{cohort}.apc.total20", cohort, source["pages"], claim, total, label=label, url=source["source_url"])]
        if cohort in {"111", "112", "113", "114"}:
            assertions.append(_assertion(f"minor.{cohort}.apc.unnamed4", cohort, source["pages"], "另有未具名必修課程 4 學分", 4, evidence_state=VERIFIED, label=label, url=source["source_url"]))
        return _enrich_minor_assertions(assertions, source, label, metadata)
    if program == "earth":
        assertions = [_assertion(f"minor.{cohort}.earth.total24", cohort, source["pages"], "輔系為共同必修 24 學分", 24, label=label, url=source["source_url"])]
        assertions.append(_assertion(f"minor.{cohort}.earth.zero_gate", cohort, source["pages"], "共同必修 0 學分項目是否為輔系 gate", "unknown", evidence_state=MANUAL_REVIEW, label=label, url=source["source_url"]))
        return _enrich_minor_assertions(assertions, source, label, metadata)
    if program == "cs":
        claim = (
            "資科輔系 aggregate 6 + 其他本系開設課程 14 = 20；命名基礎列項未完整辨識"
            if cohort == "115"
            else "計算機概論 3 + C 程式設計 3 + 其他本系開設課程 14 = 20"
        )
        assertions = [_assertion(f"minor.{cohort}.cs.total20", cohort, source["pages"], claim, 20, label=label, url=source["source_url"])]
        if cohort == "115":
            assertions.append(
                _assertion(
                    "minor.115.cs.missing_named_core",
                    cohort,
                    source["pages"],
                    "輔系第二個命名基礎課程",
                    "not_transcribed",
                    evidence_state=MISSING,
                    label=label,
                    url=source["source_url"],
                )
            )
        return _enrich_minor_assertions(assertions, source, label, metadata)
    assertions = [_assertion(f"minor.{cohort}.math.total20", cohort, source["pages"], "微積分 8 + 表列選修至少 12 = 20", 20, label=label, url=source["source_url"])]
    if cohort == "113":
        assertions.append(
            _assertion(
                f"minor.{cohort}.math.intro_current",
                cohort,
                source["pages"],
                "數學導論現行學分",
                3,
                original_clause="數學導論現行 3 學分；4 學分為修訂前值，僅保留於修訂來源。",
                label=label,
                url=source["source_url"],
            )
        )
    return _enrich_minor_assertions(assertions, source, label, metadata)


def _catalog_pool_projection(
    kind: str,
    cohort: str,
    program: str,
    track: str | None,
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Expose pool metadata for target/minor rows built outside primary rules."""

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        pool_ids = tuple(row.get("pool_ids", ())) + tuple(row.get("eligible_pool_ids", ()))
        if not pool_ids:
            continue
        for pool_id in pool_ids:
            entry = grouped.setdefault(
                str(pool_id),
                {
                    "id": str(pool_id),
                    "pool_id": str(pool_id),
                    "schema": _POOL_ID_SCHEMA,
                    "scope": _scope_slug(kind),
                    "kind": kind,
                    "curriculum_version": cohort,
                    "cohort": cohort,
                    "program_slug": program,
                    "track_slug": track or "department",
                    "label": str(row.get("bucket") or row.get("name") or pool_id),
                    "bucket": str(row.get("bucket") or "course_pool"),
                    "required_credits": None,
                    "max_credits": None,
                    "requirement_minimum_credits": None,
                    "subset_maxima": {},
                    "candidate_only": True,
                    "eligible_pool_ids": (str(pool_id),),
                    "overflow_routes": tuple(row.get("overflow_routes", ())),
                    "selection_rule": str(row.get("choice_rule") or "exact_title_credit_component"),
                    "identity_fields": _POOL_IDENTITY_FIELDS,
                    "allow_user_claimed_department": False,
                    "allowed_components": ("lecture", "lab"),
                    "evidence_state": str(row.get("evidence_state") or VERIFIED),
                    "verification_status": str(row.get("verification_status") or row.get("evidence_state") or VERIFIED),
                    "coverage_state": str(row.get("coverage_state") or PARTIAL),
                    "automation_sufficiency": str(row.get("automation_sufficiency") or PARTIAL),
                    "manual_reason": str(row.get("manual_reason") or ""),
                    "source_reference": str(row.get("source_reference") or ""),
                    "source_url": str(row.get("source_url") or ""),
                    "source_file": str(row.get("source_file") or ""),
                    "research_file": str(row.get("research_file") or ""),
                    "pdf_page": row.get("pdf_page", "未標示"),
                    "printed_page": row.get("printed_page", "未標示"),
                    "pages": row.get("pages", "未標示"),
                    "table_location": row.get("table_location", "官方課程表"),
                    "original_clause": str(row.get("original_clause") or ""),
                    "policy": {},
                    "candidate_courses": [],
                    "courses": [],
                },
            )
            if row.get("required_credits") is not None:
                entry["required_credits"] = float(row["required_credits"])
            elif row.get("kind") in {"quota", "choice"} and row.get("credits"):
                entry["required_credits"] = float(row["credits"])
            if row.get("max_credits") is not None:
                entry["max_credits"] = float(row["max_credits"])
            if row.get("requirement_minimum_credits") is not None:
                entry["requirement_minimum_credits"] = float(row["requirement_minimum_credits"])
            if isinstance(row.get("subset_maxima"), Mapping):
                entry["subset_maxima"] = deepcopy(dict(row["subset_maxima"]))
            for key in (
                "secondary_course_key",
                "math_secondary_composite_required",
                "requires_server_owned_offering",
                "pool_membership_evidence",
                "membership_assertions",
            ):
                if row.get(key) is not None:
                    entry[key] = deepcopy(row[key])
            names = tuple(row.get("eligible_course_names", ()))
            options = row.get("eligible_course_options", ())
            candidate_metadata = row.get("candidate_metadata", {})
            candidate_metadata = (
                candidate_metadata if isinstance(candidate_metadata, Mapping) else {}
            )
            option_map: dict[str, float] = {}
            if isinstance(options, (list, tuple)):
                for option in options:
                    if isinstance(option, Mapping) and option.get("name"):
                        option_map[str(option["name"])] = float(option.get("credits", row.get("credits", 0)) or 0)
            for name in names:
                option_map.setdefault(str(name), float(row.get("credits", 0) or 0))
            for name, credits in option_map.items():
                if credits <= 0:
                    continue
                if not any(
                    isinstance(item, Mapping) and item.get("name") == name
                    for item in entry["candidate_courses"]
                ):
                    entry["candidate_courses"].append(
                        _pool_course_entry(
                            cohort,
                            program,
                            track,
                            str(pool_id),
                            name,
                            credits,
                            row,
                            evidence_state=str(row.get("evidence_state") or VERIFIED),
                            course_metadata=(
                                candidate_metadata.get(name)
                                if isinstance(candidate_metadata.get(name), Mapping)
                                else None
                            ),
                        )
                    )
            entry["courses"] = entry["candidate_courses"]
    return grouped


def _target_source_contract(
    cohort: str,
    program: str,
    track: str | None,
    assertions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Normalize one target assertion into the pool source contract."""

    first = next((item for item in assertions if isinstance(item, Mapping)), {})
    nested = first.get("source") if isinstance(first.get("source"), Mapping) else {}
    pages = nested.get("pages") or first.get("pages") or "官方學生手冊"
    default_research = (
        "research/math_earth_handbook_matrix_111_115.md"
        if program in {"earth", "math"}
        else "research/apc_cs_handbook_matrix_111_115.md"
    )
    return {
        "research_file": str(nested.get("research_file") or first.get("research_file") or default_research),
        "source_reference": str(
            nested.get("source_reference")
            or first.get("source_reference")
            or f"handbook:{cohort}:target:{program}:{track or 'department'}"
        ),
        "source_url": str(nested.get("url") or nested.get("source_url") or first.get("source_url") or _HANDBOOK_URLS.get(cohort, "")),
        "source_file": str(nested.get("file") or nested.get("source_file") or first.get("source_file") or _source_file(cohort)),
        "pdf_page": nested.get("pdf_page") or first.get("pdf_page") or pages,
        "printed_page": nested.get("printed_page") or first.get("printed_page") or "未標示",
        "pages": pages,
        "table_location": str(nested.get("label") or first.get("table_location") or f"{cohort} {_PROGRAM_DISPLAY[program]} 雙主修表"),
        "original_clause": str(nested.get("original_clause") or first.get("original_clause") or first.get("claim") or ""),
    }


def _unresolved_target_pool_bundle(
    kind: str,
    cohort: str,
    program: str,
    track: str | None,
    thresholds: Mapping[str, Any],
    assertions: Sequence[Mapping[str, Any]],
    evidence_state: str,
    warning: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Expose aggregate-only target gaps as empty, year-scoped pools.

    Earth and most Math double-major pages currently prove the aggregate
    amounts but do not expose a safe named course table.  Returning no
    registry rows made those scopes impossible to audit or route for later
    manual review.  Math 113 is the documented exception: two courses struck
    from the required table are explicitly re-listed in the same-page
    elective table, so they remain candidate courses in the formal elective
    pool while the required pool stays separate.
    """

    source = _target_source_contract(cohort, program, track, assertions)
    pools: dict[str, dict[str, Any]] = {}
    requirements: list[dict[str, Any]] = []
    for bucket, amount in (("base_required", thresholds.get("base_required", thresholds.get("base"))), ("other_required", thresholds.get("other_required", thresholds.get("other")))):
        if amount is None or float(amount) <= 0:
            continue
        moved_elective_candidates: dict[str, int | float] = {}
        if kind == "double_major_target" and program == "math" and cohort == "113" and bucket == "other_required":
            moved_elective_candidates = {
                "高等微積分(一)": 4,
                "代數學(一)": 3,
            }
        has_named_elective_candidates = bool(moved_elective_candidates)
        pool_label = (
            "同頁修訂後正式選修候選課程池"
            if has_named_elective_candidates
            else f"未具名{bucket}課程池"
        )
        pool_policy = {
            "allow_user_claimed_department": False,
            "requires_official_named_course_pool": not has_named_elective_candidates,
            "aggregate_only": not has_named_elective_candidates,
        }
        if has_named_elective_candidates:
            pool_policy.update(
                {
                    "role": "formal_elective_candidate_pool",
                    "moved_from_required_courses": True,
                    "moved_course_names": tuple(moved_elective_candidates),
                }
            )
        pool = _pool_record(
            kind=kind,
            cohort=cohort,
            program=program,
            track=track,
            bucket=f"unresolved_{bucket}",
            label=pool_label,
            source=source,
            candidates=moved_elective_candidates,
            required_credits=float(amount),
            evidence_state=evidence_state,
            coverage_state=PARTIAL,
            selection_rule=(
                "official_named_elective_candidate_pool"
                if has_named_elective_candidates
                else "manual_named_pool_required"
            ),
            policy=pool_policy,
            manual_reason=warning,
        )
        pools[pool["id"]] = pool
        requirement_id = f"{program}.target.{cohort}.{track or 'department'}.pool.{_pool_token(bucket)}"
        clause = f"{source['table_location']}：{pool['label']} {float(amount):g} 學分；課程池尚未具名。"
        requirement = {
            "id": requirement_id,
            "requirement_id": requirement_id,
            "name": pool["label"],
            "raw_title": pool["label"],
            "display_name": pool["label"],
            "credits": float(amount),
            "required_credits": float(amount),
            "bucket": bucket,
            "kind": "quota",
            "requirement_type": "credit_quota",
            "choice_group": None,
            "choice_rule": (
                "official_named_elective_candidate_pool"
                if has_named_elective_candidates
                else "manual_named_pool_required"
            ),
            "eligible_course_names": tuple(moved_elective_candidates),
            "eligible_course_options": tuple(
                (name, float(credits))
                for name, credits in moved_elective_candidates.items()
            ),
            "pool_ids": (pool["id"],) if has_named_elective_candidates else (),
            "eligible_pool_ids": (pool["id"],),
            "overflow_routes": (),
            "candidate_only": False,
            "pool_requirement": True,
            "component": "quota",
            "component_type": "quota",
            "lecture_or_lab": "quota",
            "is_lab": False,
            "is_zero_credit": False,
            "waiver": False,
            "waiver_generates_credits": False,
            "allow_combined_lab_source": False,
            "evidence": evidence_state,
            "evidence_state": evidence_state,
            "verification_status": evidence_state,
            "coverage_state": PARTIAL,
            "automation_sufficiency": "PARTIAL",
            "automatic_decision": False,
            "manual_reason": warning,
            "manual_review_reason": warning,
            "program_slug": program,
            "track_slug": track or "department",
            "curriculum_version": cohort,
            "source_assertion_id": requirement_id,
            "assertion_id": requirement_id,
            "source_reference": f"{source['source_reference']}:pool:{_pool_token(bucket)}",
            "source_url": source["source_url"],
            "source_file": source["source_file"],
            "research_file": source["research_file"],
            "pdf_page": source["pdf_page"],
            "printed_page": source["printed_page"],
            "page": source["pdf_page"],
            "pages": source["pages"],
            "table_location": source["table_location"],
            "original_clause": clause,
            "original_text": clause,
            "source": {**source, "source_reference": f"{source['source_reference']}:pool:{_pool_token(bucket)}", "original_clause": clause},
            "provenance": {
                **source,
                "assertion_id": requirement_id,
                "source_reference": f"{source['source_reference']}:pool:{_pool_token(bucket)}",
                "original_clause": clause,
                "pool_id": pool["id"],
                "required_credits": float(amount),
                "evidence_state": evidence_state,
                "coverage_state": PARTIAL,
                "automatic_decision": False,
                "manual_reason": warning,
            },
        }
        requirements.append(requirement)
    return pools, requirements


def _enrich_minor_assertions(assertions: list[dict[str, Any]], source: Mapping[str, Any], label: str, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    curriculum_version = _normalize_cohort(source.get("section")) or ""
    for item in assertions:
        assertion_id = item.get("assertion_id") or item.get("id") or "assertion"
        official_reference = f"{source['source_reference']}:assertion:{assertion_id}"
        item_source = item.get("source")
        if isinstance(item_source, dict):
            item_source.update(
                {
                    "curriculum_version": curriculum_version,
                    "source_reference": official_reference,
                }
            )
        item.update(
            {
                "research_file": source["research_file"],
                "source_reference": official_reference,
                "source_url": source["source_url"],
                "source_file": source["source_file"],
                "pdf_page": source["pdf_page"],
                "printed_page": source["printed_page"],
                "table_location": source["section"],
                "original_clause": item.get("original_clause") or item.get("claim", ""),
                "original_text": item.get("original_clause") or item.get("claim", ""),
                "curriculum_version": curriculum_version,
                "coverage_state": "PARTIAL" if metadata.get("manual_review_reasons") else "COMPLETE",
                "verification_status": item.get("evidence_state", VERIFIED),
                "automatic_decision": item.get("evidence_state", VERIFIED) == VERIFIED and not metadata.get("manual_review_reasons"),
                "manual_reason": "; ".join(str(value) for value in metadata.get("manual_review_reasons", ()) if value),
                "manual_review_reason": "; ".join(str(value) for value in metadata.get("manual_review_reasons", ()) if value),
            }
        )
    return assertions


def _build_curriculum(kind: str, cohort: str, program: str, track: str | None) -> dict[str, Any]:
    curriculum_id = _canonical_id(kind, cohort, program, track)
    is_primary = kind == "primary"
    is_minor = kind in {_MINOR_TARGET_ROLE, "minor", "minor_target"}
    if is_primary:
        assertions = _primary_assertions(program, cohort, track)
        conflict = any(
            bool(item.get("blocks_decision", item.get("evidence_state") == CONFLICTED))
            for item in _primary_conflict_assertions(program, cohort)
            if isinstance(item, Mapping)
        )
        evidence_state = CONFLICTED if conflict else VERIFIED
        source_meta = _primary_evidence(cohort, program)
        # Coverage is scoped to this programme section.  A handbook may stay
        # PARTIAL overall because another department or a teacher-certification
        # branch is unresolved, while a fully transcribed Earth section can be
        # COMPLETE on its own evidence.
        coverage = source_meta.get("coverage_state", PARTIAL)
        if conflict:
            coverage = PARTIAL
        warning = "官方表格存在衝突，不能自動宣告主修門檻完整。" if conflict else source_meta.get("manual_reason", "")
    elif is_minor:
        catalog, minor_metadata = _minor_catalog(cohort, program, track)
        assertions = _minor_assertions(cohort, program, track, minor_metadata)
        row_conflict = any(
            isinstance(row, dict) and row.get("evidence_state") == CONFLICTED
            for row in catalog
        )
        assertion_conflict = any(
            isinstance(item, dict) and item.get("evidence_state") == CONFLICTED
            for item in assertions
        )
        evidence_state = CONFLICTED if row_conflict or assertion_conflict else VERIFIED
        # Every ordinary secondary catalogue is now backed by its own
        # year/track contract.  Individual course approval and server-owned
        # offering checks remain runtime gates; they do not make a complete
        # source transcription look like a missing catalogue.
        coverage = COMPLETE if not minor_metadata.get("manual_review_reasons") else PARTIAL
        warning = "; ".join(str(item) for item in minor_metadata.get("manual_review_reasons", ()) if item)
    else:
        assertions, evidence_state, coverage, warning = _double_assertions(program, cohort, track)
    if is_primary:
        thresholds = _base_thresholds(program, cohort, track)
        catalog = _course_catalog(program, cohort, kind, track, coverage)
        source_meta = _primary_evidence(cohort, program)
        course_pools, pool_requirements = _primary_pool_bundle(cohort, program, track, source_meta)
        university_requirements, non_credit_requirements = _primary_university_requirement_rows(
            cohort,
            program,
            track,
            course_pools,
            source_meta,
        )
        # Keep every zero-credit department row visible in the dedicated
        # non-credit metadata as well.  They remain exact rows in the catalog
        # for identity/provenance, while this projection lets the service
        # enforce their completion gates without treating them as credits.
        non_credit_requirements = [
            *non_credit_requirements,
            *[
                deepcopy(row)
                for row in catalog
                if isinstance(row, Mapping) and bool(row.get("is_zero_credit"))
            ],
        ]
        extra_metadata: dict[str, Any] = {
            "course_pools": course_pools,
            "pools": deepcopy(course_pools),
            "pool_requirements": pool_requirements,
            "university_requirements": university_requirements,
            "university_pool_requirements": deepcopy(university_requirements),
            "non_credit_requirements": non_credit_requirements,
        }
        if program == "cs":
            primary_catalog = _cs_primary_catalog(cohort)
            extra_metadata.update(
                {
                    "cs_primary_catalog": primary_catalog,
                    "primary_elective_rollups": (
                        {
                            "rollup_group_id": "cs_primary_elective_54",
                            "required_credits": 54,
                            "component_requirement_ids": (
                                f"cs.primary.{cohort}.alpha",
                                f"cs.primary.{cohort}.beta_remainder",
                            ),
                            "presentation_only": True,
                        },
                    ),
                }
            )
    elif is_minor:
        # Keep the aggregate rules separate from the individual rows.  The
        # extra metadata is intentionally descriptive and is consumed by the
        # service gate, never by a renderer-side recalculation.
        thresholds = {"total": 20 if program in {"apc", "cs", "math"} else 24}
        if program == "apc":
            thresholds.update(
                {
                    "fixed_base_credits": 20 if cohort == "115" else 16,
                    "remaining_required_credits": 0 if cohort == "115" else 4,
                }
            )
        if program == "earth":
            thresholds.update({"credit_total": 24, "zero_credit_gate": False})
        if program == "cs":
            thresholds.update({"mandatory_credits": 6, "other_course_credits": 14})
        if program == "math":
            thresholds.update(
                {
                    "calculus_credits": 8,
                    "elective_credits": 12,
                    "elective_maximum_credits": 20,
                }
            )
        course_pools = _catalog_pool_projection(kind, cohort, program, track, catalog)
        extra_metadata = {
            **minor_metadata,
            "course_pools": course_pools,
            "pools": deepcopy(course_pools),
            "pool_requirements": [
                deepcopy(row)
                for row in catalog
                if isinstance(row, Mapping) and row.get("eligible_pool_ids")
            ],
            "non_credit_requirements": [
                deepcopy(row)
                for row in catalog
                if isinstance(row, Mapping) and bool(row.get("is_zero_credit"))
            ],
        }
    else:
        thresholds = _double_thresholds(program, cohort, track)
        catalog = _course_catalog(program, cohort, kind, track, coverage)
        course_pools = _catalog_pool_projection(kind, cohort, program, track, catalog)
        if not course_pools:
            # Aggregate-only Earth/Math target pages still deserve an
            # explicit, empty pool and quota row.  This keeps the gap visible
            # to the allocator without inventing a target course list.
            course_pools, catalog = _unresolved_target_pool_bundle(
                kind,
                cohort,
                program,
                track,
                thresholds,
                assertions,
                evidence_state,
                warning or "官方目標頁未提供可安全逐課配置的課程池。",
            )
        extra_metadata = {
            "course_pools": course_pools,
            "pools": deepcopy(course_pools),
            "pool_requirements": [
                deepcopy(row)
                for row in catalog
                if isinstance(row, Mapping) and row.get("eligible_pool_ids")
            ],
            "non_credit_requirements": [
                deepcopy(row)
                for row in catalog
                if isinstance(row, Mapping) and bool(row.get("is_zero_credit"))
            ],
        }
    citations = []
    for item in assertions:
        source = item.get("source") or {}
        if source and source not in citations:
            citations.append(deepcopy(source))
    if not citations:
        citations = [_citation(cohort, "官方學生手冊", f"{cohort} {_PROGRAM_DISPLAY[program]}")]
    # The aggregate can be trusted independently from a named catalog.  This
    # matters for CS 115: the 15+25 statement is VERIFIED while named rows are
    # only PARTIAL.
    aggregate_status = VERIFIED if evidence_state in {VERIFIED, CONFLICTED} else evidence_state
    status = evidence_state if evidence_state == CONFLICTED else VERIFIED if coverage == COMPLETE else MANUAL_REVIEW
    record_source = next(
        (item for item in assertions if isinstance(item, dict)),
        {},
    )
    record_automatic = status == VERIFIED and coverage == COMPLETE and evidence_state == VERIFIED
    record_manual_reason = warning or ("課程目錄或官方語義尚未完整，需人工確認。" if not record_automatic else "")
    blockers: list[dict[str, Any]] = []
    if evidence_state == CONFLICTED:
        blockers.append({"code": CONFLICTED, "reason": warning or "官方來源 assertion 互相衝突。"})
    if coverage != COMPLETE:
        blockers.append({"code": MANUAL_REVIEW, "reason": f"課程目錄 coverage={coverage}，不足以安全逐課判定。"})
    return {
        "id": curriculum_id,
        "curriculum_id": curriculum_id,
        "kind": "primary" if is_primary else _MINOR_TARGET_ROLE if is_minor else "double_major_target",
        "type": "primary" if is_primary else "target",
        "curriculum_kind": "primary" if is_primary else _MINOR_TARGET_ROLE if is_minor else "double_major_target",
        "version": cohort,
        "curriculum_version": cohort,
        "admission_cohort": cohort if is_primary else None,
        "program": _PROGRAM_DISPLAY[program],
        "program_slug": program,
        "track": _TRACK_DISPLAY.get(track) if track else None,
        "track_slug": track or ("department" if is_minor else None),
        **(
            {
                "student_type": "non_teacher",
                "primary_domain_slug": track,
                "primary_domain_required": track in _MATH_PRIMARY_TRACKS.get(cohort, ()),
                "available_primary_domain_slugs": tuple(_MATH_PRIMARY_TRACKS.get(cohort, ())),
            }
            if is_primary and program == "math"
            else {}
        ),
        "program_type": "單主修" if is_primary else "輔系" if is_minor else _DOUBLE_MAJOR,
        "thresholds": thresholds,
        "requirements": deepcopy(thresholds),
        "total_required": thresholds.get("total_required", thresholds.get("total")),
        "base_required": thresholds.get("base_required") if not is_primary and not is_minor else None,
        "other_required": thresholds.get("other_required") if not is_primary and not is_minor else None,
        "course_catalog": catalog,
        "source_assertions": assertions,
        "assertions": deepcopy(assertions),
        "citations": citations,
        "source_file": _source_file(cohort),
        "source_url": _HANDBOOK_URLS.get(cohort, ""),
        "source_reference": record_source.get("source_reference", f"handbook:{cohort}:curriculum:{curriculum_id}"),
        "pdf_page": record_source.get("pdf_page", "未標示"),
        "printed_page": record_source.get("printed_page", "未標示"),
        "table_location": record_source.get("table_location", f"{cohort} {_PROGRAM_DISPLAY[program]} 課程表"),
        "original_clause": record_source.get("original_clause", ""),
        "verification_status": evidence_state,
        "automation_sufficiency": "COMPLETE" if record_automatic else "PARTIAL" if evidence_state == VERIFIED else "NONE",
        "automatic_decision": record_automatic,
        "manual_reason": record_manual_reason,
        "manual_review_reason": record_manual_reason,
        "research_file": record_source.get("research_file", ""),
        "evidence_state": evidence_state,
        "coverage_state": coverage,
        "aggregate_status": aggregate_status,
        "status": status,
        # ``pass_eligible`` describes the integrity of this curriculum
        # catalogue itself.  It is not the student's minor qualification;
        # that separate gate still requires scoped target evidence and the
        # official approval/registration chain in ``graduation_service``.
        "pass_eligible": status == VERIFIED and coverage == COMPLETE and evidence_state == VERIFIED,
        "warnings": [warning] if warning else [],
        "blockers": blockers,
        "legacy_planning": cohort in {"111", "115"} and is_primary and cohort not in _RULES.get("handbooks", {}),
        "handbook_metadata": deepcopy(_RULES.get("handbooks", {}).get(cohort, {}).get("_meta", {})),
        **extra_metadata,
    }


def _build_registry() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for cohort in _SUPPORTED_COHORTS:
        for program in ("earth", "apc", "cs", "math"):
            if program == "earth":
                primary_tracks = ["earth_environment", "life_science"]
            elif program == "apc":
                primary_tracks = ["physics", "chemistry"]
            elif program == "math":
                primary_tracks = list(_MATH_PRIMARY_TRACKS.get(cohort, ())) or [None]
            else:
                primary_tracks = [None]
            for track in primary_tracks:
                primary = _build_curriculum("primary", cohort, program, track)
                # Double-major and minor Math scopes remain department-level;
                # only the primary role receives the 113+ domain axis.
                target_track = track if program in {"earth", "apc"} else None
                target = _build_curriculum("double_major_target", cohort, program, target_track)
                result[primary["curriculum_id"]] = primary
                result.setdefault(target["curriculum_id"], target)
                # Minor targets are department-level for Earth/CS/Math and
                # track-level for APC.  Keep all 25 year scopes independent.
                minor_track = track if program == "apc" else None
                minor = _build_curriculum(_MINOR_TARGET_ROLE, cohort, program, minor_track)
                result.setdefault(minor["curriculum_id"], minor)
    return result


_REGISTRY = _build_registry()


def _normalize_kind(value: Any) -> str | None:
    text = _normalize_text(value).lower().replace("-", "_")
    if text in {"primary", "major", "home", "主修"}:
        return "primary"
    if text in {"minor", "minor_target", "secondary_minor", "輔系"}:
        return _MINOR_TARGET_ROLE
    if text in {"target", "double_major", "double_major_target", "doublemajor", "double", "雙主修", "dm"}:
        return "double_major_target"
    return None


def _record_matches(
    curriculum_id: str,
    *,
    kind: str | None = None,
    cohort: Any = None,
    program: Any = None,
    track: Any = None,
) -> bool:
    record = _REGISTRY.get(curriculum_id)
    if record is None:
        return False
    expected_kind = _normalize_kind(kind) if kind is not None else None
    expected_cohort = _normalize_cohort(cohort) if cohort not in (None, "") else None
    expected_program = _program_slug(program) if program not in (None, "") else None
    expected_track = (
        "department"
        if expected_kind == _MINOR_TARGET_ROLE and _normalize_text(track).lower() in {"department", "系級", "系所"}
        else _track_slug(track, expected_program) if track not in (None, "") else None
    )
    if kind is not None and expected_kind is None:
        return False
    if track not in (None, "") and expected_track is None:
        return False
    return all(
        (
            expected_kind is None or record.get("kind") == expected_kind,
            expected_cohort is None or record.get("version") == expected_cohort,
            expected_program is None or record.get("program_slug") == expected_program,
            expected_track is None or record.get("track_slug") == expected_track,
        )
    )


def _parse_id(value: Any, *, kind: str | None = None, cohort: Any = None, program: Any = None, track: Any = None) -> str | None:
    """Parse canonical, dotted, slash-delimited and Chinese-friendly IDs."""

    if isinstance(value, dict):
        for key in ("curriculum_id", "target_curriculum_id", "id", "version"):
            if value.get(key):
                parsed = _parse_id(value[key], kind=kind, cohort=cohort, program=program, track=track)
                if parsed:
                    return parsed
        return None
    text = _normalize_text(value)
    if not text:
        return None
    if text in _REGISTRY:
        return text if _record_matches(text, kind=kind, cohort=cohort, program=program, track=track) else None
    parts = [part for part in re.split(r"[.:/|]+", text) if part]
    parsed_kind = _normalize_kind(kind) if kind is not None else None
    parsed_cohort = _normalize_cohort(cohort)
    parsed_program = _program_slug(program)
    parsed_track = _track_slug(track, parsed_program)
    for part in parts:
        low = part.lower().replace("-", "_")
        if low in {"primary", "major", "home", "主修"}:
            parsed_kind = "primary"
        elif low in {"minor", "minor_target", "secondary_minor", "輔系"}:
            parsed_kind = _MINOR_TARGET_ROLE
        elif low in {"target", "double_major", "double_major_target", "doublemajor", "double", "雙主修", "dm"}:
            parsed_kind = "double_major_target"
        elif _normalize_cohort(part):
            parsed_cohort = _normalize_cohort(part)
        elif low in _SLUG_ALIASES:
            mapped = _SLUG_ALIASES[low]
            if mapped in _PROGRAM_SLUGS:
                parsed_program = mapped
            elif mapped in _TRACK_SLUGS:
                parsed_track = mapped
        elif part in _PROGRAMS or _program_slug(part):
            mapped = _program_slug(part)
            if mapped in _PROGRAM_SLUGS:
                parsed_program = mapped
        elif _track_slug(part, parsed_program):
            parsed_track = _track_slug(part, parsed_program)
    if parsed_kind is None:
        parsed_kind = "primary"
    if parsed_cohort not in _SUPPORTED_COHORTS or parsed_program not in _PROGRAM_SLUGS:
        return None
    if parsed_kind == _MINOR_TARGET_ROLE:
        if parsed_program == "apc" and parsed_track not in {"physics", "chemistry"}:
            return None
        if parsed_program in {"earth", "cs", "math"}:
            if parsed_track in (None, "department"):
                parsed_track = "department"
            else:
                return None
    elif parsed_program == "earth" and parsed_track not in {"earth_environment", "life_science"}:
        if parsed_track is None:
            parsed_track = "earth_environment"
        else:
            return None
    if parsed_kind != _MINOR_TARGET_ROLE and parsed_program == "apc" and parsed_track not in {"physics", "chemistry"}:
        return None
    if parsed_kind != _MINOR_TARGET_ROLE and parsed_program == "math":
        # 111/112 keep the department-level primary ID.  From 113 onward
        # the handbook has three (or, in 115, two) mutually exclusive primary
        # domains.  A bare ID must therefore fail closed instead of silently
        # choosing the first domain or merging all domain requirements.
        allowed_math_tracks = _MATH_PRIMARY_TRACKS.get(str(parsed_cohort), ())
        if parsed_kind == "primary":
            if parsed_cohort in {"111", "112"}:
                if parsed_track is not None:
                    return None
                parsed_track = None
            elif parsed_track not in allowed_math_tracks:
                return None
        elif parsed_track is not None:
            return None
        else:
            parsed_track = None
    elif parsed_kind != _MINOR_TARGET_ROLE and parsed_program == "cs":
        if parsed_track is not None:
            return None
        parsed_track = None
    expected_kind = _normalize_kind(kind) if kind is not None else None
    expected_cohort = _normalize_cohort(cohort) if cohort not in (None, "") else None
    expected_program = _program_slug(program) if program not in (None, "") else None
    expected_track = (
        "department"
        if _normalize_kind(kind) == _MINOR_TARGET_ROLE and _normalize_text(track).lower() in {"department", "系級", "系所"}
        else _track_slug(track, expected_program) if track not in (None, "") else None
    )
    if kind is not None and (expected_kind is None or parsed_kind != expected_kind):
        return None
    if expected_cohort is not None and parsed_cohort != expected_cohort:
        return None
    if expected_program is not None and parsed_program != expected_program:
        return None
    if track not in (None, "") and (expected_track is None or parsed_track != expected_track):
        return None
    canonical = _canonical_id(parsed_kind, parsed_cohort, parsed_program, parsed_track)
    return canonical if _record_matches(canonical, kind=kind, cohort=cohort, program=program, track=track) else None


def get_curriculum(curriculum_id: Any) -> dict[str, Any]:
    """Return one immutable-by-copy, versioned curriculum record.

    ``curriculum_id`` accepts the canonical IDs emitted by this module (for
    example ``primary:114:apc:chemistry`` and
    ``target:double_major:114:apc:chemistry``), dotted/slash aliases, and
    Chinese programme labels when a cohort is supplied by the caller through a
    mapping.  Unknown IDs fail explicitly rather than falling back to a
    neighbouring handbook.
    """

    if isinstance(curriculum_id, dict):
        parsed = _parse_id(curriculum_id)
    else:
        parsed = _parse_id(curriculum_id)
    if not parsed:
        raise KeyError(f"找不到版本化課程表：{curriculum_id}")
    record = deepcopy(_REGISTRY[parsed])
    from confirmed_rules import validate_confirmed_rules
    validate_confirmed_rules(_RULES, record)
    return record


def list_curriculum_ids(*, kind: str | None = None, cohort: Any = None) -> list[str]:
    """List registry IDs for discovery UIs and integration tests."""

    selected_cohort = _normalize_cohort(cohort) if cohort is not None else None
    ids = []
    normalized_kind = _normalize_kind(kind) if kind not in (None, "", "target") else None
    for curriculum_id, item in _REGISTRY.items():
        if kind == "target":
            matches_kind = item["kind"] == "double_major_target"
        elif normalized_kind is not None:
            matches_kind = item["kind"] == normalized_kind
        else:
            matches_kind = True
        if not matches_kind:
            continue
        if selected_cohort and item["version"] != selected_cohort:
            continue
        ids.append(curriculum_id)
    return sorted(ids)


def _request_value(request: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = request.get(key)
        if value not in (None, ""):
            return value
    return None


def _target_program_and_track(request: dict[str, Any]) -> tuple[str | None, str | None]:
    raw_program = _request_value(request, "target_program", "target_dept", "secondary_program", "double_major_target")
    raw_track = _request_value(request, "target_track", "secondary_track")
    program = _program_slug(raw_program)
    track = _track_slug(raw_track, program)
    if track is None:
        inferred_track = _track_slug(raw_program)
        if inferred_track:
            inferred_program = _TRACK_PROGRAMS[inferred_track]
            if program is None:
                program = inferred_program
            if program == inferred_program:
                track = inferred_track
    if program == "earth" and track is None and raw_track in (None, ""):
        track = "earth_environment"
    return program, track


def _secondary_kind(request: Mapping[str, Any]) -> str:
    """Normalize the additive secondary programme role.

    ``program_type`` remains a compatibility input for existing callers, but
    the explicit ``secondary_kind`` field wins whenever it is provided.
    """

    value = _request_value(dict(request), "secondary_kind", "secondary_program_type", "program_type")
    text = _normalize_text(value).lower().replace("-", "_").replace(" ", "")
    if text in {"minor", "minor_target", "secondary_minor", "輔系"}:
        return _MINOR_TARGET_ROLE
    if text in {"double", "double_major", "double_major_target", "doublemajor", "雙主修", "dm"}:
        return "double_major_target"
    return "none"


def _evidence_reference(request: dict[str, Any]) -> str:
    value = _request_value(
        request,
        "target_version_evidence_reference",
        "target_curriculum_evidence_reference",
        "version_evidence_reference",
        "target_version_evidence",
    )
    if isinstance(value, dict):
        value = _request_value(
            value,
            "record_id",
            "evidence_record_id",
            "reference_id",
            "reference",
            "url",
            "id",
            "evidence_reference",
        )
    return str(value or "").strip()


def _applicability_assertion(request: dict[str, Any]) -> Any:
    return _request_value(
        request,
        "scoped_applicability_assertion",
        "target_applicability_assertion",
        "version_applicability_assertion",
        "applicability_assertion",
    )


def _assertion_curriculum_id(
    assertion: Any,
    *,
    cohort: str | None,
    program: str | None,
    track: str | None,
    target_kind: str = "double_major_target",
) -> str | None:
    if not isinstance(assertion, dict):
        return None
    value = _request_value(assertion, "curriculum_id", "target_curriculum_id", "id", "version")
    if value in (None, ""):
        return None
    # A target curriculum's version is independent from the student's
    # admission cohort.  The assertion is checked against the selected target
    # identity later, so do not constrain its version here.
    return _parse_id(value, kind=target_kind, cohort=None, program=program, track=track)


def _trusted_record_id(value: Any) -> str:
    if isinstance(value, dict):
        value = _request_value(value, "record_id", "evidence_record_id", "reference_id", "id")
    return str(value or "").strip()


def _application_event(value: Any, *, year: Any = None, semester: Any = None) -> tuple[str, ...] | None:
    term = _normalize_text(value)
    if term:
        return ("term", term)
    normalized_year = _normalize_text(year)
    normalized_semester = _normalize_text(semester)
    if normalized_year and normalized_semester:
        return ("year_semester", normalized_year, normalized_semester)
    return None


def _record_application_event(record: dict[str, Any]) -> tuple[str, ...] | None:
    return _application_event(
        _request_value(record, "application_term", "application_cohort"),
        year=_request_value(record, "application_year"),
        semester=_request_value(record, "application_semester", "application_term_semester"),
    )


def _request_application_event(request: dict[str, Any]) -> tuple[str, ...] | None:
    return _application_event(
        _request_value(request, "application_term", "application_cohort"),
        year=_request_value(request, "application_year"),
        semester=_request_value(request, "application_semester", "application_term_semester"),
    )


def _same_application_event(request_event: tuple[str, ...] | None, record_event: tuple[str, ...] | None) -> bool:
    if request_event is None or record_event is None:
        return False
    if request_event == record_event:
        return True
    if request_event[0] == "term" and record_event[0] == "year_semester":
        return request_event[1] == f"{record_event[1]}-{record_event[2]}"
    if request_event[0] == "year_semester" and record_event[0] == "term":
        return record_event[1] == f"{request_event[1]}-{request_event[2]}"
    return False


def _record_source_reference(record: dict[str, Any]) -> str:
    value = _request_value(
        record,
        "evidence_reference",
        "source_reference",
        "evidence_url",
        "source_url",
    )
    if value in (None, ""):
        source = record.get("source")
        if isinstance(source, dict):
            value = _request_value(source, "reference", "url", "id")
        elif source not in (None, ""):
            value = source
    return _normalize_text(value)


def _resolve_evidence_record(
    evidence_resolver: Any,
    reference: Any,
    *,
    request: dict[str, Any],
    selected_curriculum_id: str | None,
    cohort: str | None,
    program: str | None,
    track: str | None,
    target_kind: str = "double_major_target",
) -> dict[str, Any] | None:
    """Resolve and validate one server-owned applicability record.

    ``evidence_resolver`` is deliberately injected at the public boundary;
    request/config data can contain only its opaque record ID.  The returned
    record is used for validation only and is never copied into the request
    snapshot.
    """

    if not callable(evidence_resolver):
        return None
    record_id = _trusted_record_id(reference)
    if not record_id:
        return None
    try:
        record = evidence_resolver(record_id)
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    returned_record_id = _trusted_record_id(record.get("record_id"))
    if returned_record_id and returned_record_id != record_id:
        return None
    if record.get("evidence_state") != VERIFIED:
        return None
    record_curriculum_id = _parse_id(
        _request_value(record, "curriculum_id", "target_curriculum_id"),
        kind=target_kind,
        cohort=None,
        program=program,
        track=track,
    )
    if not record_curriculum_id or (selected_curriculum_id and record_curriculum_id != selected_curriculum_id):
        return None
    record_cohort = _normalize_cohort(_request_value(record, "admission_cohort", "cohort"))
    if not cohort or not record_cohort or record_cohort != cohort:
        return None
    record_program = _program_slug(_request_value(record, "target_program", "target_dept", "program"))
    if not program or not record_program or record_program != program:
        return None
    record_track_value = _request_value(record, "target_track", "track")
    record_track = (
        "department"
        if target_kind == _MINOR_TARGET_ROLE and _normalize_text(record_track_value).lower() in {"department", "系級", "系所", ""}
        else _track_slug(record_track_value, record_program)
    )
    if track:
        if not record_track or record_track != track:
            return None
    elif record_track_value not in (None, ""):
        # CS and Math have no target track.  Reject a record that tries to
        # smuggle another programme's track into their applicability scope.
        return None
    request_event = _request_application_event(request)
    record_event = _record_application_event(record)
    if not _same_application_event(request_event, record_event):
        return None
    if _normalize_text(record.get("authority")) == "":
        return None
    if not _record_source_reference(record):
        return None
    return deepcopy(record)


_REQUEST_SNAPSHOT_KEYS = {
    "admission_cohort",
    "handbook_year",
    "cohort",
    "primary_program",
    "primary_dept",
    "primary_track",
    "program_name",
    "domain",
    "primary_curriculum_id",
    "primary_curriculum_version",
    "primary_version",
    "program_type",
    "program",
    "secondary_kind",
    "secondary_program_type",
    "target_program",
    "target_dept",
    "secondary_program",
    "target_track",
    "secondary_track",
    "target_curriculum_version",
    "target_curriculum_id",
    "target_version",
    "target_version_evidence_reference",
    "target_curriculum_evidence_reference",
    "version_evidence_reference",
    "target_version_evidence",
    "scoped_applicability_assertion",
    "target_applicability_assertion",
    "version_applicability_assertion",
    "applicability_assertion",
    "application_term",
    "application_cohort",
    "application_year",
    "application_semester",
    "application_term_semester",
}

_EVIDENCE_REQUEST_KEYS = {
    "target_version_evidence_reference",
    "target_curriculum_evidence_reference",
    "version_evidence_reference",
    "target_version_evidence",
    "scoped_applicability_assertion",
    "target_applicability_assertion",
    "version_applicability_assertion",
    "applicability_assertion",
}


def _request_snapshot(request: dict[str, Any]) -> dict[str, Any]:
    """Keep an audit-safe request without injected services or user records."""

    snapshot: dict[str, Any] = {}
    for key in _REQUEST_SNAPSHOT_KEYS:
        value = request.get(key)
        if value in (None, ""):
            continue
        if key in _EVIDENCE_REQUEST_KEYS:
            if isinstance(value, dict):
                opaque_id = _trusted_record_id(value)
                if opaque_id:
                    snapshot[key] = opaque_id
            else:
                snapshot[key] = _normalize_text(value)
            continue
        if isinstance(value, (str, int, float, bool)):
            snapshot[key] = value
        elif key in {"target_curriculum_version", "target_curriculum_id", "target_version"}:
            opaque_id = _trusted_record_id(value)
            if opaque_id:
                snapshot[key] = opaque_id
    return snapshot


def _is_scoped_assertion(assertion: Any, request: dict[str, Any]) -> bool:
    if not isinstance(assertion, dict):
        return False
    scope_keys = {
        "scope",
        "student_id",
        "admission_cohort",
        "application_term",
        "application_year",
        "target_program",
        "target_dept",
        "target_track",
        "effective_term",
        "approval_id",
    }
    if not any(assertion.get(key) not in (None, "") for key in scope_keys):
        return False
    # If the assertion carries the same dimensions, reject an obvious
    # cross-program/cross-cohort citation instead of treating it as scoped.
    req_cohort = _normalize_cohort(_request_value(request, "admission_cohort", "handbook_year"))
    assertion_cohort = _normalize_cohort(assertion.get("admission_cohort"))
    if req_cohort and assertion_cohort and req_cohort != assertion_cohort:
        return False
    req_program, req_track = _target_program_and_track(request)
    assertion_program = _program_slug(_request_value(assertion, "target_program", "target_dept", "program"))
    assertion_track = _track_slug(_request_value(assertion, "target_track", "track"), assertion_program)
    if req_program and assertion_program and req_program != assertion_program:
        return False
    if req_track and assertion_track and req_track != assertion_track:
        return False
    return True


def _dimension(
    status: str,
    *,
    value: Any = None,
    curriculum: dict[str, Any] | None = None,
    reason: str = "",
    evidence_reference: str = "",
    evidence_state: str | None = None,
    coverage_state: str | None = None,
) -> dict[str, Any]:
    item = {
        "status": status,
        "state": status,
        "value": value,
        "reason": reason,
        "evidence_reference": evidence_reference,
        "evidence_state": evidence_state or (curriculum or {}).get("evidence_state"),
        "coverage_state": coverage_state or (curriculum or {}).get("coverage_state"),
        "curriculum_id": (curriculum or {}).get("curriculum_id"),
    }
    if curriculum is not None:
        item["curriculum"] = deepcopy(curriculum)
    return item


def resolve_rule_context(request: Any, *, evidence_resolver: Any = None) -> dict[str, Any]:
    """Resolve primary/target rule dimensions without guessing versions.

    The target dimension is intentionally stricter than the primary one.  A
    plain ``target_curriculum_version`` is only a user selection and therefore
    produces ``MANUAL_REVIEW``.  It becomes ``RESOLVED`` only when an opaque
    evidence record ID can be resolved by the injected server-owned resolver
    and its concrete applicability fields match this request.  Even then a
    conflicted or partially covered curriculum leaves a blocker in the overall
    resolution.
    """

    request = dict(request) if isinstance(request, dict) else {}
    cohort = _normalize_cohort(_request_value(request, "admission_cohort", "handbook_year", "cohort"))
    primary_id_value = _request_value(
        request,
        "primary_curriculum_id",
        "primary_curriculum_version",
        "primary_version",
    )
    primary_id_provided = primary_id_value not in (None, "")
    explicit_primary_id = _parse_id(primary_id_value, kind="primary") if primary_id_provided else None
    explicit_primary = _REGISTRY.get(explicit_primary_id) if explicit_primary_id else None
    primary_raw = _request_value(request, "primary_program", "primary_dept", "domain", "program_name")
    primary_program = _program_slug(primary_raw)
    primary_track = _track_slug(_request_value(request, "primary_track", "track"), primary_program)
    if primary_track is None:
        inferred_track = _track_slug(primary_raw)
        if inferred_track:
            inferred_program = _TRACK_PROGRAMS[inferred_track]
            if primary_program is None:
                primary_program = inferred_program
            if primary_program == inferred_program:
                primary_track = inferred_track
    if primary_program == "earth" and primary_track is None and _request_value(request, "primary_track", "track") in (None, ""):
        primary_track = "earth_environment"
    primary_identity_mismatch = False
    if explicit_primary is not None:
        explicit_program = _program_slug(explicit_primary.get("program_slug"))
        explicit_track = _track_slug(explicit_primary.get("track_slug"), explicit_program)
        raw_primary_track = _request_value(request, "primary_track", "track")
        if primary_program is None:
            primary_program = explicit_program
            if raw_primary_track in (None, ""):
                primary_track = explicit_track
        elif primary_program != explicit_program:
            primary_identity_mismatch = True
        if raw_primary_track not in (None, ""):
            requested_track = _track_slug(raw_primary_track, primary_program or explicit_program)
            if requested_track is None or requested_track != explicit_track:
                primary_identity_mismatch = True
    secondary_kind = _secondary_kind(request)
    is_double = secondary_kind == "double_major_target"
    is_minor = secondary_kind == _MINOR_TARGET_ROLE
    target_program, target_track = _target_program_and_track(request)
    if is_minor and target_program in {"earth", "cs", "math"}:
        # These minor scopes are department-level.  Never inherit a primary
        # track (or infer one from a friendly label) for them.
        target_track = None

    dimensions: dict[str, dict[str, Any]] = {}
    blockers: list[dict[str, Any]] = []
    warnings: list[str] = []

    if not cohort:
        dimensions["admission_cohort"] = _dimension(MISSING, reason="缺少 admission_cohort；不能選擇原主修手冊。")
    else:
        dimensions["admission_cohort"] = _dimension(RESOLVED, value=cohort, reason="已由使用者明確提供入學 cohort。")
    if primary_id_provided and explicit_primary is None:
        dimensions["primary_curriculum"] = _dimension(
            MISSING,
            value=primary_id_value,
            reason="指定的 primary curriculum ID 不是 registry 中可驗證的原主修課表，不能靜默改用 admission_cohort。",
        )
    elif not primary_program:
        dimensions["primary_curriculum"] = _dimension(MISSING, reason="缺少原主修系所／組別。")
    elif not cohort:
        dimensions["primary_curriculum"] = _dimension(
            MISSING,
            value=explicit_primary_id if explicit_primary else None,
            curriculum=explicit_primary,
            reason="缺 admission_cohort，不能確認原主修課表是否與入學 cohort 相符。",
        )
    else:
        primary_id = explicit_primary_id or _canonical_id("primary", cohort, primary_program, primary_track)
        primary = _REGISTRY.get(primary_id)
        if primary is None:
            dimensions["primary_curriculum"] = _dimension(MISSING, reason=f"找不到原主修課表：{primary_id}。")
        else:
            version_mismatch = bool(explicit_primary and primary.get("version") != cohort)
            if primary_identity_mismatch:
                primary_status = MANUAL_REVIEW
                primary_reason = "explicit primary curriculum ID 與請求中的主修系所／組別不一致，需人工確認。"
            elif version_mismatch:
                primary_status = MANUAL_REVIEW
                primary_reason = "explicit primary curriculum handbook 年度與 admission cohort 不同，需人工確認；不得自動套用 admission cohort。"
            else:
                primary_status = CONFLICTED if primary["evidence_state"] == CONFLICTED else RESOLVED
                primary_reason = "原主修課表由明確選定的 primary curriculum ID 驗證，且與 admission cohort／主修身分一致。" if explicit_primary else "原主修課表由 admission_cohort 與主修系所解析。"
            dimensions["primary_curriculum"] = _dimension(
                primary_status,
                value=primary_id,
                curriculum=primary,
                reason=primary_reason,
            )

    application_term = _request_value(request, "application_term", "application_cohort")
    application_year = _request_value(request, "application_year")
    application_semester = _request_value(request, "application_semester", "application_term_semester")
    if application_term not in (None, "") or application_year not in (None, "") or application_semester not in (None, ""):
        dimensions["application_event"] = _dimension(
            RESOLVED,
            value=application_term or {"year": application_year, "semester": application_semester},
            reason="已保留使用者提供的申請事件；它不會單獨推定目標課表版本。",
        )
    elif is_double or is_minor:
        dimensions["application_event"] = _dimension(
            MISSING,
            reason="雙主修／輔系缺少 application term／申請事件證據。",
        )
    else:
        dimensions["application_event"] = _dimension(NOT_APPLICABLE, reason="單主修不需要雙主修申請事件。")

    target_curriculum: dict[str, Any] | None = None
    target_kind = _MINOR_TARGET_ROLE if is_minor else "double_major_target"
    target_label = "輔系" if is_minor else "雙主修"
    if not (is_double or is_minor):
        dimensions["target_program"] = _dimension(NOT_APPLICABLE, reason="目前沒有雙主修或輔系規劃。")
        dimensions["target_curriculum_version"] = _dimension(NOT_APPLICABLE, reason="單主修不需要目標課表版本。")
    elif not target_program:
        dimensions["target_program"] = _dimension(MISSING, reason=f"缺少{target_label}目標系所／組別。")
        dimensions["target_curriculum_version"] = _dimension(MISSING, reason="未能建立目標系所，故不能解析 target curriculum version。")
    else:
        target_id_value = _request_value(request, "target_curriculum_version", "target_curriculum_id", "target_version")
        assertion = _applicability_assertion(request)
        assertion_scoped = _is_scoped_assertion(assertion, request)
        assertion_id = _assertion_curriculum_id(
            assertion,
            cohort=cohort,
            program=target_program,
            track=target_track,
            target_kind=target_kind,
        )
        evidence_value = _request_value(
            request,
            "target_version_evidence_reference",
            "target_curriculum_evidence_reference",
            "version_evidence_reference",
            "target_version_evidence",
        )
        evidence_reference = _evidence_reference(request)
        if not cohort:
            dimensions["target_program"] = _dimension(RESOLVED, value=target_program, reason="目標系所已提供，但缺 admission_cohort。")
            dimensions["target_curriculum_version"] = _dimension(MISSING, reason="只有 application term 或其他事件時，不能解析目標課表版本。")
        elif target_program == "apc" and target_track is None:
            dimensions["target_program"] = _dimension(MISSING, value=target_program, reason=f"物化{target_label}必須明確指定化學組或物理組。")
            dimensions["target_curriculum_version"] = _dimension(MISSING, reason=f"缺少物化{target_label}組別，不能解析目標課表版本。")
        else:
            dimensions["target_program"] = _dimension(RESOLVED, value=target_program, reason="目標系所／組別已由使用者提供。")
            # Target curriculum version is an independent dimension.  The
            # selected target may be from a different handbook version than
            # the student's admission cohort.
            selected_target_id = _parse_id(
                target_id_value,
                kind=target_kind,
                cohort=None,
                program=target_program,
                track=target_track,
            )
            selected_id_provided = target_id_value not in (None, "")
            assertion_id_provided = assertion_id is not None or (
                isinstance(assertion, dict)
                and _request_value(assertion, "curriculum_id", "target_curriculum_id", "id", "version") not in (None, "")
            )
            assertion_mismatch = bool(assertion_id_provided and assertion_id is None)
            if selected_target_id and assertion_id and selected_target_id != assertion_id:
                assertion_mismatch = True
            if isinstance(assertion, dict) and assertion is not None and not assertion_scoped:
                assertion_mismatch = assertion_mismatch or any(
                    assertion.get(key) not in (None, "")
                    for key in (
                        "scope",
                        "student_id",
                        "admission_cohort",
                        "application_term",
                        "application_year",
                        "target_program",
                        "target_dept",
                        "target_track",
                        "effective_term",
                        "approval_id",
                    )
                )
            # A string assertion is only meaningful as a trusted record ID.
            # If both evidence references are supplied, validate both rather
            # than silently letting one hide a mismatching other reference.
            trusted_evidence_record = _resolve_evidence_record(
                evidence_resolver,
                evidence_value,
                request=request,
                selected_curriculum_id=selected_target_id,
                cohort=cohort,
                program=target_program,
                track=target_track,
                target_kind=target_kind,
            )
            trusted_assertion_record = _resolve_evidence_record(
                evidence_resolver,
                assertion,
                request=request,
                selected_curriculum_id=selected_target_id,
                cohort=cohort,
                program=target_program,
                track=target_track,
                target_kind=target_kind,
            )
            if assertion not in (None, "") and not isinstance(assertion, dict) and trusted_assertion_record is None:
                assertion_mismatch = True
            if (
                trusted_evidence_record is not None
                and trusted_assertion_record is not None
                and _request_value(trusted_evidence_record, "curriculum_id", "target_curriculum_id")
                != _request_value(trusted_assertion_record, "curriculum_id", "target_curriculum_id")
            ):
                assertion_mismatch = True
            # Keep a selected ID visible for diagnostics, but never infer a
            # target version from an applicability assertion alone.
            if not selected_id_provided:
                dimensions["target_curriculum_version"] = _dimension(
                    MISSING,
                    reason=f"TARGET_SCOPE_INCOMPLETE：{target_label} target curriculum version 必須明確提供；不能由 admission_cohort 或 application term 推定。",
                )
            elif selected_target_id is None:
                dimensions["target_curriculum_version"] = _dimension(
                    MISSING,
                    value=target_id_value,
                    reason=f"指定的 target curriculum ID 必須是符合目標系所／組別的 {target_kind} 版本。",
                )
            elif assertion_mismatch:
                dimensions["target_curriculum_version"] = _dimension(
                    MANUAL_REVIEW,
                    value=selected_target_id,
                    reason="selected target curriculum ID 與 applicability assertion 不一致或 assertion 未通過 scope 驗證。",
                )
            else:
                target_curriculum = _REGISTRY.get(selected_target_id)
                trusted_record = trusted_evidence_record or trusted_assertion_record
                if target_curriculum is None:
                    dimensions["target_curriculum_version"] = _dimension(MISSING, value=selected_target_id, reason="指定的目標課表版本不在 registry。")
                elif trusted_record is None and not (assertion_scoped or evidence_reference):
                    dimensions["target_curriculum_version"] = _dimension(
                        MANUAL_REVIEW,
                        value=selected_target_id,
                        curriculum=target_curriculum,
                        reason="目前只有 user-selected version，缺 scoped applicability assertion 或 version evidence reference。",
                    )
                elif trusted_record is None:
                    dimensions["target_curriculum_version"] = _dimension(
                        MANUAL_REVIEW,
                        value=selected_target_id,
                        curriculum=target_curriculum,
                        evidence_reference=evidence_reference,
                        reason="提供的 evidence reference／scoped assertion 未由 server-owned evidence resolver 驗證，不能自動解析。",
                    )
                elif target_curriculum["evidence_state"] == CONFLICTED:
                    dimensions["target_curriculum_version"] = _dimension(
                        CONFLICTED,
                        value=selected_target_id,
                        curriculum=target_curriculum,
                        evidence_reference=evidence_reference,
                        reason="指定版本的官方 assertions 互相衝突，不能自動選邊。",
                    )
                else:
                    dimensions["target_curriculum_version"] = _dimension(
                        RESOLVED,
                        value=selected_target_id,
                        curriculum=target_curriculum,
                        evidence_reference=evidence_reference,
                        reason="目標版本由 server-owned evidence resolver 的適用範圍記錄解析。",
                    )
                    if target_curriculum["coverage_state"] != COMPLETE:
                        warnings.append(
                            f"目標課表 {selected_target_id} 的 course catalog coverage={target_curriculum['coverage_state']}；逐課結果仍需人工複核。"
                        )

    # Keep blocker details structured and also expose compact aliases for
    # clients that only need the first/unique code.
    for name, dimension in dimensions.items():
        status = dimension.get("status")
        if status in {MISSING, MANUAL_REVIEW, CONFLICTED}:
            blockers.append(
                {
                    "code": status,
                    "dimension": name,
                    "reason": dimension.get("reason", ""),
                    "curriculum_id": dimension.get("curriculum_id"),
                }
            )
    primary_curriculum_record = dimensions.get("primary_curriculum", {}).get("curriculum")
    if (
        isinstance(primary_curriculum_record, dict)
        and primary_curriculum_record.get("coverage_state") != COMPLETE
    ):
        blockers.append(
            {
                "code": MANUAL_REVIEW,
                "dimension": "primary_curriculum_coverage",
                "reason": "原主修課表 course catalog 未達 COMPLETE，不得產生逐課 PASS。",
                "curriculum_id": primary_curriculum_record.get("curriculum_id"),
            }
        )
    if target_curriculum is not None and target_curriculum.get("coverage_state") != COMPLETE:
        blockers.append(
            {
                "code": MANUAL_REVIEW,
                "dimension": "target_curriculum_coverage",
                "reason": "目標課表 course catalog 未達 COMPLETE，不得產生逐課 PASS。",
                "curriculum_id": target_curriculum.get("curriculum_id"),
            }
        )
    unique_codes: list[str] = []
    for blocker in blockers:
        if blocker["code"] not in unique_codes:
            unique_codes.append(blocker["code"])
    if CONFLICTED in unique_codes:
        status = CONFLICTED
    elif MISSING in unique_codes:
        status = MISSING
    elif MANUAL_REVIEW in unique_codes:
        status = MANUAL_REVIEW
    else:
        status = RESOLVED
    primary = dimensions.get("primary_curriculum", {})
    target = dimensions.get("target_curriculum_version", {})
    can_pass = (
        status == RESOLVED
        and primary.get("status") == RESOLVED
        and target.get("status") in {RESOLVED, NOT_APPLICABLE}
        and (not primary.get("coverage_state") or primary.get("coverage_state") == COMPLETE)
        and (not target.get("coverage_state") or target.get("coverage_state") == COMPLETE)
    )
    return {
        "status": status,
        "state": status,
        "resolved": can_pass,
        "can_pass": can_pass,
        "blocker": unique_codes[0] if unique_codes else None,
        "blockers": blockers,
        "blocker_codes": unique_codes,
        "warnings": warnings,
        "request": _request_snapshot(request),
        "secondary_kind": secondary_kind,
        "target_role": target_kind if is_double or is_minor else "none",
        "dimensions": dimensions,
        "primary_curriculum": deepcopy(primary.get("curriculum")) if primary.get("curriculum") else None,
        "target_curriculum": deepcopy(target.get("curriculum")) if target.get("curriculum") else None,
        "primary_curriculum_id": primary.get("curriculum_id"),
        "target_curriculum_id": target.get("curriculum_id"),
        "admission_cohort": cohort,
        "application_term": application_term,
        "target_program": _PROGRAM_DISPLAY.get(target_program) if target_program else None,
        "target_track": _TRACK_DISPLAY.get(target_track) if target_track else None,
    }


__all__ = [
    "COMPLETE",
    "CONFLICTED",
    "COVERAGE_NONE",
    "MANUAL_REVIEW",
    "MISSING",
    "NOT_APPLICABLE",
    "PARTIAL",
    "RESOLVED",
    "VERIFIED",
    "get_curriculum",
    "list_curriculum_ids",
    "resolve_rule_context",
]
