# 라즈베리파이 상시 실행 준비

Project Hub의 Discord 첨부파일 수집 봇을 로그인이나 브라우저와 관계없이 실행하는 설정입니다.

2026-10-02에 실제 라즈베리파이(Debian 13, ARM64)의 `/home/khb/project-hub`에 설치하고 운영을 전환했습니다. **라즈베리파이 서비스는 실행 중이며 부팅 시 자동 시작이 켜져 있고, PC 봇은 중지했습니다.**

- Python 3.11.17, 봇 테스트 19개와 배포 테스트 11개, 서비스·로그 정리 구문 검사를 통과했습니다.
- Discord 채널 2개 접근, Google Drive 인증, 실제 봇의 Discord 연결을 확인했습니다.
- 서비스를 정상 재시작한 뒤 사람의 인증 입력 없이 다시 연결되는 것을 확인했습니다. 라즈베리파이 기기 전체의 재부팅 시험은 하지 않았습니다.
- 수집 기록 4건과 storage 파일 11개(메타데이터 포함)를 옮기고 첨부파일 4건의 SHA-256 및 DB 무결성을 확인했습니다. DB·메타데이터의 PC 경로는 라즈베리파이 경로로 바꿨습니다.
- 기존 업로드 완료 2건과 검토 필요 2건의 상태를 보존했습니다. 검토 필요 항목을 임의로 재업로드하지 않았습니다.
- PC 원본 파일과 DB는 남아 있습니다. 앞으로 새 파일은 라즈베리파이에서 수집되므로 PC 봇을 동시에 다시 실행하지 마세요. PC 원본은 전환 시점의 사본이며 이후 자료까지 자동 동기화되지 않습니다.

아래 설치 절차는 재설치·환경 복구 시 사용하는 안내입니다. 현재 운영 상태에서는 다시 설치할 필요가 없습니다. 새 Discord 첨부파일의 실제 수집·Drive 업로드 확인은 테스트 파일 하나를 지정 채널에 올려 확인할 수 있습니다.

## 구성

- `setup.sh`: 라즈베리파이용 Python 3.11 가상환경 생성, 의존성 설치, 로컬 설정 점검, 서비스·로그 정리 설정 등록.
- `service_runner.py`: 인증 파일과 채널 설정을 먼저 확인하고 기존 봇 실행. 비밀 값은 출력하지 않습니다.
- `project-hub.service.in`: 자동 실행 설정 원본. 설치할 때 실제 사용자와 프로젝트 경로가 채워집니다.
- `project-hub.logrotate.in`: 봇 파일 로그를 매일 확인하고 오래된 로그 최대 7개 보관.
- `test_service_runner.py`: 가짜 인증 자료를 사용하는 오프라인 테스트.

설치 파일은 봇 코드를 변경하지 않습니다. Python 환경은 `deploy/raspberry-pi/.venv`에 따로 만들며, PC의 `.venv`를 재사용하지 않습니다. 현재 프로젝트의 `.gitignore`가 이 가상환경도 제외합니다.

## 1. 라즈베리파이에 코드와 설정 준비

기존 라즈베리파이 계정은 `khb`입니다. 프로젝트 경로 예시는 `/home/khb/project-hub`이며 실제 경로는 설치 스크립트가 자동으로 찾습니다. 프로젝트 경로에는 영문·숫자·밑줄·하이픈을 사용하세요.

**다른 채팅에서 수정 중인 최신 PC 코드를 옮긴 뒤 사용하세요.** 아직 GitHub에 올라가지 않은 변경은 `git clone`이나 `git pull`만으로 받을 수 없습니다. `bot/discord_bot.py`에 `main()` 실행 함수가 있는 버전이 필요합니다.

필요한 폴더 구조:

```text
project-hub/
  bot/                         # 최신 discord_bot.py, database.py, drive.py
  config/channels.json
  requirements.txt
  .env                         # 로컬 전용
  credentials.json             # 로컬 전용, Google Desktop OAuth 클라이언트
  token.json                   # 로컬 전용, PC에서 인증 완료 후 생성
  deploy/raspberry-pi/          # 이 폴더 전체
```

