"""One-time Pi administrator installation. Run with sudo after preparing venv311."""
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

OLD = '''khbps.duckdns.org {
\tforward_auth 127.0.0.1:9091 {
\t\turi /api/authz/forward-auth
\t\tcopy_headers Remote-User Remote-Groups Remote-Email Remote-Name
\t}
\treverse_proxy 127.0.0.1:8080
}'''
NEW = '''khbps.duckdns.org {
\t@library path /library /library/*
\thandle @library {
\t\tredir /library /library/ 308
\t\treverse_proxy 127.0.0.1:8090
\t}
\thandle {
\t\tforward_auth 127.0.0.1:9091 {
\t\t\turi /api/authz/forward-auth
\t\t\tcopy_headers Remote-User Remote-Groups Remote-Email Remote-Name
\t\t}
\t\treverse_proxy 127.0.0.1:8080
\t}
}'''


def build_caddy(original):
    if original.count(OLD) != 1:
        raise RuntimeError('Caddy configuration changed. No changes applied; review it first.')
    return original.replace(OLD, NEW)


def run(*args):
    subprocess.run(args, check=True)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Run this installer with sudo.')
    root=Path('/home/khb/project-hub')
    caddy=Path('/etc/caddy/Caddyfile')
    unit=Path('/etc/systemd/system/project-hub-library.service')
    if unit.exists():
        raise RuntimeError('Existing library service preserved. Review updates manually.')
    for p in [root/'organizer/web.py',root/'organizer/project-hub-library.service',
              Path('/home/khb/.local/share/project-hub-library/venv311/bin/python'),
              Path('/home/khb/.config/project-hub/library.env')]:
        if not p.is_file():
            raise RuntimeError('Missing installation prerequisite: '+str(p))
    original=caddy.read_text()
    updated=build_caddy(original)
    backup=caddy.with_name('Caddyfile.pre-library.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    fd, temporary=tempfile.mkstemp(prefix='library-',suffix='.caddyfile',dir=caddy.parent)
    changed=False
    try:
        with os.fdopen(fd,'w') as f: f.write(updated)
        run('caddy','validate','--config',temporary,'--adapter','caddyfile')
        run('systemd-analyze','verify',str(root/'organizer/project-hub-library.service'))
        shutil.copy2(caddy,backup)
        shutil.copyfile(root/'organizer/project-hub-library.service',unit)
        os.chmod(unit,0o644)
        run('systemctl','daemon-reload')
        run('systemctl','enable','--now','project-hub-library.service')
        for attempt in range(20):
            try:
                with urlopen('http://127.0.0.1:8090/library/',timeout=2) as r:
                    if r.status==200: break
            except OSError:
                time.sleep(.5)
        else:
            raise RuntimeError('Library did not become ready')
        try:
            with urlopen('http://127.0.0.1:8090/library/api/files',timeout=2):
                raise RuntimeError('Anonymous access unexpectedly allowed')
        except HTTPError as e:
            if e.code != 401: raise
        os.chmod(temporary,0o644)
        os.replace(temporary,caddy)
        changed=True
        run('systemctl','reload','caddy')
        run('systemctl','is-active','project-hub-library.service','caddy','project-hub.service')
    except Exception:
        if changed:
            shutil.copy2(backup,caddy)
            subprocess.run(['systemctl','reload','caddy'])
        if unit.exists():
            subprocess.run(['systemctl','disable','--now','project-hub-library.service'])
            unit.unlink()
            subprocess.run(['systemctl','daemon-reload'])
        raise
    finally:
        if Path(temporary).exists(): Path(temporary).unlink()
    print('LIBRARY_READY: share URL is in /home/khb/.config/project-hub/library-share-url.txt')


if __name__=='__main__':
    main()
