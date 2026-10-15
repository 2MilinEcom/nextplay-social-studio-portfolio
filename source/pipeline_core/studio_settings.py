from __future__ import annotations

from typing import Any

from .storage import atomic_write_json, read_json

DEFAULT_SETTINGS: dict[str, Any] = {
    "target_duration": 20.0,
    "max_duration": 24.0,
    "captions_enabled": True,
    "headline_enabled": True,
    "headline_text": "",
    "audio_normalize": True,
    "audio_fade_ms": 70,
    "product_match_min": 0.45,
    "video_relevance_min": 0.45,
    "require_cleared_rights": False,
    "quality_threshold": 78,
    "ab_variants": 6,
    "download_limit": 30,
}


def settings_path(workspace):
    return workspace.root / "studio_settings.json"


def load_studio_settings(workspace) -> dict[str, Any]:
    data = read_json(settings_path(workspace), {}) or {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    return merged


def save_studio_settings(workspace, data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    # Sanitize the few numeric controls that directly drive FFmpeg/planning.
    merged["target_duration"] = max(8.0, min(60.0, float(merged["target_duration"])))
    merged["max_duration"] = max(merged["target_duration"], min(60.0, float(merged["max_duration"])))
    merged["audio_fade_ms"] = max(0, min(500, int(merged["audio_fade_ms"])))
    merged["product_match_min"] = max(0.0, min(1.0, float(merged["product_match_min"])))
    merged["video_relevance_min"] = max(0.0, min(1.0, float(merged["video_relevance_min"])))
    merged["quality_threshold"] = max(0, min(100, int(merged["quality_threshold"])))
    merged["ab_variants"] = max(2, min(20, int(merged["ab_variants"])))
    merged["download_limit"] = max(1, min(100, int(merged["download_limit"])))
    merged["headline_text"] = str(merged.get("headline_text") or "")[:140]
    for key in ("captions_enabled", "headline_enabled", "audio_normalize", "require_cleared_rights"):
        merged[key] = bool(merged[key])
    atomic_write_json(settings_path(workspace), merged)
    return merged


def update_studio_settings(workspace, patch: dict[str, Any]) -> dict[str, Any]:
    data = load_studio_settings(workspace)
    data.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS})
    return save_studio_settings(workspace, data)
