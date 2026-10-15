from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from pipeline_core.ad_builder import build_ab_test_plans, build_ad_plans, render_ad_plans
from pipeline_core.analysis_jobs import prepare_analysis_jobs
from pipeline_core.analyzer import analyze_jobs
from pipeline_core.dashboard import serve_dashboard
from pipeline_core.creative_quality import auto_improve_ads, review_ads
from pipeline_core.review_state import load_review_state
from pipeline_core.rights import load_rights_state
from pipeline_core.source_quality import load_source_quality, update_from_analyses
from pipeline_core.studio_settings import load_studio_settings
from pipeline_core.pipeline import ProductPipeline
from pipeline_core.storage import load_product_workspace

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def _print_summary(product, workspace):
    plan = workspace.load_search_plan()
    print("\nProduct ready")
    print(f"  title: {product.title}")
    print(f"  brand: {product.brand or '(unknown)'}")
    print(f"  product id: {product.product_id}")
    print(f"  workspace: {workspace.root}")
    print(f"  search queries: {len(plan.queries)}")
    for query in plan.queries:
        print(f"    - {query}")
    print(f"  hashtags: {', '.join(plan.hashtags)}")



def _creative_kwargs(workspace, *, reviewed=True):
    settings = load_studio_settings(workspace)
    return settings, {
        "max_duration": float(settings["max_duration"]),
        "target_duration": float(settings["target_duration"]),
        "review_state": load_review_state(workspace) if reviewed else None,
        "rights_state": load_rights_state(workspace),
        "source_quality": load_source_quality(workspace),
        "product_match_min": float(settings["product_match_min"]),
        "video_relevance_min": float(settings["video_relevance_min"]),
        "require_cleared_rights": bool(settings["require_cleared_rights"]),
        "render_settings": settings,
    }

def cmd_ingest(args):
    pipeline = ProductPipeline(PROJECT_DIR)
    product, workspace = pipeline.ingest(
        args.url,
        fixture=args.fixture,
        allow_browser_fallback=not args.no_browser_fallback,
        interactive=not args.no_prompt,
        download_image=not args.no_image,
    )
    _print_summary(product, workspace)


def cmd_discover(args):
    product, workspace = load_product_workspace(args.workspace)
    pipeline = ProductPipeline(PROJECT_DIR)
    candidates = pipeline.discover(
        product,
        workspace,
        scrolls_per_query=args.scrolls,
        max_per_query=args.max_per_query,
        prompt_login=not args.no_prompt,
        visual_rank=args.visual_rank,
        visual_limit=args.visual_limit,
    )
    print(f"\nSaved {len(candidates)} unique candidates to {workspace.videos_json}")
    for candidate in candidates[:15]:
        print(f"  {candidate.combined_score:.3f}  {candidate.url}")


def cmd_download(args):
    product, workspace = load_product_workspace(args.workspace)
    pipeline = ProductPipeline(PROJECT_DIR)
    candidates = pipeline.download(product, workspace, min_score=args.min_score, limit=args.limit)
    downloaded = sum(c.download_status == "downloaded" for c in candidates)
    failed = sum(c.download_status == "failed" for c in candidates)
    print(f"\nDownloaded total: {downloaded}; failed: {failed}")
    print(f"Raw videos: {workspace.raw_dir}")


def cmd_prepare_analysis(args):
    product, workspace = load_product_workspace(args.workspace)
    jobs = prepare_analysis_jobs(product, workspace.load_candidates(), workspace.analysis_dir)
    print(f"Prepared {len(jobs)} analysis job(s) in {workspace.analysis_dir}")
    print("A third-party analyzer only needs to fill provider/transcript/segments in each JSON file.")



def cmd_analyze(args):
    product, workspace = load_product_workspace(args.workspace)
    jobs = prepare_analysis_jobs(product, workspace.load_candidates(), workspace.analysis_dir)
    print(f"Analysis queue contains {len(jobs)} downloaded video job(s).")
    completed = analyze_jobs(
        workspace.analysis_dir,
        provider=args.provider,
        limit=args.limit,
        force=args.force,
        analysis_model=args.analysis_model,
        transcription_model=args.transcription_model,
        frame_count=args.frames,
    )
    print(f"Completed/available analyses: {len(completed)}")
    update_from_analyses(workspace)
    print(f"Analysis directory: {workspace.analysis_dir}")


