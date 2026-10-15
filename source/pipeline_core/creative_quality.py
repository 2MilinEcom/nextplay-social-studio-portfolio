from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ad_builder import load_segment_pool, render_ad_plans
from .media import extract_audio, extract_frames, image_data_url, probe_duration, temp_media_dir
from .rights import load_rights_state
from .source_quality import load_source_quality
from .storage import atomic_write_json, read_json
from .studio_settings import load_studio_settings

QUALITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall": {"type": "integer", "minimum": 0, "maximum": 100},
        "hook": {"type": "integer", "minimum": 0, "maximum": 100},
        "product_clarity": {"type": "integer", "minimum": 0, "maximum": 100},
        "pacing": {"type": "integer", "minimum": 0, "maximum": 100},
        "story_coherence": {"type": "integer", "minimum": 0, "maximum": 100},
        "cta": {"type": "integer", "minimum": 0, "maximum": 100},
        "audio_continuity": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["strong", "usable", "rebuild"]},
        "weakest_role": {"type": "string"},
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "message": {"type": "string"},
                    "segment_index": {"type": "integer", "minimum": -1},
                },
                "required": ["severity", "category", "message", "segment_index"],
            },
        },
        "recommended_action": {"type": "string"},
        "headline_suggestion": {"type": "string"},
    },
    "required": [
        "overall", "hook", "product_clarity", "pacing", "story_coherence", "cta",
        "audio_continuity", "verdict", "weakest_role", "summary", "strengths", "issues",
        "recommended_action", "headline_suggestion",
    ],
}


def quality_dir(workspace) -> Path:
    path = workspace.ads_dir / "quality"
    path.mkdir(parents=True, exist_ok=True)
    return path


def quality_path(workspace, ad_name: str) -> Path:
    return quality_dir(workspace) / f"{ad_name}.json"


def _normalized_verdict(overall: int) -> str:
    return "strong" if overall >= 85 else "usable" if overall >= 72 else "rebuild"


def normalize_quality_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result or {})
    try:
        overall = max(0, min(100, int(result.get("overall", 0) or 0)))
    except Exception:
        overall = 0
    raw_verdict = str(result.get("verdict") or "")
    verdict = _normalized_verdict(overall)
    if raw_verdict and raw_verdict != verdict and "model_verdict" not in result:
        result["model_verdict"] = raw_verdict
    result["overall"] = overall
    result["verdict"] = verdict
    return result


def heuristic_quality(plan: dict) -> dict[str, Any]:
    segs = list(plan.get("segments") or [])
    roles = [str(s.get("role") or "") for s in segs]
    durations = [max(0.0, float(s.get("end", 0)) - float(s.get("start", 0))) for s in segs]
    total = sum(durations) or float(plan.get("duration_seconds", 0) or 0)
    first = segs[0] if segs else {}
    hook = 90 if first.get("role") == "hook" and durations and durations[0] <= 4.5 else 76 if first.get("role") in {"hook", "problem"} else 45
    matches = [float(s.get("product_match_score", 0) or 0) for s in segs if s.get("role") in {"product_reveal", "demo", "benefit", "before_after"}]
    visible = [s for s in segs if s.get("product_visible")]
    product = round(100 * (sum(matches) / max(1, len(matches)))) if matches else 45
    if visible:
        product = min(100, product + 8)
    long_cuts = sum(1 for d in durations if d > 6.2)
    target = float(plan.get("target_duration_seconds", 20) or 20)
    pacing = 96 - long_cuts * 12 - round(abs(total - target) * 2.5)
    pacing = max(30, min(100, pacing))
    order = {"hook": 0, "problem": 0, "product_reveal": 1, "demo": 2, "before_after": 2, "benefit": 3, "social_proof": 3, "objection": 3, "cta": 4}
    positions = [order.get(r, 2) for r in roles]
    inversions = sum(1 for a, b in zip(positions, positions[1:]) if b < a - 1)
    coherence = max(40, 94 - inversions * 16)
    if len({s.get("video_id") for s in segs}) < max(1, len(segs) - 1):
        coherence -= 5
    cta = 92 if "cta" in roles else 68 if roles and roles[-1] in {"benefit", "social_proof"} else 35
    creator_changes = sum(1 for a, b in zip(segs, segs[1:]) if a.get("creator") != b.get("creator"))
    audio = max(55, 90 - max(0, creator_changes - 2) * 7)
    overall = round(hook * 0.2 + product * 0.22 + pacing * 0.16 + coherence * 0.2 + cta * 0.12 + audio * 0.10)
    weakest_map = {"hook": hook, "product_reveal": product, "demo": product, "benefit": coherence, "cta": cta}
    weakest_role = min(weakest_map, key=weakest_map.get)
    issues = []
    if hook < 70:
        issues.append({"severity": "high", "category": "hook", "message": "The opening is not a strong hook/problem cut.", "segment_index": 0 if segs else None})
    if product < 65:
        issues.append({"severity": "high", "category": "product", "message": "Product visibility/match is weak in the product-bearing cuts.", "segment_index": None})
    if pacing < 70:
        issues.append({"severity": "medium", "category": "pacing", "message": "The total length or one or more clips are dragging.", "segment_index": None})
    if cta < 70:
        issues.append({"severity": "medium", "category": "cta", "message": "The ending does not contain a clear CTA.", "segment_index": len(segs) - 1 if segs else None})
    return {
        "provider": "heuristic",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "overall": max(0, min(100, overall)),
        "hook": hook,
        "product_clarity": product,
        "pacing": pacing,
        "story_coherence": coherence,
        "cta": cta,
        "audio_continuity": audio,
        "verdict": "strong" if overall >= 85 else "usable" if overall >= 72 else "rebuild",
        "weakest_role": weakest_role,
        "summary": "Deterministic creative QA based on the edit recipe, clip scores, product visibility, duration, and role order.",
        "strengths": [],
        "issues": issues,
        "recommended_action": "Keep" if overall >= 85 else "Review weak dimensions and replace only the weakest clip.",
        "headline_suggestion": "",
    }


