"""A standalone, diskless archive for one Discord text channel."""
import asyncio
from datetime import datetime, timezone
import io
import logging
import mimetypes
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import discord
import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

LOG = logging.getLogger('drive-archive')
KST = ZoneInfo('Asia/Seoul')
FOLDER = 'application/vnd.google-apps.folder'
MIB = 1024 * 1024


class SetupError(RuntimeError):
    """Configuration error with a safe message for the operator."""


def setting(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise SetupError(f'{name} 설정이 필요합니다.')
    return value


def safe_name(name):
    return re.sub(r'[\x00-\x1f/\\]', '_', name).strip()[:150] or '첨부파일'


def attachment_type(attachment):
    mime = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
    mime = (mime or 'application/octet-stream').split(';', 1)[0]
    prefix = mime.partition('/')[0]
    if prefix in {'image', 'video', 'audio'}:
        return {'image': '이미지', 'video': '동영상', 'audio': '음성'}[prefix], mime
    suffix = Path(attachment.filename).suffix.lower()
    if suffix in {'.zip', '.7z', '.rar', '.tar', '.gz'}:
        return '압축파일', mime
    if prefix == 'text' or suffix in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.hwp', '.hwpx', '.csv', '.odt'}:
        return '문서', mime
    return '기타', mime


class DriveArchive:
    def __init__(self, token_path, root):
        self.token_path, self.root = token_path, root
        self.files = None
        self.drive_id = None
        self.folders = {}

    def connect(self):
        if self.files is not None:
            return
        creds = Credentials.from_authorized_user_file(self.token_path)
        if not creds.refresh_token:
            raise SetupError('Drive 토큰에 자동 갱신용 refresh_token이 필요합니다.')
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=45))
        files = build('drive', 'v3', http=http, cache_discovery=False).files()
        folder = files.get(fileId=self.root, supportsAllDrives=True,
                           fields='mimeType,trashed,driveId,capabilities(canAddChildren)').execute()
        if folder.get('trashed') or folder.get('mimeType') != FOLDER or not folder.get('capabilities', {}).get('canAddChildren'):
            raise SetupError('지정한 Drive 폴더에 파일을 추가할 권한이 필요합니다.')
        self.drive_id = folder.get('driveId')
        self.files = files

    def lookup(self, parent, key):
        # Parent IDs and archive keys are validated IDs or internally generated ASCII.
        query = f"trashed = false and '{parent}' in parents and appProperties has {{ key='archive_key' and value='{key}' }}"
        params = dict(q=query, fields='nextPageToken,files(id,mimeType,appProperties)',
                      supportsAllDrives=True, includeItemsFromAllDrives=True, pageSize=2)
        if self.drive_id:
            params.update(corpora='drive', driveId=self.drive_id)
        while True:
            page = self.files.list(**params).execute()
            found = page.get('files', [])
            if found:
                return found[0]
            if not page.get('nextPageToken'):
                return None
            params['pageToken'] = page['nextPageToken']

    def folder(self, parent, key, name, cursor=None):
        if cursor is None and (parent, key) in self.folders:
            return {'id': self.folders[parent, key]}
        item = self.lookup(parent, key)
        if item is None:
            props = {'archive_key': key}
            if cursor is not None:
                props['cursor'] = str(cursor)
            item = self.files.create(body={'name': name, 'parents': [parent], 'mimeType': FOLDER,
                                           'appProperties': props}, supportsAllDrives=True,
                                     fields='id,mimeType,appProperties').execute()
        if item['mimeType'] != FOLDER:
            raise SetupError('Drive 보관 경로를 확인해 주세요.')
        self.folders[parent, key] = item['id']
        return item

    def start(self, channel, initial_cursor):
        self.connect()
        key = f'channel-{channel.id}'
        item = self.folder(self.root, key, f'{safe_name(channel.name)} [{channel.id}]', initial_cursor)
        return item['id'], int(item['appProperties']['cursor'])

    def month(self, parent, created_at):
        month = created_at.astimezone(KST).strftime('%Y-%m')
        return self.folder(parent, 'month-' + month, month)['id']

    def put(self, parent, key, filename, mime, data):
        with io.BytesIO(data) as stream:
            media = MediaIoBaseUpload(stream, mimetype=mime, chunksize=4 * MIB, resumable=True)
            request = self.files.create(body={'name': filename, 'parents': [parent],
                                              'appProperties': {'archive_key': key}},
                                        supportsAllDrives=True, media_body=media, fields='id')
            result = None
            while result is None:
                _, result = request.next_chunk(num_retries=2)
            return result['id']

    def checkpoint(self, folder, channel_id, message_id):
        self.files.update(fileId=folder, supportsAllDrives=True,
                          body={'appProperties': {'archive_key': f'channel-{channel_id}',
                                                  'cursor': str(message_id)}}, fields='id').execute()


