"""Local OCR helpers, used only inside the bounded extraction worker."""
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import warnings

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
MAX_PIXELS = 20_000_000
MAX_EDGE = 3000
MAX_OCR_PAGES = 10
OCR_BUDGET = 90
STEP_TIMEOUT = 20


class OCRUnavailable(Exception):
    pass


class OCRLimit(Exception):
    pass


class ImageTooLarge(Exception):
    pass


def has_visual_content(page):
    # Do not decode every embedded image just to decide whether OCR is needed.
    resources = page.get('/Resources')
    if resources and resources.get_object().get('/XObject'):
        return True
    contents = page.get_contents()
    return bool(contents is not None and re.search(rb'(?<!\S)BI\s', contents.get_data()))


class OCR:
    def __init__(self, scratch=None):
        self.temporary = tempfile.TemporaryDirectory(prefix='ocr-', dir=scratch)
        self.root = Path(self.temporary.name)
        self.deadline = time.monotonic() + OCR_BUDGET
        self.count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.temporary.cleanup()

    def run(self, args):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise OCRLimit()
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}}
        env['OMP_THREAD_LIMIT'] = '1'
        try:
            completed = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env,
                                       timeout=min(STEP_TIMEOUT, remaining), check=True)
        except subprocess.TimeoutExpired as error:
            if time.monotonic() >= self.deadline:
                raise OCRLimit() from error
            raise OCRUnavailable() from error
        except (OSError, subprocess.CalledProcessError) as error:
            raise OCRUnavailable() from error
        return completed.stdout

    def image(self, data):
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            try:
                with Image.open(io.BytesIO(data)) as source:
                    if source.width * source.height > MAX_PIXELS:
                        raise ImageTooLarge()
                    if source.format not in {'PNG', 'JPEG', 'WEBP'}:
                        raise ValueError('Unsupported image format')
                    partial = getattr(source, 'n_frames', 1) > 1
                    ImageOps.exif_transpose(source, in_place=True)
                    source.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                    rgba = source.convert('RGBA')
                    background = Image.new('RGB', rgba.size, 'white')
                    background.paste(rgba, mask=rgba.getchannel('A'))
                    background.save(self.root / 'input.png')
            except (Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
                raise ImageTooLarge() from error
        text = self.run(['tesseract', str(self.root / 'input.png'), 'stdout',
                         '-l', 'kor+eng', '--oem', '1', '--psm', '3', '--dpi', '300'])
        return text.decode('utf-8', errors='replace'), partial

    def pdf_page(self, data, number):
        if self.count >= MAX_OCR_PAGES or time.monotonic() >= self.deadline:
            raise OCRLimit()
        self.count += 1
        source = self.root / 'source.pdf'
        if not source.exists():
            source.write_bytes(data)
        self.run(['pdftoppm', '-f', str(number), '-l', str(number), '-singlefile',
                  '-scale-to', str(MAX_EDGE), '-png', str(source), str(self.root / 'page')])
        return self.image((self.root / 'page.png').read_bytes())[0]
