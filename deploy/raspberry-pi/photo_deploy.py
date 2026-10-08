"""One-time photo setup followed by the existing explicit commit deployment."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import phone_deploy as deploy


class SetupError(RuntimeError):
    pass


def command(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def folder_setting(text, folder_id):
    """Replace only the requested setting, preserving unrelated configuration."""
    pattern = re.compile(r'^\s*(?:export\s+)?PROJECT_HUB_PHOTO_FOLDER_ID\s*=.*$', re.M)
    line = 'PROJECT_HUB_PHOTO_FOLDER_ID=' + folder_id
    if pattern.search(text):
        return pattern.sub(line, text)
    return text + ('' if not text or text.endswith('\n') else '\n') + line + '\n'


def validate_connection(root, config, folder_id):
    # Use the existing bot environment. Do not copy credentials into the checkout,
    # open an OAuth browser on the server, or rewrite either authorization token.
    code = '''
import os, sys
from pathlib import Path
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
root, config, folder = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
load_dotenv(root / '.env', override=False)
existing = os.environ.get('PROJECT_HUB_PHOTO_FOLDER_ID', '')
if existing and existing != folder:
    print('운영 .env 또는 서비스 환경의 사진 폴더 설정이 다릅니다. 기존 값을 먼저 확인하세요.')
    raise SystemExit(2)
load_dotenv(config, override=False)
token = Path(os.environ.get('PROJECT_HUB_PHOTO_TOKEN', str(config.parent / 'photos-token.json'))).expanduser()
if not token.is_file():
    print('사진용 Google 인증이 한 번 필요합니다. --upload로 만든 photos-token.json을 ~/.config/project-hub/에 업로드한 뒤 같은 명령을 다시 실행하세요.')
    raise SystemExit(2)
try:
    creds = Credentials.from_authorized_user_file(token)
    if not creds.has_scopes(['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/drive.file']):
        print('사진 토큰에 추가 Google 동의가 필요합니다. organizer.photos --upload로 다시 인증하세요.')
        raise SystemExit(2)
    if not creds.valid:
        if not creds.refresh_token:
            raise ValueError('No refresh token')
        creds.refresh(Request())
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    try:
        item = service.files().get(fileId=folder, fields='mimeType,trashed,capabilities(canAddChildren)', supportsAllDrives=True).execute()
        if item.get('trashed') or item.get('mimeType') != 'application/vnd.google-apps.folder' or not item.get('capabilities', {}).get('canAddChildren'):
            raise ValueError('Folder unavailable')
    finally:
        service.close()
except Exception:
    print('Google 사진 인증 또는 폴더 접근 확인에 실패했습니다. 계정 권한과 인증을 확인하세요.')
    raise SystemExit(2)
'''
    try:
        command(str(root / 'deploy/raspberry-pi/.venv/bin/python'), '-c', code,
                str(root), str(config), folder_id)
    except subprocess.CalledProcessError:
        raise SetupError('사진 연결 준비가 필요합니다. 운영 설정과 서비스는 변경하지 않았습니다.') from None


def prepare_and_apply(checkout, root, folder_id, home):
    config = home / '.config/project-hub/library.env'
    if config.is_symlink():
        raise SetupError('library.env 심볼릭 링크는 자동 수정하지 않습니다.')
    validate_connection(root, config, folder_id)
    # Dedicated dependency plan: back up installed web packages, then install the
    # reviewed pinned web requirements. No bot environment or DB schema changes.
    command('bash', 'scripts/cloud-setup.sh', cwd=checkout)
    state = home / '.local/state/project-hub-deploy'
    state.mkdir(parents=True, exist_ok=True)
    backup = state / ('photos-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()))
    backup.mkdir(mode=0o700)
    web_python = home / '.local/share/project-hub-library/venv311/bin/python'
    with (backup / 'web-packages-before.txt').open('w') as stream:
        command(str(web_python), '-m', 'pip', 'freeze', stdout=stream)
    print('사진 준비 백업:', backup, flush=True)
    command(str(web_python), '-m', 'pip', 'install', '-r',
            str(checkout / 'organizer/requirements-web.txt'))
    old = config.read_bytes() if config.exists() else None
    if old is not None:
        (backup / 'library.env').write_bytes(old)
    new = folder_setting((old or b'').decode('utf-8'), folder_id).encode('utf-8')
    config.parent.mkdir(parents=True, exist_ok=True)
    code_changed = any(not deploy.same_code(root / name, checkout / name)
                       for name in deploy.run('git', 'ls-files', '-z', cwd=checkout,
                                              capture=True).split('\0') if deploy.allowed(name))
    changed = old != new
    temp = config.with_name('library.env.photos-' + str(os.getpid()))
    try:
        if changed:
            # Exclusive creation and restrictive umask also protect backup files.
            with temp.open('xb') as stream:
                stream.write(new)
            temp.replace(config)
        command(str(checkout / '.venv/bin/python'),
                str(checkout / 'deploy/raspberry-pi/phone_deploy.py'),
                '--root', str(root), '--apply')
        if not code_changed:
            command('sudo', 'systemctl', 'restart', *deploy.SERVICES)
            deploy.healthy()
    except BaseException:
        if changed and config.is_file() and config.read_bytes() == new:
            if old is None:
                config.unlink()
            else:
                with temp.open('xb') as stream:
                    stream.write(old)
                temp.replace(config)
        # A failed code deployment may already have restarted the old code with
        # the temporary setting. Reload the restored configuration too.
        if changed:
            try:
                command('sudo', 'systemctl', 'restart', *deploy.SERVICES)
            except Exception:
                print('설정은 복구했습니다. 두 서비스의 상태를 확인하세요.', flush=True)
        print('사진 적용 실패. 설정/패키지 백업: ' + str(backup), flush=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
    print('사진 설정과 배포 완료. 허브의 기존 → 사진에서 새로고침하세요.')
    print('설정/패키지 백업:', backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder-id', required=True)
    parser.add_argument('--root', type=Path, default=Path.home() / 'project-hub')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() == 0 or sys.version_info[:2] != (3, 11):
        parser.error('Pi 일반 사용자로 Python 3.11을 사용하세요.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', args.folder_id):
        parser.error('올바른 사진 폴더 ID를 지정하세요.')
    os.umask(0o077)
    checkout = Path(__file__).resolve().parents[2]
    if deploy.run('git', 'status', '--porcelain', cwd=checkout, capture=True).strip():
        parser.error('선택한 커밋의 깨끗한 개발 체크아웃에서 실행하세요.')
    root = args.root.resolve()
    state = Path.home() / '.local/state/project-hub-deploy'
    state.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (state / 'photo-setup.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            prepare_and_apply(checkout, root, args.folder_id, Path.home())
        except SetupError as exc:
            parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
