import asyncio
from contextlib import closing
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject, ArrayObject
import test_file_search as fixtures
from bot import database, content_extract as extraction, content_search as content


def pdf_bytes(texts=('servo motor settings',), *, encrypted=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    cmap = DecodedStreamObject()
    cmap.set_data(b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
        b'/CIDSystemInfo << /Registry (Test) /Ordering (Unicode) /Supplement 0 >> def\n'
        b'/CMapName /Test def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange\n'
        b'2 beginbfchar <01> <C11C> <02> <BCF4> endbfchar endcmap\n'
        b'CMapName currentdict /CMap defineresource pop end end')
    font[NameObject('/ToUnicode')] = writer._add_object(cmap)
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 200 Td (' + text.encode('latin1') + b') Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
    if encrypted:
        writer.encrypt('test-password')
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class ContentFixture(fixtures.CatalogueFixture):
    def setUp(self):
        super().setUp()
        content.initialize()
        self.storage = database.DB_PATH.parent / 'storage'
        self.storage.mkdir()

    def file(self, data=b'hello servo', filename='manual.txt', **kwargs):
        file_id = self.add_file(filename, **kwargs)
        path = self.storage / (str(file_id) + '-' + filename)
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        with closing(database.get_connection()) as conn, conn:
            conn.execute('UPDATE files SET local_path=?, sha256=? WHERE id=?', (str(path), digest, file_id))
        return {'id': file_id, 'local_path': str(path), 'sha256': digest, 'original_filename': filename}

    def job(self, row):
        return {'filename': row['original_filename'], 'path': row['local_path'],
                'sha256': row['sha256'], 'storage': str(self.storage)}

    def index(self, row):
        result = extraction.extract(self.job(row))
        content.save_result(row, result)
        return result


class ExtractionTests(ContentFixture, unittest.TestCase):
    def test_korean_utf8_cp949_and_utf16(self):
        for encoding in ('utf-8', 'utf-8-sig', 'cp949', 'utf-16'):
            result = self.index(self.file('서보 모터\nPLC 설정'.encode(encoding)))
            self.assertEqual(result['pages'], [[1, '서보 모터 PLC 설정']])
            self.assertEqual(result['status'], 'indexed')

    def test_real_pdf_pages_and_korean_cmap(self):
        result = self.index(self.file(pdf_bytes(('first page', 'servo motor', '\x01\x02')), 'manual.pdf'))
        self.assertEqual(result['status'], 'indexed')
        self.assertIn('servo motor', result['pages'][1][1])
        self.assertEqual(result['pages'][2], [3, '서보'])
        rows, total, _ = content.find_content(1, [10], '서보')
        self.assertEqual((total, rows[0]['page']), (1, 3))

    def test_encrypted_blank_binary_and_unsupported_are_explicit(self):
        cases = [(pdf_bytes(encrypted=True), 'secret.pdf', 'encrypted'),
                 (pdf_bytes(('',)), 'blank.pdf', 'no_text'),
                 (b'abc\x00def', 'binary.txt', 'binary_text'),
                 (b'body', 'notes.md', 'unsupported')]
        for data, filename, expected in cases:
            self.assertEqual(self.index(self.file(data, filename))['status'], expected)

    def test_file_size_page_and_character_limits(self):
        row = self.file(b'123456')
        with patch.object(extraction, 'MAX_BYTES', 5):
            self.assertEqual(extraction.extract(self.job(row))['status'], 'too_large')
        with patch.object(extraction, 'MAX_CHARS', 3):
            result = extraction.extract(self.job(row))
            self.assertEqual(result, {'status': 'partial', 'pages': [[1, '123']]})
        row = self.file(pdf_bytes(('one', 'two', 'three')), 'long.pdf')
        with patch.object(extraction, 'MAX_PAGES', 2):
            result = extraction.extract(self.job(row))
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(result['pages']), 2)

    def test_paths_outside_storage_and_hash_changes_are_rejected(self):
        row = self.file()
        job = self.job(row)
        outside = self.storage.parent / 'private.txt'
        outside.write_text('private credential')
        job['path'] = str(outside)
        self.assertEqual(extraction.extract(job)['status'], 'unsafe_path')
        Path(row['local_path']).write_text('changed')
        self.assertEqual(extraction.extract(self.job(row))['status'], 'changed')


