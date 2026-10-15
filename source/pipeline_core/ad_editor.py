from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ad_builder import ROLE_CAPS, load_segment_pool, render_ad_plans
from .media import probe_duration
from .review_state import load_review_state
from .rights import load_rights_state, rights_status
from .source_quality import load_source_quality
from .storage import atomic_write_json, read_json
from .studio_settings import load_studio_settings

MAX_EDITOR_CLIPS = 20
MAX_EDITOR_DURATION = 90.0
MIN_CLIP_DURATION = 0.25


def load_ad_plans(workspace) -> list[dict]:
    return read_json(workspace.ads_dir / "ad_plans.json", []) or []


def find_ad_plan(workspace, name: str) -> dict:
    for plan in load_ad_plans(workspace):
        if str(plan.get("name") or "") == str(name):
            return deepcopy(plan)
    raise ValueError(f"Ad plan not found: {name}")


def _analysis_index(workspace) -> tuple[dict[str, dict], dict[str, dict]]:
    videos: dict[str, dict] = {}
    segments: dict[str, dict] = {}
    rights_state = load_rights_state(workspace)
    for path in sorted(workspace.analysis_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = data.get("source") or {}
        local_path = str(source.get("local_path") or "")
        if not local_path:
            continue
        video_id = str(source.get("video_id") or Path(local_path).stem)
        videos[video_id] = {
            "video_id": video_id,
            "video_path": local_path,
            "creator": source.get("creator") or "",
            "tiktok_url": source.get("tiktok_url") or "",
            "duration_seconds": float(data.get("duration_seconds") or 0.0),
            "video_relevance_score": float(data.get("video_relevance_score") or 0.0),
        }
        for index, segment in enumerate(data.get("segments") or []):
            seg_id = f"{video_id}:{index}"
            item = dict(segment)
            item.update(videos[video_id])
            item["segment_id"] = seg_id
            item["rights_status"] = rights_status(rights_state, video_id)
            segments[seg_id] = item
    return videos, segments


def _candidate_index(workspace) -> dict[str, dict]:
    out = {}
    for candidate in workspace.load_candidates():
        out[str(candidate.video_id)] = {
            "video_id": str(candidate.video_id),
            "video_path": str(candidate.local_path or ""),
            "creator": str(candidate.creator or ""),
            "tiktok_url": str(candidate.url or ""),
            "caption": str(candidate.caption or ""),
        }
    return out


def _duration(path: str, hinted: float = 0.0) -> float:
    if hinted and hinted > 0:
        return float(hinted)
    try:
        return float(probe_duration(path))
    except Exception:
        return 0.0


def _resolve_video(workspace, video_id: str, videos: dict[str, dict], candidates: dict[str, dict]) -> dict:
    item = dict(videos.get(str(video_id)) or candidates.get(str(video_id)) or {})
    path = Path(str(item.get("video_path") or "")).expanduser()
    if not path.is_file():
        raise ValueError(f"Source video is not available locally: {video_id}")
    try:
        path.resolve().relative_to(workspace.root.resolve())
    except ValueError:
        raise ValueError("Source media must be inside the product workspace") from None
    item["video_path"] = str(path.resolve())
    item["duration_seconds"] = _duration(item["video_path"], float(item.get("duration_seconds") or 0.0))
    item["rights_status"] = rights_status(load_rights_state(workspace), str(video_id))
    return item


def _clamp_range(start: float, end: float, source_duration: float) -> tuple[float, float]:
    start = max(0.0, float(start))
    end = float(end)
    if source_duration > 0:
        start = min(start, max(0.0, source_duration - MIN_CLIP_DURATION))
        end = min(end, source_duration)
    if end <= start:
        end = start + MIN_CLIP_DURATION
        if source_duration > 0:
            end = min(end, source_duration)
            start = max(0.0, min(start, end - MIN_CLIP_DURATION))
    return round(start, 3), round(end, 3)


def sanitize_editor_segments(workspace, payload_segments: list[dict]) -> list[dict]:
    if not isinstance(payload_segments, list) or not payload_segments:
        raise ValueError("The edited ad needs at least one clip.")
    if len(payload_segments) > MAX_EDITOR_CLIPS:
        raise ValueError(f"An edited ad can contain at most {MAX_EDITOR_CLIPS} clips.")

    videos, analyzed_segments = _analysis_index(workspace)
    candidates = _candidate_index(workspace)
    clean: list[dict] = []
    total = 0.0

    for raw in payload_segments:
        raw = dict(raw or {})
        segment_id = str(raw.get("segment_id") or "")
        base = dict(analyzed_segments.get(segment_id) or {})
        video_id = str(raw.get("video_id") or base.get("video_id") or "")
        if not video_id:
            raise ValueError("Every editor clip must reference a known source video.")
        source = _resolve_video(workspace, video_id, videos, candidates)

        default_start = float(base.get("start") or 0.0)
        default_end = float(base.get("end") or min(source.get("duration_seconds") or 4.0, 4.0))
        start, end = _clamp_range(
            float(raw.get("start", default_start) or 0.0),
            float(raw.get("end", default_end) or default_end),
            float(source.get("duration_seconds") or 0.0),
        )
        if end - start < MIN_CLIP_DURATION - 1e-6:
            raise ValueError(f"Clip {video_id} is too short after trimming.")

        role = str(raw.get("role") or base.get("role") or "custom").lower().strip()
        if not re.fullmatch(r"[a-z0-9_\-]{1,40}", role):
            role = "custom"
        gap_before = max(0.0, min(float(raw.get("gap_before", 0.0) or 0.0), MAX_EDITOR_DURATION))
        item = {
            "video_path": source["video_path"],
            "video_id": video_id,
            "creator": str(source.get("creator") or base.get("creator") or ""),
            "tiktok_url": str(source.get("tiktok_url") or base.get("tiktok_url") or ""),
            "start": start,
            "end": end,
            "role": role,
            "segment_id": segment_id,
            "score": float(raw.get("score", base.get("score", 0.0)) or 0.0),
            "raw_score": float(raw.get("raw_score", base.get("score", 0.0)) or 0.0),
            "product_match_score": float(raw.get("product_match_score", base.get("product_match_score", 0.0)) or 0.0),
            "product_visible": bool(raw.get("product_visible", base.get("product_visible", False))),
            "text": str(raw.get("text") if "text" in raw else base.get("text") or ""),
            "reason": str(raw.get("reason") or base.get("reason") or ""),
            "rights_status": source.get("rights_status", "unknown"),
            "source_duration_seconds": round(float(source.get("duration_seconds") or 0.0), 3),
            "gap_before": round(gap_before, 3),
            "audio_mode": str(raw.get("audio_mode") or "original") if str(raw.get("audio_mode") or "original") in {"original", "mute", "external"} else "original",
        }

        audio_segment_id = str(raw.get("audio_segment_id") or "")
        if audio_segment_id and item["audio_mode"] != "mute":
            item["audio_mode"] = "external"
            audio_base = analyzed_segments.get(audio_segment_id)
            if not audio_base:
                raise ValueError(f"Voiceover source could not be resolved: {audio_segment_id}")
            audio_video_id = str(audio_base.get("video_id") or "")
            audio_source = _resolve_video(workspace, audio_video_id, videos, candidates)
            audio_start = float(raw.get("audio_start", audio_base.get("start", 0.0)) or 0.0)
            audio_end = float(raw.get("audio_end", audio_start + (end - start)) or (audio_start + end - start))
            audio_start, audio_end = _clamp_range(audio_start, audio_end, float(audio_source.get("duration_seconds") or 0.0))
            desired = end - start
            available = audio_end - audio_start
            if available < desired:
                item["end"] = round(item["start"] + available, 3)
            item.update({
                "audio_path": audio_source["video_path"],
                "audio_start": audio_start,
                "audio_end": round(audio_start + (item["end"] - item["start"]), 3),
                "audio_segment_id": audio_segment_id,
                "audio_creator": str(audio_source.get("creator") or audio_base.get("creator") or ""),
                "audio_tiktok_url": str(audio_source.get("tiktok_url") or audio_base.get("tiktok_url") or ""),
            })

        total += item["gap_before"] + (item["end"] - item["start"])
        if total > MAX_EDITOR_DURATION:
            raise ValueError(f"Edited ad exceeds the {int(MAX_EDITOR_DURATION)} second safety limit.")
        clean.append(item)

    return clean


def _next_version_name(workspace, parent_name: str, kind: str) -> str:
    plans = load_ad_plans(workspace)
    safe_parent = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(parent_name)).strip("_") or "ad"
    safe_kind = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(kind)).strip("_") or "edit"
    prefix = f"{safe_parent}_{safe_kind}_v"
    existing = {str(p.get("name") or "") for p in plans}
    version = 1
    while f"{prefix}{version:02d}" in existing or (workspace.ads_dir / f"{prefix}{version:02d}.mp4").exists():
        version += 1
    return f"{prefix}{version:02d}"


