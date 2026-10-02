import importlib.util
import hashlib
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('phone_deploy', Path(__file__).with_name('phone_deploy.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def test_line_endings_do_not_trigger_code_deployment(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / 'a.py', Path(tmp) / 'b.py'
            a.write_bytes(b'x = 1\r\n')
            b.write_bytes(b'x = 1\n')
            self.assertTrue(deploy.same_code(a, b))
            b.write_bytes(b'x = 2\n')
            self.assertFalse(deploy.same_code(a, b))

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

    @unittest.skipUnless(os.name == 'posix', 'Pi deployment transaction')
    def test_failed_health_or_stop_recovers_without_losing_configuration(self):
        for failure in ('health', 'stop', 'concurrent'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                root, checkout = home / 'live', home / 'checkout'
                for base, content in [(root, 'old'), (checkout, 'new')]:
                    for name in ['bot/discord_bot.py', 'organizer/web.py']:
                        path = base / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content)
                (root / '.env').write_text('local-only')
                stopped = []

                def fake_run(*args, **kwargs):
                    if args[:2] == ('git', 'rev-parse'):
                        return 'a' * 40
                    if args[:2] == ('git', 'status'):
                        return ''
                    if args[:2] == ('git', 'ls-files'):
                        return 'bot/discord_bot.py\0organizer/web.py\0'
                    if args[:3] == ('sudo', 'systemctl', 'stop'):
                        stopped.append(True)
                        if len(stopped) == 1:
                            if failure == 'stop':
                                raise RuntimeError('stop failed')
                            if failure == 'concurrent':
                                (root / 'bot/discord_bot.py').write_text('other edit')
                    return ''

                with patch.object(deploy, '__file__', str(checkout / 'deploy/raspberry-pi/phone_deploy.py')), \
                     patch.object(Path, 'home', return_value=home), \
                     patch.object(deploy, 'run', side_effect=fake_run) as calls, \
                     patch.object(deploy, 'healthy', side_effect=RuntimeError('health failed')), \
                     patch.object(deploy.sys, 'argv', ['phone_deploy', '--root', str(root), '--apply']):
                    with self.assertRaises(RuntimeError):
                        deploy.main()
                expected = 'other edit' if failure == 'concurrent' else 'old'
                self.assertEqual((root / 'bot/discord_bot.py').read_text(), expected)
                self.assertEqual((root / 'organizer/web.py').read_text(), 'old')
                self.assertEqual((root / '.env').read_text(), 'local-only')
                calls.assert_any_call('sudo', 'systemctl', 'start', *deploy.SERVICES)
