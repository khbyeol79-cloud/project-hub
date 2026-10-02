import asyncio
from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
import test_content as fixtures
import test_file_search as search_fixtures
from bot import database, content_extract as extraction, content_search as content, ocr_extract as ocr

SAMPLE = Path(__file__).with_name('fixtures') / 'ocr-sample.png'
TOOLS = bool(shutil.which('tesseract') and shutil.which('pdftoppm'))


def scanned_pdf(*, mixed=False, count=1):
    stream = io.BytesIO()
    with Image.open(SAMPLE) as source:
        source.convert('RGB').save(stream, format='PDF', resolution=150)
    writer = PdfWriter()
    if mixed:
        writer.append(PdfReader(io.BytesIO(fixtures.pdf_bytes(('native beginning',)))))
    for _ in range(count):
        writer.append(PdfReader(io.BytesIO(stream.getvalue())))
    if mixed:
        writer.append(PdfReader(io.BytesIO(fixtures.pdf_bytes(('native ending',)))))
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class OCRExtractionTests(fixtures.ContentFixture, unittest.TestCase):
    def test_native_and_blank_pdf_do_not_run_ocr(self):
        with patch.object(ocr, 'OCR') as worker:
            result = self.index(self.file(fixtures.pdf_bytes(('native text', '')), 'manual.pdf'))
        worker.assert_not_called()
        self.assertEqual(result['pages'], [[1, 'native text']])

    def test_scanned_pdf_mixes_original_and_ocr_text_with_page_numbers(self):
        with patch.object(ocr.OCR, 'pdf_page', return_value='서보 모터 SERVO') as read:
            result = self.index(self.file(scanned_pdf(mixed=True), 'scanned.pdf'))
        self.assertEqual(read.call_args.args[1], 2)
        self.assertEqual(result['status'], 'indexed')
        self.assertEqual(len(result['pages']), 3)
        rows, total, _ = content.find_content(1, [10], '서보')
        self.assertEqual((total, rows[0]['location']), (1, 'PDF 2쪽 · OCR'))

    def test_inline_pdf_images_are_detected_without_xobjects(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        page[NameObject('/Resources')] = DictionaryObject()
        stream = DecodedStreamObject()
        stream.set_data(b'q 20 0 0 20 0 0 cm BI /W 1 /H 1 /BPC 8 /CS /RGB ID \x00\x00\x00 EI Q')
        page[NameObject('/Contents')] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        with patch.object(ocr.OCR, 'pdf_page', return_value='inline image content') as read:
            result = self.index(self.file(output.getvalue(), 'inline.pdf'))
        read.assert_called_once()
        self.assertEqual(result['pages'], [[1, 'inline image content']])

    def test_pdf_ocr_cap_keeps_later_native_text_and_marks_partial(self):
        with patch.object(ocr, 'MAX_OCR_PAGES', 1), \
                patch.object(ocr.OCR, 'run', return_value=b''), \
                patch.object(ocr.OCR, 'image', return_value=('first scanned page', False)), \
                patch.object(Path, 'read_bytes', return_value=SAMPLE.read_bytes()):
            # Call the PDF helper directly so the read_bytes stub cannot replace the source PDF.
            data = scanned_pdf(mixed=True, count=2)
            row = self.file(data, 'long.pdf')
            result = extraction.extract(self.job(row))
        self.assertEqual(result['status'], 'partial')
        self.assertIn([4, 'native ending'], result['pages'])
        self.assertEqual(result['locations'], {'2': 'PDF 2쪽 · OCR'})

    def test_transient_ocr_failure_preserves_native_text_and_retries(self):
        row = self.file(scanned_pdf(mixed=True), 'mixed.pdf')
        with patch.object(ocr.OCR, 'pdf_page', side_effect=ocr.OCRUnavailable):
            result = self.index(row)
        self.assertEqual(result['status'], 'ocr_pending')
        self.assertEqual(content.find_content(1, [10], 'native')[1], 1)
        self.assertEqual(content.due_files(now=0), [])
        self.assertEqual([r['id'] for r in content.due_files(now=10**12)], [row['id']])
        with patch.object(ocr.OCR, 'pdf_page', return_value='restored OCR'):
            self.index(row)
        self.assertEqual(content.find_content(1, [10], 'restored')[1], 1)
        self.assertEqual(content.due_files(), [])

    def test_image_formats_and_location(self):
        for suffix, fmt in [('.PNG', 'PNG'), ('.jpg', 'JPEG'), ('.JPEG', 'JPEG'), ('.webp', 'WEBP')]:
            data = io.BytesIO()
            with Image.open(SAMPLE) as source:
                source.save(data, format=fmt)
            with patch.object(ocr.OCR, 'run', return_value='서보 image'.encode()):
                result = self.index(self.file(data.getvalue(), 'photo' + suffix))
            self.assertEqual(result['status'], 'indexed')
            self.assertEqual(result['locations'], {'1': '이미지 OCR'})
        self.assertEqual(content.find_content(1, [10], '서보')[1], 4)

    def test_images_obey_size_hash_and_path_guards(self):
        row = self.file(SAMPLE.read_bytes(), 'guard.png')
        with patch.object(ocr, 'MAX_PIXELS', 100):
            self.assertEqual(self.index(row)['status'], 'too_large')
        with patch.object(extraction, 'MAX_BYTES', 100):
            self.assertEqual(extraction.extract(self.job(row))['status'], 'too_large')
        Path(row['local_path']).write_bytes(b'changed')
        with patch.object(ocr.OCR, 'image') as read:
            self.assertEqual(extraction.extract(self.job(row))['status'], 'changed')
        read.assert_not_called()
        outside = self.storage.parent / 'outside.png'
        outside.write_bytes(SAMPLE.read_bytes())
        job = self.job(row)
        job['path'] = str(outside)
        self.assertEqual(extraction.extract(job)['status'], 'unsafe_path')

    def test_exif_orientation_transparency_and_dimension_limit(self):
        source = Image.new('RGBA', (320, 120), (0, 0, 0, 0))
        exif = Image.Exif()
        exif[274] = 6
        buffer = io.BytesIO()
        source.save(buffer, 'PNG', exif=exif)
        with ocr.OCR() as worker, patch.object(ocr, 'MAX_EDGE', 160):
            def inspect(args):
                with Image.open(args[1]) as normalized:
                    self.assertEqual(normalized.size, (60, 160))
                    self.assertEqual(normalized.convert('RGB').getpixel((0, 0)), (255, 255, 255))
                return b'servo'
            with patch.object(worker, 'run', side_effect=inspect):
                self.assertEqual(worker.image(buffer.getvalue()), ('servo', False))

    def test_animated_image_reports_first_frame_only(self):
        buffer = io.BytesIO()
        Image.new('RGB', (20, 20), 'white').save(buffer, 'WEBP', save_all=True,
            append_images=[Image.new('RGB', (20, 20), 'black')], duration=100)
        with patch.object(ocr.OCR, 'run', return_value=b'first frame'):
            result = self.index(self.file(buffer.getvalue(), 'animated.webp'))
        self.assertEqual(result['status'], 'partial')

    def test_missing_engine_retries_and_deadline_stops_new_work(self):
        with patch.object(ocr.OCR, 'run', side_effect=ocr.OCRUnavailable):
            result = self.index(self.file(SAMPLE.read_bytes(), 'photo.png'))
        self.assertEqual(result['status'], 'ocr_pending')
        with ocr.OCR() as worker:
            worker.deadline = 0
            with patch.object(subprocess, 'run') as run:
                with self.assertRaises(ocr.OCRLimit):
                    worker.run(['tesseract'])
            run.assert_not_called()

    def test_ocr_subprocess_does_not_receive_secrets_or_extra_threads(self):
        with ocr.OCR() as worker, patch.dict(os.environ, {'DISCORD_TOKEN': 'synthetic-secret'}), \
                patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'ok')) as run:
            self.assertEqual(worker.run(['tesseract', 'test.png', 'stdout']), b'ok')
        env = run.call_args.kwargs['env']
        self.assertNotIn('DISCORD_TOKEN', env)
        self.assertEqual(env['OMP_THREAD_LIMIT'], '1')
        self.assertLessEqual(run.call_args.kwargs['timeout'], ocr.STEP_TIMEOUT)

    def test_upgrade_reindexes_pdf_and_photos_without_hiding_old_pdf_text(self):
        text = self.file(b'unchanged TXT')
        self.index(text)
        pdf = self.file(fixtures.pdf_bytes(('existing PDF content',)), 'old.pdf')
        self.index(pdf)
        photo = self.file(SAMPLE.read_bytes(), 'old.png')
        with closing(database.get_connection()) as conn, conn:
            conn.execute('UPDATE content_documents SET index_version=1 WHERE file_id=?', (pdf['id'],))
            conn.execute('INSERT INTO content_documents (file_id,source_sha256,index_version,status) VALUES (?,?,1,?)',
                         (photo['id'], photo['sha256'], 'unsupported'))
        self.assertEqual({r['id'] for r in content.due_files()}, {pdf['id'], photo['id']})
        self.assertEqual(content.find_content(1, [10], 'existing')[1], 1)
        self.assertEqual(content.find_content(1, [10], 'unchanged')[1], 1)


