"""Synthetic regression for independently partitioned program charts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_service  # Establish the same core import boundary as deployment.
import audit_bridge

SETTINGS=dict(primary='地生（地球環境）', handbook='113', admission='113',
              plan='雙主修', targetYear='114', target='資科')

def check():
    original = audit_bridge.evaluate
    snapshots = []
    def capture(request):
        snapshot = original(request)
        snapshots.append(snapshot)
        return snapshot
    audit_bridge.evaluate = capture
    data = audit_bridge.audit_courses([], {}, SETTINGS)
    for key, required in [('total',128), ('secondaryTotal',40)]:
        total=data[key]
        assert total['partition_valid'] and total['mapping_valid']
        assert total['required']==required
        assert sum(r['credits'] for r in total['records']+total['gaps'])==required
    assert {r['category'] for r in data['secondaryTotal']['gaps']}=={'指定必修','其他開設課程'}
    courses=[dict(course_name=name,term='114-1',credits=3,grade=80,course_type='必修')
             for name in ['C程式設計','Java程式設計','演算法']]
    earned=audit_bridge.audit_courses(courses,{},SETTINGS)
    secondary=earned['secondaryTotal']
    assert secondary['completed']==9 and secondary['required']==40
    assert secondary['partition_valid']
    assert sum(r['credits'] for r in secondary['gaps'])==31
    assert len(secondary['records'])==3
    if '--preview' in sys.argv:
        import json
        preview=Path(__file__).resolve().parents[2]/'ui-verification/program-progress-data.json'
        preview.write_text(json.dumps(earned,ensure_ascii=False),encoding='utf-8')
    single=audit_bridge.audit_courses([], {}, {**SETTINGS,'plan':'單主修'})
    assert single['secondaryTotal'] is None
    print('PASS: independent primary 128 / secondary 40 partitions; single-major has no switch.')

if __name__=='__main__':
    check()