def _append_after_render(workspace, plan: dict) -> None:
    path = workspace.ads_dir / "ad_plans.json"
    plans = load_ad_plans(workspace)
    plans.append(plan)
    atomic_write_json(path, plans)


def render_editor_version(
    workspace,
    parent_name: str,
    payload_segments: list[dict],
    *,
    render_patch: dict | None = None,
    label: str = "",
    strategy: str = "manual_edit",
    version_kind: str = "edit",
    metadata: dict | None = None,
) -> dict:
    parent = find_ad_plan(workspace, parent_name)
    segments = sanitize_editor_segments(workspace, payload_segments)
    name = _next_version_name(workspace, parent_name, version_kind)
    render = dict(parent.get("render") or load_studio_settings(workspace))
    for key, value in dict(render_patch or {}).items():
        if key in {"captions_enabled", "headline_enabled", "headline_text", "audio_normalize", "audio_fade_ms"}:
            render[key] = value
    total = sum(float(s.get("gap_before", 0.0) or 0.0) + float(s["end"]) - float(s["start"]) for s in segments)
    root_ad = str(parent.get("root_ad") or _root_ad_name(workspace, parent_name) or parent_name)
    plan = {
        "name": name,
        "template": parent.get("template") or "editor",
        "strategy": strategy,
        "parent_ad": parent_name,
        "version_parent": parent_name,
        "root_ad": root_ad,
        "edited_from": parent_name,
        "edit_label": str(label or ""),
        "duration_seconds": round(total, 3),
        "target_duration_seconds": parent.get("target_duration_seconds", load_studio_settings(workspace)["target_duration"]),
        "segments": segments,
        "output_path": str(workspace.ads_dir / f"{name}.mp4"),
        "render": render,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rights_notice": "Clear source creator usage rights before commercial publication.",
    }
    if metadata:
        plan.update(deepcopy(metadata))
    render_ad_plans([plan])
    _append_after_render(workspace, plan)
    return plan


