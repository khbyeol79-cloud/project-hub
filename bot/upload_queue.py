"""Durable upload attempts, executed on the single Drive worker thread."""
import json
import logging
import time
from pathlib import Path

if __package__:
    from . import database, drive
else:
    import database
    import drive

logger = logging.getLogger("project-hub")


def write_metadata(row):
    if not row.get("metadata_path"):
        return
    path = Path(row["metadata_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(row, drive_upload_status=row["upload_status"])
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class UploadWorker:
    def __init__(self):
        self.service = None
        self.photo_service = None

    def close(self):
        if self.service is not None:
            self.service.close()
            self.service = None
        if self.photo_service is not None:
            self.photo_service.close()
            self.photo_service = None

    def upload(self, file_id):
        row = database.get_file(file_id)
        if not row or row["upload_status"] in ("uploaded", "needs_review"):
            return
        if row["next_retry_at"] > time.time():
            return
        database.mark_upload_attempt(file_id)
        try:
            if row['category'] == 'photos':
                if self.photo_service is None:
                    self.photo_service = drive.get_photo_upload_service()
                service = self.photo_service
            else:
                if self.service is None:
                    self.service = drive.get_drive_service(interactive=False)
                service = self.service
            planned_id = row["planned_drive_id"]
            if not planned_id:
                planned_id = database.reserve_drive_id(file_id, drive.generate_file_id(service))
            result = drive.upload_file(
                service, row["local_path"], row["category"], row["original_filename"],
                file_id=planned_id, expected_sha256=row["sha256"],
            )
            database.update_drive_info(file_id, result["file_id"], result["url"])
            logger.info("FILE_SAVED | category=%s | message_id=%s | file_id=%s",
                        row["category"], row["discord_message_id"], file_id)
        except Exception as exc:
            # Store only the exception type/status, never OAuth URLs or token data.
            error = type(exc).__name__
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status is not None:
                error += f" HTTP {status}"
            database.mark_upload_failed(file_id, error)
            logger.error("UPLOAD_RETRY_SCHEDULED | file_id=%s | error=%s", file_id, error)
        try:
            write_metadata(database.get_file(file_id))
        except OSError:
            # SQLite is authoritative; a JSON error must not trigger a new upload.
            logger.error("METADATA_WRITE_FAILED | file_id=%s", file_id)
