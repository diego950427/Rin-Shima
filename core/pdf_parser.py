"""
Transcript PDF Parser using Coordinates-based Layout Reconstruct
"""

import os
import re

import fitz

from handbook_rules import normalize_course_name
from course_display import grouped_course_rows, course_term
from course_status import is_withdrawn_grade

MAX_PDF_BYTES = 20 * 1024 * 1024


def detect_department_track(text):
    """Detect one of the four supported department families from transcript text.

    Detection is deliberately anchored to department/track phrases when
    possible; a course title mentioning physics or mathematics is not enough
    to infer a student's primary department.
    """

    value = str(text or "")
    candidates = [
        ("地生", "生命科學", ("生命科學系", "生命科學組", "地生生命")),
        ("地生", "地球環境", ("地球環境暨生物資源學系", "地球環境組", "地球環境")),
        ("物化", "電子物理", ("電子物理系", "電子物理組", "物理化學系")),
        ("物化", "應用化學", ("應用化學系", "應用化學組", "化學組")),
        ("資科", None, ("資訊科學系", "資訊科學", "資科系")),
        ("數學", None, ("數據科學與數學", "數學系", "數學科學")),
    ]
    for department, track, markers in candidates:
        if any(marker in value for marker in markers):
            return {"department": department, "track": track, "matched_text": next(marker for marker in markers if marker in value)}
    return {"department": "", "track": None, "matched_text": ""}


def detect_admission_cohort(value):
    """Extract a supported ROC admission cohort from ROC/Gregorian text."""

    text = str(value or "")
    for token in re.findall(r"(?<!\d)\d{3,4}(?!\d)", text):
        number = int(token)
        if 111 <= number <= 115:
            return str(number)
        if 2022 <= number <= 2100 and 111 <= number - 1911 <= 189:
            return str(number - 1911)
    return ""


def _open_transcript(source):
    """Open a path, uploaded file, or bytes object after basic PDF validation."""
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"找不到成績單 PDF：{path}")
        if os.path.getsize(path) > MAX_PDF_BYTES:
            raise ValueError("PDF 超過 20 MB，請先壓縮後再試。")
        with open(path, "rb") as handle:
            signature = handle.read(5)
        if signature != b"%PDF-":
            raise ValueError("檔案內容不是有效的 PDF。")
        return fitz.open(path)

    if hasattr(source, "getvalue"):
        data = source.getvalue()
    elif hasattr(source, "read"):
        data = source.read()
    else:
        data = source
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("成績單來源必須是 PDF 路徑或二進位內容。")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF 超過 20 MB，請先壓縮後再試。")
    if not bytes(data).startswith(b"%PDF-"):
        raise ValueError("檔案內容不是有效的 PDF。")
    return fitz.open(stream=bytes(data), filetype="pdf")


_TOTAL_TOLERANCE = 0.01
_SUMMARY_NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$")


def _group_words_by_row(words, y_tol=4):
    """Group PyMuPDF words into visual rows without joining columns."""

    rows = []
    for word in sorted(words, key=lambda item: (item[1], item[0])):
        y0 = word[1]
        for row in rows:
            if abs(y0 - row["y"]) < y_tol:
                row["words"].append(word)
                # Keep the representative close to the center of a wrapped
                # row so later rows with a small baseline shift still join.
                row["y"] = (row["y"] * (len(row["words"]) - 1) + y0) / len(row["words"])
                break
        else:
            rows.append({"y": y0, "words": [word]})
    return rows


def _summary_label_kind(text):
    """Return a cumulative-summary field name for a label, if unambiguous."""

    compact = re.sub(r"\s+", "", str(text or ""))
    lower = compact.casefold()
    if "修習中" in compact and "學分" in compact and ("總" in compact or "歷年" in compact):
        return "in_progress"
    if (
        ("修習" in compact or "修讀" in compact or "修課" in compact)
        and "學分" in compact
        and ("總" in compact or "歷年" in compact)
    ) or lower in {
        "attemptedcredits",
        "totalattemptedcredits",
        "creditsattempted",
    }:
        return "attempted"
    if (
        "實得" in compact
        and "學分" in compact
        and ("總" in compact or "歷年" in compact)
    ) or lower in {
        "earnedcredits",
        "totalearnedcredits",
        "creditsearned",
    }:
        return "earned"
    return None


def _summary_label_boxes(page):
    """Find cumulative labels and their visual bounding boxes on one page."""

    words = page.get_text("words")
    rows = _group_words_by_row(words, y_tol=4)
    boxes = []
    # A bilingual/multi-level heading may put each part on an adjacent row.
    # Join at most four close rows, but never rows from a separate table.
    for start in range(len(rows)):
        found = False
        for end in range(start, min(len(rows), start + 4)):
            if rows[end]["y"] - rows[start]["y"] > 48:
                break
            row_words = [word for row in rows[start : end + 1] for word in row["words"]]
            # Summary columns can share one visual row.  Keep the threshold
            # below the roughly 20-point gap used by the portal's two-column
            # footer while still joining words within one bilingual label.
            clusters = []
            for word in sorted(row_words, key=lambda item: item[0]):
                if not clusters or word[0] - clusters[-1][-1][2] > 14:
                    clusters.append([word])
                else:
                    clusters[-1].append(word)
            for cluster in clusters:
                text_words = [word for word in cluster if _SUMMARY_NUMERIC_RE.fullmatch(re.sub(r"\s+", "", str(word[4] or ""))) is None]
                label = "".join(word[4] for word in text_words)
                kind = _summary_label_kind(label)
                if not kind or not text_words:
                    continue
                boxes.append(
                    (
                        kind,
                        (
                            min(word[0] for word in text_words),
                            min(word[1] for word in text_words),
                            max(word[2] for word in text_words),
                            max(word[3] for word in text_words),
                        ),
                    )
                )
                found = True
            if found:
                # Once the label is recognized, do not absorb its numeric
                # value into the bounding box on a later row.
                break
    # The sliding windows above intentionally find wrapped headings, so
    # collapse the same physical box before collecting values.
    unique = []
    for kind, box in boxes:
        if any(
            kind == other_kind
            and abs(box[0] - other_box[0]) < 1
            and abs(box[1] - other_box[1]) < 1
            and abs(box[2] - other_box[2]) < 1
            and abs(box[3] - other_box[3]) < 1
            for other_kind, other_box in unique
        ):
            continue
        unique.append((kind, box))
    return unique


def _summary_value_for_box(page, box):
    """Read the closest numeric value below a summary label's column."""

    x0, y0, x1, y1 = box
    center = (x0 + x1) / 2.0
    width = max(1.0, x1 - x0)
    values = []
    for word in page.get_text("words"):
        token = re.sub(r"\s+", "", str(word[4] or ""))
        if not _SUMMARY_NUMERIC_RE.fullmatch(token):
            continue
        gap = word[1] - y1
        if gap < -1 or gap > 120:
            continue
        word_center = (word[0] + word[2]) / 2.0
        if word_center < x0 - 24 or word_center > x1 + 24:
            # A narrow label can have a centered value just outside its box,
            # but a value from a neighboring summary column must stay out.
            continue
        distance = 0.0 if x0 - 8 <= word_center <= x1 + 8 else abs(word_center - center)
        if distance > max(38.0, width * 0.9):
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        if value < 0:
            continue
        values.append((gap, distance, value))
    if not values:
        return None
    values.sort(key=lambda item: (item[0], item[1]))
    return values[0][2]


def _extract_reported_totals_from_doc(doc):
    """Extract cumulative attempted/earned totals by label-to-value alignment.

    This deliberately has no full-text fallback: a term heading such as
    ``修習學分`` is not evidence of a cumulative total.
    """

    values = {"attempted": [], "earned": [], "in_progress": []}
    for page_index, page in enumerate(doc):
        for kind, box in _summary_label_boxes(page):
            value = _summary_value_for_box(page, box)
            if value is not None:
                values[kind].append((page_index, value))

    def resolve(kind):
        candidates = [value for _page_index, value in values[kind]]
        unique_values = []
        for value in candidates:
            if not any(abs(value - prior) <= _TOTAL_TOLERANCE for prior in unique_values):
                unique_values.append(value)
        conflict = len(unique_values) > 1
        return (None if conflict or not unique_values else unique_values[0]), unique_values, conflict

    attempted, attempted_candidates, attempted_conflict = resolve("attempted")
    earned, earned_candidates, earned_conflict = resolve("earned")
    in_progress, in_progress_candidates, in_progress_conflict = resolve("in_progress")
    return {
        "reported_total": attempted,
        "reported_earned_total": earned,
        "reported_in_progress_total": in_progress,
        "attempted_candidates": attempted_candidates,
        "earned_candidates": earned_candidates,
        "in_progress_candidates": in_progress_candidates,
        "attempted_conflict": attempted_conflict,
        "earned_conflict": earned_conflict,
        "in_progress_conflict": in_progress_conflict,
    }


