"""Synthetic fixtures only: no source books, private images or network."""
import builtins
import io
from pathlib import Path
import socket
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

import pytest

from ebe import export


def png(width=2, height=2, extra=b''):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + extra + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * width) * height)) + chunk(b'IEND', b''))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / 'assets'
    root.mkdir()
    (root / 'test.png').write_bytes(png())
    return root


def test_html_injection_and_embedded_image(tmp_path, assets):
    out = tmp_path / 'book.html'
    export.export_book('# Title\n\n<script>alert(1)</script><img src="https://evil/x">\n\n'
                       '[click](javascript:alert(1))\n\n![<evil>](test.png)\n\n```html\n<script>bad</script>\n```', out,
                       title='<svg onload="bad">', assets_dir=assets)
    text = out.read_text('utf-8')
    assert '<script>' not in text and '<svg' not in text
    assert '&lt;script&gt;' in text and '&lt;evil&gt;' in text
    assert 'src="https:' not in text and '<a ' not in text
    assert 'data:image/png;base64,' in text and 'Content-Security-Policy' in text
    assert export.NOTICE in text


@pytest.mark.parametrize('reference', ['../test.png', '%2e%2e/test.png', '/etc/passwd',
    'C:/secret.png', 'file:///secret.png', 'https://example.com/a.png', '//host/a.png',
    '..\\a.png', 'test.png:stream', 'missing.png', '%252e%252e/test.png'])
@pytest.mark.parametrize('suffix', ['.html', '.epub'])
def test_assets_fail_closed(tmp_path, assets, reference, suffix):
    out = tmp_path / ('book' + suffix)
    out.write_bytes(b'previous')
    with pytest.raises(ValueError):
        export.export_book(f'![bad]({reference})', out, assets_dir=assets)
    assert out.read_bytes() == b'previous'


def test_assets_required(tmp_path):
    with pytest.raises(ValueError):
        export.export_book('![x](test.png)', tmp_path / 'book.html')


def test_network_filesystem_refused_before_stat(tmp_path, monkeypatch):
    original = Path.lstat
    def guard(path, *args, **kwargs):
        assert not str(path).replace('\\', '/').startswith('//'), 'UNC stat would use network'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', guard)
    with pytest.raises(ValueError, match='network_or_device'):
        export.export_book('![x](test.png)', tmp_path / 'book.html', assets_dir='//server/share')
    with pytest.raises(ValueError, match='network_or_device'):
        export.export_book('text', '//server/share/book.html')


