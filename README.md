# Project Hub

Discord 첨부파일을 로컬에 저장하고 SHA-256, SQLite 중복 판정, 카테고리별 Google Drive 업로드를 처리합니다. Python **3.11** 기준입니다.

## Windows 개발환경

```powershell
cd D:\project\project-hub
uv venv --python 3.11 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m bot.discord_bot
```

`uv`가 없다면 설치된 Python 3.11 실행파일로 `-m venv .venv`를 실행하고 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`를 사용합니다. 다른 PC의 `.venv`를 복사하지 말고 각 PC에서 생성하세요. 기존 실행 방식 `python bot/discord_bot.py`도 지원합니다.

## 로컬 설정

- `.env.example`을 `.env`로 복사한 후 `DISCORD_BOT_TOKEN`을 로컬에서 입력합니다. 기존 `.env`는 덮어쓰지 마세요. 환경변수가 있으면 해당 값을 우선합니다.
- Google OAuth 데스크톱 앱의 `credentials.json`을 저장소 루트에 둡니다. 첫 실행 때 브라우저 인증 후 `token.json`이 생성됩니다.
- `config/channels.json`의 `channels`에 Discord 채널 ID와 카테고리를 지정합니다. 지원 카테고리: `common`, `plc`, `robot`, `vision`, `3d_model`, `arduino`, `meeting`, `final`.
- Discord 개발자 설정에서 Message Content Intent를 켜고 봇에 수집 채널 열람 권한을 부여합니다.
- `.env`, `credentials.json`, `token.json`, `.venv`, 수집 파일, 로그, SQLite DB는 로컬 전용이며 Git에서 제외됩니다.

## 처리와 검증 범위

SQLite에는 로컬 저장 직후 기록하며, JSON 메타데이터의 `drive_upload_status`는 `pending`, `failed`, `uploaded`로 바뀝니다. 업로드 실패 시 로컬 파일과 DB, JSON을 보존하고 다음 첨부파일을 처리합니다. Drive 요청은 전용 작업 스레드에서 순차 실행합니다. 봇은 이 저장소에서 한 프로세스만 실행하세요.

실패한 업로드는 30초, 1분, 2분 순서로 간격을 늘려 최대 1시간 간격으로 재시도합니다. 재시도 대기열은 30초마다 확인하므로 실제 시작 시점은 최대 한 주기 늦어질 수 있습니다. DB에 횟수와 다음 시각을 저장해 봇 재시작 후에도 이어서 처리합니다. 토큰 재인증이 필요한 경우 백그라운드에서 브라우저를 열지 않습니다. 로컬에서 `.\.venv\Scripts\python.exe bot/drive.py`로 인증하고 봇을 재시작하세요.

Discord 메시지 ID와 첨부파일 ID 조합으로 같은 첨부를 다시 수집하지 않습니다. Drive 파일 ID를 업로드 전에 DB에 저장하므로 업로드 응답이 유실되거나 업로드 뒤 DB 갱신이 실패해도 같은 ID로 복구합니다. 원격 파일의 해시 표식이 다르거나 휴지통에 있으면 성공으로 처리하지 않습니다. [Google Drive의 사전 생성 ID를 이용한 재시도](https://developers.google.com/workspace/drive/api/guides/manage-uploads#use_a_pre-generated_id_to_upload_files)를 사용합니다.

내용 중복 판정은 기존대로 전체 DB에서 같은 SHA-256이면 `exact_duplicate`, 같은 이름에 다른 내용이면 `new_version`입니다. **다른 메시지에 다시 첨부한 같은 내용의 파일은 별도 기록과 업로드로 유지합니다.** 버전 번호 자동 증가는 아직 없습니다.

## 꺼져 있던 동안의 첨부파일 수집

봇이 시작하거나 Discord에 재연결되면 설정된 채널의 메시지 이력을 오래된 순서대로 확인합니다. 평소에도 60초마다 확인합니다. 채널별 최대 100개씩 읽고, 더 있으면 1초 뒤 다음 묶음을 확인합니다. Discord의 요청 제한에 따라 실제 처리 속도는 느려질 수 있습니다.

- 최초 적용 시 기존 수집 기록이 있는 채널은 마지막 저장 메시지부터 다시 확인합니다. 기록이 전혀 없는 채널은 최초 시작 시점의 최근 24시간부터 확인합니다. 그보다 오래된 전체 이력을 자동으로 가져오지는 않습니다.
- `channel_cursors` 테이블에 마지막 확인 위치를 저장합니다. 이후 재시작 때는 저장된 위치부터 계속하며, 오래 꺼져 있어도 시작 범위를 다시 최근 24시간으로 줄이지 않습니다.
- 새 메시지의 실시간 처리는 이 확인 위치를 앞당기지 않습니다. 복구 중 새 파일이 도착해도 중간의 누락 메시지를 건너뛰지 않습니다.
- 파일 다운로드나 DB 기록에 실패한 메시지에서는 해당 채널의 확인 위치를 멈추고 다음 주기에 재시도합니다. 다른 채널은 계속 처리합니다. 이미 저장한 첨부는 메시지·첨부 ID로 건너뜁니다.
- 로컬 파일과 DB에 기록되면 메시지 확인은 완료입니다. Drive 업로드는 기존 재시도 대기열에서 별도로 처리합니다.
- 봇에는 **채널 보기(View Channel)** 및 **메시지 기록 보기(Read Message History)** 권한과 Message Content Intent가 필요합니다. 권한 오류는 `HISTORY_SCAN_FAILED`, 파일 저장 오류로 멈춘 위치는 `HISTORY_MESSAGE_BLOCKED`로 기록합니다.

삭제된 메시지·첨부파일과 이미 확인한 과거 메시지를 편집해서 나중에 추가한 첨부는 복구하지 않습니다. 수집 대상은 `config/channels.json`에 명시된 채널이며, 스레드와 포럼을 자동 탐색하지 않습니다. 최초 적용 전에 기존 DB를 백업하고, 운영 중인 봇 한 곳에만 갱신하세요.

구현 참고: [discord.py 메시지 이력 API](https://discordpy.readthedocs.io/en/stable/api.html#discord.TextChannel.history).

기존 DB는 첫 실행 때 필요한 열과 인덱스만 추가합니다. 과거 기록 중 Drive ID가 있으면 `uploaded`, 없으면 `needs_review`로 보존합니다. 과거 실패 기록은 사전 생성 ID가 없으므로 원격 생성 여부를 확인하기 전 자동 재업로드하지 않습니다. JSON보다 SQLite 상태가 우선이며 JSON 쓰기 실패 자체로 원격 파일을 다시 만들지는 않습니다.

자동 테스트는 임시 DB와 모의 Drive를 사용하며 실제 Discord/Drive에는 접속하지 않습니다. 실제 인증과 업로드 확인은 봇 실행 후 지정 채널에 테스트 첨부파일을 올려 수행합니다.

## GitHub 저장

코드 저장 전 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`와 `git diff --check`를 실행하고, 변경 파일에 비밀정보와 실행 데이터가 없는지 확인하세요. 필요한 파일만 명시적으로 추가합니다. `.env.example`에는 빈 설정 항목만 둡니다. 가상환경과 실제 수집 자료는 GitHub 코드 백업에 포함되지 않습니다.
