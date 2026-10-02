import sqlite3
import time
from contextlib import closing
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "project_hub.db"


def get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            discord_message_id TEXT NOT NULL,
            discord_guild_id TEXT,
            discord_channel_id TEXT NOT NULL,
            discord_channel TEXT NOT NULL,

            discord_author_id TEXT NOT NULL,
            discord_author TEXT NOT NULL,

            uploaded_at TEXT NOT NULL,
            category TEXT NOT NULL,

            original_filename TEXT NOT NULL,
            saved_filename TEXT NOT NULL,
            local_path TEXT NOT NULL,

            file_size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,

            duplicate_of INTEGER,
            version_group TEXT,
            duplicate_type TEXT,

            discord_url TEXT,

            google_drive_file_id TEXT,
            google_drive_url TEXT,

            description TEXT,
            version INTEGER DEFAULT 1,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(files)")}
    additions = {
        "discord_attachment_id": "TEXT",
        "upload_status": "TEXT NOT NULL DEFAULT 'pending'",
        "upload_attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "REAL NOT NULL DEFAULT 0",
        "last_upload_error": "TEXT",
        "planned_drive_id": "TEXT",
        "metadata_path": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            cursor.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")
    if "upload_status" not in columns:
        # Old failed uploads had no stable remote ID. Do not blindly re-upload them.
        cursor.execute("""
            UPDATE files SET upload_status = CASE
                WHEN google_drive_file_id IS NOT NULL THEN 'uploaded'
                ELSE 'needs_review' END
        """)
    cursor.execute("""CREATE UNIQUE INDEX IF NOT EXISTS unique_discord_attachment
        ON files(discord_message_id, discord_attachment_id)
        WHERE discord_attachment_id IS NOT NULL""")
    cursor.execute("CREATE INDEX IF NOT EXISTS upload_queue ON files(upload_status, next_retry_at)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS channel_cursors (
        channel_id TEXT PRIMARY KEY,
        last_message_id TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")

    conn.commit()
    conn.close()


def find_existing_file(original_filename, sha256):
    conn = get_connection()
    cursor = conn.cursor()

    # 내용이 완전히 같은 파일 확인
    cursor.execute("""
        SELECT
            id,
            original_filename,
            sha256,
            version_group
        FROM files
        WHERE sha256 = ?
        ORDER BY id ASC
        LIMIT 1
    """, (sha256,))

    same_hash = cursor.fetchone()

    if same_hash:
        conn.close()

        return {
            "type": "exact_duplicate",
            "duplicate_of": same_hash[0],
            "version_group": (
                same_hash[3]
                if same_hash[3]
                else same_hash[1]
            )
        }

    # 파일명은 같지만 내용이 다른 경우
    cursor.execute("""
        SELECT
            id,
            original_filename,
            sha256,
            version_group
        FROM files
        WHERE original_filename = ?
        ORDER BY id ASC
        LIMIT 1
    """, (original_filename,))

    same_name = cursor.fetchone()

    conn.close()

    if same_name:
        return {
            "type": "new_version",
            "duplicate_of": None,
            "version_group": (
                same_name[3]
                if same_name[3]
                else original_filename
            )
        }

    # 완전히 새로운 파일
    return {
        "type": "new_file",
        "duplicate_of": None,
        "version_group": original_filename
    }


def insert_file(metadata):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO files (
            discord_message_id,
            discord_guild_id,
            discord_channel_id,
            discord_channel,
            discord_author_id,
            discord_author,
            uploaded_at,
            category,
            original_filename,
            saved_filename,
            local_path,
            file_size_bytes,
            sha256,
            duplicate_of,
            version_group,
            duplicate_type,
            discord_url,
            discord_attachment_id,
            metadata_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        metadata["discord_message_id"],
        metadata["discord_guild_id"],
        metadata["discord_channel_id"],
        metadata["discord_channel"],
        metadata["discord_author_id"],
        metadata["discord_author"],
        metadata["uploaded_at"],
        metadata["category"],
        metadata["original_filename"],
        metadata["saved_filename"],
        metadata["local_path"],
        metadata["file_size_bytes"],
        metadata["sha256"],
        metadata["duplicate_of"],
        metadata["version_group"],
        metadata["duplicate_type"],
        metadata["discord_url"],
        metadata.get("discord_attachment_id"),
        metadata.get("metadata_path")
    ))

    file_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return file_id


def update_drive_info(
    file_id,
    drive_file_id,
    drive_url
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE files
        SET
            google_drive_file_id = ?,
            google_drive_url = ?,
            upload_status = 'uploaded',
            next_retry_at = 0,
            last_upload_error = NULL
        WHERE id = ?
    """, (
        drive_file_id,
        drive_url,
        file_id
    ))

    conn.commit()
    conn.close()


def get_file(file_id):
    with closing(get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(row) if row else None


def find_attachment(message_id, attachment_id, legacy_prefix):
    with closing(get_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("""SELECT * FROM files
            WHERE discord_message_id = ? AND discord_attachment_id = ?""",
            (message_id, attachment_id)).fetchone()
        if row is None:
            # Adopt pre-migration records without downloading or uploading again.
            row = conn.execute("""SELECT * FROM files
                WHERE discord_message_id = ? AND discord_attachment_id IS NULL
                  AND substr(saved_filename, 1, ?) = ?
                ORDER BY google_drive_file_id IS NULL, id LIMIT 1""",
                (message_id, len(legacy_prefix), legacy_prefix)).fetchone()
            if row:
                conn.execute("UPDATE files SET discord_attachment_id = ? WHERE id = ?",
                             (attachment_id, row["id"]))
        return dict(row) if row else None


def reserve_drive_id(file_id, candidate):
    with closing(get_connection()) as conn, conn:
        conn.execute("UPDATE files SET planned_drive_id = COALESCE(planned_drive_id, ?) WHERE id = ?",
                     (candidate, file_id))
        return conn.execute("SELECT planned_drive_id FROM files WHERE id = ?", (file_id,)).fetchone()[0]


def due_uploads(now=None, limit=50):
    with closing(get_connection()) as conn:
        return [row[0] for row in conn.execute("""SELECT id FROM files
            WHERE upload_status IN ('pending', 'failed') AND next_retry_at <= ?
            ORDER BY next_retry_at, id LIMIT ?""", (time.time() if now is None else now, limit))]


def mark_upload_attempt(file_id):
    with closing(get_connection()) as conn, conn:
        conn.execute("UPDATE files SET upload_attempts = upload_attempts + 1 WHERE id = ?", (file_id,))


def mark_upload_failed(file_id, error, now=None):
    with closing(get_connection()) as conn, conn:
        attempts = conn.execute("SELECT upload_attempts FROM files WHERE id = ?", (file_id,)).fetchone()[0]
        delay = min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7))
        conn.execute("""UPDATE files SET upload_status = 'failed', last_upload_error = ?,
            next_retry_at = ? WHERE id = ?""", (error, (time.time() if now is None else now) + delay, file_id))


def initialize_channel_cursor(channel_id, fallback_id):
    """Freeze the initial boundary before live events can insert newer records."""
    with closing(get_connection()) as conn, conn:
        latest = conn.execute("""SELECT MAX(CAST(discord_message_id AS INTEGER))
            FROM files WHERE discord_channel_id = ?""", (str(channel_id),)).fetchone()[0]
        # Include the last stored message: it may contain a partially saved batch.
        initial = max(0, latest - 1) if latest else int(fallback_id)
        conn.execute("INSERT OR IGNORE INTO channel_cursors(channel_id, last_message_id) VALUES (?, ?)",
                     (str(channel_id), str(initial)))


def get_channel_cursor(channel_id):
    with closing(get_connection()) as conn:
        row = conn.execute("SELECT last_message_id FROM channel_cursors WHERE channel_id = ?",
                           (str(channel_id),)).fetchone()
        return int(row[0]) if row else None


def advance_channel_cursor(channel_id, message_id):
    """Only the ordered history scan advances this boundary, never live events."""
    with closing(get_connection()) as conn, conn:
        conn.execute("""UPDATE channel_cursors
            SET last_message_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE channel_id = ? AND CAST(last_message_id AS INTEGER) < ?""",
            (str(message_id), str(channel_id), int(message_id)))
