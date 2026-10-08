import asyncio
from contextlib import closing
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from bot import database, file_search as search


class CatalogueFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        setting = patch.object(database, "DB_PATH", Path(temporary.name) / "files.db")
        setting.start()
        self.addCleanup(setting.stop)
        database.init_db()

    def add_file(self, filename, *, guild="1", channel="10", category="common",
                 date="2026-10-02T01:00:00+00:00", status="uploaded", drive_id="valid_drive-id"):
        with closing(database.get_connection()) as conn, conn:
            return conn.execute("""INSERT INTO files (discord_message_id, discord_guild_id,
                discord_channel_id, discord_channel, discord_author_id, discord_author,
                uploaded_at, category, original_filename, saved_filename, local_path,
                file_size_bytes, sha256, upload_status, google_drive_file_id)
                VALUES ('100', ?, ?, 'channel', '5', 'author', ?, ?, ?, 'saved',
                        '/private/path', 1, 'sha', ?, ?)""",
                (guild, channel, date, category, filename, status, drive_id)).lastrowid


def member():
    result = MagicMock(spec=discord.Member)
    result.guild = SimpleNamespace(id=1)
    result.id = 5
    return result


def channel(channel_id=10, *, visible=True, history=True, guild=1, private=False, manager=False):
    result = MagicMock(spec=discord.Thread if private else discord.TextChannel)
    result.id = channel_id
    result.guild = SimpleNamespace(id=guild)
    result.permissions_for.return_value = SimpleNamespace(
        view_channel=visible, read_message_history=history, manage_threads=manager)
    if private:
        result.is_private.return_value = True
        result.fetch_member = AsyncMock()
    return result


