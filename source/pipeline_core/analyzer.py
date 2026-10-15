from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .media import extract_audio, extract_frames, image_data_url, probe_duration, temp_media_dir
from .storage import atomic_write_json

SEGMENT_ROLES = [
    "hook",
    "problem",
    "product_reveal",
    "demo",
    "benefit",
    "social_proof",
    "objection",
    "before_after",
    "cta",
]

def resolve_timestamp_transcription_model(requested_model: str | None) -> str:
    """Return a file-transcription model that can emit segment timestamps.

    The editing pipeline depends on timestamp_granularities=["segment"].
    OpenAI currently documents that option for whisper-1. This resolver also
    makes old V3 .env files self-healing instead of repeating a 400 error.
    """
    requested = (requested_model or "").strip()
    if requested == "whisper-1":
        return requested
    return "whisper-1"


ANALYSIS_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "video_relevance_score": {"type": "number", "minimum": 0, "maximum": 1},
        "recommended_use": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "role": {"type": "string", "enum": SEGMENT_ROLES},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "text": {"type": "string"},
                    "reason": {"type": "string"},
                    "product_visible": {"type": "boolean"},
                    "product_match_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "start",
                    "end",
                    "role",
                    "score",
                    "text",
                    "reason",
                    "product_visible",
                    "product_match_score",
                ],
            },
        },
    },
    "required": ["summary", "video_relevance_score", "recommended_use", "segments"],
}


def _timestamped_transcript(transcript_payload: dict[str, Any]) -> str:
    segments = transcript_payload.get("segments") or []
    lines: list[str] = []
    for seg in segments:
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except Exception:
            continue
        text = str(seg.get("text") or "").strip()
        if text:
            lines.append(f"[{start:.2f}-{end:.2f}] {text}")
    if lines:
        return "\n".join(lines)
    return str(transcript_payload.get("text") or "").strip()


