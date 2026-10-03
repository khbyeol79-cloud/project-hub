"""Durable local content index and permission-scoped substring queries."""
import asyncio
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import signal
import sys
import tempfile
import time

if __package__:
    from . import database
    from .content_extract import clean_text, MAX_CHARS, MAX_PAGES
    from .office_extract import MAX_PARTS
    from .ocr_extract import IMAGE_SUFFIXES
else:
    import database
    from content_extract import clean_text, MAX_CHARS, MAX_PAGES
    from office_extract import MAX_PARTS
    from ocr_extract import IMAGE_SUFFIXES

INDEX_VERSION = 1
OFFICE_INDEX_VERSION = 2
OCR_INDEX_VERSION = 3
WORKER_TIMEOUT = 20
OCR_WORKER_TIMEOUT = 120
logger = logging.getLogger('project-hub')
OFFICE_SUFFIX = "lower(substr(f.original_filename, -5)) IN ('.docx', '.xlsx')"
PDF_SUFFIX = "lower(substr(f.original_filename, -4)) = '.pdf'"
OCR_SUFFIX = "(lower(substr(f.original_filename, -4)) IN ('.pdf', '.png', '.jpg') OR lower(substr(f.original_filename, -5)) IN ('.jpeg', '.webp'))"
ELIGIBLE = "(lower(substr(f.original_filename, -4)) = '.txt' OR " + OFFICE_SUFFIX + ' OR ' + OCR_SUFFIX + ")"
INDEX_FORMAT = f"CASE WHEN {OFFICE_SUFFIX} THEN {OFFICE_INDEX_VERSION} WHEN {OCR_SUFFIX} THEN {OCR_INDEX_VERSION} ELSE {INDEX_VERSION} END"
STATUSES = {'indexed', 'partial', 'no_text', 'unsupported', 'unsafe_path', 'too_large',
            'changed', 'binary_text', 'encrypted', 'missing', 'encoding', 'failed', 'ocr_pending'}


