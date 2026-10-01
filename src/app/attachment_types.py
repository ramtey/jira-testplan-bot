"""What a tester may attach to a workflow comment, and how each kind renders.

Two call sites validate uploads — the workflow transition routes
(``workflow_routes._validate_and_read_images``) and the walkthrough PUT
(``main.put_ticket_walkthrough``) — and each used to carry its own copy of
the allow-list. They import from here instead so a new file type is added
once.

Images and PDFs render inline in the posted Jira comment via ``mediaSingle``.
Text payloads (an API response body, a curl transcript, a log excerpt, a
markdown repro) have nothing to preview, so they render as a
``📎 <filename>`` callout and are read from the ticket's Attachments panel.
Audio (an MP3 voice note walking through what was tested) is the same story
with a ``🎧`` — there is no still frame to show, so the comment names the
file and the reader plays it from the Attachments panel.
"""

import os

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # Jira allows more; this is a sane UI cap.

_IMAGE_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
}

# Non-image payloads a tester attaches as evidence for API/HTTP work:
# a response body, a request transcript, a log excerpt, a CSV export.
_TEXT_MIME = {
    "text/plain",
    "text/markdown",
    "application/json",
    "text/csv",
}

# A spoken walkthrough of the UAT run, attached instead of typed out.
_AUDIO_MIME = {"audio/mpeg"}

ALLOWED_ATTACHMENT_MIME = _IMAGE_MIME | {"application/pdf"} | _TEXT_MIME | _AUDIO_MIME

ALLOWED_ATTACHMENT_LABEL = "PNG, JPEG, GIF, WEBP, PDF, MP3, TXT, LOG, MD, JSON, CSV"

_TEXT_EXTENSIONS = {".txt", ".log", ".md", ".json", ".csv"}

_AUDIO_EXTENSIONS = {".mp3"}

# Extensions whose attachment has no inline preview — rendered as a
# `📎`/`🎧 <filename>` callout instead of a media node. Audio is here for the
# same reason text is: `mediaSingle` is the "show the reader this picture"
# node, and an MP3 has no frame to show.
_NO_PREVIEW_EXTENSIONS = _TEXT_EXTENSIONS | _AUDIO_EXTENSIONS

# Browsers disagree about text payloads: Chrome reports `application/json`
# for a .json file, other sources report `text/plain`, a file dragged out of
# an archive or a terminal arrives as `application/octet-stream`, and some
# drops carry no type at all. Fall back to the extension in those cases
# rather than rejecting a file the tester clearly meant to attach.
_EXTENSION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
}

# Types that say nothing about the file's contents — trust the extension.
_UNINFORMATIVE_MIME = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "text/x-unknown-content-type",
}

# Spellings that mean an allowed type under another name.
_MIME_ALIASES = {
    # Safari and several recorders label an MP3 `audio/mp3`; the legacy
    # `audio/mpeg3` / `audio/x-mpeg-3` spellings still come off older tools.
    "audio/mp3": "audio/mpeg",
    "audio/x-mp3": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "audio/mpg": "audio/mpeg",
    "text/json": "application/json",
    "application/x-json": "application/json",
    "text/x-log": "text/plain",
    "text/x-markdown": "text/markdown",
    "application/csv": "text/csv",
    "text/x-csv": "text/csv",
    "text/comma-separated-values": "text/csv",
}

# Spellings that mean an allowed type only for one extension. Windows
# browsers with Excel installed label a .csv `application/vnd.ms-excel` —
# the .xls type — so that type is a CSV only when the name says so.
_EXTENSION_SCOPED_ALIASES = {
    ("application/vnd.ms-excel", ".csv"): "text/csv",
}


def _extension(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def resolve_attachment_mime(filename: str, declared_mime: str | None) -> str | None:
    """Return the mime type to upload this file with, or None if it isn't allowed.

    `declared_mime` is whatever the browser put on the multipart part, which
    may carry parameters (`text/plain; charset=utf-8`), be an alias, or be
    absent entirely.
    """
    mime = (declared_mime or "").split(";")[0].strip().lower()
    mime = _MIME_ALIASES.get(mime, mime)
    mime = _EXTENSION_SCOPED_ALIASES.get((mime, _extension(filename)), mime)
    if mime in ALLOWED_ATTACHMENT_MIME:
        return mime
    if mime in _UNINFORMATIVE_MIME:
        return _EXTENSION_MIME.get(_extension(filename))
    return None


def has_inline_preview(filename: str) -> bool:
    """True when this attachment should render as a media node in the comment.

    Text and audio payloads don't — a `mediaSingle` around a .json or an .mp3
    shows the reader a broken picture where a filename would have done.
    """
    return _extension(filename) not in _NO_PREVIEW_EXTENSIONS


def attachment_icon(filename: str) -> str:
    """Emoji prefix for an attachment's text callout."""
    if _extension(filename) in _AUDIO_EXTENSIONS:
        return "🎧"
    return "📷" if has_inline_preview(filename) else "📎"
