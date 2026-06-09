"""
tests/test_doc_extract.py
--------------------------
Functional-document text extraction (.docx dependency-free, text, errors)
and the /api/v1/docs/extract endpoint.
"""
from __future__ import annotations
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from ingestion.doc_extract import extract_text


def _docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
        z.writestr("[Content_Types].xml", "<x/>")
    return buf.getvalue()


def test_extract_docx():
    text, err = extract_text("Spec.docx", _docx(["AC1: reject negatives.", "AC2: idempotent."]))
    assert err is None
    assert "AC1: reject negatives." in text and "AC2: idempotent." in text


def test_extract_text_file():
    text, err = extract_text("req.md", b"# Title\nA requirement.")
    assert err is None and "requirement" in text


def test_legacy_doc_rejected():
    _, err = extract_text("old.doc", b"\xd0\xcf\x11\xe0junk")
    assert err and ".docx" in err


def test_bad_docx_reports_error():
    _, err = extract_text("broken.docx", b"not a zip")
    assert err


def test_unsupported_type():
    _, err = extract_text("image.png", b"\x89PNG")
    assert err and "Unsupported" in err


@pytest.fixture
def client():
    from api.app import create_app
    return TestClient(create_app())


def test_extract_endpoint(client):
    r = client.post("/api/v1/docs/extract?filename=Spec.docx", content=_docx(["Hello requirement"]))
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "Hello requirement" and body["words"] == 2


def test_extract_endpoint_empty(client):
    r = client.post("/api/v1/docs/extract?filename=x.docx", content=b"")
    assert r.status_code == 400
