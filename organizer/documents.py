"""Supplementary index kept separate from the concurrently running collector."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

SUFFIXES={'.md','.markdown','.pptx','.html','.htm'}


class Documents:
    def __init__(self, source, storage, cache):
        self.source=Path(source).resolve()
        self.storage=Path(storage).resolve()
        self.cache=Path(cache)
        self.cache.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.cache,os.O_CREAT|os.O_RDWR,0o600)
        os.close(fd)
        self.lock=threading.Lock()
        with closing(sqlite3.connect(self.cache)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, sha TEXT, result TEXT, body TEXT, updated REAL)')

    def metadata(self, file_id):
        with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT id,original_filename,local_path,sha256 FROM files WHERE id=?',(file_id,)).fetchone()
            return dict(row) if row else None

    def get(self,file_id,reading=False):
        row=self.metadata(file_id)
        if not row or Path(row['original_filename']).suffix.lower() not in ({'.docx','.html','.htm'} if reading else SUFFIXES):
            return None
        cache_id=-file_id if reading else file_id
        with self.lock:
            with closing(sqlite3.connect(self.cache)) as db:
                saved=db.execute('SELECT result,updated FROM documents WHERE id=? AND sha=?',(cache_id,row['sha256'])).fetchone()
            if saved:
                result=json.loads(saved[0])
                if result['status'] in {'indexed','partial','no_text','too_large','binary_text'} or time.time()-saved[1]<300:
                    return result
            path=Path(row['local_path'])
            if not path.is_absolute(): path=self.source.parent/path
            job={'path':str(path),'storage':str(self.storage),'filename':row['original_filename'],'sha256':row['sha256'],'reading':reading}
            env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','LANG','LC_ALL'}}
            try:
                proc=subprocess.run([sys.executable,'-I',str(Path(__file__).with_name('extract_extra.py'))],
                    input=json.dumps(job),text=True,capture_output=True,timeout=15,env=env)
                result=json.loads(proc.stdout) if proc.returncode==0 else {'status':'failed','pages':[]}
            except (subprocess.TimeoutExpired,ValueError):
                result={'status':'failed','pages':[]}
            with closing(sqlite3.connect(self.cache)) as db, db:
                db.execute('INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?)',
                    (cache_id,row['sha256'],json.dumps(result), '\n'.join(p['text'] for p in result['pages']).casefold(),time.time()))
            return result

    def matches(self,query):
        with closing(sqlite3.connect(self.cache)) as db:
            return db.execute('SELECT id,sha FROM documents WHERE instr(body,?)>0 LIMIT 200',(query.casefold(),)).fetchall()

    def scan(self):
        with closing(sqlite3.connect(self.source.as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute('SELECT id,original_filename FROM files').fetchall()
        for file_id,name in rows:
            if Path(name).suffix.lower() in SUFFIXES:
                self.get(file_id)

    def start(self):
        def work():
            while True:
                try: self.scan()
                except (OSError,sqlite3.Error,ValueError): pass
                time.sleep(30)
        threading.Thread(target=work,daemon=True,name='library-document-index').start()
