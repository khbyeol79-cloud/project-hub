"""Public processing counts without paths, credentials or raw error details."""
from collections import Counter
from contextlib import closing
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
from .documents import SUFFIXES


def snapshot(db,cache,channels):
    columns={r[1] for r in db.execute('PRAGMA table_info(files)')}
    upload='f.upload_status' if 'upload_status' in columns else "'unknown'"
    rows=db.execute('''SELECT f.id,f.original_filename,f.sha256,'''+upload+''' AS upload,
        CASE WHEN d.source_sha256=f.sha256 THEN d.status ELSE 'pending' END AS content
        FROM files f LEFT JOIN content_documents d ON d.file_id=f.id''').fetchall()
    with closing(sqlite3.connect(Path(cache).resolve().as_uri()+'?mode=ro',uri=True)) as saved:
        extra={r[0]:(r[1],json.loads(r[2]).get('status','pending')) for r in saved.execute('SELECT id,sha,result FROM documents WHERE id>0')}
    counts=Counter();uploads=Counter()
    for r in rows:
        state=r['content'] or 'pending'
        if Path(r['original_filename']).suffix.lower() in SUFFIXES:
            result=extra.get(r['id'])
            state=result[1] if result and result[0]==r['sha256'] else 'pending'
        bucket=('ready' if state=='indexed' else 'partial' if state=='partial' else
                'pending' if state in {'pending','ocr_pending'} else
                'failed' if state in {'failed','missing','changed','encoding'} else 'original_only')
        counts[bucket]+=1
        uploads[r['upload'] if r['upload'] in {'uploaded','pending','failed','needs_review'} else 'unknown']+=1
    checks=[]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='health_checks'").fetchone():
        keys=[prefix+str(c) for c in channels for prefix in ('history:','messages:')]
        if keys:
            checks=db.execute('SELECT failures,last_checked_at FROM health_checks WHERE check_key IN ('+','.join('?' for _ in keys)+')',keys).fetchall()
    latest=max((r['last_checked_at'] for r in checks if r['last_checked_at']),default=None)
    state='unknown'
    if checks:
        def recent(value):
            if not value:return False
            try:
                dt=datetime.fromisoformat(value.replace('Z','+00:00'))
                if dt.tzinfo is None:dt=dt.replace(tzinfo=timezone.utc)
                return 0<=(datetime.now(timezone.utc)-dt).total_seconds()<300
            except ValueError:return False
        state='attention' if any(r['failures'] for r in checks) else 'healthy' if len(checks)==len(keys) and all(recent(r['last_checked_at']) for r in checks) else 'stale'
    return {'total_files':len(rows),'content':dict(counts),'drive':dict(uploads),
            'collection':state,'last_checked_at':latest,'checked_at':datetime.now(timezone.utc).isoformat()}
