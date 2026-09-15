"""Publish a verified crawler snapshot's display fields only (no login data)."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def build(source):
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    validation = json.loads((source / 'validation.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'COMPLETE' and validation['status'] == 'PASS'
    query = manifest['query']
    year, semester = int(query['academic_year']), int(query['semester'])
    assert query['campus'] == '博愛'
    raw = source / 'courses_listed.json'
    records = json.loads(raw.read_text(encoding='utf-8'))
    assert len(records) == manifest['listed_rows'] == validation['listed_rows']
    departments = {q['id']: q['department_name'] for q in manifest['queries']}
    fields = ('class_name', 'course_code', 'course_name_zh', 'credits',
              'required_elective', 'teaching_raw', 'mixed_classes', 'remarks', 'campus')
    courses = []
    for record in records:
        assert int(record['academic_year']) == year and int(record['semester']) == semester
        assert record['campus'] == '博愛' and record['status'] == 'listed'
        item = {key: record.get(key, '') for key in fields}
        item['departments'] = sorted({departments[key] for key in record['source_queries']})
        item['official_category'] = record.get('category', '')
        courses.append(item)
    result = dict(query=query, captured_at=manifest['finished_at'],
                  source_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(), courses=courses)
    destination = ROOT / 'assets' / f'semester_courses_{year}_{semester}.json'
    destination.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'{year}-{semester}: {len(courses)} verified listed courses published')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    build(parser.parse_args().source)
