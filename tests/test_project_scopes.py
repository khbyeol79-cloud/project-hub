"""Cross-project regressions using real SQLite files, messages and web routes."""
from contextlib import closing
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bot import database, projects, file_search, content_search, message_archive, command_data
from organizer.web import create_app
from test_assistant_commands import SuiteFixture


class ProjectScopesTests(SuiteFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ids = {}
        self.channels = {'main': '10', '1a': next(iter(projects.TEAM_CHANNEL_IDS)), '1b': '300'}
        self.guild = projects.GUILD_ID
        now = datetime.now(timezone.utc)
        for index, scope in enumerate(projects.LABELS):
            category = projects.TEAM_CATEGORIES.get(scope, 'common')
            text = '공통토큰 ' + scope + '-only'
            cid = self.channels[scope]
            fid = self.doc(text, '같은파일.txt', guild=self.guild, channel=cid,
                           category=category, date=now.isoformat())
            self.ids[scope] = fid
            msg = self.message(100 + index, text, channel=int(cid), guild=int(self.guild))
            msg.created_at = now
            message_archive.save_message(msg, category)
            with closing(database.get_connection()) as db, db:
                db.execute('UPDATE files SET discord_message_id=? WHERE id=?', (str(msg.id), fid))
                db.execute('UPDATE files SET discord_channel=? WHERE id=?', (scope, fid))
        self.ai = Mock()
        self.ai.settings = SimpleNamespace(enabled=True, max_chars=12000,
            state=database.DB_PATH.parent, daily_limit=20, monthly_limit=300)
        self.ai.run.return_value = {'answer': 'scoped answer', 'sources': [], 'partial': True, 'cached': False}
        self.app = create_app({'TESTING': True, 'DB_PATH': database.DB_PATH,
            'STORAGE': self.storage, 'PUBLIC_ACCESS': True, 'PUBLIC_ORIGIN': 'https://example.test',
            'MESSAGE_CHANNELS': ['10']}, self.ai)
        self.client = self.app.test_client()
        self.headers = {'Origin': 'https://example.test', 'X-Requested-With': 'ProjectHub'}

    def query(self, scope):
        return '?project=main' if scope == 'main' else '?project=additional&team=' + scope

    def test_lists_search_counts_filters_and_messages_never_mix(self):
        for index, scope in enumerate(projects.LABELS):
            q = self.query(scope)
            response = self.client.get('/library/api/files' + q + '&q=공통토큰').json
            self.assertEqual(response['total'], 1)
            self.assertEqual([f['id'] for f in response['files']], [self.ids[scope]])
            self.assertEqual([c['id'] for c in response['channels']], [self.channels[scope]])
            self.assertEqual(response['extensions'], ['.txt'])
            chats = self.client.get('/library/api/messages' + q + '&q=공통토큰').json
            self.assertEqual([m['message_id'] for m in chats['messages']], [str(100 + index)])
            self.assertEqual([c['id'] for c in chats['channels']], [self.channels[scope]])
            self.assertEqual(chats['messages'][0]['files'][0]['id'], self.ids[scope])
            other = '1b' if scope != '1b' else 'main'
            self.assertEqual(self.client.get('/library/api/files' + q + '&channel=' + self.channels[other]).json['total'], 0)
            self.assertEqual(self.client.get('/library/api/messages' + q + '&channel=' + self.channels[other]).json['messages'], [])
            self.assertEqual(self.client.get('/library/api/files' + q + '&view=latest&days=7').json['total'], 1)

    def test_all_detail_preview_download_and_file_ai_routes_enforce_scope(self):
        for scope, fid in self.ids.items():
            wrong = '1b' if scope != '1b' else 'main'
            for route in [f'/library/api/files/{fid}', f'/library/api/files/{fid}/text-preview',
                          f'/library/files/{fid}/download', f'/library/files/{fid}/preview',
                          f'/library/files/{fid}/html-preview', f'/library/files/{fid}/docx-preview']:
                self.assertEqual(self.client.get(route + self.query(wrong)).status_code, 404, route)
            self.assertEqual(self.client.post(f'/library/api/files/{fid}/ai' + self.query(wrong),
                headers=self.headers, json={'task': 'summary'}).status_code, 404)
            self.assertEqual(self.client.get(f'/library/api/files/{fid}' + self.query(scope)).status_code, 200)
            with self.client.get(f'/library/files/{fid}/download' + self.query(scope)) as downloaded:
                self.assertEqual(downloaded.status_code, 200)
        self.ai.run.assert_not_called()
        self.assertEqual(self.client.get('/library/api/messages/101?project=main').status_code, 404)
        self.assertEqual(self.client.get('/library/api/messages/101' + self.query('1a')).status_code, 200)

    def test_integrated_ai_and_weekly_saved_results_are_separate(self):
        for scope in projects.LABELS:
            q = self.query(scope)
            r = self.client.post('/library/api/ai' + q, headers=self.headers,
                                 json={'task': 'ask', 'question': '공통토큰'})
            self.assertEqual(r.status_code, 200, r.json)
            sources = self.ai.run.call_args.args[1]
            self.assertEqual({s['file_id'] for s in sources if 'file_id' in s}, {self.ids[scope]})
            self.assertTrue(all(scope + '-only' in s['text'] for s in sources))
            self.assertIsNone(self.client.get('/library/api/weekly' + q).json['saved'])
            self.ai.run.return_value['answer'] = scope + ' summary'
            r = self.client.post('/library/api/weekly' + q, headers=self.headers, json={})
            self.assertEqual(r.status_code, 200, r.json)
            self.assertTrue(all(scope + '-only' in s['text'] for s in self.ai.run.call_args.args[1]))
        calls = self.ai.run.call_count
        for scope in projects.LABELS:
            data = self.client.get('/library/api/weekly' + self.query(scope)).json
            self.assertEqual(data['saved']['answer'], scope + ' summary')
            self.assertEqual([f['id'] for f in data['files']], [self.ids[scope]])
        self.assertEqual(self.ai.run.call_count, calls)

    def test_duplicate_relationships_and_collection_classification_are_separate(self):
        with closing(database.get_connection()) as db, db:
            db.execute("UPDATE files SET sha256='equal-hash'")
        for scope, fid in self.ids.items():
            category = projects.TEAM_CATEGORIES.get(scope, 'common')
            found = database.find_existing_file('같은파일.txt', 'equal-hash', category, self.guild)
            self.assertEqual(found['duplicate_of'], fid)
            detail = self.client.get(f'/library/api/files/{fid}' + self.query(scope)).json
            self.assertEqual(detail['identical_count'], 0)
            self.assertEqual(detail['related'], [])
            self.assertEqual(detail['latest_id'], fid)
        self.assertEqual(database.find_existing_file('같은파일.txt', 'different', 'robot_1a', self.guild)['type'], 'new_version')
        self.assertEqual(database.find_existing_file('같은파일.txt', 'equal-hash', 'robot_1b', '999')['type'], 'new_file')

    def test_bot_queries_apply_scope_before_counts_pagination_and_ai(self):
        channels = list(self.channels.values())
        for scope, fid in self.ids.items():
            for query in (file_search.find_files, file_search.find_versions):
                rows, total = query(self.guild, channels, '같은파일', scope=scope)
                self.assertEqual((total, [r['id'] for r in rows]), (1, [fid]))
            rows, total, _ = content_search.find_content(self.guild, channels, '공통토큰', scope=scope)
            self.assertEqual((total, [r['id'] for r in rows]), (1, [fid]))
            rows, total = message_archive.find_messages(self.guild, channels, '공통토큰', scope=scope)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]['channel_id'], self.channels[scope])
            rows, counts = command_data.unified(self.guild, channels, '공통토큰', scope=scope)
            self.assertEqual(counts, {'file': 1, 'message': 1})
            self.assertEqual([r['id'] for r in rows if r['kind'] == 'file'], [fid])
            self.assertEqual([r['id'] for r in command_data.suggestions(self.guild, channels, '', scope=scope)], [fid])
            self.assertEqual(command_data.status(self.guild, channels, scope=scope)['conversations'], 1)
            sources, _ = command_data.sources(self.guild, channels, question='공통토큰', scope=scope)
            self.assertTrue(all(scope + '-only' in s['text'] for s in sources))
            self.assertTrue(command_data.sources_current(self.guild, channels, sources, scope=scope))
            wrong = '1b' if scope != '1b' else 'main'
            self.assertFalse(command_data.sources_current(self.guild, channels, sources, scope=wrong))
            self.assertIsNone(command_data.detail(self.guild, channels, fid, scope=wrong))

    def test_invalid_or_missing_team_does_not_fall_back_to_combined_data(self):
        for q in ['?project=additional', '?project=additional&team=all', '?project=unknown', '?project=main&team=1a']:
            self.assertEqual(self.client.get('/library/api/files' + q).status_code, 400)
            self.assertEqual(self.client.get('/library/api/messages' + q).status_code, 400)
        self.assertEqual(self.client.get('/library/api/files').json['total'], 1)

    def test_processing_counts_and_source_database_are_unchanged_by_reads(self):
        before = database.DB_PATH.read_bytes()
        for scope in projects.LABELS:
            data = self.client.get('/library/api/processing' + self.query(scope)).json
            self.assertEqual(data['total_files'], 1)
            self.assertEqual(data['content'], {'ready': 1})
            self.assertEqual(data['drive'], {'uploaded': 1})
        self.assertEqual(database.DB_PATH.read_bytes(), before)

    def test_exact_channel_discovery_rejects_ambiguity_and_other_servers(self):
        def channel(cid, name, guild=self.guild):
            return SimpleNamespace(id=int(cid), name=name, guild=SimpleNamespace(id=int(guild)))
        a = channel(self.channels['1a'], '로봇-1a🥁')
        b = channel('300', '로봇-1b🎸')
        self.assertEqual(projects.discover([a, b]), {str(a.id): 'robot_1a', '300': 'robot_1b'})
        self.assertNotIn('300', projects.discover([b, channel('301', '로봇-1b🥁')]))
        self.assertEqual(projects.discover([channel('302', '로봇-1b', guild='999')]), {})
        self.assertEqual(projects.discover([channel('303', '로봇-1b-other')]), {})
        self.assertEqual(projects.invocation_scope(SimpleNamespace(channel=b)), '1b')
        b.name = 'changed-name'
        self.assertEqual(projects.invocation_scope(SimpleNamespace(channel=b)), '1b')
        self.assertEqual(projects.invocation_scope(SimpleNamespace(channel=b), 'main'), 'main')

    def test_bot_command_registration_keeps_eleven_commands_and_scope_choices(self):
        import discord
        commands = file_search.SearchCommands(discord.Client(intents=discord.Intents.none())).tree.get_commands()
        self.assertEqual(len(commands), 11)
        for command in commands:
            if command.name == '도움말':
                continue
            parameter = next(p for p in command.parameters if p.name == 'scope')
            self.assertEqual(parameter.display_name, '자료범위')
            self.assertEqual({c.value for c in parameter.choices}, set(projects.LABELS))


