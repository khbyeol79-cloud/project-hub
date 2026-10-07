import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import test_file_search as fixtures
from bot import database, message_archive as archive, discord_bot as bot
from bot.history import HistoryCollector


class MessageFixture(fixtures.CatalogueFixture):
    def setUp(self):
        super().setUp()
        archive.initialize()

    def message(self, mid=100, content='서보 모터 설정', channel=10, guild=1, parent=None,
                edited=None, reply=None, automated=False, attachments=None):
        return SimpleNamespace(id=mid, content=content, guild=SimpleNamespace(id=guild) if guild else None,
            channel=SimpleNamespace(id=channel, parent_id=parent, guild=SimpleNamespace(id=guild)),
            author=SimpleNamespace(id=5, display_name='작성자 @everyone', bot=automated),
            created_at=datetime(2026, 10, 2, 1, tzinfo=timezone.utc), edited_at=edited,
            type=discord.MessageType.default, reference=reply, attachments=attachments or [])

    def save(self, *args, category='common', **kwargs):
        message = self.message(*args, **kwargs)
        archive.save_message(message, category)
        return message

    def payload(self, mid=100, channel=10, guild=1, **data):
        return SimpleNamespace(message_id=mid, channel_id=channel, guild_id=guild, data=data)


class MessageStorageTests(MessageFixture, unittest.TestCase):
    def test_text_without_attachments_is_persisted_and_not_duplicated(self):
        self.save()
        self.save()
        archive.initialize()
        rows, total = archive.find_messages(1, [10], '서보')
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['attachment_count'], 0)
        self.assertNotIn('content', rows[0])

    def test_newer_edit_wins_over_stale_history_and_old_edit(self):
        original = self.save(content='old text')
        edit_time = original.created_at + timedelta(hours=2)
        self.save(content='new text', edited=edit_time)
        archive.save_message(original, 'common')
        archive.update_content(self.payload(content='stale edit', edited_timestamp=(edit_time-timedelta(hours=1)).isoformat()))
        self.assertEqual(archive.find_messages(1, [10], 'new')[1], 1)
        self.assertEqual(archive.find_messages(1, [10], 'old')[1], 0)
        self.assertEqual(archive.find_messages(1, [10], 'stale')[1], 0)

    def test_partial_event_without_content_keeps_text_and_empty_edit_clears_it(self):
        self.save()
        self.assertTrue(archive.update_content(self.payload(embeds=[])))
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 1)
        self.assertTrue(archive.update_content(self.payload(content='', edited_timestamp='2026-10-02T04:00:00Z')))
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 0)

    def test_delete_erases_text_and_blocks_late_create_history_or_edit(self):
        original = self.save()
        archive.delete_messages(1, 10, [100, 101])
        archive.save_message(original, 'common')
        self.save(101, content='late arrival')
        self.assertTrue(archive.update_content(self.payload(content='late edit', edited_timestamp='2026-10-02T04:00:00Z')))
        with closing(database.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM conversation_tombstones').fetchone()[0], 2)

    def test_search_scope_precedes_counts_and_snippets(self):
        self.save(100, content='servo public')
        self.save(101, content='servo secret', channel=20)
        self.save(102, content='servo foreign', guild=2)
        rows, total = archive.find_messages(1, [10], 'servo')
        self.assertEqual((total, len(rows)), (1, 1))
        self.assertIn('public', str(rows))
        self.assertNotIn('secret', str(rows))
        self.assertNotIn('foreign', str(rows))
        self.assertEqual(archive.find_messages(1, [], 'servo'), ([], 0))

    def test_literal_normalized_korean_and_pagination(self):
        for mid in range(100, 107):
            self.save(mid, content='서보\n모터 100% PLC_제어 Straße', category='plc')
        self.save(200, content='서보 다른 분류')
        for keyword in ('서보 모터', '100%', 'plc_', 'STRASSE'):
            rows, total = archive.find_messages(1, [10], keyword, 'plc', 1)
            self.assertEqual((total, len(rows)), (7, 5))
        self.assertEqual(len(archive.find_messages(1, [10], '서보', 'plc', 2)[0]), 2)
        self.assertEqual(archive.find_messages(1, [10], "' OR 1=1 --")[1], 0)
        self.assertEqual(archive.find_messages(1, [10], '서보', 'plc', 3)[0], [])

    def test_reply_and_file_linkage_stay_in_same_guild_channel(self):
        self.save(reply=SimpleNamespace(message_id=90, channel_id=10, guild_id=1))
        self.add_file('linked.txt')
        self.add_file('private.txt', channel='20')
        self.add_file('foreign.txt', guild='2')
        row = archive.find_messages(1, [10], '서보')[0][0]
        self.assertEqual((row['reply_message_id'], row['stored_files']), ('90', 1))
        self.save(101, reply=SimpleNamespace(message_id=91, channel_id=20, guild_id=1))
        self.assertIsNone(archive.find_messages(1, [10], '서보')[0][0]['reply_message_id'])

    def test_delete_preserves_independently_archived_files(self):
        self.save()
        self.add_file('archive.txt')
        archive.delete_messages(1, 10, [100])
        with closing(database.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM files').fetchone()[0], 1)

    def test_cursor_initial_window_frozen_and_separate_from_file_cursor(self):
        fallback = discord.utils.time_snowflake(datetime.now(timezone.utc) - timedelta(hours=24))
        database.initialize_channel_cursor(10, 123)
        archive.initialize_channel_cursor(10, fallback)
        archive.initialize_channel_cursor(10, fallback + 100)
        self.assertEqual(archive.get_channel_cursor(10), fallback)
        archive.advance_channel_cursor(10, fallback + 200)
        archive.advance_channel_cursor(10, fallback + 100)
        self.assertEqual(archive.get_channel_cursor(10), fallback + 200)
        self.assertEqual(database.get_channel_cursor(10), 123)
        archive.initialize_channel_cursor(20, 0)
        self.assertGreaterEqual(archive.get_channel_cursor(20), fallback)

    def test_invalid_queries_and_content_bound(self):
        for text, page in [('', 1), (' ', 1), ('a'*101, 1), ('a', 0), ('a', 10001)]:
            with self.assertRaises(ValueError):
                archive.find_messages(1, [10], text, page=page)
        self.save(content='a' * (archive.MAX_CONTENT + 100))
        with closing(database.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT length(content) FROM conversation_messages').fetchone()[0], archive.MAX_CONTENT)


class MessageEventsTests(MessageFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MagicMock()
        self.client.get_channel.return_value = self.message().channel
        self.worker = archive.MessageArchive(self.client, {'10': 'common'}, {'50': 'meeting'})

    async def test_collects_configured_human_text_and_forum_only(self):
        self.assertTrue(await self.worker.save(self.message(100)))
        self.assertTrue(await self.worker.save(self.message(101, channel=51, parent=50)))
        for message in [self.message(102, guild=None), self.message(103, channel=20), self.message(104, automated=True)]:
            self.assertTrue(await self.worker.save(message))
        system = self.message(105)
        system.type = discord.MessageType.pins_add
        self.assertTrue(await self.worker.save(system))
        self.assertEqual(archive.find_messages(1, [10, 51], '서보')[1], 2)
        self.assertEqual(archive.find_messages(1, [51], '서보', 'meeting')[1], 1)

    async def test_edit_known_message_uses_complete_text_without_network(self):
        self.save()
        await self.worker.edited(self.payload(content='edited servo', edited_timestamp='2026-10-02T04:00:00Z'))
        self.assertEqual(archive.find_messages(1, [10], 'edited')[1], 1)
        self.client.fetch_channel.assert_not_called()

    async def test_unknown_edit_fetches_full_author_and_current_message(self):
        channel = self.message().channel
        channel.fetch_message = AsyncMock(return_value=self.message(content='latest snapshot', edited=datetime.now(timezone.utc)))
        self.client.get_channel.return_value = channel
        await self.worker.edited(self.payload(content='partial old', edited_timestamp='2026-10-02T04:00:00Z'))
        channel.fetch_message.assert_awaited_once_with(100)
        self.assertEqual(archive.find_messages(1, [10], 'latest')[1], 1)
        self.assertEqual(archive.find_messages(1, [10], 'partial')[1], 0)

    async def test_raw_single_and_bulk_delete_remove_uncached_messages(self):
        self.save(100)
        self.save(101)
        self.save(102)
        await self.worker.deleted(self.payload())
        payload = self.payload()
        payload.message_ids = {101, 102}
        await self.worker.deleted(payload)
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 0)

    async def test_events_outside_configured_guild_channel_do_nothing(self):
        self.save()
        for payload in (self.payload(guild=None), self.payload(guild=2)):
            await self.worker.deleted(payload)
        self.client.get_channel.return_value = self.message(channel=20).channel
        await self.worker.deleted(self.payload(channel=20))
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 1)

    async def test_live_archive_failure_does_not_block_file_collector(self):
        worker = SimpleNamespace(save=AsyncMock(return_value=False))
        with patch.object(bot, 'client', SimpleNamespace(message_archive=worker, health_monitor=None)), \
                patch.object(bot, 'process_message', AsyncMock()) as files:
            await bot.on_message(self.message())
        files.assert_awaited_once()

    async def test_live_and_history_concurrency_does_not_duplicate_or_resurrect(self):
        message = self.message()
        await asyncio.gather(self.worker.save(message), self.worker.save(message))
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 1)
        await asyncio.gather(self.worker.save(message), self.worker.deleted(self.payload()))
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 0)

    async def test_history_resumes_own_cursor_and_does_not_skip_live_gap(self):
        archive.initialize_channel_cursor(10, 1)
        database.initialize_channel_cursor(10, 777)
        messages = [self.message(mid) for mid in (100, 101, 102)]
        async def history(**kwargs):
            for message in [m for m in messages if m.id > kwargs['after'].id][:kwargs['limit']]:
                yield message
        channel = messages[0].channel
        channel.history = history
        self.client.get_channel.return_value = channel
        collector = HistoryCollector(self.client, ['10'], self.worker.save, cursor_store=archive,
                                     health_prefix='messages:', batch_size=2)
        await self.worker.save(messages[-1])
        self.assertEqual(archive.get_channel_cursor(10), 1)
        self.assertTrue(await collector.scan_once())
        self.assertEqual(archive.get_channel_cursor(10), 101)
        collector.initialize()
        self.assertFalse(await collector.scan_once())
        self.assertEqual(archive.get_channel_cursor(10), 102)
        self.assertEqual(database.get_channel_cursor(10), 777)
        self.assertEqual(archive.find_messages(1, [10], '서보')[1], 3)

    async def test_failed_save_does_not_advance_message_cursor(self):
        archive.initialize_channel_cursor(10, 1)
        async def history(**kwargs):
            yield self.message()
        self.client.get_channel.return_value = SimpleNamespace(history=history)
        collector = HistoryCollector(self.client, ['10'], AsyncMock(return_value=False), cursor_store=archive,
                                     health_prefix='messages:')
        with self.assertLogs('project-hub', level='ERROR'):
            await collector.scan_once()
        self.assertEqual(archive.get_channel_cursor(10), 1)


