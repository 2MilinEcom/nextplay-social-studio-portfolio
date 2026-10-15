from __future__ import annotations

import json
import re
from pathlib import Path

from .rights import is_cleared, rights_status

ROLE_CAPS = {
    "hook": 4.5,
    "problem": 5.0,
    "product_reveal": 4.5,
    "demo": 7.0,
    "benefit": 5.5,
    "social_proof": 5.0,
    "objection": 5.0,
    "before_after": 6.0,
    "cta": 4.5,
}


def _sentence_score(text: str) -> float:
    text = str(text or "").strip()
    if not text:
        return 0.45
    score = 0.55
    if len(text.split()) >= 3:
        score += 0.12
    if re.search(r"[.!?]['\"]?$", text):
        score += 0.18
    if text[:1].isupper():
        score += 0.05
    if text.lower().startswith(("and ", "but ", "so ", "because ")):
        score -= 0.12
    return max(0.0, min(1.0, score))


def _load_segments(
    analysis_dir: Path,
    review_state: dict | None = None,
    *,
    rights_state: dict | None = None,
    source_quality: dict | None = None,
    product_match_min: float = 0.0,
    video_relevance_min: float = 0.0,
    require_cleared_rights: bool = False,
) -> dict[str, list[dict]]:
    by_role: dict[str, list[dict]] = {}
    segment_reviews = dict((review_state or {}).get("segments") or {})
    quality_sources = dict((source_quality or {}).get("sources") or {})
    for path in sorted(Path(analysis_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("status") != "complete":
            continue
        source = data.get("source") or {}
        source_path = source.get("local_path")
        if not source_path:
            continue
        video_id = str(source.get("video_id") or Path(source_path).stem)
        if require_cleared_rights and not is_cleared(rights_state, video_id):
            continue
        video_relevance = float(data.get("video_relevance_score", 0.5) or 0.0)
        if video_relevance < float(video_relevance_min or 0.0):
            continue
        source_q = dict(quality_sources.get(video_id) or {})
        duplicate_penalty = 0.08 if source_q.get("duplicate_of") else 0.0
        for segment_index, segment in enumerate(data.get("segments") or []):
            role = str(segment.get("role") or "").lower()
            try:
                start = float(segment["start"])
                end = float(segment["end"])
                score = float(segment.get("score", 0.5))
                product_match = float(segment.get("product_match_score", 0.5))
            except Exception:
                continue
            if end <= start or not role:
                continue
            # Product-bearing roles should visibly match the product. Hooks/problems
            # can still work before the product is shown, so they use a softer gate.
            if role in {"product_reveal", "demo", "benefit", "before_after"} and product_match < product_match_min:
                continue
            segment_id = f"{video_id}:{segment_index}"
            review = dict(segment_reviews.get(segment_id) or {})
            if review.get("status") == "rejected":
                continue
            review_boost = 0.18 if review.get("favorite") else 0.10 if review.get("status") == "approved" else 0.0
            completeness = _sentence_score(segment.get("text") or "")
            visible_boost = 0.04 if segment.get("product_visible") else 0.0
            rank_score = (
                score * 0.54
                + product_match * 0.22
                + video_relevance * 0.10
                + completeness * 0.10
                + visible_boost
                + review_boost
                - duplicate_penalty
            )
            enriched = dict(segment)
            if review.get("caption"):
                enriched["text"] = str(review.get("caption"))
            enriched.update(
                {
                    "video_path": source_path,
                    "video_id": video_id,
                    "segment_id": segment_id,
                    "creator": source.get("creator") or "",
                    "tiktok_url": source.get("tiktok_url") or "",
                    "start": start,
                    "end": end,
                    "score": score,
                    "product_match_score": product_match,
                    "video_relevance_score": video_relevance,
                    "review": review,
                    "rights_status": rights_status(rights_state, video_id),
                    "sentence_completeness": completeness,
                    "rank_score": rank_score,
                }
            )
            by_role.setdefault(role, []).append(enriched)
    for role in by_role:
        by_role[role].sort(key=lambda s: -s.get("rank_score", 0.0))
    return by_role


def load_segment_pool(*args, **kwargs) -> dict[str, list[dict]]:
    return _load_segments(*args, **kwargs)


def _pick(pool: list[dict], offset: int, used_video_ids: set[str], used_creators: set[str]) -> dict | None:
    if not pool:
        return None
    # First pass maximizes creator/source diversity.
    for shift in range(len(pool)):
        item = pool[(offset + shift) % len(pool)]
        creator = str(item.get("creator") or "")
        if item.get("video_id") not in used_video_ids and (not creator or creator not in used_creators):
            return item
    # Second pass allows another creator/source if the role pool is small.
    for shift in range(len(pool)):
        item = pool[(offset + shift) % len(pool)]
        if item.get("video_id") not in used_video_ids:
            return item
    return pool[offset % len(pool)]


def _choose_for_role(by_role: dict[str, list[dict]], role_options: tuple[str, ...], offset: int, used: set[str], creators: set[str]):
    for role in role_options:
        item = _pick(by_role.get(role, []), offset, used, creators)
        if item:
            return item
    return None


def _trim_item(item: dict, remaining: float) -> dict | None:
    item = dict(item)
    role = str(item.get("role") or "")
    cap = ROLE_CAPS.get(role, 6.0)
    desired = min(item["end"] - item["start"], cap, remaining)
    if desired < 0.6:
        return None
    item["end"] = item["start"] + desired
    return item


def _plan_segment(item: dict) -> dict:
    return {
        "video_path": item["video_path"],
        "video_id": item["video_id"],
        "creator": item.get("creator", ""),
        "tiktok_url": item.get("tiktok_url", ""),
        "start": round(item["start"], 3),
        "end": round(item["end"], 3),
        "role": item["role"],
        "segment_id": item.get("segment_id", ""),
        "score": round(item.get("rank_score", 0.0), 4),
        "raw_score": round(float(item.get("score", 0.0)), 4),
        "product_match_score": round(float(item.get("product_match_score", 0.0)), 4),
        "product_visible": bool(item.get("product_visible", False)),
        "text": str(item.get("text") or ""),
        "reason": item.get("reason", ""),
        "rights_status": item.get("rights_status", "unknown"),
    }


def _headline_from_segments(segments: list[dict]) -> str:
    first = next((s for s in segments if s.get("role") in {"hook", "problem"}), segments[0] if segments else {})
    text = str(first.get("text") or "").strip()
    if len(text) > 92:
        text = text[:89].rstrip() + "..."
    return text


def build_ad_plans(
    analysis_dir: Path,
    ads_dir: Path,
    count: int = 10,
    *,
    max_duration: float = 28.0,
    target_duration: float | None = None,
    review_state: dict | None = None,
    rights_state: dict | None = None,
    source_quality: dict | None = None,
    product_match_min: float = 0.0,
    video_relevance_min: float = 0.0,
    require_cleared_rights: bool = False,
    render_settings: dict | None = None,
) -> list[dict]:
    by_role = _load_segments(
        analysis_dir,
        review_state=review_state,
        rights_state=rights_state,
        source_quality=source_quality,
        product_match_min=product_match_min,
        video_relevance_min=video_relevance_min,
        require_cleared_rights=require_cleared_rights,
    )
    if not by_role.get("hook") and not by_role.get("problem"):
        raise ValueError("Need at least one usable hook/problem segment before ads can be built.")
    if not any(by_role.get(role) for role in ("demo", "benefit", "product_reveal")):
        raise ValueError("Need at least one usable product/demo/benefit segment before ads can be built.")

    target_duration = float(target_duration or min(max_duration, 20.0))
    max_duration = max(target_duration, float(max_duration))
    templates = [
        ("direct_response", [("hook",), ("product_reveal", "demo"), ("demo", "benefit"), ("benefit", "social_proof"), ("cta",)]),
        ("problem_solution", [("problem", "hook"), ("product_reveal", "demo"), ("demo",), ("benefit",), ("cta",)]),
        ("social_proof", [("hook",), ("social_proof",), ("demo", "product_reveal"), ("benefit",), ("cta",)]),
        ("before_after", [("hook",), ("before_after", "problem"), ("demo",), ("benefit",), ("cta",)]),
        ("objection_answer", [("hook", "problem"), ("objection",), ("demo", "product_reveal"), ("benefit",), ("cta",)]),
        ("voiceover_broll", [("hook",), ("demo", "product_reveal"), ("benefit",), ("cta",)]),
    ]

    ads_dir.mkdir(parents=True, exist_ok=True)
    plans: list[dict] = []
    for index in range(max(0, count)):
        template_name, role_steps = templates[index % len(templates)]
        offset = index // len(templates)
        used: set[str] = set()
        used_segment_ids: set[str] = set()
        creators: set[str] = set()
        segments: list[dict] = []
        total = 0.0
        if template_name == "voiceover_broll":
            hook = _choose_for_role(by_role, ("hook", "problem"), offset, used, creators)
            visual = _choose_for_role(by_role, ("demo", "product_reveal"), offset + 1, used, creators)
            voice = _choose_for_role(by_role, ("benefit", "social_proof"), offset + 2, used, creators)
            cta = _choose_for_role(by_role, ("cta",), offset + 3, used, creators)
            if hook:
                h = _trim_item(hook, max_duration - total)
                if h:
                    segments.append(_plan_segment(h)); total += h["end"] - h["start"]
                    used.add(str(h.get("video_id") or "")); creators.add(str(h.get("creator") or ""))
            if visual and voice and total < max_duration:
                duration = min(5.5, visual["end"] - visual["start"], voice["end"] - voice["start"], max_duration - total)
                if duration >= 0.8:
                    composite = dict(visual)
                    composite["end"] = composite["start"] + duration
                    planned = _plan_segment(composite)
                    planned.update({
                        "role": "benefit",
                        "text": str(voice.get("text") or ""),
                        "reason": "Voiceover-style B-roll: benefit/social-proof audio continues over a separate product visual.",
                        "audio_path": voice["video_path"],
                        "audio_start": round(float(voice["start"]), 3),
                        "audio_end": round(float(voice["start"]) + duration, 3),
                        "audio_segment_id": voice.get("segment_id", ""),
                        "audio_creator": voice.get("creator", ""),
                        "audio_tiktok_url": voice.get("tiktok_url", ""),
                        "broll_segment_id": visual.get("segment_id", ""),
                    })
                    segments.append(planned); total += duration
                    used.add(str(visual.get("video_id") or "")); used.add(str(voice.get("video_id") or ""))
                    creators.add(str(visual.get("creator") or "")); creators.add(str(voice.get("creator") or ""))
            if cta and total < max_duration:
                c = _trim_item(cta, max_duration - total)
                if c:
                    segments.append(_plan_segment(c)); total += c["end"] - c["start"]
            role_steps = []
        for step_index, role_options in enumerate(role_steps):
            item = _choose_for_role(by_role, role_options, offset + step_index, used, creators)
            if not item or str(item.get("segment_id") or "") in used_segment_ids:
                continue
            remaining = max_duration - total
            trimmed = _trim_item(item, remaining)
            if not trimmed:
                continue
            segments.append(_plan_segment(trimmed))
            total += trimmed["end"] - trimmed["start"]
            used.add(str(trimmed.get("video_id") or ""))
            used_segment_ids.add(str(trimmed.get("segment_id") or ""))
            if trimmed.get("creator"):
                creators.add(str(trimmed.get("creator")))
            if total >= target_duration and len(segments) >= 4:
                break
        if len(segments) < 2:
            continue
        render = dict(render_settings or {})
        if render.get("headline_enabled", True) and not render.get("headline_text"):
            render["headline_text"] = _headline_from_segments(segments)
        plans.append(
            {
                "name": f"ad_{index + 1:03d}_{template_name}",
                "template": template_name,
                "strategy": "creative_mix",
                "duration_seconds": round(total, 3),
                "target_duration_seconds": round(target_duration, 3),
                "segments": segments,
                "output_path": str(ads_dir / f"ad_{index + 1:03d}_{template_name}.mp4"),
                "render": render,
                "rights_notice": "Clear source creator usage rights before commercial publication. Use the Rights controls to mark licensed/approved sources.",
            }
        )
    return plans


def build_ab_test_plans(
    analysis_dir: Path,
    ads_dir: Path,
    *,
    count: int = 6,
    max_duration: float = 24.0,
    target_duration: float = 20.0,
    review_state: dict | None = None,
    rights_state: dict | None = None,
    source_quality: dict | None = None,
    product_match_min: float = 0.0,
    video_relevance_min: float = 0.0,
    require_cleared_rights: bool = False,
    render_settings: dict | None = None,
) -> list[dict]:
    """Create controlled creative variants where one variable changes at a time."""
    pool = _load_segments(
        analysis_dir,
        review_state=review_state,
        rights_state=rights_state,
        source_quality=source_quality,
        product_match_min=product_match_min,
        video_relevance_min=video_relevance_min,
        require_cleared_rights=require_cleared_rights,
    )
    hooks = pool.get("hook") or pool.get("problem") or []
    demos = pool.get("demo") or pool.get("product_reveal") or []
    benefits = pool.get("benefit") or pool.get("social_proof") or []
    ctas = pool.get("cta") or pool.get("social_proof") or []
    if not hooks or not demos or not benefits:
        raise ValueError("A/B tests require at least a hook, demo/product reveal, and benefit/social-proof segment.")
    base = [hooks[0], demos[0], benefits[0]] + ([ctas[0]] if ctas else [])
    variables = [("hook", hooks), ("demo", demos), ("benefit", benefits)]
    if ctas:
        variables.append(("cta", ctas))
    plans: list[dict] = []
    variant_index = 0
    for variable, variants in variables:
        for candidate in variants[:3]:
            if len(plans) >= count:
                break
            chosen = list(base)
            role_index = {"hook": 0, "demo": 1, "benefit": 2, "cta": 3}.get(variable)
            if role_index is None or role_index >= len(chosen):
                continue
            chosen[role_index] = candidate
            # Avoid exact duplicate plans while allowing a controlled single-variable change.
            ids = [x.get("segment_id") for x in chosen]
            if any([p.get("ab_segment_ids") == ids for p in plans]):
                continue
            total = 0.0
            plan_segments = []
            for item in chosen:
                remaining = max_duration - total
                trimmed = _trim_item(item, remaining)
                if not trimmed:
                    continue
                plan_segments.append(_plan_segment(trimmed))
                total += trimmed["end"] - trimmed["start"]
                if total >= target_duration and len(plan_segments) >= 4:
                    break
            if len(plan_segments) < 3:
                continue
            variant_index += 1
            experiment = f"{variable}_test"
            label = chr(ord("A") + (variant_index - 1) % 26)
            name = f"ab_{variant_index:03d}_{variable}_{label}"
            render = dict(render_settings or {})
            if render.get("headline_enabled", True) and not render.get("headline_text"):
                render["headline_text"] = _headline_from_segments(plan_segments)
            plans.append({
                "name": name,
                "template": "ab_controlled",
                "strategy": "ab_test",
                "experiment_id": experiment,
                "variable": variable,
                "variant": label,
                "ab_segment_ids": [s.get("segment_id") for s in plan_segments],
                "duration_seconds": round(total, 3),
                "target_duration_seconds": round(target_duration, 3),
                "segments": plan_segments,
                "output_path": str(Path(ads_dir) / f"{name}.mp4"),
                "render": render,
                "rights_notice": "Clear source creator usage rights before commercial publication.",
            })
        if len(plans) >= count:
            break
    return plans[:count]


def render_ad_plans(plans: list[dict]) -> list[str]:
    from splice_videos import splice_videos

    outputs = []
    for index, plan in enumerate(plans, start=1):
        print(f"Rendering ad {index}/{len(plans)} -> {plan['output_path']}")
        render = dict(plan.get("render") or {})
        outputs.append(
            splice_videos(
                plan["segments"],
                plan["output_path"],
                captions=bool(render.get("captions_enabled", False)),
                headline=str(render.get("headline_text") or "") if render.get("headline_enabled", False) else "",
                audio_normalize=bool(render.get("audio_normalize", True)),
                audio_fade_ms=int(render.get("audio_fade_ms", 70) or 0),
            )
        )
    return outputs
