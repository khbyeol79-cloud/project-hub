import asyncio
import hashlib
import importlib
import json
import os
import tempfile
import threading
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot import database, discord_bot as bot, drive
from bot.upload_queue import UploadWorker


class SetupTests(unittest.TestCase):
    def test_import_has_no_external_side_effects(self):
        with patch("bot.drive.get_drive_service") as auth, patch.object(database, "init_db") as db, patch("discord.Client.run") as run:
            importlib.reload(bot)
            auth.assert_not_called()
            db.assert_not_called()
            run.assert_not_called()
        importlib.reload(bot)

    def test_missing_token_fails_before_database_or_auth(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bot, "load_dotenv"), patch.object(database, "init_db") as db, patch.object(drive, "get_drive_service") as auth:
            with self.assertRaises(RuntimeError):
                bot.main()
            db.assert_not_called()
            auth.assert_not_called()

    def test_invalid_category_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "channels.json"
            config.write_text(json.dumps({"channels": {"1": "../../escape"}}))
            with patch.object(bot, "CONFIG_PATH", config), self.assertRaises(ValueError):
                bot.load_channel_map()

    def test_safe_filename(self):
        self.assertEqual(bot.safe_filename('../bad\\name:test?.txt'), '.._bad_name_test_.txt')
        self.assertEqual(bot.safe_filename('...'), 'attachment')


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown)
        for target, name, value in (
            (database, "DB_PATH", root / "test.db"),
            (bot, "STORAGE_DIR", root / "storage"),
            (bot, "CHANNEL_MAP", {"2": "common"}),
            (bot, "drive_executor", self.executor),
            (bot, "upload_worker", UploadWorker()),
            (bot, "collection_lock", asyncio.Lock()),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        database.init_db()
        for name, value in (("get_drive_service", object()), ("generate_file_id", "reserved-id")):
            p = patch.object(drive, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def message(self, message_id=1, content=b"hello", filename="sample.txt"):
        async def save(path):
            Path(path).write_bytes(content)
        return SimpleNamespace(
            id=message_id, author=SimpleNamespace(id=3, bot=False),
            channel=SimpleNamespace(id=2, name="test"), guild=SimpleNamespace(id=4),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            attachments=[SimpleNamespace(id=message_id * 10, filename=filename, save=save)],
        )

    def metadata(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(bot.STORAGE_DIR.rglob("*.json"))]

    async def test_collection_duplicate_and_new_version(self):
        with patch.object(drive, "upload_file", return_value={"file_id": "remote", "url": "https://example.test/file"}):
            await bot.on_message(self.message())
            await bot.on_message(self.message(2))
            await bot.on_message(self.message(3, b"changed"))
        items = self.metadata()
        self.assertEqual([m["duplicate_type"] for m in items], ["new_file", "exact_duplicate", "new_version"])
        self.assertEqual(items[0]["sha256"], hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(items[1]["duplicate_of"], 1)
        self.assertTrue(all(m["drive_upload_status"] == "uploaded" for m in items))
        with closing(database.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM files WHERE google_drive_file_id = 'remote'").fetchone()[0], 3)

    async def test_upload_failure_preserves_local_metadata_and_continues(self):
        message = self.message()
        message.attachments += self.message(filename="second.txt").attachments
        message.attachments[1].id = 11
        with patch.object(drive, "upload_file", side_effect=[RuntimeError("offline"), {"file_id": "ok", "url": None}]), self.assertLogs(bot.logger, level="ERROR"):
            await bot.on_message(message)
        items = self.metadata()
        self.assertEqual([m["drive_upload_status"] for m in items], ["failed", "uploaded"])
        self.assertTrue(all(Path(m["local_path"]).exists() for m in items))
        with closing(database.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)

    async def test_upload_does_not_block_event_loop(self):
        started, release = threading.Event(), threading.Event()
        def upload(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise RuntimeError("event loop blocked")
            return {"file_id": "remote", "url": None}
        with patch.object(drive, "upload_file", side_effect=upload):
            task = asyncio.create_task(bot.on_message(self.message()))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
        self.assertEqual(self.metadata()[0]["drive_upload_status"], "uploaded")

    async def test_same_message_concurrently_is_uploaded_once(self):
        with patch.object(drive, "upload_file", return_value={"file_id": "remote", "url": None}) as upload:
            await asyncio.gather(bot.on_message(self.message()), bot.on_message(self.message()))
            await bot.on_message(self.message())
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(len(self.metadata()), 1)

    async def test_failed_upload_survives_restart_and_respects_backoff(self):
        with patch.object(drive, "upload_file", side_effect=TimeoutError("private error detail")), self.assertLogs(bot.logger, level="ERROR"):
            await bot.on_message(self.message())
        failed = database.get_file(1)
        self.assertEqual(failed["last_upload_error"], "TimeoutError")
        self.assertEqual(failed["planned_drive_id"], "reserved-id")
        self.assertEqual(database.due_uploads(now=failed["next_retry_at"] - 1), [])
        self.assertEqual(database.due_uploads(now=failed["next_retry_at"]), [1])
        # Simulate a fresh process reading the durable queue and the deadline passing.
        database.init_db()
        with closing(database.get_connection()) as conn, conn:
            conn.execute("UPDATE files SET next_retry_at = 0 WHERE id = 1")
        with patch.object(bot, "upload_worker", UploadWorker()), patch.object(drive, "generate_file_id") as generate, patch.object(drive, "upload_file", return_value={"file_id": "reserved-id", "url": None}) as upload:
            await bot.retry_pending_once()
            await bot.retry_pending_once()
        generate.assert_not_called()
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(upload.call_args.kwargs["file_id"], "reserved-id")
        self.assertEqual(database.get_file(1)["upload_status"], "uploaded")
        self.assertEqual(database.get_file(1)["upload_attempts"], 2)

    async def test_db_failure_after_upload_reuses_reserved_remote_id(self):
        result = {"file_id": "reserved-id", "url": None}
        with patch.object(drive, "upload_file", return_value=result), patch.object(database, "update_drive_info", side_effect=sqlite3.OperationalError("locked")), self.assertLogs(bot.logger, level="ERROR"):
            await bot.on_message(self.message())
        with closing(database.get_connection()) as conn, conn:
            conn.execute("UPDATE files SET next_retry_at = 0 WHERE id = 1")
        with patch.object(drive, "generate_file_id") as generate, patch.object(drive, "upload_file", return_value=result) as upload:
            await bot.retry_pending_once()
        generate.assert_not_called()
        self.assertEqual(upload.call_args.kwargs["file_id"], "reserved-id")
        self.assertEqual(database.get_file(1)["upload_status"], "uploaded")

    async def test_legacy_uploaded_attachment_is_adopted_without_upload(self):
        with patch.object(drive, "upload_file", return_value={"file_id": "legacy-remote", "url": None}):
            await bot.on_message(self.message())
        with closing(database.get_connection()) as conn, conn:
            conn.execute("UPDATE files SET discord_attachment_id = NULL WHERE id = 1")
        with patch.object(drive, "upload_file") as upload:
            await bot.on_message(self.message())
        upload.assert_not_called()
        self.assertEqual(database.get_file(1)["discord_attachment_id"], "10")

    async def test_json_failure_does_not_reupload_completed_file(self):
        with patch.object(drive, "upload_file", return_value={"file_id": "remote", "url": None}) as upload, patch("bot.upload_queue.write_metadata", side_effect=OSError("disk full")), self.assertLogs(bot.logger, level="ERROR"):
            await bot.on_message(self.message())
            await bot.retry_pending_once()
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(database.get_file(1)["upload_status"], "uploaded")


if __name__ == "__main__":
    unittest.main()