def _pool_for_workspace(workspace) -> dict[str, list[dict]]:
    settings = load_studio_settings(workspace)
    return load_segment_pool(
        workspace.analysis_dir,
        review_state=load_review_state(workspace),
        rights_state=load_rights_state(workspace),
        source_quality=load_source_quality(workspace),
        product_match_min=float(settings.get("product_match_min", 0.0) or 0.0),
        video_relevance_min=float(settings.get("video_relevance_min", 0.0) or 0.0),
        require_cleared_rights=bool(settings.get("require_cleared_rights", False)),
    )


def _pool_item_to_segment(item: dict, max_duration: float | None = None) -> dict:
    start = float(item.get("start") or 0.0)
    end = float(item.get("end") or start + 4.0)
    cap = float(max_duration or ROLE_CAPS.get(str(item.get("role") or ""), 6.0))
    end = min(end, start + cap)
    return {
        "video_path": item["video_path"],
        "video_id": item["video_id"],
        "creator": item.get("creator", ""),
        "tiktok_url": item.get("tiktok_url", ""),
        "start": round(start, 3),
        "end": round(end, 3),
        "role": item.get("role") or "custom",
        "segment_id": item.get("segment_id", ""),
        "score": float(item.get("rank_score", item.get("score", 0.0)) or 0.0),
        "raw_score": float(item.get("score", 0.0) or 0.0),
        "product_match_score": float(item.get("product_match_score", 0.0) or 0.0),
        "product_visible": bool(item.get("product_visible", False)),
        "text": str(item.get("text") or ""),
        "reason": str(item.get("reason") or ""),
        "rights_status": item.get("rights_status", "unknown"),
    }


