"""Local conversation archive for configured guild channels and forum threads."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import sqlite3

import discord

if __package__:
    from . import database
    from .content_extract import clean_text
    from .content_search import snippet
else:
    import database
    from content_extract import clean_text
    from content_search import snippet

logger = logging.getLogger('project-hub')
MAX_CONTENT = 8000


def initialize():
    with closing(database.get_connection()) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS conversation_messages (
            message_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            parent_id TEXT, category TEXT NOT NULL, author_id TEXT NOT NULL, author_name TEXT NOT NULL,
            content TEXT NOT NULL, search_content TEXT NOT NULL, created_at TEXT NOT NULL,
            edited_at TEXT, revision REAL NOT NULL, reply_message_id TEXT, attachment_count INTEGER NOT NULL,
            saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE INDEX IF NOT EXISTS conversation_scope
            ON conversation_messages(guild_id, channel_id, category, created_at)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS conversation_tombstones (
            message_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS conversation_cursors (
            channel_id TEXT PRIMARY KEY, last_message_id INTEGER NOT NULL)''')


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def save_message(message, category):
    content = str(message.content or '')[:MAX_CONTENT]
    created = stamp(message.created_at)
    edited = stamp(message.edited_at) if message.edited_at else None
    reference = getattr(message, 'reference', None)
    reply = str(reference.message_id) if (reference and reference.message_id
        and reference.channel_id == message.channel.id
        and getattr(reference, 'guild_id', message.guild.id) == message.guild.id) else None
    values = (str(message.id), str(message.guild.id), str(message.channel.id),
        str(message.channel.parent_id) if getattr(message.channel, 'parent_id', None) else None,
        category, str(message.author.id), str(getattr(message.author, 'display_name', message.author))[:200],
        content, clean_text(content).casefold(), created.isoformat(), edited.isoformat() if edited else None,
        (edited or created).timestamp(), reply, len(message.attachments))
    with closing(database.get_connection()) as conn, conn:
        # The tombstone check is part of the write, so a late history fetch cannot resurrect deletion.
        conn.execute('''INSERT INTO conversation_messages
            (message_id,guild_id,channel_id,parent_id,category,author_id,author_name,content,
             search_content,created_at,edited_at,revision,reply_message_id,attachment_count)
            SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (
                SELECT 1 FROM conversation_tombstones WHERE message_id=?)
            ON CONFLICT(message_id) DO UPDATE SET author_name=excluded.author_name,
                content=excluded.content, search_content=excluded.search_content,
                edited_at=excluded.edited_at, revision=excluded.revision,
                reply_message_id=excluded.reply_message_id, attachment_count=excluded.attachment_count,
                saved_at=CURRENT_TIMESTAMP
            WHERE excluded.revision >= conversation_messages.revision
                AND excluded.guild_id=conversation_messages.guild_id
                AND excluded.channel_id=conversation_messages.channel_id''', (*values, str(message.id)))


def update_content(payload):
    """Apply complete text from a partial gateway event to an already known author."""
    data = payload.data
    if 'content' not in data:
        return True
    if not data.get('edited_timestamp'):
        return False
    edited = stamp(data['edited_timestamp'])
    text = str(data['content'] or '')[:MAX_CONTENT]
    ids = (str(payload.message_id), str(payload.guild_id), str(payload.channel_id))
    with closing(database.get_connection()) as conn, conn:
        row = conn.execute('SELECT revision FROM conversation_messages WHERE message_id=? AND guild_id=? AND channel_id=?', ids).fetchone()
        if not row:
            return bool(conn.execute('SELECT 1 FROM conversation_tombstones WHERE message_id=?', ids[:1]).fetchone())
        conn.execute('''UPDATE conversation_messages SET content=?,search_content=?,edited_at=?,
            revision=?,saved_at=CURRENT_TIMESTAMP WHERE message_id=? AND guild_id=? AND channel_id=?
            AND revision<=?''', (text, clean_text(text).casefold(), edited.isoformat(), edited.timestamp(),
                                  *ids, edited.timestamp()))
        return True


def delete_messages(guild_id, channel_id, message_ids):
    ids = [(str(mid), str(guild_id), str(channel_id)) for mid in message_ids]
    with closing(database.get_connection()) as conn, conn:
        conn.executemany('INSERT OR IGNORE INTO conversation_tombstones (message_id,guild_id,channel_id) VALUES (?,?,?)', ids)
        conn.executemany('DELETE FROM conversation_messages WHERE message_id=? AND guild_id=? AND channel_id=?', ids)


def initialize_channel_cursor(channel_id, fallback):
    # Forum threads also start with a bounded first import, rather than all past history.
    fallback = fallback or discord.utils.time_snowflake(datetime.now(timezone.utc) - timedelta(hours=24))
    with closing(database.get_connection()) as conn, conn:
        conn.execute('INSERT OR IGNORE INTO conversation_cursors VALUES (?,?)', (str(channel_id), fallback))


def get_channel_cursor(channel_id):
    with closing(database.get_connection()) as conn:
        row = conn.execute('SELECT last_message_id FROM conversation_cursors WHERE channel_id=?', (str(channel_id),)).fetchone()
        return row[0] if row else None


def advance_channel_cursor(channel_id, message_id):
    with closing(database.get_connection()) as conn, conn:
        conn.execute('UPDATE conversation_cursors SET last_message_id=MAX(last_message_id,?) WHERE channel_id=?',
                     (int(message_id), str(channel_id)))


def candidate_channels(guild_id, category=None):
    with closing(database.get_connection()) as conn:
        return [r[0] for r in conn.execute('''SELECT DISTINCT channel_id FROM conversation_messages
            WHERE guild_id=? AND (? IS NULL OR category=?)''', (str(guild_id), category, category))]


def find_messages(guild_id, channels, keyword, category=None, page=1, *, scope=None):
    needle = clean_text(keyword).casefold()
    if not needle or len(keyword) > 100 or not 1 <= page <= 10000:
        raise ValueError('Invalid conversation query')
    with closing(database.get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TEMP TABLE visible_channels (id TEXT PRIMARY KEY)')
        conn.executemany('INSERT OR IGNORE INTO visible_channels VALUES (?)', [(str(cid),) for cid in channels])
        clause = '''m.guild_id=? AND m.channel_id IN (SELECT id FROM visible_channels)
            AND (? IS NULL OR m.category=?) AND instr(m.search_content,?)>0'''
        if scope is not None:
            if __package__:
                from . import projects
            else:
                import projects
            clause += ' AND ' + projects.predicate(scope, 'm.category')
        values = [str(guild_id), category, category, needle]
        total = conn.execute('SELECT COUNT(*) FROM conversation_messages m WHERE '+clause, values).fetchone()[0]
        rows = [dict(r) for r in conn.execute('''SELECT m.message_id,m.guild_id,m.channel_id,m.category,
            m.author_name,m.content,m.created_at,m.edited_at,m.reply_message_id,m.attachment_count,
            (SELECT COUNT(*) FROM files f WHERE f.discord_message_id=m.message_id
                AND f.discord_guild_id=m.guild_id AND f.discord_channel_id=m.channel_id) AS stored_files
            FROM conversation_messages m WHERE '''+clause+'''
            ORDER BY m.created_at DESC, CAST(m.message_id AS INTEGER) DESC LIMIT 5 OFFSET ?''',
            [*values, (page-1)*5])]
        for row in rows:
            row['excerpt'] = snippet(clean_text(row.pop('content')), keyword)
        return rows, total


class MessageArchive:
    def __init__(self, client, channels, forums):
        self.client, self.channels, self.forums = client, dict(channels), dict(forums)

    def category(self, channel):
        return self.channels.get(str(channel.id)) or self.channels.get(str(getattr(channel, 'parent_id', ''))) or self.forums.get(str(getattr(channel, 'parent_id', '')))

    async def save(self, message, *, upload=False):
        category = self.category(message.channel)
        if (not message.guild or message.author.bot or category is None
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)):
            return True
        try:
            await asyncio.to_thread(save_message, message, category)
            return True
        except Exception as error:
            logger.error('MESSAGE_SAVE_FAILED | message_id=%s | error=%s', message.id, type(error).__name__)
            return False

    async def event_channel(self, payload):
        if payload.guild_id is None:
            return None
        channel = self.client.get_channel(payload.channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(payload.channel_id)
        if (not getattr(channel, 'guild', None) or channel.guild.id != payload.guild_id
                or self.category(channel) is None):
            return None
        return channel

    async def edited(self, payload):
        if 'content' not in payload.data:
            return
        channel = await self.event_channel(payload)
        if channel is None:
            return
        if not await asyncio.to_thread(update_content, payload):
            try:
                message = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                await asyncio.to_thread(delete_messages, payload.guild_id, payload.channel_id, [payload.message_id])
            else:
                if not await self.save(message):
                    raise RuntimeError('MessageSaveFailed')

    async def deleted(self, payload):
        if await self.event_channel(payload) is not None:
            ids = payload.message_ids if hasattr(payload, 'message_ids') else [payload.message_id]
            await asyncio.to_thread(delete_messages, payload.guild_id, payload.channel_id, ids)
