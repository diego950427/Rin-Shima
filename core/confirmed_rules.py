"""User-confirmed handbook contracts. Never regenerate without explicit approval."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
CONTRACT_PATH = ROOT / "data" / "confirmed_handbooks.json"


def selected_rules(config):
    earth = config["handbooks"]["113"]["earth_life_major"]
    return {
        "earth_113": {k: earth[k] for k in ("total_req", "common_compulsory", "common_alternatives", "domains", "common_electives")},
        "cs_double_114": config["handbooks"]["114"]["cs_rules"]["double_major"],
    }


def confirmed_label(curriculum_id):
    if curriculum_id in {"primary:113:earth:earth_environment", "primary:113:earth:life_science"}:
        return "113 地生手冊：已確認資料（2026-09-08，依使用者提供課程表）"
    if curriculum_id == "target:double_major:114:cs":
        return "114 資科雙主修 40 學分規定：已確認資料（2026-09-08）"
    return ""


def validate_confirmed_rules(config, record):
    label = confirmed_label(record["curriculum_id"])
    if not label:
        return
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    key = "earth_113" if record["program_slug"] == "earth" else "cs_double_114"
    if selected_rules(config)[key] != contract["rules"][key]:
        raise ValueError("已確認手冊資料遭變更；須取得使用者明確同意後才可更新。")
    expected = contract["thresholds"][record["curriculum_id"]]
    if record["thresholds"] != expected:
        raise ValueError("已確認手冊學分門檻不一致，已停止使用此版本。")
    if key == "earth_113":
        if any("service_learning" in str(row.get("requirement_id", ""))
               for row in record.get("non_credit_requirements", ())):
            raise ValueError("113 地生已確認不列服務學習，不得重新加入。")
        pools = {p["bucket"]: p for p in record["course_pools"].values()}
        expected_pools = {"university_compulsory": 10, "ge_common_elective": 2,
            "ge_藝術與美感": 4, "ge_公民素養與社會探索": 4,
            "ge_人文與文化思考": 4, "ge_自然、生命與科技": 4,
            "free_elective": 15, "common_compulsory": 24, "domain_required": 14,
            "domain_elective": 20, "department_professional": 27, "common_alternative_1": 2}
        if any(pools[k]["required_credits"] != v for k, v in expected_pools.items()):
            raise ValueError("已確認地生課程分類門檻遭變更。")
        if pools["free_elective"]["policy"].get("minimum_science_college_credits") != 3:
            raise ValueError("已確認的自由選修理學院至少 3 學分限制遭變更。")
    record["user_confirmation"] = {"label": label, "locked": True,
        "source": "data/confirmed_handbooks.json", "confirmed_on": "2026-09-08"}


def verify_sources():
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return all(hashlib.sha256((ROOT / row["file"]).read_bytes()).hexdigest() == row["sha256"]
               for row in contract["sources"])


def route_confirmed_attempts(primary_id, attempts, requirements):
    """Keep confirmed classifications, rather than fill arbitrary quotas.

    This only removes lower-priority allocation routes. It never changes
    identity, grade, earned credits or adds a new membership/approval.
    """
    if primary_id not in {"primary:113:earth:earth_environment", "primary:113:earth:life_science"}:
        return attempts
    from dataclasses import replace
    from allocation_engine import _direct_match
    free = {pool for req in requirements if req.bucket == "free_elective"
            for pool in req.eligible_pool_ids}
    other = {pool for req in requirements if req.bucket == "department_professional"
             for pool in req.eligible_pool_ids}
    output = []
    for attempt in attempts:
        matches = [req for req in requirements
                   if req.bucket != "free_elective" and _direct_match(attempt, req) is True]
        blocked = set(free) if matches else set()
        if any(req.bucket in {"domain_elective", "domain_required", "common_compulsory"}
               or req.bucket.endswith(":compulsory") or req.bucket.startswith("common_alternative")
               for req in matches):
            blocked.update(other)
        output.append(replace(attempt,
            pool_ids=tuple(p for p in attempt.pool_ids if p not in blocked),
            pool_memberships=tuple(p for p in attempt.pool_memberships if p not in blocked),
            pool_membership_evidence=tuple(p for p in attempt.pool_membership_evidence if p[0] not in blocked)))
    return tuple(output)