def _sanitize_segments(segments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for raw in segments:
        try:
            start = max(0.0, float(raw["start"]))
            end = min(duration, float(raw["end"]))
            score = min(1.0, max(0.0, float(raw.get("score", 0.5))))
            product_match = min(1.0, max(0.0, float(raw.get("product_match_score", 0.5))))
        except Exception:
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in SEGMENT_ROLES or end <= start:
            continue
        # Tiny cuts are hard to use, and excessively long cuts make remix ads drag.
        if end - start < 0.45:
            continue
        if end - start > 8.0:
            end = start + 8.0
        cleaned.append(
            {
                "start": round(start, 3),
                "end": round(min(end, duration), 3),
                "role": role,
                "score": round(score, 4),
                "text": str(raw.get("text") or "").strip(),
                "reason": str(raw.get("reason") or "").strip(),
                "product_visible": bool(raw.get("product_visible", False)),
                "product_match_score": round(product_match, 4),
            }
        )
    cleaned.sort(key=lambda item: (item["start"], item["end"], item["role"]))
    return cleaned


class OpenAIAnalyzer:
    name = "openai"

    def __init__(
        self,
        *,
        analysis_model: str = "gpt-5.6-luna",
        transcription_model: str = "whisper-1",
        frame_count: int = 8,
    ):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The OpenAI analyzer requires the optional analysis dependencies. "
                "Run: python3 -m pip install -r requirements-analysis.txt"
            ) from exc
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Put it in scraper/.env or export it in your shell before analysis."
            )
        self.client = OpenAI()
        self.analysis_model = analysis_model
        self.requested_transcription_model = transcription_model
        # The ad editor needs segment timestamps. OpenAI's current file-transcription
        # API only exposes timestamp_granularities with whisper-1/verbose_json.
        # Older V3 .env files used gpt-4o-mini-transcribe, which only accepts JSON.
        self.transcription_model = resolve_timestamp_transcription_model(transcription_model)
        self.frame_count = frame_count
        if self.transcription_model != self.requested_transcription_model:
            print(
                "Timestamped editing requires segment timestamps; "
                f"using {self.transcription_model} instead of "
                f"{self.requested_transcription_model}."
            )

    def _transcribe(self, audio_path: Path) -> dict[str, Any]:
        with audio_path.open("rb") as fh:
            transcript = self.client.audio.transcriptions.create(
                model=self.transcription_model,
                file=fh,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        if hasattr(transcript, "model_dump"):
            return transcript.model_dump()
        if isinstance(transcript, dict):
            return transcript
        return {"text": str(transcript), "segments": []}

    def analyze_job(self, job: dict[str, Any]) -> dict[str, Any]:
        source = job.get("source") or {}
        product = job.get("product") or {}
        video_path = Path(str(source.get("local_path") or ""))
        if not video_path.exists():
            raise FileNotFoundError(f"Downloaded video not found: {video_path}")

        duration = probe_duration(video_path)
        with temp_media_dir() as temp_name:
            temp_dir = Path(temp_name)
            audio_path = extract_audio(video_path, temp_dir / "audio.mp3")
            transcript = self._transcribe(audio_path)
            frame_paths = extract_frames(video_path, temp_dir / "frames", count=self.frame_count)

            prompt = (
                "You are selecting source-video moments for short-form direct-response product ads. "
                "Analyze only what is supported by the transcript and supplied video frames. "
                "Do not invent product claims. Choose concise, reusable cuts with accurate timestamps. "
                "A segment may be visually strong even when it has little speech. Prefer segments that clearly show "
                "the referenced product or a closely matching product. Keep segments roughly 0.5-8 seconds. "
                "Return useful segments across roles when present; do not force roles that are absent.\n\n"
                f"PRODUCT TITLE: {product.get('title', '')}\n"
                f"BRAND: {product.get('brand', '')}\n"
                f"PRODUCT DESCRIPTION: {product.get('description', '')}\n"
                f"VIDEO DURATION: {duration:.3f} seconds\n"
                f"SOURCE CREATOR: {source.get('creator', '')}\n"
                f"SOURCE URL: {source.get('tiktok_url', '')}\n\n"
                "TIMESTAMPED TRANSCRIPT:\n"
                f"{_timestamped_transcript(transcript) or '(no usable speech transcript)'}\n\n"
                "Score each cut for ad usefulness. product_match_score means how confidently the visible product matches "
                "the reference product/context, not merely the general product category."
            )

            content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
            product_image = product.get("primary_image_path") or job.get("product_image_path")
            if product_image and Path(str(product_image)).exists():
                content.append({"type": "input_text", "text": "REFERENCE PRODUCT IMAGE:"})
                content.append({"type": "input_image", "image_url": image_data_url(product_image), "detail": "high"})
            for timestamp, frame_path in frame_paths:
                content.append({"type": "input_text", "text": f"VIDEO FRAME at {timestamp:.3f}s:"})
                content.append({"type": "input_image", "image_url": image_data_url(frame_path), "detail": "low"})

            response = self.client.responses.create(
                model=self.analysis_model,
                input=[{"role": "user", "content": content}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "ugc_video_analysis",
                        "strict": True,
                        "schema": ANALYSIS_OUTPUT_SCHEMA,
                    }
                },
            )
            parsed = json.loads(response.output_text)

        result = dict(job)
        result["status"] = "complete"
        result["provider"] = {
            "name": self.name,
            "analysis_model": self.analysis_model,
            "transcription_model": self.transcription_model,
            "requested_transcription_model": self.requested_transcription_model,
        }
        result["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(duration, 3)
        result["transcript"] = transcript
        result["summary"] = str(parsed.get("summary") or "")
        result["video_relevance_score"] = float(parsed.get("video_relevance_score", 0.0))
        result["recommended_use"] = str(parsed.get("recommended_use") or "")
        result["segments"] = _sanitize_segments(parsed.get("segments") or [], duration)
        result.pop("error", None)
        return result


class MockAnalyzer:
    """Deterministic local analyzer for smoke tests. It never calls an external service."""

    name = "mock"

    def analyze_job(self, job: dict[str, Any]) -> dict[str, Any]:
        source = job.get("source") or {}
        video_path = Path(str(source.get("local_path") or ""))
        duration = probe_duration(video_path)
        cut = max(0.55, min(1.4, duration / 3.0))
        roles = ["hook", "demo", "cta"]
        segments = []
        cursor = 0.0
        for index, role in enumerate(roles):
            start = min(cursor, max(duration - 0.5, 0.0))
            end = min(duration, start + cut)
            if end - start >= 0.45:
                segments.append(
                    {
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "role": role,
                        "score": round(0.9 - index * 0.05, 3),
                        "text": "",
                        "reason": "mock smoke-test segment",
                        "product_visible": True,
                        "product_match_score": 0.9,
                    }
                )
            cursor = end
        result = dict(job)
        result.update(
            {
                "status": "complete",
                "provider": {"name": self.name},
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(duration, 3),
                "transcript": {"text": "", "segments": []},
                "summary": "Mock analysis for local pipeline validation.",
                "video_relevance_score": 0.9,
                "recommended_use": "smoke_test",
                "segments": segments,
            }
        )
        return result


def analyze_jobs(
    analysis_dir: str | Path,
    *,
    provider: str = "openai",
    limit: int | None = None,
    force: bool = False,
    analysis_model: str = "gpt-5.6-luna",
    transcription_model: str = "whisper-1",
    frame_count: int = 8,
) -> list[Path]:
    analysis_dir = Path(analysis_dir)
    job_paths = sorted(analysis_dir.glob("*.json"))
    if limit is not None:
        job_paths = job_paths[: max(0, int(limit))]

    if provider == "openai":
        analyzer = OpenAIAnalyzer(
            analysis_model=analysis_model,
            transcription_model=transcription_model,
            frame_count=frame_count,
        )
    elif provider == "mock":
        analyzer = MockAnalyzer()
    else:
        raise ValueError(f"Unknown analyzer provider: {provider}")

    completed: list[Path] = []
    total = len(job_paths)
    for index, path in enumerate(job_paths, start=1):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[{index}/{total}] skipping unreadable job {path.name}: {exc}")
            continue
        if job.get("status") == "complete" and not force:
            print(f"[{index}/{total}] already complete: {path.name}")
            completed.append(path)
            continue
        print(f"[{index}/{total}] analyzing {path.name} with {provider}...")
        try:
            result = analyzer.analyze_job(job)
            atomic_write_json(path, result)
            completed.append(path)
            print(f"  -> {len(result.get('segments') or [])} usable segment(s)")
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            atomic_write_json(path, job)
            print(f"  failed: {job['error']}")
    return completed
