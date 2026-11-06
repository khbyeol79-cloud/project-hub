# Discord → 공유 드라이브 보관 봇

지정한 일반 텍스트 채널 한 곳의 새 메시지와 첨부를 다른 Project Hub 서비스와 독립적으로 보관합니다.
Drive에 `채널명 [ID] / 연월 / 내용·이미지·동영상·음성·문서·압축파일·기타` 폴더를 만듭니다.
`내용`의 TXT에는 작성자, 한국 시간, 원문 링크, 본문과 저장한 첨부파일 링크가 들어갑니다.

첫 연결 시에는 봇 시작 이후 메시지부터 확인합니다. 처리 위치는 Drive의 채널 폴더에 기록하므로,
재시작하면 그 위치부터 읽습니다. 파일은 메모리에서 한 개씩 전송하고 로컬 파일·자료실 DB·검색에
등록하지 않습니다. 이 보관본은 수집 시점의 사본입니다. 이후 원문 수정이나 삭제는 동기화하지 않습니다.

첨부파일은 기본 64 MiB까지 업로드합니다. Pi 메모리에 여유가 있다면 `ARCHIVE_MAX_ATTACHMENT_MIB`로
최대 128 MiB까지 설정할 수 있습니다. 한도를 넘는 파일은 TXT에 미저장 사실과 원문 위치를 남깁니다.
다운로드나 업로드에 실패하면 마지막 완료 위치에서 다시 시도합니다. 같은 보관 봇은 한 프로세스만 실행하세요.

## 운영 설정

1. 새 Discord 봇을 만들고, 대상 서버에 초대합니다. 개발자 포털에서 **Message Content Intent**를 켜고,
   대상 채널의 **채널 보기**, **메시지 기록 보기** 권한을 허용합니다. 이 봇은 메시지를 게시하지 않습니다.
2. 대상 공유 드라이브 안의 폴더 ID와, 그 폴더를 보고 파일을 추가할 수 있는 Google 사용자 OAuth
   토큰 파일을 준비합니다. 토큰에는 `refresh_token`이 있어야 합니다. 기존 인증이 대상 폴더에 접근할 수
   있는지 확인한 뒤 재사용할 수 있습니다. 인증 파일과 봇 토큰은 GitHub나 채팅에 올리지 않습니다.
3. 검토한 커밋을 `/home/khb/discord-drive-archive`의 별도 체크아웃에 받고 Python 3.11 환경을
   `bash scripts/cloud-setup.sh`로 준비합니다. 기존 운영 Project Hub를 배포하거나 재시작할 필요는 없습니다.
4. `/home/khb/.config/discord-drive-archive/bot.env`를 권한 `0600`으로 만들고 실제 값을 설정합니다.
   서비스 템플릿의 사용자·경로는 Pi에 맞게 조정하세요.

```ini
ARCHIVE_DISCORD_TOKEN=
ARCHIVE_CHANNEL_ID=
ARCHIVE_DRIVE_FOLDER_ID=
ARCHIVE_DRIVE_TOKEN_FILE=/home/khb/.config/discord-drive-archive/google-token.json
ARCHIVE_MAX_ATTACHMENT_MIB=64
```

```bash
sudo install -m 644 drive_archive/discord-drive-archive.service /etc/systemd/system/discord-drive-archive.service
sudo systemctl daemon-reload
sudo systemctl enable --now discord-drive-archive.service
sudo journalctl -u discord-drive-archive.service -n 30 --no-pager
```

지정 채널에 테스트 글과 작은 첨부를 올려 TXT의 본문·원문·첨부 링크와 Drive 원본이 맞는지 확인합니다.
자료를 복사하는 대상 폴더의 열람자는 해당 채널 내용을 열람해도 되는 사람으로 설정합니다.