PC의 `.venv`는 복사하지 않습니다. `.env`, `credentials.json`, `token.json`은 개인 SSH/SCP 연결이나 인증된 code-server 파일 전송으로 옮기고 Git에는 넣지 마세요. 인증 파일 내용을 채팅에 붙여 넣을 필요는 없습니다.

라즈베리파이 서비스는 브라우저 인증을 진행할 수 없으므로, PC에서 Drive 인증을 마친 `token.json`이 먼저 있어야 합니다. 토큰에는 재발급용 `refresh_token`과 `drive.file` 권한이 필요합니다. 설치 점검은 파일 형식만 확인하며, 실제 토큰 유효성은 봇 시작 후 확인합니다.

기존 수집 이력도 이전하려면 PC 봇을 정지한 상태에서 `project_hub.db`와 `storage/`를 함께 옮기세요. 이 설치 과정은 기존 이력을 자동으로 이전하지 않습니다. DB·JSON의 `local_path`에는 Windows 경로가 남을 수 있어 기존 파일 조회 기능에서 별도 경로 변환이 필요합니다.

## 2. 설치 준비와 등록

라즈베리파이의 터미널 또는 code-server 터미널에서 실행합니다. 다음 명령의 프로젝트 경로는 실제 위치에 맞추세요.

```bash
cd /home/khb/project-hub
command -v python3.11
command -v uv
command -v logrotate
```

