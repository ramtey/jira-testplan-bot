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


def test_resolve_accepts_text_and_json_payloads():
    assert resolve_attachment_mime("curl.txt", "text/plain") == "text/plain"
    assert resolve_attachment_mime("resp.json", "application/json") == "application/json"


def test_resolve_strips_charset_parameter():
    assert resolve_attachment_mime("curl.txt", "text/plain; charset=utf-8") == "text/plain"


def test_resolve_normalizes_json_aliases():
    assert resolve_attachment_mime("resp.json", "text/json") == "application/json"


def test_resolve_falls_back_to_extension_when_browser_sends_nothing():
    # Dragging a .json out of Finder or an archive can arrive with an empty
    # or generic type; the extension decides rather than a 400.
    assert resolve_attachment_mime("resp.json", "") == "application/json"
    assert resolve_attachment_mime("resp.json", None) == "application/json"
    assert (
        resolve_attachment_mime("log.txt", "application/octet-stream") == "text/plain"
    )


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


def test_attachment_icon_matches_preview_capability():
    assert attachment_icon("shot.png") == "📷"
    assert attachment_icon("resp.json") == "📎"


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
        # No content-type at all — resolved from the extension.
        _upload("trace.json", b"[]", None),
    ])
    assert [(name, mime) for name, _, mime in files] == [
        ("resp.json", "application/json"),
        ("curl.txt", "text/plain"),
        ("trace.json", "application/json"),
    ]
    assert files[0][1] == b'{"ok":true}'


@pytest.mark.asyncio
async def test_validate_rejects_a_type_outside_the_allow_list():
    with pytest.raises(HTTPException) as exc:
        await _validate_and_read_images([_upload("bundle.zip", b"PK", "application/zip")])
    assert exc.value.status_code == 400
    assert "application/zip" in exc.value.detail
    assert "TXT, JSON" in exc.value.detail
