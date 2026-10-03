"""Consistent Project Hub backups, verified Drive upload, and isolated restoration."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
KIND = 'project-hub-backup-v1'
SCOPES = ['https://www.googleapis.com/auth/drive.file']


class BackupError(Exception):
    pass


def file_hash(path, algorithm='sha256'):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def relative_storage(root, path):
    resolved = Path(path).resolve()
    storage = (root / 'storage').resolve()
    if not resolved.is_relative_to(storage) or Path(path).is_symlink():
        raise BackupError('Attachment path is outside project storage or is a symlink.')
    return resolved.relative_to(root.resolve()).as_posix()


def build_snapshot(root, directory, instance):
    """SQLite online backup establishes the point in time; JSON is regenerated from it."""
    root, directory = Path(root).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    name = f'project-hub-{stamp}-{uuid.uuid4().hex[:8]}.zip'
    final = directory / name
    partial = directory / (name + '.part')
    manifest = {'format': 1, 'kind': KIND, 'instance': instance, 'source_root': str(root),
                'created_at': datetime.now(timezone.utc).isoformat(), 'files': {}, 'records': {}}
    try:
        with tempfile.TemporaryDirectory(prefix='snapshot-', dir=directory) as temporary:
            database_path = Path(temporary) / 'project_hub.db'
            with closing(sqlite3.connect((root / 'project_hub.db').as_uri() + '?mode=ro', uri=True)) as source, \
                    closing(sqlite3.connect(database_path)) as target:
                source.backup(target, pages=256, sleep=0.05)
            with closing(sqlite3.connect(database_path)) as connection:
                if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise BackupError('SQLite integrity check failed.')
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute('SELECT * FROM files ORDER BY id')]
            estimated = database_path.stat().st_size
            for row in rows:
                relative = relative_storage(root, row['local_path'])
                if not (root / relative).is_file():
                    raise BackupError(f"Attachment missing for record {row['id']}.")
                estimated += (root / relative).stat().st_size
            if shutil.disk_usage(directory).free < estimated + 100 * 1024 * 1024:
                raise BackupError('Not enough free space to create a backup.')

            with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                def add_file(path, relative):
                    if relative in manifest['files']:
                        return
                    if Path(path).is_symlink():
                        raise BackupError('Symlink sources are not supported.')
                    expected = file_hash(path)
                    archive.write(path, relative)
                    manifest['files'][relative] = expected

                add_file(database_path, 'project_hub.db')
                for row in rows:
                    attachment = relative_storage(root, row['local_path'])
                    add_file(root / attachment, attachment)
                    if manifest['files'][attachment] != row['sha256']:
                        raise BackupError(f"Attachment hash differs for record {row['id']}.")
                    metadata = (f"storage/{row['category']}/_metadata/backup-record-{row['id']}.json")
                    if row.get('metadata_path'):
                        metadata = relative_storage(root, row['metadata_path'])
                    if metadata in manifest['files']:
                        raise BackupError('Duplicate metadata path in database.')
                    content = json.dumps(dict(row, drive_upload_status=row['upload_status']),
                                         ensure_ascii=False, indent=2).encode('utf-8')
                    archive.writestr(metadata, content)
                    manifest['files'][metadata] = hashlib.sha256(content).hexdigest()
                    manifest['records'][str(row['id'])] = {'attachment': attachment, 'metadata': metadata}
                # Explicit code/config allowlist: no credentials, virtualenv, logs or previous backups.
                for relative in ('requirements.txt', 'README.md', '.gitignore', '.python-version',
                                 '.env.example', 'config/channels.json'):
                    if (root / relative).is_file():
                        add_file(root / relative, relative)
                for folder in ('bot', 'tests', 'deploy/raspberry-pi'):
                    for path in sorted((root / folder).glob('*')):
                        if path.is_file() and path.suffix in ('.py', '.sh', '.in', '.md'):
                            add_file(path, path.relative_to(root).as_posix())
                archive.writestr('backup-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_archive(partial)
        partial.replace(final)
        return final
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def safe_member(name):
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and '..' not in path.parts and '\\' not in name \
        and ':' not in path.parts[0] and str(path) == name


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(not safe_member(name) for name in names):
            raise BackupError('Unsafe or duplicate archive path.')
        if any(stat.S_ISLNK(item.external_attr >> 16) for item in archive.infolist()):
            raise BackupError('Symlinks are not allowed in backups.')
        if archive.getinfo('backup-manifest.json').file_size > 16 * 1024 * 1024:
            raise BackupError('Backup manifest is too large.')
        manifest = json.loads(archive.read('backup-manifest.json'))
        if manifest.get('format') != 1 or manifest.get('kind') != KIND:
            raise BackupError('Unsupported backup format.')
        if set(names) != set(manifest['files']) | {'backup-manifest.json'}:
            raise BackupError('Backup entry list does not match manifest.')
        for name, expected in manifest['files'].items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise BackupError('Backup checksum mismatch.')
        for record in manifest['records'].values():
            if any(record[key] not in manifest['files'] or not record[key].startswith('storage/')
                   for key in ('attachment', 'metadata')):
                raise BackupError('Invalid record mapping in backup.')
        return manifest


def restore_archive(archive_path, destination):
    """Restore only to a new directory; never overwrite or start the live collector."""
    manifest = verify_archive(archive_path)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError('Restore destination must not already exist.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    destination = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        needed = sum(item.file_size for item in archive.infolist())
        if needed + 100 * 1024 * 1024 > shutil.disk_usage(destination).free:
            raise BackupError('Not enough disk space for restore.')
        for name in manifest['files']:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
    review_count = 0
    with closing(sqlite3.connect(destination / 'project_hub.db')) as connection, connection:
        connection.row_factory = sqlite3.Row
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise BackupError('Restored SQLite integrity check failed.')
        rows = [dict(row) for row in connection.execute('SELECT * FROM files')]
        if {str(row['id']) for row in rows} != set(manifest['records']):
            raise BackupError('Restored record list does not match manifest.')
        for row in rows:
            paths = manifest['records'][str(row['id'])]
            local = destination / paths['attachment']
            metadata = destination / paths['metadata']
            if file_hash(local) != row['sha256']:
                raise BackupError('Restored attachment does not match its database record.')
            # The upload may have completed after the backup. No reserved ID means unsafe to retry.
            needs_review = row['upload_status'] in ('pending', 'failed') and not row.get('planned_drive_id')
            status = 'needs_review' if needs_review else row['upload_status']
            review_count += int(needs_review)
            connection.execute('UPDATE files SET local_path=?,metadata_path=?,upload_status=? WHERE id=?',
                               (str(local), str(metadata), status, row['id']))
            row.update(local_path=str(local), metadata_path=str(metadata), upload_status=status,
                       drive_upload_status=status)
            save_json(metadata, row)
    return {'records_restored': len(rows), 'pending_uploads_requiring_review': review_count,
            'destination': str(destination), 'credentials_included': False}


def make_drive(root):
    import httplib2
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    credentials = Credentials.from_authorized_user_file(root / 'token.json', SCOPES)
    if not credentials.valid:
        credentials.refresh(Request())
    # Keep refreshed access credentials in memory to avoid racing the collector's token.json writes.
    return build('drive', 'v3', http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=60)),
                 cache_discovery=False)


def get_backup_folder(service, instance):
    query = ("mimeType='application/vnd.google-apps.folder' and trashed=false "
             f"and appProperties has {{key='backup_instance' and value='{instance}'}} "
             "and appProperties has {key='backup_role' and value='folder'}")
    folders = service.files().list(q=query, fields='files(id)', pageSize=100).execute(num_retries=3)['files']
    if folders:
        return folders[0]['id']
    # A separate My Drive folder avoids inheriting sharing from the collected-file project folder.
    body = {'name': 'Project Hub Backups', 'mimeType': 'application/vnd.google-apps.folder',
            'appProperties': {'backup_instance': instance, 'backup_role': 'folder'}}
    return service.files().create(body=body, fields='id').execute(num_retries=3)['id']


def remote_metadata(service, file_id):
    from googleapiclient.errors import HttpError
    try:
        return service.files().get(fileId=file_id,
            fields='id,name,size,md5Checksum,trashed,appProperties,parents,webViewLink').execute(num_retries=3)
    except HttpError as error:
        if error.resp.status == 404:
            return None
        raise


def check_remote(info, archive, instance):
    if not info or info.get('trashed') or int(info.get('size', -1)) != archive.stat().st_size \
            or info.get('md5Checksum') != file_hash(archive, 'md5') \
            or info.get('appProperties', {}).get('backup_instance') != instance:
        raise BackupError('Drive backup verification failed.')


def upload_snapshot(service, archive, receipt_path, folder, instance):
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt['sha256'] != file_hash(archive):
            raise BackupError('Pending backup changed before upload.')
    else:
        receipt = {'archive': archive.name, 'sha256': file_hash(archive), 'uploaded': False,
                   'drive_id': service.files().generateIds(count=1, space='drive', type='files')
                   .execute(num_retries=3)['ids'][0]}
        save_json(receipt_path, receipt)
    info = remote_metadata(service, receipt['drive_id'])
    if info is None:
        media = MediaFileUpload(str(archive), mimetype='application/zip', resumable=True,
                                chunksize=8 * 1024 * 1024)
        try:
            request = service.files().create(body={
                'id': receipt['drive_id'], 'name': archive.name, 'parents': [folder],
                'appProperties': {'backup_kind': KIND, 'backup_instance': instance,
                                  'backup_sha256': receipt['sha256']},
            }, media_body=media, fields='id')
            response = None
            while response is None:
                _, response = request.next_chunk(num_retries=3)
        except HttpError as error:
            if error.resp.status != 409:
                raise
        finally:
            media.stream().close()
        info = remote_metadata(service, receipt['drive_id'])
    check_remote(info, archive, instance)
    receipt.update(uploaded=True, verified_at=datetime.now(timezone.utc).isoformat())
    save_json(receipt_path, receipt)
    return receipt


def retention_candidates(items, instance, folder, keep):
    eligible = [item for item in items if not item.get('trashed') and folder in item.get('parents', [])
                and item.get('appProperties', {}).get('backup_kind') == KIND
                and item.get('appProperties', {}).get('backup_instance') == instance]
    return sorted(eligible, key=lambda item: (item['createdTime'], item['id']), reverse=True)[keep:]


def prune(service, directory, folder, instance, cloud_keep=14, local_keep=7):
    items, page = [], None
    while True:
        response = service.files().list(q=f"'{folder}' in parents and trashed=false",
            fields='nextPageToken,files(id,createdTime,trashed,parents,appProperties)',
            pageSize=1000, pageToken=page).execute(num_retries=3)
        items.extend(response.get('files', []))
        page = response.get('nextPageToken')
        if not page:
            break
    for item in retention_candidates(items, instance, folder, cloud_keep):
        service.files().update(fileId=item['id'], body={'trashed': True}, fields='id').execute(num_retries=3)
    completed = []
    for receipt_path in directory.glob('project-hub-*.zip.upload.json'):
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('uploaded'):
            completed.append((receipt_path, receipt))
    completed.sort(key=lambda pair: pair[1]['archive'], reverse=True)
    for receipt_path, receipt in completed[local_keep:]:
        archive = directory / receipt['archive']
        if archive.parent != directory or not archive.name.startswith('project-hub-'):
            raise BackupError('Invalid local backup filename.')
        archive.unlink(missing_ok=True)
        receipt_path.unlink()


def run_backup(root):
    import fcntl
    root = Path(root).resolve()
    directory = root / 'logs' / 'backups'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'backup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('BACKUP_ALREADY_RUNNING')
            return
        instance_path = directory / 'instance.json'
        if not instance_path.exists():
            save_json(instance_path, {'instance': uuid.uuid4().hex})
        instance = json.loads(instance_path.read_text())['instance']
        if not instance.isalnum():
            raise BackupError('Invalid backup instance identifier.')
        service = make_drive(root)
        try:
            folder = get_backup_folder(service, instance)
            # Resume failed uploads using their already reserved IDs before creating the next snapshot.
            for pending_archive in sorted(directory.glob('project-hub-*.zip')):
                receipt_path = Path(str(pending_archive) + '.upload.json')
                receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
                if not receipt.get('uploaded'):
                    if verify_archive(pending_archive)['instance'] != instance:
                        raise BackupError('Pending archive belongs to another backup instance.')
                    upload_snapshot(service, pending_archive, receipt_path, folder, instance)
            archive = build_snapshot(root, directory, instance)
            receipt = upload_snapshot(service, archive, Path(str(archive) + '.upload.json'), folder, instance)
            result = {'completed_at': datetime.now(timezone.utc).isoformat(),
                      'archive': archive.name, 'drive_id': receipt['drive_id'], 'folder_id': folder,
                      'records': len(verify_archive(archive)['records']),
                      'bytes': archive.stat().st_size, 'sha256': receipt['sha256']}
            save_json(directory / 'last-success.json', result)
            prune(service, directory, folder, instance)
            print('BACKUP_SUCCESS ' + json.dumps(result))
        finally:
            service.close()


def download_latest(root, destination):
    from googleapiclient.http import MediaIoBaseDownload
    directory = root / 'logs' / 'backups'
    latest = json.loads((directory / 'last-success.json').read_text())
    destination = Path(destination)
    if destination.exists():
        raise BackupError('Download destination already exists.')
    service = make_drive(root)
    try:
        with destination.open('xb') as output:
            downloader = MediaIoBaseDownload(output, service.files().get_media(fileId=latest['drive_id']))
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)
        if file_hash(destination) != latest['sha256']:
            raise BackupError('Downloaded backup hash mismatch.')
        verify_archive(destination)
        print('DRIVE_DOWNLOAD_VERIFIED')
    finally:
        service.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('run')
    verify = commands.add_parser('verify')
    verify.add_argument('archive', type=Path)
    restore = commands.add_parser('restore')
    restore.add_argument('archive', type=Path)
    restore.add_argument('--destination', required=True, type=Path)
    download = commands.add_parser('download-latest')
    download.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'run':
        run_backup(ROOT)
    elif args.command == 'verify':
        print(json.dumps({'verified': True, 'records': len(verify_archive(args.archive)['records'])}))
    elif args.command == 'restore':
        print(json.dumps(restore_archive(args.archive, args.destination)))
    else:
        download_latest(ROOT, args.destination)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        print(f'BACKUP_FAILED: {message}', file=sys.stderr)
        sys.exit(1)
