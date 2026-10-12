"""Runnable boundary checks; deliberately not executed during implementation.

Run: python -m backend.tests_files
DB/HTTP acceptance requires the full integration routes, real DB and a legally
provisioned account. This module does not fabricate users or a license grant.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import io
import hashlib
import zipfile
import xml.etree.ElementTree as ET
from .domains.files_support import safe_name, store_stream, decoded
from .domains.upload import ChunkReader, session_view, CHUNK_SIZE
from .domains.reports import table_html, period, generate_sign
from .domains.search import extract_text, sql_literal
from .dav import normalized_path, xml_parse, DavError
from .errors import ApiError


def raises(kind, call):
    try:
        call()
    except kind:
        return
    raise AssertionError('Expected ' + kind.__name__)


def run():
    for name in ('../escape', '.', '..', 'a/b', 'a\\b', 'bad\0name', 'bad:name'):
        raises(ApiError, lambda: safe_name(name))
    assert safe_name('项目文件 你好.txt') == '项目文件 你好.txt'
    assert normalized_path('/files/项目/你好.txt/') == 'files/项目/你好.txt'
    raises(DavError, lambda: normalized_path('files/../private'))
    raises(DavError, lambda: xml_parse(b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///secret">]><a>&x;</a>'))
    assert xml_parse(b'<d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>').tag == '{DAV:}propfind'
    assert "\\'" in sql_literal("a'b")
    state = {'id': 'a' * 32, 'size': CHUNK_SIZE + 1, 'received': '{"1":{},"0":{}}'}
    assert session_view(state) == {'done': False, 'upload_id': 'a' * 32, 'chunk_size': CHUNK_SIZE, 'chunk_count': 2, 'received': [0, 1]}
    with TemporaryDirectory() as directory:
        settings = SimpleNamespace(storage_root=Path(directory))
        raw = '真实字节\n'.encode()
        stored = store_stream(settings, io.BytesIO(raw), expected_hash=hashlib.md5(raw).hexdigest())
        assert (settings.storage_root / stored['url']).read_bytes() == raw
        assert stored['size'] == len(raw) and stored['sha256'] == hashlib.sha256(raw).hexdigest()
        with (settings.storage_root / stored['url']).open('rb') as source:
            duplicate = store_stream(settings, source)
            assert duplicate == stored
            assert source.seek(0) == 0 and source.read() == raw
        raises(ApiError, lambda: store_stream(settings, io.BytesIO(raw), maximum=1))
        raises(ApiError, lambda: store_stream(settings, io.BytesIO(raw), expected_hash='0' * 32))
        first, second = Path(directory) / 'one', Path(directory) / 'two'
        first.write_bytes(b'abc')
        second.write_bytes(b'def')
        reader = ChunkReader([first, second])
        try:
            assert b''.join(iter(lambda: reader.read(2), b'')) == b'abcdef'
        finally:
            reader.close()
        text = extract_text(settings.storage_root / stored['url'], 'txt', 1, 2)
        assert text['content'] == '实字' and text['has_more']
        document = Path(directory) / 'sample.docx'
        with zipfile.ZipFile(document, 'w') as archive:
            archive.writestr('word/document.xml', '<w:document xmlns:w="urn:w"><w:p><w:t>真实Office文本</w:t></w:p></w:document>')
        assert '真实Office文本' in extract_text(document, 'docx')['content']
    rendered = table_html(['列一', '列二', '列三'], [['A', 'B', 'C']])
    root = ET.fromstring(rendered)
    assert len(root.findall('./thead/tr/th')) == 3
    assert len(root.findall('./tbody/tr/td')) == 3
    start, end = period('weekly', 1)
    assert start.weekday() == 0 and end.weekday() == 6
    assert generate_sign(42, 'daily', start) != generate_sign(42, 'daily', start.replace(day=1)) or start.day == 1


if __name__ == '__main__':
    run()