def _parse_credit_value(value):
    token = re.sub(r"\s+", "", str(value or ""))
    if not token or token == "--":
        return 0.0
    try:
        parsed = float(token)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed >= 0 else 0.0


def _semester_status(score, credit):
    """Classify one semester using the same conservative status vocabulary."""

    if credit <= 0:
        return "ZERO_CREDIT"
    token = re.sub(r"\s+", "", str(score or "")).casefold()
    if token in {"未", "在修", "修習中", "修讀中", "修課中", "inprogress", "enrolled", "taking", "", "--"}:
        return "IN_PROGRESS"
    if token in {"p", "pass", "passed", "及格", "通過", "已修", "已完成", "修畢", "completed", "complete"}:
        return "COMPLETED"
    if token in {"免", "免修", "waived"}:
        return "WAIVED"
    if token in {"抵", "抵免", "抵認", "transfer", "transferred", "transfercredit"}:
        return "TRANSFERRED"
    if is_withdrawn_grade(score):
        return "WITHDRAWN"
    if token in {"f", "fail", "failed", "不及格", "不通過", "停", "w", "withdrawn", "停修", "撤選"}:
        return "ENDED_NO_EARNED"
    try:
        return "COMPLETED" if float(token) >= 60 else "ENDED_NO_EARNED"
    except (TypeError, ValueError):
        return "UNKNOWN"


def _totals_from_courses(course_list):
    """Compute totals per semester so mixed-status rows are never whole-row counted."""

    totals = {
        "all_course_credits": 0.0,
        "completed_attempted_credits": 0.0,
        "earned_credits": 0.0,
        "in_progress_credits": 0.0,
        "waived_credits": 0.0,
        "unverified_transfer_credits": 0.0,
        "unknown_credits": 0.0,
        "withdrawn_credits": 0.0,
    }
    for course in course_list:
        for credit_key, score_key in (("sem1_credit", "sem1_score"), ("sem2_credit", "sem2_score")):
            if credit_key not in course or course.get(credit_key) in (None, ""):
                continue
            credit = _parse_credit_value(course.get(credit_key))
            if credit <= 0:
                continue
            status = _semester_status(course.get(score_key), credit)
            totals["all_course_credits"] += credit
            if status == "IN_PROGRESS":
                totals["in_progress_credits"] += credit
            elif status == "COMPLETED":
                totals["completed_attempted_credits"] += credit
                totals["earned_credits"] += credit
            elif status == "ENDED_NO_EARNED":
                totals["completed_attempted_credits"] += credit
            elif status == "WITHDRAWN":
                # Printed 停/退 is not a failed attempt. Keep the course visible,
                # but exclude it from the transcript's attempted/earned totals.
                totals["withdrawn_credits"] += credit
            elif status == "WAIVED":
                totals["waived_credits"] += credit
            elif status == "TRANSFERRED":
                totals["unverified_transfer_credits"] += credit
            else:
                totals["unknown_credits"] += credit
    return {key: round(value, 2) for key, value in totals.items()}


def _duplicate_course_rows(course_list):
    """Return whether the selected parse contains exact repeated attempts."""

    seen = set()
    duplicates = 0
    for course in course_list:
        key = (
            course.get("name", ""),
            course.get("academic_year", ""),
            course.get("sem1_credit", ""),
            course.get("sem1_score", ""),
            course.get("sem2_credit", ""),
            course.get("sem2_score", ""),
            course.get("type", ""),
        )
        if key in seen:
            duplicates += 1
        seen.add(key)
    return duplicates


def parse_transcript_pdf(pdf_path):
    doc = _open_transcript(pdf_path)
    try:
        return _parse_transcript_document(doc)
    finally:
        # Closing in a finally block also covers malformed documents and
        # parser exceptions before the normal return path.
        doc.close()


