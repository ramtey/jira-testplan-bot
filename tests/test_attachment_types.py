"""
Tests for `src.app.attachment_types` — the shared allow-list behind both
upload entry points (the workflow transition routes and the walkthrough PUT).
"""

import io

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from src.app.attachment_types import (
    attachment_icon,
    has_inline_preview,
    resolve_attachment_mime,
)


def test_resolve_accepts_images_and_pdf_unchanged():
    assert resolve_attachment_mime("shot.png", "image/png") == "image/png"
    assert resolve_attachment_mime("doc.pdf", "application/pdf") == "application/pdf"


def test_resolve_accepts_text_markdown_and_json_payloads():
    assert resolve_attachment_mime("curl.txt", "text/plain") == "text/plain"
    assert resolve_attachment_mime("repro.md", "text/markdown") == "text/markdown"
    assert resolve_attachment_mime("resp.json", "application/json") == "application/json"
    assert resolve_attachment_mime("api.log", "text/plain") == "text/plain"


def test_resolve_accepts_an_mp3_voice_note():
    assert resolve_attachment_mime("uat-notes.mp3", "audio/mpeg") == "audio/mpeg"


def test_resolve_normalizes_the_mp3_spellings_browsers_actually_send():
    # Safari says audio/mp3; older recorders still emit the mpeg3 spellings.
    assert resolve_attachment_mime("notes.mp3", "audio/mp3") == "audio/mpeg"
    assert resolve_attachment_mime("notes.mp3", "audio/x-mp3") == "audio/mpeg"
    assert resolve_attachment_mime("notes.mp3", "audio/mpeg3") == "audio/mpeg"
    assert resolve_attachment_mime("notes.mp3", "audio/x-mpeg-3") == "audio/mpeg"
    assert resolve_attachment_mime("notes.mp3", "audio/mpg") == "audio/mpeg"


def test_resolve_falls_back_to_the_extension_for_an_mp3_with_no_type():
    # A file dragged out of Finder or an archive arrives untyped.
    assert resolve_attachment_mime("notes.mp3", "") == "audio/mpeg"
    assert resolve_attachment_mime("notes.mp3", None) == "audio/mpeg"
    assert resolve_attachment_mime("notes.mp3", "application/octet-stream") == "audio/mpeg"


def test_resolve_rejects_audio_formats_outside_the_allow_list():
    # Only MP3 is allowed — a .m4a or .wav still bounces.
    assert resolve_attachment_mime("notes.m4a", "audio/mp4") is None
    assert resolve_attachment_mime("notes.wav", "audio/wav") is None
    assert resolve_attachment_mime("notes.m4a", "application/octet-stream") is None


def test_resolve_strips_charset_parameter():
    assert resolve_attachment_mime("curl.txt", "text/plain; charset=utf-8") == "text/plain"


def test_resolve_normalizes_aliases():
    assert resolve_attachment_mime("resp.json", "text/json") == "application/json"
    assert resolve_attachment_mime("repro.md", "text/x-markdown") == "text/markdown"
    assert resolve_attachment_mime("api.log", "text/x-log") == "text/plain"


def test_resolve_accepts_csv_under_every_browser_spelling():
    assert resolve_attachment_mime("export.csv", "text/csv") == "text/csv"
    assert resolve_attachment_mime("export.csv", "application/csv") == "text/csv"
    assert resolve_attachment_mime("export.csv", "text/x-csv") == "text/csv"
    assert resolve_attachment_mime("export.csv", "") == "text/csv"
    # Windows + Excel labels a .csv with the .xls type.
    assert resolve_attachment_mime("export.CSV", "application/vnd.ms-excel") == "text/csv"
    # ...which must not let a real .xls through.
    assert resolve_attachment_mime("book.xls", "application/vnd.ms-excel") is None
    assert has_inline_preview("export.csv") is False
    assert attachment_icon("export.csv") == "📎"


