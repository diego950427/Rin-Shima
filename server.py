"""Local-only UI and in-memory PDF parsing. No uploads are written to disk."""
import json
import sys
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'core'))
from pdf_parser import parse_transcript_pdf, MAX_PDF_BYTES
from course_display import grouped_course_rows, course_term, course_type_label
from audit_bridge import audit_courses


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass  # Never log uploaded content or student details.

    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/options':
            rules = json.loads((ROOT / 'core/rules_config.json').read_text(encoding='utf-8'))
            return self.send_json({'years': sorted(rules['handbooks']), 'programs': [
                '地生（地球環境）', '地生（生命科學）', '物化（電子物理）',
                '物化（應用化學）', '數學', '資科']})
        if path not in ('/', '/index.html', '/app.js', '/forms.js') and not path.startswith(('/assets/', '/css/', '/js/')):
            return self.send_error(404)
        resolved = Path(self.translate_path(path)).resolve()
        if not resolved.is_relative_to(ROOT) or resolved.is_dir() and path != '/':
            return self.send_error(404)
        relative = resolved.relative_to(ROOT)
        if relative.parts and relative.parts[0] not in ('assets', 'css', 'js', 'index.html', 'app.js', 'forms.js'):
            return self.send_error(404)
        super().do_GET()

    def do_POST(self):
        # Local browser requests only; reject cross-origin upload attempts.
        host = self.headers.get('Host', '')
        if host not in ('127.0.0.1:8521', 'localhost:8521'):
            return self.send_json({'error': '不允許的連線來源。'}, 403)
        if self.headers.get('Origin') not in (None, f'http://{host}'):
            return self.send_json({'error': '不允許跨網站上傳。'}, 403)
        route = urlparse(self.path)
        if route.path not in ('/api/transcript', '/api/audit'):
            return self.send_json({'error': '找不到功能。'}, 404)
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            size = 0
        if not 0 < size <= MAX_PDF_BYTES:
            return self.send_json({'error': '請上傳 20 MB 以內的 PDF。'}, 413)
        payload = self.rfile.read(size)
        if not payload.startswith(b'%PDF-'):
            return self.send_json({'error': '檔案不是有效的 PDF。'}, 400)
        try:
            info, courses = parse_transcript_pdf(payload)
            if route.path == '/api/audit':
                try:
                    settings = json.loads(parse_qs(route.query).get('settings', ['{}'])[0])
                    if self.headers.get('X-Confirm-Courses') != 'yes':
                        return self.send_json({'error':'請先確認成績。'}, 400)
                    return self.send_json(audit_courses(courses, info.get('parse_diagnostics', {}), settings))
                except ValueError as error:
                    return self.send_json({'error':str(error)}, 422)
            groups = []
            for label, rows in grouped_course_rows(courses):
                groups.append({'label': label, 'rows': [{
                    'term': course_term(r), 'name': r.get('clean_name') or r.get('name', ''),
                    'kind': course_type_label(r), 'credits': r.get('total_credit', ''),
                    'grade': r.get('score', r.get('grade', r.get('eval_status', ''))),
                } for r in rows]})
            diagnostics = info.get('parse_diagnostics', {})
            self.send_json({'groups': groups, 'count': len(courses),
                            'warnings': diagnostics.get('warnings', []),
                            'complete': diagnostics.get('complete', False)})
        except Exception:
            self.send_json({'error': '無法解析這份 PDF，請確認是可選取文字的北市大歷年成績單。'}, 422)


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False


if __name__ == '__main__':
    print('UT-checker: http://localhost:8521/', flush=True)
    LocalServer(('127.0.0.1', 8521), Handler).serve_forever()
