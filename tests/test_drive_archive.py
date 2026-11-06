from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from drive_archive.bot import archive_message


class MemoryDrive:
    def __init__(self):
        self.objects = {}
        self.fail_notes = True

    def month(self, parent, created_at):
        return 'month'

    def folder(self, parent, key, name):
        return {'id': key}

    def lookup(self, parent, key):
        return self.objects.get((parent, key))

    def put(self, parent, key, filename, mime, data):
        if parent == 'messages' and self.fail_notes:
            raise OSError('simulated failure after attachment upload')
        self.objects[parent, key] = {'id': key, 'data': data}
        return key


def message(attachment):
    return SimpleNamespace(id=1, author='작성자', content='본문 한 줄\n둘째 줄',
                           created_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
                           jump_url='https://discord.com/channels/1/2/1', attachments=[attachment])


class DriveArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_attachment_then_saves_original_content_and_link(self):
        drive = MemoryDrive()
        attachment = SimpleNamespace(id=2, filename='사진.png', content_type='image/png',
                                     size=3, read=AsyncMock(return_value=b'png'))
        msg = message(attachment)
        with self.assertRaises(OSError):
            await archive_message(drive, 'channel', msg, 100)
        drive.fail_notes = False
        await archive_message(drive, 'channel', msg, 100)
        await archive_message(drive, 'channel', msg, 100)
        attachment.read.assert_awaited_once()
        self.assertEqual(drive.objects['images', 'attachment-2']['data'], b'png')
        note = drive.objects['messages', 'message-1']['data'].decode('utf-8')
        for value in (msg.content, msg.jump_url, '09:00:00+09:00', 'https://drive.google.com/open?id=attachment-2'):
            self.assertIn(value, note)

    async def test_oversize_keeps_text_and_reports_missing_attachment_without_reading_it(self):
        drive = MemoryDrive()
        drive.fail_notes = False
        attachment = SimpleNamespace(id=2, filename='영상.mp4', content_type='video/mp4',
                                     size=101, read=AsyncMock())
        with self.assertLogs('drive-archive', level='WARNING'):
            await archive_message(drive, 'channel', message(attachment), 100)
        attachment.read.assert_not_awaited()
        self.assertEqual(len(drive.objects), 1)
        self.assertIn('용량 한도 초과', drive.objects['messages', 'message-1']['data'].decode('utf-8'))
