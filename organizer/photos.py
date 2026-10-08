"""Read-only Drive gallery. Originals and OAuth tokens never enter the library DB."""
import io
import re
import threading
from pathlib import Path
from urllib.parse import urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from PIL import Image, ImageOps
import requests

READ_SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
ID = re.compile(r'^[A-Za-z0-9_-]{1,200}$')
TEAM_NAMES = {'1a': 'A팀', '1b': 'B팀'}


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

    def listing(self, scope, sort='date_desc', days=0, page_token=''):
        from datetime import datetime, timedelta, timezone
        with self.lock:
            service = self._service()
            folder = self._root(service, scope)
            if not folder:
                return dict(photos=[], next_page_token='', folder_url=None,
                            notice='이 팀의 사진 폴더가 아직 없습니다. 사진/' + TEAM_NAMES[scope] + ' 폴더를 만들면 연결됩니다.')
            query = "trashed=false and '" + folder + "' in parents and mimeType contains 'image/'"
            if days:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                query += " and createdTime >= '" + cutoff + "'"
            result = service.files().list(q=query, fields='nextPageToken,files(id,name,mimeType,createdTime,parents)',
                orderBy='createdTime desc,name' if sort == 'date_desc' else 'createdTime,name', pageSize=30,
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


def main():
    import argparse
    import os
    parser = argparse.ArgumentParser(description='Authorize a separate Drive photo token locally; never replace bot token.json.')
    parser.add_argument('--credentials', type=Path, required=True)
    parser.add_argument('--token', type=Path, default=Path.home()/'.config/project-hub/photos-token.json')
    parser.add_argument('--upload', action='store_true', help='Also allow new Discord photo uploads using drive.file, without full Drive write access')
    args = parser.parse_args()
    # Google consent covers Drive read access; the app only serves configured photo folders.
    scopes = [READ_SCOPE]
    if args.upload:
        scopes.append('https://www.googleapis.com/auth/drive.file')
    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, scopes)
    creds = flow.run_local_server(port=0)
    args.token.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.token, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(creds.to_json())
    if os.name == 'posix':
        args.token.chmod(0o600)
    print('사진 연결 인증 완료. 토큰은 개인 서버에만 보관하세요.')


if __name__ == '__main__':
    main()
