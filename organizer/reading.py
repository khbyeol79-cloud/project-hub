"""Plain structured blocks only; no collected markup is trusted by the UI."""
import io
import zipfile
from html.parser import HTMLParser
from defusedxml.ElementTree import fromstring


class HTMLBlocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks=[]
        self.parts=[]
        self.kind='p'
        self.hidden=0
        self.rows=None
        self.cells=[]

    def flush(self):
        text=''.join(self.parts).strip()
        if text: self.blocks.append({'type':self.kind,'text':text})
        self.parts=[]

    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','template','iframe','object','svg'}:
            self.hidden+=1
        if self.hidden: return
        if tag=='table':
            self.flush(); self.rows=[]
        elif tag=='tr': self.cells=[]
        elif tag in {'td','th'}: self.parts=[]
        elif tag=='br': self.parts.append('\n')
        elif self.rows is None and tag in {'p','div','section','h1','h2','h3','h4','li','pre','blockquote'}:
            self.flush(); self.kind=tag if tag not in {'div','section'} else 'p'

    def handle_endtag(self,tag):
        if tag in {'script','style','template','iframe','object','svg'}:
            self.hidden=max(0,self.hidden-1); return
        if self.hidden: return
        if tag in {'td','th'} and self.rows is not None:
            self.cells.append(''.join(self.parts).strip()); self.parts=[]
        elif tag=='tr' and self.rows is not None: self.rows.append(self.cells)
        elif tag=='table' and self.rows is not None:
            self.blocks.append({'type':'table','rows':self.rows}); self.rows=None; self.parts=[]
        elif self.rows is None and tag in {'p','div','section','h1','h2','h3','h4','li','pre','blockquote'}:
            self.flush(); self.kind='p'

    def handle_data(self,data):
        if not self.hidden: self.parts.append(data)


def extract_reading(data,suffix):
    if suffix in {'.html','.htm'}:
        try: text=data.decode('utf-16' if data.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig')
        except UnicodeDecodeError: text=data.decode('cp949')
        parser=HTMLBlocks(); parser.feed(text); parser.flush(); blocks=parser.blocks
    else:
        ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info=z.getinfo('word/document.xml')
            if info.file_size>8*1024*1024: return {'status':'too_large','pages':[],'blocks':[]}
            root=fromstring(z.read(info),forbid_dtd=True,forbid_entities=True,forbid_external=True)
        body=root.find(ns+'body'); blocks=[]
        def text(el):
            return ''.join((x.text or '') if x.tag==ns+'t' else '\n' if x.tag==ns+'br' else '\t' if x.tag==ns+'tab' else '' for x in el.iter())
        for el in body:
            if el.tag==ns+'p':
                style=el.find(ns+'pPr/'+ns+'pStyle')
                name=style.get(ns+'val','').lower() if style is not None else ''
                kind='h2' if name.startswith(('heading','title')) else 'li' if el.find(ns+'pPr/'+ns+'numPr') is not None else 'p'
                blocks.append({'type':kind,'text':text(el)})
            elif el.tag==ns+'tbl':
                blocks.append({'type':'table','rows':[[ '\n'.join(text(p) for p in c.findall(ns+'p')) for c in r.findall(ns+'tc')] for r in el.findall(ns+'tr')]})
    kept=[]; size=0; partial=False
    for block in blocks:
        cost=len(str(block))
        if size+cost>200000 or len(kept)>=3000:
            partial=True; break
        kept.append(block); size+=cost
    return {'status':'partial' if partial else 'indexed','blocks':kept,'pages':[]}
