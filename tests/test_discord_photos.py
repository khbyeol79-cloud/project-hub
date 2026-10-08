import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from bot import database, discord_bot as bot, drive, photo_channels, projects
from bot.history import HistoryCollector
import test_bot
import test_drive
from test_drive import http_error


class PhotoChannelTests(unittest.TestCase):
    def channel(self, cid, name, guild=projects.GUILD_ID):
        return SimpleNamespace(id=cid,name=name,guild=SimpleNamespace(id=int(guild)))

    def test_exact_photo_channel_only_in_the_verified_guild(self):
        for name in ['사진공유','사진-공유📷','사진_공유']:
            with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_CHANNEL_ID':''}):
                self.assertEqual(photo_channels.discover([self.channel(7,name),self.channel(8,'사진공유',guild='999')]),{'7':'photos'})
        self.assertEqual(photo_channels.discover([self.channel(7,'사진공유-비공개')]),{})

    def test_duplicate_names_need_an_explicit_verified_id(self):
        channels=[self.channel(7,'사진공유'),self.channel(8,'사진공유📷')]
        with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_CHANNEL_ID':''}):
            self.assertEqual(photo_channels.discover(channels),{})
        with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_CHANNEL_ID':'8'}):
            self.assertEqual(photo_channels.discover(channels),{'8':'photos'})
            self.assertEqual(photo_channels.discover([self.channel(8,'사진공유',guild='999')]),{})
        with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_CHANNEL_ID':'../bad'}):
            with self.assertRaises(ValueError):photo_channels.discover(channels)

    def test_image_filter_keeps_photo_formats_and_skips_other_files(self):
        for filename,mime,expected in [('IMG.JPG','image/jpeg',True),('a.heic',None,True),
            ('a.PNG','application/octet-stream',True),('a.png','text/html',False),
            ('a.py','image/png',False),('a.mp4','video/mp4',False),('a.svg','image/svg+xml',False)]:
            self.assertEqual(photo_channels.is_photo(SimpleNamespace(filename=filename,content_type=mime)),expected)


class PhotoHistoryTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_bot.CollectionTests.setUp
    message=test_bot.CollectionTests.message
    metadata=test_bot.CollectionTests.metadata

    async def test_all_old_photos_resume_without_duplicate_live_message_or_nonphotos(self):
        messages=[self.message(1,filename='old.JPG'),self.message(2,filename='notes.txt'),self.message(3,filename='latest.png')]
        async def history(**kwargs):
            self.assertTrue(kwargs['oldest_first'])
            for m in [m for m in messages if m.id>kwargs['after'].id][:kwargs['limit']]:yield m
        client=Mock();client.get_channel.return_value=SimpleNamespace(history=history)
        with patch.object(bot,'CHANNEL_MAP',{'2':'photos'}):
            # A live new photo may arrive before the initial history boundary is created.
            await bot.process_message(messages[2],upload=False)
            collector=HistoryCollector(client,['2'],bot.process_message,batch_size=2,full_history_channels=['2'])
            collector.initialize(datetime.now(timezone.utc))
            self.assertEqual(database.get_channel_cursor('2'),0)
            self.assertTrue(await collector.scan_once())
            self.assertEqual(database.get_channel_cursor('2'),2)
            restarted=HistoryCollector(client,['2'],bot.process_message,batch_size=2,full_history_channels=['2'])
            restarted.initialize(datetime.now(timezone.utc)+timedelta(days=10))
            self.assertEqual(database.get_channel_cursor('2'),2)
            self.assertFalse(await restarted.scan_once())
            self.assertEqual(database.get_channel_cursor('2'),3)
            self.assertEqual(len(self.metadata()),2)
            self.assertEqual({x['category'] for x in self.metadata()},{'photos'})
            self.assertEqual(len(database.due_uploads()),2)

    async def test_upload_uses_photo_auth_and_queue_preserves_failed_local_photos(self):
        photo_service=Mock()
        with patch.object(bot,'CHANNEL_MAP',{'2':'photos'}), patch.object(drive,'get_photo_upload_service',return_value=photo_service) as auth, \
             patch.object(drive,'upload_file',side_effect=drive.AuthorizationRequired('Photo permission required')) as upload:
            with self.assertLogs('project-hub',level='ERROR'):
                await bot.process_message(self.message(1,filename='photo.jpg'))
            self.assertEqual(upload.call_args.args[0],photo_service)
            auth.assert_called_once()
            row=database.get_file(1)
            self.assertEqual(row['upload_status'],'failed')
            self.assertTrue(Path(row['local_path']).is_file())
            reserved=row['planned_drive_id']
            upload.side_effect=None;upload.return_value={'file_id':reserved,'url':'https://example.test/photo'}
            with closing(database.get_connection()) as db,db:
                db.execute('UPDATE files SET next_retry_at=0 WHERE id=1')
            bot.upload_worker.upload(1)
            self.assertEqual(database.get_file(1)['upload_status'],'uploaded')
            self.assertEqual(upload.call_args.kwargs['file_id'],reserved)
            await bot.process_message(self.message(1,filename='photo.jpg'))
            self.assertEqual(upload.call_count,2)
            self.assertEqual(len(self.metadata()),1)

    async def test_discovery_initializes_photo_history_without_changing_team_scopes(self):
        from bot import message_archive
        message_archive.initialize()
        guild=SimpleNamespace(id=int(projects.GUILD_ID))
        guild.text_channels=[SimpleNamespace(id=2,name='사진공유📷',guild=guild),
                             SimpleNamespace(id=300,name='로봇-1b',guild=guild)]
        client=SimpleNamespace(get_guild=lambda _:guild,history_collector=None,message_collector=None,
                               message_archive=None,health_monitor=None)
        client.history_collector=HistoryCollector(client,['10'],bot.process_message)
        client.message_collector=HistoryCollector(client,['10'],None,cursor_store=message_archive)
        with patch.object(bot,'client',client), patch.object(bot,'CHANNEL_MAP',{'10':'common'}), \
             patch.object(bot,'FORUM_MAP',{}),patch.dict(os.environ,{'PROJECT_HUB_PHOTO_CHANNEL_ID':''}):
            await bot.connect_project_channels()
            self.assertEqual(bot.CHANNEL_MAP,{'10':'common','2':'photos','300':'robot_1b'})
            self.assertEqual(client.history_collector.full_history_channels,{'2'})
            self.assertEqual(database.get_channel_cursor('2'),0)
            self.assertGreater(message_archive.get_channel_cursor('2'),0)