def _parse_transcript_document(doc):
    if len(doc) == 0:
        raise ValueError("PDF file is empty")
    all_pages = [page for page in doc]
    first_page_text = all_pages[0].get_text()
    detected = detect_department_track(first_page_text)

    # 1. Extract Student Information from the first page
    student_info = {
        "name": "",
        "student_id": "",
        "department": "",
        "admission_year": "",
        "print_date": "",
        "double_major": None,
        "minor": None,
    }

    text_blocks = all_pages[0].get_text("blocks")
    for block in text_blocks:
        text = block[4].strip()
        if "姓名:" in text or "姓名：" in text:
            m = re.search(r"姓名[：:]\s*([^\s\n]+)", text)
            if m:
                student_info["name"] = m.group(1)
        if "學號:" in text or "學號：" in text:
            m = re.search(r"學號[：:]\s*([^\s\n]+)", text)
            if m:
                student_info["student_id"] = m.group(1)
        if "系所:" in text or "系所：" in text:
            m = re.search(r"系所[：:]\s*([^\s\n]+)", text)
            if m:
                student_info["department"] = m.group(1)
        elif "地球環境暨生物資源學系" in text and not student_info["department"]:
            student_info["department"] = "地球環境暨生物資源學系"
        if "雙主修:" in text or "雙主修：" in text:
            m = re.search(r"雙主修[：:]\s*([^\s\n]+)", text)
            raw_dm = m.group(1) if m else ""
            if not raw_dm or raw_dm.startswith("[WEB") or raw_dm in {"無", "--", "none", "null"}:
                m_before = re.search(r"([^\s\n]+)\s*\n\s*雙主修[：:]", text)
                if m_before:
                    raw_dm = m_before.group(1)
            if raw_dm and raw_dm not in {"無", "--", "none", "null", ""} and not raw_dm.startswith("[WEB"):
                status = "修習中" if "修習中" in raw_dm else ("已核准" if "核准" in raw_dm else "修習中")
                clean_target = raw_dm.split("-")[0]
                detected_dm = detect_department_track(clean_target)
                dept = detected_dm["department"] or clean_target
                track = detected_dm["track"]
                if not track and "化學" in clean_target:
                    track = "應用化學"
                elif not track and "物理" in clean_target:
                    track = "電子物理"
                student_info["double_major"] = {
                    "department": dept,
                    "track": track,
                    "status": status,
                    "raw": raw_dm,
                }
        if "輔系:" in text or "輔系：" in text:
            m = re.search(r"輔系[：:]\s*([^\s\n]+)", text)
            raw_minor = m.group(1) if m else ""
            if not raw_minor or raw_minor.startswith("[WEB") or raw_minor in {"無", "--", "none", "null"}:
                m_before = re.search(r"([^\s\n]+)\s*\n\s*輔系[：:]", text)
                if m_before:
                    raw_minor = m_before.group(1)
            if raw_minor and raw_minor not in {"無", "--", "none", "null", ""} and not raw_minor.startswith("[WEB"):
                status = "修習中" if "修習中" in raw_minor else ("已核准" if "核准" in raw_minor else "修習中")
                clean_target = raw_minor.split("-")[0]
                detected_minor = detect_department_track(clean_target)
                dept = detected_minor["department"] or clean_target
                track = detected_minor["track"]
                student_info["minor"] = {
                    "department": dept,
                    "track": track,
                    "status": status,
                    "raw": raw_minor,
                }
        if "入學年月:" in text or "入學年月：" in text:
            m = re.search(r"入學年月[：:]\s*([^\s\n]+)", text)
            if m:
                raw_adm = m.group(1).strip()
                greg_m = re.match(r"^(\d{4})[/-](\d{1,2})$", raw_adm)
                if greg_m:
                    roc_y = int(greg_m.group(1)) - 1911
                    m_str = f"{int(greg_m.group(2)):02d}"
                    student_info["admission_year"] = f"{roc_y}年{m_str}月"
                else:
                    student_info["admission_year"] = raw_adm
        if "列印日期" in text:
            m = re.search(r"列印日期(?:\(Date of Issue\))?[：:]\s*([^\s\n]+)", text)
            if m:
                student_info["print_date"] = m.group(1)

    # Fallback to search first_page_text if blocks didn't catch double_major or minor
    if not student_info["double_major"]:
        m = re.search(r"雙主修[：:]\s*([^\s\n]+)", first_page_text)
        if m:
            raw_dm = m.group(1)
            if raw_dm not in {"無", "--", "none", "null", ""} and not raw_dm.startswith("[WEB"):
                status = "修習中" if "修習中" in raw_dm else ("已核准" if "核准" in raw_dm else "修習中")
                clean_target = raw_dm.split("-")[0]
                detected_dm = detect_department_track(clean_target)
                dept = detected_dm["department"] or clean_target
                track = detected_dm["track"]
                if not track and "化學" in clean_target:
                    track = "應用化學"
                elif not track and "物理" in clean_target:
                    track = "電子物理"
                student_info["double_major"] = {
                    "department": dept,
                    "track": track,
                    "status": status,
                    "raw": raw_dm,
                }
    if not student_info["minor"]:
        m = re.search(r"輔系[：:]\s*([^\s\n]+)", first_page_text)
        if m:
            raw_minor = m.group(1)
            if raw_minor not in {"無", "--", "none", "null", ""} and not raw_minor.startswith("[WEB"):
                status = "修習中" if "修習中" in raw_minor else ("已核准" if "核准" in raw_minor else "修習中")
                clean_target = raw_minor.split("-")[0]
                detected_minor = detect_department_track(clean_target)
                dept = detected_minor["department"] or clean_target
                track = detected_minor["track"]
                student_info["minor"] = {
                    "department": dept,
                    "track": track,
                    "status": status,
                    "raw": raw_minor,
                }

    # Never substitute another student's data when the PDF layout changes.
    for key in ("name", "student_id", "department", "admission_year", "print_date"):
        if not student_info[key]:
            student_info[key] = "未辨識"
    primary_detected = detect_department_track(student_info["department"])
    if student_info["department"] == "未辨識" and detected["department"]:
        student_info["department"] = detected["department"]
    if primary_detected["department"]:
        student_info["department_family"] = primary_detected["department"]
        student_info["track"] = primary_detected["track"]
    else:
        student_info["department_family"] = detected["department"] or ""
        student_info["track"] = detected["track"]
    detected_admission_cohort = detect_admission_cohort(student_info.get("admission_year"))
    student_info["admission_cohort"] = detected_admission_cohort

    def group_words_by_row(words, y_tol=4):
        rows = []
        for w in sorted(words, key=lambda w: (w[1], w[0])):
            x0, y0, x1, y1, text_w, block_no, line_no, word_no = w
            placed = False
            for row in rows:
                if abs(y0 - row["y"]) < y_tol:
                    row["words"].append(w)
                    placed = True
                    break
            if not placed:
                rows.append({"y": y0, "words": [w]})
        return rows

    def merge_continuation_rows(rows, col_x=295):
        merged_rows = []
        i = 0
        while i < len(rows):
            row = rows[i]

            # --- 1. 左側欄位合併邏輯 ---
            left_words = [w for w in row["words"] if w[0] < col_x]
            left_text = "".join([w[4] for w in sorted(left_words, key=lambda w: w[0])]).strip()
            left_has_type = any(w[4] in ("必", "選") for w in left_words)
            left_has_credit = any(re.match(r"^(?:\d+|--|P|抵|免|未)$", w[4]) for w in left_words if w[0] > 180)

            # 如果當前行左側沒有課程屬性標記(必/選)，且有文字內容，且不是學期標題等，嘗試與下一行合併
            if (
                left_text
                and not left_has_type
                and not left_has_credit
                and not any(
                    term in left_text
                    for term in [
                        "學年",
                        "累計學分",
                        "實得學分",
                        "附註",
                        "學分分數",
                        "科科科科",
                        "第一學期",
                        "第二學期",
                        "學分",
                        "分數",
                    ]
                )
                and i + 1 < len(rows)
            ):
                next_row = rows[i + 1]
                next_left_words = [w for w in next_row["words"] if w[0] < col_x]
                next_left_text = "".join([w[4] for w in sorted(next_left_words, key=lambda w: w[0])]).strip()

                # 條件1: 下一行以數字編號開頭（如"一)","二)"）
                # 條件2: 當前行以"("結尾（如課程名稱換行）
                # 條件3: 下一行有"必"或"選"標記（表示是課程屬性行）
                next_has_type = any(w[4] in ("必", "選") for w in next_left_words)
                if (
                    re.match(r"^[一二三四五六七八九十][\)\]]?$", next_left_text)
                    or left_text.endswith("(")
                    or (next_left_text and next_has_type)
                ):
                    # 合併：目前行加上下一行左側的文字
                    row["words"] = row["words"] + next_left_words
                    next_row["words"] = [w for w in next_row["words"] if w[0] >= col_x]

            # --- 2. 右側欄位合併邏輯 ---
            right_words = [w for w in row["words"] if w[0] >= col_x]
            right_text = "".join([w[4] for w in sorted(right_words, key=lambda w: w[0])]).strip()
            right_has_type = any(w[4] in ("必", "選") for w in right_words)
            right_has_credit = any(re.match(r"^(?:\d+|--|P|抵|免|未)$", w[4]) for w in right_words if w[0] > 470)

            # 如果當前行右側沒有課程屬性標記(必/選)，且有文字內容，且不是學期標題等，嘗試與下一行合併
            if (
                right_text
                and not right_has_type
                and not right_has_credit
                and not any(
                    term in right_text
                    for term in [
                        "學年",
                        "累計學分",
                        "實得學分",
                        "附註",
                        "學分分數",
                        "科科科科",
                        "第一學期",
                        "第二學期",
                        "學分",
                        "分數",
                    ]
                )
                and i + 1 < len(rows)
            ):
                next_row = rows[i + 1]
                next_right_words = [w for w in next_row["words"] if w[0] >= col_x]
                next_right_text = "".join([w[4] for w in sorted(next_right_words, key=lambda w: w[0])]).strip()

                # 條件與左欄同理，下一行有必選修屬性或名稱連續
                next_right_has_type = any(w[4] in ("必", "選") for w in next_right_words)
                if (
                    re.match(r"^[一二三四五六七八九十][\)\]]?$", next_right_text)
                    or right_text.endswith("(")
                    or (next_right_text and next_right_has_type)
                ):
                    # 合併：目前行加上下一行右側的文字
                    row["words"] = row["words"] + next_right_words
                    next_row["words"] = [w for w in next_row["words"] if w[0] < col_x]

            merged_rows.append(row)
            i += 1
        return merged_rows

    reported_totals = _extract_reported_totals_from_doc(all_pages)
    reported_total = reported_totals["reported_total"]
    reported_earned_total = reported_totals["reported_earned_total"]

    def page_contains_course_rows(words):
        return any(w[4] in ("必", "選") for w in words)

    def parse_page_words(words, y_tol=3, page_carry=""):
        rows = group_words_by_row(words, y_tol=y_tol)
        rows = merge_continuation_rows(rows)
        parsed = []
        term_issues = []

        def stream_header_values(part_words):
            """Return only explicit ``NNN學年`` markers in one column row."""

            compact = re.sub(r"\s+", "", "".join(word[4] for word in part_words))
            # Identity and cumulative-summary rows contain 學年 as ordinary
            # prose.  They are not term headers for the course table.
            if any(marker in compact for marker in ("入學", "列印", "累計", "排名", "修習", "實得", "操行")):
                return []

            values = []
            for word in part_words:
                token = re.sub(r"\s+", "", str(word[4] or ""))
                match = re.fullmatch(r"(1\d{2})學年(?:度)?", token)
                if match:
                    values.append(match.group(1))
            if values:
                return values

            # Some PDF producers split the year and 學年 into separate words.
            # Restrict the fallback to the same structured marker so ordinary
            # dates and arbitrary three-digit course tokens never set a year.
            return [
                match.group(1)
                for match in re.finditer(r"(?<!\d)(1\d{2})學年(?:度)?(?!\d)", compact)
            ]

        def assign_stream(side_words, side_name, initial_year):
            current_year = str(initial_year or "")
            row_years = {}
            for row in rows:
                part_words = [word for word in row["words"] if side_words(word)]
                if not part_words:
                    continue
                header_values = stream_header_values(sorted(part_words, key=lambda word: word[0]))
                compact = re.sub(r"\s+", "", "".join(word[4] for word in part_words))
                if header_values:
                    unique_values = list(dict.fromkeys(header_values))
                    if len(unique_values) > 1:
                        term_issues.append(
                            f"{side_name}欄同一列出現互相衝突的學年標題。"
                        )
                        current_year = ""
                    else:
                        next_year = unique_values[0]
                        if current_year and int(next_year) < int(current_year):
                            term_issues.append(
                                f"{side_name}欄學年標題由{current_year}倒退至{next_year}。"
                            )
                        current_year = next_year
                elif (
                    "學年" in compact
                    and compact
                    and not any(
                        marker in compact
                        for marker in ("入學", "列印", "累計", "排名", "修習", "實得", "操行")
                    )
                ):
                    term_issues.append(f"{side_name}欄有無法辨識的學年標題。")
                    current_year = ""
                row_years[id(row)] = current_year
            return row_years, current_year

        left_year_by_row, left_final_year = assign_stream(
            lambda word: word[0] < 295,
            "左",
            page_carry,
        )
        # The right column begins after the left stream's final header.  This
        # matches pages where the right table starts with continuation rows.
        right_year_by_row, right_final_year = assign_stream(
            lambda word: word[0] >= 295,
            "右",
            left_final_year,
        )
        exclude_exact = {"科目名稱", "累計班排名/人數/百分比", "附", "註", "成績註記"}
        exclude_contains = [
            "修習學分",
            "實得學分",
            "操行成績",
            "累計學分",
            "學年",
            "實得總學分數",
            "實得學分及平均成績",
            "抵免課程",
            "抵免學分",
            "科科科科",
            "修習總學分數",
            "附註",
            "[抵]",
            "[免]",
            "[未]",
            "[P]",
            "[F]",
            "[停]",
        ]
        noise_patterns = [r"^\d+\.\d+\.\d+$", r"^\d+/\d+/\d+(?:\.\d+)?%?$", r"^\d+/\d+%$", r"^\d+\.?\d*\.\d+$"]

        def is_noise_text(text):
            cleaned = re.sub(r"\s+", "", text)
            if not cleaned:
                return True
            if re.fullmatch(r"[0-9./%]+(?:[選必])?", cleaned):
                return True
            if re.fullmatch(r"[選必][0-9./%]+", cleaned):
                return True
            if re.fullmatch(r"[0-9./%]+(?:[，,、][0-9./%]+)*", cleaned):
                return True
            if not re.search(r"[\u4e00-\u9fffA-Za-z]", cleaned) and re.search(r"[0-9]", cleaned):
                return True
            return False

        def is_valid_course_row(name, ctype, s1_cred, s1_score, s2_cred, s2_score, full_row_text):
            if not name:
                return False
            if is_noise_text(name):
                return False
            if len(name) < 2:
                return False
            if re.fullmatch(r"[0-9./%]+", name):
                return False
            if not any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in name):
                return False
            if ctype and ctype not in ("必", "選", "選修", "必修", "免", "抵"):
                if not any(tok in full_row_text for tok in ("必", "選", "修", "抵", "免")):
                    return False
            if not any([s1_cred, s1_score, s2_cred, s2_score]):
                return False
            if is_noise_text(full_row_text):
                return False
            return True

        for row in rows:
            row_words = sorted(row["words"], key=lambda w: w[0])
            full_row_text = "".join([w[4] for w in row_words]).strip()
            if not full_row_text:
                continue
            if full_row_text in exclude_exact:
                continue
            if any(re.match(pat, full_row_text) for pat in noise_patterns):
                continue
            if is_noise_text(full_row_text):
                continue
            left_part = [w for w in row_words if w[0] < 295]
            right_part = [w for w in row_words if w[0] >= 295]

            if left_part:
                name_words = [w for w in left_part if w[0] < 150]
                type_words = [w for w in left_part if 160 <= w[0] < 190]
                s1_cred_words = [w for w in left_part if 190 <= w[0] < 210]
                s1_score_words = [w for w in left_part if 210 <= w[0] < 240]
                s2_cred_words = [w for w in left_part if 240 <= w[0] < 270]
                s2_score_words = [w for w in left_part if 270 <= w[0] < 295]

                name = "".join([w[4] for w in name_words]).strip()
                ctype = "".join([w[4] for w in type_words]).strip()
                s1_cred = "".join([w[4] for w in s1_cred_words]).strip()
                s1_score = "".join([w[4] for w in s1_score_words]).strip()
                s2_cred = "".join([w[4] for w in s2_cred_words]).strip()
                s2_score = "".join([w[4] for w in s2_score_words]).strip()

                # 過濾掉註記行和全是數字/符號的行
                full_row_text = "".join([w[4] for w in row_words]).strip()
                is_page_number = re.match(r"^\d+\s*\d+/\d+\s*[\d.%]+$", full_row_text)

                if (
                    name
                    and not any(term in name for term in exclude_contains)
                    and not any(
                        k in name for k in ["姓名", "系所", "畢業年月", "列印日期", "學號", "[WEB參考用]", "(大學部)"]
                    )
                    and not is_page_number
                    and is_valid_course_row(name, ctype, s1_cred, s1_score, s2_cred, s2_score, full_row_text)
                ):
                    left_year = left_year_by_row.get(id(row), "")
                    if not left_year:
                        term_issues.append("左欄課程列前沒有可驗證的學年標題。")
                    cd = build_course_dict(name, ctype, s1_cred, s1_score, s2_cred, s2_score, left_year)
                    if cd:
                        parsed.append(cd)

            if right_part:
                name_words = [w for w in right_part if w[0] < 440]
                type_words = [w for w in right_part if 440 <= w[0] < 470]
                s1_cred_words = [w for w in right_part if 470 <= w[0] < 490]
                s1_score_words = [w for w in right_part if 490 <= w[0] < 520]
                s2_cred_words = [w for w in right_part if 520 <= w[0] < 545]
                s2_score_words = [w for w in right_part if 545 <= w[0] < 580]

                name = "".join([w[4] for w in name_words]).strip()
                ctype = "".join([w[4] for w in type_words]).strip()
                s1_cred = "".join([w[4] for w in s1_cred_words]).strip()
                s1_score = "".join([w[4] for w in s1_score_words]).strip()
                s2_cred = "".join([w[4] for w in s2_cred_words]).strip()
                s2_score = "".join([w[4] for w in s2_score_words]).strip()

                # 過濾掉註記行和全是數字/符號的行
                full_row_text = "".join([w[4] for w in row_words]).strip()
                is_page_number = re.match(r"^\d+\s*\d+/\d+\s*[\d.%]+$", full_row_text)

                if (
                    name
                    and not any(term in name for term in exclude_contains)
                    and not any(
                        k in name for k in ["姓名", "系所", "畢業年月", "列印日期", "學號", "[WEB參考用]", "(大學部)"]
                    )
                    and not is_page_number
                    and is_valid_course_row(name, ctype, s1_cred, s1_score, s2_cred, s2_score, full_row_text)
                ):
                    right_year = right_year_by_row.get(id(row), "")
                    if not right_year:
                        term_issues.append("右欄課程列前沒有可驗證的學年標題。")
                    cd = build_course_dict(name, ctype, s1_cred, s1_score, s2_cred, s2_score, right_year)
                    if cd:
                        parsed.append(cd)

        return parsed, right_final_year, term_issues

    parse_candidates = []
    for ytol in (3, 6, 9, 12, 18):
        raw_courses = []
        term_issues = []
        page_carry = ""
        for page in all_pages:
            page_courses, page_carry, page_issues = parse_page_words(
                page.get_text("words"),
                y_tol=ytol,
                page_carry=page_carry,
            )
            raw_courses.extend(page_courses)
            term_issues.extend(page_issues)
        candidate_courses = split_two_semester_courses(raw_courses)
        parse_candidates.append(
            {
                "y_tol": ytol,
                "courses": candidate_courses,
                "totals": _totals_from_courses(candidate_courses),
                "duplicate_rows": _duplicate_course_rows(candidate_courses),
                "term_assignment": {
                    "fatal": bool(term_issues),
                    "issues": list(dict.fromkeys(term_issues)),
                    "final_year": page_carry,
                },
            }
        )

    # A wider row tolerance is useful only when it improves agreement with
    # the labelled cumulative fields.  Never select the largest parse or a
    # value merely because it reaches a lower-bound threshold.
    target_totals = []
    if reported_total is not None and not reported_totals["attempted_conflict"]:
        target_totals.append(("completed_attempted_credits", reported_total))
    if reported_earned_total is not None and not reported_totals["earned_conflict"]:
        target_totals.append(("earned_credits", reported_earned_total))

    if target_totals:
        def candidate_rank(item):
            totals = item["totals"]
            distance = sum(abs(totals[key] - expected) for key, expected in target_totals)
            exact = distance <= _TOTAL_TOLERANCE * len(target_totals)
            return (
                1 if item["duplicate_rows"] else 0,
                0 if exact else 1,
                distance,
                item["y_tol"],
            )

        selected_candidate = min(parse_candidates, key=candidate_rank)
    else:
        # Without a trusted cumulative target there is no safe basis for
        # choosing a different row reconstruction.
        selected_candidate = parse_candidates[0]

    parsed_courses = selected_candidate["courses"]
    parsed_course_totals = selected_candidate["totals"]
    parsed_total = parsed_course_totals["completed_attempted_credits"]
    duplicate_rows = selected_candidate["duplicate_rows"]
    term_assignment = selected_candidate["term_assignment"]
    missing_fields = [key for key in ("name", "student_id", "department", "admission_year") if student_info.get(key) in ("", "未辨識")]
    course_code_missing = sum(1 for course in parsed_courses if not course.get("course_code"))
    department_metadata_missing = sum(1 for course in parsed_courses if not course.get("offering_department"))
    diagnostic_warnings = []
    identity_warnings = []
    fatal_warnings = []
    if missing_fields:
        warning = f"成績單基本欄位未辨識：{', '.join(missing_fields)}。"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
    if not detected["department"]:
        warning = "未能從成績單文字辨識主修系所／組別。"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
    if course_code_missing or department_metadata_missing:
        warning = "課程列未提供完整課號／開課系所欄位；非地生逐課分類需人工確認。"
        diagnostic_warnings.append(warning)
        # These fields are warnings for the title-based Earth/Life evaluator,
        # but they are an explicit identity limitation for other programs.
        identity_warnings.append(warning)
    if term_assignment["fatal"]:
        detail = "；".join(term_assignment["issues"])
        warning = f"成績單課程學年／學期判定失敗：{detail}"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)

    reconciliation_issues = []
    attempted_conflict = bool(reported_totals["attempted_conflict"])
    earned_conflict = bool(reported_totals["earned_conflict"])
    transfer_unverified = parsed_course_totals["unverified_transfer_credits"] > _TOTAL_TOLERANCE
    if attempted_conflict:
        warning = "成績單出現互相衝突的全歷年修習總學分，無法選擇可信數值。"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
        reconciliation_issues.append(warning)
    if earned_conflict:
        warning = "成績單出現互相衝突的全歷年實得總學分，無法選擇可信數值。"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
        reconciliation_issues.append(warning)
    if duplicate_rows:
        warning = f"發現 {duplicate_rows} 筆完全重複的課程紀錄，無法安全核對。"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
        reconciliation_issues.append(warning)
    if transfer_unverified:
        warning = "成績單含抵免課程但未提供正式登載的實得學分，無法完成學分核對。"
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
        reconciliation_issues.append(warning)

    def compare_total(label, parsed_value, reported_value):
        if reported_value is None:
            return None
        difference = round(parsed_value - reported_value, 2)
        if abs(difference) <= _TOTAL_TOLERANCE:
            return None
        direction = "過多" if difference > 0 else "過少"
        amount = abs(difference)
        if label == "修習學分":
            warning = (
                f"修習學分核對{direction}：解析出的已結束修習學分 {parsed_value:g}，"
                f"成績單全歷年修習總額 {reported_value:g}，差 {amount:g}。"
            )
        else:
            warning = (
                f"實得學分核對{direction}：解析出的實得學分 {parsed_value:g}，"
                f"成績單全歷年實得總額 {reported_value:g}，差 {amount:g}。"
            )
        diagnostic_warnings.append(warning)
        fatal_warnings.append(warning)
        reconciliation_issues.append(warning)
        return warning

    attempted_mismatch = compare_total(
        "修習學分", parsed_course_totals["completed_attempted_credits"], reported_total
    )
    earned_mismatch = compare_total(
        "實得學分", parsed_course_totals["earned_credits"], reported_earned_total
    )
    has_both_totals = (
        reported_total is not None
        and reported_earned_total is not None
        and not attempted_conflict
        and not earned_conflict
        and not transfer_unverified
    )
    total_reconciled = bool(has_both_totals and not attempted_mismatch and not earned_mismatch and not duplicate_rows)
    if has_both_totals and total_reconciled:
        reconciliation_status = "reconciled"
    elif attempted_conflict or earned_conflict:
        reconciliation_status = "conflict"
    elif transfer_unverified:
        reconciliation_status = "limited"
    elif reported_total is None or reported_earned_total is None:
        reconciliation_status = "not_available"
        warning = "未找到完整的成績單全歷年修習／實得總額，未執行學分核對。"
        diagnostic_warnings.append(warning)
        reconciliation_issues.append(warning)
    else:
        reconciliation_status = "mismatch"

    reconciliation = {
        "available": bool(
            reported_total is not None
            and reported_earned_total is not None
            and not attempted_conflict
            and not earned_conflict
            and not transfer_unverified
        ),
        "status": reconciliation_status,
        "attempted": {
            "reported": reported_total,
            "parsed": parsed_course_totals["completed_attempted_credits"],
            "difference": None if reported_total is None else round(parsed_course_totals["completed_attempted_credits"] - reported_total, 2),
            "reconciled": attempted_mismatch is None and reported_total is not None and not attempted_conflict,
        },
        "earned": {
            "reported": reported_earned_total,
            "parsed": parsed_course_totals["earned_credits"],
            "difference": None if reported_earned_total is None else round(parsed_course_totals["earned_credits"] - reported_earned_total, 2),
            "reconciled": earned_mismatch is None and reported_earned_total is not None and not earned_conflict,
        },
        "duplicate_rows": duplicate_rows,
        "issues": reconciliation_issues,
    }
    totals = {
        **parsed_course_totals,
        "all": parsed_course_totals["all_course_credits"],
        "all_credits": parsed_course_totals["all_course_credits"],
        "attempted": parsed_course_totals["completed_attempted_credits"],
        "completed_attempted": parsed_course_totals["completed_attempted_credits"],
        "completed": parsed_course_totals["completed_attempted_credits"],
        "earned": parsed_course_totals["earned_credits"],
        "in_progress": parsed_course_totals["in_progress_credits"],
        "total": parsed_course_totals["all_course_credits"],
    }
    student_info["parse_diagnostics"] = {
        # ``complete`` now means no fatal parser issue.  Keep all warnings in
        # the diagnostics for display, while course identity limitations are
        # scoped by the engine to programs that actually need them.
        "complete": not fatal_warnings,
        "fatal": bool(fatal_warnings),
        "fatal_warnings": fatal_warnings,
        "identity_complete": not identity_warnings,
        "identity_warnings": identity_warnings,
        "missing_fields": missing_fields,
        "department_detected": bool(detected["department"]),
        "department": detected["department"],
        "track": detected["track"],
        "detected_admission_cohort": detected_admission_cohort,
        "course_count": len(parsed_courses),
        "duplicate_course_rows": duplicate_rows,
        "course_code_missing": course_code_missing,
        "offering_department_missing": department_metadata_missing,
        "reported_total": reported_total,
        "reported_earned_total": reported_earned_total,
        "reported_in_progress_total": reported_totals["reported_in_progress_total"],
        "reported_total_candidates": reported_totals["attempted_candidates"],
        "reported_earned_total_candidates": reported_totals["earned_candidates"],
        "reported_in_progress_total_candidates": reported_totals["in_progress_candidates"],
        "term_assignment": term_assignment,
        "parsed_total": parsed_total,
        "parsed_all_total": parsed_course_totals["all_course_credits"],
        "parsed_earned_total": parsed_course_totals["earned_credits"],
        "parsed_in_progress_total": parsed_course_totals["in_progress_credits"],
        "totals": totals,
        "reconciliation": reconciliation,
        "total_reconciled": total_reconciled,
        "warnings": diagnostic_warnings,
    }
    if not parsed_courses:
        raise ValueError("未能從 PDF 辨識任何課程；請確認檔案為北市大歷年成績單。")
    return student_info, parsed_courses


