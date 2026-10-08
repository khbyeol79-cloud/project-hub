"""Create/rotate the private link configuration locally; never print secrets."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import secrets


def configure(folder, origin, rotate=False):
    if not re.fullmatch(r'https://[a-z0-9.-]+(?::[0-9]+)?', origin):
        raise ValueError('Expected an HTTPS origin without a path')
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    config=folder/'library.env'
    link=folder/'library-share-url.txt'
    if (config.exists() or link.exists()) and not rotate:
        raise FileExistsError('Existing link preserved; use --rotate to invalidate it')
    token=secrets.token_urlsafe(32)
    text=(f'PROJECT_HUB_LIBRARY_ORIGIN={origin}\n'
          f'PROJECT_HUB_LIBRARY_SHARE_HASH={hashlib.sha256(token.encode()).hexdigest()}\n'
          f'PROJECT_HUB_LIBRARY_SESSION_KEY={secrets.token_hex(32)}\n')
    for target, body in [(config,text),(link,f'{origin}/library/#key={token}\n')]:
        temporary=target.with_name(target.name+'.'+secrets.token_hex(8)+'.tmp')
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(body)
        os.replace(temporary,target)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Create a library link; restart the web service after rotation')
    parser.add_argument('--origin',required=True)
    parser.add_argument('--rotate',action='store_true')
    args=parser.parse_args()
    configure(Path.home()/'.config/project-hub',args.origin,args.rotate)
    print('Private library configuration saved. Restart project-hub-library.service to apply.')