class PhotoDriveTests(unittest.TestCase):
    setUp=test_drive.DriveRetryTests.setUp

    def test_photo_upload_uses_exact_existing_folder_without_creating_folders(self):
        self.service.files().get().execute.side_effect=[http_error(404),
            {'mimeType':'application/vnd.google-apps.folder','capabilities':{'canAddChildren':True}}]
        self.service.files().create().execute.return_value={'id':'stable-id','name':'photo.jpg','webViewLink':'https://example.test/photo'}
        with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_FOLDER_ID':'existing_photos'}), \
             patch.object(drive,'prepare_drive_folders') as prepare:
            drive.upload_file(self.service,self.path,'photos','photo.jpg',file_id='stable-id',expected_sha256=self.digest)
            prepare.assert_not_called()
        call=self.service.files().create.call_args.kwargs
        self.assertEqual(call['body']['parents'],['existing_photos'])
        self.assertEqual(call['body']['id'],'stable-id')
        self.assertTrue(call['supportsAllDrives'])

    def test_inaccessible_readonly_or_invalid_folder_never_creates_another(self):
        for result in [http_error(404),{'mimeType':'application/vnd.google-apps.folder','capabilities':{'canAddChildren':False}},
                       {'mimeType':'image/jpeg','capabilities':{'canAddChildren':True}}]:
            self.service.files().get().execute.side_effect=result if isinstance(result,Exception) else None
            self.service.files().get().execute.return_value=result
            with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_FOLDER_ID':'existing_photos'}):
                with self.assertRaises((drive.AuthorizationRequired,ValueError)):drive.photo_upload_folder(self.service)
            self.service.files().create.assert_not_called()

    def test_separate_photo_token_rejects_readonly_before_upload_without_browser(self):
        with patch.dict(os.environ,{'PROJECT_HUB_PHOTO_TOKEN':str(self.path)}), \
             patch.object(drive.Credentials,'from_authorized_user_file') as load, \
             patch.object(drive.InstalledAppFlow,'from_client_secrets_file') as browser:
            load.return_value.has_scopes.return_value=False
            with self.assertRaises(drive.AuthorizationRequired):drive.get_photo_upload_service()
            browser.assert_not_called()
            load.return_value.has_scopes.assert_called_once_with([drive.PHOTO_READ_SCOPE,drive.PHOTO_UPLOAD_SCOPE])
