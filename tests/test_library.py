from contextlib import closing
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
import io
import zipfile
from unittest.mock import Mock

from organizer.web import create_app
from organizer.share import configure
from organizer.install_web import build_caddy, OLD, NEW
from organizer.extract_extra import extract


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
            db.execute('ALTER TABLE files ADD COLUMN google_drive_file_id TEXT')
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

    def test_public_mode_works_without_cookie_but_keeps_csrf(self):
        self.app.config['PUBLIC_ACCESS']=True
        anon=self.app.test_client()
        self.assertEqual(anon.get('/library/api/files').status_code,200)
        self.assertEqual(anon.get('/library/api/share-link').json['url'],'https://example.test/library/')
        self.assertEqual(anon.post('/library/api/files/1/ai',json={'task':'summary'}).status_code,403)

    def test_drive_link_requires_valid_uploaded_id(self):
        for drive_id,expected in [(None,None),('file_123','https://drive.google.com/file/d/file_123/view'),('https://other.test/',None)]:
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE files SET google_drive_file_id=? WHERE id=1',(drive_id,))
            self.assertEqual(self.client.get('/library/api/files/1').json['drive_url'],expected)

    def put_file(self,name,body):
        p=self.storage/name
        p.write_bytes(body)
        sha=hashlib.sha256(body).hexdigest()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE files SET original_filename=?,local_path=?,sha256=? WHERE id=1',(name,str(p),sha))
            db.execute("UPDATE content_documents SET status='unsupported'")
        return {'path':str(p),'storage':str(self.storage),'filename':name,'sha256':sha}

    def test_markdown_detail_search_and_ai_share_same_text(self):
        text='# Meeting\n\nFriday inspection.\n<script>alert(1)</script>'
        self.put_file('notes.md',text.encode())
        before=self.db.read_bytes()
        detail=self.client.get('/library/api/files/1').json
        self.assertEqual(detail['pages'][0]['text'],text)
        self.assertEqual(detail['content_status'],'indexed')
        self.assertEqual(len(self.client.get('/library/api/files?q=inspection').json['files']),1)
        r=self.client.post('/library/api/files/1/ai',json={'task':'summary'},headers=self.post_headers)
        self.assertEqual(r.status_code,200)
        self.assertEqual(self.ai.run.call_args.args[1][0]['text'],text)
        self.assertEqual(self.db.read_bytes(),before)

    def test_markdown_changed_hash_is_rejected(self):
        job=self.put_file('notes.md',b'original')
        Path(job['path']).write_bytes(b'changed')
        self.assertEqual(extract(job)['status'],'changed')

    def test_html_extracts_text_without_active_content(self):
        body=b'<h1>Plan</h1><p>Friday &amp; Monday</p><script>steal()</script><style>secret</style><iframe src="https://evil.test">hidden</iframe>'
        result=extract(self.put_file('plan.html',body))
        self.assertEqual(result['pages'][0]['text'],'Plan\nFriday & Monday')

    def test_reading_preserves_html_structure_without_active_markup(self):
        self.put_file('plan.html',b'<h1>Plan</h1><script>secret()</script><table><tr><td>A</td><td>B</td></tr></table>')
        detail=self.client.get('/library/api/files/1').json
        self.assertEqual(detail['reading_blocks'][0],{'type':'h1','text':'Plan'})
        self.assertEqual(detail['reading_blocks'][1]['rows'],[['A','B']])
        self.assertNotIn('secret',str(detail['reading_blocks']))
        self.assertTrue(detail['pages'])

    def test_docx_reading_preserves_table_and_original_source_pages(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Plan</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
        self.put_file('plan.docx',out.getvalue())
        before=self.db.read_bytes()
        detail=self.client.get('/library/api/files/1').json
        self.assertEqual(detail['reading_blocks'][0]['type'],'h2')
        self.assertEqual(detail['reading_blocks'][1]['rows'],[['Cell']])
        self.assertEqual(self.db.read_bytes(),before)

    def test_html_preview_is_isolated_and_authenticated(self):
        self.put_file('plan.html',b'<style>h1{color:red}</style><h1>Plan</h1><script>alert(1)</script>')
        route='/library/files/1/html-preview'
        self.assertEqual(self.app.test_client().get(route).status_code,401)
        r=self.client.get(route)
        self.assertEqual(r.status_code,200)
        self.assertIn('<h1>Plan</h1>',r.text)
        policy=r.headers['Content-Security-Policy']
        for rule in ['sandbox;',"default-src 'none'","frame-ancestors 'self'","form-action 'none'"]:
            self.assertIn(rule,policy)
        self.assertNotIn('allow-scripts',policy)
        self.put_file('not-html.txt',b'<h1>no</h1>')
        self.assertEqual(self.client.get(route).status_code,404)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE files SET original_filename='escape.html',local_path=?",(str(self.db),))
        self.assertEqual(self.client.get(route).status_code,404)

    def test_pptx_uses_presentation_order(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('ppt/presentation.xml','''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="r2"/><p:sldId r:id="r1"/></p:sldIdLst></p:presentation>''')
            z.writestr('ppt/_rels/presentation.xml.rels','<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>')
            for number in (1,2):
                z.writestr(f'ppt/slides/slide{number}.xml',f'<root xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>Slide {number}</a:t></a:r></a:p></root>')
        result=extract(self.put_file('slides.pptx',out.getvalue()))
        self.assertEqual([p['text'] for p in result['pages']],['Slide 2','Slide 1'])
        self.assertEqual(result['pages'][0]['label'],'슬라이드 1')

    def test_markup_cannot_be_served_as_image(self):
        self.put_file('bad.png',b'<svg onload="alert(1)"></svg>')
        self.assertEqual(self.client.get('/library/files/1/preview').status_code,404)

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

    def test_extension_sort_and_pagination_use_full_collection(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            for i in range(2,36):
                db.execute('INSERT INTO files(id,original_filename,discord_channel_id,uploaded_at) VALUES(?,?,?,?)',
                           (i,f'{i:02d}.PDF','123','2026-09-01' if i==35 else '2026-10-01'))
        base='/library/api/files?extension=.pdf&channel=123'
        first=self.client.get(base+'&sort=name_asc').json
        second=self.client.get(base+'&sort=name_asc&offset=30').json
        self.assertEqual(first['total'],34)
        self.assertTrue(first['more'])
        self.assertEqual([r['id'] for r in first['files']+second['files']],list(range(2,36)))
        self.assertEqual(self.client.get(base+'&sort=date_asc').json['files'][0]['id'],35)
        self.assertEqual(self.client.get(base+'&sort=date_desc').json['files'][0]['id'],34)
        self.assertEqual(self.client.get(base+'&sort=name_desc').json['files'][0]['id'],35)
        self.assertEqual(self.client.get(base+'&q=sample').json['total'],0)
        self.assertIn('.pdf',first['extensions'])
        self.assertEqual(self.client.get(base+'&sort=malicious').status_code,400)

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
