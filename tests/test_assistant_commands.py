from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from test_content import ContentFixture
from test_messages import MessageFixture
from test_file_search import member, channel
from bot import database, content_search, message_archive, command_data as data, file_search, discord_ai


class SuiteFixture(ContentFixture):
    message = MessageFixture.message
    save = MessageFixture.save

    def setUp(self):
        super().setUp()
        message_archive.initialize()

    def doc(self, text='서보 속도 100', filename='서보.txt', **kwargs):
        row = self.file(text.encode(), filename, **kwargs)
        content_search.save_result(row, {'status':'indexed','pages':[[1,text]]})
        return row['id']


class ScopedDataTests(SuiteFixture, unittest.TestCase):
    def test_union_deduplicates_file_name_and_body_before_paging(self):
        fid = self.doc()
        for i in range(6):
            self.save(100+i, content='서보 대화')
        self.doc(channel='20')
        self.doc(guild='2')
        self.save(200, channel=20)
        self.save(201, guild=2)
        first, counts = data.unified(1,[10],'서보')
        second, _ = data.unified(1,[10],'서보',page=2)
        self.assertEqual(counts,{'file':1,'message':6})
        self.assertEqual((len(first),len(second)),(5,2))
        self.assertEqual(len({(r['kind'],r['key']) for r in first+second}),7)
        row = next(r for r in first+second if r['kind']=='file')
        self.assertEqual((row['id'],row['name_hit'],row['page']),(fid,1,1))
        self.assertNotIn('local_path',str(first+second))
        self.assertEqual(data.unified(1,[],'서보'),([],{}))
        self.assertEqual(data.unified(1,[10],"' OR 1=1 --")[1],{})

    def test_file_details_versions_and_duplicates_are_scoped(self):
        ids=[self.doc(text=t,date=f'2026-10-02T0{i}:00:00+00:00') for i,t in enumerate(['A','A','B','A'])]
        hidden = self.doc(text='A',channel='20')
        self.doc(text='A',guild='2')
        self.assertIsNone(data.detail(1,[10],hidden))
        self.assertEqual([data.detail(1,[10],i)['version'] for i in ids],[1,1,2,3])
        last=data.detail(1,[10],ids[-1])
        self.assertEqual((last['version_total'],last['same_content'],last['parts']),(3,3,1))
        self.assertNotIn('sha256',last)
        self.assertEqual({r['id'] for r in data.suggestions(1,[10],'서보')},set(ids))

    def test_status_never_counts_other_channels_or_guilds(self):
        self.doc()
        self.doc(channel='20')
        self.doc(guild='2')
        self.save()
        self.save(200,channel=20)
        summary=data.status(1,[10])
        self.assertEqual(summary['uploads'],{'uploaded':1})
        self.assertEqual(summary['documents'],{'indexed':1})
        self.assertEqual(summary['conversations'],1)

    def test_retrieval_sources_are_scoped_and_current(self):
        public=self.doc()
        hidden=self.doc('서보 secret',channel='20')
        self.doc('서보 foreign',guild='2')
        self.save(content='서보 설정')
        self.save(200,content='서보 secret',channel=20)
        items,partial=data.sources(1,[10],question='서보 설정값은 뭐야?')
        self.assertTrue(partial)
        self.assertEqual({r['kind'] for r in items},{'file','message'})
        self.assertNotIn('secret',str(items))
        self.assertNotIn('foreign',str(items))
        self.assertTrue(data.sources_current(1,[10],items))
        self.assertFalse(data.sources_current(1,[],items))
        self.assertEqual(data.sources(1,[10],file_id=hidden),([],False))
        with closing(database.get_connection()) as db,db:
            db.execute('UPDATE content_pages SET body=? WHERE file_id=?',('changed',public))
        self.assertFalse(data.sources_current(1,[10],items))

    def test_deleted_conversation_cannot_be_used_after_generation(self):
        self.save()
        items,_=data.sources(1,[10],question='서보')
        message_archive.delete_messages(1,10,[100])
        self.assertFalse(data.sources_current(1,[10],items))

    def test_kst_period_boundaries_and_recent_limit(self):
        for mid,stamp in [(1,'2026-10-01T14:59:59+00:00'),(2,'2026-10-01T15:00:00+00:00'),
                          (3,'2026-10-02T14:59:59+00:00'),(4,'2026-10-02T15:00:00+00:00')]:
            msg=self.message(mid)
            msg.created_at=datetime.fromisoformat(stamp)
            message_archive.save_message(msg,'common')
        now=datetime(2026,10,3,12,tzinfo=data.KST)
        yesterday,_=data.sources(1,[10],period='yesterday',now=now)
        today,_=data.sources(1,[10],period='today',now=now)
        self.assertEqual({s['key'] for s in yesterday},{'2','3'})
        self.assertEqual([s['key'] for s in today],['4'])
        for mid in range(100,120):
            self.save(mid)
        items,partial=data.sources(1,[10],period='yesterday',now=now)
        self.assertEqual(len(items),12)
        self.assertTrue(partial)

    def test_source_budget_includes_labels_and_marks_partial(self):
        fid=self.doc('x'*18000)
        items,partial=data.sources(1,[10],file_id=fid)
        self.assertEqual(sum(len(s['text']) for s in items),12000)
        self.assertTrue(partial)
        self.assertTrue(items[0]['text'].startswith('[서보.txt'))
        with closing(database.get_connection()) as db,db:
            db.execute('UPDATE files SET sha256=? WHERE id=?',('newhash',fid))
        self.assertEqual(data.sources(1,[10],file_id=fid)[0],[])


