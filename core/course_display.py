"""Presentation-only grouping; never decides credit eligibility."""

import re
from collections import OrderedDict


def general_education_category(row):
    evidence = " ".join(str(row.get(key) or "") for key in (
        "prefix_tag", "bracket_tag", "inferred_category", "course_type", "type"
    ))
    domains = (
        ("人文", "人文與文化思考領域"),
        ("公民", "公民素養與社會探索領域"),
        ("自然", "自然、生命與科技領域"),
        ("藝術", "藝術與美感領域"),
    )
    for token, label in domains:
        if token in evidence and any(word in evidence for word in ("通", "領域")):
            return label
    if any(word in evidence for word in ("通識", "通選", "共同選修")):
        return "其他通識／共同選修"
    return ""


def course_term(row):
    term = str(row.get("term") or row.get("academic_term") or "").strip()
    if term:
        return term
    year = str(row.get("academic_year") or "").strip()
    semester = str(row.get("semester") or row.get("semester_code") or "").strip()
    return "-".join(part for part in (year, semester) if part) or "學期待補"


def course_type_label(row):
    evidence = " ".join(str(row.get(key) or "") for key in (
        "course_type", "type", "prefix_tag", "inferred_category"
    ))
    if "必" in evidence:
        return "必修"
    if "選" in evidence:
        return "選修"
    return "類別待核對"


def _term_key(term):
    numbers = tuple(int(value) for value in re.findall(r"\d+", term))
    return (0, numbers) if numbers else (1, ())


def course_sort_key(row):
    category = general_education_category(row)
    kind = {"必修": 0, "選修": 1}.get(course_type_label(row), 2)
    return (0 if category else 1, category, _term_key(course_term(row)), kind)


def sorted_course_rows(rows):
    # Stable sort keeps original order within equivalent groups and retains
    # repeated attempts; no deduplication or inferred academic classification.
    return sorted(rows, key=course_sort_key)


def grouped_course_rows(rows):
    groups = OrderedDict()
    for row in sorted_course_rows(rows):
        category = general_education_category(row)
        title = f"通識課程｜{category}" if category else f"{course_term(row)} 學期"
        groups.setdefault(title, []).append(row)
    return tuple(groups.items())
