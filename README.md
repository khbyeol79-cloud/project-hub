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
- `config/channels.json`의 `channels`에는 일반 채팅, `forums`에는 포럼 ID와 카테고리를 지정합니다. 현재 Drive 폴더는 `hello⭐`, `기구제작🎨`, `pc💻`, `plc🛠️`, `게시물`입니다. 기존 기록과의 호환성을 위해 내부 키 `common`, `3d_model`, `vision`, `plc`, `meeting`은 유지합니다.
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

삭제된 메시지·첨부파일과 이미 확인한 과거 메시지를 편집해서 나중에 추가한 첨부는 복구하지 않습니다. 최초 적용 전에 기존 DB를 백업하고, 운영 중인 봇 한 곳에만 갱신하세요.

`forums`에 등록한 포럼은 접근 가능한 활성·보관 게시글을 모두 탐색합니다. 게시글별 확인 위치를 따로 저장하고 처음 발견한 게시글은 처음 메시지부터 확인합니다. 각 게시글에서 한 번에 최대 100개 메시지를 읽고 다음 주기에 이어갑니다. 실시간으로 먼저 받은 첨부도 이 확인 위치를 앞당기지 않습니다. 보관된 게시글이 많으면 Discord 요청 제한에 따라 전체 확인이 오래 걸릴 수 있습니다. 설정하지 않은 포럼, 일반 채팅의 하위 스레드, 음성 채널은 수집하지 않습니다. 포럼 이름으로 업로드와 상태 집계를 묶으며 원래 게시글·메시지 링크는 기록에 보존합니다.

현재 채널 연결:

| Discord 채널 | Drive 폴더 | 종류 |
| --- | --- | --- |
| hello⭐ | hello⭐ | 일반 채팅 |
| 기구제작🎨 | 기구제작🎨 | 일반 채팅 |
| pc💻 | pc💻 | 일반 채팅 |
| plc🛠️ | plc🛠️ | 일반 채팅 |
| 게시물 | 게시물 | 포럼 게시글 |

기존 Drive 폴더의 이름을 바꾸어 파일 ID와 링크를 유지합니다. 사용하지 않는 옛 분류는 `Project Hub/_이전 분류`에 보존합니다. 이 이름 연결은 현재 서버 구성 기준이며, 이후 Discord 채널명을 바꾸면 Drive 이름과 코드 설정도 함께 갱신해야 합니다.

구현 참고: [discord.py 메시지 이력 API](https://discordpy.readthedocs.io/en/stable/api.html#discord.TextChannel.history).

기존 DB는 첫 실행 때 필요한 열과 인덱스만 추가합니다. 과거 기록 중 Drive ID가 있으면 `uploaded`, 없으면 `needs_review`로 보존합니다. 과거 실패 기록은 사전 생성 ID가 없으므로 원격 생성 여부를 확인하기 전 자동 재업로드하지 않습니다. JSON보다 SQLite 상태가 우선이며 JSON 쓰기 실패 자체로 원격 파일을 다시 만들지는 않습니다.

자동 테스트는 임시 DB와 모의 Drive를 사용하며 실제 Discord/Drive에는 접속하지 않습니다. 실제 인증과 업로드 확인은 봇 실행 후 지정 채널에 테스트 첨부파일을 올려 수행합니다.

## 수집 상태와 선택적 장애 알림

Discord 알림은 기본적으로 꺼져 있습니다. 서버에서 다음 명령으로 상태를 확인할 수 있습니다. 결과에는 업로드 완료·대기·실패·과거 확인 필요 건수, 채널별 마지막 로컬 기록 시각과 이력 확인 시각을 표시합니다. 시각은 UTC이며 한국 시간은 9시간을 더합니다. 파일 내용·파일명·인증정보는 표시하지 않습니다.

```bash
# 라즈베리파이 프로젝트 폴더에서 실행
deploy/raspberry-pi/.venv/bin/python -m bot.status
# 같은 결과를 JSON으로 확인
deploy/raspberry-pi/.venv/bin/python -m bot.status --json
```

Windows에서는 `.\.venv\Scripts\python.exe -m bot.status`를 사용합니다. 상태 조회 전에 최신 봇을 한 번 실행해 상태 테이블을 생성해야 합니다.

나중에 알림을 켜려면 로컬 `.env`에 `DISCORD_ALERT_CHANNEL_ID=관리채널ID`를 설정하고 봇을 재시작하세요. 빈 값이면 자동 알림과 Discord 상태 명령이 모두 꺼집니다. 지정 채널에서 `!hub 상태` 또는 `!hub status`를 입력하면 상태를 보여줍니다. 응답은 30초에 한 번으로 제한하며 과거 메시지 복구 중에는 명령에 응답하지 않습니다.

알림은 60초마다 확인합니다. 업로드 3회 이상 실패, 이력 확인·재시도 대기열 처리 3회 연속 실패 시 장애 알림을 보냅니다. Google 재인증 필요 오류는 첫 실패부터 알립니다. 동일 장애는 재시작해도 반복 통지하지 않고, 해소되면 복구 알림을 한 번 보냅니다. 전송 실패 시 다음 주기에 다시 시도합니다. Discord 전송 직후 응답 유실이나 DB 저장 실패 같은 불확실한 상황에서는 알림이 중복될 수 있습니다. 멘션은 사용하지 않습니다.

봇·서버 자체가 완전히 꺼지거나 Discord에 연결하지 못하면 이 봇으로 알림을 보낼 수 없습니다. 알림 채널 전송 권한 문제는 서버 로그의 `HEALTH_ALERT_FAILED`로 확인하세요. 조용한 채널에 새 파일이 없다는 이유만으로 장애로 판단하지 않습니다.

## GitHub 저장

코드 저장 전 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`와 `git diff --check`를 실행하고, 변경 파일에 비밀정보와 실행 데이터가 없는지 확인하세요. 필요한 파일만 명시적으로 추가합니다. `.env.example`에는 빈 설정 항목만 둡니다. 가상환경과 실제 수집 자료는 GitHub 코드 백업에 포함되지 않습니다.

## Drive 사진 갤러리

자료실 상단 **사진** 메뉴에서 `Project Hub/사진`을 확인합니다.
`사진공유` Discord 채널의 기존·새 사진도 동일 폴더로 자동 수집하도록 연결했습니다.
사진 인증과 웹 의존성 설치/배포 절차는 [organizer/PHOTOS.md](organizer/PHOTOS.md)를 따르세요.
