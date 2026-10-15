from __future__ import annotations

from typing import Any

from .storage import atomic_write_json, read_json

RIGHTS_STATUSES = (
    "unknown",
    "reference_only",
    "permission_requested",
    "licensed",
    "approved_for_ads",
)
CLEARED_STATUSES = {"licensed", "approved_for_ads"}


def rights_path(workspace):
    return workspace.root / "rights.json"


def load_rights_state(workspace) -> dict[str, Any]:
    data = read_json(rights_path(workspace), {}) or {}
    return {"sources": dict(data.get("sources") or {})}


def save_rights_state(workspace, data: dict[str, Any]) -> dict[str, Any]:
    clean = {"sources": dict(data.get("sources") or {})}
    atomic_write_json(rights_path(workspace), clean)
    return clean


def update_source_rights(workspace, video_id: str, *, status: str, note: str = "") -> dict[str, Any]:
    video_id = str(video_id or "").strip()
    if not video_id:
        raise ValueError("Video id is required.")
    if status not in RIGHTS_STATUSES:
        raise ValueError(f"Unknown rights status: {status}")
    data = load_rights_state(workspace)
    entry = {"status": status}
    note = str(note or "").strip()
    if note:
        entry["note"] = note[:2000]
    data["sources"][video_id] = entry
    save_rights_state(workspace, data)
    return entry


def rights_status(rights_state: dict | None, video_id: str) -> str:
    entry = dict(((rights_state or {}).get("sources") or {}).get(str(video_id)) or {})
    return str(entry.get("status") or "unknown")


def is_cleared(rights_state: dict | None, video_id: str) -> bool:
    return rights_status(rights_state, video_id) in CLEARED_STATUSES
