import sqlite3
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

    conn.commit()
    conn.close()


def find_existing_file(original_filename, sha256):
    conn = get_connection()
    cursor = conn.cursor()

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
            discord_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        metadata["discord_url"]
    ))

    conn.commit()
    conn.close()