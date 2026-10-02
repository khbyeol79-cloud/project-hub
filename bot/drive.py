from pathlib import Path
import hashlib

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


BASE_DIR = Path(__file__).resolve().parent.parent

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]


CATEGORY_FOLDERS = {
    "common": "hello⭐",
    "3d_model": "기구제작🎨",
    "vision": "pc💻",
    "plc": "plc🛠️",
    "meeting": "게시물",
}

ROOT_FOLDER_NAME = "Project Hub"


class AuthorizationRequired(RuntimeError):
    """Background upload needs interactive authorization on the user's PC."""


def get_drive_service(interactive=True):
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(
            TOKEN_PATH,
            SCOPES
        )

    if not creds or not creds.valid:

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())

        else:
            if not interactive:
                raise AuthorizationRequired("Google OAuth login required; run bot/drive.py locally")
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH,
                SCOPES
            )

            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(
            creds.to_json(),
            encoding="utf-8"
        )

    return build(
        "drive",
        "v3",
        credentials=creds
    )


def find_folder(
    service,
    folder_name,
    parent_id=None
):
    query = [
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
        f"name='{folder_name}'"
    ]

    if parent_id:
        query.append(
            f"'{parent_id}' in parents"
        )

    result = service.files().list(
        q=" and ".join(query),
        spaces="drive",
        fields="files(id, name)"
    ).execute()

    folders = result.get(
        "files",
        []
    )

    if folders:
        return folders[0]["id"]

    return None


def create_folder(
    service,
    folder_name,
    parent_id=None
):
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder"
    }

    if parent_id:
        metadata["parents"] = [
            parent_id
        ]

    folder = service.files().create(
        body=metadata,
        fields="id, name"
    ).execute()

    return folder["id"]


def get_or_create_folder(
    service,
    folder_name,
    parent_id=None
):
    folder_id = find_folder(
        service,
        folder_name,
        parent_id
    )

    if folder_id:
        return folder_id

    return create_folder(
        service,
        folder_name,
        parent_id
    )


def prepare_drive_folders(
    service
):
    root_id = get_or_create_folder(
        service,
        ROOT_FOLDER_NAME
    )

    folder_ids = {}

    for category, folder_name in CATEGORY_FOLDERS.items():

        folder_id = get_or_create_folder(
            service,
            folder_name,
            root_id
        )

        folder_ids[category] = folder_id

    return folder_ids


def upload_file(
    service,
    file_path,
    category,
    drive_filename=None,
    file_id=None,
    expected_sha256=None
):
    file_path = Path(file_path)

    if file_id:
        existing = find_uploaded_file(service, file_id, expected_sha256)
        if existing:
            return existing

    if expected_sha256:
        with file_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
                raise ValueError("Local file changed since collection")

    if not file_path.exists():
        raise FileNotFoundError(
            f"업로드할 파일이 없습니다: {file_path}"
        )

    if category not in CATEGORY_FOLDERS:
        raise ValueError(
            f"알 수 없는 카테고리입니다: {category}"
        )

    folder_ids = prepare_drive_folders(
        service
    )

    target_folder_id = folder_ids[
        category
    ]

    if drive_filename is None:
        drive_filename = file_path.name

    metadata = {
        "name": drive_filename,
        "parents": [
            target_folder_id
        ]
    }

    if file_id:
        metadata["id"] = file_id
    if expected_sha256:
        metadata["appProperties"] = {"sha256": expected_sha256}

    media = MediaFileUpload(
        str(file_path),
        resumable=True
    )

    try:
        uploaded = service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, webViewLink"
        ).execute()
    except HttpError as exc:
        if exc.resp.status != 409 or not file_id:
            raise
        existing = find_uploaded_file(service, file_id, expected_sha256)
        if not existing:
            raise
        return existing
    finally:
        media.stream().close()

    return {
        "file_id": uploaded["id"],
        "name": uploaded["name"],
        "url": uploaded.get(
            "webViewLink"
        )
    }


def generate_file_id(service):
    return service.files().generateIds(count=1, space="drive", type="files").execute()["ids"][0]


def find_uploaded_file(service, file_id, expected_sha256=None):
    try:
        item = service.files().get(
            fileId=file_id, fields="id,name,webViewLink,trashed,appProperties"
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise
    if item.get("trashed"):
        raise ValueError("Reserved Drive file is in trash; manual review required")
    if expected_sha256 and item.get("appProperties", {}).get("sha256") != expected_sha256:
        raise ValueError("Reserved Drive file hash does not match")
    return {"file_id": item["id"], "name": item["name"], "url": item.get("webViewLink")}


if __name__ == "__main__":
    service = get_drive_service()

    folders = prepare_drive_folders(
        service
    )

    print(
        "Google Drive 폴더 준비 완료"
    )

    for category, folder_id in folders.items():
        print(
            f"{category} -> "
            f"{CATEGORY_FOLDERS[category]} -> "
            f"{folder_id}"
        )
