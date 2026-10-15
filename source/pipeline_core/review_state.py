from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import atomic_write_json, read_json


DEFAULT_REVIEW = {
    "segments": {},
    "ads": {},
}


def review_path(workspace) -> Path:
    return workspace.root / "review.json"


def load_review_state(workspace) -> dict[str, Any]:
    data = read_json(review_path(workspace), {}) or {}
    return {
        "segments": dict(data.get("segments") or {}),
        "ads": dict(data.get("ads") or {}),
    }


def save_review_state(workspace, data: dict[str, Any]) -> dict[str, Any]:
    clean = {
        "segments": dict(data.get("segments") or {}),
        "ads": dict(data.get("ads") or {}),
    }
    atomic_write_json(review_path(workspace), clean)
    return clean


def update_review_item(
    workspace,
    *,
    kind: str,
    item_id: str,
    status: str | None = None,
    favorite: bool | None = None,
    note: str | None = None,
    caption: str | None = None,
) -> dict[str, Any]:
    if kind not in {"segments", "ads"}:
        raise ValueError("Review kind must be 'segments' or 'ads'.")
    if not item_id:
        raise ValueError("Review item id is required.")
    if status is not None and status not in {"approved", "rejected", "unreviewed"}:
        raise ValueError("Status must be approved, rejected, or unreviewed.")

    data = load_review_state(workspace)
    entry = dict(data[kind].get(item_id) or {})
    if status is not None:
        if status == "unreviewed":
            entry.pop("status", None)
        else:
            entry["status"] = status
    if favorite is not None:
        if favorite:
            entry["favorite"] = True
        else:
            entry.pop("favorite", None)
    if note is not None:
        note = note.strip()
        if note:
            entry["note"] = note[:2000]
        else:
            entry.pop("note", None)
    if caption is not None:
        caption = caption.strip()
        if caption:
            entry["caption"] = caption[:500]
        else:
            entry.pop("caption", None)

    if entry:
        data[kind][item_id] = entry
    else:
        data[kind].pop(item_id, None)
    save_review_state(workspace, data)
    return entry
