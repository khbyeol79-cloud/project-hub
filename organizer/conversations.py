"""Read-only access to conversation channels explicitly published in the library."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3


def discord_url(guild, channel, message):
    values = [str(v) for v in (guild, channel, message)]
    if not all(re.fullmatch(r'[0-9]{1,20}', v) for v in values):
        return None
    return 'https://discord.com/channels/' + '/'.join(values)


class Conversations:
    def __init__(self, db_path, channels=()):
        self.path = Path(db_path).resolve()
        self.channels = tuple(dict.fromkeys(str(c) for c in channels if re.fullmatch(r'[0-9]{1,20}', str(c))))

    def connect(self):
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def search(self, query='', channel='', days=0, offset=0, limit=20, terms=None, message_id=None):
        if not self.channels:
            return {'messages': [], 'more': False, 'channels': []}
        with closing(self.connect()) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'conversation_messages' not in tables:
                return {'messages': [], 'more': False, 'channels': []}
            marks = ','.join('?' for _ in self.channels)
            scope = f'(m.channel_id IN ({marks}) OR m.parent_id IN ({marks}))'
            params = [*self.channels, *self.channels]
            if 'conversation_tombstones' in tables:
                scope += ' AND NOT EXISTS (SELECT 1 FROM conversation_tombstones t WHERE t.message_id=m.message_id)'
            channel_rows = db.execute('SELECT DISTINCT m.channel_id,m.parent_id,m.category FROM conversation_messages m WHERE '+scope, params).fetchall()
            where = scope + (" AND trim(m.content)<>''" if message_id is None else ' AND m.message_id=?')
            if message_id is not None: params.append(message_id)
            if channel:
                where += ' AND (m.channel_id=? OR m.parent_id=?)'
                params.extend([channel, channel])
            if days:
                since = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
                where += ' AND julianday(m.created_at)>=julianday(?)'
                params.append(since)
            words = terms if terms is not None else ([query.casefold()] if query else [])
            if words:
                where += ' AND (' + ' OR '.join('instr(m.search_content,?)>0' for _ in words) + ')'
                params.extend(words)
            # Rank keyword matches before recency; do not load an unbounded archive into RAM.
            ranking = ' + '.join('(instr(m.search_content,?)>0)' for _ in words) or '0'
            rows = db.execute('SELECT m.*, ('+ranking+') AS score FROM conversation_messages m WHERE '+where+
                ' ORDER BY score DESC,julianday(m.created_at) DESC,m.message_id DESC LIMIT ? OFFSET ?',
                [*words, *params, limit+1, offset]).fetchall()
            messages = []
            for row in rows[:limit]:
                item = {k: row[k] for k in ('message_id','guild_id','channel_id','parent_id','category',
                    'author_name','content','created_at','edited_at','revision')}
                item['url'] = discord_url(row['guild_id'],row['channel_id'],row['message_id'])
                item['files'] = [dict(f) for f in db.execute('''SELECT id,original_filename FROM files
                    WHERE discord_message_id=? AND discord_guild_id=? AND discord_channel_id=?
                    ORDER BY id LIMIT 20''', (row['message_id'],row['guild_id'],row['channel_id']))]
                messages.append(item)
            names={r[0]:r[1] for r in db.execute('SELECT DISTINCT discord_channel_id,discord_channel FROM files')}
            categories={'common':'공통','plc':'PLC','vision':'PC','3d_model':'기구제작','meeting':'게시물'}
            channels = [{'id': r['channel_id'], 'name': names.get(r['channel_id']) or
                         categories.get(r['category'],r['category'])+(' · 게시글 '+r['channel_id'][-4:] if r['parent_id'] else '')} for r in channel_rows]
            parents={r['parent_id']:r['category'] for r in channel_rows if r['parent_id'] in self.channels}
            existing={c['id'] for c in channels}
            channels.extend({'id':pid,'name':categories.get(category,category)+' · 전체 게시글'}
                            for pid,category in parents.items() if pid not in existing)
            return {'messages': messages, 'more': len(rows)>limit, 'channels': channels}

    def context(self, anchor, days=0):
        """Bounded same-channel context; never follow a reply across permission scopes."""
        if not ({anchor['channel_id'],anchor['parent_id']} & set(self.channels)):
            return []
        with closing(self.connect()) as db:
            columns={r[1] for r in db.execute('PRAGMA table_info(conversation_messages)')}
            scope="m.guild_id=? AND m.channel_id=? AND trim(m.content)<>''"
            args=[anchor['guild_id'],anchor['channel_id']]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_tombstones'").fetchone():
                scope+=' AND NOT EXISTS (SELECT 1 FROM conversation_tombstones t WHERE t.message_id=m.message_id)'
            if days:
                scope+=' AND julianday(m.created_at)>=julianday(?)'
                args.append((datetime.now(timezone.utc)-timedelta(days=days)).isoformat())
            result=[]
            if 'reply_message_id' in columns:
                result.extend(db.execute('SELECT m.* FROM conversation_messages m WHERE '+scope+''' AND
                    (m.message_id=(SELECT reply_message_id FROM conversation_messages WHERE message_id=?)
                     OR m.reply_message_id=?) ORDER BY julianday(m.created_at) DESC LIMIT 3''',
                    [*args,anchor['message_id'],anchor['message_id']]).fetchall())
            for operator,order in [('<','DESC'),('>','ASC')]:
                result.extend(db.execute('SELECT m.* FROM conversation_messages m WHERE '+scope+
                    f' AND julianday(m.created_at){operator}julianday(?) AND abs(julianday(m.created_at)-julianday(?))<=15.0/1440'
                    f' ORDER BY julianday(m.created_at) {order},m.message_id {order} LIMIT 2',
                    [*args,anchor['created_at'],anchor['created_at']]).fetchall())
            output=[]
            for row in result:
                item=dict(row)
                item['url']=discord_url(row['guild_id'],row['channel_id'],row['message_id'])
                item['files']=[dict(f) for f in db.execute('''SELECT id,original_filename FROM files
                    WHERE discord_message_id=? AND discord_guild_id=? AND discord_channel_id=? ORDER BY id LIMIT 20''',
                    (row['message_id'],row['guild_id'],row['channel_id']))]
                output.append(item)
            return output

    def sources(self, question, days=0, max_chars=6000, channel=''):
        from .retrieval import search_terms
        terms = search_terms(question)
        if question and not terms:
            return []
        rows = self.search(days=days,limit=6,terms=terms,channel=channel)['messages']
        # Keep direct hits first, then round-robin context so one busy thread cannot take every slot.
        candidates=[(row,'검색된 대화') for row in rows]
        groups=[self.context(row,days) for row in rows] if question else []
        for index in range(max((len(g) for g in groups),default=0)):
            for anchor,group in zip(rows,groups):
                if index<len(group): candidates.append((group[index],f"대화 {anchor['message_id']}의 주변·답글 맥락"))
        result=[];seen=set()
        for row,relation in candidates:
            if max_chars<100 or len(result)>=12: break
            if row['message_id'] in seen: continue
            seen.add(row['message_id'])
            label=f"{row['category']} 대화 · {row['author_name']} · {row['created_at']}"
            content=row['content']
            # Include the matched region of a long message, not just its beginning.
            positions=[content.casefold().find(t) for t in terms if t in content.casefold()]
            start=max(0,min(positions)-200) if positions else 0
            attachments=' · '.join(f['original_filename'][:100] for f in row['files'][:5])
            text=(f"대화: {label}\n메시지 ID: {row['message_id']} · {relation}\n"+
                  (f'첨부: {attachments}\n' if attachments else '')+content[start:start+500])[:max_chars]
            if not text.strip(): continue
            result.append({'kind':'message','message_id':row['message_id'],'guild_id':row['guild_id'],
                'channel_id':row['channel_id'],'revision':row['revision'],'url':row['url'],
                'label':label,'text':text,'attachment_ids':[f['id'] for f in row['files']]})
            max_chars-=len(text)
        return result

    def current(self, sources):
        messages=[s for s in sources if s.get('kind')=='message']
        if not messages: return True
        with closing(self.connect()) as db:
            for source in messages:
                row=db.execute('SELECT revision,channel_id,parent_id FROM conversation_messages WHERE message_id=? AND guild_id=?',
                               (source['message_id'],source['guild_id'])).fetchone()
                if (not row or row['revision']!=source['revision'] or row['channel_id']!=source['channel_id']
                        or not ({row['channel_id'],row['parent_id']} & set(self.channels))): return False
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_tombstones'").fetchone():
                    if db.execute('SELECT 1 FROM conversation_tombstones WHERE message_id=?',(source['message_id'],)).fetchone(): return False
        return True
