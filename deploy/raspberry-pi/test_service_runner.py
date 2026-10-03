"""Readiness regression tests using synthetic credentials only; no network access."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("service_runner", Path(__file__).with_name("service_runner.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bot").mkdir()
        (self.root / "config").mkdir()
        (self.root / ".env").write_text("DISCORD_BOT_TOKEN=fake-discord-secret\n", encoding="utf-8")
        (self.root / "bot" / "discord_bot.py").write_text("def main():\n    pass\n", encoding="utf-8")
        for name in ("drive.py", "database.py"):
            (self.root / "bot" / name).write_text("", encoding="utf-8")
        self.write_json("config/channels.json", {"channels": {"123456789": "plc"}})
        self.write_json("credentials.json", {"installed": {"client_id": "test-client", "client_secret": "fake-client-secret"}})
        self.token = {
            "client_id": "test-client", "client_secret": "fake-client-secret",
            "refresh_token": "fake-refresh-secret", "token": "fake-access-secret",
            "scopes": [runner.DRIVE_SCOPE], "expiry": "2020-01-01T00:00:00Z",
            "token_uri": "https://oauth2.googleapis.com/token"
        }
        self.write_json("token.json", self.token)
        for name in (".env", "credentials.json", "token.json"):
            (self.root / name).chmod(0o600)

    def write_json(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def test_expired_access_token_with_refresh_token_is_ready_offline(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network is forbidden")):
            self.assertEqual(runner.check(self.root), [])

    def test_missing_token_requires_manual_authentication(self):
        (self.root / "token.json").unlink()
        self.assertTrue(any("token.json" in error for error in runner.check(self.root)))

    def test_refresh_token_is_required_even_when_access_token_is_present(self):
        self.token.pop("refresh_token")
        self.write_json("token.json", self.token)
        self.assertTrue(any("refresh_token" in error for error in runner.check(self.root)))

    def test_client_mismatch_and_missing_scope_are_rejected(self):
        self.token.update(client_id="different-client", scopes=[])
        self.write_json("token.json", self.token)
        errors = runner.check(self.root)
        self.assertTrue(any("different OAuth" in error for error in errors))
        self.assertTrue(any("permission" in error for error in errors))

    def test_empty_channels_and_invalid_category_are_rejected(self):
        for channels in ({}, {"123": "not-a-category"}, {"123": ["plc"]}, {"0": "plc"}, {"bad-id": "plc"}):
            with self.subTest(channels=channels):
                self.write_json("config/channels.json", {"channels": channels})
                self.assertTrue(any("channels.json" in error for error in runner.check(self.root)))

    def test_malformed_json_and_expiry_are_reported_without_secret_values(self):
        (self.root / "credentials.json").write_text("not-json-fake-client-secret", encoding="utf-8")
        self.token["expiry"] = "not-a-date"
        self.write_json("token.json", self.token)
        errors = runner.check(self.root)
        self.assertTrue(errors)
        self.assertNotIn("fake-client-secret", "\n".join(errors))
        self.assertNotIn("fake-refresh-secret", "\n".join(errors))

    def test_old_bot_entry_point_is_rejected_without_importing_it(self):
        (self.root / "bot" / "discord_bot.py").write_text("raise Exception('must not execute')\n", encoding="utf-8")
        self.assertTrue(any("main()" in error for error in runner.check(self.root)))

    def test_config_error_exits_78_without_starting_the_bot(self):
        (self.root / "token.json").unlink()
        stderr = io.StringIO()
        with patch.object(runner, "ROOT", self.root), patch("sys.argv", ["service_runner.py"]), \
                patch.object(runner.runpy, "run_module") as run, contextlib.redirect_stderr(stderr):
            self.assertEqual(runner.main(), 78)
            run.assert_not_called()
        self.assertNotIn("fake-discord-secret", stderr.getvalue())

    def test_check_never_runs_the_bot_or_creates_runtime_files(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        with patch.object(runner, "ROOT", self.root), patch("sys.argv", ["service_runner.py", "--check"]), \
                patch.object(runner.runpy, "run_module") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 0)
            run.assert_not_called()
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))

    def run_fake_linux_service(self, error):
        # Exercise exit-code mapping on Windows without starting a real bot or taking a real Linux lock.
        fake_fcntl = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *args: None)
        stderr = io.StringIO()
        with patch.object(runner, "ROOT", self.root), patch("sys.argv", ["service_runner.py"]), \
                patch("sys.platform", "linux"), patch.dict("sys.modules", {"fcntl": fake_fcntl}), \
                patch.object(runner.os, "chdir"), patch.object(runner.os, "umask"), \
                patch.object(runner.runpy, "run_module", side_effect=error), \
                patch.object(runner.sys, "path", list(runner.sys.path)), contextlib.redirect_stderr(stderr):
            result = runner.main()
        self.assertNotIn("sensitive-value", stderr.getvalue())
        return result

    def test_permanent_auth_errors_stop_restarting(self):
        import discord
        from google.auth.exceptions import RefreshError
        for error in (discord.LoginFailure("sensitive-value"), RefreshError("sensitive-value")):
            with self.subTest(kind=type(error).__name__):
                self.assertEqual(self.run_fake_linux_service(error), 78)

    def test_temporary_google_auth_error_allows_restart(self):
        from google.auth.exceptions import RefreshError
        self.assertEqual(self.run_fake_linux_service(RefreshError("sensitive-value", retryable=True)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
