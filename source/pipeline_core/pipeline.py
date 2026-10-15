from __future__ import annotations

from pathlib import Path

from .analysis_jobs import prepare_analysis_jobs
from .browser import init_browser
from .discovery import discover_tiktok_videos, merge_candidates
from .downloader import download_ranked
from .models import Product
from .product import fetch_product_with_browser, fetch_product_with_http, load_fixture, save_primary_image
from .ranking import rank_candidates
from .search_plan import build_search_plan
from .source_quality import evaluate_candidate_thumbnails
from .storage import ProductWorkspace
from .visual import apply_visual_scores


class ProductPipeline:
    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir).resolve()
        self.products_dir = self.project_dir / "products"
        self.profile_dir = self.project_dir / ".browser-profile"

    def ingest(
        self,
        shop_url: str,
        *,
        fixture: str | None = None,
        allow_browser_fallback: bool = True,
        interactive: bool = True,
        download_image: bool = True,
    ) -> tuple[Product, ProductWorkspace]:
        driver = None
        try:
            if fixture:
                product = load_fixture(fixture, shop_url)
            else:
                try:
                    product = fetch_product_with_http(shop_url)
                except Exception as http_error:
                    if not allow_browser_fallback:
                        raise
                    print(f"HTTP product ingest did not work ({http_error}). Falling back to Chromium...")
                    temp_download = self.project_dir / ".browser-downloads"
                    driver = init_browser(self.profile_dir, temp_download, headless=False)
                    product = fetch_product_with_browser(shop_url, driver, interactive=interactive)

            plan = build_search_plan(product)
            workspace = ProductWorkspace(self.products_dir, product).ensure()
            if download_image:
                suffix = ".jpg"
                if product.image_urls and ".webp" in product.image_urls[0].lower():
                    suffix = ".webp"
                save_primary_image(product, workspace.root / f"product{suffix}", driver=driver)
            workspace.save_product(product)
            workspace.save_search_plan(plan)
            return product, workspace
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    def discover(
        self,
        product: Product,
        workspace: ProductWorkspace,
        *,
        scrolls_per_query: int = 6,
        max_per_query: int = 40,
        prompt_login: bool = True,
        visual_rank: bool = False,
        visual_limit: int = 120,
    ):
        plan = workspace.load_search_plan()
        driver = init_browser(self.profile_dir, workspace.raw_dir, headless=False)
        try:
            if prompt_login:
                driver.get("https://www.tiktok.com/")
                print("\nChromium is ready for TikTok discovery.")
                print("Log in if needed. This project uses its own persistent browser profile.")
                input("Press ENTER to start searches...")
            found = discover_tiktok_videos(
                driver,
                plan,
                scrolls_per_query=scrolls_per_query,
                max_per_query=max_per_query,
                debug_dir=workspace.debug_dir,
            )
            merged = merge_candidates(workspace.load_candidates(), found)
            ranked = rank_candidates(product, plan, merged)
            if visual_rank:
                if not product.primary_image_path:
                    print("Visual ranking requested, but no local product image was available; keeping text-only scores.")
                else:
                    print("\nRunning optional CLIP product-vs-thumbnail ranking...")
                    apply_visual_scores(
                        Path(product.primary_image_path),
                        ranked,
                        workspace.thumbnails_dir,
                        limit=visual_limit,
                    )
                    ranked = rank_candidates(product, plan, ranked)
            workspace.save_candidates(ranked)
            return ranked
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def download(
        self,
        product: Product,
        workspace: ProductWorkspace,
        *,
        min_score: float = 0.45,
        limit: int | None = None,
        dedupe_thumbnails: bool = True,
    ):
        plan = workspace.load_search_plan()
        candidates = rank_candidates(product, plan, workspace.load_candidates())
        excluded_ids: set[str] = set()
        if dedupe_thumbnails and candidates:
            print("Pre-download source check: fetching top thumbnails and filtering obvious near-duplicates...")
            quality = evaluate_candidate_thumbnails(workspace, candidates, limit=max(60, (limit or 30) * 3))
            excluded_ids = {video_id for video_id, row in (quality.get("sources") or {}).items() if row.get("duplicate_of")}
            if excluded_ids:
                print(f"  skipped {len(excluded_ids)} obvious near-duplicate thumbnail candidate(s)")
        driver = init_browser(self.profile_dir, workspace.raw_dir, headless=False)
        try:
            candidates = download_ranked(
                driver,
                candidates,
                workspace.raw_dir,
                workspace.quarantine_dir,
                min_score=min_score,
                limit=limit,
                excluded_ids=excluded_ids,
            )
            workspace.save_candidates(candidates)
            return candidates
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def prepare_analysis(self, product: Product, workspace: ProductWorkspace):
        return prepare_analysis_jobs(product, workspace.load_candidates(), workspace.analysis_dir)
