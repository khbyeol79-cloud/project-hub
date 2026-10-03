import hashlib
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock
import zipfile

SPEC = importlib.util.spec_from_file_location('backup', Path(__file__).with_name('backup.py'))
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        (self.root / 'storage' / 'plc' / '_metadata').mkdir(parents=True)
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'channels.json').write_text('{"channels":{"123":"plc"}}')
        (self.root / 'requirements.txt').write_text('example==1.0\n')
        for name in ('.env', 'token.json', 'credentials.json'):
            (self.root / name).write_text('private-credential-sentinel')
        with closing(sqlite3.connect(self.root / 'project_hub.db')) as connection, connection:
            connection.execute('CREATE TABLE files (id INTEGER PRIMARY KEY, category TEXT, local_path TEXT, '
                'sha256 TEXT, metadata_path TEXT, upload_status TEXT, planned_drive_id TEXT)')
        self.add_row(1, 'uploaded')
        self.output = self.root / 'logs' / 'backups'

    def add_row(self, record_id, status, planned=None):
        path = self.root / 'storage' / 'plc' / f'{record_id}.txt'
        content = f'attachment {record_id}'.encode()
        path.write_bytes(content)
        metadata = path.parent / '_metadata' / f'{record_id}.json'
        metadata.write_text('{"stale":true}')
        with closing(sqlite3.connect(self.root / 'project_hub.db')) as connection, connection:
            connection.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?)',
                (record_id, 'plc', str(path), hashlib.sha256(content).hexdigest(), str(metadata), status, planned))

    def snapshot(self):
        return backup.build_snapshot(self.root, self.output, 'testinstance')

    def test_snapshot_excludes_credentials_and_regenerates_consistent_metadata(self):
        archive = self.snapshot()
        manifest = backup.verify_archive(archive)
        self.assertEqual(len(manifest['records']), 1)
        with zipfile.ZipFile(archive) as content:
            for name in ('.env', 'credentials.json', 'token.json'):
                self.assertNotIn(name, content.namelist())
            self.assertNotIn(b'private-credential-sentinel', b''.join(content.read(name) for name in content.namelist()))
            metadata = json.loads(content.read(manifest['records']['1']['metadata']))
            self.assertEqual(metadata['upload_status'], 'uploaded')
            self.assertNotIn('stale', metadata)

    def test_online_backup_includes_committed_wal_changes(self):
        connection = sqlite3.connect(self.root / 'project_hub.db')
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("UPDATE files SET upload_status='needs_review' WHERE id=1")
        connection.commit()
        archive = self.snapshot()
        restored = self.base / 'wal-restore'
        backup.restore_archive(archive, restored)
        with closing(sqlite3.connect(restored / 'project_hub.db')) as result:
            self.assertEqual(result.execute('SELECT upload_status FROM files').fetchone()[0], 'needs_review')

    def test_restore_rewrites_paths_without_touching_live_database(self):
        original = backup.file_hash(self.root / 'project_hub.db')
        restored = self.base / 'recovered'
        result = backup.restore_archive(self.snapshot(), restored)
        self.assertEqual(result['records_restored'], 1)
        self.assertEqual(original, backup.file_hash(self.root / 'project_hub.db'))
        with closing(sqlite3.connect(restored / 'project_hub.db')) as connection:
            local, metadata = connection.execute('SELECT local_path,metadata_path FROM files').fetchone()
        self.assertTrue(Path(local).is_relative_to(restored))
        self.assertTrue(Path(local).is_file())
        self.assertEqual(json.loads(Path(metadata).read_text())['local_path'], local)
        self.assertFalse((restored / 'token.json').exists())

    def test_restore_pauses_ambiguous_upload_but_preserves_reserved_id_retry(self):
        self.add_row(2, 'pending')
        self.add_row(3, 'failed', 'reserved-drive-id')
        restored = self.base / 'queue-restore'
        result = backup.restore_archive(self.snapshot(), restored)
        self.assertEqual(result['pending_uploads_requiring_review'], 1)
        with closing(sqlite3.connect(restored / 'project_hub.db')) as connection:
            states = dict(connection.execute('SELECT id,upload_status FROM files'))
        self.assertEqual(states, {1: 'uploaded', 2: 'needs_review', 3: 'failed'})

    def test_restore_refuses_existing_destination(self):
        archive = self.snapshot()
        with self.assertRaises(backup.BackupError):
            backup.restore_archive(archive, self.root)

    def test_changed_attachment_blocks_backup_and_leaves_no_complete_archive(self):
        (self.root / 'storage' / 'plc' / '1.txt').write_text('changed after collection')
        with self.assertRaises(backup.BackupError):
            self.snapshot()
        self.assertEqual(list(self.output.glob('*.zip')), [])
        self.assertEqual(list(self.output.glob('*.part')), [])

    def test_path_outside_storage_is_rejected(self):
        with closing(sqlite3.connect(self.root / 'project_hub.db')) as connection, connection:
            connection.execute('UPDATE files SET local_path=?', (str(self.root / 'token.json'),))
        with self.assertRaises(backup.BackupError):
            self.snapshot()

    def test_corrupt_and_traversal_archives_are_rejected(self):
        original = self.snapshot()
        corrupt = self.base / 'corrupt.zip'
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(corrupt, 'w') as target:
            for name in source.namelist():
                target.writestr(name, b'corrupt' if name.endswith('1.txt') else source.read(name))
        with self.assertRaises(backup.BackupError):
            backup.verify_archive(corrupt)
        traversal = self.base / 'traversal.zip'
        with zipfile.ZipFile(traversal, 'w') as archive:
            archive.writestr('../outside.txt', 'unsafe')
        with self.assertRaises(backup.BackupError):
            backup.restore_archive(traversal, self.base / 'unsafe-restore')
        self.assertFalse((self.base / 'outside.txt').exists())

    def test_retention_only_selects_old_backups_from_this_instance_and_folder(self):
        def item(number, instance='ours', folder='folder', kind=backup.KIND):
            return {'id': str(number), 'createdTime': f'2026-10-{number:02d}', 'parents': [folder],
                    'appProperties': {'backup_instance': instance, 'backup_kind': kind}}
        items = [item(1), item(2), item(3), item(4, instance='other'), item(5, folder='elsewhere'),
                 item(6, kind='user-file')]
        self.assertEqual([entry['id'] for entry in backup.retention_candidates(items, 'ours', 'folder', 2)], ['1'])

    def test_lost_upload_response_reuses_reserved_id_and_does_not_upload_again(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response
        archive = self.snapshot()
        receipt = Path(str(archive) + '.upload.json')
        service = MagicMock()
        files = service.files.return_value
        files.generateIds.return_value.execute.return_value = {'ids': ['reserved-backup-id']}
        not_found = HttpError(Response({'status': '404'}), b'not found')
        unavailable = HttpError(Response({'status': '503'}), b'lost upload response')
        files.get.return_value.execute.side_effect = not_found
        files.create.return_value.next_chunk.side_effect = unavailable
        with self.assertRaises(HttpError):
            backup.upload_snapshot(service, archive, receipt, 'folder', 'testinstance')
        self.assertEqual(json.loads(receipt.read_text())['drive_id'], 'reserved-backup-id')
        files.get.return_value.execute.side_effect = None
        files.get.return_value.execute.return_value = {
            'id': 'reserved-backup-id', 'size': str(archive.stat().st_size),
            'md5Checksum': backup.file_hash(archive, 'md5'),
            'appProperties': {'backup_instance': 'testinstance'},
        }
        result = backup.upload_snapshot(service, archive, receipt, 'folder', 'testinstance')
        self.assertTrue(result['uploaded'])
        self.assertEqual(files.create.call_count, 1)
        self.assertEqual(files.generateIds.call_count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
