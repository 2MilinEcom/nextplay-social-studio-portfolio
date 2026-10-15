from __future__ import annotations

import json
from pathlib import Path

from .models import Product, VideoCandidate
from .storage import atomic_write_json

ANALYSIS_SCHEMA_VERSION = 2


def _instructions() -> dict:
    return {
        "segment_roles": [
            "hook",
            "problem",
            "product_reveal",
            "demo",
            "benefit",
            "social_proof",
            "objection",
            "before_after",
            "cta",
        ],
        "segment_shape": {
            "start": "seconds (float)",
            "end": "seconds (float)",
            "role": "one of segment_roles",
            "score": "0..1 ad usefulness",
            "text": "spoken/on-screen text if useful",
            "reason": "why this cut works",
            "product_visible": "boolean",
            "product_match_score": "0..1 match to reference product",
        },
    }


def _new_payload(product: Product, candidate: VideoCandidate) -> dict:
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "status": "pending",
        "product": {
            "product_id": product.product_id,
            "title": product.title,
            "brand": product.brand,
            "description": product.description,
            "primary_image_path": product.primary_image_path,
        },
        "product_image_path": product.primary_image_path,
        "source": {
            "video_id": candidate.video_id,
            "tiktok_url": candidate.url,
            "creator": candidate.creator,
            "local_path": candidate.local_path,
        },
        "provider": None,
        "transcript": None,
        "summary": None,
        "video_relevance_score": None,
        "recommended_use": None,
        "segments": [],
        "instructions": _instructions(),
    }


def _upgrade_existing(payload: dict, product: Product, candidate: VideoCandidate) -> dict:
    payload = dict(payload)
    payload["schema_version"] = ANALYSIS_SCHEMA_VERSION
    product_payload = dict(payload.get("product") or {})
    product_payload.update(
        {
            "product_id": product.product_id,
            "title": product.title,
            "brand": product.brand,
            "description": product.description,
            "primary_image_path": product.primary_image_path,
        }
    )
    payload["product"] = product_payload
    payload["product_image_path"] = product.primary_image_path
    source = dict(payload.get("source") or {})
    source.update(
        {
            "video_id": candidate.video_id,
            "tiktok_url": candidate.url,
            "creator": candidate.creator,
            "local_path": candidate.local_path,
        }
    )
    payload["source"] = source
    payload.setdefault("status", "pending")
    payload.setdefault("provider", None)
    payload.setdefault("transcript", None)
    payload.setdefault("summary", None)
    payload.setdefault("video_relevance_score", None)
    payload.setdefault("recommended_use", None)
    payload.setdefault("segments", [])
    payload["instructions"] = _instructions()
    return payload


def prepare_analysis_jobs(product: Product, candidates: list[VideoCandidate], analysis_dir: Path) -> list[Path]:
    analysis_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[Path] = []
    for candidate in candidates:
        if candidate.download_status != "downloaded" or not candidate.local_path:
            continue
        path = analysis_dir / f"{candidate.video_id}.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                upgraded = _upgrade_existing(existing, product, candidate)
                if upgraded != existing:
                    atomic_write_json(path, upgraded)
            except Exception:
                atomic_write_json(path, _new_payload(product, candidate))
            jobs.append(path)
            continue
        atomic_write_json(path, _new_payload(product, candidate))
        jobs.append(path)
    return jobs