def build_course_dict(name, ctype, s1_cred, s1_score, s2_cred, s2_score, academic_year):
    # Extract bracket tag prefix if present (e.g., [通選自然], [共同選修], [通選藝術])
    raw_name = str(name or "").strip()
    tag_m = re.match(r"^(\[[^\]]+\])", raw_name)
    prefix_tag = tag_m.group(1) if tag_m else ""
    bracket_tag = prefix_tag  # backward compatibility alias
    clean_name = raw_name[len(prefix_tag):].strip() if prefix_tag else raw_name

    inferred_category = ""
    if prefix_tag:
        if "自然" in prefix_tag:
            inferred_category = "自然、生命與科技領域"
        elif "藝術" in prefix_tag:
            inferred_category = "藝術與美感領域"
        elif "人文" in prefix_tag:
            inferred_category = "人文與文化思考領域"
        elif "公民" in prefix_tag:
            inferred_category = "公民素養與社會探索領域"
        elif "共同選修" in prefix_tag:
            inferred_category = "共同選修"
        elif "校共同" in prefix_tag or "校定必修" in prefix_tag:
            inferred_category = "校共同必修"
        elif "系必修" in prefix_tag or "系定必修" in prefix_tag:
            inferred_category = "專業必修"
        elif "系選修" in prefix_tag or "系定選修" in prefix_tag:
            inferred_category = "專業選修"

    # Normalize course name
    norm_name = normalize_course_name(clean_name or raw_name)
    if not norm_name:
        return None

    # Helper to parse credit
    def parse_credit(c_str):
        if not c_str or c_str == "--" or c_str == "":
            return 0.0
        try:
            return float(c_str)
        except ValueError:
            return 0.0

    c1 = parse_credit(s1_cred)
    c2 = parse_credit(s2_cred)

    # Determine course status.  A transferred row is deliberately not marked
    # earned here because the PDF row has no verified posted-earned field.
    # The adapter applies the same rule and will keep it unconfirmed.
    is_c1_completed = False
    is_c2_completed = False
    is_c1_ip = False
    is_c2_ip = False

    def eval_status(score, credit):
        if credit <= 0:
            return (
                False,
                False,
            )
        if not score or score == "--":
            # Blank/undefined score with credit likely indicates ongoing course enrollment
            return False, True if credit > 0 else (False, False)
        if score == "未":
            return False, True  # In Progress
        if score == "P":
            return True, False  # Completed
        if score == "抵" or score == "免":
            return False, False
        if score == "F" or is_withdrawn_grade(score):
            return False, False  # Failed/Withdraw
        try:
            val = float(score)
            return val >= 60, False  # Completed if >= 60
        except ValueError:
            return False, False

    is_c1_completed, is_c1_ip = eval_status(
        s1_score, c1 if c1 > 0 else 1.0
    )  # Treat 0-credit physical education/guidance as 1.0 for check
    is_c2_completed, is_c2_ip = eval_status(s2_score, c2 if c2 > 0 else 1.0)

    # In some cases, university transcript lists credits and scores differently. Let's make sure:
    total_credit = 0.0
    completed_credit = 0.0
    is_completed = False
    is_ip = False

    if c1 > 0:
        total_credit += c1
        if is_c1_completed:
            completed_credit += c1
        if is_c1_ip:
            is_ip = True

    if c2 > 0:
        total_credit += c2
        if is_c2_completed:
            completed_credit += c2
        if is_c2_ip:
            is_ip = True

    # For 0-credit courses like 大學生活學習與輔導 and 體育 (網球, 桌球, 武術)
    is_zero_credit = total_credit == 0.0
    if is_zero_credit:
        def zero_credit_passed(score):
            token = str(score or "").strip().casefold()
            if token in {"p", "pass", "passed", "免", "免修", "waived"}:
                return True
            try:
                return float(token) >= 60
            except (TypeError, ValueError):
                return False

        if zero_credit_passed(s1_score):
            is_c1_completed = True
        if zero_credit_passed(s2_score):
            is_c2_completed = True
        if s1_score == "未" or s2_score == "未":
            is_ip = True

    is_completed = (completed_credit == total_credit) if not is_zero_credit else (is_c1_completed or is_c2_completed)

    acad_y = str(academic_year or "").strip()
    sem_code = ""
    grade = ""
    has_s1 = bool(s1_cred and s1_cred != "--") or bool(s1_score and s1_score != "--")
    has_s2 = bool(s2_cred and s2_cred != "--") or bool(s2_score and s2_score != "--")
    if has_s1 and not has_s2:
        sem_code = "1"
        grade = s1_score if s1_score and s1_score != "--" else ""
    elif has_s2 and not has_s1:
        sem_code = "2"
        grade = s2_score if s2_score and s2_score != "--" else ""
    elif has_s1 and has_s2:
        sem_code = "1-2"
        grade = s1_score or s2_score or ""
    else:
        sem_code = "1"

    return {
        "name": norm_name,
        "raw_name": raw_name,
        "prefix_tag": prefix_tag,
        "bracket_tag": bracket_tag,
        "clean_name": clean_name,
        "normalized_name": norm_name,
        "inferred_category": inferred_category,
        "academic_year": acad_y,
        "semester_code": sem_code,
        "credits": total_credit,
        "grade": grade,
        "is_completed": is_completed,
        "is_passed": is_completed,
        "is_in_progress": is_ip and not is_completed,
        "is_zero_credit": is_zero_credit,
        "course_code": "",
        "offering_department": "",
        # Filled by the graduation engine once a row is allocated to a
        # department-scoped requirement; keeping stable empty fields here lets
        # CSV/PDF fallbacks share one export schema.
        "identity_status": "",
        "identity_scope": "",
        "identity_reason": "",
        "identity_authority": "",
        "identity_evidence_reference": "",
        "type": ctype if ctype else "選",
        "sem1_credit": s1_cred if s1_cred else "",
        "sem1_score": s1_score if s1_score else "",
        "sem2_credit": s2_cred if s2_cred else "",
        "sem2_score": s2_score if s2_score else "",
        "total_credit": total_credit,
        "completed_credit": completed_credit,
    }


