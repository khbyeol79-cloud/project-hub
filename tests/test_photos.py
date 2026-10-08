from pathlib import Path
import io
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image
from organizer.photos import Photos, PhotoError, photo_service
from organizer.web import create_app


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.photos = Photos(lambda: self.service, 'photos_root')

    def test_list_pagination_days_and_remote_parent_validation(self):
        self.service.files().list().execute.return_value = {'nextPageToken':'next', 'files':[
            {'id':'pic1','name':'<script>.jpg','parents':['photos_root'],'createdTime':'2026-10-01'},
            {'id':'other','name':'private.jpg','parents':['private']}]}
        result=self.photos.listing('main','date_asc',7,'page')
        args=self.service.files().list.call_args.kwargs
        self.assertIn("'photos_root' in parents",args['q'])
        self.assertIn('createdTime >=',args['q'])
        self.assertEqual(args['pageToken'],'page')
        self.assertEqual(args['orderBy'],'createdTime,name')
        self.assertEqual([x['id'] for x in result['photos']],['pic1'])
        self.assertEqual(result['next_page_token'],'next')
        self.assertNotIn('thumbnailLink',str(result))

    def test_teams_never_fall_back_to_main(self):
        self.service.files().list().execute.return_value={'files':[]}
        r=self.photos.listing('1a')
        self.assertEqual(r['photos'],[])
        self.assertIsNone(r['folder_url'])
        self.assertIn('사진/A팀',r['notice'])
        self.assertIn("name='A팀'",self.service.files().list.call_args.kwargs['q'])

    def test_team_query_is_separate_and_thumbnail_checks_parent(self):
        self.service.files().list().execute.side_effect=[{'files':[{'id':'team_a'}]}, {'files':[]}]
        self.photos.listing('1a')
        self.assertIn("'team_a' in parents",self.service.files().list.call_args.kwargs['q'])
        self.service.files().list().execute.side_effect=None
        self.service.files().list().execute.return_value={'files':[{'id':'team_b'}]}
        self.service.files().get().execute.return_value={'mimeType':'image/jpeg','parents':['team_a'],'thumbnailLink':'https://lh3.googleusercontent.com/photo'}
        with patch('organizer.photos.requests.get') as get:
            with self.assertRaises(FileNotFoundError):self.photos.thumbnail('1b','pic1')
            get.assert_not_called()

    def test_thumbnail_never_fetches_untrusted_host_or_outside_folder(self):
        for parents,url in [(['private'],'https://lh3.googleusercontent.com/a'),(['photos_root'],'https://evil.test/a'),(['photos_root'],'http://lh3.googleusercontent.com/a')]:
            self.service.files().get().execute.return_value={'mimeType':'image/jpeg','parents':parents,'thumbnailLink':url}
            with patch('organizer.photos.requests.get') as get:
                with self.assertRaises((PhotoError,FileNotFoundError)):self.photos.thumbnail('main','pic1')
                get.assert_not_called()

    def test_thumbnail_is_bounded_reencoded_and_drops_metadata(self):
        raw=io.BytesIO();Image.new('RGB',(800,600),'green').save(raw,'PNG')
        self.service.files().get().execute.return_value={'mimeType':'image/png','parents':['photos_root'],'thumbnailLink':'https://lh3.googleusercontent.com/a'}
        response=Mock(status_code=200);response.iter_content.return_value=[raw.getvalue()]
        self.service._photo_credentials=Mock(token='test-only')
        with patch('organizer.photos.requests.get') as get:
            get.return_value.__enter__.return_value=response
            result=self.photos.thumbnail('main','pic1')
            self.assertEqual(get.call_args.kwargs['headers'],{'Authorization':'Bearer test-only'})
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            with Image.open(io.BytesIO(result)) as image:
                self.assertEqual(image.format,'JPEG');self.assertLessEqual(max(image.size),640)
            response.iter_content.return_value=[b'x'*(4*1024*1024+1)]
            with self.assertRaises(PhotoError):self.photos.thumbnail('main','pic1')

    def test_missing_token_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as folder, patch('organizer.photos.InstalledAppFlow') as flow:
            with self.assertRaises(PhotoError):photo_service(Path(folder)/'missing.json')
            flow.assert_not_called()

    def test_routes_require_access_and_validate_scope_and_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            mock=Mock()
            mock.listing.return_value={'photos':[],'next_page_token':'','folder_url':None}
            mock.thumbnail.return_value=b'test-jpeg'
            app=create_app({'DB_PATH':Path(folder)/'db.sqlite','STORAGE':folder,'PHOTOS':mock,'SECRET_KEY':'test','PHOTO_FOLDER_ID':'photos_root'})
            client=app.test_client()
            for route in ['/library/api/photos','/library/photos/pic/thumbnail']:
                self.assertEqual(client.get(route).status_code,401)
            app.config['PUBLIC_ACCESS']=True
            for query in ['project=unknown','sort=bad','days=1','page_token='+'x'*4097]:
                self.assertEqual(client.get('/library/api/photos?'+query).status_code,400)
            self.assertEqual(client.get('/library/api/photos?project=additional&team=1a').status_code,200)
            mock.listing.assert_called_once_with('1a','date_desc',0,'')
            self.assertEqual(client.get('/library/photos/pic/thumbnail').mimetype,'image/jpeg')
            mock.listing.side_effect=PhotoError('test error')
            self.assertIsNone(client.get('/library/api/photos?project=additional&team=1b').json['folder_url'])
            self.assertEqual(client.get('/library/api/photos').json['folder_url'],'https://drive.google.com/drive/folders/photos_root')
            self.assertEqual(client.get('/library/api/photos',environ_base={'REMOTE_ADDR':'192.168.1.2'}).status_code,403)
