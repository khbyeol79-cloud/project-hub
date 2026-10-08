import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location('photo_deploy', Path(__file__).with_name('photo_deploy.py'))
photos = importlib.util.module_from_spec(spec)
spec.loader.exec_module(photos)


class PhotoDeploymentTests(unittest.TestCase):
    def test_folder_setting_preserves_unrelated_values_and_replaces_duplicates(self):
        original = '# local settings\nSECRET="keep me"\nexport PROJECT_HUB_PHOTO_FOLDER_ID=old\nPROJECT_HUB_PHOTO_FOLDER_ID=older\n'
        result = photos.folder_setting(original, 'chosen-folder')
        self.assertIn('SECRET="keep me"', result)
        self.assertNotIn('=old', result)
        self.assertEqual(result.count('PROJECT_HUB_PHOTO_FOLDER_ID=chosen-folder'), 2)
        self.assertEqual(photos.folder_setting(result, 'chosen-folder'), result)
        self.assertEqual(photos.folder_setting('OTHER=yes', 'chosen-folder'),
                         'OTHER=yes\nPROJECT_HUB_PHOTO_FOLDER_ID=chosen-folder\n')

    def setup_fixture(self, tmp):
        home = Path(tmp)
        root, checkout = home / 'live', home / 'checkout'
        for base, value in [(root, 'old'), (checkout, 'new')]:
            for name in ['bot/discord_bot.py', 'organizer/web.py']:
                path = base / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
        config = home / '.config/project-hub/library.env'
        config.parent.mkdir(parents=True)
        config.write_text('PRIVATE_SETTING=preserved\n')
        (root / 'token.json').write_text('original bot token')
        (root / 'project_hub.db').write_bytes(b'live data')
        return home, root, checkout, config

    def test_auth_missing_leaves_packages_configuration_and_services_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root, checkout, config = self.setup_fixture(tmp)
            with patch.object(photos, 'validate_connection', side_effect=photos.SetupError('consent needed')), \
                 patch.object(photos, 'command') as command:
                with self.assertRaises(photos.SetupError):
                    photos.prepare_and_apply(checkout, root, 'chosen-folder', home)
            command.assert_not_called()
            self.assertEqual(config.read_text(), 'PRIVATE_SETTING=preserved\n')
            self.assertEqual((root / 'token.json').read_text(), 'original bot token')
            self.assertEqual((root / 'project_hub.db').read_bytes(), b'live data')

    def test_packages_failed_does_not_change_configuration_or_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root, checkout, config = self.setup_fixture(tmp)
            def command(*args, **kwargs):
                if 'install' in args:
                    raise subprocess.CalledProcessError(1, args)
            with patch.object(photos, 'validate_connection'), \
                 patch.object(photos, 'command', side_effect=command) as calls:
                with self.assertRaises(subprocess.CalledProcessError):
                    photos.prepare_and_apply(checkout, root, 'chosen-folder', home)
            self.assertEqual(config.read_text(), 'PRIVATE_SETTING=preserved\n')
            self.assertFalse(any('--apply' in c.args for c in calls.call_args_list))

    def test_apply_failed_restores_configuration_and_reloads_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root, checkout, config = self.setup_fixture(tmp)
            def command(*args, **kwargs):
                if '--apply' in args:
                    self.assertIn('PROJECT_HUB_PHOTO_FOLDER_ID=chosen-folder', config.read_text())
                    raise subprocess.CalledProcessError(1, args)
            with patch.object(photos, 'validate_connection'), \
                 patch.object(photos, 'command', side_effect=command) as calls, \
                 patch.object(photos.deploy, 'run', return_value='bot/discord_bot.py\0organizer/web.py\0'):
                with self.assertRaises(subprocess.CalledProcessError):
                    photos.prepare_and_apply(checkout, root, 'chosen-folder', home)
            self.assertEqual(config.read_text(), 'PRIVATE_SETTING=preserved\n')
            calls.assert_any_call('sudo', 'systemctl', 'restart', *photos.deploy.SERVICES)
            backups = list((home / '.local/state/project-hub-deploy').glob('photos-*/library.env'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'PRIVATE_SETTING=preserved\n')

    def test_unchanged_code_still_reloads_a_replaced_photo_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, root, checkout, config = self.setup_fixture(tmp)
            config.write_text('PROJECT_HUB_PHOTO_FOLDER_ID=chosen-folder\n')
            with patch.object(photos, 'validate_connection'), \
                 patch.object(photos, 'command') as calls, \
                 patch.object(photos.deploy, 'run', return_value='bot/discord_bot.py\0organizer/web.py\0'), \
                 patch.object(photos.deploy, 'same_code', return_value=True), \
                 patch.object(photos.deploy, 'healthy') as healthy:
                photos.prepare_and_apply(checkout, root, 'chosen-folder', home)
            calls.assert_any_call('sudo', 'systemctl', 'restart', *photos.deploy.SERVICES)
            healthy.assert_called_once()
            self.assertEqual((root / 'token.json').read_text(), 'original bot token')

    def test_connection_failure_is_a_clear_error_without_token_output(self):
        with patch.object(photos, 'command', side_effect=subprocess.CalledProcessError(2, 'auth')):
            with self.assertRaisesRegex(photos.SetupError, '운영 설정과 서비스는 변경하지 않았습니다'):
                photos.validate_connection(Path('/live'), Path('/private/library.env'), 'chosen-folder')


if __name__ == '__main__':
    unittest.main()
