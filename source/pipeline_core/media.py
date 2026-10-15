from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path

from ffmpeg_bin import FFMPEG

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _ffmpeg_info(path: Path) -> str:
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return (proc.stderr or "") + (proc.stdout or "")


def probe_duration(path: str | Path) -> float:
    path = Path(path)
    info = _ffmpeg_info(path)
    match = _DURATION_RE.search(info)
    if not match:
        raise RuntimeError(f"Could not determine duration for {path}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def has_audio(path: str | Path) -> bool:
    return "Audio:" in _ffmpeg_info(Path(path))


def extract_audio(video_path: str | Path, output_path: str | Path) -> Path:
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(output_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"FFmpeg audio extraction failed: {proc.stderr.strip()}")
    return output_path


def sample_frame_times(duration: float, count: int = 8) -> list[float]:
    if duration <= 0:
        return [0.0]
    count = max(1, min(int(count), 16))
    if count == 1:
        return [min(duration * 0.5, max(duration - 0.05, 0.0))]
    # Avoid exact first/last frames, which are often black or transitional.
    start = min(0.35, duration * 0.1)
    end = max(start, duration - min(0.35, duration * 0.1))
    if end <= start:
        return [duration * 0.5]
    step = (end - start) / (count - 1)
    return [round(start + step * i, 3) for i in range(count)]


def extract_frames(video_path: str | Path, output_dir: str | Path, count: int = 8) -> list[tuple[float, Path]]:
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video_path)
    frames: list[tuple[float, Path]] = []
    for index, timestamp in enumerate(sample_frame_times(duration, count), start=1):
        target = output_dir / f"frame_{index:02d}_{timestamp:.3f}.jpg"
        proc = subprocess.run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(720,iw)':-2",
                "-q:v",
                "4",
                str(target),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and target.exists() and target.stat().st_size > 0:
            frames.append((timestamp, target))
    return frames


def image_data_url(path: str | Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    media_type = "image/png" if suffix == ".png" else "image/webp" if suffix == ".webp" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def temp_media_dir(prefix: str = "scraper-analysis-"):
    return tempfile.TemporaryDirectory(prefix=prefix)