class TeamCollectionTests(SuiteFixture, unittest.IsolatedAsyncioTestCase):
    async def test_autocomplete_obeys_the_actual_korean_discord_option_name(self):
        import discord
        from test_file_search import member, channel
        a = next(iter(projects.TEAM_CHANNEL_IDS))
        self.doc('main', guild=projects.GUILD_ID, channel='10')
        self.doc('a', guild=projects.GUILD_ID, channel=a, category='robot_1a')
        bid = self.doc('b', guild=projects.GUILD_ID, channel='300', category='robot_1b')
        client = discord.Client(intents=discord.Intents.none())
        lookup = patch.object(client, "get_channel", side_effect=lambda cid: channel(cid, guild=int(projects.GUILD_ID)))
        lookup.start()
        self.addCleanup(lookup.stop)
        commands = file_search.SearchCommands(client)
        user = member()
        user.guild.id = int(projects.GUILD_ID)
        interaction = SimpleNamespace(guild_id=int(projects.GUILD_ID), user=user, channel=None,
                                      namespace=SimpleNamespace(**{'자료범위': '1b'}))
        choices = await commands.extra_commands.autocomplete(interaction, '')
        self.assertEqual([c.value for c in choices], [str(bid)])

    async def test_collection_separates_originals_duplicates_and_thread_messages(self):
        from bot import discord_bot
        a = next(iter(projects.TEAM_CHANNEL_IDS))
        mappings = {'10': 'common', a: 'robot_1a', '300': 'robot_1b'}
        async def save(path):
            path.write_bytes(b'same original')
        with patch.object(discord_bot, 'CHANNEL_MAP', mappings), patch.object(discord_bot, 'FORUM_MAP', {}), \
                patch.object(discord_bot, 'STORAGE_DIR', self.storage), patch.object(discord_bot, 'collection_lock', asyncio.Lock()):
            for mid, cid in enumerate(['10', a, '300', a], 1):
                attachment = SimpleNamespace(id=mid, filename='same.txt', save=save)
                message = self.message(mid, channel=int(cid), guild=int(projects.GUILD_ID), attachments=[attachment])
                self.assertTrue(await discord_bot.process_message(message, upload=False))
            thread = self.message(5, channel=400, parent=int(a), guild=int(projects.GUILD_ID),
                                  attachments=[SimpleNamespace(id=5, filename='same.txt', save=save)])
            self.assertTrue(await discord_bot.process_message(thread, upload=False))
            archive = message_archive.MessageArchive(None, mappings, {})
            self.assertTrue(await archive.save(thread))
        with closing(database.get_connection()) as db:
            rows = list(db.execute('SELECT category,duplicate_type,duplicate_of,local_path FROM files ORDER BY id'))
            self.assertEqual([r[0] for r in rows], ['common', 'robot_1a', 'robot_1b', 'robot_1a', 'robot_1a'])
            self.assertEqual([r[1] for r in rows], ['new_file', 'new_file', 'new_file', 'exact_duplicate', 'exact_duplicate'])
            self.assertEqual([r[2] for r in rows], [None, None, None, 2, 2])
            self.assertTrue(all(category in path for category, _, _, path in rows))
            self.assertEqual(db.execute('SELECT category,parent_id FROM conversation_messages').fetchone(), ('robot_1a', a))

    async def test_discovered_channels_update_all_collectors_and_history_cursors(self):
        from bot import discord_bot
        from bot.history import HistoryCollector
        guild = SimpleNamespace(id=int(projects.GUILD_ID))
        a = next(iter(projects.TEAM_CHANNEL_IDS))
        guild.text_channels = [SimpleNamespace(id=int(a), name='로봇-1a🥁', guild=guild),
                               SimpleNamespace(id=300, name='로봇-1b🎸', guild=guild)]
        client = SimpleNamespace(get_guild=lambda _: guild)
        client.history_collector = HistoryCollector(client, ['10'], None)
        client.message_collector = HistoryCollector(client, ['10'], None, cursor_store=message_archive)
        client.message_archive = message_archive.MessageArchive(client, {'10': 'common'}, {})
        client.health_monitor = SimpleNamespace(channel_map={'10': 'common'})
        with patch.object(discord_bot, 'client', client), patch.object(discord_bot, 'CHANNEL_MAP', {'10': 'common'}), \
                patch.object(discord_bot, 'FORUM_MAP', {}):
            await discord_bot.connect_project_channels()
            self.assertEqual(set(discord_bot.CHANNEL_MAP), {'10', a, '300'})
            for collector in (client.history_collector, client.message_collector):
                self.assertEqual(set(collector.channel_ids), {'10', a, '300'})
                self.assertEqual(set(collector.forum_ids), {a, '300'})
                self.assertIsNotNone(collector.cursors.get_channel_cursor('300'))
                self.assertTrue(collector.wakeup.is_set())
            self.assertEqual(client.message_archive.channels['300'], 'robot_1b')
            self.assertEqual(client.health_monitor.channel_map['300'], 'robot_1b')