def test_symlink_guard(tmp_path, assets, monkeypatch):
    # lstat-based reparse detection is tested even on Windows without symlink privilege.
    real = Path.lstat
    def linked(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if path.name == 'test.png':
            import types
            return types.SimpleNamespace(st_mode=0o120777, st_file_attributes=0)
        return info
    monkeypatch.setattr(Path, 'lstat', linked)
    with pytest.raises(ValueError, match='symlink'):
        export.export_book('![x](test.png)', tmp_path / 'book.html', assets_dir=assets)


def test_real_directory_symlink(tmp_path, assets):
    link = assets / 'linked'
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    with pytest.raises(ValueError, match='symlink'):
        export.export_book('![x](linked/assets/test.png)', tmp_path / 'book.html', assets_dir=assets)


@pytest.mark.parametrize('data', [b'<svg/>', b'GIF89a', b'not an image', png()[:-4], png() + b'extra'])
def test_bad_images(tmp_path, assets, data):
    (assets / 'test.png').write_bytes(data)
    with pytest.raises(ValueError):
        export.export_book('![x](test.png)', tmp_path / 'book.html', assets_dir=assets)


def test_limits_and_animation(tmp_path, assets, monkeypatch):
    monkeypatch.setattr(export, 'MAX_PIXELS', 3)
    with pytest.raises(ValueError, match='pixel'):
        export.export_book('![x](test.png)', tmp_path / 'book.html', assets_dir=assets)
    monkeypatch.setattr(export, 'MAX_PIXELS', 20_000_000)
    monkeypatch.setattr(export, 'MAX_IMAGE_BYTES', 5)
    with pytest.raises(ValueError, match='byte'):
        export.export_book('![x](test.png)', tmp_path / 'book.html', assets_dir=assets)
    monkeypatch.setattr(export, 'MAX_IMAGE_BYTES', 20 * 1024 * 1024)
    data = b'acTL' + struct.pack('>II', 1, 0)
    extra = struct.pack('>I', 8) + data + struct.pack('>I', zlib.crc32(data) & 0xffffffff)
    (assets / 'test.png').write_bytes(png(extra=extra))
    with pytest.raises(ValueError, match='animated'):
        export.export_book('![x](test.png)', tmp_path / 'book.html', assets_dir=assets)


def test_epub_opens_and_references_resolve(tmp_path, assets):
    out = tmp_path / 'book.epub'
    export.export_book('# 第一章\n\n![图](test.png)\n\n## 第二节\n\n正文', out, title='书 & <标题>', assets_dir=assets)
    with zipfile.ZipFile(out) as book:
        assert book.testzip() is None
        first = book.infolist()[0]
        assert first.filename == 'mimetype' and first.compress_type == zipfile.ZIP_STORED
        assert book.read('mimetype') == b'application/epub+zip'
        container = ET.fromstring(book.read('META-INF/container.xml'))
        opf_path = next(container.iter('{urn:oasis:names:tc:opendocument:xmlns:container}rootfile')).attrib['full-path']
        opf = ET.fromstring(book.read(opf_path))
        ns = {'p': 'http://www.idpf.org/2007/opf'}
        items = opf.findall('p:manifest/p:item', ns)
        for item in items:
            assert 'OEBPS/' + item.attrib['href'] in book.namelist()
        assert any(i.attrib.get('properties') == 'nav' for i in items)
        assert opf.find('p:spine/p:itemref', ns).attrib['idref'] == 'book'
        nav = ET.fromstring(book.read('OEBPS/nav.xhtml'))
        body = ET.fromstring(book.read('OEBPS/book.xhtml'))
        ids = {e.attrib['id'] for e in body.iter() if 'id' in e.attrib}
        for link in nav.iter('{http://www.w3.org/1999/xhtml}a'):
            assert link.attrib['href'].split('#')[1] in ids
        assert len([n for n in book.namelist() if '/assets/' in n]) == 1


def test_stdlib_only(tmp_path, assets, monkeypatch):
    original = builtins.__import__
    def restricted(name, *args, **kwargs):
        if name.split('.')[0] in ('PIL', 'reportlab'):
            raise ImportError('optional dependency deliberately absent')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', restricted)
    for ext in ('html', 'epub'):
        export.export_book('![x](test.png)', tmp_path / ('book.' + ext), assets_dir=assets)
    with pytest.raises(RuntimeError, match='optional extra'):
        export.export_book('text', tmp_path / 'book.pdf')
    assert not (tmp_path / 'book.pdf').exists()


def pdf_font(tmp_path=None):
    reportlab = pytest.importorskip('reportlab')
    pytest.importorskip('PIL')
    font = Path(reportlab.__file__).parent / 'fonts/Vera.ttf'
    if tmp_path is not None:
        # Some Linux distributions symlink bundled fonts to system fonts.
        # Production intentionally refuses symlinks; use a regular fixture.
        fixture = tmp_path / 'fixture-font.ttf'
        fixture.write_bytes(font.read_bytes())
        return fixture
    return font


def test_pdf_missing_font_and_glyphs(tmp_path, monkeypatch):
    font = pdf_font(tmp_path)
    monkeypatch.setattr(export, '_font_candidates', lambda: iter(()))
    with pytest.raises(ValueError, match='font unavailable'):
        export.export_book('中文', tmp_path / 'book.pdf')
    with pytest.raises(ValueError, match='missing_glyphs'):
        export.export_book('中文', tmp_path / 'book.pdf', font_path=font)
    assert not (tmp_path / 'book.pdf').exists()


def test_pdf_opens_and_minus_normalizes(tmp_path, assets):
    font = pdf_font(tmp_path)
    out = tmp_path / 'book.pdf'
    export.export_book('# Formula\n\n```\nx = 2 − 1\n```\n\n![Caption](test.png)', out, assets_dir=assets, font_path=font)
    data = out.read_bytes()
    assert data.startswith(b'%PDF-') and b'%%EOF' in data[-30:]
    reader = pytest.importorskip('pypdf').PdfReader(out)
    text = ''.join(page.extract_text() for page in reader.pages)
    assert '2 - 1' in text and 'Caption' in text


def test_pdf_chinese_long_code_and_caption(tmp_path, assets):
    pdf_font()
    reader_module = pytest.importorskip('pypdf')
    fonts = [path for path in export._font_candidates() if path.is_file()]
    if not fonts:
        pytest.skip('no supported Chinese TTF installed')
    out = tmp_path / 'chinese.pdf'
    markdown = '# 中文标题\n\n' + ('正文内容。' * 40 + '\n\n') * 15
    markdown += '\n```text\n    数值 = 3 − 1\n' + 'a' * 600 + '\n```\n\n![独特图注](test.png)'
    export.export_book(markdown, out, title='中文电子书', assets_dir=assets, font_path=fonts[0])
    reader = reader_module.PdfReader(out)
    texts = [page.extract_text() for page in reader.pages]
    assert '中文电子书' in texts[0]
    assert any('数值' in text for text in texts)
    caption_pages = [page for page in reader.pages if '独特图注' in page.extract_text()]
    assert len(caption_pages) == 1 and len(caption_pages[0].images) == 1
    assert len(reader.pages) > 1


def test_font_coverage_checks_title_caption_code(tmp_path, assets):
    font = pdf_font(tmp_path)
    for title, manuscript in [('中', 'ASCII'), ('ASCII', '![中](test.png)'), ('ASCII', '```\n中\n```')]:
        with pytest.raises(ValueError, match='missing_glyphs'):
            export.export_book(manuscript, tmp_path / 'book.pdf', title=title, assets_dir=assets, font_path=font)
    assert not (tmp_path / 'book.pdf').exists()


def test_jpeg_static(tmp_path, assets):
    Image = pytest.importorskip('PIL.Image')
    stream = io.BytesIO()
    Image.new('RGB', (3, 2)).save(stream, 'JPEG')
    (assets / 'test.jpg').write_bytes(stream.getvalue())
    export.export_book('![jpeg](test.jpg)', tmp_path / 'book.html', assets_dir=assets)
    assert 'data:image/jpeg;base64,' in (tmp_path / 'book.html').read_text()


def test_code_never_loads_images(tmp_path):
    export.export_book('```\n![x](https://evil/x)\n```', tmp_path / 'book.html')


def test_invalid_suffix_and_xml_text(tmp_path):
    for text, name in [('text', 'book.txt'), ('bad\x00', 'book.epub')]:
        with pytest.raises(ValueError):
            export.export_book(text, tmp_path / name)
        assert not (tmp_path / name).exists()
