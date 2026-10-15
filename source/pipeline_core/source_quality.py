from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ffmpeg_bin import FFMPEG

from .storage import atomic_write_json, read_json
from .visual import fetch_thumbnail


def quality_path(workspace) -> Path:
    return workspace.root / "source_quality.json"


def load_source_quality(workspace) -> dict[str, Any]:
    data = read_json(quality_path(workspace), {}) or {}
    return {"sources": dict(data.get("sources") or {})}


def _dhash(path: Path) -> int | None:
    proc = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-vf", "scale=9:8,format=gray", "-frames:v", "1", "-f", "rawvideo", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    pixels = proc.stdout
    if proc.returncode != 0 or len(pixels) < 72:
        return None
    bits = 0
    for y in range(8):
        row = pixels[y * 9:(y + 1) * 9]
        for x in range(8):
            bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def evaluate_candidate_thumbnails(workspace, candidates, *, limit: int = 120, duplicate_distance: int = 5) -> dict[str, Any]:
    """Fetch candidate thumbnails and mark obvious near-duplicates.

    This is intentionally a conservative pre-download filter. It does not claim
    product identity; the later video analyzer is the stronger product check.
    """
    ordered = sorted(candidates, key=lambda c: -float(c.combined_score or 0.0))[: max(1, int(limit))]
    sources: dict[str, Any] = {}
    accepted_hashes: list[tuple[str, int]] = []
    for candidate in ordered:
        thumb = workspace.thumbnails_dir / f"{candidate.video_id}.jpg"
        if not thumb.exists():
            fetch_thumbnail(candidate, thumb)
        entry = {
            "thumbnail_path": str(thumb) if thumb.exists() else None,
            "duplicate_of": None,
            "duplicate_distance": None,
        }
        if thumb.exists():
            digest = _dhash(thumb)
            if digest is not None:
                for other_id, other_hash in accepted_hashes:
                    distance = _hamming(digest, other_hash)
                    if distance <= duplicate_distance:
                        entry["duplicate_of"] = other_id
                        entry["duplicate_distance"] = distance
                        break
                if not entry["duplicate_of"]:
                    accepted_hashes.append((candidate.video_id, digest))
        sources[candidate.video_id] = entry
    payload = {"sources": sources}
    atomic_write_json(quality_path(workspace), payload)
    return payload


def update_from_analyses(workspace) -> dict[str, Any]:
    data = load_source_quality(workspace)
    sources = data["sources"]
    for path in workspace.analysis_dir.glob("*.json"):
        try:
            import json
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") != "complete":
            continue
        source = payload.get("source") or {}
        video_id = str(source.get("video_id") or path.stem)
        segments = payload.get("segments") or []
        product_scores = [float(s.get("product_match_score", 0.0) or 0.0) for s in segments]
        visible = [s for s in segments if s.get("product_visible")]
        entry = dict(sources.get(video_id) or {})
        entry.update({
            "video_relevance_score": float(payload.get("video_relevance_score", 0.0) or 0.0),
            "max_product_match_score": max(product_scores) if product_scores else 0.0,
            "product_visible_segments": len(visible),
            "analysis_verified": True,
        })
        sources[video_id] = entry
    atomic_write_json(quality_path(workspace), {"sources": sources})
    return {"sources": sources}
