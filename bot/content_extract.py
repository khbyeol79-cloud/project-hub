"""Bounded document extraction worker. No network, credentials, or DB access."""
import hashlib
import io
import json
import logging
from contextlib import ExitStack
from pathlib import Path
import sys
import unicodedata

if __package__:
    from . import ocr_extract as ocr
else:
    import importlib.util
    spec = importlib.util.spec_from_file_location('ocr_extract', Path(__file__).with_name('ocr_extract.py'))
    ocr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ocr)

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 200
MAX_CHARS = 500_000
MAX_PAGE_CHARS = 100_000


def clean_text(text):
    text = unicodedata.normalize('NFKC', text)
    text = ''.join(c if c.isspace() or not unicodedata.category(c).startswith('C') else '' for c in text)
    return ' '.join(text.split())


def extract(job):
    suffix = Path(job['filename']).suffix.lower()
    if suffix not in {'.txt', '.pdf', '.docx', '.xlsx'} | ocr.IMAGE_SUFFIXES:
        return {'status': 'unsupported', 'pages': []}
    path = Path(job['path'])
    storage = Path(job['storage']).resolve(strict=True)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(storage) or path.is_symlink() or not resolved.is_file():
        return {'status': 'unsafe_path', 'pages': []}
    if resolved.stat().st_size > MAX_BYTES:
        return {'status': 'too_large', 'pages': []}
    with resolved.open('rb') as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return {'status': 'too_large', 'pages': []}
    if hashlib.sha256(data).hexdigest() != job['sha256']:
        return {'status': 'changed', 'pages': []}
    if suffix in ocr.IMAGE_SUFFIXES:
        try:
            with ocr.OCR(job.get('scratch')) as worker:
                raw, partial = worker.image(data)
            text = clean_text(raw)
            partial = partial or len(text) > MAX_PAGE_CHARS
            return {'status': 'partial' if partial else ('indexed' if text else 'no_text'),
                    'pages': [[1, text[:MAX_PAGE_CHARS]]] if text else [],
                    'locations': {'1': '이미지 OCR'}}
        except ocr.ImageTooLarge:
            return {'status': 'too_large', 'pages': []}
        except ocr.OCRUnavailable:
            return {'status': 'ocr_pending', 'pages': []}
        except ocr.OCRLimit:
            return {'status': 'partial', 'pages': []}
    if suffix in ('.docx', '.xlsx'):
        # -I workers cannot import siblings by their ordinary module name.
        import importlib.util
        spec = importlib.util.spec_from_file_location('office_extract', Path(__file__).with_name('office_extract.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.extract_office(data, suffix, clean_text, MAX_CHARS)
    if suffix == '.txt':
        if data.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = data.decode('utf-16')
        else:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('cp949')
        if '\x00' in text or sum(ord(c) < 32 and not c.isspace() for c in text) > max(2, len(text) // 100):
            return {'status': 'binary_text', 'pages': []}
        text = clean_text(text)
        return {'status': ('partial' if len(text) > MAX_CHARS else 'indexed') if text else 'no_text',
                'pages': [[1, text[:MAX_CHARS]]] if text else []}

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        return {'status': 'encrypted', 'pages': []}
    pages, locations, remaining = [], {}, MAX_CHARS
    partial = len(reader.pages) > MAX_PAGES
    retry_ocr, worker = False, None
    with ExitStack() as stack:
        for number, page in enumerate(reader.pages[:MAX_PAGES], 1):
            text = clean_text(page.extract_text() or '')
            if not text and ocr.has_visual_content(page):
                if worker is None:
                    worker = stack.enter_context(ocr.OCR(job.get('scratch')))
                try:
                    text = clean_text(worker.pdf_page(data, number))
                    if text:
                        locations[str(number)] = f'PDF {number}쪽 · OCR'
                except ocr.OCRLimit:
                    partial = True
                except ocr.OCRUnavailable:
                    retry_ocr = True
            take = min(remaining, MAX_PAGE_CHARS)
            if len(text) > take:
                partial = True
            text = text[:take]
            if text:
                pages.append([number, text])
                remaining -= len(text)
            if not remaining:
                partial = True
                break
    return {'status': 'ocr_pending' if retry_ocr else ('partial' if partial else ('indexed' if pages else 'no_text')),
            'pages': pages, 'locations': locations}


def main():
    logging.disable(logging.CRITICAL)
    job = json.load(sys.stdin)
    # A malformed/compressed PDF must not consume the collector's memory or CPU.
    if sys.platform.startswith('linux'):
        import resource
        import os
        os.nice(10)
        cpu = 45 if Path(job['filename']).suffix.lower() in ocr.IMAGE_SUFFIXES | {'.pdf'} else 15
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        result = extract(job)
    except FileNotFoundError:
        result = {'status': 'missing', 'pages': []}
    except UnicodeError:
        result = {'status': 'encoding', 'pages': []}
    except Exception:
        result = {'status': 'failed', 'pages': []}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
