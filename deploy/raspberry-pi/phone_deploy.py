"""Explicit code-only deployment from a clean checkout; run as the Pi login user."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

SERVICES = ['project-hub.service', 'project-hub-library.service']


def run(*args, cwd=None, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def same_code(left, right):
    # Existing PC-copied production files may have CRLF. Avoid needless restarts.
    return (left.is_file() and right.is_file()
            and left.read_bytes().replace(b'\r\n', b'\n')
            == right.read_bytes().replace(b'\r\n', b'\n'))


def allowed(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name:
        return False
    return ((len(p.parts) == 2 and p.parts[0] in {'bot', 'organizer'} and p.suffix == '.py')
            or (len(p.parts) == 3 and p.parts[:2] == ('organizer', 'static')
                and p.suffix in {'.js', '.css', '.html'}))


def safe_path(root, name):
    if not allowed(name):
        raise ValueError('Disallowed application path: ' + name)
    p = root / name
    for part in [p, *p.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('Symlink in application path: ' + name)
    if not p.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escaped application root')
    return p


def save_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temp.replace(path)


def restore(root, backup, manifest):
    # Validate all paths before restoring anything.
    for name in manifest['files']:
        safe_path(root, name)
        old = manifest['files'][name]
        if old is not None and digest(backup / 'code' / name) != old:
            raise ValueError('Backup checksum mismatch: ' + name)
    for name, old_hash in manifest['files'].items():
        target = safe_path(root, name)
        if old_hash is None:
            target.unlink(missing_ok=True)
        else:
            source = backup / 'code' / name
            if digest(source) != old_hash:
                raise ValueError('Backup checksum mismatch: ' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def healthy():
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            run('systemctl', 'is-active', '--quiet', *SERVICES)
            with urllib.request.urlopen('http://127.0.0.1:8090/library/api/files', timeout=3) as r:
                data = json.load(r)
                if r.status == 200 and isinstance(data.get('files'), list):
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Service / library health check failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / 'project-hub')
    parser.add_argument('--apply', action='store_true', help='Stop services and apply tested code')
    parser.add_argument('--rollback', type=Path, help='Restore the printed local backup directory')
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() == 0:
        parser.error('Run as the normal Pi login user, not as root')
    os.umask(0o077)
    root = args.root.resolve()
    state = Path.home() / '.local/state/project-hub-deploy'
    state.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (state / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback:
            backup = args.rollback.resolve()
            if not backup.is_relative_to(state):
                parser.error('Rollback directory must be under ' + str(state))
            manifest = json.loads((backup / 'manifest.json').read_text())
            if manifest['root'] != str(root):
                parser.error('Backup belongs to another installation')
            # Never overwrite changes made since this deployment.
            for name, expected in manifest['new_hashes'].items():
                if digest(safe_path(root, name)) != expected:
                    parser.error('Application changed since deployment: ' + name)
            run('sudo', '-v')
            run('sudo', 'systemctl', 'stop', *SERVICES)
            restore(root, backup, manifest)
            run('sudo', 'systemctl', 'start', *SERVICES)
            healthy()
            print('Previous application code restored. Database was not rewound.')
            return

        checkout = Path(__file__).resolve().parents[2]
        sha = run('git', 'rev-parse', 'HEAD', cwd=checkout, capture=True).strip()
        if run('git', 'status', '--porcelain', cwd=checkout, capture=True).strip():
            parser.error('Use a clean checkout of the chosen commit')
        tracked = run('git', 'ls-files', '-z', cwd=checkout, capture=True).split('\0')
        names = [name for name in tracked if allowed(name)]
        if not {'bot/discord_bot.py', 'organizer/web.py'}.issubset(names):
            parser.error('Incomplete source checkout')
        for name in names:
            safe_path(checkout, name)
            safe_path(root, name)
        # Deletions require a migration plan instead of silently leaving old modules.
        existing = [p.relative_to(root).as_posix() for folder in ('bot', 'organizer')
                    for p in (root / folder).rglob('*') if p.is_file()]
        removed = [name for name in existing if allowed(name) and name not in names]
        if removed:
            parser.error('Code deletion requires a manual plan: ' + ', '.join(removed))
        changed = [name for name in names if not same_code(root / name, checkout / name)]
        print('Commit:', sha)
        print('Code changes:', ', '.join(changed) or '(none)')
        # Tests run in the credential-free checkout with its own Python 3.11 environment.
        run('bash', 'scripts/check.sh', cwd=checkout)
        # Check target environments without installing or upgrading production packages.
        checks = [(root / 'deploy/raspberry-pi/.venv/bin/python', 'requirements.txt'),
                  (Path.home() / '.local/share/project-hub-library/venv311/bin/python',
                   'organizer/requirements-web.txt')]
        check = ('import importlib.metadata as m,sys; from pathlib import Path; '
                 'assert sys.version_info[:2]==(3,11); '
                 'items=[s.strip().split("==") for s in Path(sys.argv[1]).read_text().splitlines() '
                 'if s.strip() and not s.lstrip().startswith("#")]; '
                 'assert all(len(x)==2 and m.version(x[0])==x[1] for x in items), '
                 '"Production dependencies differ; prepare an explicit dependency upgrade"')
        for python, requirement in checks:
            run(str(python), '-c', check, str(checkout / requirement))
        if not args.apply:
            print('Checks passed. No live files changed. Add --apply to deploy this commit.')
            return
        if not changed:
            print('Already matches live application. No restart needed.')
            return
        run('sudo', '-v')  # Ask in the authenticated terminal, never in chat.
        for service in SERVICES:
            run('systemctl', 'is-active', '--quiet', service)
        backup = state / (time.strftime('%Y%m%d-%H%M%S') + '-' + sha[:12])
        backup.mkdir()
        manifest = {'root': str(root), 'sha': sha,
                    'files': {n: digest(root / n) for n in changed},
                    'new_hashes': {n: digest(checkout / n) for n in changed}}
        for name, old in manifest['files'].items():
            if old is not None:
                dest = backup / 'code' / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / name, dest)
        save_json(backup / 'manifest.json', manifest)
        print('Rollback directory:', backup, flush=True)
        applied = False
        try:
            run('sudo', 'systemctl', 'stop', *SERVICES)
            if (root / 'project_hub.db').is_file():
                with closing(sqlite3.connect(root / 'project_hub.db')) as source:
                    with closing(sqlite3.connect(backup / 'project_hub.db')) as dest:
                        source.backup(dest)
            for name in changed:
                if digest(root / name) != manifest['files'][name]:
                    raise RuntimeError('Concurrent application edit: ' + name)
            applied = True
            for name in changed:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(checkout / name, target)
            run('sudo', 'systemctl', 'start', *SERVICES)
            healthy()
        except BaseException:
            run('sudo', 'systemctl', 'stop', *SERVICES)
            if applied:
                restore(root, backup, manifest)
            run('sudo', 'systemctl', 'start', *SERVICES)
            print('Deployment failed; previous code restored. Inspect services and database backup.')
            raise
        save_json(state / 'current.json', {'sha': sha, 'backup': str(backup)})
        print('Deployed:', sha)


if __name__ == '__main__':
    main()
