from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
from functools import lru_cache
from pathlib import Path

from ffmpeg_bin import FFMPEG
from pipeline_core.media import has_audio


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "FFmpeg command failed")


def _escape_concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _ass_time(seconds: float) -> str:
    centis = max(0, int(round(float(seconds) * 100)))
    h, rem = divmod(centis, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean_caption(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > 150:
        text = text[:147].rstrip() + "..."
    return text


def _ass_escape_text(text: str, *, width: int) -> str:
    cleaned = _clean_caption(text).replace("{", "(").replace("}", ")").replace("\\", "/")
    if not cleaned:
        return ""
    return textwrap.fill(cleaned, width=width).replace("\n", r"\N")


def _timeline_duration(segments: list[dict]) -> float:
    return sum(
        max(0.0, float(segment.get("gap_before", 0.0) or 0.0))
        + max(0.0, float(segment["end"]) - float(segment["start"]))
        for segment in segments
    )


def _caption_events(segments: list[dict]) -> list[tuple[float, float, str]]:
    events: list[tuple[float, float, str]] = []
    cursor = 0.0
    for segment in segments:
        cursor += max(0.0, float(segment.get("gap_before", 0.0) or 0.0))
        duration = max(0.0, float(segment["end"]) - float(segment["start"]))
        text = _clean_caption(segment.get("text") or "")
        if text and duration >= 0.4:
            words = text.split()
            if len(words) > 13 and duration >= 2.4:
                midpoint = len(words) // 2
                chunks = [" ".join(words[:midpoint]), " ".join(words[midpoint:])]
            else:
                chunks = [text]
            chunk_duration = duration / len(chunks)
            for chunk_index, chunk in enumerate(chunks):
                start = cursor + chunk_index * chunk_duration
                end = cursor + (chunk_index + 1) * chunk_duration
                events.append((start, end, chunk))
        cursor += duration
    return events


def _write_ass_overlays(
    segments: list[dict],
    target: Path,
    *,
    captions: bool,
    headline: str,
) -> bool:
    events: list[str] = []
    if captions:
        for start, end, text in _caption_events(segments):
            rendered = _ass_escape_text(text, width=34)
            if rendered:
                events.append(
                    f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{rendered}"
                )

    total_duration = _timeline_duration(segments)
    headline_text = _ass_escape_text(headline, width=28)
    if headline_text and total_duration > 0:
        events.append(
            f"Dialogue: 1,0:00:00.00,{_ass_time(min(3.0, total_duration))},Headline,,0,0,0,,{headline_text}"
        )

    if not events:
        return False

    ass = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        "Style: Caption,Arial,58,&H00FFFFFF,&H000000FF,&H00101010,&H64000000,-1,0,0,0,100,100,0,0,1,3,1,2,80,80,110,1",
        "Style: Headline,Arial,76,&H00FFFFFF,&H000000FF,&H00101010,&H64000000,-1,0,0,0,100,100,0,0,1,5,1,8,80,80,140,1",
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        *events,
        "",
    ])
    target.write_text(ass, encoding="utf-8")
    return True


@lru_cache(maxsize=1)
def _ffmpeg_filters() -> frozenset[str]:
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-filters"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except Exception:
        return frozenset()
    filters: set[str] = set()
    for line in proc.stdout.splitlines():
        # Typical line: " TSC overlay           VV->V      Overlay ..."
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[.A-Z|]{3}", parts[0]):
            filters.add(parts[1])
    return frozenset(filters)