class ContentQueryTests(ContentFixture, unittest.TestCase):
    def test_body_search_not_filename_and_literal_queries(self):
        self.index(self.file('서보 모터 100% PLC_설정 Straße'.encode(), 'random.txt'))
        self.index(self.file(b'no match', 'servo.txt'))
        for term in ('서보', '100%', 'plc_', 'STRASSE'):
            rows, total, _ = content.find_content(1, [10], term)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]['original_filename'], 'random.txt')
            self.assertNotIn('body', rows[0])
        self.assertEqual(content.find_content(1, [10], 'servo')[1], 0)
        self.assertEqual(content.find_content(1, [10], "' OR 1=1 --")[1], 0)

    def test_permissions_and_guild_filter_precede_counts_snippets_and_states(self):
        self.index(self.file(b'servo public', channel='10'))
        self.index(self.file(b'servo secret', channel='20'))
        self.index(self.file(b'servo foreign', guild='2'))
        self.file(channel='20')
        rows, total, states = content.find_content(1, [10], 'servo')
        self.assertEqual(total, 1)
        self.assertEqual(states, {'indexed': 1})
        self.assertIn('public', rows[0]['excerpt'])
        self.assertNotIn('secret', str(rows))
        self.assertEqual(content.find_content(1, [], 'servo'), ([], 0, {}))

    def test_category_file_paging_and_first_matching_pdf_page(self):
        for i in range(7):
            self.index(self.file(pdf_bytes(('nothing', 'servo first', 'servo again')), f'manual{i}.pdf', category='plc'))
        self.index(self.file(b'servo other category'))
        rows, total, _ = content.find_content(1, [10], 'servo', category='plc')
        self.assertEqual((total, len(rows), rows[0]['page'], rows[0]['matching_pages']), (7, 5, 2, 2))
        self.assertEqual(len(content.find_content(1, [10], 'servo', category='plc', page=2)[0]), 2)
        self.assertEqual(content.find_content(1, [10], 'servo', category='plc', page=3)[0], [])

    def test_changed_record_hides_stale_index_then_atomically_replaces_it(self):
        row = self.file(b'old keyword')
        self.index(row)
        data = b'new phrase'
        row['sha256'] = hashlib.sha256(data).hexdigest()
        Path(row['local_path']).write_bytes(data)
        with closing(database.get_connection()) as conn, conn:
            conn.execute('UPDATE files SET sha256=? WHERE id=?', (row['sha256'], row['id']))
        self.assertEqual(content.find_content(1, [10], 'old')[1:], (0, {'pending': 1}))
        self.index(row)
        self.assertEqual(content.find_content(1, [10], 'new')[1], 1)
        self.assertEqual(content.find_content(1, [10], 'old')[1], 0)

    def test_stale_worker_cannot_attach_text_to_updated_file(self):
        row = self.file()
        with closing(database.get_connection()) as conn, conn:
            conn.execute("UPDATE files SET sha256='changed' WHERE id=?", (row['id'],))
        self.assertFalse(content.save_result(row, {'status': 'indexed', 'pages': [[1, 'stale']]}))
        self.assertEqual(content.find_content(1, [10], 'stale')[1], 0)

    def test_restart_skips_indexed_files_and_failed_extraction_retries_later(self):
        ok = self.file()
        self.index(ok)
        failed = self.file(b'broken')
        content.save_result(failed, {'status': 'failed', 'pages': []})
        content.initialize()
        self.assertEqual(content.due_files(now=0), [])
        self.assertEqual([r['id'] for r in content.due_files(now=10**12)], [failed['id']])

    def test_snippet_preserves_case_and_queries_validate(self):
        self.assertIn('Straße', content.snippet('Lead Straße SERVO tail', 'STRASSE'))
        self.assertIn('SERVO', content.snippet('before ' * 100 + 'SERVO after ' * 20, 'servo'))
        for keyword, page in [('', 1), (' ', 1), ('a' * 101, 1), ('a', 0), ('a', 10001)]:
            with self.assertRaises(ValueError):
                content.find_content(1, [10], keyword, page=page)


