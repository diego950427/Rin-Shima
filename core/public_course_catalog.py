"""Validated, term-scoped public course catalog evidence.

The university transcript export used by the application does not always
carry a course number or opening department.  This module is the small
server-owned adapter for the public course listings that can fill those
identity properties.  It deliberately has no dependency on transcript
parsers or curriculum rules: a public listing proves only the properties in
that listing, while the handbook/registry remains the authority for
eligibility and required credits.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

VERIFIED = "VERIFIED"
UNKNOWN = "UNKNOWN"
CONFLICTED = "CONFLICTED"
NOT_MEMBER = "NOT_MEMBER"

PUBLIC_CATALOG_SCHEMA = "public-course-catalog:v1"
_TERM_RE = re.compile(r"^\d{3}-[12]$")
_OFFICIAL_HOSTS = frozenset({"my.utaipei.edu.tw", "genedu.utaipei.edu.tw"})

# These values are intentionally closed.  A caller cannot inject a new
# category by placing it in a transcript row or by supplying a custom
# catalog record which the loader has not reviewed.
ALLOWED_OFFICIAL_CATEGORIES = frozenset(
    {
        "國文類",
        "英文類",
        "體育類",
        "藝術與美感領域",
        "人文與文化思考領域",
        "公民素養與社會探索領域",
        "自然、生命與科技領域",
        "共同選修",
        "共同必修",
        "校定必修",
        "系定必修",
        "系定選修",
        "分組必修",
        "分組選修",
        "服務學習類",
        "專業課程系定選修",
        "分組專業課程分組選修",
        "共修",
        "共選",
        "自然",
    }
)

_COURSE_FIELDS = (
    "term",
    "course_name",
    "credits",
    "hours",
    "official_course_code",
    "section",
    "selection_code",
    "course_type",
    "class_id",
    "class_label",
    "campus",
    "official_category",
    "department_unit",
    "college",
    "source_reference",
    "source_url",
    "source_listing",
    "active",
)
_ACADEMIC_ID_FIELDS = (
    "term",
    "course_name",
    "credits",
    "official_course_code",
    "section",
    "selection_code",
    "course_type",
    "class_id",
)


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float, bool)) else ""


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _decimal_key(value: Any) -> str:
    amount = _decimal(value)
    if amount is None:
        return ""
    # Treat JSON ``2`` and ``2.0`` as the same academic credit value.  The
    # source files intentionally preserve their numeric spelling, but joins
    # and exact lookup must compare the value rather than its serialization.
    normalized = amount.normalize()
    return str(normalized)


def normalize_course_name(value: Any) -> str:
    """Normalize exact course-name spelling without fuzzy matching.

    NFKC handles full-width punctuation and Roman numerals used by the
    handbook/transcript pair.  Subtitles, parenthetical components, and
    words remain part of the identity key.
    """

    from handbook_rules import normalize_course_name as _canonical_norm

    text = _canonical_norm(_text(value))
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("：", ":").replace("﹕", ":").replace("　", "")
    text = re.sub(r"\s+", "", text)
    return text.casefold()


_AG102_CATEGORY_PREFIX_RE = re.compile(r"^\[[^\[\]]+\]")
_AG102_CATEGORY_PREFIXES = {
    "[通選公民]": "公民素養與社會探索領域",
    "[通選人文]": "人文與文化思考領域",
    "[通選自然]": "自然、生命與科技領域",
    "[通選藝術]": "藝術與美感領域",
    "[共同選修]": "共同選修",
}


def _ag102_catalog_lookup_name(value: Any) -> tuple[str, str]:
    """Return an exact catalog title and the optional reviewed AG102 tag.

    AG102 may put one of the reviewed category labels in front of the course
    title.  The label is only a lookup aid and a consistency check; it is not
    itself evidence for a public category or any other membership.
    """

    text = unicodedata.normalize("NFKC", _text(value)).strip()
    match = _AG102_CATEGORY_PREFIX_RE.match(text)
    if match is None:
        return text, ""
    prefix = normalize_course_name(match.group(0))
    category = _AG102_CATEGORY_PREFIXES.get(prefix, "")
    if not category:
        return text, ""
    return text[match.end() :].strip(), category


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _hash_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _plain(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_is_official(url: Any) -> bool:
    parsed = urlparse(_text(url))
    host = (parsed.hostname or "").casefold()
    return parsed.scheme.casefold() == "https" and host in _OFFICIAL_HOSTS and not parsed.username and not parsed.password


def _record_copy(record: Mapping[str, Any], *, require_source: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _COURSE_FIELDS:
        value = record.get(key)
        if key == "credits" or key == "hours":
            amount = _decimal(value)
            if amount is None:
                raise ValueError(f"invalid public catalog {key}")
            result[key] = float(amount) if amount.as_tuple().exponent < 0 else int(amount)
        elif key == "active":
            if value is not None and not isinstance(value, bool):
                raise ValueError("public catalog active must be boolean")
            result[key] = True if value is None else value
        elif isinstance(value, (str, int, float, bool)) and not isinstance(value, (bytes, bytearray)):
            result[key] = value
    if not _TERM_RE.fullmatch(_text(result.get("term"))):
        raise ValueError("public catalog term is invalid")
    if not _text(result.get("course_name")):
        raise ValueError("public catalog course_name is missing")
    if not _text(result.get("official_category")) or _text(result["official_category"]) not in ALLOWED_OFFICIAL_CATEGORIES:
        raise ValueError("public catalog category is not allowlisted")
    if require_source:
        if not _text(result.get("source_reference")) or not _source_is_official(result.get("source_url")):
            raise ValueError("public catalog source is not an official HTTPS source")
        if result.get("source_listing") and not _source_is_official(result.get("source_listing")):
            raise ValueError("public catalog listing source is not official")
    return result


def _membership_copy(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError("public IT membership must be an object")
    row: dict[str, Any] = {}
    for key in (
        "term",
        "course_name",
        "credits",
        "membership_id",
        "catalog_matches",
        "official_category",
        "selection_code",
        "campus",
        "source_reference",
        "source_url",
        "source_listing",
        "active",
    ):
        value = record.get(key)
        if key == "credits":
            row[key] = value
        elif key == "catalog_matches":
            row[key] = _safe_refs(value)
        elif key == "active":
            if value is not None and not isinstance(value, bool):
                raise ValueError("public IT membership active must be boolean")
            row[key] = True if value is None else value
        elif isinstance(value, (str, int, float, bool)) and not isinstance(value, (bytes, bytearray)):
            row[key] = value
    if not _TERM_RE.fullmatch(_text(row.get("term"))) or not _text(row.get("course_name")):
        raise ValueError("public IT membership term/name is invalid")
    amount = _decimal(row.get("credits"))
    if amount is None:
        raise ValueError("public IT membership credits are invalid")
    if not _text(row.get("membership_id")) or not _text(row.get("source_reference")):
        raise ValueError("public IT membership identity is missing")
    if not _source_is_official(row.get("source_url")):
        raise ValueError("public IT membership source is not official")
    if row.get("source_listing") and not _source_is_official(row.get("source_listing")):
        raise ValueError("public IT membership listing source is not official")
    matches = _safe_refs(row.get("catalog_matches"))
    if not matches:
        raise ValueError("public IT membership has no catalog join")
    row["credits"] = float(amount) if amount.as_tuple().exponent < 0 else int(amount)
    row["catalog_matches"] = matches
    return row


def _course_identity_tuple(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        _text(record.get(field)) if field != "credits" else _decimal_key(record.get(field))
        for field in _ACADEMIC_ID_FIELDS
    )


def _public_course_identity_key(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Identity shared by sections of one official offering."""

    return (
        _text(record.get("term")),
        normalize_course_name(record.get("course_name")),
        _decimal_key(record.get("credits")),
        _text(record.get("official_course_code")),
    )