def _load_font(size: int, *, bold: bool = True):
    try:
        from PIL import ImageFont
    except ImportError:
        return None

    candidates = []
    if bold:
        candidates.extend([
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Helvetica.ttc",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ])
    candidates.extend([
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ])
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    for family in ("Arial.ttf", "Helvetica.ttc", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(family, size=size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _wrap_pixels(draw, text: str, font, max_width: int) -> str:
    words = _clean_caption(text).split()
    if not words:
        return ""
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = current + " " + word
        bbox = draw.textbbox((0, 0), trial, font=font, stroke_width=0)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return "\n".join(lines)


def _render_text_png(
    target: Path,
    text: str,
    *,
    kind: str,
) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    font_size = 76 if kind == "headline" else 58
    font = _load_font(font_size, bold=True)
    if font is None:
        return False

    image = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    wrapped = _wrap_pixels(draw, text, font, 900 if kind == "headline" else 930)
    if not wrapped:
        return False
    spacing = 12 if kind == "headline" else 10
    stroke = 5 if kind == "headline" else 4
    bbox = draw.multiline_textbbox(
        (0, 0), wrapped, font=font, spacing=spacing, align="center", stroke_width=stroke
    )
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = int((1080 - width) / 2)
    if kind == "headline":
        y = 165
    else:
        y = max(1100, 1920 - height - 190)

    # Subtle translucent backing plate improves readability without requiring drawtext/libass.
    pad_x = 30
    pad_y = 20
    draw.rounded_rectangle(
        (x - pad_x, y - pad_y, x + width + pad_x, y + height + pad_y),
        radius=22,
        fill=(0, 0, 0, 125),
    )
    draw.multiline_text(
        (x, y),
        wrapped,
        font=font,
        fill=(255, 255, 255, 255),
        spacing=spacing,
        align="center",
        stroke_width=stroke,
        stroke_fill=(16, 16, 16, 235),
    )
    image.save(target, format="PNG", optimize=True)
    return True


def _bitmap_overlay_events(
    segments: list[dict],
    *,
    captions: bool,
    headline: str,
) -> list[tuple[float, float, str, str]]:
    events: list[tuple[float, float, str, str]] = []
    if captions:
        for start, end, text in _caption_events(segments):
            cleaned = _clean_caption(text)
            if cleaned:
                events.append((start, end, cleaned, "caption"))
    total_duration = _timeline_duration(segments)
    headline_text = _clean_caption(headline)
    if headline_text and total_duration > 0:
        # Render headline last so it sits above a simultaneous opening caption.
        events.append((0.0, min(3.0, total_duration), headline_text, "headline"))
    return events


def _render_bitmap_overlays(
    joined: Path,
    output: Path,
    temp_dir: Path,
    segments: list[dict],
    *,
    captions: bool,
    headline: str,
) -> bool:
    events = _bitmap_overlay_events(segments, captions=captions, headline=headline)
    if not events:
        return False
    filters = _ffmpeg_filters()
    if "overlay" not in filters:
        return False

    png_events: list[tuple[Path, float, float]] = []
    for index, (start, end, text, kind) in enumerate(events, start=1):
        png = temp_dir / f"overlay_{index:03d}_{kind}.png"
        if _render_text_png(png, text, kind=kind):
            png_events.append((png, start, end))
    if not png_events:
        return False

    total_duration = max(
        0.05,
        _timeline_duration(segments),
    )
    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined)]
    for png, _start, _end in png_events:
        args += ["-loop", "1", "-framerate", "1", "-t", f"{total_duration:.3f}", "-i", str(png)]

    chains: list[str] = []
    current = "[0:v]"
    for input_index, (_png, start, end) in enumerate(png_events, start=1):
        out = f"[ov{input_index}]"
        chains.append(
            f"{current}[{input_index}:v]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'{out}"
        )
        current = out
    filter_complex = ";".join(chains)
    args += [
        "-filter_complex", filter_complex,
        "-map", current,
        "-map", "0:a?",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(output),
    ]
    _run(args)
    return True


def _apply_text_overlays(
    joined: Path,
    output: Path,
    temp_dir: Path,
    segments: list[dict],
    *,
    captions: bool,
    headline: str,
) -> None:
    if not captions and not _clean_caption(headline):
        output.write_bytes(joined.read_bytes())
        return

    filters = _ffmpeg_filters()
    force_bitmap = os.getenv("SCRAPER_FORCE_BITMAP_OVERLAYS", "").strip().lower() in {"1", "true", "yes"}

    if "ass" in filters and not force_bitmap:
        overlay = temp_dir / "overlays.ass"
        if _write_ass_overlays(segments, overlay, captions=captions, headline=headline):
            overlay_path = _escape_filter_path(overlay)
            try:
                _run([
                    FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined),
                    "-vf", f"ass=filename='{overlay_path}'",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", str(output),
                ])
                return
            except RuntimeError as exc:
                print(f"  text overlay: ASS unavailable at runtime ({exc}); trying bitmap fallback")

    try:
        if _render_bitmap_overlays(
            joined,
            output,
            temp_dir,
            segments,
            captions=captions,
            headline=headline,
        ):
            print("  text overlay: rendered with bitmap fallback")
            return
    except Exception as exc:
        print(f"  text overlay: bitmap fallback unavailable ({type(exc).__name__}: {exc})")

    # Text decoration should never prevent a valid ad from being rendered.
    print("  text overlay: skipped because this FFmpeg/Python build lacks a supported text overlay path")
    output.write_bytes(joined.read_bytes())


def splice_videos(
    segments: list[dict],
    output_path: str,
    *,
    captions: bool = False,
    headline: str = "",
    audio_normalize: bool = True,
    audio_fade_ms: int = 70,
):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    valid_segments = []
    for seg in segments:
        path = Path(str(seg.get("video_path") or ""))
        try:
            start = float(seg["start"])
            end = float(seg["end"])
        except Exception:
            continue
        if path.exists() and end > start:
            gap_before = max(0.0, float(seg.get("gap_before", 0.0) or 0.0))
            valid_segments.append({**seg, "video_path": str(path), "start": start, "end": end, "gap_before": gap_before})
    if not valid_segments:
        raise ValueError("No valid video segments found.")

    with tempfile.TemporaryDirectory(prefix="scraper-ad-", dir=str(output.parent)) as temp_name:
        temp_dir = Path(temp_name)
        clips: list[Path] = []
        for index, seg in enumerate(valid_segments, start=1):
            gap_before = max(0.0, float(seg.get("gap_before", 0.0) or 0.0))
            if gap_before >= (1 / 60):
                gap = temp_dir / f"gap_{index:03d}.mp4"
                _run([
                    FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30",
                    "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-t", f"{gap_before:.3f}",
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                    "-shortest", "-movflags", "+faststart", str(gap),
                ])
                print(f"  gap before clip {index}: {gap_before:.2f}s")
                clips.append(gap)
            source = Path(seg["video_path"])
            duration = max(0.05, seg["end"] - seg["start"])
            clip = temp_dir / f"clip_{index:03d}.mp4"
            video_filter = (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1,fps=30"
            )
            base = [
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(seg["start"]), "-t", str(duration), "-i", str(source),
            ]
            audio_mode = str(seg.get("audio_mode") or "original").lower()
            audio_source = Path(str(seg.get("audio_path") or "")) if seg.get("audio_path") else None
            use_external_audio = bool(audio_mode != "mute" and audio_source and audio_source.exists() and has_audio(audio_source))
            if use_external_audio:
                audio_start = float(seg.get("audio_start", 0.0) or 0.0)
                base += ["-ss", str(audio_start), "-t", str(duration), "-i", str(audio_source)]
            fade = min(max(0.0, audio_fade_ms / 1000.0), max(0.0, duration / 4))
            audio_filters = ["aresample=48000"]
            if audio_normalize:
                audio_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
            if fade >= 0.02:
                audio_filters.extend([
                    f"afade=t=in:st=0:d={fade:.3f}",
                    f"afade=t=out:st={max(0.0, duration-fade):.3f}:d={fade:.3f}",
                ])
            if audio_mode != "mute" and (use_external_audio or has_audio(source)):
                audio_map = "1:a:0" if use_external_audio else "0:a:0"
                args = base + [
                    "-map", "0:v:0", "-map", audio_map,
                    "-vf", video_filter, "-af", ",".join(audio_filters),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart", str(clip),
                ]
            else:
                args = base + [
                    "-f", "lavfi", "-t", str(duration), "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-map", "0:v:0", "-map", "1:a:0", "-vf", video_filter,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-shortest",
                    "-movflags", "+faststart", str(clip),
                ]
            print(
                f"  clip {index}/{len(valid_segments)}: {source.name} "
                f"{seg['start']:.2f}-{seg['end']:.2f}s ({seg.get('role', 'segment')})"
            )
            _run(args)
            clips.append(clip)

        concat_file = temp_dir / "concat.txt"
        concat_file.write_text("".join(f"file '{_escape_concat_path(path)}'\n" for path in clips), encoding="utf-8")
        joined = temp_dir / "joined.mp4"
        _run([
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", str(joined),
        ])

        _apply_text_overlays(
            joined,
            output,
            temp_dir,
            valid_segments,
            captions=bool(captions),
            headline=headline,
        )
    return str(output)
