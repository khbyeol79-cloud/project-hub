"""Local administrator CLI. Not a public or Discord authorization boundary."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from dotenv import load_dotenv
from .ai import AIError, Organizer, Settings


def read_document(db_path, file_id, max_chars=12000):
    """Read one consistent snapshot of the existing index; never mutate bot tables."""
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('BEGIN')
            row = db.execute('''SELECT f.original_filename, f.sha256, d.source_sha256, d.status
                                FROM files f JOIN content_documents d ON d.file_id=f.id WHERE f.id=?''', (file_id,)).fetchone()
            if not row or row[1] != row[2] or row[3] not in ('indexed', 'partial'):
                raise AIError('이 파일의 최신 검색용 텍스트가 아직 준비되지 않았습니다.')
            pages = db.execute('SELECT page, body FROM content_pages WHERE file_id=? ORDER BY page', (file_id,))
            sources, count, partial = [], 0, row[3] == 'partial'
            for page, body in pages:
                if not body.strip():
                    continue
                remaining = max_chars - count
                if remaining <= 0 or len(sources) >= 24:
                    partial = True
                    break
                text = body[:remaining]
                partial |= len(text) < len(body)
                sources.append({'text': text, 'file_id': file_id, 'page': page,
                                'label': f'{row[0]} · 추출 구간 {page}'})
                count += len(text)
            if not sources:
                raise AIError('읽을 수 있는 문서 내용이 없습니다.')
            return sources, partial
    except sqlite3.Error:
        raise AIError('자료 DB 또는 검색 인덱스를 읽지 못했습니다.') from None


def main():
    parser = argparse.ArgumentParser(description='관리자용 자료 요약·질문 (선택한 문서 텍스트를 Google Cloud AI로 전송)')
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/project-hub/ai.env')
    parser.add_argument('--db', type=Path, default=Path('project_hub.db'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    sub.add_parser('smoke', help='가상 자료만 사용하는 실제 AI 연결 확인')
    for command in ('summary', 'ask'):
        p = sub.add_parser(command)
        p.add_argument('--file-id', type=int, required=True)
        if command == 'ask':
            p.add_argument('--question', required=True)
    args = parser.parse_args()
    try:
        load_dotenv(args.config, override=False)
        client = Organizer(Settings.from_env())
        if args.command == 'status':
            result = dict(client.ledger.counts(), enabled=client.settings.enabled, model=client.settings.model,
                          daily_limit=client.settings.daily_limit, monthly_limit=client.settings.monthly_limit)
        elif args.command == 'smoke':
            result = client.run('summary', [{'label': '가상 연결 확인 자료',
                'text': '이 문서는 가상 테스트 자료입니다. 팀은 금요일에 샘플 센서를 점검하기로 결정했습니다. 담당자는 미정입니다.'}])
        else:
            sources, partial = read_document(args.db, args.file_id, client.settings.max_chars)
            result = client.run(args.command, sources, getattr(args, 'question', ''), partial)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AIError, ValueError, OSError) as error:
        message = str(error) if isinstance(error, AIError) else '로컬 설정 또는 파일 접근을 확인하세요.'
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