def _fallback_iteration(parent: dict, pool: dict[str, list[dict]], instruction: str, mode: str) -> tuple[list[dict], str]:
    instruction_l = str(instruction or "").lower()
    current = [deepcopy(s) for s in parent.get("segments") or []]
    if mode == "regenerate":
        order = ["hook", "product_reveal", "demo", "benefit", "cta"]
        fresh: list[dict] = []
        used: set[str] = set()
        for role in order:
            options = pool.get(role) or ([] if role != "product_reveal" else pool.get("demo") or [])
            candidate = next((x for x in options if str(x.get("segment_id") or "") not in used), None)
            if candidate:
                fresh.append(_pool_item_to_segment(candidate))
                used.add(str(candidate.get("segment_id") or ""))
        return (fresh or current), "Created a fresh role-based remix from the strongest available reviewed clips."

    role_words = ["hook", "demo", "benefit", "cta", "problem", "product_reveal", "social_proof", "objection", "before_after"]
    requested = next((role for role in role_words if role.replace("_", " ") in instruction_l or role in instruction_l), None)
    if requested:
        target_index = next((i for i, s in enumerate(current) if str(s.get("role") or "") == requested), None)
    else:
        target_index = min(range(len(current)), key=lambda i: float(current[i].get("score", 0.0))) if current else None
    if target_index is not None:
        role = str(current[target_index].get("role") or requested or "demo")
        current_ids = {str(s.get("segment_id") or "") for s in current}
        options = pool.get(role) or (pool.get("demo") if role == "product_reveal" else []) or []
        replacement = next((x for x in options if str(x.get("segment_id") or "") not in current_ids), None)
        if replacement:
            current[target_index] = _pool_item_to_segment(replacement)
            return current, f"Replaced the {role.replace('_', ' ')} with a stronger alternative while preserving the rest of the ad."
    return current, "Preserved the current structure because no safer higher-ranked replacement was available."


def _ai_choose_sequence(product, parent: dict, pool: dict[str, list[dict]], instruction: str, mode: str, model: str) -> tuple[list[dict], str]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI Python package is not installed.") from exc
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    option_map: dict[str, dict] = {}
    option_lines: list[str] = []
    for index, seg in enumerate(parent.get("segments") or []):
        key = f"current:{index}"
        option_map[key] = deepcopy(seg)
        option_lines.append(
            f"{key} | CURRENT | {seg.get('role')} | @{seg.get('creator','')} | {float(seg.get('end',0))-float(seg.get('start',0)):.1f}s | "
            f"score={float(seg.get('score',0)):.2f} | product={float(seg.get('product_match_score',0)):.2f} | {str(seg.get('text') or '')[:150]}"
        )

    for role, items in pool.items():
        for item in items[:6]:
            key = str(item.get("segment_id") or "")
            if not key or key in option_map:
                continue
            option_map[key] = _pool_item_to_segment(item)
            option_lines.append(
                f"{key} | ALTERNATIVE | {role} | @{item.get('creator','')} | {float(item.get('end',0))-float(item.get('start',0)):.1f}s | "
                f"rank={float(item.get('rank_score',0)):.2f} | product={float(item.get('product_match_score',0)):.2f} | {str(item.get('text') or '')[:150]}"
            )
            if len(option_map) >= 56:
                break
        if len(option_map) >= 56:
            break

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "ordered_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 8},
        },
        "required": ["summary", "ordered_ids"],
        "additionalProperties": False,
    }
    mode_rule = (
        "Make a fresh version that can replace multiple clips while keeping a coherent direct-response story."
        if mode == "regenerate"
        else "Make a conservative iteration: preserve most of the current ad and change only what clearly helps the instruction."
    )
    prompt = (
        "You are editing one short-form UGC ad. Choose only from the clip IDs below; never invent a clip or claim. "
        "Return the desired ordered clip IDs. Favor a strong first 3 seconds, clear product/demo footage, coherent speech, and a concise CTA. "
        "Avoid repeating the same idea or creator unnecessarily. Keep total duration roughly 15-30 seconds unless the user's instruction says otherwise. "
        f"{mode_rule}\n\nPRODUCT: {product.title}\nBRAND: {product.brand}\n"
        f"USER EDIT DIRECTION: {instruction or 'Make another stronger iteration of this ad.'}\n"
        f"CURRENT AD: {parent.get('name')} ({parent.get('duration_seconds')}s)\n\nAVAILABLE CLIPS:\n" + "\n".join(option_lines)
    )
    client = OpenAI()
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        text={"format": {"type": "json_schema", "name": "ad_iteration_plan", "strict": True, "schema": schema}},
    )
    data = json.loads(response.output_text)
    chosen: list[dict] = []
    seen = set()
    for key in data.get("ordered_ids") or []:
        if key in seen or key not in option_map:
            continue
        chosen.append(deepcopy(option_map[key]))
        seen.add(key)
    if len(chosen) < 2:
        raise RuntimeError("AI did not return enough valid source clips.")
    return chosen, str(data.get("summary") or "AI-created ad iteration.")



