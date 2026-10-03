"""Bounded keyword retrieval over current file indexes; no provider call for search."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


def retrieve(db_path, documents, question, max_chars=12000):
    tokens=re.findall(r'[a-z0-9가-힣_]+',question.casefold())[:40]
    stop={'무엇','뭐야','뭐로','어떻게','알려줘','정리해줘','요약','자료','전체','대한','관련','있는','있어','했지'}
    terms=[]
    for word in tokens:
        if word in stop: continue
        word=re.sub(r'(에서는|에서|으로|은|는|을|를|이|가|과|와)$','',word) if len(word)>2 else word
        if len(word)>1 and word not in terms: terms.append(word)
    candidates=[]
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        metadata={r['id']:dict(r) for r in db.execute('SELECT id,original_filename,sha256,uploaded_at FROM files')}
        def add(file_id,page,text,label):
            f=metadata.get(file_id)
            if not f or not text.strip(): return
            for start in range(0,min(len(text),200000),1000):
                chunk=text[start:start+1200]
                body=chunk.casefold(); name=f['original_filename'].casefold()
                score=sum(3*(t in body)+1*(t in name) for t in terms)
                if question and (not terms or not score): continue
                candidates.append((score,f['uploaded_at'] or '',file_id,page,chunk,f"{f['original_filename']} · {label}"))
                if len(candidates)>500: candidates.sort(reverse=True);del candidates[150:]
        for r in db.execute('''SELECT p.file_id,p.page,p.body FROM content_pages p
            JOIN content_documents d ON d.file_id=p.file_id JOIN files f ON f.id=p.file_id
            WHERE d.source_sha256=f.sha256 AND d.status IN ('indexed','partial')'''):
            add(r['file_id'],r['page'],r['body'],f"구간 {r['page']}")
        with closing(sqlite3.connect(documents.cache)) as cache:
            for file_id,sha,raw in cache.execute('SELECT id,sha,result FROM documents WHERE id>0'):
                if file_id not in metadata or metadata[file_id]['sha256']!=sha: continue
                result=json.loads(raw)
                if result['status'] not in {'indexed','partial'}: continue
                for p in result['pages']: add(file_id,p['page'],p['text'],p['label'])
    candidates.sort(reverse=True)
    sources=[]; counts={}; used=set(); remaining=max_chars
    for _,date,file_id,page,text,label in candidates:
        if counts.get(file_id,0)>=2 or text in used: continue
        if remaining<=0 or len(sources)>=12: break
        used.add(text);counts[file_id]=counts.get(file_id,0)+1
        snippet=f'자료: {label}\n업로드 날짜: {date}\n{text}'[:remaining]
        sources.append({'file_id':file_id,'page':page,'text':snippet,'label':label})
        remaining-=len(snippet)
    return sources