def test_resolve_falls_back_to_extension_when_browser_sends_nothing():
    # Dragging a .json out of Finder or an archive can arrive with an empty
    # or generic type; the extension decides rather than a 400.
    assert resolve_attachment_mime("resp.json", "") == "application/json"
    assert resolve_attachment_mime("resp.json", None) == "application/json"
    assert (
        resolve_attachment_mime("log.txt", "application/octet-stream") == "text/plain"
    )
    # A .md is the one that most often arrives mislabelled — Chrome says
    # text/markdown, Finder drags say nothing at all.
    assert resolve_attachment_mime("repro.md", "") == "text/markdown"
    # A .log carries no registered type, so every browser leaves it blank.
    assert resolve_attachment_mime("api.log", "") == "text/plain"
    assert resolve_attachment_mime("api.log", "application/octet-stream") == "text/plain"


def test_resolve_rejects_unknown_extension_behind_a_generic_mime():
    assert resolve_attachment_mime("payload.zip", "application/octet-stream") is None
    assert resolve_attachment_mime("noext", "") is None


def test_resolve_rejects_a_named_type_outside_the_allow_list():
    assert resolve_attachment_mime("archive.zip", "application/zip") is None
    assert resolve_attachment_mime("clip.mov", "video/quicktime") is None


def test_inline_preview_only_for_non_text_attachments():
    assert has_inline_preview("shot.PNG") is True
    assert has_inline_preview("doc.pdf") is True
    assert has_inline_preview("resp.JSON") is False
    assert has_inline_preview("curl.txt") is False
    assert has_inline_preview("repro.md") is False
    assert has_inline_preview("api.LOG") is False
    # An mp3 has no still frame, so it takes the callout path too.
    assert has_inline_preview("notes.MP3") is False


def test_attachment_icon_matches_preview_capability():
    assert attachment_icon("shot.png") == "📷"
    assert attachment_icon("resp.json") == "📎"
    assert attachment_icon("repro.md") == "📎"


def test_attachment_icon_marks_audio_as_something_to_listen_to():
    assert attachment_icon("uat-notes.mp3") == "🎧"
    assert attachment_icon("UAT-NOTES.MP3") == "🎧"


# ---------- the workflow route's upload validator ----------

from src.app.workflow_routes import _validate_and_read_images


def _upload(filename: str, content: bytes, content_type: str | None) -> UploadFile:
    headers = Headers({"content-type": content_type}) if content_type is not None else Headers({})
    return UploadFile(file=io.BytesIO(content), filename=filename, headers=headers)


@pytest.mark.asyncio
async def test_validate_reads_text_attachments_with_resolved_mime():
    files = await _validate_and_read_images([
        _upload("resp.json", b'{"ok":true}', "application/json"),
        _upload("curl.txt", b"HTTP/2 200", "text/plain; charset=utf-8"),
        _upload("repro.md", b"# Steps", "text/markdown"),
        # No content-type at all — resolved from the extension.
        _upload("trace.json", b"[]", None),
    ])
    assert [(name, mime) for name, _, mime in files] == [
        ("resp.json", "application/json"),
        ("curl.txt", "text/plain"),
        ("repro.md", "text/markdown"),
        ("trace.json", "application/json"),
    ]
    assert files[0][1] == b'{"ok":true}'


@pytest.mark.asyncio
async def test_validate_reads_an_mp3_voice_note():
    files = await _validate_and_read_images([
        _upload("uat-notes.mp3", b"ID3\x03", "audio/mpeg"),
        # Safari's spelling, and a Finder drag with no type at all.
        _upload("take-2.mp3", b"ID3\x03", "audio/mp3"),
        _upload("take-3.mp3", b"ID3\x03", None),
    ])
    assert [(name, mime) for name, _, mime in files] == [
        ("uat-notes.mp3", "audio/mpeg"),
        ("take-2.mp3", "audio/mpeg"),
        ("take-3.mp3", "audio/mpeg"),
    ]


@pytest.mark.asyncio
async def test_validate_rejects_a_type_outside_the_allow_list():
    with pytest.raises(HTTPException) as exc:
        await _validate_and_read_images([_upload("bundle.zip", b"PK", "application/zip")])
    assert exc.value.status_code == 400
    assert "application/zip" in exc.value.detail
    assert "MP3, TXT, LOG, MD, JSON, CSV" in exc.value.detail