def cmd_finish(args):
    product, workspace = load_product_workspace(args.workspace)
    jobs = prepare_analysis_jobs(product, workspace.load_candidates(), workspace.analysis_dir)
    print(f"Prepared/found {len(jobs)} analysis job(s).")
    completed = analyze_jobs(
        workspace.analysis_dir, provider=args.provider, limit=args.limit, force=args.force,
        analysis_model=args.analysis_model, transcription_model=args.transcription_model, frame_count=args.frames,
    )
    print(f"Completed/available analyses: {len(completed)}")
    update_from_analyses(workspace)
    if not completed:
        print("No analyses completed successfully, so no ads were built.")
        return
    settings, kwargs = _creative_kwargs(workspace, reviewed=True)
    # CLI override remains available while all other creative controls come from studio_settings.json.
    kwargs["max_duration"] = float(args.max_duration or settings["max_duration"])
    plans = build_ad_plans(workspace.analysis_dir, workspace.ads_dir, count=args.count, **kwargs)
    plan_path = workspace.ads_dir / "ad_plans.json"
    plan_path.write_text(json.dumps(plans, indent=2) + "\n", encoding="utf-8")
    print(f"Created {len(plans)} ad plan(s): {plan_path}")
    if not args.plan_only:
        outputs = render_ad_plans(plans)
        print("Rendered ads:")
        for output in outputs:
            print(f"  {output}")

def cmd_build_ads(args):
    _product, workspace = load_product_workspace(args.workspace)
    settings, kwargs = _creative_kwargs(workspace, reviewed=True)
    kwargs["max_duration"] = float(args.max_duration or settings["max_duration"])
    plans = build_ad_plans(workspace.analysis_dir, workspace.ads_dir, count=args.count, **kwargs)
    plan_path = workspace.ads_dir / "ad_plans.json"
    plan_path.write_text(json.dumps(plans, indent=2) + "\n", encoding="utf-8")
    print(f"Created {len(plans)} ad plan(s): {plan_path}")
    if not args.plan_only:
        outputs = render_ad_plans(plans)
        print("Rendered:")
        for output in outputs:
            print(f"  {output}")


def cmd_ab_tests(args):
    _product, workspace = load_product_workspace(args.workspace)
    settings, kwargs = _creative_kwargs(workspace, reviewed=True)
    plans = build_ab_test_plans(workspace.analysis_dir, workspace.ads_dir, count=args.count or settings["ab_variants"], **kwargs)
    plan_path = workspace.ads_dir / "ad_plans.json"
    existing = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else []
    # Replace prior A/B definitions atomically. Render first so a renderer failure
    # never leaves phantom A/B plans in the project state.
    existing = [p for p in existing if p.get("strategy") != "ab_test"]
    if not args.plan_only:
        render_ad_plans(plans)
    plan_path.write_text(json.dumps(existing + plans, indent=2) + "\n", encoding="utf-8")
    print(f"Created {len(plans)} controlled A/B ad variant(s).")


def cmd_quality(args):
    product, workspace = load_product_workspace(args.workspace)
    results = review_ads(product, workspace, provider=args.provider, force=args.force, limit=args.limit)
    print(f"Reviewed {len(results)} rendered ad(s).")


def cmd_auto_improve(args):
    product, workspace = load_product_workspace(args.workspace)
    provider = args.provider
    review_ads(product, workspace, provider=provider, force=False)
    improved = auto_improve_ads(product, workspace, threshold=args.threshold, review_provider=provider, max_ads=args.limit)
    print(f"Created {len(improved)} repaired ad version(s).")



def cmd_dashboard(args):
    product, workspace = load_product_workspace(args.workspace)
    serve_dashboard(
        product,
        workspace,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )

