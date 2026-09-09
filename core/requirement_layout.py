"""Presentation-only requirement groups; never allocates or recounts credits."""
from html import escape
from decimal import Decimal, InvalidOperation


def category_title(item):
    bucket = str(item.get("bucket", ""))
    if bucket.startswith("ge_") or bucket == "university_compulsory":
        return "通識課程"
    if bucket == "common_compulsory" or bucket.startswith("common_alternative"):
        return "系共同必修"
    if bucket.endswith(":compulsory"):
        return bucket.split(":")[0] + "領域必修"
    if bucket == "domain_required":
        return "主修領域必修"
    if bucket == "domain_elective":
        return str(item.get("name") or "主修領域選修")
    if bucket in {"department_professional", "common_elective"}:
        return "系內其他選修"
    if bucket == "free_elective":
        return "自由選修"
    return "其他主修要求"


def deficit_markup(items):
    missing = []
    for item in items:
        try:
            amount = Decimal(str(item.get("deficit")))
        except InvalidOperation:
            if item.get("status") != "PASS":
                missing.append(f'{escape(str(item.get("name", "此項要求")))}：缺額待核對')
            continue
        if not amount.is_finite():
            missing.append(f'{escape(str(item.get("name", "此項要求")))}：缺額待核對')
        elif amount > 0:
            missing.append(f'{escape(str(item.get("name", "此項要求")))}：尚缺 {amount.normalize():f} 學分')
        elif item.get("status") != "PASS":
            missing.append(f'{escape(str(item.get("name", "此項要求")))}：學分缺額 0，另有條件待核對')
    if not missing:
        return '<p class="snapshot-category-complete">本區學分要求已完成。</p>'
    summary = ('<strong>本區尚缺</strong><ul>' + ''.join(f'<li>{text}</li>' for text in missing) + '</ul>')
    candidates = []
    seen = set()
    for item in items:
        if item.get('status') in {'PASS', 'NOT_APPLICABLE'}:
            continue
        try:
            deficit = Decimal(str(item.get('deficit')))
            if deficit.is_finite() and deficit <= 0:
                continue
        except InvalidOperation:
            continue  # Unknown quota is not a known course deficit.
        for course in item.get('courses', ()):
            if str(course.get('status', '')).upper() not in {'NOT_ATTEMPTED', 'NOT_TAKEN'}:
                continue
            name = str(course.get('course_name') or course.get('name') or '').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            elective = ('選修' in str(item.get('kind', '')) or 'elective' in str(item.get('bucket', ''))
                        or item.get('bucket') == 'department_professional' or 'alternative' in str(item.get('bucket', '')))
            candidates.append(f'<li>{escape(name)}<span>{"可選課程，非全部必修" if elective else "尚未出現在成績紀錄"}</span></li>')
    note = '<small>依目前已採計學分列示；修習中尚未計入，各項缺額不重複加總。</small>'
    listing = '<ul class="snapshot-missing-courses">' + ''.join(candidates) + '</ul>' if candidates else ''
    if len(candidates) >= 3:
        return ('<details class="snapshot-category-deficit snapshot-deficit-disclosure"><summary>'
                + summary + f'<span class="snapshot-deficit-toggle">查看未修課程（{len(candidates)} 門）</span></summary>'
                + '<div class="snapshot-deficit-content">' + listing + note + '</div></details>')
    return '<footer class="snapshot-category-deficit">' + summary + listing + note + '</footer>'


def requirement_section(item):
    owner = str(item.get("owner", "")).upper()
    identity = str(item.get("requirement_id", ""))
    if owner == "MINOR" or identity.startswith("minor:"):
        return "minor"
    if owner in {"TARGET", "SECONDARY"} or identity.startswith("target:"):
        return "secondary"
    return "primary"


