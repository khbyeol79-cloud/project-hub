from contextlib import closing
import sqlite3
import unittest
import test_library


class VersionTests(unittest.TestCase):
    setUp=test_library.LibraryTests.setUp

    def prepare(self):
        with closing(sqlite3.connect(self.db)) as db,db:
            db.execute("UPDATE files SET uploaded_at='2026-10-01T00:00:00Z'")
            for fid,name,channel,sha,date in [
                (2,'sample.txt','123','new','2026-10-02T09:00:00+09:00'),
                (3,'sample.txt','123','new','2026-10-02T00:00:00Z'),
                (4,'renamed.txt','999','hash','2026-10-03T00:00:00Z'),
                (5,'sample.txt','999','other','2026-10-04T00:00:00Z')]:
                db.execute('''INSERT INTO files(id,original_filename,discord_channel_id,discord_channel,
                    sha256,uploaded_at,file_size_bytes) VALUES(?,?,?,?,?,?,?)''',
                    (fid,name,channel,channel,sha,date,5))

    def test_groups_do_not_merge_channels_and_duplicate_names_are_not_required(self):
        self.prepare()
        f=self.client.get('/library/api/files/1').json
        self.assertEqual((f['upload_count'],f['content_versions'],f['identical_count'],f['latest_id']),(3,2,1,3))
        self.assertEqual({r['id'] for r in f['related']},{2,3,4})
        self.assertTrue(next(r for r in f['related'] if r['id']==4)['identical'])
        self.assertNotIn('local_path',f)

    def test_latest_uses_dates_and_id_ties_before_search_and_pagination(self):
        self.prepare()
        result=self.client.get('/library/api/files?view=latest').json
        self.assertEqual({f['id'] for f in result['files']},{3,4,5})
        self.assertEqual(result['total'],3)
        all_rows=self.client.get('/library/api/files?sort=date_desc').json['files']
        self.assertEqual([f['id'] for f in all_rows],[5,4,3,2,1])
        self.assertEqual(self.client.get('/library/api/files?view=latest&channel=123').json['files'][0]['id'],3)
        # The older file matches sensor, but the latest upload does not: no false latest result.
        self.assertEqual(self.client.get('/library/api/files?view=latest&q=sensor').json['total'],0)
        self.assertEqual(self.client.get('/library/api/files?view=unknown').status_code,400)

    def test_integrated_citations_label_upload_date(self):
        self.prepare()
        self.client.post('/library/api/ai',headers=self.post_headers,json={'task':'ask','question':'sensor'})
        self.assertIn('업로드 2026-10-01',self.ai.run.call_args.args[1][0]['label'])
