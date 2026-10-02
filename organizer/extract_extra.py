"""Isolated, bounded Markdown/PPTX reader; never executes document content."""
import hashlib
import io
import json
from html.parser import HTMLParser
from pathlib import Path
import posixpath
import sys
import zipfile

MAX_BYTES=25*1024*1024
MAX_CHARS=200000


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden=0
        self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','template','iframe','object'}:
            self.hidden+=1
        elif not self.hidden and tag in {'p','div','br','li','tr','h1','h2','h3','h4','section'}:
            self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in {'script','style','template','iframe','object'}:
            self.hidden=max(0,self.hidden-1)
        elif not self.hidden and tag in {'p','div','li','tr','h1','h2','h3','h4','section'}:
            self.parts.append('\n')
    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)


def extract(job):
    path=Path(job['path']).resolve(strict=True)
    if not path.is_relative_to(Path(job['storage']).resolve()) or not path.is_file():
        return {'status':'unsafe_path','pages':[]}
    if path.stat().st_size>MAX_BYTES:
        return {'status':'too_large','pages':[]}
    with path.open('rb') as f:
        data=f.read(MAX_BYTES+1)
    if len(data)>MAX_BYTES:
        return {'status':'too_large','pages':[]}
    if hashlib.sha256(data).hexdigest()!=job['sha256']:
        return {'status':'changed','pages':[]}
    suffix=Path(job['filename']).suffix.lower()
    if job.get('reading'):
        import runpy
        reader=runpy.run_path(str(Path(__file__).with_name('reading.py')))
        return reader['extract_reading'](data,suffix)
    if suffix in {'.md','.markdown','.html','.htm'}:
        if data.startswith((b'\xff\xfe',b'\xfe\xff')):
            text=data.decode('utf-16')
        else:
            try: text=data.decode('utf-8-sig')
            except UnicodeDecodeError: text=data.decode('cp949')
        if '\x00' in text:
            return {'status':'binary_text','pages':[]}
        label='Markdown 본문'
        if suffix in {'.html','.htm'}:
            parser=HTMLText()
            parser.feed(text)
            text='\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
            label='HTML 본문'
        return {'status':'partial' if len(text)>MAX_CHARS else ('indexed' if text.strip() else 'no_text'),
                'pages':[{'page':1,'label':label,'text':text[:MAX_CHARS]}] if text.strip() else []}
    if suffix!='.pptx':
        return {'status':'unsupported','pages':[]}
    from defusedxml.ElementTree import fromstring
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if len(z.infolist())>5000:
            return {'status':'too_large','pages':[]}
        used=0
        def xml(name):
            nonlocal used
            info=z.getinfo(name)
            used+=info.file_size
            if info.file_size>2*1024*1024 or used>20*1024*1024:
                raise ValueError('XML size limit')
            return fromstring(z.read(name),forbid_dtd=True,forbid_entities=True,forbid_external=True)
        rels={r.attrib['Id']:r.attrib['Target'] for r in xml('ppt/_rels/presentation.xml.rels')
              if r.attrib.get('TargetMode')!='External'}
        pns='{http://schemas.openxmlformats.org/presentationml/2006/main}'
        rns='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
        ans='{http://schemas.openxmlformats.org/drawingml/2006/main}'
        slides=xml('ppt/presentation.xml').findall('.//'+pns+'sldId')
        pages,total=[],0
        partial=len(slides)>150
        for number,slide in enumerate(slides[:150],1):
            target=posixpath.normpath(posixpath.join('ppt',rels[slide.attrib[rns+'id']]))
            if not target.startswith('ppt/slides/') or not target.endswith('.xml'):
                raise ValueError('Invalid slide target')
            root=xml(target)
            text='\n'.join(''.join(t.text or '' for t in p.iter(ans+'t')) for p in root.iter(ans+'p')).strip()
            if not text:
                partial=True
                continue
            remaining=MAX_CHARS-total
            if remaining<=0:
                partial=True
                break
            partial|=len(text)>remaining
            text=text[:remaining]
            pages.append({'page':number,'label':f'슬라이드 {number}','text':text})
            total+=len(text)
        return {'status':('partial' if partial else 'indexed') if pages else 'no_text','pages':pages}


if __name__=='__main__':
    try:
        if sys.platform!='win32':
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
            resource.setrlimit(resource.RLIMIT_CPU,(10,10))
        result=extract(json.load(sys.stdin))
    except Exception:
        result={'status':'failed','pages':[]}
    print(json.dumps(result,ensure_ascii=True))