def transcript_to_markdown(
    courses,
    student_info=None,
    *,
    collapsible=True,
    expanded=False,
    summary_title=None,
    include_header=True,
):
    """Generate a clean GFM markdown table of parsed courses for UI preview and auditing."""
    lines = []

    summary_text = summary_title or "成績單中介檢核表格（點擊展開）"
    open_attr = " open" if expanded else ""
    if collapsible:
        lines.append(f"<details{open_attr}>")
        lines.append(f"<summary>{summary_text}</summary>")
        lines.append("")

    def _course_is_done(c):
        if c.get("is_completed") or c.get("is_passed"):
            return True
        st = str(c.get("status") or "").upper()
        return st in ("COMPLETED", "PASSED")

    def _course_is_ip(c):
        if c.get("is_in_progress"):
            return True
        st = str(c.get("status") or "").upper()
        return st == "IN_PROGRESS"

    def _course_done_cred(c):
        if not _course_is_done(c):
            return 0.0
        val = c.get("completed_credit")
        if val is None or val == "":
            val = c.get("earned_credits")
        if val is None or val == "":
            val = c.get("credits")
        try:
            return float(val or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _course_ip_cred(c):
        if not _course_is_ip(c):
            return 0.0
        val = c.get("total_credit")
        if val is None or val == "":
            val = c.get("credits")
        try:
            return float(val or 0.0)
        except (TypeError, ValueError):
            return 0.0

    if student_info and include_header:
        dept = student_info.get("department", "")
        track = student_info.get("track", "")
        dm = student_info.get("double_major")
        dm_str = (
            f"{dm.get('department', '')}{dm.get('track', '') or ''} ({dm.get('status', '修習中')})"
            if dm and isinstance(dm, dict)
            else (dm or "無")
        )
        lines.append("### 學生歷年成績單解析預覽")
        lines.append(f"- **姓名**: {student_info.get('name', '未標示')} | **學號**: {student_info.get('student_id', '未標示')} | **主修**: {dept} {track or ''} | **雙主修**: {dm_str}")
        completed_c = sum(_course_done_cred(c) for c in courses)
        ip_c = sum(_course_ip_cred(c) for c in courses)
        lines.append(f"- **學分核對總計**: 已修畢 **{completed_c:g}** 學分 | 修習中 **{ip_c:g}** 學分 | 累計 **{completed_c + ip_c:g}** 學分")
        lines.append("")

    # Expand each printed semester independently before grouping the display.
    # This is presentation only and does not release any formal attempts.
    display_courses = []
    for original in courses:
        semester_rows = []
        for semester in ("1", "2"):
            credit = original.get(f"sem{semester}_credit")
            if credit in (None, "", "--"):
                continue
            item = dict(original)
            score = original.get(f"sem{semester}_score", "")
            item.update(semester_code=semester, semester=semester, grade=score,
                        credits=credit, term=f"{original.get('academic_year', '')}-{semester}")
            state = _semester_status(score, max(_parse_credit_value(credit) or 0, 1))
            item.update(is_completed=state == "COMPLETED", is_passed=state == "COMPLETED",
                        is_in_progress=state == "IN_PROGRESS", status=state)
            semester_rows.append(item)
        display_courses.extend(semester_rows or [original])
    grouped = grouped_course_rows(display_courses)
    grouped_rows = []
    for title, group in grouped:
        grouped_rows.append((title, None))
        grouped_rows.extend((None, row) for row in group)
    for title, c in grouped_rows:
        if c is None:
            lines.extend(["", f"#### {title}", "",
                          "| 學期 | 課程名稱 | 原始標籤 | 類別 | 學分 | 成績 | 修課狀態 | 歸屬領域 / 審查備註 |",
                          "|:---:|:---|:---:|:---:|:---:|:---:|:---:|:---|"])
            continue
        name = str(c.get("normalized_name") or c.get("course_name") or c.get("name", "") or "").replace("|", "\\|")
        tag = str(c.get("prefix_tag") or c.get("bracket_tag", "") or "").replace("|", "\\|")
        ctype = str(c.get("course_type") or c.get("type", "選") or "選").replace("|", "\\|")
        acad_y = c.get("academic_year", "")
        sem_code = c.get("semester_code", "")
        s1_cred = c.get("sem1_credit", "")
        s1_score = c.get("sem1_score", "")
        s2_cred = c.get("sem2_credit", "")
        s2_score = c.get("sem2_score", "")

        is_done = _course_is_done(c)
        is_ip = _course_is_ip(c)
        status_desc = "已修畢" if is_done else ("修習中" if is_ip else "未通過/停修")

        inferred = c.get("inferred_category") or ""
        if is_ip:
            audit_note = f"{inferred}（修習中，完成後認列）" if inferred else "修習中，完成後認列"
        else:
            audit_note = inferred if inferred else "-"
        audit_note = str(audit_note).replace("|", "\\|")

        cred_val = c.get("credits") if c.get("credits") is not None else c.get("total_credit", 0.0)
        try:
            cred_float = float(cred_val)
            cred_str = f"{cred_float:g}"
        except (TypeError, ValueError):
            cred_str = str(cred_val)
        score_val = c.get("grade") or (s1_score if s1_score and s1_score != "--" else (s2_score if s2_score and s2_score != "--" else ""))
        score_str = str(score_val) if score_val else "-"
        if is_withdrawn_grade(score_val) or str(c.get("status", "")).upper() == "WITHDRAWN":
            status_desc = "已退選／停修"
            audit_note = "已退選，不計入取得學分"

        if sem_code in ("1", "2"):
            sem_str = f"{acad_y}-{sem_code}" if acad_y else str(sem_code)
        elif "-" in str(sem_code):
            sem_str = f"{acad_y}-{sem_code}" if acad_y and not str(sem_code).startswith(str(acad_y)) else str(sem_code)
        elif s1_cred and s1_cred != "--":
            sem_str = f"{acad_y}-1"
        elif s2_cred and s2_cred != "--":
            sem_str = f"{acad_y}-2"
        else:
            sem_str = str(acad_y or "-")
        sem_str = sem_str.replace("|", "\\|")
        if c.get("term") or c.get("semester"):
            sem_str = course_term(c).replace("|", "\\|")

        lines.append(f"| {sem_str} | {name} | {tag or '-'} | {ctype} | {cred_str} | {score_str} | {status_desc} | {audit_note} |")

    if collapsible:
        lines.append("")
        lines.append("</details>")

    return "\n".join(lines)



def split_two_semester_courses(courses):
    split_targets = ["普通生物學", "地球科學"]
    new_courses = []

    def get_sem_status(score, cred_str):
        if not cred_str or cred_str == "--" or cred_str == "":
            return False, False, False
        try:
            c_val = float(cred_str)
        except ValueError:
            c_val = 0.0
        if c_val <= 0.0:
            return False, False, False

        if not score or score == "--":
            return True, False, True
        if score == "未":
            return True, False, True
        if score in ("P", "抵", "免"):
            return True, True, False
        if score == "F" or is_withdrawn_grade(score):
            return True, False, False
        try:
            val = float(score)
            return True, val >= 60.0, False
        except ValueError:
            return True, False, False

    for c in courses:
        target_name = c.get("normalized_name") or c.get("name", "")
        if target_name in split_targets:
            s1_active, s1_done, s1_ip = get_sem_status(c.get("sem1_score"), c.get("sem1_credit"))
            s2_active, s2_done, s2_ip = get_sem_status(c.get("sem2_score"), c.get("sem2_credit"))

            clean_name_base = c.get("clean_name") or target_name
            norm_name_base = c.get("normalized_name") or target_name
            raw_name_base = c.get("raw_name") or target_name
            p_tag = c.get("prefix_tag") or c.get("bracket_tag", "")
            inferred = c.get("inferred_category", "")

            if s1_active or s2_active:
                if s1_active:
                    try:
                        cred = float(c["sem1_credit"])
                    except ValueError:
                        cred = 3.0
                    new_courses.append(
                        {
                            "name": f"{norm_name_base}(上)",
                            "raw_name": f"{raw_name_base}(上)",
                            "prefix_tag": p_tag,
                            "bracket_tag": p_tag,
                            "clean_name": f"{clean_name_base}(上)",
                            "normalized_name": f"{norm_name_base}(上)",
                            "inferred_category": inferred,
                            "course_code": c.get("course_code", ""),
                            "offering_department": c.get("offering_department", ""),
                            "identity_status": c.get("identity_status", ""),
                            "identity_scope": c.get("identity_scope", ""),
                            "identity_reason": c.get("identity_reason", ""),
                            "identity_authority": c.get("identity_authority", ""),
                            "identity_evidence_reference": c.get("identity_evidence_reference", ""),
                            "type": c.get("type", "必"),
                            "academic_year": c.get("academic_year", ""),
                            "semester_code": "1",
                            "credits": cred,
                            "grade": c.get("sem1_score", ""),
                            "sem1_credit": c.get("sem1_credit", ""),
                            "sem1_score": c.get("sem1_score", ""),
                            "sem2_credit": "",
                            "sem2_score": "",
                            "total_credit": cred,
                            "completed_credit": cred if s1_done else 0.0,
                            "is_completed": s1_done,
                            "is_passed": s1_done,
                            "is_in_progress": s1_ip,
                            "is_zero_credit": False,
                        }
                    )
                if s2_active:
                    try:
                        cred = float(c["sem2_credit"])
                    except ValueError:
                        cred = 3.0
                    new_courses.append(
                        {
                            "name": f"{norm_name_base}(下)",
                            "raw_name": f"{raw_name_base}(下)",
                            "prefix_tag": p_tag,
                            "bracket_tag": p_tag,
                            "clean_name": f"{clean_name_base}(下)",
                            "normalized_name": f"{norm_name_base}(下)",
                            "inferred_category": inferred,
                            "course_code": c.get("course_code", ""),
                            "offering_department": c.get("offering_department", ""),
                            "identity_status": c.get("identity_status", ""),
                            "identity_scope": c.get("identity_scope", ""),
                            "identity_reason": c.get("identity_reason", ""),
                            "identity_authority": c.get("identity_authority", ""),
                            "identity_evidence_reference": c.get("identity_evidence_reference", ""),
                            "type": c.get("type", "必"),
                            "academic_year": c.get("academic_year", ""),
                            "semester_code": "2",
                            "credits": cred,
                            "grade": c.get("sem2_score", ""),
                            "sem1_credit": "",
                            "sem1_score": "",
                            "sem2_credit": c.get("sem2_credit", ""),
                            "sem2_score": c.get("sem2_score", ""),
                            "total_credit": cred,
                            "completed_credit": cred if s2_done else 0.0,
                            "is_completed": s2_done,
                            "is_passed": s2_done,
                            "is_in_progress": s2_ip,
                            "is_zero_credit": False,
                        }
                    )
            else:
                half_credit = c["total_credit"] / 2.0
                score_str = "P" if c.get("is_completed") else ("未" if c.get("is_in_progress") else "")
                new_courses.append(
                    {
                        "name": f"{norm_name_base}(上)",
                        "raw_name": f"{raw_name_base}(上)",
                        "prefix_tag": p_tag,
                        "bracket_tag": p_tag,
                        "clean_name": f"{clean_name_base}(上)",
                        "normalized_name": f"{norm_name_base}(上)",
                        "inferred_category": inferred,
                        "course_code": c.get("course_code", ""),
                        "offering_department": c.get("offering_department", ""),
                        "identity_status": c.get("identity_status", ""),
                        "identity_scope": c.get("identity_scope", ""),
                        "identity_reason": c.get("identity_reason", ""),
                        "identity_authority": c.get("identity_authority", ""),
                        "identity_evidence_reference": c.get("identity_evidence_reference", ""),
                        "type": c.get("type", "必"),
                        "academic_year": c.get("academic_year", ""),
                        "semester_code": "1",
                        "credits": half_credit,
                        "grade": score_str,
                        "sem1_credit": str(half_credit),
                        "sem1_score": score_str,
                        "sem2_credit": "",
                        "sem2_score": "",
                        "total_credit": half_credit,
                        "completed_credit": half_credit if c.get("is_completed") else 0.0,
                        "is_completed": c.get("is_completed", False),
                        "is_passed": c.get("is_completed", False),
                        "is_in_progress": c.get("is_in_progress", False),
                        "is_zero_credit": False,
                    }
                )
                new_courses.append(
                    {
                        "name": f"{norm_name_base}(下)",
                        "raw_name": f"{raw_name_base}(下)",
                        "prefix_tag": p_tag,
                        "bracket_tag": p_tag,
                        "clean_name": f"{clean_name_base}(下)",
                        "normalized_name": f"{norm_name_base}(下)",
                        "inferred_category": inferred,
                        "course_code": c.get("course_code", ""),
                        "offering_department": c.get("offering_department", ""),
                        "identity_status": c.get("identity_status", ""),
                        "identity_scope": c.get("identity_scope", ""),
                        "identity_reason": c.get("identity_reason", ""),
                        "identity_authority": c.get("identity_authority", ""),
                        "identity_evidence_reference": c.get("identity_evidence_reference", ""),
                        "type": c.get("type", "必"),
                        "academic_year": c.get("academic_year", ""),
                        "semester_code": "2",
                        "credits": half_credit,
                        "grade": score_str,
                        "sem1_credit": "",
                        "sem1_score": "",
                        "sem2_credit": str(half_credit),
                        "sem2_score": score_str,
                        "total_credit": half_credit,
                        "completed_credit": half_credit if c.get("is_completed") else 0.0,
                        "is_completed": c.get("is_completed", False),
                        "is_passed": c.get("is_completed", False),
                        "is_in_progress": c.get("is_in_progress", False),
                        "is_zero_credit": False,
                    }
                )
        else:
            new_courses.append(c)
    return new_courses


def load_courses_from_csv(csv_path):
    """Load courses from an exported CSV file used as fallback when PDF parsing misses entries.

    Returns: student_info (dict), courses (list)
    """
    import csv

    student_info = {
        "name": "",
        "student_id": "",
        "department": "",
        "admission_year": "",
        "print_date": "",
        "department_family": "",
        "track": None,
        "parse_diagnostics": {"complete": False, "warnings": ["CSV 匯入沒有原始 PDF 欄位完整性證據。"]},
    }
    courses = []
    if not os.path.exists(csv_path):
        return student_info, courses

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            name = r.get("科目名稱") or r.get("name") or ""
            raw_name = name
            tag_m = re.match(r"^(\[[^\]]+\])", raw_name)
            prefix_tag = tag_m.group(1) if tag_m else ""
            clean_name = raw_name[len(prefix_tag):].strip() if prefix_tag else raw_name
            inferred_category = ""
            if prefix_tag:
                if "自然" in prefix_tag:
                    inferred_category = "自然、生命與科技領域"
                elif "藝術" in prefix_tag:
                    inferred_category = "藝術與美感領域"
                elif "人文" in prefix_tag:
                    inferred_category = "人文與文化思考領域"
                elif "公民" in prefix_tag:
                    inferred_category = "公民素養與社會探索領域"
                elif "共同選修" in prefix_tag:
                    inferred_category = "共同選修"

            ctype = r.get("科目屬性") or r.get("type") or ""
            ay = r.get("修課學年") or r.get("academic_year") or ""
            cred_str = r.get("學分數") or r.get("credit") or "0"
            done = str(r.get("是否完成") or r.get("is_completed") or "").strip().lower() in ("true", "1", "yes")
            ip = str(r.get("修讀中") or r.get("is_in_progress") or "").strip().lower() in ("true", "1", "yes")
            try:
                credit = float(cred_str)
            except Exception:
                import re

                m = re.search(r"\d+(?:\.\d+)?", cred_str or "")
                credit = float(m.group(0)) if m else 0.0

            completed_credit = credit if done else 0.0

            course = {
                "name": normalize_course_name(clean_name or name),
                "raw_name": raw_name,
                "prefix_tag": prefix_tag,
                "bracket_tag": prefix_tag,
                "clean_name": clean_name,
                "normalized_name": normalize_course_name(clean_name or name),
                "inferred_category": inferred_category,
                "course_code": r.get("課號") or r.get("course_code") or "",
                "offering_department": r.get("開課系所") or r.get("offering_department") or "",
                "identity_status": r.get("課程身分") or r.get("identity_status") or "",
                "identity_scope": r.get("身分範圍") or r.get("identity_scope") or "",
                "identity_reason": r.get("身分理由") or r.get("identity_reason") or "",
                "identity_authority": r.get("核准單位") or r.get("identity_authority") or "",
                "identity_evidence_reference": r.get("證據引用") or r.get("identity_evidence_reference") or "",
                "type": ctype if ctype else "選",
                "academic_year": ay,
                "semester_code": "1",
                "credits": credit,
                "grade": "P" if done else ("未" if ip else ""),
                "sem1_credit": "",
                "sem1_score": "",
                "sem2_credit": str(credit) if credit else "",
                "sem2_score": "P" if done else ("未" if ip else ""),
                "total_credit": credit,
                "completed_credit": completed_credit,
                "is_completed": bool(done),
                "is_passed": bool(done),
                "is_in_progress": bool(ip) and not done,
                "is_zero_credit": credit == 0.0,
            }
            courses.append(course)

    courses = split_two_semester_courses(courses)
    return student_info, courses
