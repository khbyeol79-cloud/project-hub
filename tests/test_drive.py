import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
from googleapiclient.errors import HttpError
from bot import drive


def http_error(status):
    return HttpError(httplib2.Response({"status": str(status)}), b'{"error":{"message":"test"}}')


class DriveRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "file.txt"
        self.path.write_bytes(b"payload")
        self.digest = hashlib.sha256(b"payload").hexdigest()
        self.service = MagicMock()
        self.remote = {"id": "stable-id", "name": "file.txt", "appProperties": {"sha256": self.digest}, "trashed": False}

    def upload(self):
        return drive.upload_file(self.service, self.path, "common", file_id="stable-id", expected_sha256=self.digest)

    def test_remote_success_after_lost_response_skips_create(self):
        self.service.files().get().execute.return_value = self.remote
        self.path.unlink()
        self.assertEqual(self.upload()["file_id"], "stable-id")
        self.service.files().create.assert_not_called()

    def test_create_uses_reserved_id_and_409_recovers(self):
        self.service.files().get().execute.side_effect = [http_error(404), self.remote]
        self.service.files().create().execute.side_effect = http_error(409)
        with patch.object(drive, "prepare_drive_folders", return_value={"common": "folder"}):
            result = self.upload()
        self.assertEqual(result["file_id"], "stable-id")
        kwargs = self.service.files().create.call_args.kwargs
        self.assertEqual(kwargs["body"]["id"], "stable-id")
        self.assertEqual(kwargs["body"]["appProperties"]["sha256"], self.digest)
        self.assertTrue(kwargs["media_body"].stream().closed)

    def test_403_does_not_create_another_file(self):
        self.service.files().get().execute.side_effect = http_error(403)
        with self.assertRaises(HttpError):
            self.upload()
        self.service.files().create.assert_not_called()

    def test_changed_local_file_is_not_uploaded(self):
        self.service.files().get().execute.side_effect = http_error(404)
        self.path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.upload()
        self.service.files().create.assert_not_called()

    def test_remote_hash_mismatch_is_not_treated_as_success(self):
        self.remote["appProperties"]["sha256"] = "different"
        self.service.files().get().execute.return_value = self.remote
        with self.assertRaises(ValueError):
            self.upload()

    def test_retry_auth_never_opens_browser(self):
        with patch.object(drive, "TOKEN_PATH", self.path.parent / "missing.json"), patch.object(drive.InstalledAppFlow, "from_client_secrets_file") as flow:
            with self.assertRaises(RuntimeError):
                drive.get_drive_service(interactive=False)
            flow.assert_not_called()
