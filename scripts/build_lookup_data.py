"""Join only same-term, same-campus, same-code, same-name public categories."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
source=json.loads((ROOT/'core/data/semester_courses_115_1.json').read_text(encoding='utf-8'))
catalog=json.loads((ROOT/'core/data/public_course_catalog.json').read_text(encoding='utf-8'))
categories={}
for c in catalog['courses']:
    if c['term']=='115-1':
        key=(c['campus'],c['selection_code'],c['course_name'])
        categories.setdefault(key,set()).add(c['official_category'])
matched=0
for c in source['courses']:
    found=categories.get((c['campus'],c['course_code'],c['course_name_zh']),set())
    c['official_category']=next(iter(found)) if len(found)==1 else ''
    matched+=bool(c['official_category'])
(ROOT/'assets/semester_courses_115_1.json').write_text(json.dumps(source,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
print(f'{len(source["courses"])} courses; {matched} exact category matches')
