import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class CloudSetupTests(unittest.TestCase):
    def run_setup(self, root, valid):
        (root / 'scripts').mkdir()
        script = Path(__file__).resolve().parents[1] / 'scripts/cloud-setup.sh'
        shutil.copy2(script, root / 'scripts/cloud-setup.sh')
        python = root / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/bash\n'
                          'if [[ "$1" == "-c" ]]; then exit ' + ('0' if valid else '1') + '; fi\n'
                          'printf "%s\\n" "$*" >> "$PROJECT_HUB_SETUP_TEST_LOG"\n')
        python.chmod(0o755)
        # Reproduce the Pi shell: an existing venv, but neither python3.11 nor uv
        # on PATH. dirname is the only external bootstrap command needed here.
        path = root / 'path'
        path.mkdir()
        (path / 'dirname').symlink_to(shutil.which('dirname'))
        log = root / 'commands.txt'
        env = dict(os.environ, PATH=str(path), PROJECT_HUB_SETUP_TEST_LOG=str(log))
        result = subprocess.run(['/bin/bash', str(root / 'scripts/cloud-setup.sh')],
                                env=env, text=True, capture_output=True)
        return result, log

    @unittest.skipUnless(os.name == 'posix', 'Linux/Pi setup script')
    def test_existing_python311_works_without_system_python_or_uv(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, log = self.run_setup(Path(tmp), valid=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(),
                             '-m pip install -r requirements.txt -r organizer/requirements-web.txt\n')
            self.assertIn('Ready.', result.stdout)

    @unittest.skipUnless(os.name == 'posix', 'Linux/Pi setup script')
    def test_wrong_existing_python_does_not_install_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, log = self.run_setup(Path(tmp), valid=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Python 3.11', result.stderr)
            self.assertFalse(log.exists())


if __name__ == '__main__':
    unittest.main()
