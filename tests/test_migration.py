import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bot import database


class MigrationTests(unittest.TestCase):
    def test_legacy_rows_are_preserved_and_unknown_uploads_need_review(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(database, "DB_PATH", Path(folder) / "legacy.db"):
            with closing(database.get_connection()) as conn, conn:
                conn.execute('''CREATE TABLE files (
                    id INTEGER PRIMARY KEY, discord_message_id TEXT,
                    saved_filename TEXT, google_drive_file_id TEXT)''')
                conn.execute("INSERT INTO files VALUES (1, 'old', 'old.txt', 'remote')")
                conn.execute("INSERT INTO files VALUES (2, 'unknown', 'unknown.txt', NULL)")
            database.init_db()
            database.init_db()
            self.assertEqual(database.get_file(1)["upload_status"], "uploaded")
            self.assertEqual(database.get_file(2)["upload_status"], "needs_review")
            self.assertEqual(database.due_uploads(), [])
            with closing(database.get_connection()) as conn, conn:
                conn.execute("UPDATE files SET discord_attachment_id = 'a' WHERE id = 1")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute("INSERT INTO files (id, discord_message_id, discord_attachment_id) VALUES (3, 'old', 'a')")
