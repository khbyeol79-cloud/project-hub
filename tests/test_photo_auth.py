import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse, urlencode
from unittest.mock import Mock, patch

from google_auth_oauthlib.flow import InstalledAppFlow
from organizer.photos import PhotoError, callback_code, pi_authorize, pi_client, save_photo_token


CLIENT = {'installed': {'client_id': 'test-client.apps.googleusercontent.com',
          'client_secret': 'test-only-secret',
          'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
          'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': ['http://localhost']}}


class PhotoAuthorizationTests(unittest.TestCase):
    def test_response_must_match_redirect_state_and_single_code(self):
        redirect = 'http://127.0.0.1:31415/'
        good = redirect + '?state=chosen-state&code=test-only-code'
        self.assertEqual(callback_code(good, redirect, 'chosen-state'), 'test-only-code')
        for bad in [good.replace('31415', '31416'), good.replace('127.0.0.1', 'evil.test'),
                    good.replace('chosen-state', 'other-state'), good + '&state=chosen-state',
                    good + '&code=second', good + '#fragment',
                    redirect + '?state=chosen-state&error=access_denied',
                    redirect + '?code=missing-state', 'test-only-code']:
            with self.subTest(bad=bad), self.assertRaises(PhotoError):
                callback_code(bad, redirect, 'chosen-state')

    def test_pi_flow_keeps_pkce_and_code_local_without_browser_or_http_override(self):
        flow = InstalledAppFlow.from_client_config(CLIENT, ['scope-a'],
                                                   autogenerate_code_verifier=True)
        received = {}
        real_authorization_url = flow.authorization_url
        def authorization_url(**kwargs):
            url, state = real_authorization_url(**kwargs)
            received.update(url=url, state=state)
            return url, state
        def response(_prompt):
            return flow.redirect_uri + '?' + urlencode({'state': received['state'], 'code': 'test-only-code'})
        with patch.object(flow, 'authorization_url', side_effect=authorization_url), \
             patch.object(flow, 'fetch_token') as exchange, \
             patch('getpass.getpass', side_effect=response), \
             patch.object(type(flow), 'credentials', new_callable=unittest.mock.PropertyMock, return_value='test-creds'), \
             patch('webbrowser.open') as browser, patch('sys.stdout', new_callable=io.StringIO) as output:
            previous = os.environ.get('OAUTHLIB_INSECURE_TRANSPORT')
            self.assertEqual(pi_authorize(flow), 'test-creds')
            self.assertEqual(os.environ.get('OAUTHLIB_INSECURE_TRANSPORT'), previous)
            browser.assert_not_called()
            exchange.assert_called_once_with(code='test-only-code', timeout=30)
            self.assertNotIn('test-only-code', output.getvalue())
            self.assertNotIn('test-only-secret', output.getvalue())
        params = parse_qs(urlparse(received['url']).query)
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertTrue(params['code_challenge'])
        self.assertEqual(params['prompt'], ['consent'])
        self.assertEqual(params['access_type'], ['offline'])
        self.assertEqual(params['redirect_uri'], [flow.redirect_uri])
        self.assertNotEqual(params['code_challenge'][0], flow.code_verifier)

    def test_wrong_response_never_exchanges_code(self):
        flow = Mock()
        flow.authorization_url.return_value = ('https://accounts.google.com/auth', 'current-state')
        with patch('getpass.getpass', return_value='http://evil.test/?state=current-state&code=test'), \
             patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaises(PhotoError):
                pi_authorize(flow)
        flow.fetch_token.assert_not_called()

    def test_existing_client_or_token_is_read_without_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'credentials.json'
            original = json.dumps(CLIENT)
            source.write_text(original)
            self.assertEqual(pi_client(root), CLIENT)
            self.assertEqual(source.read_text(), original)
            source.unlink()
            source = root / 'token.json'
            original = json.dumps({'client_id': CLIENT['installed']['client_id'],
                                   'client_secret': 'test-only-secret', 'refresh_token': 'bot-test-only'})
            source.write_text(original)
            client = pi_client(root)
            self.assertEqual(client['installed']['client_id'], CLIENT['installed']['client_id'])
            self.assertNotIn('refresh_token', client['installed'])
            self.assertEqual(source.read_text(), original)

    def test_save_is_atomic_private_and_never_overwrites_bot_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            creds = Mock()
            creds.to_json.return_value = '{"test":"photo-token"}'
            path = root / 'private/photos-token.json'
            save_photo_token(path, creds)
            self.assertEqual(path.read_text(), creds.to_json.return_value)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            creds.to_json.side_effect = RuntimeError('test write failure')
            with self.assertRaises(RuntimeError):
                save_photo_token(path, creds)
            self.assertEqual(path.read_text(), '{"test":"photo-token"}')
            self.assertEqual(list(path.parent.glob('.photos-token-*')), [])
            for name in ['token.json', 'credentials.json']:
                original = root / name
                original.write_text('original')
                with self.assertRaises(PhotoError):
                    save_photo_token(original, creds)
                self.assertEqual(original.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
