"""
ingestion/doc_extract.py
-------------------------
Extract plain text from uploaded functional-document files.

  • .docx  — dependency-free (a .docx is a zip; we read word/document.xml and
             pull the <w:t> runs, paragraph by paragraph). No python-docx needed.
  • .pdf   — best-effort via pypdf / PyPDF2 if installed; otherwise a clear error.
  • .txt/.md/.csv/.json/... — decoded as UTF-8.

Returns (text, error). On success error is None.
"""
from __future__ import annotations
import html
import io
import re
import zipfile

_MAX_CHARS = 200_000   # safety cap on extracted text

_TEXT_EXT = (".txt", ".md", ".markdown", ".csv", ".json", ".text", ".adoc",
             ".rst", ".log", ".yaml", ".yml", ".html", ".xml")


def _docx_text(data: bytes) -> tuple[str, str | None]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except KeyError:
        return "", "Not a valid .docx (missing word/document.xml)."
    except zipfile.BadZipFile:
        return "", "File is not a valid .docx (old .doc binary format is unsupported — save as .docx)."

    paras = re.findall(r"<w:p\b.*?</w:p>", xml, re.DOTALL)
    lines: list[str] = []
    for p in paras:
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.DOTALL)
        line = html.unescape("".join(runs)).strip()
        if line:
            lines.append(line)
    if not lines:                      # fallback: strip all tags
        stripped = html.unescape(re.sub(r"<[^>]+>", " ", xml))
        lines = [l.strip() for l in stripped.splitlines() if l.strip()]
    return "\n".join(lines), None


def _pdf_text(data: bytes) -> tuple[str, str | None]:
    Reader = None
    try:
        from pypdf import PdfReader as Reader            # noqa: N814
    except Exception:
        try:
            from PyPDF2 import PdfReader as Reader        # noqa: N814
        except Exception:
            return "", ("PDF parsing isn’t available on this server. "
                        "Install pypdf, or export the PDF to .txt/.docx.")
    try:
        reader = Reader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages), None
    except Exception as exc:
        return "", f"Could not read PDF: {exc}"


def extract_text(filename: str, data: bytes) -> tuple[str, str | None]:
    name = (filename or "").lower()
    if name.endswith(".docx"):
        text, err = _docx_text(data)
    elif name.endswith(".pdf"):
        text, err = _pdf_text(data)
    elif name.endswith(".doc"):
        return "", "Legacy .doc binary format is unsupported — save as .docx or .txt."
    elif name.endswith(_TEXT_EXT) or "." not in name:
        try:
            text, err = data.decode("utf-8", "ignore"), None
        except Exception as exc:
            return "", f"Could not decode text file: {exc}"
    else:
        return "", f"Unsupported file type: {filename}. Use .docx, .pdf, or a text format."

    if err:
        return "", err
    text = (text or "").strip()
    if not text:
        return "", "No extractable text found in the document."
    return text[:_MAX_CHARS], None