def _root_ad_name(workspace, ad_name: str) -> str:
    plans = {str(p.get("name") or ""): p for p in load_ad_plans(workspace)}
    current = str(ad_name or "")
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        plan = plans.get(current) or {}
        explicit = str(plan.get("root_ad") or "")
        if explicit:
            return explicit
        parent = str(plan.get("version_parent") or plan.get("parent_ad") or plan.get("edited_from") or "")
        if not parent or parent not in plans:
            return current
        current = parent
    return str(ad_name or "")


def restore_ad_version(workspace, current_name: str, restore_name: str, *, label: str = "") -> dict:
    """Clone a previous rendered plan into a new non-destructive version."""
    current = find_ad_plan(workspace, current_name)
    target = find_ad_plan(workspace, restore_name)
    if not target.get("segments"):
        raise ValueError("The selected previous version has no editable source cuts.")
    root = str(current.get("root_ad") or _root_ad_name(workspace, current_name) or current_name)
    return render_editor_version(
        workspace,
        current_name,
        deepcopy(target.get("segments") or []),
        render_patch=dict(target.get("render") or {}),
        label=label or f"Restored from {restore_name}",
        strategy="manual_restore",
        version_kind="restore",
        metadata={
            "root_ad": root,
            "restored_from": restore_name,
            "restore_source_strategy": target.get("strategy") or target.get("template") or "",
        },
    )


def _deterministic_direction_reply(messages: list[dict]) -> dict:
    user_texts = [str(m.get("content") or "").strip() for m in messages if str(m.get("role") or "") == "user"]
    instruction = "\n".join(x for x in user_texts if x).strip()
    lower = instruction.lower()
    vague = not instruction or lower in {
        "make it better", "better", "improve it", "improve", "another version", "try again", "regenerate",
    }
    has_change = any(word in lower for word in [
        "hook", "demo", "benefit", "cta", "problem", "reveal", "social proof", "before", "after",
        "replace", "keep", "remove", "add", "reorder", "order", "shorter", "longer", "pacing", "fast", "slow",
    ])
    has_duration = bool(re.search(r"\b\d{1,2}(?:\.\d+)?\s*(?:s|sec|secs|second|seconds)\b", lower))
    if vague:
        return {
            "ready": False,
            "reply": (
                "Give me as much detail as you can so I can make the edit accurately. "
                "What should I definitely keep, what should I change (hook/demo/benefit/CTA), and about how long should the ad feel?"
            ),
            "direction": instruction,
            "provider": "deterministic",
        }
    if not has_change and not has_duration and len(instruction.split()) < 10:
        return {
            "ready": False,
            "reply": (
                "I can do that. To make the next version precise, tell me at least one concrete edit—for example which section to keep/replace, "
                "whether to add another source clip, the order you want, or a target length."
            ),
            "direction": instruction,
            "provider": "deterministic",
        }
    return {
        "ready": True,
        "reply": "Got it. I have enough detail to make a focused iteration. You can add another note, or generate the version now.",
        "direction": instruction,
        "provider": "deterministic",
    }


