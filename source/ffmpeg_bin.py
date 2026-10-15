from __future__ import annotations

import os
import shutil


def _find_ffmpeg() -> str:
    explicit = os.getenv("FFMPEG_BINARY")
    if explicit:
        return explicit
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "FFmpeg was not found. Install it with `brew install ffmpeg` or install requirements-analysis.txt."
        ) from exc


FFMPEG = _find_ffmpeg()