class IndexWorkerTests(ContentFixture, unittest.IsolatedAsyncioTestCase):
    async def test_actual_worker_indexes_existing_and_new_files_without_duplicates(self):
        self.file('서보 제어'.encode())
        worker = content.ContentIndexer(self.storage)
        self.assertEqual(await worker.scan_once(), 1)
        self.assertEqual(await worker.scan_once(), 0)
        self.file(pdf_bytes(('servo pdf',)), 'new.pdf')
        self.assertEqual(await worker.scan_once(), 1)
        self.assertEqual(content.find_content(1, [10], '서보')[1], 1)
        self.assertEqual(content.find_content(1, [10], 'servo')[1], 1)

    async def test_bad_pdf_does_not_block_next_file(self):
        self.file(b'not a pdf', 'broken.pdf')
        self.file(b'servo healthy', 'healthy.txt')
        self.assertEqual(await content.ContentIndexer(self.storage).scan_once(), 2)
        rows, total, states = content.find_content(1, [10], 'servo')
        self.assertEqual((total, states), (1, {'failed': 1, 'indexed': 1}))

    async def test_worker_timeout_and_cancellation_kill_child(self):
        for cancel in (False, True):
            entered = asyncio.Event()
            process = SimpleNamespace(returncode=None)
            def kill():
                process.returncode = -9
            async def communicate(*args):
                if process.returncode is not None:
                    return b'', b''
                entered.set()
                await asyncio.Event().wait()
            process.kill, process.communicate = kill, communicate
            with patch.object(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), \
                    patch.object(content, 'WORKER_TIMEOUT', 0.01):
                task = asyncio.create_task(content.ContentIndexer(self.storage).extract_one(self.file()))
                await entered.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual((await task)['status'], 'failed')
                self.assertEqual(process.returncode, -9)


class ContentCommandTests(ContentFixture, unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CommandTests.asyncSetUp
    interaction = fixtures.CommandTests.interaction

    async def test_command_shows_safe_private_excerpt_page_and_drive_link(self):
        self.index(self.file(pdf_bytes(('intro', 'servo @everyone [link](https://bad.test)')), 'guide.pdf'))
        self.index(self.file(b'servo hidden', channel='20'))
        interaction = self.interaction()
        channels = {10: fixtures.channel(), 20: fixtures.channel(20, visible=False)}
        with patch.object(self.client, 'get_channel', side_effect=channels.get):
            await self.commands.tree.get_command('내용검색').callback(interaction, 'servo')
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        embed = interaction.edit_original_response.call_args.kwargs['embed'].to_dict()
        self.assertEqual(embed['title'], '파일 내용 검색')
        self.assertEqual(len(embed['fields']), 1)
        self.assertIn('PDF 2쪽', str(embed))
        self.assertIn('drive.google.com', str(embed))
        self.assertNotIn('@everyone', str(embed))
        self.assertNotIn('hidden', str(embed))

    async def test_pending_and_excluded_counts_only_include_accessible_supported_files(self):
        self.file()
        self.index(self.file(pdf_bytes(encrypted=True), 'secret.pdf'))
        self.file(channel='20')
        self.file(filename='notes.md')
        interaction = self.interaction()
        channels = {10: fixtures.channel(), 20: fixtures.channel(20, visible=False)}
        with patch.object(self.client, 'get_channel', side_effect=channels.get):
            await self.commands.tree.get_command('내용검색').callback(interaction, 'servo')
        footer = interaction.edit_original_response.call_args.kwargs['embed'].footer.text
        self.assertIn('준비 중 1', footer)
        self.assertIn('추출 제외 1', footer)

    async def test_schema_and_blank_input(self):
        command = self.commands.tree.get_command('내용검색')
        self.assertTrue(command.guild_only)
        self.assertEqual([p['name'] for p in command.to_dict(self.commands.tree)['options']],
                         ['키워드', '분류', '페이지'])
        interaction = self.interaction()
        await command.callback(interaction, '  ')
        interaction.response.defer.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
