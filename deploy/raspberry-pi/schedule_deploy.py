"""Download one explicitly selected Drive workbook privately, then apply reviewed code."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT))
from dotenv import dotenv_values
from googleapiclient.errors import HttpError
from organizer.photos import photo_service, PhotoError
from organizer.schedule import parse_schedule, ScheduleError


class SetupError(RuntimeError):
    pass


def download_source(file_id, token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', file_id):
        raise SetupError('Drive 파일 ID를 확인해 주세요.')
    service = photo_service(token)
    files = service.files()
    try:
        meta = files.get(fileId=file_id, fields='name,mimeType,size,trashed', supportsAllDrives=True).execute()
    except HttpError as exc:
        raise SetupError('Drive 일정표를 읽을 수 없습니다 ('+str(exc.resp.status)+'). 사진 인증 계정의 파일 접근 권한을 확인해 주세요.') from None
    if (meta.get('trashed') or meta.get('mimeType') != 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            or not 0 < int(meta.get('size', 0)) <= 8 * 1024 * 1024):
        raise SetupError('8MB 이하의 원본 Excel 일정표를 선택해 주세요.')
    if not isinstance(meta.get('name'), str) or not 0 < len(meta['name']) <= 1000:
        raise SetupError('Drive 일정표 이름을 확인해 주세요.')
    try:
        data = files.get_media(fileId=file_id, supportsAllDrives=True).execute()
    except HttpError as exc:
        raise SetupError('Drive 일정표 다운로드 실패 ('+str(exc.resp.status)+'). 파일 읽기 권한을 확인해 주세요.') from None
    if not isinstance(data, bytes) or not 0 < len(data) <= 8 * 1024 * 1024:
        raise SetupError('Drive 일정표 다운로드 결과를 확인해 주세요.')
    return meta['name'], data


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix='schedule-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def prepare_and_apply(checkout, root, home, file_id):
    if not (root/'organizer/web.py').is_file():
        raise SetupError('운영 Project Hub 폴더를 확인해 주세요.')
    config = home/'.config/project-hub/library.env'
    settings = dotenv_values(config) if config.is_file() else {}
    token = Path(settings.get('PROJECT_HUB_PHOTO_TOKEN') or home/'.config/project-hub/photos-token.json').expanduser()
    name, data = download_source(file_id, token)
    directory = home/'.config/project-hub/schedule'
    if directory.is_symlink():
        raise SetupError('일정 저장 폴더의 심볼릭 링크는 자동 변경하지 않습니다.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = directory/'main.json'
    if manifest.is_symlink():
        raise SetupError('일정 설정의 심볼릭 링크는 자동 변경하지 않습니다.')
    sha = hashlib.sha256(data).hexdigest()
    path = directory/(sha+'.xlsx')
    if path.is_symlink():
        raise SetupError('일정표의 심볼릭 링크는 자동 변경하지 않습니다.')
    # Validate before replacing the active pointer or touching services.
    fd, temporary = tempfile.mkstemp(suffix='.xlsx', dir=directory)
    candidate = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        schedule = parse_schedule(candidate)
        if path.exists():
            if path.read_bytes() != data:
                raise SetupError('기존 일정 파일의 체크섬을 확인해 주세요.')
        else:
            candidate.replace(path)
    finally:
        candidate.unlink(missing_ok=True)
    state = home/'.local/state/project-hub-deploy'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = state/('schedule-'+time.strftime('%Y%m%d-%H%M%S')+'-'+str(os.getpid()))
    backup.mkdir(mode=0o700)
    old = manifest.read_bytes() if manifest.exists() else None
    if old is not None:
        (backup/'main.json').write_bytes(old)
    new = json.dumps({'name': name, 'file_id': file_id, 'sha256': sha}, ensure_ascii=False).encode('utf-8')
    print('Drive 일정표 내려받기·변환 완료:', schedule['teaching_days'], '일 /', schedule['teaching_periods'], '교시', flush=True)
    try:
        atomic_write(manifest, new)
        subprocess.run([str(checkout/'.venv/bin/python'), str(checkout/'deploy/raspberry-pi/phone_deploy.py'),
                        '--root', str(root), '--apply'], cwd=checkout, check=True)
        with urllib.request.urlopen('http://127.0.0.1:8090/library/api/schedule', timeout=20) as response:
            result = json.load(response)
        if not result.get('available') or result.get('source') != name or result.get('days') != schedule['days']:
            raise SetupError('자료실의 일정 연결 확인에 실패했습니다.')
    except BaseException:
        if manifest.exists() and manifest.read_bytes() == new:
            if old is None:
                manifest.unlink()
            else:
                atomic_write(manifest, old)
        print('일정 연결 실패. 이전 일정 설정 복구. 백업:', backup, flush=True)
        raise
    print('일정 연결과 적용 완료. 자료실에서 새로고침하세요.')
    print('일정 설정 백업:', backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file-id', required=True)
    parser.add_argument('--root', type=Path, default=Path.home()/'project-hub')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() == 0:
        parser.error('일반 Pi 로그인 사용자로 실행하세요.')
    os.umask(0o077)
    state = Path.home()/'.local/state/project-hub-deploy'
    state.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (state/'schedule.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            prepare_and_apply(CHECKOUT, args.root.resolve(), Path.home(), args.file_id)
        except Exception as exc:
            # Avoid printing OAuth responses, tokens, or private HTTP URLs in tracebacks.
            reason = str(exc) if isinstance(exc, (SetupError, ScheduleError, PhotoError)) else type(exc).__name__
            print('일정 연결을 완료하지 못했습니다:', reason, file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