def refine_ai_direction(
    product,
    workspace,
    ad_name: str,
    messages: list[dict],
    *,
    draft_segments: list[dict] | None = None,
    model: str = "gpt-5.6-luna",
) -> dict:
    """Turn a short editor conversation into a precise edit direction, asking a follow-up when needed."""
    parent = find_ad_plan(workspace, ad_name)
    clean_messages = []
    for message in list(messages or [])[-12:]:
        role = str(message.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content") or "").strip()[:1200]
        if content:
            clean_messages.append({"role": role, "content": content})
    fallback = _deterministic_direction_reply(clean_messages)
    if not os.getenv("OPENAI_API_KEY"):
        return fallback
    try:
        from openai import OpenAI

        schema = {
            "type": "object",
            "properties": {
                "ready": {"type": "boolean"},
                "reply": {"type": "string"},
                "direction": {"type": "string"},
            },
            "required": ["ready", "reply", "direction"],
            "additionalProperties": False,
        }
        clip_summary = []
        prompt_segments = list(draft_segments or parent.get("segments") or [])
        for index, seg in enumerate(prompt_segments):
            clip_summary.append(
                f"{index + 1}. {seg.get('role','custom')} @{seg.get('creator','')} "
                f"{float(seg.get('start',0)):.2f}-{float(seg.get('end',0)):.2f}s"
            )
        transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in clean_messages)
        prompt = (
            "You are the creative director inside a short-form UGC ad editor. Help the user specify one edit accurately before rendering. "
            "Directions are optional, but when they provide a direction you should make it precise. If the request is vague or could be interpreted multiple ways, "
            "ask exactly one useful clarifying question. Encourage specificity about what to KEEP, CHANGE, ADD/REMOVE, ORDER, target duration, pacing, and any source/creator preference. "
            "Do not invent product claims. If the request is already actionable, set ready=true and summarize it as a concise editing instruction in direction. "
            "Keep reply under 80 words.\n\n"
            f"PRODUCT: {product.title}\nBRAND: {product.brand}\nAD: {ad_name}\n"
            f"CURRENT CUTS:\n" + "\n".join(clip_summary) + "\n\nEDITOR CHAT:\n" + (transcript or "(no user direction yet)")
        )
        client = OpenAI()
        response = client.responses.create(
            model=model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            text={"format": {"type": "json_schema", "name": "editor_direction", "strict": True, "schema": schema}},
        )
        data = json.loads(response.output_text)
        return {
            "ready": bool(data.get("ready")),
            "reply": str(data.get("reply") or fallback["reply"])[:1200],
            "direction": str(data.get("direction") or fallback["direction"])[:2400],
            "provider": "openai",
        }
    except Exception as exc:
        fallback["reply"] = fallback["reply"] + f" (Local guidance used because AI clarification was unavailable: {type(exc).__name__}.)"
        return fallback

def ai_iterate_ad(
    product,
    workspace,
    ad_name: str,
    *,
    instruction: str = "",
    mode: str = "iterate",
    model: str = "gpt-5.6-luna",
    base_segments: list[dict] | None = None,
) -> dict:
    mode = "regenerate" if str(mode).lower() in {"regenerate", "remix", "fresh"} else "iterate"
    parent = find_ad_plan(workspace, ad_name)
    working_parent = deepcopy(parent)
    based_on_draft = bool(base_segments)
    if base_segments:
        working_parent["segments"] = sanitize_editor_segments(workspace, list(base_segments))
        working_parent["duration_seconds"] = round(sum(float(s["end"]) - float(s["start"]) for s in working_parent["segments"]), 3)
    pool = _pool_for_workspace(workspace)
    provider = "deterministic"
    try:
        segments, summary = _ai_choose_sequence(product, working_parent, pool, instruction, mode, model)
        provider = "openai"
    except Exception as exc:
        segments, summary = _fallback_iteration(working_parent, pool, instruction, mode)
        summary += f" (AI fallback: {type(exc).__name__})"

    kind = "regen" if mode == "regenerate" else "ai"
    plan = render_editor_version(
        workspace,
        ad_name,
        segments,
        strategy="ai_regenerate" if mode == "regenerate" else "ai_iteration",
        version_kind=kind,
        label=instruction,
        metadata={
            "ai_iteration": {
                "mode": mode,
                "instruction": str(instruction or ""),
                "summary": summary,
                "provider": provider,
                "model": model if provider == "openai" else "deterministic",
                "based_on_editor_draft": based_on_draft,
            }
        },
    )
    return plan
