"""Permission-scoped queries shared by the extra Discord commands."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3

if __package__:
    from . import database, content_search, projects, projects
else:
    import database, content_search, projects

clean = content_search.clean_text
CURRENT = '''d.source_sha256=f.sha256 AND d.status IN ('indexed','partial','ocr_pending')'''
FSCOPE = 'f.discord_guild_id=? AND f.discord_channel_id IN (SELECT id FROM visible_channels) AND in_project(f.category)'
MSCOPE = 'm.guild_id=? AND m.channel_id IN (SELECT id FROM visible_channels) AND in_project(m.category)'
KST = timezone(timedelta(hours=9))


@contextmanager
def connection(channels, scope=None):
    db = database.get_connection()
    try:
        if scope is not None: projects.validate(scope)
        db.create_function('in_project',1,lambda category: scope is None or projects.category_scope(category)==scope, deterministic=True)
        db.row_factory = sqlite3.Row
        db.create_function('normalized', 1, lambda s: clean(s or '').casefold(), deterministic=True)
        db.execute('CREATE TEMP TABLE visible_channels (id TEXT PRIMARY KEY)')
        db.executemany('INSERT OR IGNORE INTO visible_channels VALUES (?)', [(str(cid),) for cid in channels])
        # Freeze counts, results and source text in the same read snapshot.
        db.commit()
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def candidates(guild_id, category=None):
    with connection([]) as db:
        rows = db.execute('''SELECT discord_channel_id FROM files WHERE discord_guild_id=? AND (? IS NULL OR category=?)
            UNION SELECT channel_id FROM conversation_messages WHERE guild_id=? AND (? IS NULL OR category=?)''',
            (str(guild_id), category, category, str(guild_id), category, category))
        ids = {r[0] for r in rows}
    path = database.BASE_DIR/'config/channels.json'
    if path.is_file():
        config = json.loads(path.read_text(encoding='utf-8'))
        for cid, value in {**config.get('channels', {}), **config.get('forums', {})}.items():
            if category is None or value == category:
                ids.add(cid)
    return list(ids)


def catalogue(db, guild_id, ids):
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    return {r['id']: dict(r) for r in db.execute('''SELECT f.id,f.original_filename,f.uploaded_at,f.category,
        f.discord_guild_id,f.discord_channel_id,f.discord_message_id,f.google_drive_file_id,f.upload_status,
        f.discord_author,f.file_size_bytes,f.sha256 FROM files f WHERE '''+FSCOPE+f' AND f.id IN ({marks})',
        [str(guild_id), *ids])}


def unified(guild_id, channels, keyword, category=None, page=1, *, scope=None):
    if not clean(keyword) or len(keyword)>100 or not 1<=page<=10000:
        raise ValueError('Invalid query')
    needle = clean(keyword).casefold()
    sql = '''WITH scoped AS (
        SELECT f.*, instr(normalized(f.original_filename),?)>0 AS name_hit
        FROM files f WHERE '''+FSCOPE+''' AND (? IS NULL OR f.category=?)
    ), body_hits AS (
        SELECT f.id,MIN(p.page) AS page FROM scoped f
        JOIN content_documents d ON d.file_id=f.id AND '''+CURRENT+'''
        JOIN content_pages p ON p.file_id=f.id WHERE instr(p.search_body,?)>0 GROUP BY f.id
    ), hits AS (
        SELECT 'file' AS kind,CAST(f.id AS TEXT) AS key,f.uploaded_at AS stamp,f.name_hit,
               h.page FROM scoped f LEFT JOIN body_hits h ON h.id=f.id WHERE f.name_hit OR h.id IS NOT NULL
        UNION ALL SELECT 'message',m.message_id,m.created_at,0,NULL FROM conversation_messages m
        WHERE '''+MSCOPE+''' AND (? IS NULL OR m.category=?) AND instr(m.search_content,?)>0
    ) '''
    values = [needle,str(guild_id),category,category,needle,str(guild_id),category,category,needle]
    with connection(channels,scope) as db:
        counts = dict(db.execute(sql+'SELECT kind,COUNT(*) FROM hits GROUP BY kind', values))
        hits = [dict(r) for r in db.execute(sql+'''SELECT * FROM hits ORDER BY julianday(stamp) DESC,
            kind,CAST(key AS INTEGER) DESC LIMIT 5 OFFSET ?''', [*values,(page-1)*5])]
        files = catalogue(db, guild_id, [int(h['key']) for h in hits if h['kind']=='file'])
        for hit in hits:
            if hit['kind']=='file':
                hit.update(files[int(hit['key'])])
                if hit['page'] is not None:
                    part = db.execute('''SELECT p.body,COALESCE(l.label,'') FROM content_pages p
                        LEFT JOIN content_locations l ON l.file_id=p.file_id AND l.page=p.page
                        WHERE p.file_id=? AND p.page=?''', (int(hit['key']),hit['page'])).fetchone()
                    hit['excerpt'] = content_search.snippet(part[0],keyword)
                    hit['location'] = part[1] or f'본문 위치 {hit["page"]}'
            else:
                row = db.execute('SELECT m.* FROM conversation_messages m WHERE '+MSCOPE+' AND m.message_id=?',
                                 (str(guild_id),hit['key'])).fetchone()
                hit.update(dict(row))
                hit['excerpt'] = content_search.snippet(clean(hit.pop('content')),keyword)
                hit.pop('search_content',None)
        return hits,counts


def suggestions(guild_id, channels, keyword, *, scope=None):
    with connection(channels,scope) as db:
        return [dict(r) for r in db.execute('''SELECT f.id,f.original_filename,f.uploaded_at FROM files f WHERE '''+FSCOPE+'''
            AND instr(normalized(f.original_filename),?)>0 ORDER BY julianday(f.uploaded_at) DESC,f.id DESC LIMIT 20''',
            (str(guild_id),clean(keyword[:100]).casefold()))]


def detail(guild_id, channels, file_id, *, scope=None):
    with connection(channels,scope) as db:
        row = catalogue(db,guild_id,[file_id]).get(file_id)
        if row is None:
            return None
        group = '''WITH ordered AS (
            SELECT f.id,f.sha256,f.uploaded_at,f.discord_message_id,LAG(f.sha256) OVER (
                ORDER BY julianday(f.uploaded_at),CAST(f.discord_message_id AS INTEGER),f.id) AS previous
            FROM files f WHERE '''+FSCOPE+''' AND f.discord_channel_id=? AND f.category=?
                AND normalized(f.original_filename)=normalized(?)
        ), versions AS (
            SELECT *,SUM(CASE WHEN sha256<>'' AND sha256=previous THEN 0 ELSE 1 END) OVER (
                ORDER BY julianday(uploaded_at),CAST(discord_message_id AS INTEGER),id) AS version FROM ordered
        ) SELECT version,(SELECT MAX(version) FROM versions) FROM versions WHERE id=?'''
        version = db.execute(group,(str(guild_id),row['discord_channel_id'],row['category'],row['original_filename'],file_id)).fetchone()
        row['version'],row['version_total'] = version
        row['same_content'] = db.execute('SELECT COUNT(*) FROM files f WHERE '+FSCOPE+' AND f.sha256=?',
                                        (str(guild_id),row['sha256'])).fetchone()[0]
        doc = db.execute('''SELECT d.status,(SELECT COUNT(*) FROM content_pages WHERE file_id=d.file_id)
            FROM content_documents d WHERE d.file_id=? AND d.source_sha256=?''',(file_id,row['sha256'])).fetchone()
        row['content_status'],row['parts'] = (doc[0],doc[1]) if doc else ('pending',0)
        row.pop('sha256')
        return row


def status(guild_id,channels, *, scope=None):
    with connection(channels,scope) as db:
        uploads = dict(db.execute('SELECT f.upload_status,COUNT(*) FROM files f WHERE '+FSCOPE+' GROUP BY f.upload_status',(str(guild_id),)))
        documents = dict(db.execute('''SELECT COALESCE(d.status,'pending'),COUNT(*) FROM files f
            LEFT JOIN content_documents d ON d.file_id=f.id AND d.source_sha256=f.sha256 WHERE '''+FSCOPE+
            " GROUP BY COALESCE(d.status,'pending')",(str(guild_id),)))
        conversations = db.execute('SELECT COUNT(*) FROM conversation_messages m WHERE '+MSCOPE,(str(guild_id),)).fetchone()[0]
        checks = [dict(r) for r in db.execute('''SELECT check_key,failures,last_checked_at,last_success_at
            FROM health_checks WHERE substr(check_key,instr(check_key,':')+1) IN (SELECT id FROM visible_channels)
            AND (check_key LIKE 'history:%' OR check_key LIKE 'messages:%')''')]
        return dict(uploads=uploads,documents=documents,conversations=conversations,checks=checks)


def question_terms(question):
    words = re.findall(r'[a-zA-Z0-9가-힣_]+',clean(question).casefold())
    stop = {'뭐야','뭐였지','알려줘','알려주세요','무엇','어떤','있어','있나요','자료','파일','내용','질문','the','is','what','how'}
    terms = []
    for word in words:
        if word in stop:
            continue
        if len(word)>2 and re.fullmatch(r'[가-힣]+',word):
            word = re.sub(r'(에서|으로|은|는|을|를|이|가|의|에)$','',word)
        if len(word)>=2 or word.isdigit():
            if word not in terms:
                terms.append(word)
    return terms[:8]


def make_source(row, kind, text=None):
    body = row['body']
    if kind=='file':
        gid,cid,mid = row['discord_guild_id'],row['discord_channel_id'],row['discord_message_id']
        label = f"{row['original_filename']} · {row['location'] or ('본문 위치 '+str(row['page']))}"
        key,file_id,page,version = str(row['id']),row['id'],row['page'],row['sha256']
    else:
        gid,cid,mid = row['guild_id'],row['channel_id'],row['message_id']
        when = datetime.fromisoformat(row['created_at']).astimezone(KST).strftime('%Y-%m-%d %H:%M')
        label = f"대화 · {when} 한국시간 · {row['author_name']}"
        key,file_id,page,version = str(mid),None,None,str(row['revision'])
    return {'text':text if text is not None else body,'label':label[:200], 'file_id':file_id,'page':page,
        'kind':kind,'key':key,'version':version,'channel_id':str(cid),'guild_id':str(gid),
        'message_id':str(mid),'body_hash':hashlib.sha256(body.encode()).hexdigest()}


def sources(guild_id,channels,*,file_id=None,question='',period=None,category=None,now=None,max_chars=12000,scope=None):
    """Select authorized text before any AI call. Never call an unscoped administrator reader."""
    with connection(channels,scope) as db:
        if file_id is not None:
            if catalogue(db,guild_id,[file_id]).get(file_id) is None:
                return [],False
            rows = list(db.execute('''SELECT f.id,f.original_filename,f.sha256,f.discord_guild_id,
                f.discord_channel_id,f.discord_message_id,p.page,p.body,d.status,COALESCE(l.label,'') AS location
                FROM files f JOIN content_documents d ON d.file_id=f.id AND '''+CURRENT+'''
                JOIN content_pages p ON p.file_id=f.id LEFT JOIN content_locations l ON l.file_id=p.file_id AND l.page=p.page
                WHERE '''+FSCOPE+' AND f.id=? ORDER BY p.page',(str(guild_id),file_id)))
            tagged = [('file',dict(r)) for r in rows if r['body'].strip()]
            partial = any(r['status']!='indexed' for r in rows)
        elif period:
            now = (now or datetime.now(KST)).astimezone(KST)
            midnight = now.replace(hour=0,minute=0,second=0,microsecond=0)
            start,end = {'today':(midnight,now), 'yesterday':(midnight-timedelta(days=1),midnight),
                         'week':(now-timedelta(days=7),now)}[period]
            rows = db.execute('''SELECT m.*,m.content AS body FROM conversation_messages m WHERE '''+MSCOPE+'''
                AND (? IS NULL OR m.category=?) AND julianday(m.created_at)>=julianday(?)
                AND julianday(m.created_at)<julianday(?) AND m.content<>''
                ORDER BY m.created_at DESC,CAST(m.message_id AS INTEGER) DESC LIMIT 13''',
                (str(guild_id),category,category,start.isoformat(),end.isoformat()))
            tagged = [('message',dict(r)) for r in rows]
            partial = len(tagged)>12
        else:
            terms = question_terms(question)
            if not terms:
                return [],False
            def score(body):
                text = clean(body or '').casefold()
                return sum(min(text.count(term),3) for term in terms)
            db.create_function('relevance',1,score,deterministic=True)
            rows = db.execute('''SELECT f.id,f.original_filename,f.sha256,f.discord_guild_id,
                f.discord_channel_id,f.discord_message_id,p.page,p.body,d.status,COALESCE(l.label,'') AS location,
                relevance(p.body)+relevance(f.original_filename) AS score FROM files f
                JOIN content_documents d ON d.file_id=f.id AND '''+CURRENT+'''
                JOIN content_pages p ON p.file_id=f.id LEFT JOIN content_locations l ON l.file_id=p.file_id AND l.page=p.page
                WHERE '''+FSCOPE+''' AND (? IS NULL OR f.category=?) AND score>0
                ORDER BY score DESC,f.id DESC,p.page LIMIT 24''',(str(guild_id),category,category))
            tagged = [('file',dict(r)) for r in rows]
            rows = db.execute('''SELECT m.*,m.content AS body,relevance(m.content) AS score
                FROM conversation_messages m WHERE '''+MSCOPE+''' AND (? IS NULL OR m.category=?) AND score>0
                ORDER BY score DESC,m.created_at DESC LIMIT 24''',(str(guild_id),category,category))
            tagged += [('message',dict(r)) for r in rows]
            tagged.sort(key=lambda item:item[1]['score'],reverse=True)
            partial = True
        if question and file_id is not None:
            terms = question_terms(question)
            if terms:
                tagged.sort(key=lambda item:sum(clean(item[1]['body']).casefold().count(term) for term in terms),reverse=True)
        selected,used = [],0
        for kind,row in tagged:
            if len(selected)>=12 or used>=max_chars:
                partial = True
                break
            body = row['body']
            if question:
                terms = question_terms(question)
                term = next((t for t in terms if t in body.casefold()),'')
                text = content_search.snippet(body,term,width=1400) if term else body[:1400]
            else:
                text = body
            item = make_source(row,kind,text)
            prefix = '['+item['label']+']\n'
            remaining = max_chars-used-len(prefix)
            if remaining<=0:
                partial = True
                break
            text = text[:remaining]
            if len(text)<len(body):
                partial = True
            if text.strip():
                item['text'] = prefix+text
                selected.append(item)
                used += len(item['text'])
        return selected,partial


def sources_current(guild_id,channels,items, *, scope=None):
    with connection(channels,scope) as db:
        for item in items:
            if item['kind']=='file':
                row = db.execute('''SELECT f.sha256,p.body FROM files f JOIN content_documents d ON d.file_id=f.id
                    AND '''+CURRENT+''' JOIN content_pages p ON p.file_id=f.id WHERE '''+FSCOPE+
                    ' AND f.id=? AND p.page=?',(str(guild_id),item['key'],item['page'])).fetchone()
            else:
                row = db.execute('SELECT m.revision,m.content FROM conversation_messages m WHERE '+MSCOPE+
                                 ' AND m.message_id=?',(str(guild_id),item['key'])).fetchone()
            if row is None or str(row[0])!=item['version'] or hashlib.sha256(row[1].encode()).hexdigest()!=item['body_hash']:
                return False
        return bool(items)
