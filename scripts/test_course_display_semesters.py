"""Synthetic display regression: no personal transcript data."""
import sys
from pathlib import Path
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from course_display import grouped_course_rows

rows = [dict(name='大學生活學習與輔導', academic_year=str(year),
             type='必', total_credit=0, sem1_credit='0', sem1_score='89',
             sem2_credit='0', sem2_score='88') for year in (113, 114)]
original = deepcopy(rows)
groups = dict(grouped_course_rows(rows))
assert list(groups) == ['113-1 學期', '113-2 學期', '114-1 學期', '114-2 學期']
assert [r['grade'] for group in groups.values() for r in group] == ['89', '88', '89', '88']
assert sum(r['total_credit'] for group in groups.values() for r in group) == 0
assert rows == original
single = dict(rows[0], sem2_credit='', sem2_score='')
assert len(dict(grouped_course_rows([single]))) == 1
expanded = [r for group in groups.values() for r in group]
assert sum(len(group) for _, group in grouped_course_rows(expanded)) == 4
categories = [dict(name='sample', inferred_category=category) for category in
              ('其他通識', '藝術與美感領域', '自然通識', '人文通識', '公民通識')]
labels = [label for label, _ in grouped_course_rows(categories)]
assert labels[-2:] == ['通識課程｜藝術與美感領域', '通識課程｜其他通識／共同選修']
print('PASS: semester rows, grades, zero totals, blank semester, idempotence, category order')
