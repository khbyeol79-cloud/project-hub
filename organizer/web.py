"""Revocable link-access library behind Caddy. Bind only to loopback."""
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import hashlib
import hmac
import os
import re
from pathlib import Path
import sqlite3
import threading
import time
import io

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException

from .ai import AIError, Organizer, Settings
from .__main__ import read_document
from .documents import Documents, SUFFIXES
from .retrieval import retrieve
from .conversations import Conversations
from . import versions
from bot import projects
from .photos import Photos, PhotoError, photo_service
from .schedule import schedule_snapshot, ScheduleError

TEXT_PREVIEW_SUFFIXES = {'.md', '.markdown', '.txt', '.py', '.c', '.cpp', '.h', '.hpp', '.java',
                         '.js', '.ts', '.json', '.csv', '.yaml', '.yml', '.ini', '.cfg', '.css'}
TEXT_PREVIEW_BYTES = 2 * 1024 * 1024

ROOT = Path(__file__).resolve().parent
FIELDS = '''f.id, f.original_filename, f.discord_channel, f.discord_channel_id,
            f.category, f.uploaded_at, f.file_size_bytes, f.version, f.duplicate_type,
            CASE WHEN d.source_sha256=f.sha256 THEN d.status ELSE 'pending' END AS content_status'''


