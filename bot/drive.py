from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


BASE_DIR = Path(__file__).resolve().parent.parent

CREDENTIALS_PATH = BASE_DIR / "credentials.json"
TOKEN_PATH = BASE_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]


CATEGORY_FOLDERS = {
    "common": "00_공통",
    "plc": "01_PLC",
    "robot": "02_Robot",
    "vision": "03_Vision",
    "3d_model": "04_3D_Model",
    "arduino": "05_Arduino",
    "meeting": "06_회의자료",
    "final": "99_최종자료"
}

ROOT_FOLDER_NAME = "Project Hub"


def get_drive_service():
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
    drive_filename=None
):
    file_path = Path(file_path)

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

    media = MediaFileUpload(
        str(file_path),
        resumable=True
    )

    uploaded = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()

    return {
        "file_id": uploaded["id"],
        "name": uploaded["name"],
        "url": uploaded.get(
            "webViewLink"
        )
    }


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