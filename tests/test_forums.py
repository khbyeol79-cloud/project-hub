from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_bot
from bot import database, discord_bot as bot
from bot.history import HistoryCollector
from bot.drive import CATEGORY_FOLDERS


class ForumTests(unittest.IsolatedAsyncioTestCase):
    message = test_bot.CollectionTests.message

    def setUp(self):
        test_bot.CollectionTests.setUp(self)
        setting = patch.object(bot, "FORUM_MAP", {"50": "meeting"})
        setting.start()
        self.addCleanup(setting.stop)

    def thread(self, thread_id, message_ids, parent_id=50, fail=False):
        thread = SimpleNamespace(id=thread_id, parent_id=parent_id, name="post")
        messages = [self.message(i) for i in message_ids]
        for message in messages:
            message.channel = thread
        async def history(**kwargs):
            if fail:
                raise PermissionError("denied")
            for message in [m for m in messages if m.id > kwargs["after"].id][:kwargs["limit"]]:
                yield message
        thread.history = history
        return thread, messages

    def collector(self, active, archived, batch_size=100):
        async def archives(**kwargs):
            self.assertIsNone(kwargs["limit"])
            for thread in archived:
                yield thread
        forum = SimpleNamespace(id=50, guild=SimpleNamespace(active_threads=AsyncMock(return_value=active)), archived_threads=archives)
        client = MagicMock()
        client.get_channel.return_value = forum
        collector = HistoryCollector(client, (), bot.process_message, forums={"50": "meeting"}, batch_size=batch_size)
        collector.initialize()
        return collector

    async def test_active_and_archived_posts_are_collected_once(self):
        first, _ = self.thread(100, [100, 101])
        archived, _ = self.thread(200, [200])
        unrelated, _ = self.thread(300, [300], parent_id=99)
        collector = self.collector([first, unrelated], [first, archived])
        await collector.scan_once()
        await collector.scan_once()
        snapshot = database.health_snapshot({"50": "meeting"})
        self.assertEqual(snapshot["channels"][0]["collected"], 3)
        self.assertEqual(database.get_file(1)["discord_parent_channel_id"], "50")
        self.assertEqual(database.get_file(1)["category"], "meeting")
        self.assertEqual(database.get_channel_cursor("100"), 101)
        self.assertEqual(database.get_channel_cursor("200"), 200)
        self.assertIsNone(database.get_channel_cursor("300"))

    async def test_live_new_post_does_not_skip_earlier_messages(self):
        thread, messages = self.thread(100, [100, 101])
        await bot.process_message(messages[1], upload=False)
        self.assertEqual(database.get_channel_cursor("100"), 0)
        collector = self.collector([thread], [])
        await collector.scan_once()
        self.assertEqual(database.health_snapshot({"50": "meeting"})["channels"][0]["collected"], 2)

    async def test_inaccessible_post_does_not_block_other_posts(self):
        inaccessible, _ = self.thread(100, [100], fail=True)
        readable, _ = self.thread(200, [200])
        collector = self.collector([inaccessible, readable], [])
        with self.assertLogs(bot.logger, level="ERROR"):
            await collector.scan_once()
        self.assertEqual(database.get_channel_cursor("100"), 0)
        self.assertEqual(database.get_channel_cursor("200"), 200)
        self.assertEqual(database.health_snapshot({})["checks"]["history:50"]["failures"], 1)

    async def test_thread_pagination_is_resumable(self):
        thread, _ = self.thread(100, [100, 101, 102])
        collector = self.collector([], [thread], batch_size=2)
        self.assertTrue(await collector.scan_once())
        self.assertEqual(database.get_channel_cursor("100"), 101)
        self.assertFalse(await collector.scan_once())
        self.assertEqual(database.get_channel_cursor("100"), 102)

    async def test_unconfigured_thread_is_ignored(self):
        _, messages = self.thread(100, [100], parent_id=99)
        await bot.process_message(messages[0], upload=False)
        self.assertEqual(database.due_uploads(), [])

    def test_folders_match_current_discord_names(self):
        self.assertEqual(set(CATEGORY_FOLDERS.values()), {"hello⭐", "기구제작🎨", "pc💻", "plc🛠️", "게시물", "추가 프로젝트 - 1팀", "추가 프로젝트 - 2팀", "사진"})
