# Google Drive 사진 갤러리

자료실 상단의 **사진**에서 Drive `Project Hub/사진` 폴더의 이미지 파일을
썸네일로 봅니다. 최신/오래된 **등록일** 순서, 최근 7일/30일 필터, 30장씩
더 보기, 크게 보기, 이전/다음, Drive 원본 링크를 제공합니다.
사진 이름을 HTML로 해석하지 않고 썸네일을 인증된 같은 출처로 제공합니다.
원본은 Drive에 유지하며 DB나 storage로 복사하지 않습니다.
크게 보기는 기존 Drive 미리보기와 공유 권한을 사용하므로, 팀원이 원본을 볼
수 있도록 Drive에서 해당 폴더의 현재 공유 설정을 확인하세요. 이 기능은 공유
권한을 자동 변경하지 않습니다.

범위는 `기존 → 사진`의 직접 이미지, `A팀 → 사진/A팀`, `B팀 → 사진/B팀`입니다.
팀 폴더가 없으면 빈 상태를 보여주며 기존 사진으로 대체하지 않습니다.
임의 하위 폴더와 Drive 바로가지는 이번 버전에서 탐색하지 않습니다.

## 연결 전 준비

Discord 봇의 `drive.file`은 앱에서 생성하거나 명시적으로 선택한 파일에 대한
권한입니다. 사용자가 Drive에 직접 넣은 모든 사진을 자동으로 읽는 권한은
아닙니다. 별도의 사진 인증을 사용하고 봇의 `token.json`은 보존합니다. Discord 사진 수집도
사용할 때는 `drive.readonly`(사진 목록/미리보기)와 `drive.file`(앱의 새 파일 생성)을
함께 동의합니다. 기존 파일 전체를 수정하는 `drive` 권한은 요청하지 않습니다.
Google 동의 화면의 `drive.readonly`는 계정의 Drive 파일 전체 읽기 권한이며,
허브가 게시하는 범위는 설정한 사진 폴더로 제한됩니다.
[Google 권한 설명](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
[Google 썸네일 설명](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)

실제 운영 인증과 썸네일은 클라우드 테스트로 검증할 수 없습니다.
인증 토큰과 credentials.json은 개인 PC와 Pi에만 보관하고 GitHub/Discord/자료실/
클라우드 개발환경에 올리지 마세요. Google OAuth 앱의 테스트 사용자 등록 또는
추가 권한 동의가 필요한 경우 Google 화면에서 먼저 완료합니다.

## 개인 PC에서 사진 인증

인증 브라우저가 실행되는 개인 PC의 Python 3.11 환경에서 수행합니다.
기존 OAuth 데스크톱 앱 credentials.json을 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m organizer.photos --credentials .\credentials.json --token .\photos-token.json --upload
```

`photos-token.json`을 Pi의 인증된 code-server 파일 업로드로
`/home/khb/.config/project-hub/photos-token.json`에 옮깁니다. 공유 폴더에는 두지
않습니다. Pi 터미널에서 `chmod 600 ~/.config/project-hub/photos-token.json`을 실행합니다.
토큰 생성 때문에 봇을 중단하거나 봇 토큰을 재인증할 필요는 없습니다.

## Pi 설정과 배포 계획

기존 `~/.config/project-hub/library.env`를 보존하면서 아래 항목을 추가합니다.
사진 폴더 ID는 Drive 주소의 `/folders/` 뒤 문자열입니다. 이번 요청으로 확인한
실제 ID는 공개 저장소에 저장하지 않습니다.

```dotenv
PROJECT_HUB_PHOTO_FOLDER_ID=사진_폴더_ID
# 선택 사항: 기본 경로가 아닌 개인 토큰 경로
# PROJECT_HUB_PHOTO_TOKEN=/home/khb/.config/project-hub/photos-token.json
```

폴더 ID를 설정하지 않으면 동일 이름의 `Project Hub/사진`을 찾습니다. 폴더 이름이
중복되면 연결을 중단하므로 실제 ID 설정을 권장합니다.

이 변경은 **자료실 가상환경에 패키지 추가**가 필요합니다. DB 마이그레이션은
없습니다. 봇 requirements.txt의 버전과 같은 API 클라이언트, OAuth 라이브러리,
Pillow를 웹 가상환경에도 명시합니다. `phone_deploy.py`는 패키지를 설치하지 않으며
패키지가 없으면 서비스 중지 전에 검사에서 멈춥니다.

1. PHONE.md에 따라 검토한 커밋을 개발 체크아웃에서 선택합니다.
2. 개인 인증과 library.env 설정을 완료합니다.
3. 웹 가상환경의 설치 목록을 로컬 백업하고 변경된 requirements를 설치합니다.
4. 배포 도구의 검사가 통과한 뒤 같은 커밋을 `--apply`합니다.

```bash
cd ~/project-hub-dev
git status --short
git fetch origin
git switch --detach <선택한_커밋_SHA>
bash scripts/cloud-setup.sh
mkdir -p ~/.local/state/project-hub-deploy
~/.local/share/project-hub-library/venv311/bin/python -m pip freeze > ~/.local/state/project-hub-deploy/photos-packages-before.txt
~/.local/share/project-hub-library/venv311/bin/python -m pip install -r organizer/requirements-web.txt
.venv/bin/python deploy/raspberry-pi/phone_deploy.py
.venv/bin/python deploy/raspberry-pi/phone_deploy.py --apply
```

패키지 설치에 실패하면 배포하지 않습니다. 배포 뒤 자료실에서 기존 사진 목록,
썸네일, 크게 보기와 A/B팀 분리를 확인합니다. 사진 한 장을 Drive에 추가한 뒤
**새로고침**으로 나타나는지 확인합니다. code-only 도구의 복구 방법은 PHONE.md를
따르며, 위 패키지 설치 결과는 별도로 보존합니다. 원래 웹 의존성 버전은 바꾸지
않고 추가 라이브러리를 설치하는 계획입니다.

사진 읽기 인증이 없거나 만료되어 복구할 수 없으면 사진 화면에 연결 안내를
보여줍니다. 파일/채팅/AI 자료실과 Discord 업로드는 기존 설정으로 계속 사용합니다.
토큰을 교체했다면 자료실 서비스를 다시 시작해 새 인증을 반영합니다.

## Discord 사진공유 채널 → 동일 사진 폴더

운영 봇은 검증된 프로젝트 Discord 서버에서 `사진공유`, `사진-공유`,
`사진_공유`라는 일반 텍스트 채널을 자동 연결합니다. 이름 뒤 이모지도 인식합니다.
다른 서버의 같은 이름 채널은 연결하지 않습니다. 동일 이름이 여러 개면 자동으로
선택하지 않고, 아래 선택 설정으로 실제 채널 ID를 지정할 수 있습니다.

```dotenv
# ~/.config/project-hub/library.env 또는 운영 .env에 선택적으로 지정
PROJECT_HUB_PHOTO_CHANNEL_ID=실제_사진공유_채널_ID
```

`PROJECT_HUB_PHOTO_FOLDER_ID`는 위에서 연결한 **기존 사진 폴더의 ID**를 반드시
지정합니다. 봇과 갤러리는 같은 설정 파일을 읽습니다. 사진 업로드 때문에 새
사진 폴더를 만들거나 기존 폴더의 공유 권한을 바꾸지 않습니다.

배포 뒤 첫 연결에서는 접근 가능한 **기존 사진 메시지를 처음부터** 확인합니다.
한 번에 100개 메시지를 오래된 순서대로 읽고 확인 위치를 기존 DB에 저장합니다.
재시작 시 이어서 처리합니다. 일반 파일/채팅의 최초 24시간 규칙은 유지하며,
사진공유의 첨부사진에만 전체 이력 수집을 적용합니다. 사진 채널의 스레드도
접근 가능한 활성/보관 스레드 범위에서 기존 수집 방식으로 확인합니다.

사진 파일만 `storage/photos`에 원본과 메타데이터를 저장하고 SQLite에 기록한 뒤
동일 Drive 폴더로 업로드합니다. 문서/동영상 등 다른 첨부는 사진 수집에서
제외합니다. 원본 메시지 ID와 첨부 ID를 재사용해 재스캔/재시작 때 같은 사진을
다시 업로드하지 않습니다. Drive에 사용자가 직접 넣어 둔 사진은 그대로
갤러리에 함께 표시하며, 봇이 원격 기존 사진을 내려받거나 삭제하지 않습니다.
다른 메시지에 반복 첨부한 동일 내용은 기존 정책대로 개별 기록을 유지합니다.

이미 읽기 전용 `photos-token.json`을 만들어 두었다면 개인 PC에서 `--upload`로
다시 인증한 사진 토큰만 Pi에서 교체하세요. 봇의 `token.json`은 교체하지 않습니다.
사진 토큰이 없으면 기존 봇 인증으로 업로드를 시도하지만, 수동 생성 폴더에
접근할 수 없는 권한이면 로컬 저장과 재시도 대기 상태로 남습니다. 사진 토큰이
있는데 읽기 전용이라면 업로드를 시도하지 않고 인증 필요 상태로 남깁니다.

봇에는 채널 보기, 메시지 기록 보기, Message Content Intent가 필요하고 Google
계정에는 설정한 사진 폴더의 파일 추가 권한이 필요합니다. 삭제된 Discord
사진이나 더 이상 다운로드할 수 없는 첨부는 복구할 수 없습니다. 최초 수집은
사진 수와 Discord/Drive 요청 제한에 따라 시간이 걸립니다.

이번 확장은 DB 테이블/열/인덱스를 추가하지 않고 기존 수집 체크포인트와
업로드 대기열을 사용합니다. 위 패키지 준비와 인증 후 검토한 최신 커밋을
PHONE.md 절차로 적용하면 봇 재시작 때 수집을 시작합니다. 인증과 폴더 설정을
바꾼 뒤에는 두 서비스를 다시 시작해 새 설정을 반영합니다.

### 운영 확인

- 봇 로그의 `PROJECT_CHANNELS_READY`에서 `photo_channels=1`인지 확인합니다.
- `HISTORY_SCANNED`와 `FILE_SAVED | category=photos`가 기록되는지 확인합니다.
- 자료실 `사진` 화면의 **새로고침**으로 수집 사진을 확인합니다.
- 새 사진 한 장을 채널에 올려 로컬 저장/Drive/갤러리에 나타나는지 확인합니다.
- 서비스 재시작 후 동일 메시지가 다시 업로드되지 않는지 확인합니다.

운영 설정 파일에 이미 같은 채널을 다른 카테고리로 수동 연결했다면 그 항목을
`photos`로 변경해야 합니다. 팀 카테고리와 충돌하는 채널을 자동 재분류하지
않습니다. 기존 체크포인트가 있는 채널은 되감지 않습니다. 처음부터의 확인이
필요하다면 먼저 운영 DB를 백업하고 해당 채널의 수집 위치를 별도로 검토합니다.