class ExtraCommandTests(SuiteFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=discord.Client(intents=discord.Intents.none())
        self.commands=file_search.SearchCommands(self.client)
        self.suite=self.commands.extra_commands
        self.addAsyncCleanup(self.client.close)
        scoped=patch.object(self.suite,'allowed',new=AsyncMock(return_value=[10]))
        scoped.start()
        self.addCleanup(scoped.stop)

    def interaction(self):
        return SimpleNamespace(guild_id=1,user=member(),response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
                               edit_original_response=AsyncMock())

    async def test_new_commands_schema_and_private_responses(self):
        fid=self.doc()
        for name,args in [('도움말',()),('찾기',('서보',)),('상태',()),('파일정보',(str(fid),))]:
            command=self.commands.tree.get_command(name)
            self.assertTrue(command.guild_only)
            interaction=self.interaction()
            await command.callback(interaction,*args)
            interaction.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
            response=interaction.edit_original_response.call_args.kwargs
            self.assertLessEqual(len(response['embed']),6000)
            self.assertFalse(response['allowed_mentions'].everyone)
        for name in ['파일정보','요약','질문']:
            options=self.commands.tree.get_command(name).to_dict(self.commands.tree)['options']
            self.assertTrue(next(o for o in options if o['name']=='파일')['autocomplete'])

    async def test_autocomplete_and_direct_hidden_id(self):
        public=self.doc()
        hidden=self.doc(filename='private.txt',channel='20')
        choices=await self.suite.autocomplete(self.interaction(),'')
        self.assertEqual([c.value for c in choices],[str(public)])
        interaction=self.interaction()
        await self.commands.tree.get_command('파일정보').callback(interaction,str(hidden))
        self.assertIn('확인할 수 없습니다',interaction.edit_original_response.call_args.kwargs['content'])

    async def test_no_sources_means_no_provider_call(self):
        hidden=self.doc(channel='20')
        with patch.object(discord_ai,'configured') as configured:
            for file in [str(hidden),None]:
                interaction=self.interaction()
                await self.suite.ai(interaction,'ask',file=file,question='서보')
                self.assertIn('AI는 호출하지 않았습니다',interaction.edit_original_response.call_args.kwargs['content'])
            configured.assert_not_called()

    async def test_answer_uses_scoped_sources_and_rechecks_live_permissions(self):
        fid=self.doc()
        self.doc('private secret',channel='20')
        engine=MagicMock()
        engine.run.return_value={'answer':'속도는 100입니다. [S1]','sources':[{'id':'S1'}],'cached':True}
        with patch.object(discord_ai,'configured',return_value=engine), \
             patch.object(file_search,'visible_channels',new=AsyncMock(return_value=[10])):
            interaction=self.interaction()
            await self.suite.ai(interaction,'ask',file=str(fid),question='속도?')
            embed=interaction.edit_original_response.call_args.kwargs['embed']
            self.assertIn('100',embed.description)
            self.assertIn('discord.com/channels/1/10/100',str(embed.to_dict()))
            self.assertNotIn('secret',str(engine.run.call_args))
        with patch.object(discord_ai,'configured',return_value=engine), \
             patch.object(file_search,'visible_channels',new=AsyncMock(return_value=[])):
            interaction=self.interaction()
            await self.suite.ai(interaction,'summary',file=str(fid))
            self.assertIn('권한이 바뀌어',interaction.edit_original_response.call_args.kwargs['content'])

    async def test_source_changed_during_generation_is_not_displayed(self):
        fid=self.doc()
        def generate(*args):
            with closing(database.get_connection()) as db,db:
                db.execute('DELETE FROM content_pages WHERE file_id=?',(fid,))
            return {'answer':'old answer [S1]','sources':[{'id':'S1'}]}
        engine=MagicMock()
        engine.run.side_effect=generate
        with patch.object(discord_ai,'configured',return_value=engine), \
             patch.object(file_search,'visible_channels',new=AsyncMock(return_value=[10])):
            interaction=self.interaction()
            await self.suite.ai(interaction,'summary',file=str(fid))
            self.assertIn('바뀌어',interaction.edit_original_response.call_args.kwargs['content'])

    async def test_disabled_ai_returns_safe_error(self):
        fid=self.doc()
        with patch.object(discord_ai,'configured',side_effect=discord_ai.AIError('AI 기능이 꺼져 있습니다.')):
            interaction=self.interaction()
            await self.suite.ai(interaction,'summary',file=str(fid))
            self.assertEqual(interaction.edit_original_response.call_args.kwargs['content'],'AI 기능이 꺼져 있습니다.')
