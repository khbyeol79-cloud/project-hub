import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('phone_deploy', Path(__file__).with_name('phone_deploy.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def test_only_application_code_is_allowed(self):
        for name in ['bot/discord_bot.py', 'organizer/web.py', 'organizer/static/app.js']:
            self.assertTrue(deploy.allowed(name), name)
        for name in ['.env', 'credentials.json', 'token.json', 'config/channels.json',
                     'storage/file.py', 'bot/../.env', '/bot/x.py', 'bot\\x.py',
                     'organizer/static/key.json', 'bot/.venv/test.py']:
            self.assertFalse(deploy.allowed(name), name)

    def test_rollback_restores_old_removes_new_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            backup = Path(tmp) / 'backup'
            (root / 'bot').mkdir(parents=True)
            (backup / 'code/bot').mkdir(parents=True)
            (root / 'bot/old.py').write_bytes(b'new')
            (root / 'bot/new.py').write_bytes(b'added')
            (root / '.env').write_bytes(b'local configuration')
            (root / 'project_hub.db').write_bytes(b'newest data')
            (backup / 'code/bot/old.py').write_bytes(b'old')
            manifest = {'files': {'bot/old.py': hashlib.sha256(b'old').hexdigest(),
                                  'bot/new.py': None}}
            deploy.restore(root, backup, manifest)
            self.assertEqual((root / 'bot/old.py').read_bytes(), b'old')
            self.assertFalse((root / 'bot/new.py').exists())
            self.assertEqual((root / '.env').read_bytes(), b'local configuration')
            self.assertEqual((root / 'project_hub.db').read_bytes(), b'newest data')

    def test_corrupt_backup_rejected_before_any_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'bot').mkdir()
            (root / 'bot/new.py').write_bytes(b'keep')
            manifest = {'files': {'bot/new.py': None, 'bot/old.py': 'wrong'}}
            with self.assertRaises(ValueError):
                deploy.restore(root, root / 'backup', manifest)
            self.assertEqual((root / 'bot/new.py').read_bytes(), b'keep')

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            try:
                (root / 'bot').symlink_to(Path(tmp), target_is_directory=True)
            except OSError:
                self.skipTest('Symlinks unavailable for this user')
            with self.assertRaises(ValueError):
                deploy.safe_path(root, 'bot/external.py')