async def archive_message(drive, folder, message, max_bytes):
    if not message.content and not message.attachments:
        return
    month = await asyncio.to_thread(drive.month, folder, message.created_at)
    notes = await asyncio.to_thread(drive.folder, month, 'messages', '내용')
    key = f'message-{message.id}'
    if await asyncio.to_thread(drive.lookup, notes['id'], key):
        return
    links = []
    for attachment in message.attachments:
        name = safe_name(attachment.filename)
        if attachment.size > max_bytes:
            links.append(f'- {name} — 용량 한도 초과, 원문에서 확인')
            LOG.warning('첨부파일 용량 한도 초과: 메시지 %s, 첨부 %s', message.id, attachment.id)
            continue
        label, mime = attachment_type(attachment)
        folder_key = {'이미지': 'images', '동영상': 'videos', '음성': 'audio',
                      '압축파일': 'archives', '문서': 'documents', '기타': 'other'}[label]
        target = await asyncio.to_thread(drive.folder, month, folder_key, label)
        attachment_key = f'attachment-{attachment.id}'
        item = await asyncio.to_thread(drive.lookup, target['id'], attachment_key)
        if item:
            file_id = item['id']
        else:
            data = await attachment.read()
            if len(data) != attachment.size or len(data) > max_bytes:
                raise RuntimeError('첨부파일 크기 불일치')
            file_id = await asyncio.to_thread(drive.put, target['id'], attachment_key,
                                              f'{attachment.id}_{name}', mime, data)
            del data
        links.append(f'- {name}: https://drive.google.com/open?id={file_id}')
    date = message.created_at.astimezone(KST).isoformat(timespec='seconds')
    text = f'작성 시각: {date}\n작성자: {message.author}\n원문: {message.jump_url}\n\n{message.content}\n'
    if links:
        text += '\n첨부파일\n' + '\n'.join(links) + '\n'
    await asyncio.to_thread(drive.put, notes['id'], key, f'{message.id}.txt',
                            'text/plain', text.encode('utf-8'))


class ArchiveClient(discord.Client):
    def __init__(self, channel_id, drive, max_bytes):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.channel_id, self.drive, self.max_bytes = channel_id, drive, max_bytes
        self.initial_cursor = discord.utils.time_snowflake(datetime.now(timezone.utc), high=True)
        self.worker = None

    async def setup_hook(self):
        self.worker = asyncio.create_task(self.collect())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().close()

    async def collect(self):
        channel, folder, cursor = None, None, None
        while not self.is_closed():
            await self.wait_until_ready()
            try:
                if channel is None:
                    candidate = await self.fetch_channel(self.channel_id)
                    if not isinstance(candidate, discord.TextChannel):
                        raise SetupError('일반 디스코드 텍스트 채널 ID를 지정해 주세요.')
                    channel = candidate
                if folder is None:
                    folder, cursor = await asyncio.to_thread(self.drive.start, channel, self.initial_cursor)
                    LOG.info('지정 채널의 Drive 보관을 시작했습니다.')
                count = 0
                async for message in channel.history(limit=50, after=discord.Object(id=cursor), oldest_first=True):
                    await archive_message(self.drive, folder, message, self.max_bytes)
                    await asyncio.to_thread(self.drive.checkpoint, folder, channel.id, message.id)
                    cursor = message.id
                    count += 1
                if count:
                    LOG.info('처리 완료: 메시지 %d건', count)
                await asyncio.sleep(2 if count == 50 else 30)
            except SetupError as exc:
                LOG.error('%s', exc)
                await asyncio.sleep(60)
            except Exception as exc:
                # API exceptions can include request URLs or data. Do not log those.
                LOG.warning('보관 중 오류(%s). 마지막 완료 위치부터 다시 시도합니다.', type(exc).__name__)
                await asyncio.sleep(60)


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    try:
        channel = setting('ARCHIVE_CHANNEL_ID')
        root = setting('ARCHIVE_DRIVE_FOLDER_ID')
        limit = os.environ.get('ARCHIVE_MAX_ATTACHMENT_MIB', '64')
        if not re.fullmatch(r'[1-9][0-9]{15,20}', channel) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', root):
            raise SetupError('채널 ID와 Drive 폴더 ID 형식을 확인해 주세요.')
        if not re.fullmatch(r'[1-9][0-9]{0,2}', limit) or not 1 <= int(limit) <= 128:
            raise SetupError('첨부파일 메모리 한도는 1~128 MiB로 설정해 주세요.')
        token_path = Path(setting('ARCHIVE_DRIVE_TOKEN_FILE')).expanduser()
        if not token_path.is_file():
            raise SetupError('Drive 인증 파일 경로를 확인해 주세요.')
        client = ArchiveClient(int(channel), DriveArchive(token_path, root), int(limit) * MIB)
        client.run(setting('ARCHIVE_DISCORD_TOKEN'), log_handler=None)
    except SetupError as exc:
        LOG.error('%s', exc)
        raise SystemExit(1) from None
    except Exception as exc:
        LOG.error('봇 시작 실패(%s). 인증 및 설정을 확인해 주세요.', type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
