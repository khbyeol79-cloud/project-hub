# 자동 백업과 복원

매일 **한국시간 오전 4시**에 SQLite 수집 기록, DB에 기록된 첨부파일 원본, 채널 설정, 메타데이터와 프로젝트 코드를 백업합니다. Google Drive의 별도 **Project Hub Backups** 폴더에 최근 **14회 성공분**, 라즈베리파이에는 최근 **7회 성공분**을 보관합니다. 전원이 꺼져 예약을 놓쳤다면 다음 부팅 후 실행됩니다.

2026-10-02 19:32(한국시간)에 첫 백업을 완료했습니다. Drive에서 다시 다운로드한 파일의 해시를 검증하고, 별도 폴더에 기록 4건과 첨부파일을 복원했습니다. 백업 관련 자동 테스트 10개가 PC와 라즈베리파이 모두에서 통과했습니다. 다음 예약은 2026-10-03 오전 4시입니다. 현재 수집 봇은 계속 실행 중이며, 기기 전체 재부팅 시험은 아직 하지 않았습니다.

[Google Drive 백업 폴더 열기](https://drive.google.com/drive/folders/1kpEOHQwsKrDsi2aHl01MFjvAzT0KF5W7)

백업 폴더는 수집 파일의 `Project Hub` 폴더와 분리해 만듭니다. 수집 폴더의 공유 설정을 상속하지 않습니다. 기존 수집 파일과 다른 프로그램의 백업은 정리 대상이 아닙니다. Drive에서 오래된 백업은 휴지통으로 이동하고, 라즈베리파이의 오래된 성공 백업은 삭제합니다. 실패한 업로드의 로컬 백업은 성공하기 전에는 삭제하지 않습니다.

## 포함 범위와 보관 위치

- 포함: SQLite DB, DB에 기록된 첨부파일 원본, 그 DB 상태에서 재생성한 JSON 메타데이터, `config/channels.json`, 실행 코드, 의존성 목록, 배포·복원 스크립트.
- 제외: `.env`, `credentials.json`, `token.json`, SSH 키, 가상환경, 로그, 이전 백업, 아직 DB에 등록되지 않은 다운로드 중 파일.
- 라즈베리파이 로컬 백업: `/home/khb/project-hub/logs/backups/`.
- 최근 성공 기록: 같은 폴더의 `last-success.json`. 백업 이름, Drive 파일·폴더 ID, 기록 수, 검증 해시를 담습니다.
- 원본 Drive 업로드가 완료되지 않은 첨부파일도 DB에 기록되어 있다면 백업에 포함됩니다.

봇을 멈추지 않고 SQLite 온라인 백업으로 일관된 DB 사본을 만듭니다. 해당 사본이 가리키는 첨부파일을 해시로 확인하고 JSON 메타데이터를 생성합니다. DB와 무관하게 storage 폴더에 수동으로 넣은 파일이나 JSON만 직접 수정한 내용은 이 백업의 기준이 아닙니다.

백업 ZIP에 파일별 SHA-256 목록이 들어갑니다. 업로드 후 Drive가 계산한 체크섬과 크기를 비교합니다. 업로드 응답이 유실됐을 때는 예약해 둔 동일한 Drive 파일 ID로 확인·재시도해 같은 백업을 중복 생성하지 않습니다.

인증 파일은 이 ZIP에 포함하지 않습니다. 기기를 교체하거나 초기화한 뒤에는 PC에 보관한 인증 파일을 별도로 옮기거나 Google 인증을 다시 해야 합니다. 일상적인 재부팅 때마다 인증할 필요는 없습니다.

## 예약과 수동 실행

기존 수집 서비스와 독립적인 `project-hub-backup.service` 및 `project-hub-backup.timer`를 사용합니다. 백업 실패로 수집 봇을 중지하지 않습니다. 실패 시 5분 간격으로 재시도하며 한 시간 안에 최대 3회 실행을 시도합니다. 그 뒤에도 다음 날 예약은 남습니다.

```bash
# 다음 예약 시각
systemctl list-timers project-hub-backup.timer --no-pager

# 최근 성공 기록
cat /home/khb/project-hub/logs/backups/last-success.json

# 백업을 지금 한 번 실행
sudo systemctl start project-hub-backup.service

# 최근 실행 결과
sudo journalctl -u project-hub-backup.service -n 40 --no-pager

# 일시적으로 예약 중지 / 다시 켜기
sudo systemctl disable --now project-hub-backup.timer
sudo systemctl enable --now project-hub-backup.timer
```

서비스는 백업을 완료하면 종료되는 방식이므로 성공 후 `inactive (dead)`여도 정상입니다. `Result=success`, `ExecMainStatus=0`, `BACKUP_SUCCESS` 기록을 확인합니다. 중복 실행은 잠금으로 막습니다.

새 기기에 등록할 때는 수집 환경을 먼저 설치한 다음 실행합니다. 이 단계는 수집 봇을 재시작하지 않습니다.

```bash
cd /home/khb/project-hub
bash deploy/raspberry-pi/setup-backup.sh
sudo systemctl start project-hub-backup.service
sudo systemctl enable --now project-hub-backup.timer
```

## 운영 데이터를 건드리지 않는 복원 시험

현재 서버에 접근할 수 있다면 최근 Drive 백업을 다시 내려받습니다. 아래 다운로드 파일과 복원 폴더는 아직 존재하지 않는 이름이어야 합니다. 이미 사용한 이름이라면 새 이름으로 바꾸세요.

```bash
cd /home/khb/project-hub
deploy/raspberry-pi/.venv/bin/python deploy/raspberry-pi/backup.py download-latest \
  --destination /home/khb/backup-restore-test.zip

deploy/raspberry-pi/.venv/bin/python deploy/raspberry-pi/backup.py verify \
  /home/khb/backup-restore-test.zip

deploy/raspberry-pi/.venv/bin/python deploy/raspberry-pi/backup.py restore \
  /home/khb/backup-restore-test.zip --destination /home/khb/project-hub-restore-test
```

복원 스크립트는 기존 폴더에 덮어쓰지 않습니다. SQLite 무결성과 첨부파일 해시를 확인하고 새 폴더에 맞게 DB·JSON 경로를 바꿉니다. 복원만으로 봇이나 서비스가 실행되지는 않습니다.

백업 시점 이후에 업로드가 완료됐을 수 있으므로, 복원 DB에서 Drive 예약 ID가 없는 `pending`·`failed` 항목은 `needs_review`로 바꿉니다. 원격 파일 존재 여부를 확인한 뒤 처리해야 중복 업로드를 피할 수 있습니다. 예약 ID가 있는 대기 항목은 같은 ID로 재시도할 수 있도록 보존합니다. 기존 `uploaded`·`needs_review` 상태도 유지합니다.

## 라즈베리파이를 잃어버리거나 저장장치가 고장 난 경우

1. Google Drive에서 **Project Hub Backups** 폴더의 최신 ZIP을 다운로드합니다. 파일 이름의 시각은 UTC입니다.
2. 보관 중인 배포 스크립트 또는 ZIP 안의 `deploy/raspberry-pi/backup.py`를 사용해 새 폴더로 복원합니다. 복원 명령은 Python 3.11 표준 라이브러리만으로 실행할 수 있습니다.
3. `.env`, `credentials.json`, `token.json`을 별도로 준비합니다.
4. 기존 서버의 봇이 중지돼 있는지 확인한 다음, 복원한 프로젝트에서 수집 서비스 설치와 시작 절차를 진행합니다. 같은 이름의 서비스가 이미 등록돼 있다면 정지한 뒤 새 경로로 등록합니다.
5. 수집 기록과 Drive 업로드를 확인하고 백업 서비스를 다시 등록합니다. 새 설치는 새 백업 식별자를 사용하므로 이전 서버의 백업을 자동 삭제하지 않습니다.

수집 파일이 커지면 매일 전체 첨부파일을 포함한 백업 크기도 커집니다. 현재 방식은 간단한 전체 백업이며 증분 백업은 아닙니다.

설계 참고: [Python SQLite 온라인 백업](https://docs.python.org/3.11/library/sqlite3.html#sqlite3.Connection.backup), [Drive 예약 ID를 이용한 업로드 재시도](https://developers.google.com/workspace/drive/api/guides/manage-uploads#use_a_pre-generated_id_to_upload_files), [Drive 체크섬·파일 속성](https://developers.google.com/workspace/drive/api/reference/rest/v3/files).
