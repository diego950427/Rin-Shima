"""Versioned Science College handbook rules.

The data lives in ``rules_config.json``.  Matching code deliberately keeps
course identity separate by handbook year and department scope; a similar
name is never treated as an equivalence by itself.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import warnings
from copy import deepcopy

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules_config.json")
ADMISSION_COHORTS = ["111", "112", "113", "114", "115"]
COURSE_POOL_SCHEMA = "pool:v1"
COURSE_POOL_IDENTITY_FIELDS = (
    "curriculum_version",
    "program_slug",
    "track_slug",
    "raw_title",
    "credits",
    "lecture_or_lab",
)

# The JSON contains source-scoped records for all supported admission cohorts.
# A record can still be PARTIAL: that describes an intentionally incomplete
# transcription and never licenses a neighbouring-year fallback or PASS.
_PLANNING_COHORTS = {"111", "115"}
_LEGACY_SOURCE_NAMES = {
    "112": "3-理學院 (112).pdf",
    "113": "3-理學院 (113).pdf",
    "114": "3-理學院 (114).pdf",
}
_EVIDENCE_SOURCE_NAMES = {
    "111": "3-理學院.pdf",
    "112": "3-理學院 (112).pdf",
    "113": "3-理學院 (113).pdf",
    "114": "3-理學院 (114).pdf",
    "115": "3-理學院 (115).pdf",
}
_HANDBOOK_URLS = {
    "111": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MemN4TDNCMFlWODVNREkyTWw4eE56STBPREZmT0RBNU5qY3VjR1Jt&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "112": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MelV5TDNCMFlWOHhNRFF3T1RKZk16YzNORGc0TVY4ek16ZzJOaTV3WkdZPQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "113": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MemcxTDNCMFlWOHhNell5TmpoZk16TXhNREE0TVY4ek9UY3lNUzV3WkdZPQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "114": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9MemN5TDNCMFlWOHhNemN4TWpSZk5UQTVNalkzTWw4ek9UY3lNUzV3WkdZPQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
    "115": "https://curr.utaipei.edu.tw/app/index.php?Action=downloadfile&file=WVhSMFlXTm9Memt3TDNCMFlWOHhOelExTnpKZk1qTXlNak0yWHpjNE5qSXhMbkJrWmc9PQ==&fname=0054YSGHRK10PPXXTSZSTSYW14PKJDKKQO343510LP25LKQPZWROYSA43454QOUSSSPOCDYTVSPKDHDH&cg=5",
}


# APC double-major target requirements are represented independently from the
# legacy aggregate ``basic_core``/``other_req`` values.  The IDs are generated
# from the selected admission cohort and target track, so a decision recorded
# for one handbook can never silently be applied to another handbook.
_APC_SPLIT_BASE = (
    ("普通物理學(一)", 3.0, "physics_1"),
    ("普通物理實驗(一)", 1.0, "physics_lab_1"),
    ("普通化學(一)", 3.0, "chemistry_1"),
    ("普通化學實驗(一)", 1.0, "chemistry_lab_1"),
    ("普通物理學(二)", 3.0, "physics_2"),
    ("普通物理實驗(二)", 1.0, "physics_lab_2"),
    ("普通化學(二)", 3.0, "chemistry_2"),
    ("普通化學實驗(二)", 1.0, "chemistry_lab_2"),
)

_APC_115_BASE = {
    "物理組": (
        ("普通物理學(一)", 3.0, "physics_1"),
        ("普通化學(一)", 3.0, "chemistry_1"),
        ("普通物理實驗(一)", 1.0, "physics_lab_1"),
        ("微積分(一)", 3.0, "calculus_1"),
        ("普通物理學(二)", 3.0, "physics_2"),
        ("普通化學(二)", 3.0, "chemistry_2"),
        ("普通物理實驗(二)", 1.0, "physics_lab_2"),
        ("微積分(二)", 3.0, "calculus_2"),
    ),
    "化學組": (
        ("普通物理學(一)", 3.0, "physics_1"),
        ("普通化學(一)", 3.0, "chemistry_1"),
        ("普通化學實驗(一)", 1.0, "chemistry_lab_1"),
        ("微積分(一)", 3.0, "calculus_1"),
        ("普通物理學(二)", 3.0, "physics_2"),
        ("普通化學(二)", 3.0, "chemistry_2"),
        ("普通化學實驗(二)", 1.0, "chemistry_lab_2"),
        ("微積分(二)", 3.0, "calculus_2"),
    ),
}

# These are the 115 handbook's track-specific "other required" catalogues.
# They are intentionally catalogues, not aliases: a source course still needs
# an exact title/credit match or an auditable department-approved decision.
_APC_115_OTHER_CATALOGS = {
    "物理組": {
        "應用科學專題(一)": 1,
        "應用科學專題(二)": 1,
        "物理數學(一)": 3,
        "電磁學(一)": 3,
        "電磁學實驗": 1,
        "力學(一)": 2,
        "電子學(一)": 3,
        "電子學(二)": 3,
        "電子學實驗(一)": 1,
        "物理數學(二)": 3,
        "電磁學(二)": 3,
        "光學": 3,
        "光學實驗": 1,
        "半導體物理": 3,
        "光電子學": 3,
        "近代物理": 3,
        "近代物理實驗": 1,
        "電子學實驗(二)": 1,
        "固態物理(一)": 3,
        "固態物理(二)": 3,
    },
    "化學組": {
        "應用科學專題(一)": 1,
        "應用科學專題(二)": 1,
        "普通化學實驗(一)": 1,
        "普通化學實驗(二)": 1,
        "分析化學(一)": 3,
        "有機化學 (一)": 3,
        "有機化學實驗 (一)": 1,
        "化學數學(一)": 2,
        "物理化學 (一)": 3,
        "物理化學實驗 (一)": 1,
        "有機化學 (二)": 3,
        "有機化學實驗 (二)": 1,
        "物理化學 (二)": 3,
        "物理化學實驗 (二)": 1,
        "材料科學": 3,
        "無機化學(一)": 3,
        "儀器分析(一)": 3,
        "物理化學(三)": 3,
        "生物化學": 3,
        "無機化學(二)": 3,
        "儀器分析實驗": 1,
    },
}


def _load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict) or not isinstance(config.get("handbooks"), dict):
            raise ValueError("規則檔必須包含 handbooks 物件。")
        if not config["handbooks"]:
            raise ValueError("規則檔至少要包含一個學生手冊版本。")
        return config
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        warnings.warn(f"[學生手冊規則載入失敗] {exc}")
        return {"_meta": {"default_academic_year": "114"}, "shared": {}, "handbooks": {}}


_CFG = _load_config()


def _deep_merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def get_available_handbook_years(include_planning=False):
    """Return detailed handbook years, newest first.

    The default preserves the original API (detailed course tables only).
    ``include_planning=True`` exposes all selectable admission cohorts,
    including years whose verified threshold plan is intentionally
    review-gated rather than a guessed course catalogue.
    """

    configured = sorted((str(year) for year in _CFG.get("handbooks", {})), key=int, reverse=True)
    if include_planning:
        return sorted(set(configured).union(ADMISSION_COHORTS), key=int, reverse=True)
    return configured


def get_available_admission_cohorts():
    """Return all supported admission cohorts in chronological order."""

    return list(ADMISSION_COHORTS)


get_admission_cohort_options = get_available_admission_cohorts
get_supported_cohorts = get_available_admission_cohorts


def get_default_handbook_year():
    configured = str(_CFG.get("_meta", {}).get("default_academic_year", "113"))
    years = get_available_handbook_years()
    if configured in years:
        return configured
    return years[0] if years else configured


def normalize_handbook_year(year=None):
    selected = str(year or get_default_handbook_year()).strip()
    selected = selected.replace("學年度", "").strip()
    if selected not in get_available_handbook_years(include_planning=True):
        raise ValueError(f"不支援的學生手冊年度：{selected}")
    return selected


def _make_planning_handbook(selected):
    """Build a threshold-only compatibility record for 111/115.

    The old implementation copied the nearest configured handbook, which
    silently applied 112 rules to 111 and 114 rules to 115.  Keep the legacy
    shape so callers can still read aggregate thresholds, but deliberately
    leave every course catalogue empty.  The registry and evaluator therefore
    expose ``coverage_state=NONE``/``PARTIAL`` and cannot produce a PASS until
    the cohort-specific source tables are transcribed.
    """

    if selected not in _PLANNING_COHORTS:
        return {}
    if selected == "111":
        domain_elective = 22
        other_elective = 13
    else:
        domain_elective = 20
        other_elective = 27
    handbook = {
        "_meta": {
            "version": selected,
            "academic_year": selected,
            "college": "理學院",
            "source_file": _EVIDENCE_SOURCE_NAMES[selected],
            "evidence_file": _EVIDENCE_SOURCE_NAMES[selected],
            "total_graduation_credits": 128,
            "coverage_state": "NONE",
            "course_catalog_coverage": "NONE",
            "document_warnings": [
                f"{selected} 學年度僅建立官方 aggregate 門檻；未以相鄰年度課程表填入逐課清單，結果需人工複核。"
            ],
        },
        "earth_life_major": {
            "total_req": 85,
            "common_compulsory": {"total_req": 24, "courses": {}},
            "common_alternatives": [],
            "domains": {
                "domain_req": 14,
                "domain_elective_req": domain_elective,
                "other_elective_req": other_elective,
                "地球環境": {"compulsory": {}, "electives": {}},
                "生命科學": {"compulsory": {}, "electives": {}},
            },
            "common_electives": {},
        },
        "apc_rules": {
            "basic_core": {},
            "shared_other_required": {},
            "divisions": {"物理組": {"compulsory": {}}, "化學組": {"compulsory": {}}},
            "minor": {"basic_req": 16, "other_req": 4, "total_req": 20},
            "double_major": {"basic_req": 16, "other_req": 24, "total_req": 40},
        },
        "cs_rules": {
            "department_courses": {},
            "minor": {"compulsory": {}, "compulsory_req": 6, "other_req": 14, "total_req": 20},
            "double_major": {"compulsory": {}, "compulsory_req": 15, "other_req": 25, "total_req": 40},
        },
        "academic_year": selected,
    }
    if selected == "115":
        handbook["apc_rules"] = {
            **handbook["apc_rules"],
            "minor": {"basic_req": 20, "other_req": 0, "total_req": 20},
            "double_major": {"basic_req": 20, "other_req": 20, "total_req": 40},
        }
    return handbook


def get_handbook_config(year=None):
    selected = normalize_handbook_year(year)
    shared = _CFG.get("shared", {})
    version = _CFG.get("handbooks", {}).get(selected) or _make_planning_handbook(selected)
    if not version:
        raise ValueError(f"找不到 {selected} 學年度學生手冊規則。")
    merged = _deep_merge(shared, version)
    merged["academic_year"] = selected
    meta = merged.setdefault("_meta", {})
    # Keep an auditable, stable metadata contract even for older configured
    # records that predate the source-evidence matrix.  These defaults never
    # upgrade coverage; they only expose the known official source and make
    # the absence of automation explicit.
    meta.setdefault("version", selected)
    meta.setdefault("academic_year", selected)
    meta.setdefault("source_file", _EVIDENCE_SOURCE_NAMES.get(selected, "未標示"))
    meta.setdefault("source_url", _HANDBOOK_URLS.get(selected, ""))
    meta.setdefault("evidence_state", "VERIFIED" if selected in _CFG.get("handbooks", {}) else "MISSING")
    meta.setdefault("verification_status", meta.get("evidence_state", "MISSING"))
    meta.setdefault("coverage_state", "PARTIAL")
    meta.setdefault("automation_sufficiency", "PARTIAL")
    meta.setdefault("manual_review_reason", "逐課資料或上位規章不足時必須人工確認。")
    return merged


def get_rules_meta(year=None):
    selected = normalize_handbook_year(year)
    handbook = get_handbook_config(selected)
    meta = deepcopy(handbook.get("_meta", {}))
    meta.setdefault("version", selected)
    meta.setdefault("academic_year", selected)
    meta.setdefault("last_updated", _CFG.get("_meta", {}).get("last_updated", "N/A"))
    meta["evidence_file"] = _EVIDENCE_SOURCE_NAMES.get(selected, meta.get("source_file", "未標示"))
    meta.setdefault("source_url", _HANDBOOK_URLS.get(selected, ""))
    # Keep source_file stable for legacy callers while exposing the actual
    # local PDF filename used for new citations.
    if selected in _LEGACY_SOURCE_NAMES:
        meta["source_file"] = _LEGACY_SOURCE_NAMES[selected]
    meta["available_years"] = get_available_handbook_years()
    meta["available_cohorts"] = get_available_admission_cohorts()
    return meta


def get_primary_requirements(cohort, primary_program, track=None):
    """Lazy wrapper for the cohort-aware policy plan API."""

    from policy_audit import get_primary_requirements as _get_primary_requirements

    return _get_primary_requirements(cohort, primary_program, track)


def get_double_major_structure(cohort, target_program, track=None):
    """Lazy wrapper for verified double-major structure facts."""

    from policy_audit import get_double_structure

    return get_double_structure(cohort, target_program, track)


get_cohort_requirements = get_primary_requirements
get_primary_plan = get_primary_requirements


def get_rule_sets(year=None):
    """Build the engine-facing rule dictionaries for one handbook year."""
    handbook = get_handbook_config(year)
    common = handbook["university_common"]
    earth = handbook["earth_life_major"]
    domains = earth["domains"]

    university_common = {
        "compulsory": deepcopy(common["compulsory"]["courses"]),
        "category_domains": deepcopy(common["ge_categories"]["categories"]),
        "per_category_req": float(common["ge_categories"].get("per_category_req", 4)),
    }
    earth_life_major = {
        "common_compulsory": deepcopy(earth["common_compulsory"]["courses"]),
        "common_alternatives": deepcopy(earth.get("common_alternatives", [])),
        "domains": {
            name: deepcopy(data["compulsory"])
            for name, data in domains.items()
            if isinstance(data, dict) and "compulsory" in data
        },
        "domain_electives": {
            name: deepcopy(data.get("electives", {}))
            for name, data in domains.items()
            if isinstance(data, dict) and "compulsory" in data
        },
    }
    earth_life_major["domain_electives"]["common_electives"] = deepcopy(earth.get("common_electives", {}))

    apc_target_requirements = {}
    for target_track in ("物理組", "化學組"):
        for target_type in ("雙主修", "輔系"):
            apc_target_requirements[f"{target_track}:{target_type}"] = get_apc_target_requirements(
                handbook["academic_year"], target_track, target_type
            )
    return {
        "academic_year": handbook["academic_year"],
        "handbook_meta": deepcopy(handbook.get("_meta", {})),
        "course_pool_schema": {
            "version": COURSE_POOL_SCHEMA,
            "identity_fields": COURSE_POOL_IDENTITY_FIELDS,
            "named_requirement_field": "pool_ids",
            "quota_requirement_field": "eligible_pool_ids",
            "candidate_row_requirement_type": "candidate_course",
            "allow_user_claimed_department": False,
        },
        "university_common": university_common,
        "earth_life_major": earth_life_major,
        "apc_rules": deepcopy(handbook["apc_rules"]),
        "cs_rules": deepcopy(handbook["cs_rules"]),
        "math_rules": deepcopy(handbook.get("math_rules", {})),
        "rule_evidence": {
            "earth_life": deepcopy(earth.get("evidence", {})),
            "apc": deepcopy(handbook.get("apc_rules", {}).get("evidence", {})),
            "math": deepcopy(handbook.get("math_rules", {}).get("evidence", {})),
            "cs": deepcopy(handbook.get("cs_rules", {}).get("evidence", {})),
        },
        "course_aliases": deepcopy(handbook.get("course_aliases", {})),
        "document_warnings": deepcopy(handbook.get("_meta", {}).get("document_warnings", [])),
        "apc_target_requirements": apc_target_requirements,
    }


def _apc_target_slug(value):
    """Return a stable, ASCII-like component for an APC target ID."""

    mapping = {
        "物理組": "physics",
        "電子物理": "physics",
        "化學組": "chemistry",
        "應用化學": "chemistry",
        "普通物理學(一)": "physics_1",
        "普通物理學(二)": "physics_2",
        "普通物理實驗(一)": "physics_lab_1",
        "普通物理實驗(二)": "physics_lab_2",
        "普通化學(一)": "chemistry_1",
        "普通化學(二)": "chemistry_2",
        "普通化學實驗(一)": "chemistry_lab_1",
        "普通化學實驗(二)": "chemistry_lab_2",
        "微積分(一)": "calculus_1",
        "微積分(二)": "calculus_2",
        "其餘必修課程": "other_required",
    }
    if value in mapping:
        return mapping[value]
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "").strip()).strip("_").lower()
    return text or "requirement"


def apc_target_requirement_id(cohort, track, requirement_name, program_type="雙主修"):
    """Build the stable ID used by the source-attempt binding workflow."""

    selected = normalize_handbook_year(cohort)
    track_slug = _apc_target_slug(track)
    program_slug = "dm" if program_type == "雙主修" else "minor" if program_type == "輔系" else _apc_target_slug(program_type)
    requirement_slug = _apc_target_slug(requirement_name)
    return f"apc.{program_slug}.{selected}.{track_slug}.{requirement_slug}"


def _apc_target_source(selected, selected_track, program_type):
    """Return the source contract for an APC target table.

    The handbook page is evidence for the named rows and/or aggregate, not
    proof that a particular student's target curriculum has been approved.
    Keep the page, research matrix, and unresolved semantic gap attached to
    the returned target plan so callers cannot mistake a bare name/credit map
    for an executable equivalency rule.
    """

    physics_pages = {
        "111": (10, 9),
        "112": (10, 9),
        "113": (11, 10),
        "114": ("11–12", "10–11"),
        "115": ("12–13", "11–12"),
    }
    chemistry_pages = {
        "111": (19, 18),
        "112": (19, 18),
        "113": (24, 23),
        "114": (24, 23),
        "115": ("24–25", "23–24"),
    }
    pdf_page, printed_page = (
        physics_pages if selected_track == "物理組" else chemistry_pages
    )[selected]
    if program_type == "輔系":
        research_file = "research/minor_program_matrix_111_115.md"
        source_reference = f"{research_file}#5.1 APC {selected} {'physics' if selected_track == '物理組' else 'chemistry'}"
        table_location = f"{selected} 學年度物化系{selected_track}輔系課程表"
    else:
        research_file = "research/apc_cs_handbook_matrix_111_115.md"
        source_reference = f"{research_file}#apc-{selected}"
        table_location = f"{selected} 學年度物化系{selected_track}雙主修課程表"
    url = _HANDBOOK_URLS[selected]
    return {
        "research_file": research_file,
        "source_reference": source_reference,
        "source_url": url,
        "source_file": _EVIDENCE_SOURCE_NAMES[selected],
        "pdf_page": pdf_page,
        "printed_page": printed_page,
        "pages": f"PDF p.{pdf_page}（印刷 p.{printed_page}）",
        "table_location": table_location,
    }


def _apc_target_course_pools(
    selected,
    selected_track,
    program_type,
    source,
    base_rows,
    candidate_catalogue,
    other_required,
    evidence,
    coverage,
    warnings,
):
    """Build target pools while keeping candidate rows out of requirements."""

    target_scope = "double_major" if program_type == "雙主修" else "minor"
    track_slug = _apc_target_slug(selected_track)
    base_pool_id = f"pool:{target_scope}:{selected}:apc:{track_slug}:base"
    other_pool_id = f"pool:{target_scope}:{selected}:apc:{track_slug}:other_required"

    def candidate_entry(pool_id, name, credits):
        component = "lab" if "實驗" in str(name) else "lecture"
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(name)).strip("_").lower() or "course"
        return {
            "name": str(name),
            "raw_title": str(name),
            "credits": float(credits),
            "component": component,
            "component_type": component,
            "lecture_or_lab": component,
            "curriculum_version": selected,
            "program_slug": "apc",
            "track_slug": track_slug,
            "official_course_identity": f"{selected}:apc:{track_slug}:{slug}",
            "pool_ids": (pool_id,),
            "candidate_only": True,
            "requirement_type": "candidate_course",
            "evidence_state": "VERIFIED" if evidence != "MISSING" else "MISSING",
            "source_reference": f"{source['source_reference']}:pool:{pool_id.rsplit(':', 1)[-1]}:course:{slug}",
        }

    base_candidates = tuple(candidate_entry(base_pool_id, name, credits) for name, credits, _ in base_rows)
    other_candidates = tuple(candidate_entry(other_pool_id, name, credits) for name, credits in candidate_catalogue.items())

    def pool_record(pool_id, label, bucket, required_credits, selection_rule, candidates, clause, policy, pool_coverage):
        return {
            "id": pool_id,
            "pool_id": pool_id,
            "schema": COURSE_POOL_SCHEMA,
            "scope": target_scope,
            "kind": "double_major_target" if target_scope == "double_major" else "minor_target",
            "curriculum_version": selected,
            "program_slug": "apc",
            "track_slug": track_slug,
            "label": label,
            "bucket": bucket,
            "required_credits": float(required_credits),
            "candidate_only": True,
            "eligible_pool_ids": (pool_id,),
            "overflow_routes": (),
            "selection_rule": selection_rule,
            "identity_fields": COURSE_POOL_IDENTITY_FIELDS,
            "allow_user_claimed_department": False,
            "allowed_components": ("lecture", "lab"),
            "evidence_state": evidence,
            "verification_status": evidence,
            "coverage_state": pool_coverage,
            "automation_sufficiency": "COMPLETE" if pool_coverage == "COMPLETE" else "PARTIAL",
            "manual_reason": "; ".join(warnings),
            "source_reference": f"{source['source_reference']}:pool:{bucket}",
            "source_url": source["source_url"],
            "source_file": source["source_file"],
            "research_file": source["research_file"],
            "pdf_page": source["pdf_page"],
            "printed_page": source["printed_page"],
            "pages": source["pages"],
            "table_location": source["table_location"],
            "original_clause": clause,
            "policy": policy,
            "candidate_courses": candidates,
            "courses": candidates,
        }

    return {
        base_pool_id: pool_record(
            base_pool_id,
            f"{selected_track}基礎列項",
            "base",
            sum(float(credits) for _, credits, _ in base_rows),
            "exact_title_credit_component",
            base_candidates,
            f"{source['table_location']}：基礎列項。",
            {"exact_title_credit_component": True},
            coverage,
        ),
        other_pool_id: pool_record(
            other_pool_id,
            f"{selected_track}其餘必修課程池",
            "other_required",
            other_required,
            "manual_department_approval",
            other_candidates,
            f"{source['table_location']}：其餘必修課程 {float(other_required):g} 學分。",
            {"requires_department_approval": True, "candidate_only": True},
            "COMPLETE" if not other_required else "PARTIAL",
        ),
    }


def get_apc_target_requirements(cohort, track="化學組", program_type="雙主修"):
    """Return auditable APC target rows for one cohort and track.

    The legacy engine's aggregate quota fields remain available for backward
    compatibility.  This API is the source of truth for new course-level
    decisions: every course row and the remaining named/generic quota have a
    cohort-scoped ID, while the catalogue stays exact-title/exact-credit.
    """

    selected = normalize_handbook_year(cohort)
    track_text = str(track or "化學組").strip()
    if track_text in {"電子物理", "物理", "物理組", "物化系物理組"}:
        selected_track = "物理組"
    elif track_text in {"應用化學", "化學", "化學組", "物化系化學組"}:
        selected_track = "化學組"
    else:
        raise ValueError(f"不支援的物化系組別：{track}")
    if program_type not in {"雙主修", "輔系"}:
        raise ValueError(f"不支援的物化系修讀身分：{program_type}")

    handbook = get_handbook_config(selected)
    apc = handbook.get("apc_rules", {})
    program_key = "double_major" if program_type == "雙主修" else "minor"
    program_rules = apc.get(program_key, {})
    source = _apc_target_source(selected, selected_track, program_type)
    target_scope = "double_major" if program_type == "雙主修" else "minor"
    track_slug = _apc_target_slug(selected_track)
    base_pool_id = f"pool:{target_scope}:{selected}:apc:{track_slug}:base"
    other_pool_id = f"pool:{target_scope}:{selected}:apc:{track_slug}:other_required"
    if selected == "115":
        base_rows = _APC_115_BASE[selected_track]
        # The 115 handbook verifies the aggregate "other required" amount,
        # but the target page does not provide a complete named pool in the
        # checked-in evidence.  Keep possible primary-track titles separate
        # as candidates; never feed them to the legacy allocator as if they
        # were target-approved requirements.
        candidate_catalogue = _APC_115_OTHER_CATALOGS[selected_track]
        # The 115 handbook says the named base list is 20 credits for both
        # minor/double-major tables; only the double-major table adds the
        # remaining 20-credit other-required quota.
        other_required = 20.0 if program_type == "雙主修" else 0.0
        evidence = "VERIFIED"
        coverage = "COMPLETE" if program_type == "輔系" else "PARTIAL"
        warnings = (
            []
            if program_type == "輔系"
            else ["115 雙主修其餘必修 20 學分的完整命名課程池與系所核准條件尚未由目標頁逐課核對。"]
        )
    elif selected != "115" and program_type == "雙主修" and selected_track == "物理組":
        # The 111–114 electronic-physics target pages are legible enough for
        # an aggregate heading, but not for a reliable course identity.  The
        # minor matrix has a separate eight-row transcription; it must not be
        # reused for a double-major target.  Keep the executable course list
        # empty and expose only a manual-review quota.
        base_rows = ()
        candidate_catalogue = dict(apc.get("shared_other_required", {}))
        candidate_catalogue.update(apc.get("divisions", {}).get(selected_track, {}).get("compulsory", {}))
        other_required = float(program_rules.get("other_req", 24.0) or 24.0)
        evidence = "MISSING"
        coverage = "PARTIAL"
        warnings = [
            "111–114 電子物理組雙主修頁的舊式掃描不足以安全建立課程 identity；不得以輔系列項或其他年度課名補猜。"
        ]
    else:
        base_rows = _APC_SPLIT_BASE
        # 111–114 pages verify the fixed eight-row amount.  The 111/112
        # chemistry footer also verifies an additional 24-credit target
        # requirement; its named pool is still incomplete.  Do not expose a
        # primary-track catalogue as an automatically consumable target pool.
        candidate_catalogue = dict(apc.get("shared_other_required", {}))
        candidate_catalogue.update(apc.get("divisions", {}).get(selected_track, {}).get("compulsory", {}))
        other_required = float(program_rules.get("other_req", 0.0) or 0.0)
        coverage = "PARTIAL"
        if program_type == "雙主修" and selected_track == "化學組" and selected in {"111", "112"}:
            evidence = "VERIFIED"
            warnings = ["111／112 應用化學雙主修表已核對可見16學分＋額外目標必修24學分＝40；24學分的正式目標課程池與核准 mapping 尚未完整轉錄。"]
        else:
            evidence = "VERIFIED"
            warnings = ["111–114 物化目標表的其餘必修課名／選擇條件未完整核對，固定列項可供規劃預覽但整體需人工確認。"]

    requirements = []
    for name, credits, slug in base_rows:
        component = "lab" if "實驗" in name else "lecture"
        original_clause = f"{source['table_location']}：{name} {float(credits):g} 學分。"
        requirement_id = apc_target_requirement_id(selected, selected_track, name, program_type)
        requirements.append(
            {
                "id": requirement_id,
                "requirement_id": requirement_id,
                "name": name,
                "credits": float(credits),
                "bucket": "base",
                "kind": "course",
                "requirement_type": "named_course",
                "candidate_only": False,
                "pool_requirement": False,
                "pool_ids": (base_pool_id,),
                "eligible_pool_ids": (),
                "overflow_routes": (),
                "evidence": evidence,
                "evidence_state": "VERIFIED" if evidence != "CONFLICTED" else "VERIFIED",
                "verification_status": "VERIFIED" if evidence != "CONFLICTED" else "VERIFIED",
                "coverage_state": coverage,
                "automation_sufficiency": "COMPLETE" if coverage == "COMPLETE" else "PARTIAL",
                "automatic_decision": coverage == "COMPLETE" and not warnings,
                "manual_reason": "; ".join(warnings),
                "manual_review_reason": "; ".join(warnings),
                "component": component,
                "component_type": component,
                "component_label": "實驗" if component == "lab" else "講授",
                "lecture_or_lab": component,
                "is_lab": component == "lab",
                "allow_combined_lab_source": False,
                "research_file": source["research_file"],
                "source_reference": f"{source['source_reference']}:row:{slug}",
                "source_url": source["source_url"],
                "source_file": source["source_file"],
                "pdf_page": source["pdf_page"],
                "printed_page": source["printed_page"],
                "pages": source["pages"],
                "table_location": source["table_location"],
                "original_clause": original_clause,
                "original_text": original_clause,
                "provenance": {
                    **source,
                    "assertion_id": requirement_id,
                    "raw_title": name,
                    "original_clause": original_clause,
                    "evidence_state": "VERIFIED" if evidence != "CONFLICTED" else "VERIFIED",
                    "verification_status": "VERIFIED" if evidence != "CONFLICTED" else "VERIFIED",
                    "coverage_state": coverage,
                    "automation_sufficiency": "COMPLETE" if coverage == "COMPLETE" else "PARTIAL",
                    "automatic_decision": coverage == "COMPLETE" and not warnings,
                    "manual_reason": "; ".join(warnings),
                    "manual_review_reason": "; ".join(warnings),
                },
            }
        )
    quota_name = f"{selected_track}其餘必修課程"
    quota_id = apc_target_requirement_id(selected, selected_track, "其餘必修課程", program_type)
    quota_clause = f"{source['table_location']}：其餘必修課程 {float(other_required):g} 學分。"
    requirements.append(
        {
            "id": quota_id,
            "requirement_id": quota_id,
            "name": quota_name,
            "credits": float(other_required),
            "bucket": "other_required",
            "kind": "quota",
            "requirement_type": "credit_quota",
            "candidate_only": False,
            "pool_requirement": True,
            "pool_ids": (),
            "eligible_pool_ids": (other_pool_id,),
            "overflow_routes": (),
            "evidence": evidence,
            "evidence_state": evidence,
            "verification_status": evidence,
            "coverage_state": coverage,
            "automation_sufficiency": "COMPLETE" if coverage == "COMPLETE" and not other_required else "PARTIAL",
            "automatic_decision": False,
            "manual_reason": "; ".join(warnings) or "目標頁未提供足以自動配置的命名課程池。",
            "manual_review_reason": "; ".join(warnings) or "目標頁未提供足以自動配置的命名課程池。",
            "component": "quota",
            "component_type": "quota",
            "lecture_or_lab": "quota",
            "is_lab": False,
            "allow_combined_lab_source": False,
            "catalog": [],
            "candidate_catalog": [
                {"name": str(name), "credits": float(credits)} for name, credits in candidate_catalogue.items()
            ],
            "named_course_pool_state": "COMPLETE" if not other_required else "MISSING",
            "research_file": source["research_file"],
            "source_reference": f"{source['source_reference']}:footer:other_required",
            "source_url": source["source_url"],
            "source_file": source["source_file"],
            "pdf_page": source["pdf_page"],
            "printed_page": source["printed_page"],
            "pages": source["pages"],
            "table_location": source["table_location"],
            "original_clause": quota_clause,
            "original_text": quota_clause,
            "provenance": {
                **source,
                "assertion_id": quota_id,
                "raw_title": quota_name,
                "original_clause": quota_clause,
                "evidence_state": evidence,
                "verification_status": evidence,
                "coverage_state": coverage,
                "automation_sufficiency": "COMPLETE" if coverage == "COMPLETE" and not other_required else "PARTIAL",
                "automatic_decision": False,
                "manual_reason": "; ".join(warnings) or "目標頁未提供足以自動配置的命名課程池。",
                "manual_review_reason": "; ".join(warnings) or "目標頁未提供足以自動配置的命名課程池。",
            },
        }
    )
    course_pools = _apc_target_course_pools(
        selected,
        selected_track,
        program_type,
        source,
        base_rows,
        candidate_catalogue,
        other_required,
        evidence,
        coverage,
        warnings,
    )
    return {
        "cohort": selected,
        "program": "物化",
        "track": selected_track,
        "program_type": program_type,
        "total_required": float(program_rules.get("total_req", 40.0 if program_type == "雙主修" else 20.0)),
        "base_required": sum(row["credits"] for row in requirements if row["bucket"] == "base"),
        "other_required": float(other_required),
        "requirements": requirements,
        "course_pool_schema": {
            "version": COURSE_POOL_SCHEMA,
            "identity_fields": COURSE_POOL_IDENTITY_FIELDS,
            "named_requirement_field": "pool_ids",
            "quota_requirement_field": "eligible_pool_ids",
            "candidate_row_requirement_type": "candidate_course",
            "allow_user_claimed_department": False,
        },
        "course_pools": course_pools,
        "pools": deepcopy(course_pools),
        "course_requirements": {
            row["name"]: row["credits"] for row in requirements if row["kind"] == "course"
        },
        # The empty executable pool is intentional.  Candidate titles are
        # exposed separately so a UI can explain possible choices without
        # allowing a primary-track course to satisfy an unresolved target
        # quota automatically.
        "other_required_catalog": {},
        "other_required_candidates": candidate_catalogue,
        "named_course_pool_state": "COMPLETE" if not other_required else "MISSING",
        "evidence": evidence,
        "evidence_state": evidence,
        "verification_status": evidence,
        "coverage_state": coverage,
        "automation_sufficiency": "COMPLETE" if coverage == "COMPLETE" else "PARTIAL",
        "automatic_decision": False,
        "source": source,
        "source_reference": source["source_reference"],
        "source_url": source["source_url"],
        "source_file": source["source_file"],
        "pdf_page": source["pdf_page"],
        "printed_page": source["printed_page"],
        "pages": source["pages"],
        "table_location": source["table_location"],
        "manual_review_reason": "; ".join(warnings),
        "warnings": warnings,
    }


get_target_requirements = get_apc_target_requirements
get_apc_requirement_catalog = get_apc_target_requirements


def _legacy_policy_scope(target_dept):
    """Infer the narrow legacy policy scope from a displayed department name.

    ``get_credit_requirements`` predates the registry's scoped policy contract
    and only receives a free-form target department.  Keep its numeric return
    shape, but select the matching year/program/track contract so Math's cap
    and the other departments' Science College subset are not silently
    flattened into one shared default.
    """

    text = str(target_dept or "")
    if "資科" in text or "資訊科學" in text:
        return "cs", "department"
    if "數學" in text:
        return "math", "department"
    if "應物" in text or "物化" in text or "應用物理" in text:
        return "apc", "chemistry" if "化學" in text else "physics"
    if "生命" in text or "生物" in text:
        return "earth", "life_science"
    if "地球" in text or "環境" in text:
        return "earth", "earth_environment"
    return "earth", "department"


def _legacy_policy_contract(handbook, policy_key, program, track):
    """Return one year/program/track policy contract for legacy callers."""

    policy = handbook.get(policy_key, {})
    if not isinstance(policy, dict):
        return {}
    evidence = policy.get("evidence_by_cohort", {})
    if policy_key != "free_elective" or not isinstance(evidence, dict):
        return policy
    cohort = str(handbook.get("academic_year") or "")
    cohort_contract = evidence.get(cohort, {})
    if not isinstance(cohort_contract, dict):
        return policy
    program_contract = cohort_contract.get(program, {})
    if not isinstance(program_contract, dict):
        return policy
    selected = {
        key: deepcopy(value)
        for key, value in program_contract.items()
        if key != "tracks"
    }
    tracks = program_contract.get("tracks", {})
    if isinstance(tracks, dict):
        track_contract = tracks.get(track) or tracks.get("default", {})
        if isinstance(track_contract, dict):
            selected.update(deepcopy(track_contract))
    return {**policy, **selected}


def get_credit_requirements(program="單主修", target_dept="", handbook_year=None):
    handbook = get_handbook_config(handbook_year)
    meta = handbook.get("_meta", {})
    common = handbook["university_common"]
    ge = common["ge_categories"]
    major = handbook["earth_life_major"]
    domains = major["domains"]
    policy_program, policy_track = _legacy_policy_scope(target_dept)
    free_policy = _legacy_policy_contract(
        handbook,
        "free_elective",
        policy_program,
        policy_track,
    )
    physical_policy = handbook.get("physical_education", {})
    physical_policy = physical_policy if isinstance(physical_policy, dict) else {}
    physical_contract = physical_policy.get("evidence_by_cohort", {}).get(
        str(handbook.get("academic_year") or ""),
        {},
    )
    physical_contract = physical_contract if isinstance(physical_contract, dict) else {}
    requirements = {
        "total": float(meta.get("total_graduation_credits", 128)),
        "common_total": float(common.get("total_req", 28)),
        "common_compulsory": float(common["compulsory"].get("total_req", 10)),
        "ge_categories_total": float(ge.get("total_req", 16)),
        "ge_per_category": float(ge.get("per_category_req", 4)),
        "ge_common_elective": float(common.get("ge_common_elective_req", 2)),
        "major_total": float(major.get("total_req", 85)),
        "major_common_compulsory": float(major["common_compulsory"].get("total_req", 24)),
        "domain_compulsory": float(domains.get("domain_req", 14)),
        "domain_elective": float(domains.get("domain_elective_req", 20)),
        "major_other_elective": float(domains.get("other_elective_req", 27)),
        "free_elective": float(free_policy.get("total_req", 15)),
        "pe_semesters": int(
            physical_contract.get(
                "semesters_required",
                physical_policy.get("semesters_required", 4),
            )
        ),
        "target_total": 0.0,
    }
    if program in {"雙主修", "輔系"}:
        program_key = "double_major" if program == "雙主修" else "minor"
        target_rules = handbook["cs_rules"] if "資科系" in target_dept else handbook["apc_rules"]
        requirements["target_total"] = float(target_rules[program_key]["total_req"])
    return requirements


_SEQUENCE_NUMERAL_MAP = {
    "I": "一", "II": "二", "III": "三", "IV": "四", "V": "五", "VI": "六",
    "1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六",
    "一": "一", "二": "二", "三": "三", "四": "四", "五": "五", "六": "六",
}


def normalize_course_name(name):
    """Normalize typography only; never erase semantic course identity.

    NFKC safely aligns full-width punctuation and Unicode Roman glyphs.
    Parenthesized Roman numerals (I)~(VI) and Arabic numerals (1)~(6)
    are standardized to Chinese numerals (一)~(六).  The sequence suffix,
    words such as ``實驗``/``含實驗``, and course subtitles are otherwise
    preserved.  In particular, ``微積分`` remains distinct from ``微積分(一)``.
    """
    if not name:
        return ""
    value = unicodedata.normalize("NFKC", str(name)).strip()
    value = value.replace("：", ":").replace("﹕", ":")
    value = re.sub(r"\[?(?:◇|※|◎|§|▲|╳)\]?", "", value)
    value = re.sub(r"^[◇※◎§▲╳★●■□◆☆]\s*", "", value)
    value = re.sub(r"\s+", "", value)

    def _seq_repl(match):
        token = match.group(1).upper()
        chinese = _SEQUENCE_NUMERAL_MAP.get(token, token)
        return f"({chinese})"

    value = re.sub(r"\((I{1,3}|IV|V|VI|[1-6]|[一二三四五六])\)", _seq_repl, value, flags=re.IGNORECASE)
    value = re.sub(r"^(英文\([一二三]\))[:-].*", r"\1", value)
    value = re.sub(r"\((?![IVXivx一二三四五六])[A-Za-z甲乙丙丁]\)$", "", value)
    if re.fullmatch(r"[\d./%]+", value):
        return ""
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", value) and re.search(r"\d", value):
        return ""
    return value


def get_aliases(rule_name, handbook_year=None, scope="university_common"):
    """Return aliases explicitly declared inside one department scope."""
    rule_sets = get_rule_sets(handbook_year)
    aliases = rule_sets.get("course_aliases", {}).get(scope, {}).get(rule_name, [])
    return [rule_name, *aliases]


def validate_rule_collisions(year=None):
    """Check duplicate aliases inside a single scope; cross-scope names stay separate."""
    selected = normalize_handbook_year(year)
    aliases = get_rule_sets(selected).get("course_aliases", {})
    collisions = []
    for scope, mapping in aliases.items():
        owner = {}
        for official, values in mapping.items():
            for alias in [official, *values]:
                key = normalize_course_name(alias)
                previous = owner.get(key)
                if previous and previous != official:
                    collisions.append({"scope": scope, "alias": alias, "courses": [previous, official]})
                owner[key] = official
    return collisions