class OCRCommandTests(fixtures.ContentFixture, unittest.IsolatedAsyncioTestCase):
    asyncSetUp = search_fixtures.CommandTests.asyncSetUp
    interaction = search_fixtures.CommandTests.interaction

    async def test_image_results_are_private_and_only_from_permitted_channels(self):
        with patch.object(ocr.OCR, 'run', return_value='서보 @everyone'.encode()):
            self.index(self.file(SAMPLE.read_bytes(), 'public.png'))
            self.index(self.file(SAMPLE.read_bytes(), 'secret.jpg', channel='20'))
            self.index(self.file(SAMPLE.read_bytes(), 'foreign.png', guild='2'))
        interaction = self.interaction()
        channels = {10: search_fixtures.channel(), 20: search_fixtures.channel(20, visible=False)}
        with patch.object(self.client, 'get_channel', side_effect=channels.get):
            await self.commands.tree.get_command('내용검색').callback(interaction, '서보')
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        embed = interaction.edit_original_response.call_args.kwargs['embed'].to_dict()
        self.assertEqual(len(embed['fields']), 1)
        self.assertIn('이미지 OCR', str(embed))
        self.assertIn('원본으로 확인', embed['footer']['text'])
        self.assertNotIn('secret', str(embed))
        self.assertNotIn('foreign', str(embed))
        self.assertNotIn('@everyone', str(embed))