# Adapted from the user-provided Uiverse.io / Codewithvinay .card example.
# Fluid size and separate theme surfaces preserve the supplied relief effect.
RELIEF_CSS = """
.snapshot-requirement-section {
  --relief-surface: #e4eee8;
  --relief-low: #becbc3;
  --relief-high: #ffffff;
  --relief-ink: #20332b;
  --relief-muted: #40594c;
  --relief-line: #a7bdb0;
  margin-block: 32px 56px;
  padding: 8px 20px 20px;
  min-width: 0;
}
.snapshot-requirement-section--secondary, .snapshot-requirement-section--minor {
  --relief-surface: #ece7f4;
  --relief-low: #c8bfd6;
  --relief-high: #ffffff;
  --relief-ink: #44305f;
  --relief-muted: #614d78;
  --relief-line: #bbacd0;
}
.snapshot-requirement-section > h3 { color: var(--relief-ink); margin: 0 0 24px; font-size: 1.4rem; }
.snapshot-category {
  background: var(--relief-surface); color: var(--relief-ink);
  padding: 28px; margin-block: 28px 40px; border-radius: 32px;
  box-shadow: 12px 12px 28px var(--relief-low), -10px -10px 24px var(--relief-high);
  min-width: 0;
}
.snapshot-category > h4 { font-size: 1.25rem; margin: 0 0 20px; color: var(--relief-ink); }
.snapshot-category-deficit { background: #a52232; color: #fff; padding: 20px 24px; border-radius: 16px; margin-top: 24px; }
.snapshot-category-deficit ul { margin: 8px 0 12px; padding-left: 24px; }
.snapshot-category-deficit small { color: #fff; }
.snapshot-deficit-disclosure { padding: 0; }
.snapshot-deficit-disclosure > summary { padding: 20px 24px; cursor: pointer; list-style: none; min-height: 48px; }
.snapshot-deficit-disclosure > summary::-webkit-details-marker { display: none; }
.snapshot-deficit-disclosure > summary:focus-visible { outline: 3px solid currentColor; outline-offset: 4px; }
.snapshot-deficit-toggle { display: inline-block; text-decoration: underline; text-underline-offset: 4px; }
.snapshot-deficit-toggle::after { content: ' ＋'; }
.snapshot-deficit-disclosure[open] .snapshot-deficit-toggle::after { content: ' −'; }
.snapshot-deficit-content { padding: 0 24px 24px; }
.snapshot-missing-courses li { margin-block: 12px; }
.snapshot-missing-courses span { display: block; font-size: .85em; }
.snapshot-category-complete { color: var(--relief-ink); }
.snapshot-requirement-section .snapshot-requirement-expander {
  background: var(--relief-surface); color: var(--relief-ink);
  border: 0; border-bottom: 1px solid var(--relief-line); border-radius: 0; margin-block: 8px;
  box-shadow: none;
  min-width: 0; height: auto;
  --snapshot-text: var(--relief-ink); --snapshot-muted: var(--relief-muted);
  --snapshot-border: var(--relief-line); --snapshot-card: var(--relief-surface);
}
.snapshot-requirement-section .snapshot-requirement-expander > summary {
  padding: 24px 28px; min-height: 72px; gap: 12px 24px;
}
.snapshot-requirement-section .snapshot-requirement-title { color: var(--relief-ink); overflow-wrap: anywhere; }
.snapshot-requirement-section .snapshot-requirement-progress { color: var(--relief-muted); }
.snapshot-requirement-section .snapshot-requirement-body { padding: 24px 28px 32px; }
.snapshot-requirement-section .snapshot-requirement-expander > summary:focus-visible {
  outline: 3px solid var(--relief-ink); outline-offset: 4px; border-radius: 32px;
}
html[data-utaipei-theme="dark"] .snapshot-requirement-section {
  --relief-surface: #233831; --relief-low: #0b1512; --relief-high: #304a40;
  --relief-ink: #e3f0e8; --relief-muted: #c1d3c8; --relief-line: #516e5e;
}
html[data-utaipei-theme="dark"] .snapshot-requirement-section--secondary,
html[data-utaipei-theme="dark"] .snapshot-requirement-section--minor {
  --relief-surface: #332a46; --relief-low: #140f20; --relief-high: #4b3e61;
  --relief-ink: #f1e9ff; --relief-muted: #d5c5ea; --relief-line: #73618a;
}
@media (prefers-color-scheme: dark) {
  html:not([data-utaipei-theme="light"]) .snapshot-requirement-section {
    --relief-surface: #233831; --relief-low: #0b1512; --relief-high: #304a40;
    --relief-ink: #e3f0e8; --relief-muted: #c1d3c8; --relief-line: #516e5e;
  }
  html:not([data-utaipei-theme="light"]) .snapshot-requirement-section--secondary,
  html:not([data-utaipei-theme="light"]) .snapshot-requirement-section--minor {
    --relief-surface: #332a46; --relief-low: #140f20; --relief-high: #4b3e61;
    --relief-ink: #f1e9ff; --relief-muted: #d5c5ea; --relief-line: #73618a;
  }
}
@media (max-width: 600px) {
  .snapshot-requirement-section { padding: 8px 8px 16px; margin-block: 24px 40px; }
  .snapshot-category { padding: 16px; border-radius: 24px; box-shadow: 6px 6px 16px var(--relief-low), -5px -5px 14px var(--relief-high); }
  .snapshot-category-deficit { padding: 16px; }
  .snapshot-requirement-section .snapshot-requirement-expander > summary { padding: 20px; }
  .snapshot-requirement-section .snapshot-requirement-body { padding: 16px 16px 24px; }
}
@media (forced-colors: active) {
  .snapshot-requirement-section .snapshot-requirement-expander { box-shadow: none; border: 1px solid CanvasText; }
}
"""


def grouped_requirements_markup(view, render_contents):
    groups = {"primary": [], "secondary": [], "minor": []}
    for item in view.get("requirements", ()):
        groups[requirement_section(item)].append(item)
    output = [f"<style>{RELIEF_CSS}</style>"]
    for key, title in (("primary", "主修畢業要求"), ("secondary", "雙主修課程要求"), ("minor", "輔系課程要求")):
        if not groups[key]:
            continue
        categories = {}
        for item in groups[key]:
            category = category_title(item) if key == "primary" else title
            categories.setdefault(category, []).append(item)
        ordered = ["通識課程", "系共同必修"]
        ordered += [name for name in categories if "領域必修" in name]
        ordered += [name for name in categories if "領域選修" in name]
        ordered += ["系內其他選修", "自由選修"]
        ordered += [name for name in categories if name not in ordered]
        cards = []
        for category in dict.fromkeys(ordered):
            if category not in categories:
                continue
            subset = {**view, "requirements": tuple(categories[category])}
            cards.append(f'<div class="snapshot-category"><h4>{escape(category)}</h4>'
                         f'{render_contents(subset)}{deficit_markup(categories[category])}</div>')
        output.append(f'<section class="snapshot-requirement-section snapshot-requirement-section--{key}" aria-labelledby="requirements-{key}">'
                      f'<h3 id="requirements-{key}">{escape(title)}</h3>{"".join(cards)}</section>')
    return "".join(output)