Python 3.11 또는 `uv` 중 하나가 있으면 됩니다. `uv`가 있으면 설치 스크립트가 Python 3.11을 내려받을 수 있습니다. 둘 다 없으면 [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에 따라 사용자 계정에 uv를 설치하고 터미널을 다시 연 뒤 진행하세요. OS 기본 Python 버전을 바꾸지 않습니다. Python 3.11의 `venv` 모듈이 빠진 OS에서는 해당 배포판의 Python 3.11 venv 패키지를 먼저 설치하세요.

`logrotate`가 없다면:

```bash
sudo apt update
sudo apt install logrotate
```

서비스 등록:

```bash
bash deploy/raspberry-pi/setup.sh
```

**스크립트 앞에 `sudo`를 붙이지 않습니다.** 필요한 서비스 등록 작업에만 내부적으로 sudo를 사용합니다. 현재 로그인한 일반 사용자로 봇이 실행됩니다. 기존 같은 이름의 서비스가 실행 중이면 먼저 멈추라는 안내가 나옵니다. 이전 서비스·로그 설정은 `/var/backups/project-hub/`에 보관합니다.

설치는 설정 파일 누락이나 잘못된 채널 분류를 발견하면 등록 전에 멈춥니다. 문제를 수정하고 같은 명령을 다시 실행하면 됩니다. 서비스 등록 성공 메시지는 로그인·업로드 성공을 의미하지 않습니다.

등록은 봇을 시작하거나 자동 시작을 새로 켜지 않습니다. 기존 서비스의 활성화 설정은 유지됩니다. 다른 PC에서 같은 봇이 돌고 있다면 다음 단계 전에 그 실행을 중지하세요. 두 기기에서 동시에 수집하면 같은 첨부파일이 중복 저장될 수 있습니다.

## 3. 상시 실행 켜기와 확인

```bash
sudo systemctl enable --now project-hub.service
sudo systemctl status project-hub.service --no-pager
sudo journalctl -u project-hub.service -n 60 --no-pager
```

실행 기록에 `Discord 로그인 완료`가 나타나는지 확인하고, 지정 채널에 작은 테스트 파일 하나를 올립니다. 이후 `FILE_SAVED` 기록, 로컬 `storage/` 파일, Google Drive 대상 폴더의 파일을 모두 확인하세요. `active (running)` 표시만으로 로그인이나 업로드 성공을 판단하지 않습니다.

부팅 후 자동 실행 여부:

```bash
sudo systemctl is-enabled project-hub.service
```

나중에 다른 작업을 저장하고 라즈베리파이를 재부팅한 뒤 동일한 상태·로그 명령으로 재확인하세요. 이 안내나 설치 스크립트는 자동으로 재부팅하지 않습니다.

## 운영 명령

자동 백업의 예약·보관·복원 방법은 [BACKUP.md](BACKUP.md)를 확인하세요.

```bash
# 최근 기록 / 실시간 기록 (Ctrl+C는 기록 보기만 종료)
sudo journalctl -u project-hub.service -n 80 --no-pager
sudo journalctl -u project-hub.service -f

# 중지 / 다시 시작
sudo systemctl stop project-hub.service
sudo systemctl restart project-hub.service

# 자동 시작 해제와 중지
sudo systemctl disable --now project-hub.service

# 네트워크 연결 없이 설정 파일만 점검
deploy/raspberry-pi/.venv/bin/python deploy/raspberry-pi/service_runner.py --check

# 로그 정리 예약 확인 (배포판에 따라 timer 대신 cron 사용)
systemctl status logrotate.timer --no-pager
```

코드를 갱신할 때는 서비스를 정지하고, 기존 파일을 보존하면서 코드를 갱신한 뒤 설치 스크립트를 다시 실행하고 서비스를 시작하세요. 실행 중인 봇의 가상환경에 패키지를 설치하지 않습니다.

## 복구 동작과 범위

- 봇 프로세스가 종료되면 30초 뒤 다시 실행합니다. 관리자가 `systemctl stop`으로 중지한 경우에는 자동 재시작하지 않습니다.
- 부팅 때 네트워크 준비 이후 시작하도록 설정합니다. 이 순서만으로 인터넷 연결이 보장되지는 않습니다. 시작 중 일시적인 통신 오류로 종료되면 다시 시도합니다.
- 인증 파일 누락·설정 오류·잘못된 Discord 인증, 시작 과정에서 서비스 실행기로 전달된 영구 Google 인증 오류는 종료 코드 78로 멈춥니다. 원인을 수정한 뒤 `systemctl restart`로 재개합니다. 최신 봇이 업로드 작업 안에서 처리한 Google 인증 오류는 업로드 실패 기록과 재시도 대기열에 남으므로, 서비스가 실행 중이더라도 Drive 업로드 오류 기록을 확인해야 합니다.
- Google 인증 화면이 '외부 / 테스트' 상태이고 Drive 권한을 사용하면 재발급 토큰이 7일 후 만료될 수 있습니다. Google Auth Platform의 게시 상태를 확인하고 본인의 사용 방식에 맞춰 설정하세요. 재부팅이나 자동 재시작은 만료된 인증을 고치지 못합니다. [Google 공식 설명](https://developers.google.com/identity/protocols/oauth2#expiration)
- 실행 중 네트워크 재연결은 기존 Discord 라이브러리가 담당합니다. 이 서비스는 살아 있지만 멈춘 프로세스를 감지하는 별도 감시 기능까지 제공하지 않습니다.
- **개별 파일 재전송·중복 처리·과거 메시지 수집은 기존 봇이 담당하는 기능입니다.** 다른 채팅에서 재시도와 중복 처리 기능을 수정 중이므로 최종 봇 버전의 설명과 테스트 결과를 확인하세요. 이 배포 설정 자체는 해당 기능을 추가하지 않습니다. 서비스 재시작과 파일 재전송은 다른 기능입니다.
- 실행 파일 로그는 logrotate의 예약 실행 시 정리됩니다. 20MB 기준은 확인 시점에 적용되므로 정확한 용량 상한이 아닙니다. logrotate timer 또는 cron이 활성화돼 있어야 합니다. `copytruncate` 방식은 정리 순간의 일부 로그가 유실될 수 있습니다. journald 보관 정책은 OS의 기존 설정을 사용합니다.
- 파일 수집용 포트나 공유기 설정을 추가로 열 필요는 없습니다. 기존 code-server·Caddy·Authelia 설정은 설치 대상에 포함되지 않습니다.

## 검증

가짜 설정을 사용한 오프라인 검사:

```bash
deploy/raspberry-pi/.venv/bin/python -m unittest discover -s deploy/raspberry-pi -p 'test_*.py' -v
```

설치 스크립트는 실제 라즈베리파이에서 서비스 구문과 로그 정리 구문도 검사합니다. PC에서의 준비 검증과 라즈베리파이의 실제 부팅·인증·수집 시험은 구분해야 합니다.

설계 참고: [systemd 서비스 공식 문서](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml), [logrotate 공식 문서](https://github.com/logrotate/logrotate/blob/main/logrotate.8.in), [uv Python 설치](https://docs.astral.sh/uv/guides/install-python/).
