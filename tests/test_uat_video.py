"""The video step on real ffmpeg: parts are joined and shrunk into one file."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from src.app.services import uat_video

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _clip(path, seconds, size="828x1792"):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=duration={seconds}:size={size}:rate=60", "-c:v", "libx264",
                    "-crf", "10", str(path)], check=True)


@pytest.mark.asyncio
async def test_parts_are_joined_shrunk_and_reused(tmp_path):
    _clip(tmp_path / "02-walkthrough-raw.mp4", 2)
    _clip(tmp_path / "07-walkthrough-part2-raw.mp4", 1)
    raw = sum(p.stat().st_size for p in tmp_path.glob("*.mp4"))

    out = await uat_video.prepare(str(tmp_path))
    assert out.name == uat_video.FINAL_NAME and 0 < out.stat().st_size < raw
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=height,codec_type",
                            "-of", "default=nw=1", str(out)], capture_output=True, text=True).stdout
    assert "height=1280" in probe and "codec_type=audio" in probe
    assert 2.5 < float(probe.split("duration=")[1].split()[0]) < 3.6

    mtime = out.stat().st_mtime_ns
    assert (await uat_video.prepare(str(tmp_path))).stat().st_mtime_ns == mtime  # not re-encoded
    assert uat_video.sources(str(tmp_path))[-1].name == "07-walkthrough-part2-raw.mp4"  # output never an input
