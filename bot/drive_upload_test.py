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


def upload_file(service, file_path):
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"파일이 없습니다: {file_path}"
        )

    file_metadata = {
        "name": file_path.name
    }

    media = MediaFileUpload(
        str(file_path),
        resumable=True
    )

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink"
    ).execute()

    return uploaded


if __name__ == "__main__":
    service = get_drive_service()

    test_file = BASE_DIR / "storage" / "common" / "drive_test.txt"

    test_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    test_file.write_text(
        "Project Hub Google Drive upload test",
        encoding="utf-8"
    )

    result = upload_file(
        service,
        test_file
    )

    print("Google Drive 업로드 성공")
    print("파일명:", result.get("name"))
    print("파일 ID:", result.get("id"))
    print("링크:", result.get("webViewLink"))