def create_app(config=None, ai=None):
    app = Flask(__name__, static_folder=None)
    app.config.update(DB_PATH=ROOT.parent / 'project_hub.db', STORAGE=ROOT.parent / 'storage',
                      SHARE_HASH='', PUBLIC_ORIGIN='', PUBLIC_ACCESS=False, MAX_CONTENT_LENGTH=8192,
                      DOCUMENT_CACHE=None, MESSAGE_CHANNELS=(),
                      PHOTO_FOLDER_ID='', PHOTO_ROOT_ID='',
                      PHOTO_TOKEN=Path.home()/'.config/project-hub/photos-token.json',
                      SHARE_URL_FILE=Path.home()/'.config/project-hub/library-share-url.txt',
                      SESSION_COOKIE_NAME='ph_library', SESSION_COOKIE_PATH='/library/',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_SAMESITE='Strict', PERMANENT_SESSION_LIFETIME=timedelta(days=14))
    if config:
        app.config.update(config)
    documents=Documents(app.config['DB_PATH'],app.config['STORAGE'],
                        app.config['DOCUMENT_CACHE'] or Path(app.config['DB_PATH']).parent/'library-text.db')
    app.extensions['documents']=documents
    app.extensions['photos']=app.config.get('PHOTOS') or Photos(
        lambda: photo_service(app.config['PHOTO_TOKEN']),
        app.config['PHOTO_FOLDER_ID'], app.config['PHOTO_ROOT_ID'])
    ai_lock = threading.Lock()
    access_lock = threading.Lock()
    access_attempts = deque()
    weekly_folder=getattr(ai.settings,'state',Path(app.config['DB_PATH']).parent) if ai else Path(app.config['DB_PATH']).parent
    weekly_path=Path(weekly_folder)/'weekly-summary.json'

    def connect():
        db = sqlite3.connect(Path(app.config['DB_PATH']).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.create_function('file_ext',1,lambda name: Path(name or '').suffix.lower() or '(none)',deterministic=True)
        return db

    def request_scope():
        try:
            return projects.select(request.args.get('project', 'main'), request.args.get('team', ''))
        except ValueError:
            abort(400)

    def scoped_conversations():
        scope = request_scope()
        teams = dict(projects.TEAM_CHANNEL_IDS)
        with closing(connect()) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_messages'").fetchone():
                teams.update(db.execute(
                    "SELECT DISTINCT channel_id,category FROM conversation_messages WHERE guild_id=? AND category IN ('robot_1a','robot_1b')",
                    (projects.GUILD_ID,)))
        # Preserve the original publication allowlist. Publish only the requested
        # team channels within the verified server for the additional project.
        if scope == 'main':
            channels = set(str(c) for c in app.config['MESSAGE_CHANNELS']) - set(teams)
        else:
            channels = {cid for cid, category in teams.items() if projects.category_scope(category) == scope}
        return Conversations(app.config['DB_PATH'], channels, scope)

    def weekly_file():
        return weekly_path.with_name('weekly-summary-' + request_scope() + '.json')

    @app.before_request
    def authenticate():
        if request.remote_addr not in {'127.0.0.1', '::1'}:
            abort(403)
        if request.method == 'POST':
            if (not app.config['PUBLIC_ORIGIN'] or request.headers.get('Origin') != app.config['PUBLIC_ORIGIN']
                    or request.headers.get('X-Requested-With') != 'ProjectHub' or not request.is_json):
                abort(403)
        if request.path in {'/library/', '/library/assets/app.js', '/library/assets/style.css', '/library/assets/reading.js', '/library/assets/photos.js', '/library/assets/schedule.js', '/library/api/access'}:
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
        if request.endpoint in {'html_preview','docx_preview'}:
            response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
        else:
            if request.endpoint == 'preview' and response.mimetype == 'application/pdf':
                response.headers['Content-Security-Policy'] = response.headers['Content-Security-Policy'].replace(
                    "frame-ancestors 'none'", "frame-ancestors 'self'")
            response.headers['Content-Security-Policy'] += "; frame-src 'self' https://drive.google.com https://docs.google.com"
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
        if name not in {'app.js', 'style.css', 'reading.js', 'photos.js', 'schedule.js'}:
            abort(404)
        return send_file(ROOT / 'static' / name)

    @app.get('/library/api/schedule')
    def education_schedule():
        scope = request_scope()
        with closing(connect()) as db:
            rows = db.execute("SELECT id, original_filename FROM files WHERE original_filename LIKE '%일정표%.xlsx' AND " +
                              projects.predicate(scope) + " ORDER BY julianday(uploaded_at) DESC, id DESC LIMIT 20").fetchall()
        if not rows:
            return jsonify(available=False, notice='이 자료 범위에 교육 일정표가 아직 없습니다.')
        row = rows[0]
        _, path = local_file(row['id'])
        try:
            return jsonify(schedule_snapshot(path, row['original_filename']))
        except ScheduleError as exc:
            return jsonify(error=str(exc)), 422
        except Exception as exc:
            app.logger.warning('SCHEDULE_READ_FAILED type=%s', type(exc).__name__)
            return jsonify(error='일정표를 읽지 못했습니다. 원본 엑셀을 확인해 주세요.'), 422

    @app.get('/library/api/photos')
    def photo_list():
        scope = request_scope()
        sort = request.args.get('sort', 'date_desc')
        token = request.args.get('page_token', '')
        try:
            days = int(request.args.get('days', '0'))
        except ValueError:
            abort(400)
        if sort not in {'date_desc', 'date_asc'} or days not in {0, 7, 30} or len(token) > 4096:
            abort(400)
        try:
            return jsonify(app.extensions['photos'].listing(scope, sort, days, token))
        except PhotoError as exc:
            message = str(exc)
        except Exception as exc:
            app.logger.warning('PHOTO_LIST_FAILED type=%s', type(exc).__name__)
            message = 'Google Drive 사진 연결을 확인해 주세요. 잠시 후 다시 시도하거나 원본 폴더를 열어 주세요.'
        # A team must never receive a link to the original project's folder on error.
        url = Photos.folder_url(app.config['PHOTO_FOLDER_ID']) if scope == 'main' else None
        return jsonify(error=message, folder_url=url), 503

    @app.get('/library/api/photos/count')
    def photo_count():
        scope = request_scope()
        try:
            days = int(request.args.get('days', '0'))
        except ValueError:
            abort(400)
        if days not in {0, 7, 30}:
            abort(400)
        try:
            return jsonify(total_count=app.extensions['photos'].count(scope, days))
        except Exception as exc:
            app.logger.warning('PHOTO_COUNT_FAILED type=%s', type(exc).__name__)
            return jsonify(error='전체 사진 수를 확인하지 못했습니다. 새로고침해 주세요.'), 503

    @app.get('/library/photos/<file_id>/thumbnail')
    def photo_thumbnail(file_id):
        scope = request_scope()
        try:
            data = app.extensions['photos'].thumbnail(scope, file_id)
        except FileNotFoundError:
            abort(404)
        except Exception as exc:
            app.logger.warning('PHOTO_THUMBNAIL_FAILED type=%s', type(exc).__name__)
            return jsonify(error='사진 미리보기를 불러오지 못했습니다.'), 503
        return send_file(io.BytesIO(data), mimetype='image/jpeg')

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
        extension = request.args.get('extension','').lower()[:100]
        orders={'date_desc':'COALESCE(julianday(f.uploaded_at),0) DESC, f.id DESC','date_asc':'COALESCE(julianday(f.uploaded_at),0) ASC, f.id ASC',
                'name_asc':'f.original_filename COLLATE NOCASE ASC, f.id ASC',
                'name_desc':'f.original_filename COLLATE NOCASE DESC, f.id DESC'}
        order=orders.get(request.args.get('sort','date_desc'))
        if order is None: abort(400)
        try:
            offset = max(0, min(int(request.args.get('offset', '0')), 100000))
            days=int(request.args.get('days','0'))
            if days not in {0,7,30}: raise ValueError()
        except ValueError:
            abort(400)
        conditions, params = [projects.predicate(request_scope(),'f.category')], []
        if days:
            conditions.append('julianday(f.uploaded_at)>=julianday(?)')
            params.append((datetime.now(timezone.utc)-timedelta(days=days)).isoformat())
        view=request.args.get('view','all')
        if view not in {'all','latest'}: abort(400)
        if view=='latest': conditions.append(versions.LATEST)
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
            with closing(connect()) as db:
                parent=any(r[1]=='discord_parent_channel_id' for r in db.execute('PRAGMA table_info(files)'))
            conditions.append('(f.discord_channel_id=?'+(' OR f.discord_parent_channel_id=?' if parent else '')+')')
            params.extend([channel,channel] if parent else [channel])
        if extension:
            conditions.append('file_ext(f.original_filename)=?')
            params.append(extension)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with closing(connect()) as db:
            rows = db.execute('SELECT ' + FIELDS + ' FROM files f LEFT JOIN content_documents d ON d.file_id=f.id'
                              + where + ' ORDER BY '+order+' LIMIT 31 OFFSET ?', params + [offset]).fetchall()
            channels = [dict(r) for r in db.execute('SELECT DISTINCT discord_channel_id AS id, discord_channel AS name FROM files f WHERE '+projects.predicate(request_scope(),'f.category')+' ORDER BY name')]
            extensions=[r[0] for r in db.execute('SELECT DISTINCT file_ext(original_filename) FROM files f WHERE '+projects.predicate(request_scope(),'f.category')+' ORDER BY 1')]
            total=db.execute('SELECT count(*) FROM files f'+where,params).fetchone()[0]
            relationships={r['id']:versions.info(db,r['id']) for r in rows[:30]}
            previews={}
            if query:
                for r in rows[:30]:
                    hit=db.execute('''SELECT p.body FROM content_pages p JOIN content_documents d ON d.file_id=p.file_id
                        JOIN files f ON f.id=p.file_id WHERE f.id=? AND f.sha256=d.source_sha256
                        AND d.status IN ('indexed','partial') AND instr(p.search_body,?)>0 ORDER BY p.page LIMIT 1''',
                        (r['id'],query.casefold())).fetchone()
                    if hit: previews[r['id']]=hit[0]
        items=[dict(r) for r in rows[:30]]
        for item in items:
            item.update(relationships[item['id']])
            body=previews.get(item['id'],'')
            if Path(item['original_filename']).suffix.lower() in SUFFIXES:
                extra=documents.get(item['id'])
                if extra:
                    item['content_status']=extra['status']
                    if query and not body:
                        body=next((p['text'] for p in extra['pages'] if query.casefold() in p['text'].casefold()),'')
            start=max(0,body.casefold().find(query.casefold())-70) if body and query else 0
            item['excerpt']=('…' if start else '')+body[start:start+220] if body else ''
        return jsonify(files=items, more=len(rows)>30, channels=channels,extensions=extensions,total=total)

    @app.get('/library/api/messages')
    def messages():
        try:
            days=int(request.args.get('days','0'))
            offset=int(request.args.get('offset','0'))
            sort=request.args.get('sort','date_desc')
            if sort not in {'date_desc','date_asc'}: raise ValueError()
            if days not in {0,7,30} or not 0<=offset<=100000: raise ValueError()
        except ValueError: abort(400)
        conversations=scoped_conversations()
        result=conversations.search(request.args.get('q','').strip()[:100],
                                    request.args.get('channel','')[:100],days,offset,sort=sort)
        result['enabled']=bool(conversations.channels)
        return jsonify(result)

    @app.get('/library/api/messages/<message_id>')
    def message_detail(message_id):
        if not re.fullmatch(r'[0-9]{1,20}',message_id): abort(404)
        rows=scoped_conversations().search(message_id=message_id,limit=1)['messages']
        if not rows: abort(404)
        return jsonify(rows[0])

    @app.get('/library/api/processing')
    def processing_status():
        from .processing import snapshot
        with closing(connect()) as db:
            return jsonify(snapshot(db,documents.cache,scoped_conversations().channels,request_scope()))

    def file_row(db, file_id):
        row = db.execute('SELECT ' + FIELDS + ', f.local_path FROM files f LEFT JOIN content_documents d ON d.file_id=f.id WHERE f.id=? AND '+projects.predicate(request_scope(),'f.category'), (file_id,)).fetchone()
        if not row:
            abort(404)
        return dict(row)

    @app.get('/library/api/files/<int:file_id>')
    def detail(file_id):
        with closing(connect()) as db:
            db.execute('BEGIN')
            row = file_row(db, file_id)
            row.pop('local_path')
            columns={c[1] for c in db.execute('PRAGMA table_info(files)')}
            raw=db.execute('SELECT upload_status FROM files WHERE id=?',(file_id,)).fetchone()[0] if 'upload_status' in columns else 'unknown'
            row['upload_status']=raw if raw in {'uploaded','pending','failed','needs_review'} else 'unknown'
            row.update(versions.info(db,file_id))
            row['related'],row['related_more']=versions.related(db,file_id)
            drive_id=db.execute('SELECT google_drive_file_id FROM files WHERE id=?',(file_id,)).fetchone()[0]
            row['drive_url']=f'https://drive.google.com/file/d/{drive_id}/view' if isinstance(drive_id,str) and re.fullmatch(r'[A-Za-z0-9_-]+',drive_id) else None
            row['drive_preview_url']=row['drive_url'].removesuffix('/view')+'/preview' if row['drive_url'] else None
            row['html_preview']=Path(row['original_filename']).suffix.lower() in {'.html','.htm'}
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
            source_page=request.args.get('source_page',type=int)
            if source_page and row['content_status'] in {'indexed','partial'} and not any(p['page']==source_page for p in pages):
                source=db.execute('SELECT body FROM content_pages WHERE file_id=? AND page=?',(file_id,source_page)).fetchone()
                if source:
                    pages.append({'page':source_page,'label':f'출처 구간 {source_page}','text':source['body'][:12000]})
            extra=documents.get(file_id)
            if extra:
                row.update(pages=extra['pages'],partial=extra['status']=='partial',content_status=extra['status'])
            reading=documents.get(file_id,reading=True)
            row['reading_blocks']=reading.get('blocks',[]) if reading else []
            row['reading_partial']=reading.get('status')=='partial' if reading else False
            row['docx_preview']=Path(row['original_filename']).suffix.lower() == '.docx'
            row['text_preview']=Path(row['original_filename']).suffix.lower() in TEXT_PREVIEW_SUFFIXES
            row['pdf_preview']=Path(row['original_filename']).suffix.lower() == '.pdf'
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
        elif Path(row['original_filename']).suffix.lower() == '.pdf' and magic.startswith(b'%PDF-'):
            mime='application/pdf'
        if mime is None: abort(404)
        return send_file(path,mimetype=mime)

    @app.get('/library/api/files/<int:file_id>/text-preview')
    def text_preview(file_id):
        row,path=local_file(file_id)
        if Path(row['original_filename']).suffix.lower() not in TEXT_PREVIEW_SUFFIXES:
            abort(404)
        with path.open('rb') as stream:
            data=stream.read(TEXT_PREVIEW_BYTES+1)
        if len(data)>TEXT_PREVIEW_BYTES:
            return jsonify(error='큰 원본은 원본 다운로드로 확인해 주세요.'),413
        try:
            if data.startswith((b'\xff\xfe',b'\xfe\xff')):
                text=data.decode('utf-16')
            else:
                try: text=data.decode('utf-8-sig')
                except UnicodeDecodeError: text=data.decode('cp949')
        except UnicodeError:
            return jsonify(error='문자 인코딩을 읽을 수 없습니다. 원본을 다운로드해 주세요.'),422
        if '\x00' in text:
            return jsonify(error='텍스트로 표시할 수 없는 원본입니다. 원본을 다운로드해 주세요.'),422
        return jsonify(text=text)

    @app.get('/library/files/<int:file_id>/docx-preview')
    def docx_preview(file_id):
        row,path=local_file(file_id)
        if Path(row['original_filename']).suffix.lower() != '.docx':
            abort(404)
        result=documents.get(file_id,reading=True)
        if not result or not result.get('original_html'):
            return jsonify(error='문서 원본을 표시할 수 없습니다. 원본을 다운로드해 주세요.'),422
        return app.response_class(result['original_html'],content_type='text/html; charset=utf-8')

    @app.get('/library/files/<int:file_id>/html-preview')
    def html_preview(file_id):
        row,path=local_file(file_id)
        if Path(row['original_filename']).suffix.lower() not in {'.html','.htm'}:
            abort(404)
        with path.open('rb') as f:
            data=f.read(25*1024*1024+1)
        if len(data)>25*1024*1024:
            abort(413)
        try:
            if data.startswith((b'\xff\xfe',b'\xfe\xff')):
                text=data.decode('utf-16')
            else:
                try: text=data.decode('utf-8-sig')
                except UnicodeDecodeError: text=data.decode('cp949')
        except UnicodeError:
            abort(422)
        return app.response_class(text,content_type='text/html; charset=utf-8')

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

    def weekly_data():
        since=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        with closing(connect()) as db:
            files=[dict(r) for r in db.execute('''SELECT id,original_filename,uploaded_at FROM files
                WHERE julianday(uploaded_at)>=julianday(?) AND '''+projects.predicate(request_scope())+''' ORDER BY julianday(uploaded_at) DESC LIMIT 8''',(since,))]
        messages=scoped_conversations().search(days=7,limit=8)['messages']
        return since,files,messages

    @app.get('/library/api/weekly')
    def weekly_get():
        conversations=scoped_conversations()
        path=weekly_file()
        since,files,messages=weekly_data()
        saved=None
        if path.exists():
            candidate=json.loads(path.read_text(encoding='utf-8'))
            valid=candidate.get('scope')==request_scope() and conversations.current(candidate['checks'])
            with closing(connect()) as db:
                for fid,sha in candidate['file_hashes'].items():
                    row=db.execute('SELECT sha256 FROM files WHERE id=?',(fid,)).fetchone()
                    valid=valid and bool(row and row['sha256']==sha)
            if valid: saved=candidate['result']
        return jsonify(files=files,messages=messages,saved=saved,since=since)

    @app.post('/library/api/weekly')
    def weekly_generate():
        if ai is None: raise AIError('AI 기능이 아직 준비되지 않았습니다.')
        if not ai_lock.acquire(blocking=False):
            return jsonify(error='다른 자료를 정리 중입니다. 잠시 후 다시 시도해 주세요.'),429
        try:
            conversations=scoped_conversations()
            path=weekly_file()
            since,_,_=weekly_data()
            chats=conversations.sources('',7,ai.settings.max_chars//2)
            sources=retrieve(app.config['DB_PATH'],documents,'',
                ai.settings.max_chars-sum(len(s['text']) for s in chats),since=since,scope=request_scope())+chats
            if not sources: return jsonify(error='최근 7일간 정리할 본문이나 공개 대화가 없습니다.'),422
            with closing(connect()) as db:
                hashes={str(s['file_id']):db.execute('SELECT sha256 FROM files WHERE id=?',
                    (s['file_id'],)).fetchone()['sha256'] for s in sources if 'file_id' in s}
            result=ai.run('summary',sources,partial=True)
            if not conversations.current(sources): raise AIError('대화가 변경되었습니다. 다시 정리해 주세요.')
            result=dict(result,generated_at=datetime.now(timezone.utc).isoformat(),since=since,
                matched_files=len({s['file_id'] for s in sources if 'file_id' in s}),matched_messages=len(chats))
            with closing(connect()) as db:
                for fid,sha in hashes.items():
                    row=db.execute('SELECT sha256 FROM files WHERE id=?',(fid,)).fetchone()
                    if not row or row['sha256']!=sha: raise AIError('파일이 변경되었습니다. 다시 정리해 주세요.')
            snapshot={'scope':request_scope(),'result':result,'checks':[{k:v for k,v in s.items() if k!='text'} for s in chats],'file_hashes':hashes}
            temp=path.with_suffix('.tmp')
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as output: json.dump(snapshot,output,ensure_ascii=False)
            temp.replace(path)
            return jsonify(result)
        finally: ai_lock.release()

    @app.post('/library/api/ai')
    def integrated_ai():
        data=request.get_json()
        if not isinstance(data,dict) or data.get('task') not in {'ask','summary'}: abort(400)
        question=data.get('question','')
        if not isinstance(question,str) or len(question)>1000 or (data['task']=='ask' and not question.strip()): abort(400)
        days=data.get('days',0)
        if type(days) is not int or days not in {0,7,30}: abort(400)
        channel=data.get('channel','')
        if not isinstance(channel,str) or (channel and not re.fullmatch(r'[0-9]{1,20}',channel)): abort(400)
        if ai is None: raise AIError('AI 기능이 아직 준비되지 않았습니다.')
        if not ai_lock.acquire(blocking=False):
            return jsonify(error='다른 자료를 정리 중입니다. 잠시 후 다시 시도해 주세요.'),429
        try:
            conversations=scoped_conversations()
            needle=question if data['task']=='ask' else ''
            chat_sources=conversations.sources(needle,days,ai.settings.max_chars//2,channel=channel)
            related_files={fid for source in chat_sources for fid in source.get('attachment_ids',[])}
            sources=retrieve(app.config['DB_PATH'],documents,needle,
                             ai.settings.max_chars-sum(len(s['text']) for s in chat_sources),
                             related_file_ids=related_files,channel=channel,
                             since=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat() if days else None,scope=request_scope()) + chat_sources
            if not sources:
                return jsonify(answer='관련 파일 본문이나 공개된 대화를 찾지 못했습니다. 구체적인 용어로 질문해 주세요.',sources=[],partial=True,cached=False,matched_files=0,matched_messages=0)
            result=ai.run(data['task'],sources,question,True)
            if not conversations.current(sources):
                raise AIError('답변 생성 중 원본 대화가 수정·삭제되었습니다. 다시 질문해 주세요.')
            result['matched_files']=len({s['file_id'] for s in sources if 'file_id' in s})
            result['matched_messages']=len(chat_sources)
            return jsonify(result)
        finally: ai_lock.release()

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
            with closing(connect()) as db:
                file_row(db,file_id)
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
                      'MESSAGE_CHANNELS':os.environ.get('PROJECT_HUB_LIBRARY_MESSAGE_CHANNELS','').split(','),
                      'PHOTO_FOLDER_ID':os.environ.get('PROJECT_HUB_PHOTO_FOLDER_ID',''),
                      'PHOTO_ROOT_ID':os.environ.get('PROJECT_HUB_PHOTO_ROOT_ID',''),
                      'PHOTO_TOKEN':os.environ.get('PROJECT_HUB_PHOTO_TOKEN',str(Path.home()/'.config/project-hub/photos-token.json')),
                      'DOCUMENT_CACHE':settings.state/'library-text.db'}, Organizer(settings))
    app.extensions['documents'].start()
    serve(app, host='127.0.0.1', port=8090, threads=4, max_request_body_size=8192,
          clear_untrusted_proxy_headers=True)


if __name__ == '__main__':
    main()