def cmd_run(args):
    pipeline = ProductPipeline(PROJECT_DIR)
    product, workspace = pipeline.ingest(
        args.url,
        fixture=args.fixture,
        allow_browser_fallback=True,
        interactive=True,
        download_image=True,
    )
    _print_summary(product, workspace)
    candidates = pipeline.discover(
        product,
        workspace,
        scrolls_per_query=args.scrolls,
        max_per_query=args.max_per_query,
        prompt_login=True,
        visual_rank=args.visual_rank,
        visual_limit=args.visual_limit,
    )
    print(f"\nDiscovered {len(candidates)} unique videos.")
    if args.download:
        candidates = pipeline.download(product, workspace, min_score=args.min_score, limit=args.limit)
        downloaded = sum(c.download_status == "downloaded" for c in candidates)
        print(f"Downloaded {downloaded} video(s) into {workspace.raw_dir}")
        jobs = pipeline.prepare_analysis(product, workspace)
        print(f"Prepared {len(jobs)} analysis job(s) in {workspace.analysis_dir}")
        if args.analyze or args.build_ads:
            completed = analyze_jobs(
                workspace.analysis_dir,
                provider=args.provider,
                limit=args.analysis_limit,
                force=args.force_analysis,
                analysis_model=args.analysis_model,
                transcription_model=args.transcription_model,
                frame_count=args.frames,
            )
            print(f"Completed/available analyses: {len(completed)}")
        if args.build_ads:
            update_from_analyses(workspace)
            settings, kwargs = _creative_kwargs(workspace, reviewed=True)
            kwargs["max_duration"] = float(args.max_ad_duration or settings["max_duration"])
            plans = build_ad_plans(
                workspace.analysis_dir, workspace.ads_dir, count=args.ad_count, **kwargs
            )
            plan_path = workspace.ads_dir / "ad_plans.json"
            plan_path.write_text(json.dumps(plans, indent=2) + "\n", encoding="utf-8")
            print(f"Created {len(plans)} ad plan(s): {plan_path}")
            if not args.plan_only:
                outputs = render_ad_plans(plans)
                print(f"Rendered {len(outputs)} ad(s) into {workspace.ads_dir}")
    else:
        print("Discovery complete. Run the download command after reviewing videos.json.")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Product-driven TikTok UGC discovery, ranking, downloading, and ad assembly pipeline."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Turn a TikTok Shop URL into a product workspace/search plan")
    ingest.add_argument("url")
    ingest.add_argument("--fixture", help="Use a local HTML/JSON fixture instead of the live page")
    ingest.add_argument("--no-browser-fallback", action="store_true")
    ingest.add_argument("--no-prompt", action="store_true")
    ingest.add_argument("--no-image", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    discover = sub.add_parser("discover", help="Search TikTok in Chromium and collect/rank video links")
    discover.add_argument("workspace")
    discover.add_argument("--scrolls", type=int, default=6)
    discover.add_argument("--max-per-query", type=int, default=40)
    discover.add_argument("--no-prompt", action="store_true")
    discover.add_argument("--visual-rank", action="store_true", help="Compare TikTok thumbnails to the saved product photo with CLIP")
    discover.add_argument("--visual-limit", type=int, default=120)
    discover.set_defaults(func=cmd_discover)

    download = sub.add_parser("download", help="Download ranked TikTok videos via the configured browser provider")
    download.add_argument("workspace")
    download.add_argument("--min-score", type=float, default=0.45)
    download.add_argument("--limit", type=int)
    download.set_defaults(func=cmd_download)

    analysis = sub.add_parser("prepare-analysis", help="Create provider-neutral analysis job JSON files")
    analysis.add_argument("workspace")
    analysis.set_defaults(func=cmd_prepare_analysis)

    analyze = sub.add_parser("analyze", help="Analyze downloaded videos and fill timestamped ad segments")
    analyze.add_argument("workspace")
    analyze.add_argument("--provider", choices=["openai", "mock"], default="openai")
    analyze.add_argument("--limit", type=int)
    analyze.add_argument("--force", action="store_true")
    analyze.add_argument("--frames", type=int, default=8)
    analyze.add_argument("--analysis-model", default=os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5.6-luna"))
    analyze.add_argument("--transcription-model", default=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"))
    analyze.set_defaults(func=cmd_analyze)

    ads = sub.add_parser("build-ads", help="Build/render ad variants from completed analysis JSON files")
    ads.add_argument("workspace")
    ads.add_argument("--count", type=int, default=10)
    ads.add_argument("--max-duration", type=float, default=28.0)
    ads.add_argument("--plan-only", action="store_true")
    ads.set_defaults(func=cmd_build_ads)

    finish = sub.add_parser("finish", help="Analyze all downloaded videos, build ad plans, and render ads")
    finish.add_argument("workspace")
    finish.add_argument("--provider", choices=["openai", "mock"], default="openai")
    finish.add_argument("--limit", type=int)
    finish.add_argument("--force", action="store_true")
    finish.add_argument("--frames", type=int, default=8)
    finish.add_argument("--analysis-model", default=os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5.6-luna"))
    finish.add_argument("--transcription-model", default=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"))
    finish.add_argument("--count", type=int, default=10)
    finish.add_argument("--max-duration", type=float, default=28.0)
    finish.add_argument("--plan-only", action="store_true")
    finish.set_defaults(func=cmd_finish)

    ab = sub.add_parser("ab-tests", help="Generate controlled A/B creative variants")
    ab.add_argument("workspace")
    ab.add_argument("--count", type=int)
    ab.add_argument("--plan-only", action="store_true")
    ab.set_defaults(func=cmd_ab_tests)

    quality = sub.add_parser("quality", help="Review finished ads with AI or deterministic QA")
    quality.add_argument("workspace")
    quality.add_argument("--provider", choices=["openai", "heuristic"], default="openai")
    quality.add_argument("--force", action="store_true")
    quality.add_argument("--limit", type=int)
    quality.set_defaults(func=cmd_quality)

    improve = sub.add_parser("auto-improve", help="Replace the weakest cut in ads below the quality threshold")
    improve.add_argument("workspace")
    improve.add_argument("--provider", choices=["openai", "heuristic"], default="openai")
    improve.add_argument("--threshold", type=int)
    improve.add_argument("--limit", type=int, default=10)
    improve.set_defaults(func=cmd_auto_improve)

    dashboard = sub.add_parser("dashboard", help="Open a local NextPlay-style review dashboard for a product workspace")
    dashboard.add_argument("workspace")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--no-open", action="store_true", help="Do not automatically open the browser")
    dashboard.set_defaults(func=cmd_dashboard)

    run = sub.add_parser("run", help="TikTok Shop URL -> discovery -> download -> optional AI analysis/ad rendering")
    run.add_argument("url")
    run.add_argument("--fixture")
    run.add_argument("--scrolls", type=int, default=6)
    run.add_argument("--max-per-query", type=int, default=40)
    run.add_argument("--download", action="store_true")
    run.add_argument("--visual-rank", action="store_true", help="Use optional CLIP product-photo ranking")
    run.add_argument("--visual-limit", type=int, default=120)
    run.add_argument("--min-score", type=float, default=0.45)
    run.add_argument("--limit", type=int)
    run.add_argument("--analyze", action="store_true", help="After download, analyze videos with the selected AI provider")
    run.add_argument("--build-ads", action="store_true", help="After analysis, generate and render ad variants")
    run.add_argument("--provider", choices=["openai", "mock"], default="openai")
    run.add_argument("--analysis-limit", type=int)
    run.add_argument("--force-analysis", action="store_true")
    run.add_argument("--frames", type=int, default=8)
    run.add_argument("--analysis-model", default=os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5.6-luna"))
    run.add_argument("--transcription-model", default=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"))
    run.add_argument("--ad-count", type=int, default=10)
    run.add_argument("--max-ad-duration", type=float, default=28.0)
    run.add_argument("--plan-only", action="store_true")
    run.set_defaults(func=cmd_run)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
