from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from .ad_builder import build_ab_test_plans, build_ad_plans, render_ad_plans
from .ad_editor import ai_iterate_ad, refine_ai_direction, render_editor_version, restore_ad_version
from .analysis_jobs import prepare_analysis_jobs
from .creative_quality import auto_improve_ads, heuristic_quality, load_quality_reviews, review_ads
from .analyzer import analyze_jobs
from .browser import init_browser
from .pipeline import ProductPipeline
from .review_state import load_review_state, save_review_state, update_review_item
from .rights import RIGHTS_STATUSES, is_cleared, load_rights_state, update_source_rights
from .dashboard_previews import poster_url, make_poster
from .source_quality import evaluate_candidate_thumbnails, load_source_quality, update_from_analyses
from .studio_settings import load_studio_settings, update_studio_settings
from .storage import ProductWorkspace, atomic_write_json, load_product_workspace, read_json

ROLE_ORDER = [
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


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _relative_media_url(workspace: ProductWorkspace, value: str | None) -> str | None:
    if not value:
        return None
    try:
        path = Path(value).expanduser().resolve()
        root = workspace.root.resolve()
        rel = path.relative_to(root)
        return "/file/" + "/".join(rel.parts)
    except Exception:
        return None


def _analysis_rows(workspace: ProductWorkspace, review_state: dict) -> list[dict]:
    reviews = review_state.get("segments") or {}
    rows: list[dict] = []
    for path in sorted(workspace.analysis_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = data.get("source") or {}
        video_id = source.get("video_id") or path.stem
        segments = []
        for index, segment in enumerate(data.get("segments") or []):
            item = dict(segment)
            item["index"] = index
            item["segment_id"] = f"{video_id}:{index}"
            item["start"] = round(_safe_float(item.get("start")), 3)
            item["end"] = round(_safe_float(item.get("end")), 3)
            item["score"] = round(_safe_float(item.get("score")), 4)
            item["product_match_score"] = round(_safe_float(item.get("product_match_score")), 4)
            item["review"] = dict(reviews.get(item["segment_id"]) or {})
            item["caption_text"] = str(item["review"].get("caption") or item.get("text") or "")
            segments.append(item)
        rows.append(
            {
                "job_file": path.name,
                "status": data.get("status") or "pending",
                "video_id": video_id,
                "creator": source.get("creator") or "",
                "tiktok_url": source.get("tiktok_url") or "",
                "local_path": source.get("local_path") or "",
                "media_url": _relative_media_url(workspace, source.get("local_path")),
                "duration_seconds": round(_safe_float(data.get("duration_seconds")), 3),
                "summary": data.get("summary") or "",
                "video_relevance_score": round(_safe_float(data.get("video_relevance_score")), 4),
                "recommended_use": data.get("recommended_use") or "",
                "provider": data.get("provider") or {},
                "error": data.get("error"),
                "segments": segments,
            }
        )
    return rows


def _ad_quality(plan: dict) -> dict:
    segs = list(plan.get("segments") or [])
    roles = [str(s.get("role") or "") for s in segs]
    duration = _safe_float(plan.get("duration_seconds"))
    avg_score = sum(_safe_float(s.get("score")) for s in segs) / max(1, len(segs))
    hook_scores = [_safe_float(s.get("score")) for s in segs if s.get("role") in {"hook", "problem"}]
    product_scores = [_safe_float(s.get("score")) for s in segs if s.get("role") in {"product_reveal", "demo", "benefit"}]
    hook = min(100, round((max(hook_scores) if hook_scores else 0.0) * 100))
    product = min(100, round((sum(product_scores) / max(1, len(product_scores))) * 100))
    if 15 <= duration <= 25:
        pacing = 100
    elif duration < 15:
        pacing = max(35, round(100 - (15 - duration) * 5))
    else:
        pacing = max(35, round(100 - (duration - 25) * 4))
    desired = {"hook", "problem", "product_reveal", "demo", "benefit", "social_proof", "cta"}
    coverage = round(min(1.0, len(set(roles) & desired) / 5.0) * 100)
    creators = {str(s.get("creator") or s.get("video_id") or "") for s in segs}
    diversity = round(min(1.0, len(creators) / max(1, min(4, len(segs)))) * 100)
    overall = round(hook * 0.25 + product * 0.25 + pacing * 0.15 + coverage * 0.2 + diversity * 0.05 + avg_score * 100 * 0.1)
    return {
        "overall": min(100, overall),
        "hook": hook,
        "product_clarity": product,
        "pacing": pacing,
        "coverage": coverage,
        "creator_diversity": diversity,
    }


def _ad_rows(workspace: ProductWorkspace, review_state: dict) -> list[dict]:
    reviews = review_state.get("ads") or {}
    plans_path = workspace.ads_dir / "ad_plans.json"
    plans = read_json(plans_path, []) or []
    rows: list[dict] = []
    for plan in plans:
        item = dict(plan)
        output_path = item.get("output_path")
        item["media_url"] = _relative_media_url(workspace, output_path)
        item["exists"] = bool(output_path and Path(output_path).expanduser().exists())
        item["review"] = dict(reviews.get(str(item.get("name") or "")) or {})
        item["quality"] = _ad_quality(item)
        rows.append(item)
    return rows


def _workspace_choices(project_dir: Path) -> list[dict]:
    products_dir = project_dir / "products"
    choices = []
    if not products_dir.exists():
        return choices
    for product_json in products_dir.glob("*/product.json"):
        try:
            data = read_json(product_json, {}) or {}
            choices.append(
                {
                    "path": str(product_json.parent.resolve()),
                    "title": data.get("title") or product_json.parent.name,
                    "brand": data.get("brand") or "",
                    "modified": product_json.stat().st_mtime,
                }
            )
        except Exception:
            continue
    choices.sort(key=lambda x: -x["modified"])
    for row in choices:
        row.pop("modified", None)
    return choices


def _project_summaries(project_dir: Path) -> list[dict]:
    rows = []
    for choice in _workspace_choices(project_dir):
        root = Path(choice["path"])
        videos = read_json(root / "videos.json", []) or []
        downloaded = sum(1 for v in videos if v.get("download_status") == "downloaded")
        analysis_files = list((root / "analysis").glob("*.json")) if (root / "analysis").exists() else []
        complete = 0
        for path in analysis_files:
            try:
                if (read_json(path, {}) or {}).get("status") == "complete":
                    complete += 1
            except Exception:
                pass
        plans = read_json(root / "ads" / "ad_plans.json", []) or []
        rendered = sum(1 for p in plans if p.get("output_path") and Path(str(p["output_path"])).exists())
        status = "READY" if rendered else "ANALYZED" if complete else "DOWNLOADED" if downloaded else "DISCOVERED" if videos else "NEW"
        rows.append({**choice, "discovered": len(videos), "downloaded": downloaded, "analyzed": complete, "ads": rendered, "status": status})
    return rows


def build_dashboard_state(product, workspace: ProductWorkspace, *, project_dir: Path | None = None, job: dict | None = None, login_active: bool = False) -> dict:
    plan = workspace.load_search_plan()
    candidates = workspace.load_candidates()
    review_state = load_review_state(workspace)
    rights_state = load_rights_state(workspace)
    source_quality = load_source_quality(workspace)
    settings = load_studio_settings(workspace)
    quality_reviews = load_quality_reviews(workspace)
    analyses = _analysis_rows(workspace, review_state)
    ads = _ad_rows(workspace, review_state)

    rights_sources = rights_state.get("sources") or {}
    quality_sources = source_quality.get("sources") or {}
    candidate_rows = []
    for candidate in candidates:
        row = asdict(candidate)
        row["combined_score"] = round(_safe_float(row.get("combined_score")), 4)
        row["text_score"] = round(_safe_float(row.get("text_score")), 4)
        if row.get("visual_score") is not None:
            row["visual_score"] = round(_safe_float(row.get("visual_score")), 4)
        row["media_url"] = _relative_media_url(workspace, row.get("local_path"))
        row["poster_url"] = poster_url(workspace.root, row.get("video_id"), row.get("local_path"))
        row["rights"] = dict(rights_sources.get(row.get("video_id")) or {"status": "unknown"})
        row["source_quality"] = dict(quality_sources.get(row.get("video_id")) or {})
        candidate_rows.append(row)

    completed = [row for row in analyses if row["status"] == "complete"]
    failed = [row for row in analyses if row["status"] == "failed"]
    downloaded = [row for row in candidate_rows if row.get("download_status") == "downloaded"]

    all_segments = []
    role_counts = {role: 0 for role in ROLE_ORDER}
    for analysis in completed:
        source_rights = dict(rights_sources.get(analysis["video_id"]) or {"status": "unknown"})
        source_q = dict(quality_sources.get(analysis["video_id"]) or {})
        for segment in analysis["segments"]:
            role = str(segment.get("role") or "").lower()
            role_counts[role] = role_counts.get(role, 0) + 1
            all_segments.append(
                {
                    **segment,
                    "video_id": analysis["video_id"],
                    "creator": analysis["creator"],
                    "tiktok_url": analysis["tiktok_url"],
                    "media_url": analysis["media_url"],
                    "video_relevance_score": analysis["video_relevance_score"],
                    "source_duration_seconds": analysis.get("duration_seconds", 0.0),
                    "rights": source_rights,
                    "source_quality": source_q,
                }
            )
    all_segments.sort(
        key=lambda x: (
            0 if x.get("review", {}).get("favorite") else 1,
            0 if x.get("review", {}).get("status") == "approved" else 1,
            -_safe_float(x.get("product_match_score")),
            -_safe_float(x.get("score")),
            x.get("role") or "",
        )
    )

    candidate_by_id = {str(row.get("video_id") or ""): row for row in candidate_rows}
    analysis_by_id = {str(row.get("video_id") or ""): row for row in analyses}
    for candidate in candidate_rows:
        analysis = analysis_by_id.get(str(candidate.get("video_id") or "")) or {}
        candidate["duration_seconds"] = round(_safe_float(analysis.get("duration_seconds")), 3)
    for ad in ads:
        reviewed = quality_reviews.get(str(ad.get("name") or ""))
        if reviewed:
            ad["quality"] = reviewed
            ad["quality_source"] = reviewed.get("provider", "openai")
        else:
            ad["quality"] = heuristic_quality(ad)
            ad["quality_source"] = "heuristic"
        statuses = [str(s.get("rights_status") or "unknown") for s in ad.get("segments") or []]
        ad["rights_cleared"] = bool(statuses) and all(status in {"licensed", "approved_for_ads"} for status in statuses)

        # Enrich every ad cut with the exact source media used to create it.
        # This powers the workflow UI's ad provenance viewer without changing
        # the persisted ad plan format.
        enriched_segments = []
        for segment in ad.get("segments") or []:
            cut = dict(segment)
            video_id = str(cut.get("video_id") or "")
            candidate = candidate_by_id.get(video_id) or {}
            analysis = analysis_by_id.get(video_id) or {}
            cut["source_media_url"] = analysis.get("media_url") or candidate.get("media_url")
            cut["source_thumbnail_url"] = candidate.get("thumbnail_url") or ""
            cut["source_poster_url"] = candidate.get("poster_url") or ""
            cut["source_tiktok_url"] = cut.get("tiktok_url") or analysis.get("tiktok_url") or candidate.get("url") or ""
            cut["source_creator"] = cut.get("creator") or analysis.get("creator") or candidate.get("creator") or ""
            cut["source_caption"] = candidate.get("caption") or ""
            cut["source_download_status"] = candidate.get("download_status") or ""
            cut["source_rank_score"] = round(_safe_float(candidate.get("combined_score")), 4)
            cut["source_duration_seconds"] = round(_safe_float(analysis.get("duration_seconds")), 3)

            audio_segment_id = str(cut.get("audio_segment_id") or "")
            audio_video_id = audio_segment_id.split(":", 1)[0] if ":" in audio_segment_id else ""
            if audio_video_id and audio_video_id != video_id:
                audio_candidate = candidate_by_id.get(audio_video_id) or {}
                audio_analysis = analysis_by_id.get(audio_video_id) or {}
                cut["audio_source_video_id"] = audio_video_id
                cut["audio_source_media_url"] = audio_analysis.get("media_url") or audio_candidate.get("media_url")
                cut["audio_source_thumbnail_url"] = audio_candidate.get("thumbnail_url") or ""
                cut["audio_source_tiktok_url"] = cut.get("audio_tiktok_url") or audio_analysis.get("tiktok_url") or audio_candidate.get("url") or ""
                cut["audio_source_creator"] = cut.get("audio_creator") or audio_analysis.get("creator") or audio_candidate.get("creator") or ""
                cut["audio_source_caption"] = audio_candidate.get("caption") or ""
            enriched_segments.append(cut)
        ad["segments"] = enriched_segments
        source_ids = []
        for segment in enriched_segments:
            for key in ("video_id", "audio_source_video_id"):
                value = str(segment.get(key) or "")
                if value and value not in source_ids:
                    source_ids.append(value)
        ad["source_video_ids"] = source_ids
        ad["source_count"] = len(source_ids)

    # Build a lightweight non-destructive version graph for the editor.  No
    # plan is overwritten; previous renders stay available to reopen or restore.
    ad_by_name = {str(row.get("name") or ""): row for row in ads}

    def resolve_root(name: str) -> str:
        current = str(name or "")
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            row = ad_by_name.get(current) or {}
            explicit = str(row.get("root_ad") or "")
            if explicit:
                return explicit
            parent_name = str(row.get("version_parent") or row.get("parent_ad") or row.get("edited_from") or "")
            if not parent_name or parent_name not in ad_by_name:
                return current
            current = parent_name
        return str(name or "")

    roots = {name: resolve_root(name) for name in ad_by_name}
    family_rows: dict[str, list[dict]] = {}
    for name, row in ad_by_name.items():
        root_name = roots.get(name) or name
        family_rows.setdefault(root_name, []).append({
            "name": name,
            "strategy": row.get("strategy") or row.get("template") or "ad",
            "parent_ad": row.get("version_parent") or row.get("parent_ad") or "",
            "edit_label": row.get("edit_label") or "",
            "restored_from": row.get("restored_from") or "",
            "duration_seconds": round(_safe_float(row.get("duration_seconds")), 3),
            "created_at": row.get("created_at") or "",
            "media_url": row.get("media_url"),
            "exists": bool(row.get("exists")),
            "ai_summary": (row.get("ai_iteration") or {}).get("summary") or "",
        })
    for root_name, versions in family_rows.items():
        versions.sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("name") or "")))
    for name, row in ad_by_name.items():
        root_name = roots.get(name) or name
        parent_name = str(row.get("version_parent") or row.get("parent_ad") or "")
        depth = 0
        cursor = parent_name
        seen: set[str] = set()
        while cursor and cursor not in seen and cursor in ad_by_name:
            seen.add(cursor)
            depth += 1
            cursor = str(ad_by_name[cursor].get("version_parent") or ad_by_name[cursor].get("parent_ad") or "")
        row["version_root"] = root_name
        row["version_depth"] = depth
        row["version_family"] = family_rows.get(root_name, [])
        row["version_count"] = len(row["version_family"])

    rendered_ads = [row for row in ads if row.get("exists")]
    product_image_url = _relative_media_url(workspace, product.primary_image_path)
    if not product_image_url:
        for candidate in [workspace.root / "product.jpg", workspace.root / "product.webp", workspace.root / "product.png"]:
            if candidate.exists():
                product_image_url = _relative_media_url(workspace, str(candidate))
                break

    review_summary = {
        "segments_approved": sum(1 for r in (review_state.get("segments") or {}).values() if r.get("status") == "approved"),
        "segments_rejected": sum(1 for r in (review_state.get("segments") or {}).values() if r.get("status") == "rejected"),
        "segments_favorite": sum(1 for r in (review_state.get("segments") or {}).values() if r.get("favorite")),
        "ads_approved": sum(1 for r in (review_state.get("ads") or {}).values() if r.get("status") == "approved"),
        "ads_rejected": sum(1 for r in (review_state.get("ads") or {}).values() if r.get("status") == "rejected"),
        "ads_favorite": sum(1 for r in (review_state.get("ads") or {}).values() if r.get("favorite")),
    }
    cleared_sources = sum(1 for r in rights_sources.values() if str(r.get("status")) in {"licensed", "approved_for_ads"})
    quality_scores = [int(a.get("quality", {}).get("overall", 0) or 0) for a in rendered_ads]
    return {
        "product": {**product.to_dict(), "image_url": product_image_url},
        "workspace": str(workspace.root),
        "workspaces": _workspace_choices(project_dir) if project_dir else [],
        "projects": _project_summaries(project_dir) if project_dir else [],
        "search_plan": plan.to_dict(),
        "settings": settings,
        "rights_statuses": list(RIGHTS_STATUSES),
        "environment": {
            "openai_key_configured": bool(os.getenv("OPENAI_API_KEY")),
            "login_active": bool(login_active),
        },
        "stats": {
            "discovered": len(candidate_rows),
            "downloaded": len(downloaded),
            "analysis_total": len(analyses),
            "analysis_complete": len(completed),
            "analysis_failed": len(failed),
            "segments": len(all_segments),
            "ad_plans": len(ads),
            "ads_rendered": len(rendered_ads),
            "quality_reviewed": sum(1 for a in ads if str(a.get("name") or "") in quality_reviews),
            "quality_average": round(sum(quality_scores) / len(quality_scores)) if quality_scores else 0,
            "rights_cleared_sources": cleared_sources,
            "duplicate_sources": sum(1 for q in quality_sources.values() if q.get("duplicate_of")),
        },
        "review_summary": review_summary,
        "role_counts": role_counts,
        "candidates": candidate_rows,
        "analyses": analyses,
        "segments": all_segments,
        "ads": ads,
        "job": job or {"status": "idle", "logs": []},
    }


def _archive_ads(workspace: ProductWorkspace) -> Path | None:
    current = [p for p in workspace.ads_dir.glob("*.mp4") if p.is_file()]
    plans = workspace.ads_dir / "ad_plans.json"
    review = workspace.root / "review.json"
    if not current and not plans.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = workspace.ads_dir / "archive" / stamp
    archive.mkdir(parents=True, exist_ok=True)
    if plans.exists():
        shutil.copy2(plans, archive / plans.name)
    if review.exists():
        shutil.copy2(review, archive / review.name)
    for path in current:
        shutil.copy2(path, archive / path.name)
    quality = workspace.ads_dir / "quality"
    if quality.exists():
        shutil.copytree(quality, archive / "quality", dirs_exist_ok=True)
    for extra in (workspace.root / "studio_settings.json", workspace.root / "rights.json", workspace.root / "source_quality.json"):
        if extra.exists():
            shutil.copy2(extra, archive / extra.name)
    return archive


class _JobLog:
    def __init__(self, context: "DashboardContext"):
        self.context = context
        self.buffer = ""

    def write(self, text: str):
        try:
            sys.__stdout__.write(text)
            sys.__stdout__.flush()
        except Exception:
            pass
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.context.append_log(line)
        return len(text)

    def flush(self):
        if self.buffer.strip():
            self.context.append_log(self.buffer.strip())
            self.buffer = ""