def _academic_fields_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    # ``class_label`` and descriptions may differ between a GE listing and a
    # department listing.  The fields below are the source-independent
    # academic identity used for a safe merge.
    return _course_identity_tuple(left) == _course_identity_tuple(right)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _values(value: Any) -> tuple[Any, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (str, bytes, bytearray)):
        return (value,)
    if isinstance(value, Sequence) or isinstance(value, set):
        return tuple(value)
    return (value,)


def _safe_refs(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(item) for item in _values(value) if _text(item)))


@dataclass(frozen=True, slots=True)
class PublicCourseEvidence:
    """Property-level result from one term-scoped public lookup."""

    public_identity_state: str = UNKNOWN
    candidate_source_refs: tuple[str, ...] = ()
    official_course_code: str = ""
    official_course_code_state: str = UNKNOWN
    official_category: str = ""
    official_category_state: str = UNKNOWN
    official_college: str = ""
    official_college_state: str = UNKNOWN
    department_unit: str = ""
    department_membership: str = ""
    department_membership_state: str = UNKNOWN
    verified_memberships: tuple[str, ...] = ()
    uncertain_memberships: tuple[str, ...] = ()
    pool_membership_evidence: tuple[tuple[str, str, str, str], ...] = ()
    pe_activity_id: str = ""
    reasons: tuple[str, ...] = ()
    term_covered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_source_refs": self.candidate_source_refs,
            "public_identity_state": self.public_identity_state,
            "official_course_code": self.official_course_code,
            "official_course_code_state": self.official_course_code_state,
            "official_category": self.official_category,
            "official_category_state": self.official_category_state,
            "official_college": self.official_college,
            "official_college_state": self.official_college_state,
            "department_unit": self.department_unit,
            "department_membership": self.department_membership,
            "department_membership_state": self.department_membership_state,
            "verified_memberships": self.verified_memberships,
            "uncertain_memberships": self.uncertain_memberships,
            "pool_membership_evidence": self.pool_membership_evidence,
            "pe_activity_id": self.pe_activity_id,
            "reasons": self.reasons,
            "term_covered": self.term_covered,
        }


