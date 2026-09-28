"""Offline, deliberately small Markdown renderer.

Supports headings, paragraphs, lists, fenced code and inline images. Raw HTML,
links and unsupported Markdown stay inert text. No scripts, remote resources,
or private book lookup. Version/content semantic quality depends on reviewed
manuscripts; rendering does not certify factual or semantic correctness.

HTML/EPUB require only the standard library. PDF requires the optional
reportlab/Pillow extra. Pass a .ttf via font_path (CLI --font), or install
Windows simhei.ttf / Linux NotoSansCJK .ttf. Missing glyphs fail closed.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import html
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import tempfile
from urllib.parse import unquote
import uuid
import zipfile
import zlib

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_SIDE = 16384
NOTICE = "Version and content semantic quality depend on reviewed manuscripts."
CSS = "body{max-width:48em;margin:2em auto;padding:1em;line-height:1.7}img{max-width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:monospace}figure{margin:1em 0}figcaption{font-size:90%}"
IMAGE = re.compile(r"!\[((?:\\.|[^\]\\])*)\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)")


@dataclass
class Asset:
    data: bytes
    mime: str
    width: int
    height: int
    name: str


def _safe_path(path):
    # Reject UNC/device paths before even stat(): filesystem APIs can otherwise
    # contact a remote SMB server despite there being no HTTP client here.
    if os.fspath(path).replace('\\', '/').startswith('//'):
        raise ValueError('network_or_device_path_refused')
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, "st_file_attributes", 0) & 0x400):
            raise ValueError("symlink_or_reparse_point_refused")
    return path


def _dimensions(width, height):
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and
            width * height <= MAX_PIXELS):
        raise ValueError("image_pixel_limit")


def _image_info(data):
    """Bounded structural validation; PNG also checks CRC and decompression.

    JPEG entropy decoding is delegated to the reader (Pillow also verifies PDF
    images). Reject concatenated JPEG/MPO and animated PNG containers.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos, chunks, compressed = 8, [], bytearray()
        width = height = 0
        while pos + 12 <= len(data):
            size = struct.unpack_from(">I", data, pos)[0]
            kind = data[pos + 4:pos + 8]
            end = pos + 12 + size
            if end > len(data):
                raise ValueError("invalid_png")
            body = data[pos + 8:pos + 8 + size]
            crc = struct.unpack_from(">I", data, pos + 8 + size)[0]
            if zlib.crc32(kind + body) & 0xffffffff != crc:
                raise ValueError("invalid_png_crc")
            if kind in (b"acTL", b"fcTL", b"fdAT"):
                raise ValueError("animated_image_refused")
            if not chunks and kind != b"IHDR":
                raise ValueError("invalid_png")
            if kind == b"IHDR":
                if chunks or size != 13:
                    raise ValueError("invalid_png")
                width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
                _dimensions(width, height)
                allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
                if depth not in allowed.get(color, ()) or comp or filt or interlace not in (0, 1):
                    raise ValueError("invalid_png_header")
            if kind == b"IDAT":
                compressed.extend(body)
            chunks.append(kind)
            pos = end
            if kind == b"IEND":
                if size or pos != len(data) or not compressed:
                    raise ValueError("invalid_png_end")
                break
        if not chunks or chunks[-1] != b"IEND":
            raise ValueError("invalid_png_end")
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        passes = [(0, 0, 1, 1)] if not interlace else [(0,0,8,8),(4,0,8,8),(0,4,4,8),(2,0,4,4),(0,2,2,4),(1,0,2,2),(0,1,1,2)]
        rows = []
        for x, y, dx, dy in passes:
            w, h = max(0, (width-x+dx-1)//dx), max(0, (height-y+dy-1)//dy)
            if w and h:
                rows.extend([1 + (w * channels * depth + 7)//8] * h)
        expected = sum(rows)
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(bytes(compressed), expected + 1)
        except zlib.error:
            raise ValueError("invalid_png_data") from None
        if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("invalid_png_data")
        offset = 0
        for row in rows:
            if raw[offset] > 4:
                raise ValueError("invalid_png_filter")
            offset += row
        return "image/png", width, height
    if data.startswith(b"\xff\xd8"):
        pos, width, height, scans = 2, 0, 0, 0
        while pos < len(data):
            if data[pos] != 255:
                raise ValueError("invalid_jpeg")
            while pos < len(data) and data[pos] == 255:
                pos += 1
            if pos == len(data):
                break
            marker = data[pos]
            pos += 1
            if marker == 0xd9:
                if pos != len(data) or not width or not scans:
                    raise ValueError("invalid_jpeg_end")
                return "image/jpeg", width, height
            if marker in (0, 0xd8) or 0xd0 <= marker <= 0xd7 or pos + 2 > len(data):
                raise ValueError("invalid_jpeg")
            length = int.from_bytes(data[pos:pos+2], "big")
            if length < 2 or pos + length > len(data):
                raise ValueError("invalid_jpeg")
            body = data[pos+2:pos+length]
            if marker == 0xe2 and body.startswith(b"MPF\0"):
                raise ValueError("multi_picture_refused")
            if marker in (0xc0, 0xc1, 0xc2):
                if width or len(body) < 6:
                    raise ValueError("invalid_jpeg_frame")
                height, width = struct.unpack_from(">HH", body, 1)
                _dimensions(width, height)
            pos += length
            if marker == 0xda:
                scans += 1
                while pos < len(data):
                    if data[pos] != 255:
                        pos += 1
                    elif pos + 1 < len(data) and (data[pos+1] == 0 or 0xd0 <= data[pos+1] <= 0xd7):
                        pos += 2
                    else:
                        break
        raise ValueError("invalid_jpeg_end")
    raise ValueError("only_static_png_jpeg_allowed")


def _asset(reference, assets_dir):
    ref = unquote(reference)
    if (assets_dir is None or not ref or "\\" in ref or ":" in ref or
            "?" in ref or "#" in ref or any(ord(c) < 32 for c in ref)):
        raise ValueError("invalid_asset_path")
    relative = PurePosixPath(ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("asset_path_escape")
    root = _safe_path(assets_dir)
    path = _safe_path(root.joinpath(*relative.parts))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("asset_path_escape")
    if not path.is_file():
        raise ValueError("missing_image")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("invalid_asset_file")
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image_byte_limit")
    mime, width, height = _image_info(data)
    name = hashlib.sha256(data).hexdigest() + (".png" if mime == "image/png" else ".jpg")
    return Asset(data, mime, width, height, name)


def _blocks(markdown, assets_dir):
    blocks, paragraph, code = [], [], None
    fence = None

    def flush():
        if paragraph:
            blocks.append(("p", " ".join(paragraph)))
            paragraph.clear()

    for line in markdown.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if code is not None:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                blocks.append(("pre", "\n".join(code)))
                code = None
            else:
                code.append(line)
            continue
        if match:
            flush()
            code, fence = [], match[1]
            continue
        if not line.strip():
            flush()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush()
            blocks.append(("h" + str(len(heading[1])), heading[2]))
        elif re.match(r"^\s*(?:[-*+] |\d+\. )", line):
            flush()
            blocks.append(("p", line.strip()))
        else:
            paragraph.append(line)
    flush()
    if code is not None:
        blocks.append(("pre", "\n".join(code)))
    result, assets = [], {}
    for kind, text in blocks:
        if kind == "pre":
            result.append((kind, text))
            continue
        start = 0
        for match in IMAGE.finditer(text):
            if match.start() > start:
                result.append((kind, text[start:match.start()]))
            asset = _asset(match[2], assets_dir)
            assets[asset.name] = asset
            caption = re.sub(r"\\(.)", r"\1", match[1])
            result.append(("image", (asset.name, caption)))
            start = match.end()
        if start < len(text):
            result.append((kind, text[start:]))
    return result, assets


def _x(value):
    return html.escape(value, quote=True)


def _body(blocks, assets, embedded):
    parts, toc = [], []
    for index, (kind, value) in enumerate(blocks):
        if kind == "image":
            name, caption = value
            asset = assets[name]
            src = ("data:" + asset.mime + ";base64," + base64.b64encode(asset.data).decode("ascii")) if embedded else "assets/" + name
            parts.append(f'<figure><img src="{src}" alt="{_x(caption)}" /><figcaption>{_x(caption)}</figcaption></figure>')
        else:
            ident = f"s{index}"
            if kind.startswith("h"):
                toc.append((ident, value))
            parts.append(f'<{kind} id="{ident}">{_x(value)}</{kind}>')
    return "\n".join(parts), toc


def _document(title, body, epub=False):
    namespace = ' xmlns="http://www.w3.org/1999/xhtml"' if epub else ""
    csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    policy = "" if epub else f'<meta http-equiv="Content-Security-Policy" content="{_x(csp)}" />'
    return (f'<!DOCTYPE html><html{namespace} lang="zh"><head><meta charset="utf-8" />{policy}'
            f'<title>{_x(title)}</title><style>{CSS}</style></head><body><h1>{_x(title)}</h1>'
            f'{body}<footer><p>{NOTICE}</p></footer></body></html>')


def _epub(title, blocks, assets):
    body, toc = _body(blocks, assets, False)
    nav = ''.join(f'<li><a href="book.xhtml#{ident}">{_x(text)}</a></li>' for ident, text in toc)
    if not nav:
        nav = f'<li><a href="book.xhtml">{_x(title)}</a></li>'
    navdoc = _document(title, f'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc" id="toc"><h2>Contents</h2><ol>{nav}</ol></nav>', True)
    manifest = ''.join(f'<item id="im{i}" href="assets/{a.name}" media-type="{a.mime}" />' for i, a in enumerate(assets.values()))
    modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    opf = (f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">urn:uuid:{uuid.uuid4()}</dc:identifier>'
           f'<dc:title>{_x(title)}</dc:title><dc:language>zh</dc:language><meta property="dcterms:modified">{modified}</meta></metadata>'
           f'<manifest><item id="book" href="book.xhtml" media-type="application/xhtml+xml" />'
           f'<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />{manifest}</manifest>'
           '<spine><itemref idref="book" /></spine></package>')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml" /></rootfiles></container>')
        archive.writestr('OEBPS/content.opf', opf)
        archive.writestr('OEBPS/nav.xhtml', navdoc)
        archive.writestr('OEBPS/book.xhtml', _document(title, body, True))
        for asset in assets.values():
            archive.writestr('OEBPS/assets/' + asset.name, asset.data)
    return output.getvalue()


def _font_candidates():
    yield Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf"
    for root in (Path('/usr/share/fonts'), Path('/usr/local/share/fonts')):
        if root.exists():
            yield from sorted(root.rglob('NotoSansCJK*.ttf'))


def _pdf(title, blocks, assets, font_path):
    try:
        from PIL import Image as PILImage
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Image, KeepTogether, Spacer
    except ImportError as exc:
        raise RuntimeError("PDF requires optional extra: install reportlab and Pillow") from exc
    normalized = [(kind, (value[0], value[1].replace('\u2212', '-')) if kind == 'image' else value.replace('\u2212', '-')) for kind, value in blocks]
    title = title.replace('\u2212', '-')
    texts = [title, NOTICE] + [value[1] if kind == 'image' else value for kind, value in normalized]
    required = {ord(c) for text in texts for c in text if not c.isspace()}
    candidates = [Path(font_path)] if font_path is not None else list(_font_candidates())
    selected, errors = None, []
    for candidate in candidates:
        candidate = _safe_path(candidate)
        if candidate.suffix.lower() != '.ttf' or not candidate.is_file():
            continue
        try:
            name = 'EBE-' + hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
            font = TTFont(name, str(candidate))
            missing = sorted(c for c in required if not font.face.charToGlyph.get(c))
            if missing:
                errors.append('missing_glyphs: ' + ', '.join(f'U+{c:04X}' for c in missing[:20]))
                continue
            pdfmetrics.registerFont(font)
            selected = name
            break
        except Exception as exc:
            errors.append(type(exc).__name__)
    if selected is None:
        raise ValueError('PDF font unavailable; supply --font / font_path with a covering .ttf. ' + '; '.join(errors))
    normal = ParagraphStyle('body', fontName=selected, fontSize=10, leading=16, wordWrap='CJK', spaceAfter=8)
    heading = ParagraphStyle('heading', parent=normal, fontSize=16, leading=22)
    code = ParagraphStyle('code', parent=normal, fontSize=9, leading=14)
    # Courier is used only for printable ASCII; every other code glyph uses the
    # preflighted TTF. CJK wraps at character boundaries, preserving indentation.
    def code_markup(text):
        pieces = []
        for run in re.findall(r'[\x20-\x7e]+|[^\x20-\x7e]+', text.expandtabs(4)):
            escaped = _x(run).replace(' ', '&#160;').replace('\n', '<br/>')
            pieces.append(f'<font name="Courier">{escaped}</font>' if all(32 <= ord(c) <= 126 for c in run) else escaped)
        return ''.join(pieces)
    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, title=title, author='EBE', leftMargin=48, rightMargin=48, topMargin=48, bottomMargin=48)
    story = [Paragraph(_x(title), heading)]
    for kind, value in normalized:
        if kind == 'image':
            asset = assets[value[0]]
            with PILImage.open(io.BytesIO(asset.data)) as image:
                if image.format not in ('PNG', 'JPEG') or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('only_static_png_jpeg_allowed')
                image.verify()
            caption = Paragraph(_x(value[1]), normal)
            _, caption_height = caption.wrap(doc.width - 12, doc.height)
            available = doc.height - caption_height - 36
            if available <= 0:
                raise ValueError('image_caption_too_tall')
            scale = min((doc.width - 12)/asset.width, available/asset.height, 1)
            picture = Image(io.BytesIO(asset.data), width=asset.width*scale, height=asset.height*scale)
            story.append(KeepTogether([picture, caption]))
        elif kind == 'pre':
            # Split at source lines so long code blocks can span pages.
            story.extend(Paragraph(code_markup(line) or '&#160;', code) for line in value.split('\n'))
        else:
            story.append(Paragraph(_x(value), heading if kind.startswith('h') else normal))
    story.extend([Spacer(1, 12), Paragraph(NOTICE, normal)])
    doc.build(story)
    return stream.getvalue()


def export_book(markdown: str, output: str | Path, title='EBE ebook', assets_dir=None, font_path=None):
    """Render an audited manuscript offline and atomically return its output Path.

    Image destinations are relative to assets_dir, not the current directory.
    Any validation failure leaves an existing output untouched. No font is
    needed for HTML/EPUB, whose readers choose their own display fonts.
    """
    if not isinstance(markdown, str) or not isinstance(title, str):
        raise TypeError('markdown and title must be strings')
    if any((ord(c) < 32 and c not in '\t\n\r') or 0xd800 <= ord(c) <= 0xdfff or ord(c) in (0xfffe, 0xffff) for c in markdown + title):
        raise ValueError('invalid_text_character')
    target = _safe_path(output)
    suffix = target.suffix.lower()
    if suffix not in ('.html', '.epub', '.pdf'):
        raise ValueError('output must end in .html, .epub or .pdf')
    blocks, assets = _blocks(markdown, assets_dir)
    if suffix == '.html':
        payload = _document(title, _body(blocks, assets, True)[0]).encode('utf-8')
    elif suffix == '.epub':
        payload = _epub(title, blocks, assets)
    else:
        payload = _pdf(title, blocks, assets, font_path)
    # Build entirely before opening the destination; atomic replacement also
    # prevents half-written books after validation/layout errors.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.ebe-export-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        _safe_path(target)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