class SearchDatabaseTests(CatalogueFixture, unittest.TestCase):
    def test_literal_unicode_and_casefold_search(self):
        self.add_file("모터_PLC_100%.pdf")
        self.add_file("회의.txt")
        self.add_file("other.pdf")
        for keyword in ("모터", "plc", "100%", "_PLC_"):
            rows, total = search.find_files(1, [10], keyword)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["original_filename"], "모터_PLC_100%.pdf")
        self.assertEqual(search.find_files(1, [10], "회의")[1], 1)
        self.assertEqual(search.find_files(1, [10], "' OR 1=1 --")[1], 0)

    def test_visibility_and_guild_are_applied_before_count_and_page(self):
        for index in range(8):
            self.add_file(f"secret{index}", channel="20")
            self.add_file(f"other-server{index}", guild="2")
        self.add_file("public")
        rows, total = search.find_files(1, [10])
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["original_filename"], "public")
        self.assertNotIn("local_path", rows[0])
        self.assertEqual(search.find_files(1, [])[1], 0)

    def test_category_recent_order_and_pages(self):
        for index in range(7):
            self.add_file(f"plc-{index}", category="plc", date=f"2026-10-0{index+1}T12:00:00+09:00")
        self.add_file("wrong-category", date="2026-12-01T00:00:00+00:00")
        rows, total = search.find_files(1, [10], category="plc")
        self.assertEqual(total, 7)
        self.assertEqual([r["original_filename"] for r in rows], [f"plc-{i}" for i in range(6, 1, -1)])
        self.assertEqual(len(search.find_files(1, [10], category="plc", page=2)[0]), 2)
        self.assertEqual(search.find_files(1, [10], category="plc", page=3)[0], [])

    def test_chronological_order_handles_timezones_and_ties(self):
        self.add_file("older", date="2026-10-02T09:00:00+09:00")
        self.add_file("newer", date="2026-10-02T01:00:00+00:00")
        self.add_file("same-time-later-id", date="2026-10-02T10:00:00+09:00")
        rows, _ = search.find_files(1, [10])
        self.assertEqual([r["original_filename"] for r in rows], ["same-time-later-id", "newer", "older"])

    def test_candidate_scope_and_invalid_inputs(self):
        self.add_file("motor", channel="10", category="plc")
        self.add_file("motor", channel="20")
        self.add_file("motor", channel="30", guild="2", category="plc")
        self.assertEqual(search.candidate_channels(1, "motor", "plc"), ["10"])
        for kwargs in ({"page": 0}, {"page": 10001}, {"category": "invented"}, {"keyword": "x" * 101}):
            with self.assertRaises(ValueError):
                search.find_files(1, [10], **kwargs)

    def test_result_links_statuses_and_display_limits(self):
        self.add_file("@everyone [bad](https://bad.test) " + "x" * 500)
        self.add_file("pending", status="needs_review", drive_id="unconfirmed")
        self.add_file("invalid-id", drive_id="bad)/view](https://bad.test)")
        rows, total = search.find_files(1, [10])
        embed = search.result_embed(rows, total, "@everyone", page=1).to_dict()
        self.assertNotIn("@everyone", str(embed))
        self.assertTrue(all(len(f["name"]) <= 200 for f in embed["fields"]))
        self.assertIn("업로드 확인 필요", str(embed))
        self.assertNotIn("file/d/unconfirmed", str(embed))
        self.assertNotIn("file/d/bad", str(embed))
        self.assertIn("https://drive.google.com/file/d/valid_drive-id/view", str(embed))
        self.assertIn("2026-10-02 10:00", str(embed))
        self.assertNotIn("/private/path", str(embed))


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_no_history_and_other_guild_excluded(self):
        channels = {10: channel(), 20: channel(20, visible=False),
                    30: channel(30, history=False), 40: channel(40, guild=2)}
        client = SimpleNamespace(get_channel=channels.get, fetch_channel=AsyncMock())
        result = await search.visible_channels(client, member(), 1, ["10", "20", "30", "40"])
        self.assertEqual(result, ["10"])

    async def test_archived_thread_fetched_and_private_membership_checked(self):
        public = channel(10, private=True)
        public.is_private.return_value = False
        private = channel(20, private=True)
        client = SimpleNamespace(get_channel=lambda _: None, fetch_channel=AsyncMock(side_effect=[public, private]))
        self.assertEqual(await search.visible_channels(client, member(), 1, ["10", "20"]), ["10", "20"])
        private.fetch_member.assert_awaited_once_with(5)
        public.fetch_member.assert_not_called()

    async def test_removed_private_member_fails_closed(self):
        private = channel(20, private=True)
        private.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
        client = SimpleNamespace(get_channel=lambda _: private)
        self.assertEqual(await search.visible_channels(client, member(), 1, ["20"]), [])

    async def test_thread_manager_can_read_without_membership(self):
        private = channel(20, private=True, manager=True)
        client = SimpleNamespace(get_channel=lambda _: private)
        self.assertEqual(await search.visible_channels(client, member(), 1, ["20"]), ["20"])
        private.fetch_member.assert_not_called()

    async def test_deleted_channel_and_non_member_fail_closed(self):
        client = SimpleNamespace(get_channel=lambda _: None, fetch_channel=AsyncMock(
            side_effect=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")))
        self.assertEqual(await search.visible_channels(client, member(), 1, ["10"]), [])
        self.assertEqual(await search.visible_channels(client, SimpleNamespace(id=5), 1, ["10"]), [])
        other = member()
        other.guild.id = 2
        self.assertEqual(await search.visible_channels(client, other, 1, ["10"]), [])


class CommandTests(CatalogueFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.commands = search.SearchCommands(self.client)
        self.addAsyncCleanup(self.client.close)

    def interaction(self):
        return SimpleNamespace(guild_id=1, user=member(),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock())

    async def test_schema_has_korean_names_filters_and_guild_only(self):
        commands = self.commands.tree.get_commands()
        self.assertEqual([c.name for c in commands], ["검색", "최근파일", "버전", "내용검색", "대화검색", "도움말", "찾기", "상태", "파일정보", "요약", "질문"])
        payload = commands[0].to_dict(self.commands.tree)
        self.assertTrue(commands[0].guild_only)
        self.assertEqual([o["name"] for o in payload["options"]], ["키워드", "분류", "페이지", "자료범위"])
        self.assertEqual({o["value"] for o in payload["options"][1]["choices"]}, set(search.CATEGORY_FOLDERS))

    async def test_search_response_is_private_filtered_and_deferred(self):
        self.add_file("public-motor")
        self.add_file("hidden-motor", channel="20")
        self.add_file("other-server-motor", guild="2")
        interaction = self.interaction()
        channels = {10: channel(), 20: channel(20, visible=False)}
        with patch.object(self.client, "get_channel", side_effect=channels.get):
            await self.commands.tree.get_command("검색").callback(interaction, "motor")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        embed = interaction.edit_original_response.call_args.kwargs["embed"].to_dict()
        self.assertIn("public-motor", str(embed))
        self.assertNotIn("hidden-motor", str(embed))
        self.assertNotIn("other-server", str(embed))
        self.assertIn("1개", embed["footer"]["text"])

    async def test_recent_empty_result_and_blank_keyword(self):
        interaction = self.interaction()
        await self.commands.tree.get_command("최근파일").callback(interaction)
        self.assertIn("조회할 수 있는 파일이 없습니다", interaction.edit_original_response.call_args.kwargs["embed"].description)
        interaction = self.interaction()
        await self.commands.tree.get_command("검색").callback(interaction, "   ")
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_called()

    async def test_upsert_only_owned_commands_once_per_guild(self):
        self.client._connection.application_id = 42
        with patch.object(self.client, "get_channel", return_value=channel()), \
                patch.object(self.client.http, "upsert_guild_command", new_callable=AsyncMock) as upsert, \
                patch.object(self.client.http, "bulk_upsert_guild_commands", new_callable=AsyncMock) as bulk:
            await asyncio.gather(self.commands.sync([10, 20]), self.commands.sync([10]))
        self.assertEqual(upsert.await_count, 11)
        self.assertEqual({c.kwargs["payload"]["name"] for c in upsert.call_args_list}, {"검색", "최근파일", "버전", "내용검색", "대화검색", "도움말", "찾기", "상태", "파일정보", "요약", "질문"})
        bulk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
