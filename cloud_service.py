"""Session-scoped Cloud transport; never persist or globally cache transcripts."""
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'core'))
from pdf_parser import parse_transcript_pdf, MAX_PDF_BYTES
from course_display import grouped_course_rows, course_term, course_type_label
from audit_bridge import audit_courses


def handle_request(request):
    request_id = str(request.get('id', ''))[:100]
    try:
        encoded = request.get('pdf', '')
        if not isinstance(encoded, str) or len(encoded) > ((MAX_PDF_BYTES + 2) // 3) * 4:
            raise ValueError('請上傳 20 MB 以內的 PDF。')
        payload = base64.b64decode(encoded, validate=True)
        if not 0 < len(payload) <= MAX_PDF_BYTES or not payload.startswith(b'%PDF-'):
            raise ValueError('請選擇有效的 PDF。')
        info, courses = parse_transcript_pdf(payload)
        if request.get('operation') == 'audit':
            if request.get('confirmed') is not True:
                raise ValueError('請先確認成績。')
            settings = request.get('settings')
            if not isinstance(settings, dict):
                raise ValueError('請先套用設定。')
            data = audit_courses(courses, info.get('parse_diagnostics', {}), settings)
        elif request.get('operation') == 'transcript':
            groups = [{'label': label, 'rows': [{
                'term': course_term(r), 'name': r.get('clean_name') or r.get('name', ''),
                'kind': course_type_label(r), 'credits': r.get('total_credit', ''),
                'grade': r.get('score', r.get('grade', r.get('eval_status', ''))),
            } for r in rows]} for label, rows in grouped_course_rows(courses)]
            diagnostics = info.get('parse_diagnostics', {})
            data = {'groups': groups, 'count': len(courses),
                    'warnings': diagnostics.get('warnings', []),
                    'complete': diagnostics.get('complete', False)}
        else:
            raise ValueError('找不到功能。')
        return {'id': request_id, 'status': 200, 'data': json.loads(json.dumps(data, default=str))}
    except ValueError as error:
        return {'id': request_id, 'status': 422, 'data': {'error': str(error)}}
    except Exception:
        return {'id': request_id, 'status': 422, 'data': {'error': '無法解析這份 PDF，請核對格式後重試。'}}