class DashboardContext:
    def __init__(self, product, workspace: ProductWorkspace):
        self.lock = threading.RLock()
        self.product = product
        self.workspace = workspace
        self.project_dir = workspace.root.parent.parent.resolve()
        self.job = {"status": "idle", "action": "", "logs": [], "error": None}
        self.job_thread: threading.Thread | None = None
        self.login_thread: threading.Thread | None = None
        self.login_stop = threading.Event()
        self.login_driver = None

    def snapshot(self) -> tuple:
        with self.lock:
            return self.product, self.workspace

    def switch_workspace(self, path: str | Path):
        target = Path(path).expanduser().resolve()
        products = (self.project_dir / "products").resolve()
        target.relative_to(products)
        product, workspace = load_product_workspace(target)
        with self.lock:
            self.product, self.workspace = product, workspace
        return product, workspace

    def job_snapshot(self) -> dict:
        with self.lock:
            result = dict(self.job)
            result["logs"] = list(self.job.get("logs") or [])[-120:]
            return result

    def append_log(self, line: str):
        with self.lock:
            logs = self.job.setdefault("logs", [])
            logs.append(str(line))
            if len(logs) > 400:
                del logs[:-400]

    def start_job(self, action: str, fn):
        with self.lock:
            if self.job_thread and self.job_thread.is_alive():
                raise RuntimeError(f"A dashboard job is already running: {self.job.get('action')}")
            self.job = {
                "status": "running",
                "action": action,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "completed_at": None,
                "logs": [],
                "error": None,
            }

        def runner():
            logger = _JobLog(self)
            try:
                with contextlib.redirect_stdout(logger), contextlib.redirect_stderr(logger):
                    fn()
                logger.flush()
                with self.lock:
                    self.job["status"] = "complete"
                    self.job["completed_at"] = datetime.now().isoformat(timespec="seconds")
            except Exception as exc:
                logger.flush()
                self.append_log(f"ERROR: {type(exc).__name__}: {exc}")
                with self.lock:
                    self.job["status"] = "failed"
                    self.job["error"] = f"{type(exc).__name__}: {exc}"
                    self.job["completed_at"] = datetime.now().isoformat(timespec="seconds")

        thread = threading.Thread(target=runner, name=f"dashboard-{action}", daemon=True)
        self.job_thread = thread
        thread.start()
        return self.job_snapshot()

    def start_login(self):
        with self.lock:
            if self.login_thread and self.login_thread.is_alive():
                return
            self.login_stop.clear()

        def runner():
            _product, workspace = self.snapshot()
            pipeline = ProductPipeline(self.project_dir)
            driver = None
            try:
                driver = init_browser(pipeline.profile_dir, workspace.raw_dir, headless=False)
                with self.lock:
                    self.login_driver = driver
                driver.get("https://www.tiktok.com/")
                while not self.login_stop.wait(0.5):
                    try:
                        _ = driver.current_url
                    except Exception:
                        break
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                with self.lock:
                    self.login_driver = None

        self.login_thread = threading.Thread(target=runner, name="dashboard-tiktok-login", daemon=True)
        self.login_thread.start()

    def stop_login(self):
        self.login_stop.set()

    def login_active(self) -> bool:
        return bool(self.login_thread and self.login_thread.is_alive())


DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NextPlay · UGC Workflow</title>
<style>
:root{--bg:#080a0f;--panel:#10141c;--panel2:#141a23;--line:#252d3a;--text:#f4f7fb;--muted:#96a2b7;--green:#9df57b;--blue:#7db8ff;--purple:#c4a1ff;--yellow:#ffd06f;--red:#ff7d8c;--radius:15px}*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}a{color:inherit;text-decoration:none}.app{display:grid;grid-template-columns:244px minmax(0,1fr);min-height:100vh}.sidebar{position:sticky;top:0;height:100vh;padding:22px 16px;border-right:1px solid var(--line);background:#0b0e14;overflow:auto}.brand{display:flex;align-items:center;gap:9px;font-size:19px;font-weight:850}.brandDot{width:11px;height:11px;border-radius:50%;background:var(--green);box-shadow:0 0 18px #9df57b88}.badge{font-size:9px;letter-spacing:.08em;background:var(--green);color:#111;padding:3px 5px;border-radius:5px}.sideLabel{font-size:10px;text-transform:uppercase;letter-spacing:.15em;color:#657188;margin:22px 9px 8px}.workspaceSelect,.input,.select,textarea{width:100%;background:#10151e;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:9px 10px}.nav{width:100%;display:flex;align-items:center;gap:10px;border:0;background:transparent;color:#aab4c7;border-radius:10px;padding:10px 12px;margin:4px 0;text-align:left}.nav:hover,.nav.active{background:#171d27;color:white}.nav.active{box-shadow:inset 3px 0 0 var(--green)}.navDot{width:7px;height:7px;border-radius:50%;background:#556176}.nav.active .navDot{background:var(--green)}.main{min-width:0;padding:28px 32px 90px}.topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px}.eyebrow{font-size:10px;font-weight:850;letter-spacing:.15em;text-transform:uppercase;color:var(--green)}.title{font-size:31px;font-weight:880;line-height:1.08;margin-top:6px}.sub,.small{font-size:12px;color:var(--muted)}.topActions,.actions,.chips{display:flex;gap:7px;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#171d27;color:#fff;border-radius:9px;padding:8px 11px;display:inline-flex;align-items:center;justify-content:center;gap:6px}.btn:hover{border-color:#48566c}.btn.primary{background:#21351f;border-color:#42693a;color:#dcffd5}.btn.blue{background:#17253a;border-color:#31527d;color:#d2e7ff}.btn.purple{background:#261d35;border-color:#5d4280;color:#ead9ff}.btn.warn{background:#2a2315;border-color:#604e27;color:#ffe2a0}.btn.danger{background:#2d181c;border-color:#69313b;color:#ffbec6}.btn:disabled{opacity:.42;cursor:not-allowed}.view{display:none}.view.active{display:block}.card{background:linear-gradient(180deg,#131923,#0f141c);border:1px solid var(--line);border-radius:var(--radius);padding:15px}.section{margin-top:26px;scroll-margin-top:20px}.sectionHead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:11px}.sectionTitle{font-size:18px;font-weight:820}.metricLabel{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted)}.metric{font-size:27px;font-weight:880;margin-top:5px}.chip{font-size:10px;padding:5px 8px;border-radius:999px;border:1px solid #2a3548;background:#1b2330;color:#cbd5e6}.chip.good{background:#17271a;border-color:#315c38;color:#c5f7c9}.chip.blue{background:#17243a;border-color:#31527d;color:#d1e8ff}.chip.warn{background:#2b2416;border-color:#5f4e28;color:#ffe1a0}.chip.bad{background:#2d181c;border-color:#67323b;color:#ffbac2}.chip.star{background:#2c2517;border-color:#66552b;color:#ffe493}.role-hook{color:#b7ff98}.role-problem{color:#ff909d}.role-product_reveal,.role-demo{color:#8fd4ff}.role-benefit{color:#d7b1ff}.role-social_proof{color:#ffda83}.role-objection{color:#ffb181}.role-before_after{color:#8de8c7}.role-cta{color:#b7ff98}.hero{display:grid;grid-template-columns:132px minmax(0,1fr) auto;gap:18px;align-items:center}.productImg{width:132px;height:132px;object-fit:cover;border-radius:14px;background:#1a202b}.hero h2{margin:5px 0 7px;font-size:24px}.heroStats{display:grid;grid-template-columns:repeat(4,minmax(98px,1fr));gap:8px;margin-top:12px}.heroStat{padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:#0d1219}.heroStat b{font-size:18px}.heroStat span{display:block;font-size:9px;color:var(--muted);text-transform:uppercase;margin-top:3px}.workflowRail{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:14px 0 4px}.railStep{border:1px solid var(--line);background:#0f141c;border-radius:12px;padding:11px;text-align:left;color:#c8d1e1}.railStep.done{border-color:#355d39}.railStep strong{display:block;font-size:12px}.railStep span{font-size:10px;color:var(--muted)}.railNum{display:inline-flex;width:22px;height:22px;border-radius:50%;align-items:center;justify-content:center;background:#1b2330;margin-bottom:7px;font-size:10px}.railStep.done .railNum{background:#203b23;color:#caffbf}.gallery{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px}.videoCard{overflow:hidden;padding:0}.videoPoster{height:225px;background:#090c11;position:relative;display:flex;align-items:center;justify-content:center}.videoPoster img{width:100%;height:100%;object-fit:cover}.posterFallback{font-size:12px;color:#667389;text-align:center;padding:20px}.playFloat{position:absolute;left:10px;bottom:10px;border:0;background:#0b0e14dc;color:white;border-radius:999px;padding:7px 10px;font-size:11px}.videoBody{padding:11px}.creator{font-weight:800;font-size:12px}.caption{font-size:11px;color:#aab5c7;line-height:1.35;margin-top:5px;min-height:30px}.videoMeta{display:flex;justify-content:space-between;gap:7px;margin-top:9px;color:#7f8ba0;font-size:10px}.clipGrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.clipCard{position:relative}.clipRole{font-size:11px;font-weight:850;text-transform:uppercase}.clipText{font-size:12px;line-height:1.38;margin:7px 0;color:#d1d8e4}.clipTime{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;color:#9ba7ba}.buildGrid{display:grid;grid-template-columns:1.1fr .9fr;gap:12px}.recipe{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin:12px 0}.recipeItem{padding:10px;border:1px solid var(--line);border-radius:10px;background:#0d1219}.recipeItem strong{display:block;font-size:11px}.recipeItem span{font-size:9px;color:var(--muted)}.adGrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.adCard video.finalVideo{width:100%;background:#000;border:1px solid #1e2530;border-radius:11px;max-height:510px}.adTitle{font-size:16px;font-weight:850;margin:10px 0 3px}.adMeta{font-size:11px;color:var(--muted)}.timeline{display:flex;gap:4px;margin:10px 0}.timelinePiece{height:5px;min-width:7px;border-radius:999px;background:#53617a;flex:1}.sourceStrip{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:10px}.sourceCut{display:grid;grid-template-columns:56px 1fr;gap:8px;padding:7px;border:1px solid #232b38;border-radius:10px;background:#0c1118}.sourceThumb{width:56px;height:76px;border-radius:7px;object-fit:cover;background:#171d27}.sourceCutTitle{font-size:10px;font-weight:820}.sourceCutMeta{font-size:9px;color:var(--muted);margin:3px 0 5px}.sourceButtons{display:flex;gap:5px;flex-wrap:wrap}.sourceButtons .btn{font-size:9px;padding:5px 7px}.details{margin-top:9px}.details summary{cursor:pointer;color:#d5deeb;font-size:11px}.reviewRow{display:flex;gap:5px;flex-wrap:wrap;margin-top:9px}.reviewRow .btn{font-size:10px;padding:6px 8px}.controlsGrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.log{height:290px;overflow:auto;white-space:pre-wrap;background:#07090d;border:1px solid var(--line);border-radius:11px;padding:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;color:#b6c1d4;line-height:1.5}.settingsGrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.setting label{display:block;font-size:10px;color:var(--muted);margin-bottom:5px}.tableWrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;min-width:960px;background:#0e1219}th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #202633;font-size:11px;vertical-align:top}th{font-size:9px;color:#77849a;text-transform:uppercase;letter-spacing:.08em;background:#11161f}.miniThumb{width:48px;height:65px;object-fit:cover;border-radius:7px;background:#171d27}.empty{padding:24px;border:1px dashed #313a49;border-radius:12px;text-align:center;color:#7e899d}.modal{position:fixed;inset:0;background:#040609dd;display:none;align-items:center;justify-content:center;z-index:50;padding:24px}.modal.show{display:flex}.modalCard{width:min(920px,96vw);max-height:94vh;overflow:auto;background:#10151d;border:1px solid #344054;border-radius:16px;padding:14px}.modalTop{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:10px}.modal video{width:100%;max-height:72vh;background:#000;border-radius:11px}.tray{position:fixed;left:260px;right:20px;bottom:14px;display:none;align-items:center;gap:10px;background:#121821ee;border:1px solid #364255;border-radius:13px;padding:9px 12px;backdrop-filter:blur(12px);z-index:30}.tray.show{display:flex}.trayItems{display:flex;gap:5px;overflow:auto;flex:1}.trayItem{font-size:9px;padding:5px 7px;border:1px solid var(--line);border-radius:7px;background:#0a0e14;white-space:nowrap}.notice{border-left:3px solid var(--yellow);padding:9px 11px;background:#211c12;color:#e7d5aa;border-radius:8px;font-size:11px}.workflowJump{scroll-margin-top:18px}.editorShell{width:min(1480px,98vw);height:min(94vh,980px);display:flex;flex-direction:column;background:#0d1219;border:1px solid #344054;border-radius:18px;overflow:hidden}.editorTop{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding:15px 17px;border-bottom:1px solid var(--line);background:#111720}.editorBody{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(330px,.75fr);gap:12px;padding:12px;overflow:auto}.editorPreview video{width:100%;max-height:390px;background:#000;border-radius:12px}.editorTimeline{display:flex;flex-direction:column;gap:7px;margin-top:10px}.editClip{display:grid;grid-template-columns:28px minmax(0,1fr) auto;gap:8px;align-items:center;padding:9px;border:1px solid #2a3342;border-radius:11px;background:#101722}.editClip.selected{border-color:#79b8ff;box-shadow:0 0 0 1px #79b8ff33 inset}.editClip.dragging{opacity:.45}.dragHandle{font-size:17px;color:#6e7b91;cursor:grab;text-align:center}.editClipTitle{font-size:11px;font-weight:850}.editClipMeta{font-size:9px;color:var(--muted);margin-top:3px}.editClipActions{display:flex;gap:4px}.editClipActions .btn{font-size:9px;padding:5px 6px}.editorSide{display:flex;flex-direction:column;gap:10px}.editorPanel{padding:12px;border:1px solid var(--line);background:#0c1118;border-radius:12px}.editorPanel h3{font-size:13px;margin:0 0 9px}.trimGrid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.trimBox label{display:block;font-size:9px;color:var(--muted);margin-bottom:4px}.nudgeRow{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:5px}.nudgeRow .btn{font-size:9px;padding:5px 4px}.editorFooter{border-top:1px solid var(--line);padding:12px 14px;background:#0b1017}.libraryTabs{display:flex;gap:6px;margin-bottom:9px}.libraryGrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;max-height:285px;overflow:auto}.libraryCard{border:1px solid #283141;background:#0e151f;border-radius:10px;padding:9px}.libraryTitle{font-size:10px;font-weight:850}.libraryMeta{font-size:9px;color:var(--muted);margin:4px 0 7px}.libraryText{font-size:10px;color:#c3ccda;line-height:1.3;min-height:28px}.editorStatus{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}.editorDuration{font-size:12px;font-weight:850}.aiPrompt{min-height:64px;resize:vertical}.versionChip{background:#1b2031;border-color:#3d4761;color:#dce4ff}@media(max-width:1180px){.editorBody{grid-template-columns:1fr}.libraryGrid{grid-template-columns:repeat(3,1fr)}.gallery{grid-template-columns:repeat(3,1fr)}.clipGrid{grid-template-columns:repeat(2,1fr)}.hero{grid-template-columns:110px 1fr}.hero .heroAction{grid-column:1/-1}.heroStats{grid-template-columns:repeat(4,1fr)}.adGrid{grid-template-columns:1fr}}@media(max-width:850px){.editorShell{width:100vw;height:100vh;border-radius:0}.libraryGrid{grid-template-columns:1fr 1fr}.app{grid-template-columns:1fr}.sidebar{height:auto;position:relative;border-right:0;border-bottom:1px solid var(--line)}.main{padding:20px 14px 90px}.gallery{grid-template-columns:repeat(2,1fr)}.workflowRail{grid-template-columns:1fr}.hero{grid-template-columns:1fr}.productImg{width:100%;height:210px}.heroStats{grid-template-columns:repeat(2,1fr)}.buildGrid,.controlsGrid,.settingsGrid{grid-template-columns:1fr}.tray{left:12px;right:12px}.sourceStrip{grid-template-columns:1fr}}

.editorToolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin:10px 0}.editorToolbarGroup{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.editorZoom{display:flex;align-items:center;gap:7px;padding:6px 8px;border:1px solid var(--line);border-radius:10px;background:#0b1017}.editorZoom input[type=range]{width:150px}.timelineViewport{overflow-x:auto;overflow-y:hidden;border:1px solid var(--line);border-radius:12px;background:#090d13;padding:8px 8px 10px}.timelineRuler{position:relative;height:23px;border-bottom:1px solid #27303d;margin-bottom:7px;color:#708096;font-size:8px;min-width:100%}.rulerTick{position:absolute;top:0;height:100%;border-left:1px solid #27303d;padding-left:3px;white-space:nowrap}.editorTrack{display:flex;align-items:stretch;gap:5px;min-height:86px;min-width:100%;transition:width .12s ease}.editClip.timelineBlock{min-width:58px;margin:0;align-items:stretch;grid-template-columns:18px minmax(0,1fr) auto;position:relative;overflow:hidden}.editClip.timelineBlock .clipFill{position:absolute;inset:0;background:linear-gradient(90deg,#1c2635,#151d28);opacity:.75;pointer-events:none}.editClip.timelineBlock.selected .clipFill{background:linear-gradient(90deg,#253d2a,#1c2b21)}.editClip.timelineBlock>*{position:relative;z-index:1}.frameBadge{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:9px;color:#9fb1c7}.frameButtons{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px;margin-top:6px}.frameButtons .btn{font-size:9px;padding:5px 6px}.trimSlider{width:100%;accent-color:#9df57b}.historyStrip{display:flex;gap:7px;overflow:auto;padding-bottom:4px}.historyCard{min-width:190px;max-width:240px;border:1px solid var(--line);border-radius:10px;padding:9px;background:#0b1017}.historyCard.current{border-color:#5c8f50;box-shadow:inset 0 0 0 1px #314b2c}.historyTitle{font-size:10px;font-weight:850}.historyMeta{font-size:9px;color:var(--muted);margin:4px 0}.aiChat{display:flex;flex-direction:column;gap:7px;max-height:260px;overflow:auto;padding:8px;border:1px solid var(--line);border-radius:10px;background:#090d13;margin:8px 0}.aiBubble{max-width:92%;padding:8px 9px;border-radius:10px;font-size:11px;line-height:1.4}.aiBubble.assistant{align-self:flex-start;background:#182031;color:#d9e6f8}.aiBubble.user{align-self:flex-end;background:#20351f;color:#e3ffdc}.aiReady{border-left:3px solid var(--green);padding:7px 9px;background:#17251a;border-radius:8px;font-size:10px;color:#d4f8cd}.editorHint{font-size:9px;color:#75839a;margin-top:5px}.editorPreviewRow{display:grid;grid-template-columns:minmax(0,1fr) 210px;gap:10px;align-items:start}.editorPreviewInfo{border:1px solid var(--line);border-radius:10px;padding:10px;background:#0b1017}.kbd{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid #3a4555;background:#171d27;border-radius:5px;padding:1px 5px;font-size:9px}.dirtyDot{width:7px;height:7px;border-radius:50%;background:var(--yellow);display:inline-block}.editorVersionBar{border:1px solid var(--line);border-radius:12px;padding:10px;background:#0c1118;margin-top:10px}.aiPromptRow{display:grid;grid-template-columns:1fr auto;gap:7px}.editorTop .btn.compact{padding:6px 9px;font-size:10px}@media(max-width:980px){.editorPreviewRow{grid-template-columns:1fr}.editorBody{grid-template-columns:1fr}.editorSide{border-left:0;border-top:1px solid var(--line)}.frameButtons{grid-template-columns:repeat(2,1fr)}}

/* V3.7 native timeline editor overrides */
.editorShell{width:min(1560px,99vw);height:min(95vh,1040px);background:#0a0e14;border-color:#2d3747}.editorTop{padding:12px 16px;background:#10151d}.editorBody{display:flex;flex-direction:column;gap:10px;padding:10px;overflow:auto;min-height:0}.nativeEditorStage{display:grid;grid-template-columns:250px minmax(440px,1fr) 310px;gap:10px;min-height:365px}.nativePanel{border:1px solid var(--line);border-radius:12px;background:#0d1219;min-width:0;overflow:hidden}.nativePanelHead{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 10px;border-bottom:1px solid var(--line);background:#111720}.nativePanelTitle{font-size:11px;font-weight:850}.nativeMediaBin{display:flex;flex-direction:column;min-height:0}.nativeMediaTabs{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;padding:7px;border-bottom:1px solid var(--line)}.nativeMediaTabs .btn{font-size:9px;padding:6px 5px}.nativeMediaList{padding:7px;overflow:auto;max-height:315px}.nativeMediaItem{border:1px solid #26303e;border-radius:9px;background:#101722;padding:8px;margin-bottom:6px;cursor:grab}.nativeMediaItem:active{cursor:grabbing}.nativeMediaItem.dragging{opacity:.5}.nativeMediaItem .libraryTitle{font-size:10px}.nativeMediaItem .actions{margin-top:6px}.nativeMediaItem .btn{font-size:9px;padding:5px 7px}.nativePreviewPane{display:flex;flex-direction:column;min-width:0}.nativePreviewWrap{display:flex;align-items:center;justify-content:center;background:#020304;min-height:285px;padding:8px;position:relative}.nativePreviewWrap video{width:100%;height:320px;max-height:42vh;object-fit:contain;background:#000;border:0;border-radius:8px}.previewStatus{position:absolute;left:14px;top:14px;display:flex;gap:6px;align-items:center;background:#080b10dd;border:1px solid #2a3340;border-radius:8px;padding:5px 7px;font-size:9px;color:#b8c4d5}.nativeTransport{display:flex;align-items:center;justify-content:center;gap:7px;padding:7px 9px;border-top:1px solid var(--line);background:#0b1017}.transportTime{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;color:#cbd5e3;min-width:92px;text-align:center}.nativeInspector{display:flex;flex-direction:column;min-height:0}.nativeInspectorBody{padding:10px;overflow:auto;max-height:365px}.inspectorClipTitle{font-size:13px;font-weight:850;margin-bottom:3px}.inspectorSource{font-size:9px;color:var(--muted);margin-bottom:9px}.inspectorRow{display:grid;grid-template-columns:1fr 1fr;gap:7px}.inspectorGroup{margin-top:8px}.inspectorGroup label{display:block;font-size:9px;color:#7e8ba0;margin-bottom:4px}.timeField{display:grid;grid-template-columns:1fr auto;gap:5px;align-items:center}.timeField .input{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;padding:7px 8px}.frameNudges{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:5px}.frameNudges .btn{font-size:8px;padding:4px 3px}.nativeTimelineDock{border:1px solid var(--line);border-radius:12px;background:#090d13;overflow:hidden;min-height:250px}.nativeTimelineToolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 9px;border-bottom:1px solid var(--line);background:#111720}.nativeTimelineTools{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.nativeTimelineTools .btn{padding:6px 8px;font-size:9px}.nativeZoom{display:flex;align-items:center;gap:6px}.nativeZoom input[type=range]{width:130px}.nativeTimelineScroll{overflow:auto;padding:0 0 8px}.nativeRulerRow{display:grid;grid-template-columns:52px minmax(0,1fr);position:sticky;top:0;z-index:8;background:#0a0e14}.nativeTrackLabel{width:52px;display:flex;align-items:center;justify-content:center;border-right:1px solid #27303d;color:#6f7d91;font-size:9px;font-weight:850;letter-spacing:.08em;background:#0d1219;position:sticky;left:0;z-index:9}.nativeRuler{height:28px;position:relative;border-bottom:1px solid #27303d}.nativeRulerTick{position:absolute;top:0;height:100%;border-left:1px solid #2a3340;padding:4px 0 0 4px;font-size:8px;color:#728198;white-space:nowrap}.nativeTrackRow{display:grid;grid-template-columns:52px minmax(0,1fr);min-height:86px;border-bottom:1px solid #1e2631}.nativeTrackRow.audio{min-height:44px}.nativeTrackCanvas{position:relative;min-height:86px;background-image:linear-gradient(to right,#121923 1px,transparent 1px);background-size:70px 100%;user-select:none}.nativeTrackRow.audio .nativeTrackCanvas{min-height:44px;background-color:#090d13}.timelineClipNative{position:absolute;top:7px;height:70px;border:1px solid #40506a;border-radius:7px;background:#182333;overflow:hidden;cursor:grab;box-shadow:inset 0 -12px 22px #0003}.timelineClipNative:active{cursor:grabbing}.timelineClipNative.selected{outline:2px solid var(--green);outline-offset:1px;z-index:20}.timelineClipNative.dragging{opacity:.45}.timelineClipNative.trimming{z-index:24!important;cursor:ew-resize!important}.timelineClipNative.drop-before:before,.timelineClipNative.drop-after:after{content:"";position:absolute;top:-5px;bottom:-5px;width:3px;background:var(--green);box-shadow:0 0 10px #9df57baa;z-index:12;pointer-events:none}.timelineClipNative.drop-before:before{left:-3px}.timelineClipNative.drop-after:after{right:-3px}.timelineClipNative[data-role="hook"]{background:#20351f;border-color:#4e7b46}.timelineClipNative[data-role="demo"],.timelineClipNative[data-role="product_reveal"]{background:#172d3b;border-color:#3e6e89}.timelineClipNative[data-role="benefit"]{background:#2a2138;border-color:#654e80}.timelineClipNative[data-role="cta"]{background:#25351f;border-color:#5c7c48}.timelineClipNative[data-role="problem"]{background:#382027;border-color:#7b4551}.timelineClipNative[data-role="social_proof"]{background:#332b1b;border-color:#755f32}.timelineClipInner{height:100%;padding:8px 10px;pointer-events:none;display:flex;flex-direction:column;justify-content:space-between;min-width:0}.timelineClipRole{font-size:9px;font-weight:900;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.timelineClipCreator{font-size:9px;color:#c4cfdd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.timelineClipDur{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:8px;color:#8fa0b5}.timelineTrimHandle{position:absolute;top:0;bottom:0;width:16px;background:#dfffd82b;z-index:25;cursor:ew-resize;display:none;touch-action:none;user-select:none}.timelineClipNative.selected .timelineTrimHandle{display:block}.timelineTrimHandle:after{content:"";position:absolute;top:50%;left:50%;width:3px;height:24px;transform:translate(-50%,-50%);border-left:1px solid var(--green);border-right:1px solid var(--green);opacity:.9}.timelineTrimHandle.left{left:0;border-right:2px solid var(--green)}.timelineTrimHandle.right{right:0;border-left:2px solid var(--green)}.timelineDropZone{position:absolute;top:4px;bottom:4px;width:28px;margin-left:-14px;z-index:7}.timelineDropZone.over:after{content:"";position:absolute;left:6px;top:0;bottom:0;width:2px;background:var(--green);box-shadow:0 0 8px #9df57b88}.timelineGapNative{position:absolute;top:7px;height:70px;border:1px dashed #7a4d52;border-radius:6px;background:repeating-linear-gradient(135deg,#2b171a 0 8px,#1a1114 8px 16px);color:#ff9aa5;font-size:8px;font-weight:850;display:flex;align-items:center;justify-content:center;overflow:hidden;pointer-events:none;z-index:2}.audioClipNative{position:absolute;top:5px;height:32px;border-radius:5px;background:#222b38;border:1px solid #394557;overflow:hidden}.audioClipNative.muted{opacity:.35;background:#231c20}.audioClipNative:after{content:"";position:absolute;inset:8px 5px;background:repeating-linear-gradient(90deg,#6e7e9188 0 2px,transparent 2px 5px);opacity:.7}.timelinePlayhead{position:absolute;top:0;bottom:0;width:1px;background:#ff7d8c;z-index:10;pointer-events:none}.timelinePlayhead:before{content:"";position:absolute;top:-4px;left:-5px;width:11px;height:8px;background:#ff7d8c;border-radius:3px 3px 6px 6px}.nativeTimelineHint{padding:7px 9px;color:#728198;font-size:9px;border-top:1px solid #1e2631}.nativeVersionStrip{display:flex;gap:6px;overflow:auto;padding:7px 9px;border-top:1px solid var(--line);background:#0c1118}.nativeVersionCard{min-width:165px;border:1px solid #293341;border-radius:8px;padding:7px;background:#101722}.nativeVersionCard.current{border-color:#547a49}.nativeBottomBar{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 9px;border-top:1px solid var(--line);background:#10151d}.nativeBottomBar .actions{align-items:center}.nativeAi{border-top:1px solid var(--line);margin-top:10px;padding-top:9px}.nativeAi .aiChat{max-height:160px}.nativeEmptyInspector{padding:20px;text-align:center;color:#748196}.editorFooter{display:none!important}
@media(max-width:1220px){.nativeEditorStage{grid-template-columns:220px minmax(360px,1fr) 285px}.nativePreviewWrap video{height:280px}}
@media(max-width:980px){.nativeEditorStage{grid-template-columns:1fr}.nativeMediaBin{order:2}.nativePreviewPane{order:1}.nativeInspector{order:3}.nativeMediaList,.nativeInspectorBody{max-height:260px}.nativePreviewWrap video{height:300px}.nativeTimelineDock{min-height:230px}}

</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
  <div class="brand"><span class="brandDot"></span>NextPlay <span class="badge">UGC</span></div>
  <div class="sideLabel">Product workspace</div>
  <select id="workspaceSelect" class="workspaceSelect" onchange="switchWorkspace(this.value)"></select>
  <div class="sideLabel">Workflow</div>
  <button class="nav active" data-view="workflow"><span class="navDot"></span>Workflow</button>
  <button class="nav" data-view="sources"><span class="navDot"></span>Found videos</button>
  <button class="nav" data-view="clips"><span class="navDot"></span>AI clips</button>
  <button class="nav" data-view="ads"><span class="navDot"></span>Edited ads</button>
  <button class="nav" data-view="controls"><span class="navDot"></span>Controls & settings</button>
  <div class="sideLabel">Reminder</div>
  <div class="small">Source footage rights are not verified automatically. Clear creator usage rights before commercial publishing.</div>
</aside>
<main class="main">
  <div class="topbar"><div><div class="eyebrow">NEXTPLAY CREATIVE WORKFLOW</div><div id="pageTitle" class="title">Loading…</div><div id="pageSub" class="sub"></div></div><div class="topActions"><button class="btn" onclick="loadState()">↻ Refresh</button></div></div>
  <section id="view-workflow" class="view active"><div id="workflow"></div></section>
  <section id="view-sources" class="view"><div id="sources"></div></section>
  <section id="view-clips" class="view"><div id="clips"></div></section>
  <section id="view-ads" class="view"><div id="ads"></div></section>
  <section id="view-controls" class="view"><div id="controls"></div></section>
</main>
</div>
<div id="videoModal" class="modal" onclick="if(event.target===this)closeVideoModal()"><div class="modalCard"><div class="modalTop"><div><div id="modalTitle" style="font-weight:850"></div><div id="modalMeta" class="small"></div></div><button class="btn" onclick="closeVideoModal()">Close</button></div><video id="modalPlayer" controls playsinline></video><div id="modalCaption" class="small" style="margin-top:9px"></div></div></div>
<div id="editorModal" class="modal" onclick="if(event.target===this)closeAdEditor()"><div class="editorShell"><div class="editorTop"><div><div class="eyebrow">NEXTPLAY AD EDITOR · TIMELINE</div><div id="editorTitle" style="font-size:20px;font-weight:880;margin-top:4px">Edit ad</div><div id="editorSub" class="small"></div></div><div class="actions"><button class="btn" onclick="closeAdEditor()">Close</button></div></div><div id="editorContent" class="editorBody"></div></div></div>
<div id="customTray" class="tray"><b style="font-size:11px">Custom ad</b><div id="trayItems" class="trayItems"></div><button class="btn" onclick="clearTray()">Clear</button><button class="btn primary" onclick="renderCustomAd()">Render selected clips</button></div>
<script>
let STATE=null, lastJobStatus='idle', CUSTOM=[], EDITOR=null, EDITOR_DRAG=null, EDITOR_LIBRARY='ai';
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=v=>{const n=Number(v||0);return n.toFixed(n<10?2:1)+'s'};
const pct=v=>Math.round(Number(v||0)*100);
const roleName=v=>String(v||'segment').replaceAll('_',' ');
function currentView(){return document.querySelector('.nav.active')?.dataset.view||'workflow'}
function openView(name){document.querySelectorAll('.nav').forEach(x=>x.classList.toggle('active',x.dataset.view===name));document.querySelectorAll('.view').forEach(x=>x.classList.toggle('active',x.id==='view-'+name));if(name==='workflow')renderWorkflow();if(name==='sources')renderSources();if(name==='clips')renderClips();if(name==='ads')renderAds();if(name==='controls')renderControls();window.scrollTo({top:0,behavior:'smooth'})}
document.querySelectorAll('.nav').forEach(b=>b.addEventListener('click',()=>openView(b.dataset.view)));
async function api(path,payload={}){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const j=await r.json();if(!r.ok)throw new Error(j.error||'Request failed');return j}
function openTimelineEditor(name){window.open('/timeline?name='+encodeURIComponent(name),'_blank')}
async function loadState(){const r=await fetch('/api/state',{cache:'no-store'});STATE=await r.json();if(STATE.error)throw new Error(STATE.error);lastJobStatus=STATE.job.status;document.getElementById('pageTitle').textContent=STATE.product.title||'Product workspace';document.getElementById('pageSub').textContent=`${STATE.product.brand||'Unknown brand'} · ${STATE.stats.discovered} found · ${STATE.stats.downloaded} downloaded · ${STATE.stats.analysis_complete} analyzed · ${STATE.stats.ads_rendered} edited ads`;renderAll()}
function renderAll(){renderWorkspaceSelect();renderWorkflow();renderSources();renderClips();renderAds();renderControls()}
function renderWorkspaceSelect(){const e=document.getElementById('workspaceSelect');e.innerHTML=(STATE.workspaces||[]).map(w=>`<option value="${esc(w.path)}" ${w.path===STATE.workspace?'selected':''}>${esc((w.brand?w.brand+' · ':'')+w.title)}</option>`).join('')}
async function switchWorkspace(path){try{await api('/api/workspace/switch',{path});await loadState();openView('workflow')}catch(e){alert(e.message)}}
function jump(id){openView('workflow');setTimeout(()=>document.getElementById(id)?.scrollIntoView({behavior:'smooth',block:'start'}),40)}
function stageCard(num,title,count,done,id){return `<button class="railStep ${done?'done':''}" onclick="jump('${id}')"><span class="railNum">${done?'✓':num}</span><strong>${esc(title)}</strong><span>${esc(count)}</span></button>`}
function sourcePoster(v,click=true){const url=v.poster_url||v.thumbnail_url;const fallback=v.media_url?'Preview unavailable · Watch to play':'Preview unavailable';const img=url?`<img loading="lazy" alt="Video preview" src="${esc(url)}" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><div class="posterFallback" hidden>${fallback}</div>`:`<div class="posterFallback">${fallback}</div>`;return `<div class="videoPoster">${img}${v.media_url&&click?`<button class="playFloat" onclick="openSourceById('${esc(v.video_id)}')">▶ Play</button>`:''}</div>`}
function sourceCard(v){const rights=(v.rights?.status||'unknown').replaceAll('_',' ');return `<div class="card videoCard">${sourcePoster(v)}<div class="videoBody"><div class="creator">@${esc(v.creator||'unknown')}</div><div class="caption">${esc((v.caption||'No caption captured').slice(0,120))}</div><div class="chips" style="margin-top:8px"><span class="chip blue">score ${Number(v.combined_score||0).toFixed(3)}</span><span class="chip ${v.download_status==='downloaded'?'good':''}">${esc(v.download_status||'found')}</span><span class="chip ${['licensed','approved_for_ads'].includes(v.rights?.status)?'good':'warn'}">${esc(rights)}</span></div><div class="actions" style="margin-top:8px">${v.media_url?`<button class="btn primary" onclick="openSourceById('${esc(v.video_id)}')">Watch</button>`:''}${v.url?`<a class="btn" target="_blank" href="${esc(v.url)}">TikTok ↗</a>`:''}</div></div></div>`}
function clipCard(x,compact=false){const r=x.review||{};return `<div class="card clipCard"><div class="clipRole role-${esc(x.role)}">${esc(roleName(x.role))}</div><div class="clipTime">${fmt(x.start)} → ${fmt(x.end)} · @${esc(x.creator||'unknown')}</div><div class="clipText">${esc((x.caption_text||x.text||x.reason||'').slice(0,compact?120:220))}</div><div class="chips"><span class="chip">AI ${pct(x.score)}%</span><span class="chip blue">product ${pct(x.product_match_score)}%</span>${r.status?`<span class="chip ${r.status==='approved'?'good':'bad'}">${esc(r.status)}</span>`:''}${r.favorite?'<span class="chip star">★ favorite</span>':''}</div><div class="reviewRow">${x.media_url?`<button class="btn blue" onclick="openClipById('${esc(x.segment_id)}')">▶ Exact clip</button>`:''}<button class="btn primary" onclick="setReview('segments','${esc(x.segment_id)}','approved')">✓</button><button class="btn danger" onclick="setReview('segments','${esc(x.segment_id)}','rejected')">✕</button><button class="btn warn" onclick="setReview('segments','${esc(x.segment_id)}',null,${!r.favorite})">★</button><button class="btn" onclick="addCustom('${esc(x.segment_id)}')">+ custom</button></div></div>`}
function sourceCutHtml(s,adName,index){const thumb=(s.source_poster_url||s.source_thumbnail_url)?`<img class="sourceThumb" loading="lazy" alt="Source preview" src="${esc(s.source_poster_url||s.source_thumbnail_url)}" onerror="this.hidden=true">`:`<div class="sourceThumb" style="display:flex;align-items:center;justify-content:center;font-size:9px;color:#6d798d">${esc(roleName(s.role))}</div>`;const audio=s.audio_source_video_id?`<div class="chips" style="margin:4px 0"><span class="chip warn">voiceover from @${esc(s.audio_source_creator||'unknown')}</span></div><div class="sourceButtons">${s.audio_source_media_url?`<button class="btn purple" onclick="openAdAudioCut('${esc(adName)}',${index})">Watch voiceover source</button>`:''}${s.audio_source_tiktok_url?`<a class="btn" target="_blank" href="${esc(s.audio_source_tiktok_url)}">Voiceover TikTok ↗</a>`:''}</div>`:'';return `<div class="sourceCut">${thumb}<div><div class="sourceCutTitle role-${esc(s.role)}">${esc(roleName(s.role))}</div><div class="sourceCutMeta">@${esc(s.source_creator||s.creator||'unknown')} · ${fmt(s.start)}–${fmt(s.end)}</div><div class="sourceButtons">${s.source_media_url?`<button class="btn blue" onclick="openAdCut('${esc(adName)}',${index})">Watch cut</button>`:''}${s.source_tiktok_url?`<a class="btn" target="_blank" href="${esc(s.source_tiktok_url)}">Original ↗</a>`:''}</div>${audio}</div></div>`}
function adCard(a,compact=false){const r=a.review||{};const segs=a.segments||[];const exists=a.media_url&&a.exists;const version=a.strategy==='manual_edit'?`<span class="chip versionChip">manual edit of ${esc(a.parent_ad||'')}</span>`:a.strategy==='ai_iteration'?`<span class="chip purple">AI iteration of ${esc(a.parent_ad||'')}</span>`:a.strategy==='ai_regenerate'?`<span class="chip purple">AI regenerate of ${esc(a.parent_ad||'')}</span>`:'';const aiSummary=a.ai_iteration?.summary?`<div class="small" style="margin-top:7px">${esc(a.ai_iteration.summary)}</div>`:'';return `<div class="card adCard">${exists?`<video class="finalVideo" controls playsinline preload="metadata" src="${esc(a.media_url)}"></video>`:`<div class="empty">Ad plan exists but MP4 is not rendered.</div>`}<div class="adTitle">${esc(a.name)}</div><div class="adMeta">${esc(a.strategy||a.template||'ad')} · ${fmt(a.duration_seconds)} · ${segs.length} cuts · ${a.source_count||0} source videos</div><div class="timeline">${segs.map(()=>'<span class="timelinePiece"></span>').join('')}</div><div class="chips">${a.experiment_id?`<span class="chip blue">A/B ${esc(a.variant||'variant')}</span>`:''}${a.parent_ad&&a.strategy==='quality_repair'?`<span class="chip warn">repair of ${esc(a.parent_ad)}</span>`:''}${version}<span class="chip ${a.rights_cleared?'good':'warn'}">rights ${a.rights_cleared?'cleared':'not fully cleared'}</span>${r.status?`<span class="chip ${r.status==='approved'?'good':'bad'}">${esc(r.status)}</span>`:''}${r.favorite?'<span class="chip star">★ winner</span>':''}</div>${aiSummary}<details class="details" ${compact?'':'open'}><summary>Source videos used in this ad (${segs.length} cuts)</summary><div class="sourceStrip">${segs.map((seg,i)=>sourceCutHtml(seg,a.name,i)).join('')}</div></details><div class="reviewRow"><button class="btn blue" onclick="openTimelineEditor('${esc(a.name)}')">✎ Edit ad</button><button class="btn purple" onclick="quickAiIteration('${esc(a.name)}','iterate')">AI iterate</button><button class="btn purple" onclick="quickAiIteration('${esc(a.name)}','regenerate')">AI regenerate</button><button class="btn primary" onclick="setReview('ads','${esc(a.name)}','approved')">✓ Approve</button><button class="btn danger" onclick="setReview('ads','${esc(a.name)}','rejected')">✕ Reject</button><button class="btn warn" onclick="setReview('ads','${esc(a.name)}',null,${!r.favorite})">★ Winner</button></div></div>`}
function renderWorkflow(){const s=STATE.stats,p=STATE.product,e=STATE.environment,j=STATE.job;const found=STATE.candidates.slice(0,8), downloaded=STATE.candidates.filter(v=>v.download_status==='downloaded').slice(0,8), clips=STATE.segments.filter(x=>x.review?.status!=='rejected').slice(0,9), ads=STATE.ads.filter(a=>a.exists).slice(-8).reverse();const img=p.image_url?`<img class="productImg" src="${esc(p.image_url)}">`:`<div class="productImg"></div>`;document.getElementById('workflow').innerHTML=`<div class="card hero">${img}<div><div class="eyebrow">ACTIVE PRODUCT</div><h2>${esc(p.title)}</h2><div class="small">${esc(p.brand||'')} · Product ID ${esc(p.product_id||'')}</div><div class="heroStats"><div class="heroStat"><b>${s.discovered}</b><span>found</span></div><div class="heroStat"><b>${s.downloaded}</b><span>downloaded</span></div><div class="heroStat"><b>${s.segments}</b><span>AI clips</span></div><div class="heroStat"><b>${s.ads_rendered}</b><span>edited ads</span></div></div></div><div class="heroAction"><div class="actions"><button class="btn blue" onclick="startLogin()">${e.login_active?'TikTok login open':'Open TikTok login'}</button><button class="btn primary" ${e.login_active||j.status==='running'?'disabled':''} onclick="startJob('run_remaining')">Run remaining workflow</button></div></div></div><div class="workflowRail">${stageCard(1,'Find videos',`${s.discovered} found`,s.discovered>0,'wf-find')}${stageCard(2,'Review sources',`${s.downloaded} downloaded`,s.downloaded>0,'wf-sources')}${stageCard(3,'AI clips',`${s.analysis_complete} analyzed`,s.analysis_complete>0,'wf-clips')}${stageCard(4,'Build ads',`${s.ad_plans} plans`,s.ad_plans>0,'wf-build')}${stageCard(5,'Review ads',`${s.ads_rendered} rendered`,s.ads_rendered>0,'wf-ads')}</div>
<div id="wf-find" class="section workflowJump"><div class="sectionHead"><div><div class="eyebrow">STEP 1</div><div class="sectionTitle">Find product videos</div><div class="small">Search TikTok using the product-specific query plan, then inspect what was actually found.</div></div><div class="actions"><button class="btn blue" onclick="startLogin()">Login</button><button class="btn primary" ${e.login_active||j.status==='running'?'disabled':''} onclick="startJob('discover')">Discover TikToks</button><button class="btn" onclick="openView('sources')">See all ${s.discovered}</button></div></div>${found.length?`<div class="gallery">${found.map(sourceCard).join('')}</div>`:'<div class="empty">No discovered videos yet. Open TikTok login if needed, then run Discover TikToks.</div>'}</div>
<div id="wf-sources" class="section workflowJump"><div class="sectionHead"><div><div class="eyebrow">STEP 2</div><div class="sectionTitle">Review downloaded source videos</div><div class="small">Watch the local source files before they become ad clips. Rights status stays separate from creative quality.</div></div><div class="actions"><button class="btn primary" ${e.login_active||j.status==='running'?'disabled':''} onclick="startJob('download')">Download best sources</button><button class="btn" onclick="openView('sources')">Full source library</button></div></div>${downloaded.length?`<div class="gallery">${downloaded.map(sourceCard).join('')}</div>`:'<div class="empty">No downloaded source videos yet.</div>'}</div>
<div id="wf-clips" class="section workflowJump"><div class="sectionHead"><div><div class="eyebrow">STEP 3</div><div class="sectionTitle">Review AI-selected moments</div><div class="small">Play exact timestamps, approve strong moments, reject weak ones, or add clips to a manual custom ad.</div></div><div class="actions"><button class="btn primary" ${j.status==='running'?'disabled':''} onclick="startJob('analyze')">Analyze remaining</button><button class="btn" onclick="openView('clips')">See all ${s.segments} clips</button></div></div>${clips.length?`<div class="clipGrid">${clips.map(x=>clipCard(x,true)).join('')}</div>`:'<div class="empty">No completed AI clips yet.</div>'}</div>
<div id="wf-build" class="section workflowJump"><div class="sectionHead"><div><div class="eyebrow">STEP 4</div><div class="sectionTitle">Build edited ads</div><div class="small">Your strongest working pattern so far is direct-response structure with controlled hook variations. You can still generate the full creative mix or a controlled A/B set.</div></div></div><div class="buildGrid"><div class="card"><div class="metricLabel">Recommended working recipe</div><div class="recipe"><div class="recipeItem"><strong class="role-hook">Hook</strong><span>stop the scroll</span></div><div class="recipeItem"><strong class="role-demo">Demo / reveal</strong><span>show product use</span></div><div class="recipeItem"><strong class="role-benefit">Benefit</strong><span>make result clear</span></div><div class="recipeItem"><strong class="role-cta">CTA</strong><span>short close</span></div></div><div class="actions"><button class="btn primary" ${j.status==='running'?'disabled':''} onclick="startJob('build_ads')">Generate creative mix</button><button class="btn purple" ${j.status==='running'?'disabled':''} onclick="startJob('build_ab_tests')">Generate controlled A/B set</button><button class="btn" onclick="openView('clips')">Build custom from clips</button></div></div><div class="card"><div class="metricLabel">Current job</div><div style="font-size:18px;font-weight:850;margin:6px 0">${esc((j.action||'idle').replaceAll('_',' '))} · ${esc(j.status)}</div><div class="small">${esc((j.logs||[]).slice(-3).join(' · ')||'No pipeline job running.')}</div><div class="actions" style="margin-top:10px"><button class="btn" onclick="openView('controls')">Open live controls</button></div></div></div></div>
<div id="wf-ads" class="section workflowJump"><div class="sectionHead"><div><div class="eyebrow">STEP 5</div><div class="sectionTitle">Watch edited ads + see exactly what made each one</div><div class="small">Every ad includes its finished MP4 and the original source cuts underneath it.</div></div><button class="btn" onclick="openView('ads')">See all ${s.ads_rendered} ads</button></div>${ads.length?`<div class="adGrid">${ads.map(a=>adCard(a,true)).join('')}</div>`:'<div class="empty">No rendered ads yet.</div>'}<div class="notice" style="margin-top:12px">Public TikTok footage is not automatically licensed for commercial advertising. Use the source-rights controls before publishing an ad.</div></div>`}
function renderSources(){const rows=STATE.candidates;document.getElementById('sources').innerHTML=`<div class="sectionHead"><div><div class="eyebrow">SOURCE LIBRARY</div><div class="sectionTitle">All videos found for this product</div><div class="small">${STATE.stats.discovered} discovered · ${STATE.stats.downloaded} downloaded locally. Downloaded files can be played directly here.</div></div><div class="actions"><button class="btn blue" onclick="startLogin()">Open TikTok login</button><button class="btn primary" onclick="startJob('discover')">Discover again</button><button class="btn" onclick="startJob('download')">Download best sources</button></div></div>${rows.length?`<div class="gallery">${rows.map(sourceCard).join('')}</div>`:'<div class="empty">No sources found yet.</div>'}<div class="section"><div class="sectionTitle">Rights management</div><div class="tableWrap"><table><thead><tr><th>Video</th><th>Creator</th><th>Score</th><th>Status</th><th>Rights</th><th>Action</th></tr></thead><tbody>${rows.map(v=>`<tr><td>${(v.poster_url||v.thumbnail_url)?`<img class="miniThumb" loading="lazy" alt="Source preview" src="${esc(v.poster_url||v.thumbnail_url)}" onerror="this.hidden=true">`:esc(v.video_id)}</td><td>@${esc(v.creator||'unknown')}</td><td>${Number(v.combined_score||0).toFixed(3)}</td><td>${esc(v.download_status||'found')}</td><td><select class="select" onchange="setRights('${esc(v.video_id)}',this.value)">${STATE.rights_statuses.map(st=>`<option value="${st}" ${(v.rights?.status||'unknown')===st?'selected':''}>${esc(st.replaceAll('_',' '))}</option>`).join('')}</select></td><td>${v.media_url?`<button class="btn blue" onclick="openSourceById('${esc(v.video_id)}')">Play</button>`:''}</td></tr>`).join('')}</tbody></table></div></div>`}
function renderClips(){const roles=['all',...Object.keys(STATE.role_counts).filter(r=>STATE.role_counts[r])];document.getElementById('clips').innerHTML=`<div class="sectionHead"><div><div class="eyebrow">AI CLIP REVIEW</div><div class="sectionTitle">Timestamped moments from analyzed source videos</div><div class="small">Approve/reject/favorite clips, or select several clips for a manual custom ad.</div></div><button class="btn primary" onclick="startJob('analyze')">Analyze remaining</button></div><div class="chips" style="margin-bottom:11px">${roles.map(r=>`<button class="btn" onclick="filterClips('${esc(r)}')">${esc(roleName(r))}${r==='all'?'':` · ${STATE.role_counts[r]||0}`}</button>`).join('')}</div><div id="clipGrid" class="clipGrid">${STATE.segments.map(x=>clipCard(x)).join('')}</div>`}
function filterClips(role){const rows=role==='all'?STATE.segments:STATE.segments.filter(x=>x.role===role);document.getElementById('clipGrid').innerHTML=rows.length?rows.map(x=>clipCard(x)).join(''):'<div class="empty">No clips in this role.</div>'}
function renderAds(){const rows=STATE.ads.filter(a=>a.exists).slice().reverse();document.getElementById('ads').innerHTML=`<div class="sectionHead"><div><div class="eyebrow">EDITED ADS</div><div class="sectionTitle">Finished videos and full source provenance</div><div class="small">Watch the edited output, then inspect and replay every original source cut that went into it.</div></div><div class="actions"><button class="btn primary" onclick="startJob('build_ads')">Generate creative mix</button><button class="btn purple" onclick="startJob('build_ab_tests')">Generate A/B set</button></div></div>${rows.length?`<div class="adGrid">${rows.map(a=>adCard(a,false)).join('')}</div>`:'<div class="empty">No rendered ads yet.</div>'}`}
function renderControls(){const e=STATE.environment,j=STATE.job,x=STATE.settings;document.getElementById('controls').innerHTML=`<div class="controlsGrid"><div><div class="sectionTitle">Pipeline controls</div><div class="card"><div class="actions"><button class="btn blue" onclick="startLogin()">Open TikTok login</button><button class="btn" ${!e.login_active?'disabled':''} onclick="finishLogin()">I’m logged in</button><button class="btn" onclick="startJob('discover')">Discover TikToks</button><button class="btn" onclick="startJob('download')">Download sources</button><button class="btn" onclick="startJob('analyze')">Analyze</button><button class="btn primary" onclick="startJob('build_ads')">Build ads</button><button class="btn purple" onclick="startJob('build_ab_tests')">Build A/B set</button><button class="btn" onclick="startJob('run_remaining')">Run remaining</button></div></div><div class="sectionTitle">New product</div><div class="card"><div class="actions"><input id="newProductUrl" class="input" style="flex:1;min-width:280px" placeholder="Paste TikTok Shop product URL"><button class="btn primary" onclick="createProduct()">Create workspace</button></div></div></div><div><div class="sectionTitle">Live job</div><div class="card"><div style="font-size:18px;font-weight:850;margin-bottom:8px">${esc((j.action||'idle').replaceAll('_',' '))} · ${esc(j.status)}</div><div id="jobLog" class="log">${esc((j.logs||[]).join('\n')||'No live job output yet.')}</div></div></div></div><div class="section"><div class="sectionTitle">Creative settings</div><div class="card"><div class="settingsGrid"><div class="setting"><label>Target seconds</label><input id="setTarget" class="input" type="number" value="${x.target_duration}" min="8" max="60"></div><div class="setting"><label>Max seconds</label><input id="setMax" class="input" type="number" value="${x.max_duration}" min="8" max="60"></div><div class="setting"><label>A/B variants</label><input id="setAB" class="input" type="number" value="${x.ab_variants}" min="2" max="20"></div><div class="setting"><label>Min product match</label><input id="setProduct" class="input" type="number" value="${x.product_match_min}" min="0" max="1" step=".05"></div><div class="setting"><label>Min relevance</label><input id="setRelevance" class="input" type="number" value="${x.video_relevance_min}" min="0" max="1" step=".05"></div><div class="setting"><label>Quality threshold</label><input id="setQuality" class="input" type="number" value="${x.quality_threshold}" min="0" max="100"></div><div class="setting"><label><input id="setCaptions" type="checkbox" ${x.captions_enabled?'checked':''}> Captions</label></div><div class="setting"><label><input id="setHeadline" type="checkbox" ${x.headline_enabled?'checked':''}> Hook headline</label></div><div class="setting"><label><input id="setAudio" type="checkbox" ${x.audio_normalize?'checked':''}> Normalize audio</label></div><div class="setting"><label><input id="setRights" type="checkbox" ${x.require_cleared_rights?'checked':''}> Rights-cleared only</label></div><div class="setting"><label>Optional fixed headline</label><input id="setHeadlineText" class="input" value="${esc(x.headline_text||'')}"></div></div><div class="actions" style="margin-top:12px"><button class="btn primary" onclick="saveSettings()">Save settings</button></div></div></div>`;const log=document.getElementById('jobLog');if(log)log.scrollTop=log.scrollHeight}
async function setReview(kind,id,status,favorite){try{const payload={kind,id};if(status!==null&&status!==undefined)payload.status=status;if(favorite!==undefined)payload.favorite=favorite;await api('/api/review',payload);await loadState();openView(currentView())}catch(e){alert(e.message)}}
async function setRights(video_id,status){try{await api('/api/rights',{video_id,status});await loadState();openView(currentView())}catch(e){alert(e.message)}}
async function startLogin(){try{await api('/api/login/start');setTimeout(loadState,500)}catch(e){alert(e.message)}}
async function finishLogin(){try{await api('/api/login/finish');setTimeout(loadState,500)}catch(e){alert(e.message)}}
async function startJob(action){try{await api('/api/job',{action});await loadState();if(currentView()!=='workflow'&&currentView()!=='controls')openView('workflow')}catch(e){alert(e.message)}}
async function createProduct(){try{const url=document.getElementById('newProductUrl').value.trim();if(!url)return alert('Paste a TikTok Shop URL first.');await api('/api/product/create',{url});await loadState()}catch(e){alert(e.message)}}
async function saveSettings(){try{const patch={target_duration:+document.getElementById('setTarget').value,max_duration:+document.getElementById('setMax').value,ab_variants:+document.getElementById('setAB').value,product_match_min:+document.getElementById('setProduct').value,video_relevance_min:+document.getElementById('setRelevance').value,quality_threshold:+document.getElementById('setQuality').value,captions_enabled:document.getElementById('setCaptions').checked,headline_enabled:document.getElementById('setHeadline').checked,audio_normalize:document.getElementById('setAudio').checked,require_cleared_rights:document.getElementById('setRights').checked,headline_text:document.getElementById('setHeadlineText').value};await api('/api/settings',patch);await loadState();openView('controls')}catch(e){alert(e.message)}}
function addCustom(id){if(!CUSTOM.includes(id)&&CUSTOM.length<12)CUSTOM.push(id);renderTray()}
function clearTray(){CUSTOM=[];renderTray()}
function renderTray(){const tray=document.getElementById('customTray'),items=document.getElementById('trayItems');if(!tray||!items)return;tray.classList.toggle('show',CUSTOM.length>0);items.innerHTML=CUSTOM.map((id,i)=>`<span class="trayItem">${i+1}. ${esc(id)} <button style="background:none;border:0;color:#ff9aa5;cursor:pointer" onclick="CUSTOM.splice(${i},1);renderTray()">×</button></span>`).join('')}
async function renderCustomAd(){try{if(CUSTOM.length<2)return alert('Choose at least two AI clips.');await api('/api/custom-ad',{segment_ids:CUSTOM});CUSTOM=[];renderTray();await loadState();openView('workflow');jump('wf-ads')}catch(e){alert(e.message)}}
function openSourceById(videoId){const v=STATE.candidates.find(x=>String(x.video_id)===String(videoId));if(!v||!v.media_url)return;openVideoModal(v.media_url,0,null,`@${v.creator||'unknown'}`,'Full downloaded source',v.caption||'')}
function openClipById(segmentId){const x=STATE.segments.find(s=>String(s.segment_id)===String(segmentId));if(!x||!x.media_url)return;openVideoModal(x.media_url,Number(x.start||0),Number(x.end||0),`${roleName(x.role)} · @${x.creator||'unknown'}`,`Exact AI clip · ${fmt(x.start)}–${fmt(x.end)}`,x.reason||x.text||'')}
function openAdCut(adName,index){const a=STATE.ads.find(x=>String(x.name)===String(adName));const s=a?.segments?.[Number(index)];if(!s||!s.source_media_url)return;openVideoModal(s.source_media_url,Number(s.start||0),Number(s.end||0),`${roleName(s.role)} · @${s.source_creator||s.creator||'unknown'}`,`Source cut used in ${a.name} · ${fmt(s.start)}–${fmt(s.end)}`,s.source_caption||s.text||'')}
function openAdAudioCut(adName,index){const a=STATE.ads.find(x=>String(x.name)===String(adName));const s=a?.segments?.[Number(index)];if(!s||!s.audio_source_media_url)return;openVideoModal(s.audio_source_media_url,Number(s.audio_start||0),Number(s.audio_end||0),`Voiceover · @${s.audio_source_creator||'unknown'}`,`Audio source used in ${a.name} · ${fmt(s.audio_start)}–${fmt(s.audio_end)}`,s.audio_source_caption||s.text||'')}

function cloneObj(x){return JSON.parse(JSON.stringify(x))}
const EDITOR_FRAME_RATE=30;
const EDITOR_FRAME=1/EDITOR_FRAME_RATE;
function editorTotal(){return (EDITOR?.segments||[]).reduce((n,s)=>n+Math.max(0,Number(s.gap_before||0))+Math.max(0,Number(s.end||0)-Number(s.start||0)),0)}
function editorGapTotal(){return (EDITOR?.segments||[]).reduce((n,s)=>n+Math.max(0,Number(s.gap_before||0)),0)}
function editorSource(seg){const vid=String(seg.video_id||'');const c=(STATE.candidates||[]).find(v=>String(v.video_id)===vid);const a=(STATE.analyses||[]).find(v=>String(v.video_id)===vid);return {media_url:seg.source_media_url||seg.media_url||a?.media_url||c?.media_url||'',duration:Number(seg.source_duration_seconds||a?.duration_seconds||c?.duration_seconds||0),creator:seg.source_creator||seg.creator||a?.creator||c?.creator||'',tiktok_url:seg.source_tiktok_url||seg.tiktok_url||a?.tiktok_url||c?.url||''}}
function editorNormalizeClient(seg){const s=cloneObj(seg||{}),src=editorSource(s);s.video_id=String(s.video_id||'');s.creator=s.creator||src.creator||'';s.tiktok_url=s.tiktok_url||src.tiktok_url||'';s.start=Math.max(0,Number(s.start||0));s.end=Math.max(s.start+.25,Number(s.end||s.start+4));if(src.duration>0){s.start=Math.min(s.start,Math.max(0,src.duration-.25));s.end=Math.min(Math.max(s.end,s.start+.25),src.duration)}s.start=Math.round(s.start*1000)/1000;s.end=Math.round(s.end*1000)/1000;s.gap_before=Math.max(0,Math.round(Number(s.gap_before||0)*1000)/1000);s.role=String(s.role||'custom');s.text=String(s.text||'');s.audio_mode=String(s.audio_mode||'original');s.source_media_url=src.media_url;s.source_duration_seconds=src.duration;return s}
function editorSnapshot(){return {segments:cloneObj(EDITOR?.segments||[]),selected:Number(EDITOR?.selected||0)}}
function editorRestoreSnapshot(snap){if(!EDITOR||!snap)return;EDITOR.segments=(snap.segments||[]).map(editorNormalizeClient);EDITOR.selected=Math.max(0,Math.min(Number(snap.selected||0),Math.max(0,EDITOR.segments.length-1)))}
function editorRemember(){if(!EDITOR)return;EDITOR.history=EDITOR.history||[];EDITOR.history.push(editorSnapshot());if(EDITOR.history.length>100)EDITOR.history.shift();EDITOR.future=[]}
function editorMutate(fn){if(!EDITOR)return;editorRemember();fn();EDITOR.segments=(EDITOR.segments||[]).map(editorNormalizeClient);EDITOR.selected=Math.max(0,Math.min(Number(EDITOR.selected||0),Math.max(0,EDITOR.segments.length-1)));EDITOR.dirty=true;renderAdEditor()}
function editorUndo(){if(!EDITOR||!(EDITOR.history||[]).length)return;EDITOR.future=EDITOR.future||[];EDITOR.future.push(editorSnapshot());const snap=EDITOR.history.pop();editorRestoreSnapshot(snap);EDITOR.dirty=true;renderAdEditor()}
function editorRedo(){if(!EDITOR||!(EDITOR.future||[]).length)return;EDITOR.history=EDITOR.history||[];EDITOR.history.push(editorSnapshot());const snap=EDITOR.future.pop();editorRestoreSnapshot(snap);EDITOR.dirty=true;renderAdEditor()}
function openAdEditor(name,focusAi=false){const a=(STATE.ads||[]).find(x=>String(x.name)===String(name));if(!a)return alert('Ad not found.');EDITOR={parentName:a.name,base:cloneObj(a),segments:(a.segments||[]).map(editorNormalizeClient),selected:0,history:[],future:[],zoom:1.15,playhead:0,fps:EDITOR_FRAME_RATE,dirty:false,aiMessages:[{role:'assistant',content:'Tell me as precisely as you can what you want changed. Mention what I should keep, replace, add or remove, the order, target length, pacing, and any creator/source preference. Directions are optional; if something is unclear, I’ll ask one question before I edit.'}],aiReady:false,aiDirection:'',pendingMode:focusAi||''};EDITOR_LIBRARY='ai';document.getElementById('editorTitle').textContent=`Edit · ${a.name}`;document.getElementById('editorSub').textContent='Timeline editor · direct edge trim · drag to reorder · every render creates a new version';document.getElementById('editorModal').classList.add('show');renderAdEditor();if(focusAi)setTimeout(()=>document.getElementById('editorAiInput')?.focus(),50)}
function closeAdEditor(){document.getElementById('editorModal').classList.remove('show');EDITOR=null;EDITOR_DRAG=null}
function editorSelect(i){if(!EDITOR)return;EDITOR.selected=Math.max(0,Math.min(Number(i),EDITOR.segments.length-1));renderAdEditor()}
function editorMove(i,delta){if(!EDITOR)return;const j=i+delta;if(j<0||j>=EDITOR.segments.length)return;editorMutate(()=>{const [x]=EDITOR.segments.splice(i,1);EDITOR.segments.splice(j,0,x);EDITOR.selected=j})}
function editorDrop(i){if(!EDITOR||EDITOR_DRAG===null)return;const from=Number(EDITOR_DRAG),to=Number(i);EDITOR_DRAG=null;if(from===to)return;editorMutate(()=>{const [x]=EDITOR.segments.splice(from,1);EDITOR.segments.splice(to,0,x);EDITOR.selected=to})}
function editorDelete(i){if(!EDITOR||EDITOR.segments.length<=1)return alert('An ad needs at least one clip.');editorMutate(()=>{EDITOR.segments.splice(i,1);EDITOR.selected=Math.max(0,Math.min(EDITOR.selected,EDITOR.segments.length-1))})}
function editorDuplicate(i){if(!EDITOR||EDITOR.segments.length>=20)return;editorMutate(()=>{EDITOR.segments.splice(i+1,0,cloneObj(EDITOR.segments[i]));EDITOR.selected=i+1})}
function editorApplyTrimRaw(index,edge,target,base=null){if(!EDITOR)return;const current=EDITOR.segments[index];if(!current)return;const orig=cloneObj(base?.segment||current),src=editorSource(orig),frame=1/EDITOR_FRAME_RATE;let st=Number(orig.start),en=Number(orig.end),value=Number(target);if(!Number.isFinite(value))return;if(edge==='left'){value=Math.max(0,Math.min(value,en-.25));if(src.duration>0)value=Math.min(value,Math.max(0,src.duration-.25));value=Math.round(value/frame)*frame;const delta=value-st;EDITOR.segments[index]={...current,start:value,end:en,gap_before:Math.max(0,Number(orig.gap_before||0)+delta)}}else{value=Math.max(st+.25,value);if(src.duration>0)value=Math.min(value,src.duration);value=Math.round(value/frame)*frame;const delta=value-en;EDITOR.segments[index]={...current,start:st,end:value,gap_before:Number(orig.gap_before||0)};if(index+1<EDITOR.segments.length){const next=EDITOR.segments[index+1],nextGap=Number(base?.next_gap??next.gap_before??0);EDITOR.segments[index+1]={...next,gap_before:Math.max(0,nextGap-delta)}}}}
function editorUpdate(field,value){if(!EDITOR)return;const s=EDITOR.segments[EDITOR.selected];if(!s)return;if(field==='start'||field==='end'){editorMutate(()=>editorApplyTrimRaw(EDITOR.selected,field==='start'?'left':'right',Number(value)));return}editorMutate(()=>{EDITOR.segments[EDITOR.selected]={...s,[field]:value}})}
function editorNudge(field,delta){if(!EDITOR)return;const s=EDITOR.segments[EDITOR.selected];if(!s)return;if(field==='start'||field==='end'){const target=Number(s[field]||0)+Number(delta);editorMutate(()=>editorApplyTrimRaw(EDITOR.selected,field==='start'?'left':'right',target));return}editorMutate(()=>{s[field]=Number(s[field]||0)+Number(delta);EDITOR.segments[EDITOR.selected]=s})}
function editorNudgeFrames(field,frames){editorNudge(field,Number(frames)/EDITOR_FRAME_RATE)}
function editorCloseGaps(){if(!EDITOR||editorGapTotal()<=.001)return;editorMutate(()=>{EDITOR.segments=EDITOR.segments.map(s=>({...s,gap_before:0}))})}
function editorSetZoom(v){if(!EDITOR)return;EDITOR.zoom=Math.max(.5,Math.min(14,Number(v||1)));renderAdEditor()}
function editorFitTimeline(){if(!EDITOR)return;const total=Math.max(1,editorTotal());EDITOR.zoom=Math.max(.5,Math.min(3,14/total));renderAdEditor()}
function editorPreview(context=false){if(!EDITOR)return;const s=EDITOR.segments[EDITOR.selected],src=editorSource(s);if(!src.media_url)return alert('Local source video is not available.');const pad=context?4:0;const st=Math.max(0,Number(s.start||0)-pad),en=context?Math.min(src.duration||Number(s.end||0)+pad,Number(s.end||0)+pad):Number(s.end||0);openVideoModal(src.media_url,st,en,`${roleName(s.role)} · @${src.creator||'unknown'}`,context?'Source context around this cut':'Exact edited cut',s.text||'')}
function editorInsert(seg,replace=false){if(!EDITOR)return;const item=editorNormalizeClient(seg);if(!item.video_id)return;editorMutate(()=>{const i=EDITOR.selected;if(replace&&EDITOR.segments.length){EDITOR.segments[i]=item;EDITOR.selected=i}else{EDITOR.segments.splice(i+1,0,item);EDITOR.selected=i+1}if(EDITOR.segments.length>20)EDITOR.segments=EDITOR.segments.slice(0,20)})}
function editorAddAi(id,replace=false){const x=(STATE.segments||[]).find(s=>String(s.segment_id)===String(id));if(x)editorInsert({...cloneObj(x),source_media_url:x.media_url,source_duration_seconds:x.source_duration_seconds},replace)}
function editorAddSource(id,replace=false){const v=(STATE.candidates||[]).find(x=>String(x.video_id)===String(id));if(!v||!v.media_url)return alert('Download this source before adding it.');const duration=Number(v.duration_seconds||0);editorInsert({video_id:String(v.video_id),creator:v.creator||'',tiktok_url:v.url||'',start:0,end:Math.min(duration||4,4),role:'custom',text:v.caption||'',source_media_url:v.media_url,source_duration_seconds:duration},replace)}
function editorAddAdCut(name,index,replace=false){const a=(STATE.ads||[]).find(x=>String(x.name)===String(name)),s=a?.segments?.[Number(index)];if(s)editorInsert(cloneObj(s),replace)}
function editorAddWholeAd(name){const a=(STATE.ads||[]).find(x=>String(x.name)===String(name));if(!a||!EDITOR)return;editorMutate(()=>{for(const s of a.segments||[]){if(EDITOR.segments.length>=20)break;EDITOR.segments.push(editorNormalizeClient(cloneObj(s)))}EDITOR.selected=EDITOR.segments.length-1})}
function editorSetLibrary(name){EDITOR_LIBRARY=name;renderEditorLibrary()}
function editorVersionFamily(){return Array.isArray(EDITOR?.base?.version_family)?EDITOR.base.version_family:[]}
async function editorRestoreVersion(name){if(!EDITOR||String(name)===String(EDITOR.parentName))return;const ok=confirm(`Restore ${name} as a new version? Your current and previous renders will stay untouched.`);if(!ok)return;try{await api('/api/editor/restore',{ad_name:EDITOR.parentName,restore_name:name,label:`Restored ${name}`});closeAdEditor();openView('ads')}catch(e){alert(e.message)}}
function editorOpenVersion(name){openAdEditor(name)}
function editorRuler(total,pxPerSec){const width=Math.max(760,total*pxPerSec);let step=5;if(EDITOR.zoom>=2)step=2;if(EDITOR.zoom>=4)step=1;if(EDITOR.zoom>=8)step=.5;const ticks=[];for(let t=0;t<=total+.001;t+=step){ticks.push(`<span class="rulerTick" style="left:${Math.min(width,Math.round(t*pxPerSec))}px">${t.toFixed(step<1?1:0)}s</span>`)}return `<div class="timelineRuler" style="width:${width}px">${ticks.join('')}</div>`}
function renderEditorLibrary(){if(!EDITOR)return;const lib=document.getElementById('editorLibrary');let cards='';if(EDITOR_LIBRARY==='ai'){const rows=(STATE.segments||[]).filter(x=>x.review?.status!=='rejected').slice(0,80);cards=rows.map(x=>`<div class="libraryCard"><div class="libraryTitle role-${esc(x.role)}">${esc(roleName(x.role))}</div><div class="libraryMeta">@${esc(x.creator||'unknown')} · ${fmt(Number(x.end)-Number(x.start))}</div><div class="libraryText">${esc((x.caption_text||x.text||'').slice(0,110))}</div><div class="actions"><button class="btn" onclick="editorAddAi('${esc(x.segment_id)}',false)">+ Add</button><button class="btn blue" onclick="editorAddAi('${esc(x.segment_id)}',true)">Replace selected</button><button class="btn" onclick="openClipById('${esc(x.segment_id)}')">Watch</button></div></div>`).join('')}else if(EDITOR_LIBRARY==='sources'){const rows=(STATE.candidates||[]).filter(x=>x.download_status==='downloaded'&&x.media_url).slice(0,100);cards=rows.map(v=>`<div class="libraryCard"><div class="libraryTitle">@${esc(v.creator||'unknown')}</div><div class="libraryMeta">source ${esc(v.video_id)} · ${fmt(v.duration_seconds||0)}</div><div class="libraryText">${esc((v.caption||'').slice(0,110))}</div><div class="actions"><button class="btn" onclick="editorAddSource('${esc(v.video_id)}',false)">+ Add source</button><button class="btn blue" onclick="editorAddSource('${esc(v.video_id)}',true)">Replace selected</button><button class="btn" onclick="openSourceById('${esc(v.video_id)}')">Watch</button></div></div>`).join('')}else{const rows=(STATE.ads||[]).filter(a=>a.exists&&a.name!==EDITOR.parentName).slice().reverse().slice(0,40);cards=rows.map(a=>`<div class="libraryCard"><div class="libraryTitle">${esc(a.name)}</div><div class="libraryMeta">${(a.segments||[]).length} cuts · ${fmt(a.duration_seconds)}</div><div class="libraryText">Cross-mix clips from this edited ad.</div><div class="actions"><button class="btn purple" onclick="editorAddWholeAd('${esc(a.name)}')">+ Whole ad</button></div>${(a.segments||[]).map((s,i)=>`<div style="border-top:1px solid #252d3a;margin-top:7px;padding-top:7px"><span class="role-${esc(s.role)}" style="font-size:9px;font-weight:800">${esc(roleName(s.role))}</span> <span class="small">@${esc(s.source_creator||s.creator||'')} ${fmt(Number(s.end)-Number(s.start))}</span><div class="actions" style="margin-top:4px"><button class="btn" onclick="editorAddAdCut('${esc(a.name)}',${i},false)">+ cut</button><button class="btn blue" onclick="editorAddAdCut('${esc(a.name)}',${i},true)">Replace</button><button class="btn" onclick="openAdCut('${esc(a.name)}',${i})">Watch</button></div></div>`).join('')}</div>`).join('')}lib.innerHTML=`<div class="sectionHead" style="margin-bottom:8px"><div><div class="eyebrow">ADD / CROSS-MIX</div><div class="sectionTitle">Clip library</div><div class="small">Insert another AI moment, add a full downloaded source, or borrow individual cuts / a whole sequence from another ad. Provenance stays attached.</div></div><div class="libraryTabs"><button class="btn ${EDITOR_LIBRARY==='ai'?'primary':''}" onclick="editorSetLibrary('ai')">AI clips</button><button class="btn ${EDITOR_LIBRARY==='sources'?'primary':''}" onclick="editorSetLibrary('sources')">Source videos</button><button class="btn ${EDITOR_LIBRARY==='ads'?'purple':''}" onclick="editorSetLibrary('ads')">Other ads</button></div></div><div class="libraryGrid">${cards||'<div class="empty">Nothing available in this library.</div>'}</div>`}
function renderEditorAiChat(){if(!EDITOR)return '';const bubbles=(EDITOR.aiMessages||[]).map(m=>`<div class="aiBubble ${m.role==='user'?'user':'assistant'}">${esc(m.content)}</div>`).join('');const ready=EDITOR.aiReady&&EDITOR.aiDirection?`<div class="aiReady"><b>Ready direction</b><br>${esc(EDITOR.aiDirection)}</div>`:'';return `<div class="aiChat" id="editorAiChat">${bubbles}</div>${ready}<div class="aiPromptRow" style="margin-top:8px"><textarea id="editorAiInput" class="input aiPrompt" rows="3" placeholder="Example: Keep hook B. Replace the demo with a closer product application. Add one extra result shot. Target 20 seconds. Keep the CTA under 3 seconds."></textarea><button class="btn purple" onclick="editorAiSend()">Send</button></div><div class="actions" style="margin-top:7px"><button class="btn" onclick="editorAiQuick('Keep the current hook, but find a stronger visual demo.')">Keep hook · better demo</button><button class="btn" onclick="editorAiQuick('Make this version tighter and faster while keeping the strongest message.')">Tighter pacing</button><button class="btn" onclick="editorAiQuick('Add another product/result source clip without changing the opening hook.')">Add another source</button></div><div class="editorHint">Be as specific as you want. If the request is ambiguous, the AI will ask one clarifying question before you generate.</div>`}
async function editorAiSend(){if(!EDITOR)return;const box=document.getElementById('editorAiInput');const text=String(box?.value||'').trim();if(!text)return;EDITOR.aiMessages.push({role:'user',content:text});EDITOR.aiReady=false;EDITOR.aiDirection='';renderAdEditor();try{const r=await api('/api/editor/ai/chat',{ad_name:EDITOR.parentName,messages:EDITOR.aiMessages,segments:editorPayload()});EDITOR.aiMessages.push({role:'assistant',content:r.reply||'Tell me a little more about the exact change you want.'});EDITOR.aiReady=!!r.ready;EDITOR.aiDirection=String(r.direction||'');renderAdEditor();setTimeout(()=>{const c=document.getElementById('editorAiChat');if(c)c.scrollTop=c.scrollHeight},20)}catch(e){EDITOR.aiMessages.push({role:'assistant',content:`I could not refine that direction: ${e.message}`});renderAdEditor()}}
function editorAiQuick(text){const box=document.getElementById('editorAiInput');if(box){box.value=text;box.focus()}}
function renderAdEditor(){if(!EDITOR)return;const content=document.getElementById('editorContent'),a=EDITOR.base,segs=EDITOR.segments,s=segs[EDITOR.selected]||null,total=editorTotal();const selected=s?editorSource(s):null;const pxPerSec=48*Number(EDITOR.zoom||1);const timelineWidth=Math.max(760,total*pxPerSec);let cursor=0;const blocks=segs.map((x,i)=>{const dur=Math.max(.25,Number(x.end)-Number(x.start)),w=Math.max(58,dur*pxPerSec),left=cursor;cursor+=dur;return `<div class="editClip timelineBlock ${i===EDITOR.selected?'selected':''}" style="width:${w}px;flex:0 0 ${w}px" draggable="true" ondragstart="EDITOR_DRAG=${i};this.classList.add('dragging')" ondragend="this.classList.remove('dragging')" ondragover="event.preventDefault()" ondrop="event.preventDefault();editorDrop(${i})" onclick="editorSelect(${i})"><div class="clipFill"></div><div class="dragHandle">⋮⋮</div><div><div class="editClipTitle role-${esc(x.role)}">${i+1}. ${esc(roleName(x.role))}</div><div class="editClipMeta">@${esc(editorSource(x).creator||'unknown')} · ${fmt(dur)}</div><div class="frameBadge">${Math.round(dur*EDITOR_FRAME_RATE)} frames · ad ${fmt(left)}→${fmt(left+dur)}</div></div><div class="editClipActions"><button class="btn" onclick="event.stopPropagation();editorMove(${i},-1)">←</button><button class="btn" onclick="event.stopPropagation();editorMove(${i},1)">→</button><button class="btn" onclick="event.stopPropagation();editorDuplicate(${i})">⧉</button><button class="btn danger" onclick="event.stopPropagation();editorDelete(${i})">×</button></div></div>`}).join('');const versions=editorVersionFamily();const versionHtml=versions.length?`<div class="editorVersionBar"><div class="sectionHead" style="margin-bottom:7px"><div><div class="eyebrow">VERSION HISTORY</div><div class="small">Open any previous render, or restore it as another non-destructive version.</div></div><span class="chip">${versions.length} versions</span></div><div class="historyStrip">${versions.map(v=>`<div class="historyCard ${String(v.name)===String(EDITOR.parentName)?'current':''}"><div class="historyTitle">${esc(v.name)}</div><div class="historyMeta">${esc(v.strategy||'ad')} · ${fmt(v.duration_seconds)}${v.edit_label?` · ${esc(v.edit_label)}`:''}</div><div class="actions"><button class="btn" onclick="editorOpenVersion('${esc(v.name)}')">Open</button>${String(v.name)!==String(EDITOR.parentName)?`<button class="btn warn" onclick="editorRestoreVersion('${esc(v.name)}')">Restore</button>`:''}</div></div>`).join('')}</div></div>`:'';const maxSrc=s?Math.max(Number(selected?.duration||0),Number(s.end||0),.25):0;const frame=1/EDITOR_FRAME_RATE;content.innerHTML=`<div><div class="editorPreviewRow"><div class="editorPreview">${a.media_url?`<video controls playsinline preload="metadata" src="${esc(a.media_url)}"></video>`:'<div class="empty">Original render unavailable.</div>'}</div><div class="editorPreviewInfo"><div class="metricLabel">Editing from</div><div style="font-size:13px;font-weight:850;margin-top:5px">${esc(EDITOR.parentName)}</div><div class="small" style="margin-top:5px">${EDITOR.dirty?'<span class="dirtyDot"></span> Unsaved timeline changes':'No unsaved timeline changes'}</div><div class="small" style="margin-top:8px"><span class="kbd">⌘/Ctrl Z</span> undo<br><span class="kbd">Shift ⌘/Ctrl Z</span> redo</div></div></div><div class="editorStatus"><span class="editorDuration">Edited timeline ${fmt(total)}</span><span class="chip">${segs.length} clips</span><span class="chip blue">original ${fmt(a.duration_seconds)}</span><span class="chip">30 fps edit grid</span></div><div class="editorToolbar"><div class="editorToolbarGroup"><button class="btn compact" ${!(EDITOR.history||[]).length?'disabled':''} onclick="editorUndo()">↶ Undo</button><button class="btn compact" ${!(EDITOR.future||[]).length?'disabled':''} onclick="editorRedo()">↷ Redo</button></div><div class="editorZoom"><button class="btn compact" onclick="editorSetZoom((EDITOR.zoom||1)/1.5)">−</button><span class="small">Timeline zoom</span><input type="range" min=".5" max="14" step=".25" value="${Number(EDITOR.zoom||1)}" onchange="editorSetZoom(this.value)"><b style="font-size:10px">${Number(EDITOR.zoom||1).toFixed(2)}×</b><button class="btn compact" onclick="editorSetZoom((EDITOR.zoom||1)*1.5)">+</button><button class="btn compact" onclick="editorFitTimeline()">Fit</button></div></div><div class="timelineViewport">${editorRuler(total,pxPerSec)}<div class="editorTrack" style="width:${timelineWidth}px">${blocks}</div></div>${versionHtml}</div><div class="editorSide">${s?`<div class="editorPanel"><h3>Selected clip</h3><div class="small">@${esc(selected.creator||'unknown')} · source ${esc(s.video_id)} ${selected.duration?`· ${fmt(selected.duration)} source`:''}</div><div class="trimGrid" style="margin-top:9px"><div class="trimBox"><label>Start · frame ${Math.round(Number(s.start)*EDITOR_FRAME_RATE)}</label><input class="input" type="number" step="${frame.toFixed(6)}" value="${Number(s.start).toFixed(3)}" onchange="editorUpdate('start',this.value)"><input class="trimSlider" type="range" min="0" max="${maxSrc.toFixed(3)}" step="${frame.toFixed(6)}" value="${Number(s.start).toFixed(3)}" onchange="editorUpdate('start',this.value)"><div class="frameButtons"><button class="btn" onclick="editorNudgeFrames('start',-5)">−5f</button><button class="btn" onclick="editorNudgeFrames('start',-1)">−1f</button><button class="btn" onclick="editorNudgeFrames('start',1)">+1f</button><button class="btn" onclick="editorNudgeFrames('start',5)">+5f</button></div><div class="nudgeRow"><button class="btn" onclick="editorNudge('start',-.5)">−.5s</button><button class="btn" onclick="editorNudge('start',.5)">+.5s</button></div></div><div class="trimBox"><label>End · frame ${Math.round(Number(s.end)*EDITOR_FRAME_RATE)}</label><input class="input" type="number" step="${frame.toFixed(6)}" value="${Number(s.end).toFixed(3)}" onchange="editorUpdate('end',this.value)"><input class="trimSlider" type="range" min="0" max="${maxSrc.toFixed(3)}" step="${frame.toFixed(6)}" value="${Number(s.end).toFixed(3)}" onchange="editorUpdate('end',this.value)"><div class="frameButtons"><button class="btn" onclick="editorNudgeFrames('end',-5)">−5f</button><button class="btn" onclick="editorNudgeFrames('end',-1)">−1f</button><button class="btn" onclick="editorNudgeFrames('end',1)">+1f</button><button class="btn" onclick="editorNudgeFrames('end',5)">+5f</button></div><div class="nudgeRow"><button class="btn" onclick="editorNudge('end',-.5)">−.5s</button><button class="btn" onclick="editorNudge('end',.5)">+.5s</button></div></div></div><div class="editorHint">Frame controls use the final 30 fps edit grid (1 frame ≈ 0.033s). Zoom in for precise trimming; the server clamps cuts to the source duration.</div><div class="setting" style="margin-top:9px"><label>Role</label><select class="select" onchange="editorUpdate('role',this.value)">${['hook','problem','product_reveal','demo','benefit','social_proof','objection','before_after','cta','custom'].map(r=>`<option value="${r}" ${r===s.role?'selected':''}>${roleName(r)}</option>`).join('')}</select></div><div class="setting" style="margin-top:9px"><label>Audio</label><select class="select" onchange="editorUpdate('audio_mode',this.value)"><option value="original" ${s.audio_mode!=='mute'?'selected':''}>Original source audio</option><option value="mute" ${s.audio_mode==='mute'?'selected':''}>Mute this clip</option></select></div><div class="setting" style="margin-top:9px"><label>Caption / transcript text</label><textarea class="input" rows="3" onchange="editorUpdate('text',this.value)">${esc(s.text||'')}</textarea></div><div class="actions" style="margin-top:9px"><button class="btn blue" onclick="editorPreview(false)">▶ Exact cut</button><button class="btn" onclick="editorPreview(true)">Watch ±4s context</button>${selected.tiktok_url?`<a class="btn" target="_blank" href="${esc(selected.tiktok_url)}">TikTok ↗</a>`:''}</div></div>`:''}<div class="editorPanel"><h3>Render another version</h3><div class="small">Your original and every previous edit stay untouched. This produces a new MP4 in the same version family.</div><input id="editorLabel" class="input" style="margin-top:8px" placeholder="Optional note, e.g. longer demo + hook B"><div class="actions" style="margin-top:8px"><button class="btn primary" onclick="renderEditorVersion()">Render new version</button></div></div><div class="editorPanel"><h3>AI Creative Director</h3><div class="small">Talk through one specific ad. The AI can preserve, reorder, replace, add or cross-mix analyzed footage, then make another iteration or a fresh regeneration.</div>${renderEditorAiChat()}<div class="actions" style="margin-top:8px"><button class="btn purple" onclick="aiIterateEditor('iterate')">Create focused iteration</button><button class="btn purple" onclick="aiIterateEditor('regenerate')">Create fresh regeneration</button></div></div></div>`;renderEditorLibrary()}

// V3.7 native timeline editor. These later declarations intentionally override the V3.6 render helpers.
let EDITOR_LIBRARY_DRAG=null, EDITOR_TRIM=null;
function editorTimelineScale(){return 70*Math.max(.35,Math.min(8,Number(EDITOR?.zoom||1)))}
function editorSetZoom(v){if(!EDITOR)return;EDITOR.zoom=Math.max(.35,Math.min(8,Number(v||1)));renderAdEditor()}
function editorFitTimeline(){if(!EDITOR)return;const total=Math.max(.5,editorTotal());const viewport=document.querySelector('.nativeTimelineScroll');const usable=Math.max(420,(viewport?.clientWidth||980)-70);EDITOR.zoom=Math.max(.35,Math.min(8,usable/(70*total)));renderAdEditor()}
function editorClearDropHints(){document.querySelectorAll('.timelineDropZone.over,.timelineClipNative.drop-before,.timelineClipNative.drop-after').forEach(x=>x.classList.remove('over','drop-before','drop-after'))}
function editorTimelineDragIndex(ev){if(EDITOR_DRAG!==null&&EDITOR_DRAG!==undefined)return Number(EDITOR_DRAG);try{const raw=String(ev?.dataTransfer?.getData('text/plain')||'');if(raw.startsWith('timeline:')){const n=Number(raw.slice(9));if(Number.isFinite(n))return n}}catch(e){}return null}
function editorReorder(from,insertIndex){if(!EDITOR)return false;from=Number(from);let to=Math.max(0,Math.min(Number(insertIndex),EDITOR.segments.length));if(!Number.isInteger(from)||from<0||from>=EDITOR.segments.length)return false;if(from<to)to-=1;if(from===to)return false;editorMutate(()=>{const [x]=EDITOR.segments.splice(from,1);EDITOR.segments.splice(to,0,x);EDITOR.selected=to});return true}
function editorDropAt(insertIndex,ev=null){if(!EDITOR)return;editorClearDropHints();if(EDITOR_LIBRARY_DRAG){const d=EDITOR_LIBRARY_DRAG;EDITOR_LIBRARY_DRAG=null;let items=[];if(d.kind==='ai'){const x=(STATE.segments||[]).find(s=>String(s.segment_id)===String(d.id));if(x)items=[editorNormalizeClient({...cloneObj(x),source_media_url:x.media_url,source_duration_seconds:x.source_duration_seconds})]}else if(d.kind==='source'){const v=(STATE.candidates||[]).find(x=>String(x.video_id)===String(d.id));if(v&&v.media_url){const duration=Number(v.duration_seconds||0);items=[editorNormalizeClient({video_id:String(v.video_id),creator:v.creator||'',tiktok_url:v.url||'',start:0,end:Math.min(duration||4,4),role:'custom',text:v.caption||'',source_media_url:v.media_url,source_duration_seconds:duration})]}}else if(d.kind==='adcut'){const a=(STATE.ads||[]).find(x=>String(x.name)===String(d.ad));const cut=a?.segments?.[Number(d.index)];if(cut)items=[editorNormalizeClient(cloneObj(cut))]}if(!items.length)return;editorMutate(()=>{let at=Math.max(0,Math.min(Number(insertIndex),EDITOR.segments.length));EDITOR.segments.splice(at,0,...items);EDITOR.selected=at});return}const from=editorTimelineDragIndex(ev);EDITOR_DRAG=null;if(from===null)return;editorReorder(from,insertIndex)}
function editorDragStart(ev,i){EDITOR_DRAG=Number(i);EDITOR_LIBRARY_DRAG=null;EDITOR.selected=Number(i);try{ev.dataTransfer.effectAllowed='move';ev.dataTransfer.setData('text/plain','timeline:'+i)}catch(e){}ev.currentTarget.classList.add('dragging')}
function editorDragEnd(ev){EDITOR_DRAG=null;ev.currentTarget.classList.remove('dragging');editorClearDropHints()}
function editorDropOver(ev){ev.preventDefault();try{ev.dataTransfer.dropEffect=EDITOR_LIBRARY_DRAG?'copy':'move'}catch(e){}ev.currentTarget.classList.add('over')}
function editorDropLeave(ev){ev.currentTarget.classList.remove('over')}
function editorClipDragOver(ev,index){ev.preventDefault();ev.stopPropagation();editorClearDropHints();const r=ev.currentTarget.getBoundingClientRect();const after=ev.clientX>=r.left+r.width/2;ev.currentTarget.classList.add(after?'drop-after':'drop-before');try{ev.dataTransfer.dropEffect=EDITOR_LIBRARY_DRAG?'copy':'move'}catch(e){}}
function editorClipDragLeave(ev){ev.currentTarget.classList.remove('drop-before','drop-after')}
function editorClipDrop(ev,index){ev.preventDefault();ev.stopPropagation();const r=ev.currentTarget.getBoundingClientRect();const after=ev.clientX>=r.left+r.width/2;const insertIndex=Number(index)+(after?1:0);editorDropAt(insertIndex,ev)}
function editorLibraryDragStart(ev,kind,id,ad='',index=-1){EDITOR_DRAG=null;EDITOR_LIBRARY_DRAG={kind,id,ad,index};try{ev.dataTransfer.effectAllowed='copy';ev.dataTransfer.setData('text/plain','library:'+kind+':'+id)}catch(e){}ev.currentTarget.classList.add('dragging')}
function editorLibraryDragEnd(ev){EDITOR_LIBRARY_DRAG=null;ev.currentTarget.classList.remove('dragging');editorClearDropHints()}
function editorInsertAt(seg,index){if(!EDITOR)return;const item=editorNormalizeClient(seg);if(!item.video_id)return;editorMutate(()=>{const at=Math.max(0,Math.min(Number(index),EDITOR.segments.length));EDITOR.segments.splice(at,0,item);EDITOR.selected=at;if(EDITOR.segments.length>20)EDITOR.segments=EDITOR.segments.slice(0,20)})}
function editorRefreshTimelineGeometry(){if(!EDITOR)return;const px=editorTimelineScale();let cursor=0;const drop0=document.querySelector('[data-drop-index="0"]');if(drop0)drop0.style.left='0px';EDITOR.segments.forEach((s,i)=>{const gap=Math.max(0,Number(s.gap_before||0)),dur=Math.max(.25,Number(s.end)-Number(s.start)),gapLeft=cursor*px;const gapEl=document.querySelector(`[data-gap-index="${i}"]`);if(gapEl){gapEl.style.left=gapLeft+'px';gapEl.style.width=Math.max(0,gap*px)+'px';gapEl.style.display=gap>.01?'flex':'none';gapEl.textContent=gap>.35?`GAP ${gap.toFixed(2)}s`:''}cursor+=gap;const left=cursor*px,w=Math.max(12,dur*px);const clip=document.querySelector(`[data-timeline-index="${i}"]`);if(clip){clip.style.left=left+'px';clip.style.width=w+'px';const d=clip.querySelector('[data-dur]');if(d)d.textContent=`${dur.toFixed(2)}s · ${Math.round(dur*EDITOR_FRAME_RATE)}f`}const audio=document.querySelector(`[data-audio-index="${i}"]`);if(audio){audio.style.left=left+'px';audio.style.width=w+'px'}cursor+=dur;const drop=document.querySelector(`[data-drop-index="${i+1}"]`);if(drop)drop.style.left=(cursor*px)+'px'});const width=Math.max(840,Math.ceil(cursor*px)+1);document.querySelectorAll('.nativeTrackCanvas,.nativeRuler').forEach(el=>el.style.width=width+'px');const total=document.getElementById('nativeTimelineTotal');if(total)total.textContent=fmt(cursor)}
function editorBeginTrim(ev,index,edge){if(!EDITOR)return;ev.preventDefault();ev.stopPropagation();EDITOR.selected=Number(index);const seg=EDITOR.segments[index];if(!seg)return;const handle=ev.currentTarget,clip=handle.closest('.timelineClipNative'),startX=Number(ev.clientX),px=editorTimelineScale(),frame=1/EDITOR_FRAME_RATE,pointerId=ev.pointerId,base={segment:cloneObj(seg),next_gap:index+1<EDITOR.segments.length?Number(EDITOR.segments[index+1].gap_before||0):0};let remembered=false,moved=false;if(clip){clip.classList.add('trimming');clip.setAttribute('draggable','false')}try{if(handle.setPointerCapture&&pointerId!==undefined)handle.setPointerCapture(pointerId)}catch(e){}const snapDelta=x=>Math.round(((Number(x)-startX)/px)/frame)*frame;const move=e=>{if(!EDITOR)return;if(pointerId!==undefined&&e.pointerId!==undefined&&e.pointerId!==pointerId)return;e.preventDefault();const delta=snapDelta(e.clientX);if(Math.abs(delta)<frame/2)return;if(!remembered){editorRemember();remembered=true}moved=true;const target=edge==='left'?Number(base.segment.start)+delta:Number(base.segment.end)+delta;editorApplyTrimRaw(index,edge,target,base);EDITOR.dirty=true;editorRefreshTimelineGeometry()};const up=e=>{if(pointerId!==undefined&&e?.pointerId!==undefined&&e.pointerId!==pointerId)return;handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',up);handle.removeEventListener('pointercancel',up);window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',up);try{if(handle.releasePointerCapture&&pointerId!==undefined&&handle.hasPointerCapture?.(pointerId))handle.releasePointerCapture(pointerId)}catch(err){}if(clip){clip.classList.remove('trimming');clip.setAttribute('draggable','true')}EDITOR_TRIM=null;if(moved){EDITOR.segments=EDITOR.segments.map(editorNormalizeClient);renderAdEditor()}};EDITOR_TRIM={index,edge};handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',up,{once:true});handle.addEventListener('pointercancel',up,{once:true});window.addEventListener('pointermove',move);window.addEventListener('pointerup',up,{once:true});window.addEventListener('pointercancel',up,{once:true})}
function editorTimelineTimeFromEvent(ev){const canvas=document.getElementById('nativeVideoTrack');if(!canvas||!EDITOR)return 0;const r=canvas.getBoundingClientRect();return Math.max(0,Math.min(editorTotal(),(ev.clientX-r.left)/editorTimelineScale()))}
function editorSeekTimeline(ev){if(!EDITOR||ev.target.closest('.timelineClipNative,.timelineDropZone'))return;const t=editorTimelineTimeFromEvent(ev);EDITOR.playhead=t;const v=document.getElementById('editorPreviewVideo');if(v&&Number.isFinite(t)){try{v.currentTime=Math.min(t,v.duration||t)}catch(e){}}editorMovePlayhead(t)}
function editorMovePlayhead(t){if(!EDITOR)return;EDITOR.playhead=Math.max(0,Number(t||0));document.querySelectorAll('.timelinePlayhead').forEach(x=>x.style.left=(EDITOR.playhead*editorTimelineScale())+'px');const tc=document.getElementById('editorTimecode');if(tc)tc.textContent=fmt(EDITOR.playhead)+' / '+fmt(editorTotal())}
function editorPreviewTime(t){if(!EDITOR||EDITOR_TRIM)return;editorMovePlayhead(Number(t||0))}
function editorTogglePlay(){const v=document.getElementById('editorPreviewVideo');if(!v)return;if(v.paused)v.play().catch(()=>{});else v.pause()}
function editorJump(delta){const v=document.getElementById('editorPreviewVideo');if(!v)return;v.currentTime=Math.max(0,Math.min(v.duration||999,Number(v.currentTime||0)+Number(delta||0)));editorMovePlayhead(v.currentTime)}
function editorSelect(i){if(!EDITOR)return;EDITOR.selected=Math.max(0,Math.min(Number(i),EDITOR.segments.length-1));renderAdEditor()}
function editorSetLibrary(name){EDITOR_LIBRARY=name;renderEditorLibrary()}
function editorRulerNative(width,pxPerSec,total){let step=5;if(EDITOR.zoom>=1.2)step=2;if(EDITOR.zoom>=2.5)step=1;if(EDITOR.zoom>=5)step=.5;const ticks=[];for(let t=0;t<=total+.001;t+=step)ticks.push(`<span class="nativeRulerTick" style="left:${Math.round(t*pxPerSec)}px">${t.toFixed(step<1?1:0)}s</span>`);return `<div class="nativeRuler" style="width:${width}px">${ticks.join('')}</div>`}
function editorTimelineHtml(){const segs=EDITOR.segments||[],px=editorTimelineScale(),total=editorTotal(),width=Math.max(840,Math.ceil(total*px)+1);let cursor=0,clips='',audio='',gaps='',drops=`<div class="timelineDropZone" data-drop-index="0" style="left:0px" ondragover="editorDropOver(event)" ondragleave="editorDropLeave(event)" ondrop="event.preventDefault();editorDropAt(0,event)"></div>`;segs.forEach((x,i)=>{const gap=Math.max(0,Number(x.gap_before||0)),dur=Math.max(.25,Number(x.end)-Number(x.start)),gapLeft=cursor*px;gaps+=`<div class="timelineGapNative" data-gap-index="${i}" style="left:${gapLeft}px;width:${Math.max(0,gap*px)}px;display:${gap>.01?'flex':'none'}">${gap>.35?`GAP ${gap.toFixed(2)}s`:''}</div>`;cursor+=gap;const left=cursor*px,w=Math.max(12,dur*px),src=editorSource(x);clips+=`<div class="timelineClipNative ${i===EDITOR.selected?'selected':''}" data-timeline-index="${i}" data-role="${esc(x.role)}" style="left:${left}px;width:${w}px" draggable="true" ondragstart="editorDragStart(event,${i})" ondragend="editorDragEnd(event)" ondragover="editorClipDragOver(event,${i})" ondragleave="editorClipDragLeave(event)" ondrop="editorClipDrop(event,${i})" onclick="event.stopPropagation();editorSelect(${i})" ondblclick="event.stopPropagation();editorSelect(${i});editorPreview(false)"><div class="timelineTrimHandle left" draggable="false" title="Drag this edge. Shortening creates timeline space; extending consumes available space first." onpointerdown="editorBeginTrim(event,${i},'left')"></div><div class="timelineClipInner"><div><div class="timelineClipRole role-${esc(x.role)}">${esc(roleName(x.role))}</div><div class="timelineClipCreator">@${esc(src.creator||'unknown')}</div></div><div class="timelineClipDur" data-dur>${dur.toFixed(2)}s · ${Math.round(dur*EDITOR_FRAME_RATE)}f</div></div><div class="timelineTrimHandle right" draggable="false" title="Drag this edge. Shortening creates timeline space; extending consumes available space first." onpointerdown="editorBeginTrim(event,${i},'right')"></div></div>`;audio+=`<div class="audioClipNative ${x.audio_mode==='mute'?'muted':''}" data-audio-index="${i}" style="left:${left}px;width:${w}px" title="${x.audio_mode==='mute'?'Muted':'Original source audio'}"></div>`;cursor+=dur;drops+=`<div class="timelineDropZone" data-drop-index="${i+1}" style="left:${cursor*px}px" ondragover="editorDropOver(event)" ondragleave="editorDropLeave(event)" ondrop="event.preventDefault();editorDropAt(${i+1},event)"></div>`});const play=Math.max(0,Math.min(total,Number(EDITOR.playhead||0)))*px,gapTotal=editorGapTotal();return `<div class="nativeTimelineDock"><div class="nativeTimelineToolbar"><div class="nativeTimelineTools"><button class="btn" ${!(EDITOR.history||[]).length?'disabled':''} onclick="editorUndo()">↶ Undo</button><button class="btn" ${!(EDITOR.future||[]).length?'disabled':''} onclick="editorRedo()">↷ Redo</button><button class="btn ${gapTotal>.01?'warn':''}" ${gapTotal<=.01?'disabled':''} onclick="editorCloseGaps()">Close gaps${gapTotal>.01?` · ${gapTotal.toFixed(2)}s`:''}</button><span class="chip">V1 video</span><span class="chip">A1 audio</span><span class="small">Drag clips to reorder · Direct edge trim: the edge you drag moves. Empty timeline space is shown as a hatched GAP.</span></div><div class="nativeZoom"><button class="btn" onclick="editorSetZoom((EDITOR.zoom||1)/1.35)">−</button><input aria-label="Timeline zoom" type="range" min=".35" max="8" step=".05" value="${Number(EDITOR.zoom||1)}" onchange="editorSetZoom(this.value)"><button class="btn" onclick="editorSetZoom((EDITOR.zoom||1)*1.35)">+</button><button class="btn" onclick="editorFitTimeline()">Fit</button></div></div><div class="nativeTimelineScroll"><div class="nativeRulerRow"><div class="nativeTrackLabel">TIME</div>${editorRulerNative(width,px,total)}</div><div class="nativeTrackRow"><div class="nativeTrackLabel">V1</div><div id="nativeVideoTrack" class="nativeTrackCanvas" style="width:${width}px" onclick="editorSeekTimeline(event)">${gaps}${clips}${drops}<div class="timelinePlayhead" style="left:${play}px"></div></div></div><div class="nativeTrackRow audio"><div class="nativeTrackLabel">A1</div><div class="nativeTrackCanvas" style="width:${width}px">${audio}<div class="timelinePlayhead" style="left:${play}px"></div></div></div></div><div class="nativeTimelineHint">Drag any clip directly onto another: left half inserts before, right half inserts after. Drag any clip directly onto another: left half inserts before, right half inserts after. Trim behaves like a normal edge edit: dragging the left edge right removes the head from the left; dragging the right edge left removes the tail from the right. Gaps are real timeline space and render black/silent unless you click Close gaps. Extending an edge consumes nearby gap first; once no space remains, later clips ripple.</div>${editorVersionsNative()}</div>`}
function editorVersionsNative(){const versions=editorVersionFamily();if(!versions.length)return '';return `<div class="nativeVersionStrip">${versions.map(v=>`<div class="nativeVersionCard ${String(v.name)===String(EDITOR.parentName)?'current':''}"><div class="historyTitle">${esc(v.name)}</div><div class="historyMeta">${esc(v.strategy||'ad')} · ${fmt(v.duration_seconds)}</div><div class="actions"><button class="btn" onclick="editorOpenVersion('${esc(v.name)}')">Open</button>${String(v.name)!==String(EDITOR.parentName)?`<button class="btn warn" onclick="editorRestoreVersion('${esc(v.name)}')">Restore</button>`:''}</div></div>`).join('')}</div>`}
function renderEditorLibrary(){if(!EDITOR)return;const lib=document.getElementById('editorLibrary');if(!lib)return;let cards='';if(EDITOR_LIBRARY==='ai'){const rows=(STATE.segments||[]).filter(x=>x.review?.status!=='rejected').slice(0,100);cards=rows.map(x=>`<div class="nativeMediaItem" draggable="true" ondragstart="editorLibraryDragStart(event,'ai','${esc(x.segment_id)}')" ondragend="editorLibraryDragEnd(event)"><div class="libraryTitle role-${esc(x.role)}">${esc(roleName(x.role))}</div><div class="libraryMeta">@${esc(x.creator||'unknown')} · ${fmt(Number(x.end)-Number(x.start))}</div><div class="libraryText">${esc((x.caption_text||x.text||'').slice(0,90))}</div><div class="actions"><button class="btn" onclick="editorAddAi('${esc(x.segment_id)}',false)">+ Timeline</button><button class="btn" onclick="openClipById('${esc(x.segment_id)}')">Watch</button></div></div>`).join('')}else if(EDITOR_LIBRARY==='sources'){const rows=(STATE.candidates||[]).filter(x=>x.download_status==='downloaded'&&x.media_url).slice(0,120);cards=rows.map(v=>`<div class="nativeMediaItem" draggable="true" ondragstart="editorLibraryDragStart(event,'source','${esc(v.video_id)}')" ondragend="editorLibraryDragEnd(event)"><div class="libraryTitle">@${esc(v.creator||'unknown')}</div><div class="libraryMeta">${fmt(v.duration_seconds||0)} · source ${esc(v.video_id)}</div><div class="libraryText">${esc((v.caption||'').slice(0,90))}</div><div class="actions"><button class="btn" onclick="editorAddSource('${esc(v.video_id)}',false)">+ Timeline</button><button class="btn" onclick="openSourceById('${esc(v.video_id)}')">Watch</button></div></div>`).join('')}else{const rows=(STATE.ads||[]).filter(a=>a.exists&&a.name!==EDITOR.parentName).slice().reverse().slice(0,40);cards=rows.map(a=>`<div class="nativeMediaItem"><div class="libraryTitle">${esc(a.name)}</div><div class="libraryMeta">${(a.segments||[]).length} cuts · ${fmt(a.duration_seconds)}</div><div class="actions"><button class="btn purple" onclick="editorAddWholeAd('${esc(a.name)}')">+ Whole ad</button></div>${(a.segments||[]).map((s,i)=>`<div draggable="true" ondragstart="editorLibraryDragStart(event,'adcut','', '${esc(a.name)}',${i})" ondragend="editorLibraryDragEnd(event)" style="border-top:1px solid #252d3a;margin-top:7px;padding-top:7px;cursor:grab"><span class="role-${esc(s.role)}" style="font-size:9px;font-weight:800">${esc(roleName(s.role))}</span> <span class="small">@${esc(s.source_creator||s.creator||'')} ${fmt(Number(s.end)-Number(s.start))}</span><div class="actions" style="margin-top:4px"><button class="btn" onclick="editorAddAdCut('${esc(a.name)}',${i},false)">+ Timeline</button><button class="btn" onclick="openAdCut('${esc(a.name)}',${i})">Watch</button></div></div>`).join('')}</div>`).join('')}lib.innerHTML=`<div class="nativePanelHead"><div class="nativePanelTitle">Media</div><span class="small">drag to timeline</span></div><div class="nativeMediaTabs"><button class="btn ${EDITOR_LIBRARY==='ai'?'primary':''}" onclick="editorSetLibrary('ai')">AI clips</button><button class="btn ${EDITOR_LIBRARY==='sources'?'primary':''}" onclick="editorSetLibrary('sources')">Sources</button><button class="btn ${EDITOR_LIBRARY==='ads'?'purple':''}" onclick="editorSetLibrary('ads')">Other ads</button></div><div class="nativeMediaList">${cards||'<div class="empty">Nothing available.</div>'}</div>`}
function editorInspectorNative(){const s=EDITOR.segments[EDITOR.selected];if(!s)return '<div class="nativeEmptyInspector">Select a timeline clip.</div>';const src=editorSource(s),dur=Math.max(.25,Number(s.end)-Number(s.start)),f=1/EDITOR_FRAME_RATE,before=Math.max(0,Number(s.start)),after=src.duration>0?Math.max(0,src.duration-Number(s.end)):null;return `<div class="inspectorClipTitle role-${esc(s.role)}">${esc(roleName(s.role))}</div><div class="inspectorSource">@${esc(src.creator||'unknown')} · ${dur.toFixed(2)}s · source ${esc(s.video_id)}</div><div class="small" style="margin:-3px 0 8px;color:#9fb0c5">Trim headroom: ${before.toFixed(2)}s earlier${after===null?'':` · ${after.toFixed(2)}s later`} · timeline gap before ${Number(s.gap_before||0).toFixed(2)}s. Drag the green left/right edges on the timeline.</div><div class="inspectorRow"><div class="inspectorGroup"><label>IN / START</label><div class="timeField"><input class="input" type="number" step="${f.toFixed(6)}" value="${Number(s.start).toFixed(3)}" onchange="editorUpdate('start',this.value)"><span class="chip">${Math.round(Number(s.start)*EDITOR_FRAME_RATE)}f</span></div><div class="frameNudges"><button class="btn" onclick="editorNudgeFrames('start',-5)">−5f</button><button class="btn" onclick="editorNudgeFrames('start',-1)">−1f</button><button class="btn" onclick="editorNudgeFrames('start',1)">+1f</button><button class="btn" onclick="editorNudgeFrames('start',5)">+5f</button></div></div><div class="inspectorGroup"><label>OUT / END</label><div class="timeField"><input class="input" type="number" step="${f.toFixed(6)}" value="${Number(s.end).toFixed(3)}" onchange="editorUpdate('end',this.value)"><span class="chip">${Math.round(Number(s.end)*EDITOR_FRAME_RATE)}f</span></div><div class="frameNudges"><button class="btn" onclick="editorNudgeFrames('end',-5)">−5f</button><button class="btn" onclick="editorNudgeFrames('end',-1)">−1f</button><button class="btn" onclick="editorNudgeFrames('end',1)">+1f</button><button class="btn" onclick="editorNudgeFrames('end',5)">+5f</button></div></div></div><div class="inspectorRow"><div class="inspectorGroup"><label>ROLE</label><select class="select" onchange="editorUpdate('role',this.value)">${['hook','problem','product_reveal','demo','benefit','social_proof','objection','before_after','cta','custom'].map(r=>`<option value="${r}" ${r===s.role?'selected':''}>${roleName(r)}</option>`).join('')}</select></div><div class="inspectorGroup"><label>AUDIO</label><select class="select" onchange="editorUpdate('audio_mode',this.value)"><option value="original" ${s.audio_mode!=='mute'?'selected':''}>Source audio</option><option value="mute" ${s.audio_mode==='mute'?'selected':''}>Muted</option></select></div></div><div class="actions" style="margin-top:9px"><button class="btn" ${EDITOR.selected<=0?'disabled':''} onclick="editorMove(${EDITOR.selected},-1)">← Move earlier</button><button class="btn" ${EDITOR.selected>=EDITOR.segments.length-1?'disabled':''} onclick="editorMove(${EDITOR.selected},1)">Move later →</button></div><div class="actions" style="margin-top:6px"><button class="btn blue" onclick="editorPreview(false)">▶ Watch cut</button><button class="btn" onclick="editorPreview(true)">±4s context</button><button class="btn" onclick="editorDuplicate(${EDITOR.selected})">Duplicate</button><button class="btn danger" onclick="editorDelete(${EDITOR.selected})">Delete</button>${src.tiktok_url?`<a class="btn" target="_blank" href="${esc(src.tiktok_url)}">TikTok ↗</a>`:''}</div><details style="margin-top:10px"><summary class="small">Advanced clip details</summary><div class="inspectorGroup"><label>CAPTION / TRANSCRIPT</label><textarea class="input" rows="3" onchange="editorUpdate('text',this.value)">${esc(s.text||'')}</textarea></div></details>`}
function renderAdEditor(){if(!EDITOR)return;const content=document.getElementById('editorContent'),a=EDITOR.base,total=editorTotal(),dirty=EDITOR.dirty;content.innerHTML=`<div class="nativeEditorStage"><section id="editorLibrary" class="nativePanel nativeMediaBin"></section><section class="nativePanel nativePreviewPane"><div class="nativePanelHead"><div><div class="nativePanelTitle">Preview</div><div class="small">${dirty?'Timeline changed — preview is the last rendered version':'Current rendered version'}</div></div><span class="chip ${dirty?'warn':'good'}">${dirty?'UNSAVED':'RENDERED'}</span></div><div class="nativePreviewWrap">${a.media_url?`<video id="editorPreviewVideo" controls playsinline preload="metadata" src="${esc(a.media_url)}" ontimeupdate="editorPreviewTime(this.currentTime)"></video>`:'<div class="empty">Rendered preview unavailable.</div>'}<div class="previewStatus">${dirty?'<span class="dirtyDot"></span> Last render':'● Rendered'} · ${esc(EDITOR.parentName)}</div></div><div class="nativeTransport"><button class="btn" onclick="editorJump(-1)">−1s</button><button class="btn primary" onclick="editorTogglePlay()">▶ / ❚❚</button><button class="btn" onclick="editorJump(1)">+1s</button><span id="editorTimecode" class="transportTime">${fmt(EDITOR.playhead||0)} / ${fmt(total)}</span></div></section><section class="nativePanel nativeInspector"><div class="nativePanelHead"><div class="nativePanelTitle">Inspector</div><span class="small">clip ${EDITOR.selected+1}/${EDITOR.segments.length}</span></div><div class="nativeInspectorBody">${editorInspectorNative()}<div class="nativeAi"><div class="nativePanelTitle">AI Creative Director</div><div class="small" style="margin-top:3px">Tell it exactly what to keep, change, add, remove, reorder, or how long the ad should be. If your direction is vague, it will ask for precision before editing.</div>${renderEditorAiChat()}<div class="actions"><button class="btn purple" onclick="aiIterateEditor('iterate')">AI iterate</button><button class="btn purple" onclick="aiIterateEditor('regenerate')">AI regenerate</button></div></div></div></section></div>${editorTimelineHtml()}<div class="nativeBottomBar"><div><div class="small">${dirty?'Unsaved timeline changes':'No unsaved timeline changes'} · ${EDITOR.segments.length} clips · <span id="nativeTimelineTotal">${fmt(total)}</span></div><div class="small">Every render creates a new version. Undo/redo remains local until you render.</div></div><div class="actions"><input id="editorLabel" class="input" style="width:240px" placeholder="Version note (optional)"><button class="btn primary" onclick="renderEditorVersion()">Render new version</button></div></div>`;renderEditorLibrary()}

function editorPayload(){return (EDITOR?.segments||[]).map(s=>({video_id:s.video_id,segment_id:s.segment_id||'',creator:s.creator||'',tiktok_url:s.tiktok_url||'',start:Number(s.start),end:Number(s.end),gap_before:Number(s.gap_before||0),role:s.role||'custom',score:Number(s.score||0),raw_score:Number(s.raw_score||0),product_match_score:Number(s.product_match_score||0),product_visible:!!s.product_visible,text:s.text||'',reason:s.reason||'',audio_mode:s.audio_mode||'original',audio_segment_id:s.audio_segment_id||'',audio_start:s.audio_start,audio_end:s.audio_end,audio_creator:s.audio_creator||'',audio_tiktok_url:s.audio_tiktok_url||''}))}
async function renderEditorVersion(){if(!EDITOR)return;const gaps=editorGapTotal();if(gaps>.01&&!confirm(`This timeline has ${gaps.toFixed(2)}s of empty gap. It will render as black/silent space. Render anyway?`))return;try{const label=document.getElementById('editorLabel')?.value||'';await api('/api/editor/render',{ad_name:EDITOR.parentName,segments:editorPayload(),label});closeAdEditor();openView('ads')}catch(e){alert(e.message)}}
function editorCollectedDirection(){if(!EDITOR)return '';if(EDITOR.aiDirection)return EDITOR.aiDirection;return (EDITOR.aiMessages||[]).filter(m=>m.role==='user').map(m=>m.content).join('\n').trim()}
async function aiIterateEditor(mode){if(!EDITOR)return;const instruction=editorCollectedDirection();if(!instruction&&!confirm('No specific direction was provided. Generate a general '+(mode==='regenerate'?'regeneration':'iteration')+' anyway?')){document.getElementById('editorAiInput')?.focus();return}try{await api('/api/editor/ai',{ad_name:EDITOR.parentName,instruction,mode,segments:editorPayload()});closeAdEditor();openView('ads')}catch(e){alert(e.message)}}
function quickAiIteration(name,mode){openAdEditor(name,mode)}

let modalEnd=null;
function openVideoModal(url,start=0,end=null,title='',meta='',caption=''){if(!url)return;const m=document.getElementById('videoModal'),p=document.getElementById('modalPlayer');document.getElementById('modalTitle').textContent=title||'Video';document.getElementById('modalMeta').textContent=meta||'';document.getElementById('modalCaption').textContent=caption||'';modalEnd=end===null||end===undefined?null:Number(end);p.pause();p.src=url;p.load();p.onloadedmetadata=()=>{p.currentTime=Math.max(0,Number(start||0));p.play().catch(()=>{})};p.ontimeupdate=()=>{if(modalEnd!==null&&p.currentTime>=modalEnd){p.pause();p.currentTime=Math.max(0,modalEnd-.03)}};m.classList.add('show')}
function closeVideoModal(){const m=document.getElementById('videoModal'),p=document.getElementById('modalPlayer');p.pause();p.removeAttribute('src');p.load();modalEnd=null;m.classList.remove('show')}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeVideoModal();closeAdEditor();return}if(!EDITOR)return;const mod=e.metaKey||e.ctrlKey;if(mod&&e.key.toLowerCase()==='z'){e.preventDefault();if(e.shiftKey)editorRedo();else editorUndo();return}if(e.altKey&&e.key==='ArrowLeft'){e.preventDefault();editorNudgeFrames(e.shiftKey?'end':'start',-1);return}if(e.altKey&&e.key==='ArrowRight'){e.preventDefault();editorNudgeFrames(e.shiftKey?'end':'start',1);return}});
let pollWorkflowRefresh=false,pollFullRefresh=false;
async function poll(){try{const old=lastJobStatus,view=currentView();const r=await fetch('/api/state',{cache:'no-store'});const next=await r.json();if(!next.error){const changed=JSON.stringify(next)!==JSON.stringify(STATE);STATE=next;lastJobStatus=STATE.job.status;document.getElementById('pageTitle').textContent=STATE.product.title||'Product workspace';document.getElementById('pageSub').textContent=`${STATE.product.brand||'Unknown brand'} · ${STATE.stats.discovered} found · ${STATE.stats.downloaded} downloaded · ${STATE.stats.analysis_complete} analyzed · ${STATE.stats.ads_rendered} edited ads`;if(changed)pollWorkflowRefresh=true;if(old==='running'&&lastJobStatus!=='running')pollFullRefresh=true;const playing=[...document.querySelectorAll('video')].some(v=>!v.paused&&!v.ended);if(!playing){if(pollFullRefresh){renderAll();openView(view);pollFullRefresh=false;pollWorkflowRefresh=false}else if(view==='workflow'&&pollWorkflowRefresh){renderWorkflow();pollWorkflowRefresh=false}else if(view==='controls'&&changed)renderControls()}}}catch(e){}finally{setTimeout(poll,1500)}}
loadState().then(()=>setTimeout(poll,1500)).catch(e=>{document.getElementById('pageTitle').textContent='Dashboard error';document.getElementById('pageSub').textContent=e.message});
</script>
</body>
</html>
'''

class _DashboardHandler(BaseHTTPRequestHandler):
    context: DashboardContext

    def log_message(self, format, *args):
        return

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            self.close_connection = True
            raise ValueError("JSON request exceeds 1 MB")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _html(self, text: str, *, head_only: bool = False):
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_file(self, relative_url_path: str, *, head_only: bool = False):
        raw = unquote(relative_url_path).lstrip("/")
        normalized = posixpath.normpath(raw)
        if normalized.startswith("../") or normalized == "..":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        _product, workspace = self.context.snapshot()
        root = workspace.root.resolve()
        path = (root / normalized).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if path.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".mp3", ".wav", ".m4a", ".aac", ".ogg"} and not (path.suffix == ".json" and normalized.startswith("nextplay/") and any(part in normalized for part in ("/renders/", "/drafts/"))):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if range_header and size:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if match:
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                elif right:
                    start = max(0, size - int(right))
                    end = size - 1
                else:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                if start >= size or end < start:
                    self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    return
                partial = True
        if end < start:
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return
        length = max(0, end - start + 1)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining:
                chunk = fh.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def _state(self):
        product, workspace = self.context.snapshot()
        return build_dashboard_state(
            product,
            workspace,
            project_dir=self.context.project_dir,
            job=self.context.job_snapshot(),
            login_active=self.context.login_active(),
        )

    def _local_request(self):
        host=urlparse("http://"+self.headers.get("Host", "")).hostname
        origin=self.headers.get("Origin")
        if host not in {"localhost", "127.0.0.1", "::1"} or (origin and urlparse(origin).netloc != self.headers.get("Host")):
            self.send_error(HTTPStatus.FORBIDDEN, "Local same-origin requests only")
            return False
        return True

    def do_HEAD(self):
        if not self._local_request():
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._html(DASHBOARD_HTML, head_only=True)
            return
        if parsed.path.startswith("/file/"):
            self._send_file(parsed.path[len("/file/"):], head_only=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_GET(self):
        if not self._local_request():
            return
        parsed = urlparse(self.path)
        from .timeline_api import get_route
        if get_route(self, parsed):
            return
        if parsed.path in ("/", "/index.html"):
            self._html(DASHBOARD_HTML)
            return
        if parsed.path == "/api/source-poster":
            video_id = parse_qs(parsed.query).get("video_id", [""])[0]
            if not video_id or len(video_id) > 128:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            _product, workspace = self.context.snapshot()
            candidate = next((c for c in workspace.load_candidates() if str(c.video_id) == video_id), None)
            if candidate is None or not candidate.local_path:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                path = make_poster(workspace.root, candidate.local_path)
                body = path.read_bytes()
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
                self.send_error(HTTPStatus.NOT_FOUND, "Preview unavailable")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if parsed.path == "/api/state":
            try:
                self._json(self._state())
            except Exception as exc:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        if parsed.path.startswith("/file/"):
            self._send_file(parsed.path[len("/file/"):])
            return
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if not self._local_request():
            return
        parsed = urlparse(self.path)
        from .timeline_api import post_route
        if post_route(self, parsed):
            return
        try:
            body = self._body_json()
            product, workspace = self.context.snapshot()

            if parsed.path == "/api/review":
                kind = str(body.get("kind") or "")
                if kind == "segment":
                    kind = "segments"
                if kind == "ad":
                    kind = "ads"
                entry = update_review_item(
                    workspace,
                    kind=kind,
                    item_id=str(body.get("id") or ""),
                    status=body.get("status") if "status" in body else None,
                    favorite=body.get("favorite") if "favorite" in body else None,
                    note=body.get("note") if "note" in body else None,
                    caption=body.get("caption") if "caption" in body else None,
                )
                self._json({"ok": True, "review": entry})
                return

            if parsed.path == "/api/rights":
                entry = update_source_rights(
                    workspace,
                    str(body.get("video_id") or ""),
                    status=str(body.get("status") or "unknown"),
                    note=str(body.get("note") or ""),
                )
                self._json({"ok": True, "rights": entry})
                return

            if parsed.path == "/api/settings":
                settings = update_studio_settings(workspace, body)
                self._json({"ok": True, "settings": settings})
                return

            if parsed.path == "/api/workspace/switch":
                if self.context.job_snapshot().get("status") == "running":
                    raise RuntimeError("Wait for the running pipeline job to finish before switching workspaces.")
                self.context.switch_workspace(body.get("path") or "")
                self._json({"ok": True})
                return

            if parsed.path == "/api/login/start":
                self.context.start_login()
                self._json({"ok": True})
                return

            if parsed.path == "/api/login/finish":
                self.context.stop_login()
                self._json({"ok": True})
                return

            if parsed.path == "/api/product/create":
                if self.context.login_active():
                    raise RuntimeError("Click 'I’m logged in' to close the TikTok login window before creating a product workspace.")
                url = str(body.get("url") or "").strip()
                if not url.startswith(("https://", "http://")):
                    raise ValueError("Paste a valid TikTok Shop URL.")

                def create_product():
                    pipeline = ProductPipeline(self.context.project_dir)
                    print("Creating product workspace...")
                    new_product, new_workspace = pipeline.ingest(url, interactive=False, download_image=True)
                    print(f"Workspace ready: {new_workspace.root}")
                    with self.context.lock:
                        self.context.product, self.context.workspace = new_product, new_workspace

                job = self.context.start_job("create_product", create_product)
                self._json({"ok": True, "job": job})
                return

            if parsed.path == "/api/custom-ad":
                ids = [str(x) for x in body.get("segment_ids") or []]
                if len(ids) < 2:
                    raise ValueError("Choose at least two segments for a custom ad.")
                state = build_dashboard_state(product, workspace)
                settings = load_studio_settings(workspace)
                by_id = {s["segment_id"]: s for s in state["segments"]}
                chosen = []
                total = 0.0
                max_duration = float(settings["max_duration"])
                for seg_id in ids[:12]:
                    seg = by_id.get(seg_id)
                    if not seg or not seg.get("media_url"):
                        continue
                    status = str((seg.get("rights") or {}).get("status") or "unknown")
                    if settings.get("require_cleared_rights") and status not in {"licensed", "approved_for_ads"}:
                        raise ValueError(f"{seg_id} is not rights-cleared. Change its Rights status or disable rights-only mode.")
                    analysis = next((a for a in state["analyses"] if a["video_id"] == seg["video_id"]), None)
                    if not analysis or not analysis.get("local_path"):
                        continue
                    duration = max(0.0, _safe_float(seg["end"]) - _safe_float(seg["start"]))
                    if total + duration > max_duration:
                        remaining = max_duration - total
                        if remaining < 0.6:
                            break
                        end = _safe_float(seg["start"]) + remaining
                    else:
                        end = _safe_float(seg["end"])
                    chosen.append({
                        "video_path": analysis["local_path"], "video_id": seg["video_id"],
                        "creator": seg.get("creator", ""), "tiktok_url": seg.get("tiktok_url", ""),
                        "start": _safe_float(seg["start"]), "end": end,
                        "role": seg.get("role") or "segment", "segment_id": seg_id,
                        "score": _safe_float(seg.get("score")), "raw_score": _safe_float(seg.get("score")),
                        "product_match_score": _safe_float(seg.get("product_match_score")),
                        "product_visible": bool(seg.get("product_visible")),
                        "text": seg.get("text") or "", "reason": seg.get("reason") or "",
                        "rights_status": status,
                    })
                    total += end - _safe_float(seg["start"])
                if len(chosen) < 2:
                    raise ValueError("The selected clips could not be resolved to local source videos.")
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                name = f"custom_{stamp}"
                render = dict(settings)
                if render.get("headline_enabled") and not render.get("headline_text"):
                    render["headline_text"] = str(chosen[0].get("text") or "")[:92]
                plan = {
                    "name": name, "template": "custom_review", "strategy": "manual_custom",
                    "duration_seconds": round(total, 3), "target_duration_seconds": settings["target_duration"],
                    "segments": chosen, "output_path": str(workspace.ads_dir / f"{name}.mp4"),
                    "render": render,
                    "rights_notice": "Clear source creator usage rights before commercial publication.",
                }

                def render_custom():
                    print(f"Rendering custom ad from {len(chosen)} selected clips...")
                    render_ad_plans([plan])
                    plans_path = workspace.ads_dir / "ad_plans.json"
                    plans = read_json(plans_path, []) or []
                    plans.append(plan)
                    atomic_write_json(plans_path, plans)
                    print(f"Custom ad ready: {plan['output_path']}")

                job = self.context.start_job("custom_ad", render_custom)
                self._json({"ok": True, "job": job, "name": name})
                return

            if parsed.path == "/api/editor/render":
                ad_name = str(body.get("ad_name") or "").strip()
                segments = body.get("segments") or []
                label = str(body.get("label") or "")[:240]
                if not ad_name:
                    raise ValueError("Choose an ad to edit first.")

                def render_edited_ad():
                    current_product, current_workspace = self.context.snapshot()
                    print(f"Rendering non-destructive edit of {ad_name}...")
                    plan = render_editor_version(current_workspace, ad_name, segments, label=label)
                    print(f"Edited ad ready: {plan['output_path']}")

                job = self.context.start_job("edit_ad", render_edited_ad)
                self._json({"ok": True, "job": job})
                return

            if parsed.path == "/api/editor/restore":
                ad_name = str(body.get("ad_name") or "").strip()
                restore_name = str(body.get("restore_name") or "").strip()
                label = str(body.get("label") or "")[:240]
                if not ad_name or not restore_name:
                    raise ValueError("Choose both the current ad and the previous version to restore.")

                def render_restored_ad():
                    _product, current_workspace = self.context.snapshot()
                    print(f"Restoring {restore_name} as a new version of {ad_name}...")
                    plan = restore_ad_version(current_workspace, ad_name, restore_name, label=label)
                    print(f"Restored ad version ready: {plan['output_path']}")

                job = self.context.start_job("restore_ad_version", render_restored_ad)
                self._json({"ok": True, "job": job})
                return

            if parsed.path == "/api/editor/ai/chat":
                ad_name = str(body.get("ad_name") or "").strip()
                messages = body.get("messages") or []
                if not ad_name:
                    raise ValueError("Choose an ad first.")
                if not isinstance(messages, list):
                    raise ValueError("AI editor messages must be a list.")
                current_product, current_workspace = self.context.snapshot()
                result = refine_ai_direction(current_product, current_workspace, ad_name, messages, draft_segments=body.get("segments") or None)
                self._json({"ok": True, **result})
                return

            if parsed.path == "/api/editor/ai":
                ad_name = str(body.get("ad_name") or "").strip()
                instruction = str(body.get("instruction") or "")[:2400]
                mode = str(body.get("mode") or "iterate")
                base_segments = body.get("segments") or None
                if not ad_name:
                    raise ValueError("Choose an ad first.")

                def render_ai_iteration():
                    current_product, current_workspace = self.context.snapshot()
                    print(f"Creating AI {mode} version of {ad_name}...")
                    plan = ai_iterate_ad(current_product, current_workspace, ad_name, instruction=instruction, mode=mode, base_segments=base_segments)
                    info = plan.get("ai_iteration") or {}
                    print(info.get("summary") or "AI iteration created.")
                    print(f"AI ad version ready: {plan['output_path']}")

                job = self.context.start_job("ai_ad_iteration", render_ai_iteration)
                self._json({"ok": True, "job": job})
                return

            if parsed.path == "/api/job":
                action = str(body.get("action") or "")
                allowed = {
                    "discover", "download", "analyze", "build_ads", "run_remaining", "rebuild_reviewed",
                    "source_check", "build_ab_tests", "quality_review", "auto_improve",
                }
                if action not in allowed:
                    raise ValueError("Unknown dashboard job.")
                if self.context.login_active() and action in {"discover", "download", "run_remaining"}:
                    raise RuntimeError("Finish the TikTok login window first, then start this stage.")

                def build_creative_plans(current_workspace, *, reviewed: bool = True, ab: bool = False):
                    settings = load_studio_settings(current_workspace)
                    reviews = load_review_state(current_workspace) if reviewed else None
                    rights = load_rights_state(current_workspace)
                    source_q = load_source_quality(current_workspace)
                    kwargs = dict(
                        max_duration=float(settings["max_duration"]),
                        target_duration=float(settings["target_duration"]),
                        review_state=reviews,
                        rights_state=rights,
                        source_quality=source_q,
                        product_match_min=float(settings["product_match_min"]),
                        video_relevance_min=float(settings["video_relevance_min"]),
                        require_cleared_rights=bool(settings["require_cleared_rights"]),
                        render_settings=settings,
                    )
                    if ab:
                        return build_ab_test_plans(
                            current_workspace.analysis_dir,
                            current_workspace.ads_dir,
                            count=int(settings["ab_variants"]),
                            **kwargs,
                        )
                    return build_ad_plans(current_workspace.analysis_dir, current_workspace.ads_dir, count=10, **kwargs)

                def run_action():
                    current_product, current_workspace = self.context.snapshot()
                    settings = load_studio_settings(current_workspace)
                    pipeline = ProductPipeline(self.context.project_dir)
                    if action == "discover":
                        pipeline.discover(current_product, current_workspace, prompt_login=False)
                        return
                    if action == "source_check":
                        candidates = current_workspace.load_candidates()
                        evaluate_candidate_thumbnails(current_workspace, candidates, limit=max(60, int(settings["download_limit"]) * 3))
                        update_from_analyses(current_workspace)
                        print("Source-quality data refreshed.")
                        return
                    if action == "download":
                        pipeline.download(current_product, current_workspace, min_score=0.45, limit=int(settings["download_limit"]))
                        prepare_analysis_jobs(current_product, current_workspace.load_candidates(), current_workspace.analysis_dir)
                        return
                    if action == "analyze":
                        prepare_analysis_jobs(current_product, current_workspace.load_candidates(), current_workspace.analysis_dir)
                        analyze_jobs(current_workspace.analysis_dir, provider="openai")
                        update_from_analyses(current_workspace)
                        return
                    if action in {"build_ads", "rebuild_reviewed"}:
                        if action == "rebuild_reviewed":
                            archive = _archive_ads(current_workspace)
                            if archive:
                                print(f"Archived previous ads to: {archive}")
                        plans = build_creative_plans(current_workspace, reviewed=(action == "rebuild_reviewed"))
                        atomic_write_json(current_workspace.ads_dir / "ad_plans.json", plans)
                        current_review = load_review_state(current_workspace)
                        current_review["ads"] = {}
                        save_review_state(current_workspace, current_review)
                        render_ad_plans(plans)
                        return
                    if action == "build_ab_tests":
                        print("Building controlled A/B variants; one creative variable changes at a time...")
                        plans = build_creative_plans(current_workspace, reviewed=True, ab=True)
                        plans_path = current_workspace.ads_dir / "ad_plans.json"
                        existing = read_json(plans_path, []) or []
                        # Replace prior A/B plans while preserving normal/custom/repair ads.
                        existing = [p for p in existing if p.get("strategy") != "ab_test"]
                        atomic_write_json(plans_path, existing + plans)
                        render_ad_plans(plans)
                        return
                    if action == "quality_review":
                        provider = "openai" if os.getenv("OPENAI_API_KEY") else "heuristic"
                        review_ads(current_product, current_workspace, provider=provider, force=False)
                        return
                    if action == "auto_improve":
                        provider = "openai" if os.getenv("OPENAI_API_KEY") else "heuristic"
                        # Make sure the finished ads have been judged before targeted repair.
                        review_ads(current_product, current_workspace, provider=provider, force=False)
                        improved = auto_improve_ads(
                            current_product,
                            current_workspace,
                            threshold=int(settings["quality_threshold"]),
                            review_provider=provider,
                            max_ads=10,
                        )
                        print(f"Created {len(improved)} repaired ad version(s).")
                        return
                    if action == "run_remaining":
                        candidates = current_workspace.load_candidates()
                        if not candidates:
                            print("Stage 1/5: discovering TikTok videos...")
                            candidates = pipeline.discover(current_product, current_workspace, prompt_login=False)
                        print("Stage 2/5: checking source thumbnails / duplicates...")
                        evaluate_candidate_thumbnails(current_workspace, candidates, limit=max(60, int(settings["download_limit"]) * 3))
                        downloaded = sum(c.download_status == "downloaded" for c in candidates)
                        if downloaded < min(int(settings["download_limit"]), len(candidates)):
                            print("Stage 3/5: downloading top ranked videos...")
                            candidates = pipeline.download(current_product, current_workspace, min_score=0.45, limit=int(settings["download_limit"]))
                        print("Stage 4/5: analyzing downloaded videos...")
                        prepare_analysis_jobs(current_product, current_workspace.load_candidates(), current_workspace.analysis_dir)
                        completed = analyze_jobs(current_workspace.analysis_dir, provider="openai")
                        update_from_analyses(current_workspace)
                        if not completed:
                            raise RuntimeError("No completed analyses are available; check the job log above.")
                        if len(list(current_workspace.ads_dir.glob("*.mp4"))) < 10:
                            print("Stage 5/5: generating polished ads...")
                            plans = build_creative_plans(current_workspace, reviewed=True)
                            atomic_write_json(current_workspace.ads_dir / "ad_plans.json", plans)
                            render_ad_plans(plans)
                        else:
                            print("Stage 5/5: ads already rendered; use Creative Lab for QA/A-B/rebuild work.")

                job = self.context.start_job(action, run_action)
                self._json({"ok": True, "job": job})
                return

            self._json({"error": "Not found"}, status=404)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, status=400)


def serve_dashboard(product, workspace: ProductWorkspace, *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True):
    context = DashboardContext(product, workspace)
    handler = type("ProductDashboardHandler", (_DashboardHandler,), {"context": context})
    server = ThreadingHTTPServer((host, int(port)), handler)
    url = f"http://{host}:{server.server_port}/"
    print("\nNextPlay UGC Workflow is ready:")
    print(f"  {url}")
    print("  Review, approve/reject clips, render custom ads, and run pipeline stages here.")
    print("  Press Control+C in this Terminal when you want to stop the dashboard.\n")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        context.stop_login()
        server.server_close()
    return url