class MessageCommandTests(MessageFixture, unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CommandTests.asyncSetUp
    interaction = fixtures.CommandTests.interaction

    async def test_private_response_permissions_snippet_reply_and_attachment_linkage(self):
        self.save(content='서보 @everyone [bad](https://bad.test)',
                  reply=SimpleNamespace(message_id=90, channel_id=10, guild_id=1))
        self.add_file('linked.txt')
        self.save(101, content='서보 secret', channel=20)
        self.save(102, content='서보 foreign', guild=2)
        channels = {10: fixtures.channel(), 20: fixtures.channel(20, visible=False)}
        interaction = self.interaction()
        with patch.object(self.client, 'get_channel', side_effect=channels.get):
            await self.commands.tree.get_command('대화검색').callback(interaction, '서보')
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        embed = interaction.edit_original_response.call_args.kwargs['embed'].to_dict()
        self.assertEqual(embed['title'], '대화 검색')
        self.assertEqual(len(embed['fields']), 1)
        self.assertIn('1개 대화', embed['footer']['text'])
        self.assertIn('함께 저장된 파일 1개', str(embed))
        self.assertIn('https://discord.com/channels/1/10/90', str(embed))
        self.assertIn('2026-10-02 10:00', str(embed))
        self.assertNotIn('@everyone', str(embed))
        self.assertNotIn('secret', str(embed))
        self.assertNotIn('foreign', str(embed))
        self.assertLessEqual(len(embed['fields'][0]['value']), 1024)

    async def test_schema_empty_query_and_dm_are_rejected_privately(self):
        command = self.commands.tree.get_command('대화검색')
        self.assertTrue(command.guild_only)
        self.assertEqual([p['name'] for p in command.to_dict(self.commands.tree)['options']], ['키워드', '분류', '페이지', '자료범위'])
        for keyword, guild in [(' ', 1), ('servo', None)]:
            interaction = self.interaction()
            interaction.guild_id = guild
            await command.callback(interaction, keyword)
            interaction.response.defer.assert_not_called()
            self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
