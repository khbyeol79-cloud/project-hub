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
아닙니다. 별도의 **읽기 전용** 인증을 사용하고 봇의 `token.json`은 보존합니다.
Google 동의 화면의 `drive.readonly`는 계정의 Drive 파일 전체 읽기 권한이며,
허브가 게시하는 범위는 설정한 사진 폴더로 제한됩니다.
[Google 권한 설명](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
[Google 썸네일 설명](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)

실제 운영 인증과 썸네일은 클라우드 테스트로 검증할 수 없습니다.
인증 토큰과 credentials.json은 개인 PC와 Pi에만 보관하고 GitHub/Discord/자료실/
클라우드 개발환경에 올리지 마세요. Google OAuth 앱의 테스트 사용자 등록 또는
추가 권한 동의가 필요한 경우 Google 화면에서 먼저 완료합니다.

## 개인 PC에서 읽기 전용 인증

인증 브라우저가 실행되는 개인 PC의 Python 3.11 환경에서 수행합니다.
기존 OAuth 데스크톱 앱 credentials.json을 사용합니다.

```powershell
.\.venv\Scripts\python.exe -m organizer.photos --credentials .\credentials.json --token .\photos-token.json
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

읽기 전용 인증이 없거나 만료되어 복구할 수 없으면 사진 화면에 연결 안내를
보여줍니다. 파일/채팅/AI 자료실과 Discord 업로드는 기존 설정으로 계속 사용합니다.
토큰을 교체했다면 자료실 서비스를 다시 시작해 새 인증을 반영합니다.