def initialize():
    with closing(database.get_connection()) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS content_documents (
            file_id INTEGER PRIMARY KEY, source_sha256 TEXT NOT NULL,
            index_version INTEGER NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1,
            next_retry_at REAL NOT NULL DEFAULT 0, indexed_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS content_pages (
            file_id INTEGER NOT NULL, page INTEGER NOT NULL, body TEXT NOT NULL,
            search_body TEXT NOT NULL, PRIMARY KEY(file_id, page))''')
        # A separate table keeps running older collectors compatible during deployment.
        conn.execute('''CREATE TABLE IF NOT EXISTS content_locations (
            file_id INTEGER NOT NULL, page INTEGER NOT NULL, label TEXT NOT NULL,
            PRIMARY KEY(file_id, page))''')


def due_files(now=None, limit=10):
    with closing(database.get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute('''SELECT f.id, f.sha256, f.local_path,
            f.original_filename FROM files f LEFT JOIN content_documents d ON d.file_id=f.id
            WHERE d.file_id IS NULL OR d.source_sha256 != f.sha256 OR d.index_version != ''' + INDEX_FORMAT + '''
               OR (d.status IN ('failed', 'missing', 'changed', 'ocr_pending') AND d.next_retry_at <= ?)
            ORDER BY d.file_id IS NOT NULL, f.id LIMIT ?''',
            (time.time() if now is None else now, limit))]


def save_result(row, result):
    status, pages = result['status'], result['pages']
    is_office = Path(row['original_filename']).suffix.lower() in ('.docx', '.xlsx')
    suffix = Path(row['original_filename']).suffix.lower()
    version = OFFICE_INDEX_VERSION if is_office else (OCR_INDEX_VERSION if suffix in IMAGE_SUFFIXES | {'.pdf'} else INDEX_VERSION)
    locations = result.get('locations', {})
    if status not in STATUSES or len(pages) > (MAX_PARTS if is_office else MAX_PAGES) or sum(len(p[1]) for p in pages) > MAX_CHARS:
        raise ValueError('Invalid extraction result')
    if status not in ('indexed', 'partial', 'ocr_pending') and pages:
        raise ValueError('Non-searchable result contains text')
    with closing(database.get_connection()) as conn, conn:
        # Only attach text to the exact record version the worker verified.
        current = conn.execute('SELECT sha256 FROM files WHERE id=?', (row['id'],)).fetchone()
        if current is None or current[0] != row['sha256']:
            return False
        old = conn.execute('SELECT source_sha256, attempts FROM content_documents WHERE file_id=?', (row['id'],)).fetchone()
        attempts = old[1] + 1 if old and old[0] == row['sha256'] else 1
        retry = time.time() + min(86400, 300 * 2 ** min(attempts - 1, 9))
        conn.execute('DELETE FROM content_pages WHERE file_id=?', (row['id'],))
        conn.execute('DELETE FROM content_locations WHERE file_id=?', (row['id'],))
        conn.executemany('INSERT INTO content_pages VALUES (?, ?, ?, ?)',
                         [(row['id'], number, body, body.casefold()) for number, body in pages])
        conn.executemany('INSERT INTO content_locations VALUES (?, ?, ?)',
                         [(row['id'], number, locations[str(number)][:240])
                          for number, _ in pages if str(number) in locations])
        conn.execute('''INSERT INTO content_documents
            (file_id, source_sha256, index_version, status, attempts, next_retry_at)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(file_id) DO UPDATE SET
            source_sha256=excluded.source_sha256, index_version=excluded.index_version,
            status=excluded.status, attempts=excluded.attempts, next_retry_at=excluded.next_retry_at,
            indexed_at=CURRENT_TIMESTAMP''',
            (row['id'], row['sha256'], version, status, attempts, retry))
        return True


class ContentIndexer:
    def __init__(self, storage=None):
        self.storage = Path(storage) if storage else database.BASE_DIR / 'storage'

    async def extract_one(self, row):
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}}
        with tempfile.TemporaryDirectory(prefix='project-hub-content-') as scratch:
            return await self._worker(row, env, scratch)

    async def _worker(self, row, env, scratch):
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-I', str(Path(__file__).with_name('content_extract.py')),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=(os.name == 'posix'))
        payload = json.dumps({'path': row['local_path'], 'storage': str(self.storage),
                              'filename': row['original_filename'], 'sha256': row['sha256'],
                              'scratch': scratch}).encode()
        timeout = OCR_WORKER_TIMEOUT if Path(row['original_filename']).suffix.lower() in IMAGE_SUFFIXES | {'.pdf'} else WORKER_TIMEOUT
        try:
            output, _ = await asyncio.wait_for(process.communicate(payload), timeout)
            if process.returncode != 0:
                return {'status': 'failed', 'pages': []}
            return json.loads(output)
        except (TimeoutError, ValueError):
            return {'status': 'failed', 'pages': []}
        finally:
            # Stop render/OCR descendants too, including after an unexpected worker exit.
            if os.name == 'posix' and getattr(process, 'pid', None):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.communicate()

    async def scan_once(self):
        rows = await asyncio.to_thread(due_files)
        for row in rows:
            result = await self.extract_one(row)
            await asyncio.to_thread(save_result, row, result)
            logger.info('CONTENT_INDEXED | file_id=%s | status=%s | pages=%s',
                        row['id'], result['status'], len(result['pages']))
        return len(rows)

    async def run(self):
        while True:
            try:
                count = await self.scan_once()
                await asyncio.to_thread(database.record_health, 'content_index')
            except Exception as error:
                count = 0
                logger.error('CONTENT_INDEX_FAILED | error=%s', type(error).__name__)
                await asyncio.to_thread(database.record_health, 'content_index', type(error).__name__)
            await asyncio.sleep(1 if count == 10 else 30)


def snippet(body, keyword, width=180):
    needle = clean_text(keyword).casefold()
    position = body.casefold().find(needle)
    # Preserve original casing even where casefold expands a character (ß -> ss).
    folded, index = 0, 0
    for index, char in enumerate(body):
        if folded >= position:
            break
        folded += len(char.casefold())
    start = max(0, index - 55)
    end = min(len(body), start + max(width, len(keyword) + 70))
    return ('…' if start else '') + body[start:end] + ('…' if end < len(body) else '')


def find_content(guild_id, channels, keyword, category=None, page=1):
    needle = clean_text(keyword).casefold()
    if not needle or len(keyword) > 100 or not 1 <= page <= 10000:
        raise ValueError('Invalid content query')
    clause = 'f.discord_guild_id=? AND f.discord_channel_id IN (SELECT id FROM visible_channels) AND ' + ELIGIBLE
    values = [str(guild_id)]
    if category:
        clause += ' AND f.category=?'
        values.append(category)
    with closing(database.get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TEMP TABLE visible_channels (id TEXT PRIMARY KEY)')
        conn.executemany('INSERT OR IGNORE INTO visible_channels VALUES (?)', [(str(c),) for c in channels])
        # Match the current hash and index format to prevent stale text disclosure.
        # Keep existing native PDF text usable while its OCR upgrade is queued.
        current_join = 'd.file_id=f.id AND d.source_sha256=f.sha256 AND (d.index_version=' + INDEX_FORMAT + f' OR ({PDF_SUFFIX} AND d.index_version={INDEX_VERSION}))'
        states = dict(conn.execute('''SELECT COALESCE(d.status, 'pending'), COUNT(*) FROM files f
            LEFT JOIN content_documents d ON ''' + current_join + ' WHERE ' + clause +
            " GROUP BY COALESCE(d.status, 'pending')", values).fetchall())
        match = ''' FROM files f JOIN content_documents d ON ''' + current_join + '''
            JOIN content_pages p ON p.file_id=f.id WHERE ''' + clause + '''
            AND d.status IN ('indexed', 'partial', 'ocr_pending') AND instr(p.search_body, ?) > 0'''
        parameters = [*values, needle]
        total = conn.execute('SELECT COUNT(DISTINCT f.id)' + match, parameters).fetchone()[0]
        query = '''WITH hits AS (SELECT f.id, MIN(p.page) AS page, COUNT(*) AS matching_pages''' + match + '''
            GROUP BY f.id) SELECT f.id, f.original_filename, f.uploaded_at, f.category,
                f.discord_guild_id, f.discord_channel_id, f.discord_message_id,
                f.google_drive_file_id, f.upload_status, d.status AS content_status,
                h.page, h.matching_pages, p.body, COALESCE(l.label, '') AS location
            FROM hits h JOIN files f ON f.id=h.id JOIN content_pages p ON p.file_id=h.id AND p.page=h.page
            JOIN content_documents d ON d.file_id=f.id
            LEFT JOIN content_locations l ON l.file_id=f.id AND l.page=h.page
            ORDER BY julianday(f.uploaded_at) DESC, f.id DESC LIMIT 5 OFFSET ?'''
        rows = [dict(row) for row in conn.execute(query, [*parameters, (page - 1) * 5])]
        for row in rows:
            row['excerpt'] = snippet(row.pop('body'), keyword)
        return rows, total, states


async def index_existing():
    initialize()
    worker = ContentIndexer()
    while await worker.scan_once() == 10:
        pass


if __name__ == '__main__':
    # Explicit local maintenance command; does not connect to Discord or Drive.
    asyncio.run(index_existing())