@unittest.skipUnless(TOOLS, 'Tesseract and Poppler are required for real OCR integration')
class RealOCRTests(fixtures.ContentFixture, unittest.IsolatedAsyncioTestCase):
    async def test_real_isolated_image_and_scanned_pdf_korean_english_search(self):
        self.file(SAMPLE.read_bytes(), 'korean.png')
        self.file(scanned_pdf(mixed=True), 'korean.pdf')
        self.assertEqual(await content.ContentIndexer(self.storage).scan_once(), 2)
        for term in ('서보', 'SERVO', '2400'):
            rows, total, states = content.find_content(1, [10], term)
            self.assertEqual((total, states), (2, {'indexed': 2}), (term, states))
            self.assertEqual({r['location'] for r in rows}, {'이미지 OCR', 'PDF 2쪽 · OCR'})
        self.assertEqual(content.find_content(1, [10], 'native ending')[1], 1)
        self.assertEqual(await content.ContentIndexer(self.storage).scan_once(), 0)

    async def test_bad_image_does_not_block_next_real_image(self):
        self.file(b'not an image', 'broken.png')
        self.file(SAMPLE.read_bytes(), 'good.png')
        self.assertEqual(await content.ContentIndexer(self.storage).scan_once(), 2)
        self.assertEqual(content.find_content(1, [10], '서보')[1], 1)


@unittest.skipUnless(os.name == 'posix', 'POSIX process-group cleanup')
class OCRProcessCleanupTests(fixtures.ContentFixture, unittest.IsolatedAsyncioTestCase):
    async def test_timeout_and_cancellation_stop_grandchildren_and_remove_scratch(self):
        spawn = asyncio.create_subprocess_exec
        for cancel in (False, True):
            marker = self.storage / ('child-' + str(cancel))
            script = ('import sys,json,subprocess,pathlib,time; '
                      'job=json.load(sys.stdin); '
                      'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                      f'pathlib.Path({str(marker)!r}).write_text(json.dumps([child.pid,job["scratch"]])); '
                      'time.sleep(60)')
            async def replacement(*args, **kwargs):
                return await spawn(sys.executable, '-c', script, **kwargs)
            with patch.object(asyncio, 'create_subprocess_exec', side_effect=replacement), \
                    patch.object(content, 'WORKER_TIMEOUT', 1):
                task = asyncio.create_task(content.ContentIndexer(self.storage).extract_one(self.file()))
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(marker.exists())
                pid, scratch = json.loads(marker.read_text())
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual((await task)['status'], 'failed')
            self.assertFalse(Path(scratch).exists())
            status = Path('/proc') / str(pid) / 'stat'
            if status.exists():
                self.assertEqual(status.read_text().split()[2], 'Z', 'OCR descendant still running')
