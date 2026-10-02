import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import test_bot
from bot import database, discord_bot as bot, drive
from bot.history import HistoryCollector


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the temporary DB/storage fixture, with no Discord or Drive network access.
    setUp = test_bot.CollectionTests.setUp
    message = test_bot.CollectionTests.message
    metadata = test_bot.CollectionTests.metadata

    def collector(self, messages, *, batch_size=100, channel_ids=("2",)):
        async def history(**kwargs):
            self.assertTrue(kwargs["oldest_first"])
            eligible = [m for m in messages if m.id > kwargs["after"].id]
            for message in sorted(eligible, key=lambda m: m.id)[:kwargs["limit"]]:
                yield message
        channel = SimpleNamespace(history=history)
        client = MagicMock()
        client.get_channel.return_value = channel
        collector = HistoryCollector(client, channel_ids, bot.process_message, batch_size=batch_size)
        for channel_id in channel_ids:
            database.initialize_channel_cursor(channel_id, 0)
        return collector

    async def test_offline_attachments_persist_and_upload_queue_handles_drive(self):
        collector = self.collector([self.message(1), self.message(2)])
        with patch.object(drive, "upload_file") as upload:
            await collector.scan_once()
            await collector.scan_once()
        upload.assert_not_called()
        self.assertEqual(database.get_channel_cursor("2"), 2)
        self.assertEqual(database.due_uploads(), [1, 2])
        self.assertTrue(all(m["drive_upload_status"] == "pending" for m in self.metadata()))

    async def test_checkpoint_survives_restart_and_batch_boundaries(self):
        messages = [self.message(i) for i in range(1, 6)]
        first = self.collector(messages, batch_size=2)
        self.assertTrue(await first.scan_once())
        self.assertEqual(database.get_channel_cursor("2"), 2)
        database.init_db()
        restarted = self.collector(messages, batch_size=2)
        restarted.initialize()
        self.assertTrue(await restarted.scan_once())
        self.assertFalse(await restarted.scan_once())
        self.assertEqual(database.get_channel_cursor("2"), 5)
        self.assertEqual(len(self.metadata()), 5)

    async def test_live_newer_message_cannot_skip_offline_gap(self):
        messages = [self.message(1), self.message(2), self.message(3)]
        collector = self.collector(messages)
        await bot.process_message(messages[2], upload=False)
        self.assertEqual(database.get_channel_cursor("2"), 0)
        await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("2"), 3)
        self.assertEqual(len(self.metadata()), 3)

    async def test_download_failure_stops_checkpoint_then_recovers(self):
        first, failed, last = self.message(1), self.message(2), self.message(3)
        save = failed.attachments[0].save
        failed.attachments[0].save = AsyncMock(side_effect=OSError("offline"))
        collector = self.collector([first, failed, last])
        with self.assertLogs(bot.logger, level="ERROR"):
            await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("2"), 1)
        self.assertEqual(len(self.metadata()), 1)
        failed.attachments[0].save = save
        await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("2"), 3)
        self.assertEqual(len(self.metadata()), 3)

    async def test_partial_attachment_failure_does_not_duplicate_saved_sibling(self):
        message = self.message(1)
        second = self.message(2, filename="second.txt").attachments[0]
        save = second.save
        second.save = AsyncMock(side_effect=OSError("offline"))
        message.attachments.append(second)
        collector = self.collector([message])
        with self.assertLogs(bot.logger, level="ERROR"):
            await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("2"), 0)
        second.save = save
        await collector.scan_once()
        with closing(database.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
        self.assertEqual(database.get_channel_cursor("2"), 1)

    async def test_permission_failure_does_not_stop_other_channels(self):
        collector = self.collector([self.message(1)], channel_ids=("9", "2"))
        normal = collector.client.get_channel.return_value
        collector.client.get_channel.side_effect = [None, normal]
        collector.client.fetch_channel = AsyncMock(side_effect=PermissionError("denied"))
        with self.assertLogs(bot.logger, level="ERROR"):
            await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("9"), 0)
        self.assertEqual(database.get_channel_cursor("2"), 1)

    async def test_text_and_bot_messages_advance_without_files(self):
        text = self.message(1)
        text.attachments = []
        automated = self.message(2)
        automated.author.bot = True
        collector = self.collector([text, automated])
        await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("2"), 2)
        self.assertEqual(database.due_uploads(), [])

    async def test_initial_window_is_frozen_and_existing_last_message_is_included(self):
        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        collector = HistoryCollector(MagicMock(), ("2",), bot.process_message)
        collector.initialize(now)
        expected = discord.utils.time_snowflake(now - timedelta(hours=24))
        self.assertEqual(database.get_channel_cursor("2"), expected)
        collector.initialize(now + timedelta(days=10))
        self.assertEqual(database.get_channel_cursor("2"), expected)
        with closing(database.get_connection()) as conn, conn:
            conn.execute("DELETE FROM channel_cursors")
        await bot.process_message(self.message(10), upload=False)
        collector.initialize(now)
        self.assertEqual(database.get_channel_cursor("2"), 9)

    async def test_concurrent_live_and_history_share_attachment_deduplication(self):
        message = self.message(1)
        collector = self.collector([message])
        await asyncio.gather(collector.scan_once(), bot.process_message(message, upload=False))
        self.assertEqual(len(self.metadata()), 1)
        self.assertEqual(database.get_channel_cursor("2"), 1)

    async def test_cursor_never_moves_backwards(self):
        database.initialize_channel_cursor("2", 100)
        database.advance_channel_cursor("2", 200)
        database.advance_channel_cursor("2", 150)
        self.assertEqual(database.get_channel_cursor("2"), 200)

    async def test_ready_and_resume_wake_single_task_and_shutdown_cancels_it(self):
        async with bot.CollectorClient(intents=discord.Intents.none()) as client:
            await client.setup_hook()
            history_task = client.history_task
            retry_task = client.retry_task
            monitor_task = client.monitor_task
            with patch.object(bot, "client", client):
                await bot.on_ready()
                client.history_collector.wakeup.clear()
                await bot.on_resumed()
            self.assertTrue(client.history_collector.wakeup.is_set())
            self.assertIs(client.history_task, history_task)
            await asyncio.sleep(0)
        self.assertTrue(history_task.cancelled())
        self.assertTrue(retry_task.cancelled())
        self.assertTrue(monitor_task.cancelled())
