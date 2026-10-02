"""Revocable link-access library behind Caddy. Bind only to loopback."""
from collections import deque
from contextlib import closing
from datetime import timedelta
import hashlib
import hmac
import os
import re
from pathlib import Path
import sqlite3
import threading
import time

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException

from .ai import AIError, Organizer, Settings
from .__main__ import read_document
from .documents import Documents, SUFFIXES

ROOT = Path(__file__).resolve().parent
FIELDS = '''f.id, f.original_filename, f.discord_channel, f.discord_channel_id,
            f.category, f.uploaded_at, f.file_size_bytes, f.version, f.duplicate_type,
            CASE WHEN d.source_sha256=f.sha256 THEN d.status ELSE 'pending' END AS content_status'''


def create_app(config=None, ai=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(DB_PATH=ROOT.parent / 'project_hub.db', STORAGE=ROOT.parent / 'storage',
                      SHARE_HASH='', PUBLIC_ORIGIN='', PUBLIC_ACCESS=False, MAX_CONTENT_LENGTH=8192,
                      DOCUMENT_CACHE=None,
                      SHARE_URL_FILE=Path.home()/'.config/project-hub/library-share-url.txt',
                      SESSION_COOKIE_NAME='ph_library', SESSION_COOKIE_PATH='/library/',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(days=14))
    if config:
        app.config.update(config)
    documents=Documents(app.config['DB_PATH'],app.config['STORAGE'],
                        app.config['DOCUMENT_CACHE'] or Path(app.config['DB_PATH']).parent/'library-text.db')
    app.extensions['documents']=documents
    ai_lock = threading.Lock()
    access_lock = threading.Lock()
    access_attempts = deque()

    def connect():
        db = sqlite3.connect(Path(app.config['DB_PATH']).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @app.before_request
    def authenticate():
        if request.remote_addr not in {'127.0.0.1', '::1'}:
            abort(403)
        if request.method == 'POST':
            if (not app.config['PUBLIC_ORIGIN'] or request.headers.get('Origin') != app.config['PUBLIC_ORIGIN']
                    or request.headers.get('X-Requested-With') != 'ProjectHub' or not request.is_json):
                abort(403)
        if request.path in {'/library/', '/library/assets/app.js', '/library/assets/style.css', '/library/api/access'}:
            return
        if app.config['PUBLIC_ACCESS']:
            return
        current = app.config['SHARE_HASH']
        saved = session.get('access', '')
        if not current or not isinstance(saved, str) or not hmac.compare_digest(current, saved):
            abort(401)

    @app.after_request
    def protect(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error_response(error):
        if isinstance(error, HTTPException):
            code = error.code
            message = {401: '전달받은 공유 링크로 열어 주세요. 링크가 변경되었다면 새 링크가 필요합니다.', 403: '접근할 수 없습니다.',
                       404: '자료를 찾을 수 없습니다.', 413: '질문이 너무 깁니다.'}.get(code, '요청을 확인해 주세요.')
        elif isinstance(error, AIError):
            code, message = 422, str(error)
        elif isinstance(error, sqlite3.Error):
            code, message = 503, '자료 목록을 준비 중입니다. 잠시 후 다시 시도해 주세요.'
        else:
            code, message = 500, '요청을 처리하지 못했습니다.'
            app.logger.error('LIBRARY_ERROR type=%s', type(error).__name__)
        return jsonify(error=message), code

    @app.get('/library/')
    def index():
        return send_file(ROOT / 'static/index.html')

    @app.get('/library/assets/<name>')
    def asset(name):
        if name not in {'app.js', 'style.css'}:
            abort(404)
        return send_file(ROOT / 'static' / name)

    @app.post('/library/api/access')
    def access():
        with access_lock:
            now = time.monotonic()
            while access_attempts and now-access_attempts[0]>60:
                access_attempts.popleft()
            if len(access_attempts)>=60:
                return jsonify(error='접속 요청이 많습니다. 1분 후 다시 열어 주세요.'), 429
            access_attempts.append(now)
        data = request.get_json()
        token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not 32<=len(token)<=128:
            abort(401)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not app.config['SHARE_HASH'] or not hmac.compare_digest(digest, app.config['SHARE_HASH']):
            abort(401)
        session.clear()
        session['access'] = digest
        session.permanent = True
        return jsonify(ok=True)

    @app.get('/library/api/files')
    def files():
        query = request.args.get('q', '').strip()[:100]
        channel = request.args.get('channel', '')[:100]
        try:
            offset = max(0, min(int(request.args.get('offset', '0')), 100000))
        except ValueError:
            abort(400)
        conditions, params = [], []
        if query:
            condition='''(instr(lower(f.original_filename),lower(?))>0 OR EXISTS
                (SELECT 1 FROM content_pages p JOIN content_documents x ON x.file_id=p.file_id
                 WHERE p.file_id=f.id AND x.source_sha256=f.sha256 AND x.status IN ('indexed','partial')
                 AND instr(p.search_body,?)>0)'''
            params.extend([query, query.casefold()])
            for file_id,sha in documents.matches(query):
                condition+=' OR (f.id=? AND f.sha256=?)'
                params.extend([file_id,sha])
            conditions.append(condition+')')
        if channel:
            conditions.append('f.discord_channel_id=?')
            params.append(channel)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with closing(connect()) as db:
            rows = db.execute('SELECT ' + FIELDS + ' FROM files f LEFT JOIN content_documents d ON d.file_id=f.id'
                              + where + ' ORDER BY f.id DESC LIMIT 31 OFFSET ?', params + [offset]).fetchall()
            channels = [dict(r) for r in db.execute('SELECT DISTINCT discord_channel_id AS id, discord_channel AS name FROM files ORDER BY name')]
        items=[dict(r) for r in rows[:30]]
        for item in items:
            if Path(item['original_filename']).suffix.lower() in SUFFIXES:
                extra=documents.get(item['id'])
                if extra: item['content_status']=extra['status']
        return jsonify(files=items, more=len(rows)>30, channels=channels)

    def file_row(db, file_id):
        row = db.execute('SELECT ' + FIELDS + ', f.local_path FROM files f LEFT JOIN content_documents d ON d.file_id=f.id WHERE f.id=?', (file_id,)).fetchone()
        if not row:
            abort(404)
        return dict(row)

    @app.get('/library/api/files/<int:file_id>')
    def detail(file_id):
        with closing(connect()) as db:
            db.execute('BEGIN')
            row = file_row(db, file_id)
            row.pop('local_path')
            drive_id=db.execute('SELECT google_drive_file_id FROM files WHERE id=?',(file_id,)).fetchone()[0]
            row['drive_url']=f'https://drive.google.com/file/d/{drive_id}/view' if isinstance(drive_id,str) and re.fullmatch(r'[A-Za-z0-9_-]+',drive_id) else None
            pages, size, truncated = [], 0, False
            if row['content_status'] in {'indexed', 'partial'}:
                for p in db.execute('''SELECT p.page, p.body, l.label FROM content_pages p
                                     LEFT JOIN content_locations l ON l.file_id=p.file_id AND l.page=p.page
                                     WHERE p.file_id=? ORDER BY p.page''', (file_id,)):
                    if size >= 60000 or len(pages) >= 100:
                        truncated = True
                        break
                    text = p['body'][:60000-size]
                    truncated |= len(text)<len(p['body'])
                    pages.append({'page': p['page'], 'label': p['label'] or f"추출 구간 {p['page']}", 'text': text})
                    size += len(text)
            row.update(pages=pages, partial=truncated or row['content_status']=='partial')
            extra=documents.get(file_id)
            if extra:
                row.update(pages=extra['pages'],partial=extra['status']=='partial',content_status=extra['status'])
            row['image_preview']=Path(row['original_filename']).suffix.lower() in {'.png','.jpg','.jpeg','.gif','.webp'}
            return jsonify(row)

    def local_file(file_id):
        with closing(connect()) as db:
            row = file_row(db, file_id)
        path = Path(row['local_path'])
        if not path.is_absolute():
            path = ROOT.parent / path
        path = path.resolve()
        storage = Path(app.config['STORAGE']).resolve()
        if not path.is_relative_to(storage) or not path.is_file():
            abort(404)
        return row,path

    @app.get('/library/files/<int:file_id>/download')
    def download(file_id):
        row,path=local_file(file_id)
        # Never render arbitrary collected HTML/SVG under the authenticated origin.
        return send_file(path, as_attachment=True, download_name=row['original_filename'], mimetype='application/octet-stream')

    @app.get('/library/files/<int:file_id>/preview')
    def preview(file_id):
        row,path=local_file(file_id)
        with path.open('rb') as f: magic=f.read(16)
        mime=None
        if magic.startswith(b'\x89PNG\r\n\x1a\n'): mime='image/png'
        elif magic.startswith(b'\xff\xd8\xff'): mime='image/jpeg'
        elif magic[:6] in {b'GIF87a',b'GIF89a'}: mime='image/gif'
        elif magic[:4]==b'RIFF' and magic[8:12]==b'WEBP': mime='image/webp'
        if mime is None: abort(404)
        return send_file(path,mimetype=mime)

    @app.get('/library/api/status')
    def status():
        if ai is None:
            return jsonify(enabled=False)
        return jsonify(enabled=ai.settings.enabled, daily_limit=ai.settings.daily_limit,
                       monthly_limit=ai.settings.monthly_limit, **ai.ledger.counts())

    @app.get('/library/api/share-link')
    def share_link():
        if app.config['PUBLIC_ACCESS']:
            return jsonify(url=app.config['PUBLIC_ORIGIN']+'/library/')
        url=Path(app.config['SHARE_URL_FILE']).read_text().strip()
        if not url.startswith(app.config['PUBLIC_ORIGIN']+'/library/#key='):
            abort(503)
        return jsonify(url=url)

    @app.post('/library/api/files/<int:file_id>/ai')
    def ask(file_id):
        data = request.get_json()
        if not isinstance(data, dict) or data.get('task') not in {'summary', 'ask'}:
            abort(400)
        question = data.get('question', '')
        if not isinstance(question, str) or len(question)>1000 or (data['task']=='ask' and not question.strip()):
            abort(400)
        if ai is None:
            raise AIError('AI 기능이 아직 준비되지 않았습니다.')
        if not ai_lock.acquire(blocking=False):
            return jsonify(error='다른 자료를 정리 중입니다. 잠시 후 다시 시도해 주세요.'), 429
        try:
            extra=documents.get(file_id)
            if extra:
                if not extra['pages']:
                    raise AIError('이 문서에서 읽을 수 있는 글자를 찾지 못했습니다.')
                sources,remaining=[],ai.settings.max_chars
                partial=extra['status']=='partial'
                for page in extra['pages']:
                    if remaining<=0 or len(sources)>=24:
                        partial=True
                        break
                    text=page['text'][:remaining]
                    partial|=len(text)<len(page['text'])
                    sources.append({'text':text,'label':page['label'],'file_id':file_id,'page':page['page']})
                    remaining-=len(text)
            else:
                sources, partial = read_document(app.config['DB_PATH'], file_id, ai.settings.max_chars)
            return jsonify(ai.run(data['task'], sources, question, partial))
        finally:
            ai_lock.release()

    return app


def main():
    from waitress import serve
    load_dotenv(Path.home()/'.config/project-hub/ai.env', override=False)
    load_dotenv(Path.home()/'.config/project-hub/library.env', override=False)
    origin = os.environ.get('PROJECT_HUB_LIBRARY_ORIGIN', '')
    share_hash=os.environ.get('PROJECT_HUB_LIBRARY_SHARE_HASH', '')
    secret=os.environ.get('PROJECT_HUB_LIBRARY_SESSION_KEY', '')
    if len(share_hash)!=64 or len(secret)<32 or not origin.startswith('https://'):
        raise RuntimeError('Library share key and HTTPS origin must be configured')
    settings=Settings.from_env()
    app = create_app({'SHARE_HASH':share_hash,'SECRET_KEY':secret,'PUBLIC_ORIGIN': origin,
                      'PUBLIC_ACCESS':os.environ.get('PROJECT_HUB_LIBRARY_PUBLIC')=='true',
                      'DOCUMENT_CACHE':settings.state/'library-text.db'}, Organizer(settings))
    app.extensions['documents'].start()
    serve(app, host='127.0.0.1', port=8090, threads=4, max_request_body_size=8192,
          clear_untrusted_proxy_headers=True)


if __name__ == '__main__':
    main()