@dataclass(frozen=True, slots=True)
class PublicCourseCatalog:
    """Immutable validated catalog and its exact lookup indexes."""

    schema: str
    dataset_id: str
    content_hash: str
    covered_terms: tuple[str, ...]
    courses: tuple[Mapping[str, Any], ...]
    it_memberships: tuple[Mapping[str, Any], ...] = ()
    it_professional_courses: tuple[Mapping[str, Any], ...] = ()
    source_urls: tuple[str, ...] = ()
    _by_code: Mapping[tuple[str, str, str], tuple[Mapping[str, Any], ...]] = field(init=False, repr=False, compare=False)
    _by_name: Mapping[tuple[str, str, str], tuple[Mapping[str, Any], ...]] = field(init=False, repr=False, compare=False)
    _it_by_ref: Mapping[str, tuple[Mapping[str, Any], ...]] = field(init=False, repr=False, compare=False)
    _it_by_key: Mapping[tuple[str, str, str], tuple[Mapping[str, Any], ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_code: defaultdict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        by_name: defaultdict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for record in self.courses:
            term = _text(record.get("term"))
            credits = _decimal_key(record.get("credits"))
            code = _text(record.get("official_course_code"))
            name = normalize_course_name(record.get("course_name"))
            if code:
                by_code[(term, code, credits)].append(record)
            if name:
                by_name[(term, name, credits)].append(record)
        it_by_ref: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
        it_by_key: defaultdict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for membership in self.it_memberships:
            refs = _safe_refs(membership.get("catalog_matches"))
            for ref in refs:
                it_by_ref[ref].append(membership)
            key = (
                _text(membership.get("term")),
                normalize_course_name(membership.get("course_name")),
                _decimal_key(membership.get("credits")),
            )
            it_by_key[key].append(membership)
        object.__setattr__(self, "_by_code", MappingProxyType({key: tuple(value) for key, value in by_code.items()}))
        object.__setattr__(self, "_by_name", MappingProxyType({key: tuple(value) for key, value in by_name.items()}))
        object.__setattr__(self, "_it_by_ref", MappingProxyType({key: tuple(value) for key, value in it_by_ref.items()}))
        object.__setattr__(self, "_it_by_key", MappingProxyType({key: tuple(value) for key, value in it_by_key.items()}))

    @classmethod
    def from_records(
        cls,
        courses: Sequence[Mapping[str, Any]],
        *,
        it_memberships: Sequence[Mapping[str, Any]] = (),
        it_professional_courses: Sequence[Mapping[str, Any]] = (),
        covered_terms: Sequence[str] | None = None,
        dataset_id: str = "public-course-catalog",
        source_urls: Sequence[str] = (),
    ) -> PublicCourseCatalog:
        course_rows = [_record_copy(row) for row in courses]
        professional_rows = [_record_copy(row) for row in it_professional_courses]
        membership_rows = [_membership_copy(row) for row in it_memberships]
        merged_course_rows = list(course_rows)
        merged_by_ref = {
            _text(row.get("source_reference")): row
            for row in merged_course_rows
            if _text(row.get("source_reference"))
        }
        for row in professional_rows:
            ref = _text(row.get("source_reference"))
            existing = merged_by_ref.get(ref)
            if existing is None:
                merged_by_ref[ref] = row
                merged_course_rows.append(row)
            elif not _academic_fields_match(existing, row):
                raise ValueError("professional course academic identity conflicts")
        terms = tuple(
            sorted(
                set(_text(row.get("term")) for row in (*merged_course_rows, *membership_rows) if _text(row.get("term")))
            )
            if covered_terms is None
            else tuple(dict.fromkeys(_text(item) for item in covered_terms if _text(item)))
        )
        payload = {
            "courses": sorted(merged_course_rows, key=lambda item: _text(item.get("source_reference"))),
            "it_memberships": sorted(membership_rows, key=lambda item: _text(item.get("source_reference"))),
            "it_professional_courses": sorted(professional_rows, key=lambda item: _text(item.get("source_reference"))),
        }
        mapping = {
            "schema": PUBLIC_CATALOG_SCHEMA,
            "dataset_id": dataset_id,
            "content_hash": _hash_payload(payload),
            "covered_terms": terms,
            "courses": merged_course_rows,
            "it_memberships": membership_rows,
            "it_professional_courses": professional_rows,
            "source_urls": tuple(source_urls),
        }
        return cls.from_mapping(mapping)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PublicCourseCatalog:
        if not isinstance(value, Mapping):
            raise ValueError("public catalog must be an object")
        schema = _text(value.get("schema"))
        if schema != PUBLIC_CATALOG_SCHEMA:
            raise ValueError("unsupported public catalog schema")
        dataset_id = _text(value.get("dataset_id"))
        if not dataset_id:
            raise ValueError("public catalog dataset_id is missing")
        raw_courses = value.get("courses", value.get("rows", ()))
        raw_professional = value.get("it_professional_courses", ())
        raw_memberships = value.get("it_memberships", ())
        if not isinstance(raw_courses, Sequence) or isinstance(raw_courses, (str, bytes, bytearray)):
            raise ValueError("public catalog courses must be a sequence")
        if not isinstance(raw_professional, Sequence) or isinstance(raw_professional, (str, bytes, bytearray)):
            raise ValueError("public catalog professional courses must be a sequence")
        if not isinstance(raw_memberships, Sequence) or isinstance(raw_memberships, (str, bytes, bytearray)):
            raise ValueError("public catalog IT memberships must be a sequence")
        if any(not isinstance(item, Mapping) for item in raw_courses):
            raise ValueError("public catalog courses contain a non-object row")
        if any(not isinstance(item, Mapping) for item in raw_professional):
            raise ValueError("public catalog professional courses contain a non-object row")
        if any(not isinstance(item, Mapping) for item in raw_memberships):
            raise ValueError("public catalog IT memberships contain a non-object row")
        course_rows = [_record_copy(item) for item in raw_courses]
        professional_rows = [_record_copy(item) for item in raw_professional]
        membership_rows = [_membership_copy(item) for item in raw_memberships]
        # A professional row is an additional official course listing.  If a
        # future source reuses a source_ref, merge only when every academic
        # identity field agrees; classification/description remains a
        # property conflict for the resolver to report.
        by_ref: dict[str, dict[str, Any]] = {}
        for row in course_rows:
            ref = _text(row.get("source_reference"))
            if not ref or ref in by_ref:
                raise ValueError("public catalog source_reference must be unique")
            by_ref[ref] = row
        for row in professional_rows:
            ref = _text(row.get("source_reference"))
            if not ref:
                raise ValueError("professional course source_reference is missing")
            existing = by_ref.get(ref)
            if existing is not None:
                if not _academic_fields_match(existing, row):
                    raise ValueError("professional course academic identity conflicts")
                # Keep the GE source's category when it exists and preserve
                # the professional row as a separate provenance source only
                # through its own IT membership record.
                continue
            by_ref[ref] = row
            course_rows.append(row)
        refs = set(by_ref)
        membership_refs: set[str] = set()
        for membership in membership_rows:
            membership_ref = _text(membership.get("source_reference"))
            if membership_ref in membership_refs:
                raise ValueError("public IT membership source_reference must be unique")
            membership_refs.add(membership_ref)
            for ref in _safe_refs(membership.get("catalog_matches")):
                if ref not in refs:
                    raise ValueError("public IT membership catalog join is unknown")
                joined = by_ref[ref]
                if (
                    _text(joined.get("term")) != _text(membership.get("term"))
                    or normalize_course_name(joined.get("course_name")) != normalize_course_name(membership.get("course_name"))
                    or _decimal_key(joined.get("credits")) != _decimal_key(membership.get("credits"))
                ):
                    raise ValueError("public IT membership catalog join does not match academic fields")
        covered_raw = value.get("covered_terms", ())
        covered = tuple(dict.fromkeys(_text(item) for item in _values(covered_raw) if _text(item)))
        if not covered or any(not _TERM_RE.fullmatch(item) for item in covered):
            raise ValueError("public catalog covered_terms are invalid")
        if any(_text(row.get("term")) not in covered for row in (*course_rows, *membership_rows)):
            raise ValueError("public catalog row is outside covered_terms")
        for ref, row in by_ref.items():
            if not ref or not _source_is_official(row.get("source_url")):
                raise ValueError("public catalog source reference is invalid")
        source_urls = tuple(dict.fromkeys(_text(item) for item in _values(value.get("source_urls")) if _text(item)))
        if any(not _source_is_official(url) for url in source_urls):
            raise ValueError("public catalog source_urls are invalid")
        payload = {
            "courses": [by_ref[ref] for ref in sorted(by_ref)],
            "it_memberships": sorted(membership_rows, key=lambda item: _text(item.get("source_reference"))),
            "it_professional_courses": sorted(professional_rows, key=lambda item: _text(item.get("source_reference"))),
        }
        expected_hash = _text(value.get("content_hash"))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or expected_hash != _hash_payload(payload):
            raise ValueError("public catalog content_hash does not match")
        ordered_courses = tuple(_freeze(by_ref[ref]) for ref in sorted(by_ref))
        ordered_memberships = tuple(_freeze(row) for row in sorted(membership_rows, key=lambda item: _text(item.get("source_reference"))))
        ordered_professional = tuple(_freeze(row) for row in professional_rows)
        return cls(
            schema=schema,
            dataset_id=dataset_id,
            content_hash=expected_hash,
            covered_terms=tuple(sorted(covered)),
            courses=ordered_courses,
            it_memberships=ordered_memberships,
            it_professional_courses=ordered_professional,
            source_urls=source_urls,
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> PublicCourseCatalog:
        target = Path(path) if path is not None else Path(__file__).with_name("data") / "public_course_catalog.json"
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return cls.from_mapping(value)

    def lookup(
        self,
        *,
        term: Any,
        course_code: Any = "",
        course_name: Any = "",
        credits: Any = 0,
        section: Any = "",
    ) -> tuple[Mapping[str, Any], ...]:
        term_text = _text(term)
        credit_key = _decimal_key(credits)
        if not term_text or term_text not in self.covered_terms:
            return ()
        code = _text(course_code)
        candidates: tuple[Mapping[str, Any], ...] = ()
        if code:
            candidates = self._by_code.get((term_text, code, credit_key), ())
        if not candidates and course_name:
            candidates = self._by_name.get((term_text, normalize_course_name(course_name), credit_key), ())
        section_text = _text(section)
        if section_text:
            candidates = tuple(item for item in candidates if _text(item.get("section")) == section_text)
        return tuple(
            item
            for item in candidates
            if item.get("active", True) is not False
            and "停開" not in _text(item.get("course_name"))
        )

    def it_for_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        term: Any,
        course_name: Any,
        credits: Any,
    ) -> tuple[str, tuple[Mapping[str, Any], ...], bool]:
        """Return (state, source rows, all-candidates-are-listed)."""

        key = (_text(term), normalize_course_name(course_name), _decimal_key(credits))
        memberships = list(self._it_by_key.get(key, ()))
        refs = {ref for item in memberships for ref in _safe_refs(item.get("catalog_matches"))}
        candidate_refs = {_text(item.get("source_reference")) for item in candidates if _text(item.get("source_reference"))}
        matched = [item for item in candidates if _text(item.get("source_reference")) in refs]
        if not matched and candidate_refs:
            # A source may publish an IT row by name/credit while the public
            # course listing has a different section token.  This fallback is
            # safe only when every candidate shares one official code and the
            # IT rows point to that same code.
            it_codes = {
                _text(course.get("official_course_code"))
                for membership in memberships
                for ref in _safe_refs(membership.get("catalog_matches"))
                for course in self.courses
                if _text(course.get("source_reference")) == ref
            }
            candidate_codes = {_text(item.get("official_course_code")) for item in candidates}
            if len(candidate_codes) == 1 and candidate_codes == it_codes and "" not in candidate_codes:
                matched = list(candidates)
        if not memberships or not candidate_refs:
            return UNKNOWN, tuple(memberships), False
        if len(matched) == len(candidates):
            return VERIFIED, tuple(memberships), True
        if matched:
            return UNKNOWN, tuple(memberships), False
        return UNKNOWN, tuple(memberships), False


_DEFAULT_CATALOG: PublicCourseCatalog | None = None


def load_public_course_catalog(path: str | Path | None = None) -> PublicCourseCatalog:
    global _DEFAULT_CATALOG
    if path is None and _DEFAULT_CATALOG is not None:
        return _DEFAULT_CATALOG
    catalog = PublicCourseCatalog.load(path)
    if path is None:
        _DEFAULT_CATALOG = catalog
    return catalog


def _pool_membership_records(
    membership_id: str,
    state: str,
    source: str,
    *,
    pool_membership_ids: Mapping[str, Sequence[str]] | None,
    kind: str = "public_catalog",
) -> list[tuple[str, str, str, str]]:
    if not membership_id:
        return []
    records = [(membership_id, state, source, kind)]
    for pool_id in (pool_membership_ids or {}).get(membership_id, ()):
        pool_text = _text(pool_id)
        if pool_text:
            records.append((pool_text, state, source, kind))
    return records


_CATEGORY_MEMBERSHIP = {
    "國文類": "university_compulsory",
    "英文類": "university_compulsory",
    "藝術與美感": "ge_art",
    "藝術與美感領域": "ge_art",
    "人文與文化思考": "ge_humanities",
    "人文與文化思考領域": "ge_humanities",
    "公民素養與社會探索": "ge_civic",
    "公民素養與社會探索領域": "ge_civic",
    "自然、生命與科技": "ge_nature",
    "自然、生命與科技領域": "ge_nature",
    "共同選修": "ge_common_elective",
    "共同必修": "university_compulsory",
    "校定必修": "university_compulsory",
}
_EXCLUDED_FROM_FREE = frozenset(
    {
        "國文類",
        "英文類",
        "體育類",
        "藝術與美感",
        "藝術與美感領域",
        "人文與文化思考",
        "人文與文化思考領域",
        "公民素養與社會探索",
        "公民素養與社會探索領域",
        "自然、生命與科技",
        "自然、生命與科技領域",
        "共同選修",
        "共同必修",
        "校定必修",
    }
)

# Math ownership is a source property, not a transcript category.  The
# public course export uses the stable department unit below; a missing unit
# remains UNKNOWN and is never treated as Math merely because the handbook
# row belongs to a Math curriculum.
_MATH_PROGRAM_SLUGS = frozenset({"math", "數學"})
_MATH_DEPARTMENT_UNITS = frozenset({"9200"})
_EXTERNAL_PROFESSIONAL_MEMBERSHIP = "external_department_or_school_professional"
_PRIMARY_IT_MEMBERSHIP = "university_it_direct_completion"
_PROGRAM_APPROVED_PREFIX = "PROGRAM_APPROVED_COURSE_EXEMPTION"
_MATH_PRIMARY_IT_COURSES = {
    "111": ("C語言程式設計", Decimal("3")),
    "112": ("C語言程式設計", Decimal("3")),
    "113": ("C語言程式設計", Decimal("3")),
    "114": ("Python程式設計", Decimal("3")),
    "115": ("Python程式設計", Decimal("3")),
}
_PROFESSIONAL_CATEGORIES = frozenset(
    {
        "系定必修",
        "系定選修",
        "分組必修",
        "分組選修",
        "專業課程系定選修",
        "分組專業課程分組選修",
        "共修",
        "共選",
    }
)


def _property_state(values: Sequence[str]) -> tuple[str, str]:
    clean = tuple(dict.fromkeys(item for item in values if item))
    if not clean:
        return UNKNOWN, ""
    if len(clean) == 1:
        return VERIFIED, clean[0]
    return CONFLICTED, ""


def _metadata_verified(candidate: Mapping[str, Any]) -> bool:
    evidence = _text(
        candidate.get("evidence_state")
        or candidate.get("evidence")
        or candidate.get("pool_evidence_state")
    ).upper()
    coverage = _text(candidate.get("coverage_state")).upper()
    return evidence == VERIFIED and (not coverage or coverage == "COMPLETE")


def _metadata_source(candidate: Mapping[str, Any]) -> str:
    for key in (
        "source_reference",
        "membership_source_reference",
        "source_assertion_id",
        "assertion_id",
    ):
        source = _text(candidate.get(key))
        if source:
            return source
    for key in ("source", "provenance", "policy_source"):
        nested = candidate.get(key)
        if isinstance(nested, Mapping):
            source = _metadata_source(nested)
            if source:
                return source
    return ""


def _metadata_course_names(candidate: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("course_name", "name", "raw_title", "display_name", "title"):
        normalized = normalize_course_name(candidate.get(key))
        if normalized:
            values.append(normalized)
    for key in ("eligible_course_names", "exact_titles"):
        values.extend(
            normalize_course_name(item)
            for item in _safe_refs(candidate.get(key))
            if normalize_course_name(item)
        )
    return tuple(dict.fromkeys(values))


def _metadata_cohort(candidate: Mapping[str, Any]) -> str:
    for key in (
        "curriculum_version",
        "version",
        "cohort",
        "admission_cohort",
        "target_curriculum_version",
    ):
        value = _text(candidate.get(key))
        if re.fullmatch(r"\d{3}", value):
            return value
    identifier = _text(
        candidate.get("official_course_identity")
        or candidate.get("course_id")
        or candidate.get("requirement_id")
        or candidate.get("id")
    )
    match = re.search(r"(?<!\d)(11[1-5])(?!\d)", identifier)
    return match.group(1) if match else ""


def _metadata_owner_state(candidate: Mapping[str, Any], *, require_explicit: bool) -> str:
    """Resolve an offering owner from server metadata only.

    ``program_slug`` on a target row describes applicability, so it is not an
    offering owner.  For secondary rows only explicit offering/dept fields are
    accepted.  A primary handbook identity containing ``:math:`` is a stable
    registry-owned identity and is allowed for the reviewed primary contract.
    """

    owner_values: list[str] = []
    for key in (
        "offering_program_slug",
        "course_program_slug",
        "course_owner_program",
        "owner_program_slug",
        "department_program_slug",
        "offering_department",
    ):
        value = _text(candidate.get(key))
        if value:
            owner_values.append(value.casefold())
    department_units: list[str] = []
    for key in (
        "department_unit",
        "official_department_unit",
        "course_department_unit",
        "offering_department_unit",
    ):
        value = _text(candidate.get(key))
        if value:
            department_units.append(value)
    membership = _text(candidate.get("department_membership"))
    if membership.startswith("official_department:"):
        department_units.append(membership.removeprefix("official_department:"))
    if owner_values:
        if all(value in _MATH_PROGRAM_SLUGS for value in owner_values):
            return VERIFIED
        return NOT_MEMBER
    if department_units:
        if all(value in _MATH_DEPARTMENT_UNITS for value in department_units):
            return VERIFIED
        return NOT_MEMBER
    if not require_explicit:
        identity = _text(candidate.get("official_course_identity") or candidate.get("course_id")).casefold()
        if ":math:" in identity:
            return VERIFIED
    return UNKNOWN


def _public_math_offering_state(candidates: Sequence[Mapping[str, Any]]) -> str:
    if not candidates:
        return UNKNOWN
    units = [_text(item.get("department_unit")) for item in candidates]
    if not units or any(not value for value in units) or len(set(units)) != 1:
        return UNKNOWN
    return VERIFIED if units[0] in _MATH_DEPARTMENT_UNITS else NOT_MEMBER


def _stable_course_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _text(value)).casefold()
    text = re.sub(r"[^0-9a-z\u3400-\u9fff]+", "_", text)
    return text.strip("_")


def _approved_membership_ids(candidate: Mapping[str, Any], *, cohort: str, course_name: str) -> tuple[str, ...]:
    explicit = _safe_refs(
        candidate.get("program_approved_membership_ids")
        or candidate.get("approved_membership_ids")
        or candidate.get("program_approved_exemption_memberships")
    )
    if explicit:
        return explicit
    key = _stable_course_key(course_name) or "course"
    return (f"{_PROGRAM_APPROVED_PREFIX}:math:{cohort}:{key}",)


def _math_primary_it_records(
    handbook_metadata: Sequence[Mapping[str, Any]],
    *,
    program_slug: str,
    course_name: str,
    credits: Any,
    candidates: Sequence[Mapping[str, Any]],
    public_identity_state: str,
    representative_source: str,
) -> tuple[tuple[str, str, str, str], ...]:
    if _text(program_slug).casefold() not in _MATH_PROGRAM_SLUGS:
        return ()
    credit_value = _decimal(credits)
    if credit_value is None:
        return ()
    title_key = normalize_course_name(course_name)
    offering_state = _public_math_offering_state(candidates)
    records: list[tuple[str, str, str, str]] = []
    for descriptor in handbook_metadata:
        if not isinstance(descriptor, Mapping) or not _metadata_verified(descriptor):
            continue
        scope = _text(descriptor.get("scope") or descriptor.get("role")).casefold()
        if scope and scope not in {"primary", "primary_course"}:
            continue
        cohort = _metadata_cohort(descriptor)
        expected = _MATH_PRIMARY_IT_COURSES.get(cohort)
        if expected is None or title_key != normalize_course_name(expected[0]) or credit_value != expected[1]:
            continue
        if title_key not in _metadata_course_names(descriptor):
            continue
        descriptor_credits = _decimal(descriptor.get("credits"))
        if descriptor_credits is None or descriptor_credits != credit_value:
            continue
        identity = _text(
            descriptor.get("official_course_identity")
            or descriptor.get("course_id")
            or descriptor.get("course_code")
        )
        source = _metadata_source(descriptor)
        if not identity or not source:
            continue
        # A named primary row is required.  Category/pool candidate metadata
        # alone cannot activate the programme-wide approved-course path.
        kind = _text(descriptor.get("kind") or descriptor.get("requirement_kind")).upper()
        bucket = _text(descriptor.get("bucket")).casefold()
        if scope not in {"primary", "primary_course"} and kind not in {"PRIMARY_COURSE", "REQUIRED_COURSE"}:
            continue
        if not ("compulsory" in bucket or "必修" in bucket or kind in {"PRIMARY_COURSE", "REQUIRED_COURSE"}):
            continue
        registry_owner = _metadata_owner_state(descriptor, require_explicit=False)
        if candidates:
            if public_identity_state != VERIFIED or offering_state != VERIFIED:
                continue
        elif registry_owner != VERIFIED:
            continue
        membership_source = representative_source if candidates else source
        for membership_id in _approved_membership_ids(descriptor, cohort=cohort, course_name=course_name):
            records.append((membership_id, VERIFIED, source, _PROGRAM_APPROVED_PREFIX))
        # The current primary IT requirement observes this same server-owned
        # approved-course evidence.  Keep the programme approval ID alongside
        # the canonical gate membership so both old and migrated registry rows
        # can consume the evidence without adding credits.
        records.append((_PRIMARY_IT_MEMBERSHIP, VERIFIED, membership_source, "public_catalog" if candidates else _PROGRAM_APPROVED_PREFIX))
    return tuple(dict.fromkeys(records))


def _math_secondary_records(
    handbook_metadata: Sequence[Mapping[str, Any]],
    *,
    program_slug: str,
    term: str,
    course_name: str,
    credits: Any,
    candidates: Sequence[Mapping[str, Any]],
    public_identity_state: str,
    representative_source: str,
) -> tuple[tuple[str, str, str, str], ...]:
    if _text(program_slug).casefold() not in _MATH_PROGRAM_SLUGS:
        return ()
    credit_value = _decimal(credits)
    if credit_value is None:
        return ()
    title_key = normalize_course_name(course_name)
    offering_state = _public_math_offering_state(candidates)
    records: list[tuple[str, str, str, str]] = []
    for descriptor in handbook_metadata:
        if not isinstance(descriptor, Mapping) or not _metadata_verified(descriptor):
            continue
        scope = _text(descriptor.get("scope") or descriptor.get("role")).casefold()
        if scope not in {"target", "minor", "secondary", "secondary_target", "double_major_target", "minor_target"}:
            continue
        if title_key not in _metadata_course_names(descriptor):
            continue
        descriptor_credits = _decimal(descriptor.get("credits"))
        if descriptor_credits is None or descriptor_credits != credit_value:
            continue
        cohort = _metadata_cohort(descriptor)
        identity = _text(
            descriptor.get("official_course_identity")
            or descriptor.get("course_id")
            or descriptor.get("course_code")
            or descriptor.get("requirement_id")
        )
        source = _metadata_source(descriptor)
        if not cohort or not identity or not source:
            continue
        owner_state = _metadata_owner_state(descriptor, require_explicit=True)
        if candidates:
            if public_identity_state != VERIFIED or offering_state != VERIFIED:
                continue
        elif owner_state != VERIFIED:
            continue
        key_value = (
            descriptor.get("secondary_course_key")
            or descriptor.get("candidate_key")
            or descriptor.get("course_key")
            or course_name
        )
        course_key = _stable_course_key(key_value)
        if not course_key:
            continue
        membership_id = f"math-secondary:{cohort}:{course_key}"
        union_membership_id = f"math-secondary:{cohort}:eligible-target-credit"
        membership_source = representative_source if candidates else source
        records.extend(
            (
                (membership_id, VERIFIED, membership_source, "public_catalog" if candidates else "registry"),
                (union_membership_id, VERIFIED, membership_source, "public_catalog" if candidates else "registry"),
            )
        )
    return tuple(dict.fromkeys(records))


def _external_professional_records(
    *,
    candidates: Sequence[Mapping[str, Any]],
    category_state: str,
    official_category: str,
    department_state: str,
    department_unit: str,
    representative_source: str,
) -> tuple[tuple[str, str, str, str], ...]:
    """Emit the Math external-professional cap evidence when provable.

    This deliberately has no negative-by-absence branch.  Only an official
    professional category together with an all-candidate department owner can
    establish either Math ``NOT_MEMBER`` or an external ``VERIFIED`` fact.
    """

    if not candidates or category_state != VERIFIED or official_category not in _PROFESSIONAL_CATEGORIES:
        return ()
    if department_state != VERIFIED or not department_unit:
        return ()
    if department_unit in _MATH_DEPARTMENT_UNITS:
        return ((_EXTERNAL_PROFESSIONAL_MEMBERSHIP, NOT_MEMBER, representative_source, "public_catalog_negative"),)
    return ((_EXTERNAL_PROFESSIONAL_MEMBERSHIP, VERIFIED, representative_source, "public_catalog"),)


def _math_approved_membership(
    handbook_metadata: Sequence[Mapping[str, Any]],
    *,
    program_slug: str,
    term: str,
) -> tuple[tuple[str, str, str, str], ...]:
    if _text(program_slug).casefold() not in {"math", "數學"}:
        return ()
    records: list[tuple[str, str, str, str]] = []
    for candidate in handbook_metadata:
        evidence_state = _text(candidate.get("evidence_state") or candidate.get("pool_evidence_state")).upper()
        if evidence_state != VERIFIED:
            continue
        membership_ids = _safe_refs(
            candidate.get("program_approved_membership_ids")
            or candidate.get("approved_membership_ids")
            or candidate.get("program_approved_exemption_memberships")
        )
        applicable_terms = _safe_refs(candidate.get("program_approved_applicable_terms") or candidate.get("approved_applicable_terms"))
        if not membership_ids or not applicable_terms or term not in applicable_terms:
            continue
        source = _text(candidate.get("source_reference") or candidate.get("membership_source_reference"))
        if not source:
            continue
        for membership_id in membership_ids:
            records.append((membership_id, VERIFIED, source, "handbook"))
    return tuple(dict.fromkeys(records))


def _official_completion_memberships(
    handbook_metadata: Sequence[Mapping[str, Any]],
    *,
    course_name: str,
    credits: Any,
    official_code: str,
    official_code_state: str,
) -> tuple[str, ...]:
    """Return exact server-owned listed-course completion memberships.

    The public listing proves the term, code, title, and credit value.  A
    curriculum descriptor supplies the scoped membership and exact identity
    contract; a transcript title or category by itself never creates one.
    This is used for zero-credit life/service series as well as any future
    official listed-course gate with the same shape.
    """

    if official_code_state != VERIFIED or not official_code:
        return ()
    credit_value = _decimal(credits)
    if credit_value is None:
        return ()
    title_key = normalize_course_name(course_name)
    matched: list[str] = []
    for descriptor in handbook_metadata:
        kind = _text(descriptor.get("kind") or descriptor.get("predicate_kind"))
        kind = kind.upper().replace("-", "_").replace(" ", "_")
        if kind != "OFFICIAL_LISTED_COURSE_COMPLETION":
            continue
        if _text(descriptor.get("evidence_state")).upper() != VERIFIED:
            continue
        if _text(descriptor.get("coverage_state")).upper() != "COMPLETE":
            continue
        if descriptor.get("automatic_decision") is not True:
            continue
        if _text(descriptor.get("term_bound")).upper() != VERIFIED:
            continue
        scope_state = _text(descriptor.get("scope_state")).upper()
        if scope_state and scope_state != VERIFIED:
            continue
        if descriptor.get("require_zero_credits") is True and credit_value != Decimal("0"):
            continue
        descriptor_credits = _decimal(descriptor.get("credits"))
        if descriptor_credits is None or descriptor_credits != credit_value:
            continue
        official_ids = set(
            _safe_refs(
                descriptor.get("official_course_ids")
                or descriptor.get("eligible_course_ids")
                or descriptor.get("course_ids")
            )
        )
        if official_code not in official_ids:
            continue
        names: list[str] = []
        for key in ("name", "raw_title", "display_name", "title_base"):
            value = normalize_course_name(descriptor.get(key))
            if value:
                names.append(value)
        for key in ("eligible_course_names", "exact_titles"):
            names.extend(normalize_course_name(item) for item in _safe_refs(descriptor.get(key)))
        for nested_key in ("match", "predicate"):
            nested = descriptor.get(nested_key)
            if not isinstance(nested, Mapping):
                continue
            names.extend(
                normalize_course_name(item)
                for item in _safe_refs(nested.get("exact_titles") or nested.get("eligible_course_names"))
            )
        name_set = {item for item in names if item}
        title_matches = title_key in name_set
        if not title_matches:
            # CS source tables enumerate Part 1–8 while the official public
            # listing may omit that suffix.  Permit that one reviewed shape
            # only when every enumerated title shares the same exact base;
            # this is still term-bound and does not create a fuzzy alias.
            part_bases = {
                match.group(1)
                for item in name_set
                if (match := re.fullmatch(r"(.+?)part[ _-]*[1-8]", item))
            }
            if len(part_bases) == 1 and len(part_bases) == len(name_set) - 7 and title_key in part_bases:
                title_matches = True
        if not title_matches:
            continue
        membership_ids = _safe_refs(
            descriptor.get("membership_ids")
            or descriptor.get("official_membership_id")
            or descriptor.get("membership_id")
        )
        matched.extend(membership_ids)
    return tuple(dict.fromkeys(matched))


def resolve_public_evidence(
    row: Mapping[str, Any] | PublicCourseCatalog,
    *,
    catalog: PublicCourseCatalog | None = None,
    term: Any = None,
    course_name: Any = None,
    credits: Any = None,
    course_code: Any = None,
    pool_membership_ids: Mapping[str, Sequence[str]] | None = None,
    handbook_metadata: Sequence[Mapping[str, Any]] = (),
    program_slug: str = "",
) -> PublicCourseEvidence:
    """Resolve public properties for one confirmed normalized row.

    Only transcript identity scalars are read.  Category, pool, membership,
    and source fields in ``row`` are intentionally ignored.
    """

    if isinstance(row, PublicCourseCatalog):
        catalog = row
        row = {
            "term": term,
            "course_name": course_name,
            "credits": credits,
            "course_code": course_code,
        }
    elif not isinstance(row, Mapping):
        row = {
            "term": term,
            "course_name": course_name,
            "credits": credits,
            "course_code": course_code,
        }

    term = _text(row.get("term"))
    course_code = _text(row.get("course_code") or row.get("official_course_code"))
    course_name = _text(row.get("course_name") or row.get("name"))
    catalog_lookup_name, tagged_category = _ag102_catalog_lookup_name(course_name)
    credits = row.get("credits", 0)
    if catalog is None:
        catalog = load_public_course_catalog()
    if term not in catalog.covered_terms:
        return PublicCourseEvidence(
            reasons=("PUBLIC_CATALOG_TERM_OUTSIDE_COVERAGE",),
            term_covered=False,
        )
    candidates = catalog.lookup(
        term=term,
        course_code=course_code,
        course_name=catalog_lookup_name,
        credits=credits,
        section=row.get("section"),
    )
    if not candidates:
        source_refs = ()
        public_identity_state = UNKNOWN
        code_state, official_code = UNKNOWN, ""
        category_state, official_category = UNKNOWN, ""
        college_state, official_college = UNKNOWN, ""
        department_state, department_unit = UNKNOWN, ""
        reasons: list[str] = ["PUBLIC_CATALOG_NO_EXACT_MATCH"]
    else:
        source_refs = tuple(sorted({_text(item.get("source_reference")) for item in candidates if _text(item.get("source_reference"))}))
        # Section/class/selection labels identify an offering instance, but the
        # course identity property remains verified when all sections share one
        # official code, title, and credit value.
        identities = {_public_course_identity_key(item) for item in candidates}
        public_identity_state = VERIFIED if len(identities) == 1 else CONFLICTED
        code_state, official_code = _property_state([_text(item.get("official_course_code")) for item in candidates])
        category_state, official_category = _property_state([_text(item.get("official_category")) for item in candidates])
        college_state, official_college = _property_state([_text(item.get("college")) for item in candidates])
        department_state, department_unit = _property_state([_text(item.get("department_unit")) for item in candidates])
        if tagged_category and category_state == VERIFIED:
            norm_tagged = tagged_category.replace("領域", "")
            norm_official = official_category.replace("領域", "")
            if norm_official != norm_tagged:
                category_state, official_category = CONFLICTED, ""
        reasons = []
    if public_identity_state != VERIFIED:
        reasons.append("PUBLIC_CATALOG_IDENTITY_CONFLICT")
    if category_state == CONFLICTED:
        reasons.append("PUBLIC_CATALOG_CATEGORY_CONFLICT")
    if college_state == CONFLICTED:
        reasons.append("PUBLIC_CATALOG_COLLEGE_CONFLICT")
    if department_state == CONFLICTED:
        reasons.append("PUBLIC_CATALOG_DEPARTMENT_CONFLICT")
    verified: list[str] = []
    uncertain: list[str] = []
    evidence: list[tuple[str, str, str, str]] = []
    representative_source = source_refs[0] if source_refs else ""
    if category_state == VERIFIED:
        category_membership = _CATEGORY_MEMBERSHIP.get(official_category)
        if category_membership:
            verified.append(category_membership)
            evidence.extend(
                _pool_membership_records(
                    category_membership,
                    VERIFIED,
                    representative_source,
                    pool_membership_ids=pool_membership_ids,
                )
            )
        if official_category in _EXCLUDED_FROM_FREE:
            verified.append("university_common_excluded_from_free")
            evidence.extend(
                _pool_membership_records(
                    "university_common_excluded_from_free",
                    VERIFIED,
                    representative_source,
                    pool_membership_ids=pool_membership_ids,
                )
            )
        if official_category == "體育類" and _decimal(credits) == Decimal("0") and code_state == VERIFIED and official_code:
            verified.append("university_physical_education_completion")
            evidence.extend(
                _pool_membership_records(
                    "university_physical_education_completion",
                    VERIFIED,
                    representative_source,
                    pool_membership_ids=pool_membership_ids,
                )
            )
            activity_id = f"pe:{official_code}"
            activity_membership = f"university_physical_education_activity:{activity_id}"
            verified.append(activity_membership)
            evidence.extend(
                _pool_membership_records(
                    activity_membership,
                    VERIFIED,
                    representative_source,
                    pool_membership_ids=pool_membership_ids,
                )
            )
        elif official_category == "體育類":
            uncertain.append("university_physical_education_completion")
    elif category_state == CONFLICTED:
        # All candidates may still be known to be outside the free pool.  Do
        # not choose a GE bucket when the category property itself conflicts.
        categories = {_text(item.get("official_category")) for item in candidates}
        if categories and categories.issubset(_EXCLUDED_FROM_FREE):
            verified.append("university_common_excluded_from_free")
            evidence.extend(
                _pool_membership_records(
                    "university_common_excluded_from_free",
                    VERIFIED,
                    representative_source,
                    pool_membership_ids=pool_membership_ids,
                )
            )
        uncertain.extend(
            _CATEGORY_MEMBERSHIP[category]
            for category in sorted(categories)
            if category in _CATEGORY_MEMBERSHIP
        )
    if college_state == VERIFIED and official_college and "理學院" in official_college:
        verified.append("science_college")
        evidence.extend(
            _pool_membership_records(
                "science_college",
                VERIFIED,
                representative_source,
                pool_membership_ids=pool_membership_ids,
            )
        )
    elif college_state in {UNKNOWN, CONFLICTED}:
        uncertain.append("science_college")
    department_membership = ""
    if department_state == VERIFIED and department_unit:
        department_membership = f"official_department:{department_unit}"
        verified.append(department_membership)
        evidence.extend(
            _pool_membership_records(
                department_membership,
                VERIFIED,
                representative_source,
                pool_membership_ids=pool_membership_ids,
            )
        )
    elif department_state in {UNKNOWN, CONFLICTED}:
        uncertain.append("official_department")
    completion_memberships = _official_completion_memberships(
        handbook_metadata,
        course_name=catalog_lookup_name,
        credits=credits,
        official_code=official_code,
        official_code_state=code_state,
    )
    for membership_id in completion_memberships:
        verified.append(membership_id)
        evidence.extend(
            _pool_membership_records(
                membership_id,
                VERIFIED,
                representative_source,
                pool_membership_ids=pool_membership_ids,
            )
        )
    it_state, it_rows, _all_listed = catalog.it_for_candidates(
        candidates,
        term=term,
        course_name=catalog_lookup_name,
        credits=credits,
    )
    it_ids = {_text(item.get("membership_id")) for item in it_rows if _text(item.get("membership_id"))}
    it_source = _text(it_rows[0].get("source_reference")) if it_rows else representative_source
    if it_state == VERIFIED and it_ids:
        for membership_id in sorted(it_ids):
            verified.append(membership_id)
            evidence.extend(
                _pool_membership_records(
                    membership_id,
                    VERIFIED,
                    it_source,
                    pool_membership_ids=pool_membership_ids,
                )
            )
    elif it_rows:
        uncertain.extend(sorted(it_ids or {"university_it_direct_completion"}))
    external_professional_records = _external_professional_records(
        candidates=candidates,
        category_state=category_state,
        official_category=official_category,
        department_state=department_state,
        department_unit=department_unit,
        representative_source=representative_source,
    )
    for membership_id, state, source, kind in external_professional_records:
        if state == VERIFIED:
            verified.append(membership_id)
        elif state != NOT_MEMBER:
            uncertain.append(membership_id)
        evidence.append((membership_id, state, source, kind))
    handbook_records = _math_approved_membership(
        handbook_metadata,
        program_slug=program_slug,
        term=term,
    )
    for membership_id, state, source, kind in handbook_records:
        verified.append(membership_id)
        evidence.append((membership_id, state, source, kind))
    math_primary_records = _math_primary_it_records(
        handbook_metadata,
        program_slug=program_slug,
        course_name=course_name,
        credits=credits,
        candidates=candidates,
        public_identity_state=public_identity_state,
        representative_source=representative_source,
    )
    for membership_id, state, source, kind in math_primary_records:
        if state == VERIFIED:
            verified.append(membership_id)
        else:
            uncertain.append(membership_id)
        evidence.append((membership_id, state, source, kind))
    math_secondary_records = _math_secondary_records(
        handbook_metadata,
        program_slug=program_slug,
        term=term,
        course_name=course_name,
        credits=credits,
        candidates=candidates,
        public_identity_state=public_identity_state,
        representative_source=representative_source,
    )
    for membership_id, state, source, kind in math_secondary_records:
        if state == VERIFIED:
            verified.append(membership_id)
        else:
            uncertain.append(membership_id)
        evidence.append((membership_id, state, source, kind))
    return PublicCourseEvidence(
        public_identity_state=public_identity_state,
        candidate_source_refs=source_refs,
        official_course_code=official_code,
        official_course_code_state=code_state,
        official_category=official_category,
        official_category_state=category_state,
        official_college=official_college,
        official_college_state=college_state,
        department_unit=department_unit,
        department_membership=department_membership,
        department_membership_state=department_state,
        verified_memberships=tuple(dict.fromkeys(sorted(verified))),
        uncertain_memberships=tuple(dict.fromkeys(sorted(uncertain))),
        pool_membership_evidence=tuple(dict.fromkeys(evidence)),
        pe_activity_id=(f"pe:{official_code}" if category_state == VERIFIED and official_category == "體育類" and code_state == VERIFIED else ""),
        reasons=tuple(dict.fromkeys(reasons)),
        term_covered=True,
    )


__all__ = [
    "ALLOWED_OFFICIAL_CATEGORIES",
    "CONFLICTED",
    "PUBLIC_CATALOG_SCHEMA",
    "PublicCourseCatalog",
    "PublicCourseEvidence",
    "NOT_MEMBER",
    "UNKNOWN",
    "VERIFIED",
    "load_public_course_catalog",
    "normalize_course_name",
    "resolve_public_evidence",
]