class OpenAICreativeReviewer:
    def __init__(self, *, model: str = "gpt-5.6-luna", frame_count: int = 10):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install analysis dependencies first: python3 -m pip install -r requirements-analysis.txt") from exc
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not configured in scraper/.env")
        self.client = OpenAI()
        self.model = model
        self.frame_count = max(4, min(16, int(frame_count)))

    def _transcript_text(self, audio_path: Path) -> str:
        with audio_path.open("rb") as fh:
            result = self.client.audio.transcriptions.create(model="whisper-1", file=fh, response_format="text")
        return str(result or "").strip()

    def review(self, product, plan: dict) -> dict[str, Any]:
        video = Path(str(plan.get("output_path") or ""))
        if not video.exists():
            raise FileNotFoundError(f"Rendered ad not found: {video}")
        with temp_media_dir(prefix="scraper-quality-") as temp_name:
            temp = Path(temp_name)
            audio = extract_audio(video, temp / "audio.mp3")
            transcript = self._transcript_text(audio)
            frames = extract_frames(video, temp / "frames", count=self.frame_count)
            recipe = "\n".join(
                f"{i+1}. {s.get('role')} {float(s.get('end',0))-float(s.get('start',0)):.1f}s — {s.get('text','')}"
                for i, s in enumerate(plan.get("segments") or [])
            )
            prompt = (
                "You are a senior direct-response short-form video creative director. Review the FINISHED rendered ad, "
                "not merely its source clips. Judge whether a cold viewer would understand the product and whether the edit "
                "feels coherent. Do not predict ROAS, sales, or platform performance. Score only observable creative quality. "
                "Flag mid-sentence cuts, repeated ideas, weak first 3 seconds, late/unclear product visibility, jarring audio, "
                "poor pacing, unsupported claims, or weak CTA. If a specific recipe segment is the problem, name its role and index.\n\n"
                f"PRODUCT: {product.title}\nBRAND: {product.brand}\nDESCRIPTION: {product.description}\n"
                f"EDIT RECIPE:\n{recipe}\n\nFINISHED-AD TRANSCRIPT:\n{transcript or '(no speech)'}"
            )
            content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
            for timestamp, frame in frames:
                content.append({"type": "input_text", "text": f"Rendered ad frame at {timestamp:.2f}s:"})
                content.append({"type": "input_image", "image_url": image_data_url(frame), "detail": "low"})
            response = self.client.responses.create(
                model=self.model,
                input=[{"role": "user", "content": content}],
                text={"format": {"type": "json_schema", "name": "finished_ad_quality", "strict": True, "schema": QUALITY_SCHEMA}},
            )
            result = json.loads(response.output_text)
        result["provider"] = "openai"
        result["model"] = self.model
        result["reviewed_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(probe_duration(video), 3)
        return result


def load_quality_reviews(workspace) -> dict[str, dict]:
    out = {}
    folder = quality_dir(workspace)
    for path in folder.glob("*.json"):
        try:
            out[path.stem] = normalize_quality_result(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def review_ads(product, workspace, *, provider: str = "openai", force: bool = False, limit: int | None = None, model: str = "gpt-5.6-luna") -> list[dict]:
    plans = read_json(workspace.ads_dir / "ad_plans.json", []) or []
    existing = load_quality_reviews(workspace)
    if limit is not None:
        plans = plans[: max(0, int(limit))]
    reviewer = OpenAICreativeReviewer(model=model) if provider == "openai" else None
    results = []
    for index, plan in enumerate(plans, start=1):
        name = str(plan.get("name") or f"ad_{index}")
        output = Path(str(plan.get("output_path") or ""))
        if not output.exists():
            continue
        if name in existing and not force:
            existing_provider = str(existing[name].get("provider") or "")
            # If AI is requested, upgrade an older heuristic/fallback review instead of silently keeping it.
            if provider != "openai" or existing_provider == "openai":
                print(f"[{index}/{len(plans)}] quality already reviewed: {name}")
                results.append(existing[name])
                continue
        print(f"[{index}/{len(plans)}] reviewing finished ad: {name}")
        try:
            result = reviewer.review(product, plan) if reviewer else heuristic_quality(plan)
        except Exception as exc:
            print(f"  AI review failed ({type(exc).__name__}: {exc}); saving deterministic QA instead.")
            result = heuristic_quality(plan)
            result["fallback_error"] = f"{type(exc).__name__}: {exc}"
        # Keep the qualitative label consistent with the numeric score even if
        # the model returns a contradictory verdict. Preserve the raw label for audit.
        result = normalize_quality_result(result)
        result["ad_name"] = name
        atomic_write_json(quality_path(workspace, name), result)
        print(f"  -> creative quality {result.get('overall')}/100 · {result.get('verdict')}")
        results.append(result)
    return results


def _replacement_for_role(pool: dict[str, list[dict]], role: str, plan: dict) -> dict | None:
    current_ids = {str(s.get("segment_id") or "") for s in plan.get("segments") or []}
    current_videos = {str(s.get("video_id") or "") for s in plan.get("segments") or []}
    candidates = list(pool.get(role) or [])
    if not candidates and role == "product_reveal":
        candidates = list(pool.get("demo") or [])
    if not candidates and role == "benefit":
        candidates = list(pool.get("social_proof") or [])
    for item in candidates:
        if item.get("segment_id") in current_ids:
            continue
        if item.get("video_id") in current_videos and len(candidates) > 2:
            continue
        return item
    return None


def auto_improve_ads(product, workspace, *, threshold: int | None = None, review_provider: str = "heuristic", max_ads: int = 10) -> list[dict]:
    settings = load_studio_settings(workspace)
    threshold = int(threshold if threshold is not None else settings["quality_threshold"])
    plans_path = workspace.ads_dir / "ad_plans.json"
    plans = read_json(plans_path, []) or []
    qualities = load_quality_reviews(workspace)
    rights = load_rights_state(workspace)
    source_quality = load_source_quality(workspace)
    from .review_state import load_review_state
    reviews = load_review_state(workspace)
    pool = load_segment_pool(
        workspace.analysis_dir,
        review_state=reviews,
        rights_state=rights,
        source_quality=source_quality,
        product_match_min=float(settings["product_match_min"]),
        video_relevance_min=float(settings["video_relevance_min"]),
        require_cleared_rights=bool(settings["require_cleared_rights"]),
    )
    new_plans = []
    for plan in plans:
        if len(new_plans) >= max_ads:
            break
        name = str(plan.get("name") or "")
        q = qualities.get(name) or heuristic_quality(plan)
        if int(q.get("overall", 0) or 0) >= threshold:
            continue
        weak_role = str(q.get("weakest_role") or "")
        index = next((i for i, s in enumerate(plan.get("segments") or []) if s.get("role") == weak_role), None)
        if index is None:
            index = min(range(len(plan.get("segments") or [])), key=lambda i: float(plan["segments"][i].get("score", 0))) if plan.get("segments") else None
        if index is None:
            continue
        old_role = str(plan["segments"][index].get("role") or weak_role)
        replacement = _replacement_for_role(pool, old_role, plan)
        if not replacement:
            continue
        replacement_seg = {
            "video_path": replacement["video_path"], "video_id": replacement["video_id"],
            "creator": replacement.get("creator", ""), "tiktok_url": replacement.get("tiktok_url", ""),
            "start": round(float(replacement["start"]), 3),
            "end": round(min(float(replacement["end"]), float(replacement["start"]) + 6.0), 3),
            "role": replacement.get("role") or old_role, "segment_id": replacement.get("segment_id", ""),
            "score": round(float(replacement.get("rank_score", 0)), 4), "raw_score": round(float(replacement.get("score", 0)), 4),
            "product_match_score": round(float(replacement.get("product_match_score", 0)), 4),
            "product_visible": bool(replacement.get("product_visible")), "text": str(replacement.get("text") or ""),
            "reason": str(replacement.get("reason") or ""), "rights_status": replacement.get("rights_status", "unknown"),
        }
        improved = json.loads(json.dumps(plan))
        improved["segments"][index] = replacement_seg
        improved["name"] = f"{name}_v2"
        improved["parent_ad"] = name
        improved["strategy"] = "quality_repair"
        improved["repaired_dimension"] = weak_role
        improved["output_path"] = str(workspace.ads_dir / f"{improved['name']}.mp4")
        improved["duration_seconds"] = round(sum(float(s["end"]) - float(s["start"]) for s in improved["segments"]), 3)
        print(f"Improving {name}: replacing {old_role} with {replacement_seg['segment_id']}")
        render_ad_plans([improved])
        new_plans.append(improved)
    if new_plans:
        atomic_write_json(plans_path, plans + new_plans)
        review_ads(product, workspace, provider=review_provider, force=False, limit=None)
    return new_plans
