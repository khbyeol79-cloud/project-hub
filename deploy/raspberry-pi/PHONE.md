# PC를 끄고 개발하기

운영 봇과 자료실은 Pi에서 계속 실행됩니다. 이 PC의 Codex 채팅이나 로컬
자동 저장 작업은 PC가 꺼지면 실행되지 않습니다.

## 휴대폰에서 수정 요청

Codex Cloud 환경에 `khbyeol79-cloud/project-hub`를 연결합니다.
현재 통합 작업 기준 브랜치는 `feat/ai-organizer`입니다. `main`은 아직 이전 코드일 수
있으므로 작업 시작 시 브랜치를 확인합니다. 설정 스크립트는
`bash scripts/cloud-setup.sh`, 검사 명령은 `bash scripts/check.sh`입니다.
환경에는 운영 비밀키를 넣지 않습니다. 설치 시 PyPI 접근이 필요합니다.
OCR 실제 통합 검사에는 Tesseract 한국어/영어와 Poppler도 필요합니다.

수정 요청 예시:

> project-hub의 feat/ai-organizer 기준으로 수정해줘. AGENTS.md를 따르고
> Python 3.11 테스트를 실행해줘. 변경사항을 GitHub에 저장하고 배포할 커밋 SHA와
> Pi에서 실행할 명령을 알려줘. 운영 키는 클라우드에 복사하지 마.

## 휴대폰에서 Pi에 적용

기존 code-server 관리자 주소로 접속하고 Authelia/OTP로 로그인합니다.
자료실의 공개 링크는 관리·배포용이 아닙니다. code-server 터미널에서 진행합니다.

Pi 개발 체크아웃은 `/home/khb/project-hub-dev`, 운영 폴더는
`/home/khb/project-hub`입니다. 서로 별개이므로 `git pull`만으로 배포되지 않습니다.

```bash
cd ~/project-hub-dev
git status --short  # 변경이 있으면 중단하고 보존
git fetch origin
git switch --detach <검토한_커밋_SHA>
bash scripts/cloud-setup.sh
.venv/bin/python deploy/raspberry-pi/phone_deploy.py
# 위 검사가 모두 통과한 뒤 같은 커밋을 적용
.venv/bin/python deploy/raspberry-pi/phone_deploy.py --apply
```

`--apply`에서 sudo 비밀번호가 필요하면 터미널에만 입력합니다. 채팅에 보내지
않습니다. 도구는 소스 테스트와 운영 Python/패키지 버전을 확인하고, 두 서비스를
잠시 중지한 뒤 코드와 SQLite DB를 로컬에 백업합니다. 코드만 교체하고 서비스를
시작해 자료실 응답을 확인합니다. 이미 운영 코드와 같으면 재시작하지 않습니다.
Discord 실제 연결·새 파일 수집 여부는 `/상태`로 추가 확인합니다.

`.env`, OAuth/AI 키, 채널 설정, 수집 파일, 로그, DB는 코드 배포 대상이 아닙니다.
의존성 변경, 파일 삭제, DB 스키마 변경은 별도 배포 계획으로 처리하세요.
DB 변경이 포함된 커밋을 이 도구만으로 배포하지 마세요.

## 복구

배포 실패 시 이전 코드를 자동 복구하고 서비스를 시작합니다. 성공 뒤 되돌리려면
배포 때 출력한 실제 백업 경로를 사용합니다.

```bash
.venv/bin/python deploy/raspberry-pi/phone_deploy.py --rollback <출력된_백업_경로>
```

배포 후 별도로 코드가 수정되었다면 덮어쓰지 않고 멈춥니다. DB는 새로 수집된
자료를 지우지 않도록 자동으로 과거 상태로 되돌리지 않습니다. 스키마 변경으로
복구가 필요한 경우 서비스를 멈추고 해당 백업의 DB를 따로 검토해야 합니다.
백업은 `~/.local/state/project-hub-deploy/` 아래 비공개 로컬 파일로 유지됩니다.

클라우드에서 운영 Pi에 직접 접속하거나 자동 배포하지 않습니다. 이 구성은
휴대폰에서 클라우드 개발 + 인증된 Pi 터미널에서 명시적 배포하는 방식입니다.
