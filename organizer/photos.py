"""Read-only Drive gallery. Originals and OAuth tokens never enter the library DB."""
import io
import re
import threading
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from PIL import Image, ImageOps
import requests

READ_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
ID = re.compile(r'^[A-Za-z0-9_-]{1,200}$')
TEAM_NAMES = {'1a': 'A팀', '1b': 'B팀'}
PAGE_SIZE = 12


class PhotoError(RuntimeError):
    pass


def photo_service(token_path):
    path = Path(token_path).expanduser()
    if not path.is_file():
        raise PhotoError('사진 연결이 아직 준비되지 않았습니다. 관리자에게 Google Drive 사진 연결을 요청해 주세요.')
    creds = Credentials.from_authorized_user_file(path)
    if not creds.has_scopes([READ_SCOPE]):
        raise PhotoError('사진을 읽을 Google Drive 권한이 필요합니다. 관리자에게 사진 연결을 요청해 주세요.')
    if not creds.valid:
        if not creds.refresh_token:
            raise PhotoError('Google Drive 사진 연결을 다시 인증해 주세요.')
        creds.refresh(Request())  # In memory: the web service cannot write auth files.
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    service._photo_credentials = creds
    return service


class Photos:
    def __init__(self, service_factory, folder_id='', root_id=''):
        self.service_factory = service_factory
        self.folder_id, self.root_id = folder_id, root_id
        self.lock = threading.Lock()
        self.service = None
        # Counting must not hold the thumbnail/list service lock. Drive client
        # transports are not shared between these concurrent request streams.
        self.count_lock = threading.Lock()
        self.count_service = None

    def _service(self):
        if self.service is None:
            self.service = self.service_factory()
        return self.service

    @staticmethod
    def folder_url(folder_id):
        return 'https://drive.google.com/drive/folders/' + folder_id if ID.fullmatch(folder_id or '') else None

    def _find(self, service, name, parent=''):
        # Names are constants; IDs are validated before interpolating Drive queries.
        query = "trashed=false and mimeType='application/vnd.google-apps.folder' and name='" + name + "'"
        if parent:
            if not ID.fullmatch(parent):
                raise PhotoError('사진 폴더 설정을 확인해 주세요.')
            query += " and '" + parent + "' in parents"
        result = service.files().list(q=query, fields='files(id)', pageSize=2,
                                      supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        folders = result.get('files', [])
        if len(folders) > 1:
            raise PhotoError('같은 이름의 사진 폴더가 여러 개입니다. 관리자에게 폴더 연결을 요청해 주세요.')
        return folders[0]['id'] if folders else ''

    def _root(self, service, scope):
        base = self.folder_id
        if not base:
            root = self.root_id or self._find(service, 'Project Hub')
            if not root:
                raise PhotoError('Project Hub 폴더를 찾을 수 없습니다.')
            base = self._find(service, '사진', root)
        if not base or not ID.fullmatch(base):
            raise PhotoError('사진 폴더를 찾을 수 없습니다. 관리자에게 폴더 연결을 요청해 주세요.')
        # Never fall back to the original project when a team has no photo folder.
        if scope in TEAM_NAMES:
            base = self._find(service, TEAM_NAMES[scope], base)
        return base

    @staticmethod
    def _query(folder, days):
        from datetime import datetime, timedelta, timezone
        query = "trashed=false and '" + folder + "' in parents and mimeType contains 'image/'"
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            query += " and createdTime >= '" + cutoff + "'"
        return query

    def count(self, scope, days=0):
        with self.count_lock:
            if self.count_service is None:
                self.count_service = self.service_factory()
            service = self.count_service
            folder = self._root(service, scope)
            if not folder:
                return 0
            query = self._query(folder, days)
            ids, pages, token = set(), set(), ''
            while True:
                result = service.files().list(q=query, fields='nextPageToken,incompleteSearch,files(id,parents)',
                    pageSize=1000, pageToken=token or None,
                    supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                if result.get('incompleteSearch'):
                    raise PhotoError('전체 사진 수를 확인하지 못했습니다. 다시 새로고침해 주세요.')
                ids.update(item['id'] for item in result.get('files', [])
                           if folder in item.get('parents', []) and ID.fullmatch(item.get('id', '')))
                token = result.get('nextPageToken', '')
                if not token:
                    return len(ids)
                if token in pages:
                    raise PhotoError('전체 사진 수를 확인하지 못했습니다. 다시 새로고침해 주세요.')
                pages.add(token)

    def listing(self, scope, sort='date_desc', days=0, page_token=''):
        with self.lock:
            service = self._service()
            folder = self._root(service, scope)
            if not folder:
                return dict(photos=[], next_page_token='', folder_url=None,
                            notice='이 팀의 사진 폴더가 아직 없습니다. 사진/' + TEAM_NAMES[scope] + ' 폴더를 만들면 연결됩니다.')
            query = self._query(folder, days)
            result = service.files().list(q=query, fields='nextPageToken,files(id,name,mimeType,createdTime,parents)',
                orderBy='createdTime desc,name' if sort == 'date_desc' else 'createdTime,name', pageSize=PAGE_SIZE,
                pageToken=page_token or None, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            items = []
            for item in result.get('files', []):
                if folder not in item.get('parents', []) or not ID.fullmatch(item.get('id', '')):
                    continue
                items.append(dict(id=item['id'], name=item.get('name', '사진'), created_at=item.get('createdTime', ''),
                                  url='https://drive.google.com/file/d/' + item['id'] + '/view',
                                  preview_url='https://drive.google.com/file/d/' + item['id'] + '/preview'))
            return dict(photos=items, next_page_token=result.get('nextPageToken', ''),
                        folder_url=self.folder_url(folder), notice='')

    def thumbnail(self, scope, file_id):
        if not ID.fullmatch(file_id):
            raise FileNotFoundError()
        with self.lock:
            service = self._service()
            folder = self._root(service, scope)
            if not folder:
                raise FileNotFoundError()
            item = service.files().get(fileId=file_id, fields='parents,mimeType,trashed,thumbnailLink',
                                       supportsAllDrives=True).execute()
            if item.get('trashed') or folder not in item.get('parents', []) or not item.get('mimeType', '').startswith('image/'):
                raise FileNotFoundError()
            url = item.get('thumbnailLink', '')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.googleusercontent.com'):
                raise PhotoError('미리보기를 준비하지 못했습니다. Google Drive에서 원본을 확인해 주세요.')
            # Thumbnail links are short-lived. Keep them server-side and stream a bounded image.
            creds = getattr(service, '_photo_credentials', None)
            headers = {'Authorization': 'Bearer ' + creds.token} if creds else {}
        with requests.get(url, headers=headers, stream=True, timeout=(5, 15), allow_redirects=False) as response:
            if response.status_code != 200:
                raise PhotoError('사진 미리보기를 불러오지 못했습니다.')
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise PhotoError('사진 미리보기가 너무 큽니다.')
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 16_000_000:
                raise PhotoError('사진 미리보기가 너무 큽니다.')
            image = ImageOps.exif_transpose(original).convert('RGB')
            image.thumbnail((640, 640))
            output = io.BytesIO()
            image.save(output, format='JPEG', quality=82)
            return output.getvalue()


def callback_code(response, redirect_uri, state):
    """Validate a copied loopback response before exchanging its private code."""
    import secrets
    if len(response) > 16384:
        raise PhotoError('인증 후 주소 전체를 다시 확인해 주세요.')
    actual, expected = urlparse(response.strip()), urlparse(redirect_uri)
    if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path) or actual.fragment:
        raise PhotoError('이번 인증에서 나온 127.0.0.1 주소 전체를 붙여넣어 주세요.')
    query = parse_qs(actual.query)
    states, codes = query.get('state', []), query.get('code', [])
    if len(states) != 1 or not secrets.compare_digest(states[0].encode(), state.encode()):
        raise PhotoError('다른 인증의 주소입니다. 이번 터미널에서 시작한 인증 주소를 사용해 주세요.')
    if 'error' in query or len(codes) != 1 or not codes[0]:
        raise PhotoError('Google 권한 동의가 완료되지 않았습니다.')
    return codes[0]


def pi_authorize(flow):
    """Keep the PKCE verifier on Pi; copy only the desktop loopback response."""
    import getpass
    import socket
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        flow.redirect_uri = 'http://127.0.0.1:' + str(listener.getsockname()[1]) + '/'
        url, state = flow.authorization_url(prompt='consent', access_type='offline')
        if urlparse(url).scheme != 'https' or urlparse(url).hostname != 'accounts.google.com':
            raise PhotoError('Google 데스크톱 앱 인증 설정을 확인해 주세요.')
        print('아래 주소를 개인 PC 브라우저에서 열고 Drive 읽기/사진 업로드 권한에 동의하세요.\n' + url, flush=True)
        print('동의 뒤 127.0.0.1 연결 오류가 나면 주소창의 전체 주소를 복사하세요.\n채팅에 보내지 말고 아래 Pi 터미널 입력에만 붙여넣으세요.', flush=True)
        response = getpass.getpass('인증 후 주소 전체(입력 내용은 숨김): ')
        code = callback_code(response, flow.redirect_uri, state)
        # State is checked above. The code and PKCE verifier are exchanged over
        # Google's HTTPS token endpoint; no insecure transport override is used.
        flow.fetch_token(code=code, timeout=30)
    return flow.credentials


def pi_client(root):
    """Use the original desktop client locally without rewriting bot auth."""
    import json
    path = root / 'credentials.json'
    if path.is_file():
        config = json.loads(path.read_text(encoding='utf-8'))
        if 'installed' not in config:
            raise PhotoError('기존 Google 데스크톱 앱 credentials.json이 필요합니다.')
        return config
    path = root / 'token.json'
    if not path.is_file():
        raise PhotoError('Pi 운영 폴더에 기존 Google 인증 설정을 찾을 수 없습니다.')
    token = json.loads(path.read_text(encoding='utf-8'))
    if not token.get('client_id') or not token.get('client_secret'):
        raise PhotoError('기존 Google 데스크톱 앱 credentials.json이 필요합니다.')
    return {'installed': {'client_id': token['client_id'], 'client_secret': token['client_secret'],
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': ['http://localhost']}}


def save_photo_token(path, creds):
    import os
    import tempfile
    if path.is_symlink() or path.name in {'token.json', 'credentials.json'}:
        raise PhotoError('사진용 photos-token.json을 사용하세요. 기존 봇 토큰은 교체하지 않습니다.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.photos-token-', delete=False) as stream:
            temp = Path(stream.name)
            stream.write(creds.to_json())
        if os.name == 'posix':
            temp.chmod(0o600)
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Authorize a separate Drive photo token locally; never replace bot token.json.')
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--pi', action='store_true', help='Use the existing Pi desktop OAuth client and paste the browser callback into the authenticated terminal')
    parser.add_argument('--token', type=Path, default=Path.home()/'.config/project-hub/photos-token.json')
    parser.add_argument('--upload', action='store_true', help='Also allow new Discord photo uploads using drive.file, without full Drive write access')
    args = parser.parse_args()
    if not args.pi and args.credentials is None:
        parser.error('--credentials 또는 --pi가 필요합니다.')
    if (args.token.name in {'token.json', 'credentials.json'} or args.token.is_symlink()
            or (args.credentials and args.token.resolve() == args.credentials.resolve())):
        parser.error('기존 봇 토큰 대신 사진용 photos-token.json을 사용하세요.')
    # Google consent covers Drive read access; the app only serves configured photo folders.
    scopes = [READ_SCOPE]
    if args.upload:
        scopes.append('https://www.googleapis.com/auth/drive.file')
    try:
        if args.pi:
            flow = InstalledAppFlow.from_client_config(pi_client(Path.home() / 'project-hub'),
                                                      scopes, autogenerate_code_verifier=True)
            creds = pi_authorize(flow)
        else:
            flow = InstalledAppFlow.from_client_secrets_file(args.credentials, scopes,
                                                           autogenerate_code_verifier=True)
            creds = flow.run_local_server(port=0, prompt='consent')
        if not creds.refresh_token or not creds.has_scopes(scopes):
            raise PhotoError('Google 권한 동의와 오프라인 접근을 다시 확인해 주세요.')
        save_photo_token(args.token, creds)
    except PhotoError as exc:
        parser.exit(2, str(exc) + '\n')
    except KeyboardInterrupt:
        parser.exit(130, '인증을 취소했습니다. 기존 토큰은 유지합니다.\n')
    except Exception:
        # OAuth failures can include private response data; keep it out of logs.
        parser.exit(2, '사진 인증에 실패했습니다. Google 동의 화면과 기존 앱 설정을 확인해 주세요.\n')
    print('사진 연결 인증 완료. 토큰은 개인 서버에만 보관하세요.')


if __name__ == '__main__':
    main()
