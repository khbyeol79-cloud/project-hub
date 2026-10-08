"""Bounded DOCX-to-HTML preview for the existing isolated reading worker.

Only generated markup, inline styles and embedded raster images are emitted.
Document scripts, external relationships and collected HTML never become active.
"""
import base64
import io
import posixpath
import re
import zipfile
from html import escape
from defusedxml.ElementTree import fromstring

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
MAX_XML = 8 * 1024 * 1024
MAX_IMAGES = 4 * 1024 * 1024
MAX_HTML = 6 * 1024 * 1024


def render_docx(data):
    partial = False
    images_used = 0
    output_size = 0
    block_count = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        def read(name, limit=MAX_XML):
            info = archive.getinfo(name)
            if info.file_size > limit:
                raise ValueError('Preview size limit')
            with archive.open(info) as stream:
                body = stream.read(limit + 1)
            if len(body) > limit:
                raise ValueError('Preview size limit')
            return body

        def xml(name):
            return fromstring(read(name), forbid_dtd=True, forbid_entities=True, forbid_external=True)

        root = xml('word/document.xml')
        relationships = {}
        if 'word/_rels/document.xml.rels' in archive.namelist():
            for rel in xml('word/_rels/document.xml.rels'):
                if rel.get('TargetMode') == 'External':
                    continue
                target = rel.get('Target', '')
                path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'word/' + target)
                if path.startswith('word/media/'):
                    relationships[rel.get('Id')] = path
        image_cache = {}

        def image(blip):
            nonlocal partial, images_used
            path = relationships.get(blip.get(R + 'embed'))
            if not path:
                partial = True
                return '<span class="omitted">[외부 이미지 또는 지원하지 않는 그림]</span>'
            if path in image_cache:
                return image_cache[path]
            try:
                body = read(path, MAX_IMAGES - images_used)
            except (KeyError, ValueError):
                partial = True
                return '<span class="omitted">[이미지 크기 제한]</span>'
            mime = None
            if body.startswith(b'\x89PNG\r\n\x1a\n'):
                mime = 'image/png'
            elif body.startswith(b'\xff\xd8\xff'):
                mime = 'image/jpeg'
            elif body[:6] in {b'GIF87a', b'GIF89a'}:
                mime = 'image/gif'
            elif body[:4] == b'RIFF' and body[8:12] == b'WEBP':
                mime = 'image/webp'
            if not mime:
                partial = True
                return '<span class="omitted">[지원하지 않는 그림 형식]</span>'
            images_used += len(body)
            html = '<img alt="문서 삽입 이미지" src="data:' + mime + ';base64,' + base64.b64encode(body).decode('ascii') + '">'
            image_cache[path] = html
            return html

        def value(parent, name, attribute='val'):
            child = parent.find(W + name) if parent is not None else None
            return child.get(W + attribute) if child is not None else None

        def number(raw, low, high):
            try:
                return max(low, min(high, int(raw)))
            except (ValueError, TypeError):
                return None

        def run(el):
            parts = []
            for child in el:
                if child.tag == W + 't':
                    parts.append(escape(child.text or ''))
                elif child.tag == W + 'tab':
                    parts.append('\t')
                elif child.tag in {W + 'br', W + 'cr'}:
                    parts.append('<br>')
                elif child.tag in {W + 'drawing', W + 'pict'}:
                    parts.extend(image(blip) for blip in child.iter(A + 'blip'))
            text = ''.join(parts)
            props = el.find(W + 'rPr')
            styles = []
            size = number(value(props, 'sz'), 12, 144)
            if size is not None:
                styles.append(f'font-size:{size / 2:g}pt')
            color = value(props, 'color')
            if color and re.fullmatch(r'[0-9A-Fa-f]{6}', color):
                styles.append('color:#' + color)
            for name, tag in [('b', 'strong'), ('i', 'em'), ('strike', 's')]:
                prop = props.find(W + name) if props is not None else None
                if prop is not None and prop.get(W + 'val') not in {'0', 'false', 'off'}:
                    text = '<' + tag + '>' + text + '</' + tag + '>'
            underline = value(props, 'u')
            if underline and underline != 'none':
                text = '<u>' + text + '</u>'
            vertical = value(props, 'vertAlign')
            if vertical in {'subscript', 'superscript'}:
                tag = 'sub' if vertical == 'subscript' else 'sup'
                text = '<' + tag + '>' + text + '</' + tag + '>'
            return '<span style="' + ';'.join(styles) + '">' + text + '</span>' if styles else text

        def paragraph(el):
            props = el.find(W + 'pPr')
            styles = []
            align = value(props, 'jc')
            if align in {'left', 'center', 'right', 'both'}:
                styles.append('text-align:' + ('justify' if align == 'both' else align))
            spacing = props.find(W + 'spacing') if props is not None else None
            for attr, css in [('before', 'margin-top'), ('after', 'margin-bottom')]:
                val = number(spacing.get(W + attr), 0, 2400) if spacing is not None else None
                if val is not None:
                    styles.append(f'{css}:{val / 20:g}pt')
            style = value(props, 'pStyle') or ''
            heading = re.fullmatch(r'Heading([1-6])', style, re.I)
            tag = 'h' + heading[1] if heading else 'h1' if style.lower() == 'title' else 'p'
            content = ''.join(run(r) for r in el.iter(W + 'r')) or '<br>'
            if props is not None and props.find(W + 'numPr') is not None:
                content = '<span class="bullet">• </span>' + content
            return '<' + tag + ' style="' + ';'.join(styles) + '">' + content + '</' + tag + '>'

        def blocks(parent, depth=0):
            nonlocal partial, block_count, output_size
            result = []
            for el in parent:
                if block_count >= 3000 or output_size > MAX_HTML:
                    partial = True
                    break
                if el.tag == W + 'p':
                    html = paragraph(el)
                elif el.tag == W + 'tbl' and depth < 4:
                    html = table(el, depth + 1)
                else:
                    continue
                block_count += 1
                output_size += len(html)
                if output_size > MAX_HTML:
                    partial = True
                    break
                result.append(html)
            return ''.join(result)

        def table(el, depth):
            nonlocal partial
            rows, active, cell_count = [], {}, 0
            for row in el.findall(W + 'tr'):
                if len(rows) >= 1000 or cell_count >= 3000:
                    partial = True
                    break
                cells, new_active, column = [], {}, 0
                for cell in row.findall(W + 'tc'):
                    cell_count += 1
                    if cell_count > 3000:
                        partial = True
                        break
                    props = cell.find(W + 'tcPr')
                    span = number(value(props, 'gridSpan'), 1, 64) or 1
                    merge = props.find(W + 'vMerge') if props is not None else None
                    previous = active.get(column)
                    if merge is not None and merge.get(W + 'val') != 'restart' and previous and previous['span'] == span:
                        previous['rows'] += 1
                        new_active[column] = previous
                    else:
                        item = {'html': blocks(cell, depth), 'span': span, 'rows': 1}
                        cells.append(item)
                        if merge is not None and merge.get(W + 'val') == 'restart':
                            new_active[column] = item
                    column += span
                active = new_active
                rows.append(cells)
            return '<div class="table-wrap"><table>' + ''.join('<tr>' + ''.join(
                f'<td colspan="{c["span"]}" rowspan="{c["rows"]}">{c["html"]}</td>' for c in row) + '</tr>' for row in rows) + '</table></div>'

        body = root.find(W + 'body')
        if body is None:
            raise ValueError('Missing document body')
        content = blocks(body)
    notice = '<p class="notice">일부 내용을 생략했습니다. 원본을 다운로드해 확인해 주세요.</p>' if partial else ''
    # Keep the generated preview self-contained, suitable for a script-free sandbox.
    html = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>문서 원본 미리보기</title><style>
*{box-sizing:border-box}body{margin:0;background:#edf0eb;color:#172d2b;font:15px/1.7 system-ui,sans-serif}.paper{max-width:820px;margin:20px auto;padding:44px;background:white;min-height:80vh}p{margin:0 0 12px;white-space:pre-wrap;overflow-wrap:anywhere}h1,h2,h3,h4,h5,h6{line-height:1.4;overflow-wrap:anywhere}img{max-width:100%;height:auto;vertical-align:middle}.table-wrap{max-width:100%;overflow:auto;margin:16px 0}table{border-collapse:collapse;min-width:100%}td{border:1px solid #b9c3b6;padding:8px;vertical-align:top;min-width:80px}td p:last-child{margin-bottom:0}.notice,.omitted{color:#825c34;background:#fff4e5;padding:8px;font-size:12px}@media(max-width:600px){.paper{margin:0;padding:18px}body{font-size:14px}}
</style><main class="paper">''' + notice + content + '</main></html>'
    return html, partial
