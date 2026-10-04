from contextlib import closing
from datetime import datetime, timezone, timedelta
import sqlite3
import unittest
import test_library
from organizer.conversations import Conversations, discord_url
from organizer.web import create_app


class WebConversationTests(unittest.TestCase):
    setUp = test_library.LibraryTests.setUp

    def prepare(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('''ALTER TABLE files ADD COLUMN discord_message_id TEXT;
                ALTER TABLE files ADD COLUMN discord_guild_id TEXT;
                CREATE TABLE conversation_messages(message_id TEXT PRIMARY KEY,guild_id TEXT,
                channel_id TEXT,parent_id TEXT,category TEXT,author_name TEXT,content TEXT,
                search_content TEXT,created_at TEXT,edited_at TEXT,revision REAL);
                CREATE TABLE conversation_tombstones(message_id TEXT PRIMARY KEY);
                UPDATE files SET discord_message_id='101',discord_guild_id='1';''')
            for mid,channel,parent in [('101','123',None),('102','456','555'),('103','999',None)]:
                db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (mid,'1',channel,parent,'plc','팀원','sensor Friday <script>bad()</script>',
                     'sensor friday <script>bad()</script>',datetime.now(timezone.utc).isoformat(),None,1.0))
        self.app=create_app({'TESTING':True,'DB_PATH':self.db,'STORAGE':self.storage,
            'SECRET_KEY':'test-key','PUBLIC_ACCESS':True,'PUBLIC_ORIGIN':'https://example.test',
            'MESSAGE_CHANNELS':['123','555']},self.ai)
        self.client=self.app.test_client()

    def test_disabled_and_missing_archive_do_not_break_library(self):
        self.assertFalse(self.client.get('/library/api/messages').json['enabled'])
        self.assertEqual(Conversations(self.db,['123']).search()['messages'],[])

    def test_search_scope_forums_links_and_attachments(self):
        self.prepare()
        rows=self.client.get('/library/api/messages?q=sensor').json['messages']
        self.assertEqual({r['message_id'] for r in rows},{'101','102'})
        row=next(r for r in rows if r['message_id']=='101')
        self.assertEqual(row['url'],'https://discord.com/channels/1/123/101')
        self.assertEqual(row['files'][0]['id'],1)
        self.assertEqual(len(self.client.get('/library/api/messages?channel=555').json['messages']),1)
        self.assertEqual(self.client.get('/library/api/messages?channel=999').json['messages'],[])
        self.assertEqual(self.client.get('/library/api/messages?q=%27%20OR%201=1').json['messages'],[])
        self.assertIsNone(discord_url('1','123','javascript:alert(1)'))

    def test_dates_pagination_and_deleted_messages(self):
        self.prepare()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE conversation_messages SET created_at=? WHERE message_id=?',
                       ((datetime.now(timezone.utc)-timedelta(days=10)).isoformat(),'101'))
        c=Conversations(self.db,['123','555'])
        self.assertEqual([r['message_id'] for r in c.search(days=7)['messages']],['102'])
        first=c.search(limit=1);second=c.search(limit=1,offset=1)
        self.assertTrue(first['more'])
        self.assertNotEqual(first['messages'][0]['message_id'],second['messages'][0]['message_id'])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO conversation_tombstones VALUES('102')")
        self.assertEqual([r['message_id'] for r in c.search()['messages']],['101'])
        self.assertEqual(self.client.get('/library/api/messages?days=bad').status_code,400)
        self.assertEqual(self.client.get('/library/api/messages?offset=-1').status_code,400)

    def test_integrated_sources_and_budget_no_provider_for_no_match(self):
        self.prepare()
        response=self.client.post('/library/api/ai',headers=self.post_headers,
                                  json={'task':'ask','question':'sensor'})
        self.assertEqual(response.status_code,200)
        sources=self.ai.run.call_args.args[1]
        self.assertTrue(any(s.get('file_id')==1 for s in sources))
        self.assertEqual({s['message_id'] for s in sources if s.get('kind')=='message'},{'101','102'})
        self.assertLessEqual(sum(len(s['text']) for s in sources),12000)
        self.ai.run.reset_mock()
        self.client.post('/library/api/ai',headers=self.post_headers,json={'task':'ask','question':'unfindable'})
        self.ai.run.assert_not_called()
        self.assertEqual(self.client.post('/library/api/ai',headers=self.post_headers,
                         json={'task':'ask','question':'sensor','days':'7'}).status_code,400)

    def test_edit_or_delete_during_generation_does_not_return_stale_answer(self):
        self.prepare()
        def changed(*args):
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE conversation_messages SET revision=2 WHERE message_id='101'")
            return {'answer':'old answer','sources':[]}
        self.ai.run.side_effect=changed
        r=self.client.post('/library/api/ai',headers=self.post_headers,json={'task':'ask','question':'sensor'})
        self.assertEqual(r.status_code,422)
        self.assertNotIn('old answer',r.text)

    def test_channel_allowlist_is_not_a_request_parameter(self):
        self.prepare()
        r=self.client.get('/library/api/messages?channels=999')
        self.assertNotIn('103',[m['message_id'] for m in r.json['messages']])
        self.assertEqual(self.client.post('/library/api/ai',json={'task':'ask','question':'sensor'}).status_code,403)

    def context_fixture(self):
        self.prepare()
        now=datetime.now(timezone.utc)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('ALTER TABLE conversation_messages ADD COLUMN reply_message_id TEXT')
            db.execute("UPDATE conversation_messages SET content='deadline',search_content='deadline',created_at=?,reply_message_id='201' WHERE message_id='101'",(now.isoformat(),))
            for mid,channel,minutes,reply in [('201','123',-60,None),('202','123',1,'101'),
                    ('203','999',1,'101'),('204','123',2,None),('205','123',30,None),
                    ('206','123',-1,None),('207','456',1,'101')]:
                db.execute('INSERT INTO conversation_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (mid,'1',channel,None,'plc','팀원','금요일까지 마무리','금요일까지 마무리',
                     (now+timedelta(minutes=minutes)).isoformat(),None,1.0,reply))
            db.execute("INSERT INTO conversation_tombstones VALUES('204')")

    def test_weekly_saved_without_provider_on_read_and_invalidated_on_edit(self):
        self.prepare()
        self.assertIsNone(self.client.get('/library/api/weekly').json['saved'])
        self.ai.run.assert_not_called()
        r=self.client.post('/library/api/weekly',headers=self.post_headers,json={})
        self.assertEqual(r.status_code,200)
        self.ai.run.reset_mock()
        self.assertEqual(self.client.get('/library/api/weekly').json['saved']['answer'],r.json['answer'])
        self.ai.run.assert_not_called()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE conversation_messages SET revision=2 WHERE message_id='101'")
        self.assertIsNone(self.client.get('/library/api/weekly').json['saved'])

    def test_weekly_filters_old_files_and_requires_csrf(self):
        self.prepare()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE files SET uploaded_at='2000-01-01'")
        self.assertEqual(self.client.get('/library/api/weekly').json['files'],[])
        self.assertEqual(self.client.post('/library/api/weekly',json={}).status_code,403)
        r=self.client.post('/library/api/weekly',headers=self.post_headers,json={})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['matched_files'],0)
        self.assertTrue(all(s.get('kind')=='message' for s in self.ai.run.call_args.args[1]))

    def test_context_includes_reply_and_neighbors_with_attached_file_without_keyword(self):
        self.context_fixture()
        r=self.client.post('/library/api/ai',headers=self.post_headers,json={'task':'ask','question':'deadline'})
        self.assertEqual(r.status_code,200)
        sources=self.ai.run.call_args.args[1]
        ids={s['message_id'] for s in sources if s.get('kind')=='message'}
        self.assertEqual(ids,{'101','201','202','206'})
        self.assertTrue(any(s.get('file_id')==1 for s in sources))
        self.assertEqual(len(ids),len([s for s in sources if s.get('kind')=='message']))
        self.assertLessEqual(sum(len(s['text']) for s in sources),12000)

    def test_context_respects_date_guild_and_revision_checks(self):
        self.context_fixture()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE conversation_messages SET created_at=? WHERE message_id=?',
                       ((datetime.now(timezone.utc)-timedelta(days=10)).isoformat(),'201'))
            db.execute("UPDATE conversation_messages SET guild_id='2' WHERE message_id='206'")
        c=Conversations(self.db,['123','555'])
        sources=c.sources('deadline',days=7)
        self.assertEqual({s['message_id'] for s in sources},{'101','202'})
        self.assertTrue(c.current(sources))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE conversation_messages SET revision=2 WHERE message_id='202'")
        self.assertFalse(c.current(sources))
