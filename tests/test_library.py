from contextlib import closing
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from organizer.web import create_app
from organizer.share import configure
from organizer.install_web import build_caddy, OLD, NEW


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.storage=self.root/'storage'
        self.storage.mkdir()
        (self.storage/'sample.txt').write_text('sample',encoding='utf-8')
        self.db=self.root/'source.db'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript('''CREATE TABLE files(id INTEGER PRIMARY KEY,original_filename TEXT,
                discord_channel TEXT,discord_channel_id TEXT,category TEXT,uploaded_at TEXT,
                file_size_bytes INTEGER,version INTEGER,duplicate_type TEXT,sha256 TEXT,local_path TEXT);
                CREATE TABLE content_documents(file_id INTEGER,source_sha256 TEXT,status TEXT);
                CREATE TABLE content_pages(file_id INTEGER,page INTEGER,body TEXT,search_body TEXT);
                CREATE TABLE content_locations(file_id INTEGER,page INTEGER,label TEXT);
                INSERT INTO content_documents VALUES(1,'hash','indexed');
                INSERT INTO content_pages VALUES(1,1,'sensor Friday','sensor friday');
                INSERT INTO content_locations VALUES(1,1,'page one');''')
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (1,'sample.txt','PLC','123','plc','2026-10-02',6,1,None,'hash',str(self.storage/'sample.txt')))
        self.ai=Mock()
        self.ai.settings=SimpleNamespace(enabled=True,max_chars=12000,daily_limit=20,monthly_limit=300)
        self.ai.run.return_value={'answer':'Friday [S1]','sources':[{'id':'S1','page':1}],'partial':False,'cached':False}
        self.ai.ledger.counts.return_value={'daily_calls':0,'monthly_calls':0}
        self.token='test-only-share-link-token-32-characters'
        self.digest=hashlib.sha256(self.token.encode()).hexdigest()
        self.app=create_app({'TESTING':True,'DB_PATH':self.db,'STORAGE':self.storage,
                             'SECRET_KEY':'test-session-key','SHARE_HASH':self.digest,
                             'PUBLIC_ORIGIN':'https://example.test'},self.ai)
        self.client=self.app.test_client()
        with self.client.session_transaction() as s:
            s['access']=self.digest
        self.headers={'Remote-User':'khb'}
        self.post_headers=dict(self.headers,Origin='https://example.test',**{'X-Requested-With':'ProjectHub'})

    def test_data_endpoints_require_valid_link_session(self):
        anon=self.app.test_client()
        for path in ['/library/api/files','/library/api/files/1',
                     '/library/files/1/download','/library/api/status']:
            self.assertEqual(anon.get(path).status_code,401,path)
            self.assertEqual(anon.get(path,headers={'Remote-User':'khb'}).status_code,401,path)
        self.assertEqual(self.client.get('/library/',headers=self.headers,environ_base={'REMOTE_ADDR':'192.168.0.5'}).status_code,403)

    def test_link_exchange_cookie_and_rotation(self):
        anon=self.app.test_client()
        self.assertEqual(anon.post('/library/api/access',json={'token':self.token}).status_code,403)
        self.assertEqual(anon.post('/library/api/access',headers=self.post_headers,json={'token':'wrong'*10}).status_code,401)
        r=anon.post('/library/api/access',headers=self.post_headers,json={'token':self.token})
        self.assertEqual(r.status_code,200)
        for flag in ['Secure','HttpOnly','SameSite=Strict','Path=/library/']:
            self.assertIn(flag,r.headers['Set-Cookie'])
        self.assertEqual(anon.get('/library/api/files').status_code,200)
        self.app.config['SHARE_HASH']='0'*64
        self.assertEqual(anon.get('/library/api/files').status_code,401)
        self.assertEqual(anon.post('/library/api/access',headers=self.post_headers,json={'token':self.token}).status_code,401)

    def test_link_configuration_requires_explicit_rotation(self):
        folder=self.root/'config'
        configure(folder,'https://example.test')
        first=(folder/'library-share-url.txt').read_text()
        with self.assertRaises(FileExistsError):
            configure(folder,'https://example.test')
        configure(folder,'https://example.test',rotate=True)
        self.assertNotEqual(first,(folder/'library-share-url.txt').read_text())

    def test_share_link_endpoint_requires_access(self):
        self.assertEqual(self.app.test_client().get('/library/api/share-link').status_code,401)
        p=self.root/'link.txt'
        p.write_text('https://example.test/library/#key='+self.token)
        self.app.config['SHARE_URL_FILE']=p
        self.assertEqual(self.client.get('/library/api/share-link').json['url'],'https://example.test/library/#key='+self.token)

    def test_search_content_and_channel_scoping(self):
        r=self.client.get('/library/api/files?q=Friday&channel=123',headers=self.headers)
        self.assertEqual(len(r.json['files']),1)
        self.assertNotIn('local_path',r.json['files'][0])
        self.assertEqual(self.client.get('/library/api/files?channel=999',headers=self.headers).json['files'],[])
        self.assertEqual(self.client.get('/library/api/files?q=%27%20OR%201=1',headers=self.headers).json['files'],[])

    def test_stale_content_hidden_from_search_and_detail(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE files SET sha256='new'")
        self.assertEqual(self.client.get('/library/api/files?q=Friday',headers=self.headers).json['files'],[])
        self.assertEqual(self.client.get('/library/api/files/1',headers=self.headers).json['pages'],[])

    def test_download_is_attachment_and_stays_in_storage(self):
        r=self.client.get('/library/files/1/download',headers=self.headers)
        self.assertIn('attachment',r.headers['Content-Disposition'])
        self.assertEqual(r.data,b'sample')
        r.close()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE files SET local_path=?',(str(self.db),))
        self.assertEqual(self.client.get('/library/files/1/download',headers=self.headers).status_code,404)

    def test_ai_csrf_and_input_validation(self):
        for headers in (self.headers,dict(self.post_headers,Origin='https://evil.test')):
            self.assertEqual(self.client.post('/library/api/files/1/ai',headers=headers,json={'task':'summary'}).status_code,403)
        self.assertEqual(self.client.post('/library/api/files/1/ai',headers=self.post_headers,json={'task':'ask','question':{}}).status_code,400)
        self.ai.run.assert_not_called()
        r=self.client.post('/library/api/files/1/ai',headers=self.post_headers,json={'task':'ask','question':'When?'})
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.ai.run.call_args.args[1][0]['text'],'sensor Friday')

    def test_security_headers_and_static_allowlist(self):
        r=self.client.get('/library/',headers=self.headers)
        self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        r.close()
        self.assertEqual(self.client.get('/library/assets/ai.env',headers=self.headers).status_code,404)

    def test_caddy_change_preserves_other_hosts_and_auth(self):
        auth='auth.khbps.duckdns.org {\n reverse_proxy 127.0.0.1:9091\n}\n\n'
        updated=build_caddy(auth+OLD+'\n')
        self.assertEqual(updated,auth+NEW+'\n')
        self.assertEqual(updated.count('forward_auth 127.0.0.1:9091'),1)

    def test_changed_caddy_is_not_overwritten(self):
        with self.assertRaises(RuntimeError):
            build_caddy(OLD.replace('8080','8081'))


if __name__=='__main__':
    unittest.main()
