"""A UAT run's walkthrough video: shrink it, then publish it where the team watches.

The runner leaves raw simulator or browser recordings in the run's evidence
directory, often in parts and far over Jira's attachment cap (run 27 left
46 MB + 177 MB). On approval the parts are joined and re-encoded with ffmpeg
(~1 MB per 90 s), and the result is published to the host named by
``UAT_VIDEO_HOST``:

* ``jira`` (default): attached to the ticket. Works on any machine, no setup.
* ``drive_folder``: copied into a folder that Google Drive for desktop syncs
  (``UAT_VIDEO_DRIVE_DIR``); the comment carries the Drive link, which plays
  in the browser like a Loom. Needs the Drive app signed in on the machine
  that approves.

If the chosen host fails, the video falls back to a Jira attachment and the
comment says why, so a video is never dropped without a word.

MOVING MACHINES (Mac mini, hosted hub): the host is the only thing to change.
A machine without the Drive app either signs one in or sets
``UAT_VIDEO_HOST=jira``. A server needs a host that does not depend on a
desktop app: a Drive API host with a Google service account (ask IT), or the
hub serving the file itself behind SkySlope login. Add it here as one more
``_publish_<host>`` function; the comment and tracker shapes stay the same.
Loom has no upload API (see the Loom note in docs/design/uat-loop.md), so a
Loom host would mean a browser agent; build it only if someone asks.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from ..attachment_types import MAX_ATTACHMENT_BYTES
from ..config import settings

logger = logging.getLogger(__name__)

VIDEO_HOSTS = ("jira", "drive_folder")
FINAL_NAME = "walkthrough-final.mp4"
DRIVE_ID_XATTR = "com.google.drivefs.item-id#S"
_FFMPEG_TIMEOUT_S = 600


class VideoError(RuntimeError):
    """The video could not be prepared or published."""


def sources(evidence_dir: str | None) -> list[Path]:
    """The recordings a run's video is made from, in order.

    A narrated cut, when there is one, is already the finished video. Otherwise
    every raw ``*walkthrough*.mp4`` part is used, in file-name order (the
    runner numbers its files).
    """
    if not evidence_dir:
        return []
    root = Path(evidence_dir).expanduser()
    if not root.is_dir():
        return []
    videos = sorted(p for p in root.glob("*.mp4") if "walkthrough" in p.name and p.name != FINAL_NAME)
    narrated = [p for p in videos if "narrated" in p.name]
    return narrated[-1:] if narrated else videos


def plan(evidence_dir: str | None) -> dict | None:
    """What the review panel shows before approval: the parts and the host."""
    parts = sources(evidence_dir)
    if not parts:
        return None
    return {"name": FINAL_NAME, "parts": [p.name for p in parts],
            "raw_bytes": sum(p.stat().st_size for p in parts), "host": host()}


def host() -> str:
    h = (settings.uat_video_host or "jira").strip().lower()
    return h if h in VIDEO_HOSTS else "jira"


async def _run(*cmd: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"timed out after {timeout:.0f}s"
    return proc.returncode, (out + err).decode(errors="replace")


async def prepare(evidence_dir: str) -> Path:
    """Join and shrink the run's recordings into ``walkthrough-final.mp4``.

    Reuses an existing output newer than every part, so a retried approval
    does not re-encode. Raises VideoError with ffmpeg's last lines on failure.
    """
    parts = sources(evidence_dir)
    if not parts:
        raise VideoError("no walkthrough recording in the evidence folder")
    root = parts[0].parent
    out = root / FINAL_NAME
    if out.exists() and out.stat().st_mtime >= max(p.stat().st_mtime for p in parts):
        return out
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        raise VideoError("ffmpeg is not installed on this machine")
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "parts.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        partial = Path(tmp) / FINAL_NAME
        # The recipe from the screen-recording work: 30 fps, long side at most
        # 1280, H.264 CRF 28, a silent AAC track (simulator recordings have no
        # audio, and players treat a missing track inconsistently).
        scale = ("scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))',fps=30")
        code, log = await _run(
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v", "-map", "1:a", "-shortest", "-vf", scale,
            "-c:v", "libx264", "-crf", "28", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(partial),
            timeout=_FFMPEG_TIMEOUT_S)
        if code != 0 or not partial.exists() or partial.stat().st_size == 0:
            tail = " ".join(log.strip().splitlines()[-3:])[:300]
            raise VideoError(f"ffmpeg could not make the video: {tail or f'exit {code}'}")
        shutil.move(str(partial), out)
    return out


async def _drive_id(path: Path, wait_s: float) -> str | None:
    """The Drive file id Drive for desktop puts on a synced file, once it has one."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        code, out = await _run("xattr", "-p", DRIVE_ID_XATTR, str(path), timeout=10)
        if code == 0 and out.strip():
            return out.strip()
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(2)


async def _publish_drive_folder(video: Path, *, ticket_key: str, run_id: int) -> dict:
    folder = Path(settings.uat_video_drive_dir or "").expanduser()
    if not settings.uat_video_drive_dir or not folder.is_dir():
        raise VideoError(f"Drive folder not found: {settings.uat_video_drive_dir or 'UAT_VIDEO_DRIVE_DIR unset'}"
                         " (is Google Drive for desktop running and signed in?)")
    dest_dir = folder / ticket_key
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"run-{run_id}-walkthrough.mp4"
    shutil.copyfile(video, dest)
    file_id = await _drive_id(dest, settings.uat_video_drive_wait_s)
    if not file_id:
        raise VideoError(f"Drive gave no file id within {settings.uat_video_drive_wait_s}s "
                         "(Drive for desktop may be paused or offline)")
    return {"host": "drive_folder", "name": dest.name, "bytes": dest.stat().st_size,
            "link": f"https://drive.google.com/file/d/{file_id}/view", "drive_id": file_id}


async def _attach_to_jira(video: Path, *, jira, ticket_key: str) -> dict:
    size = video.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        return {"host": "jira", "name": video.name, "bytes": size, "too_large": True}
    uploaded = await jira.upload_attachments(ticket_key, [(video.name, video.read_bytes(), "video/mp4")])
    return {"host": "jira", "name": video.name, "bytes": size,
            "attachment_id": str((uploaded or [{}])[0].get("id"))}


async def publish(evidence_dir: str | None, *, jira, ticket_key: str, run_id: int) -> dict | None:
    """Prepare and publish the run's video. None when the run recorded none.

    Returns what the comment needs: ``link`` (Drive), ``attachment_id``
    (Jira), ``too_large``, or ``error`` when it could not be made at all; plus
    ``fallback_reason`` when the chosen host failed and Jira took the video.
    A Jira upload error propagates, so the caller can stop and resume.
    """
    if not sources(evidence_dir):
        return None
    try:
        video = await prepare(evidence_dir)
    except VideoError as exc:
        logger.warning("UAT run %s: video not prepared: %s", run_id, exc)
        return {"host": host(), "error": str(exc)}
    fallback = None
    if host() == "drive_folder":
        try:
            return await _publish_drive_folder(video, ticket_key=ticket_key, run_id=run_id)
        except (VideoError, OSError) as exc:
            logger.warning("UAT run %s: Drive publish failed, attaching to Jira: %s", run_id, exc)
            fallback = str(exc)
    out = await _attach_to_jira(video, jira=jira, ticket_key=ticket_key)
    if fallback:
        out["fallback_reason"] = fallback
    return out
