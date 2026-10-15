import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ffmpeg_bin import FFMPEG
from pipeline_core.ad_builder import build_ab_test_plans, build_ad_plans, render_ad_plans
from pipeline_core.ad_editor import ai_iterate_ad, refine_ai_direction, render_editor_version, restore_ad_version, sanitize_editor_segments
from pipeline_core.analysis_jobs import prepare_analysis_jobs
from pipeline_core.analyzer import analyze_jobs, resolve_timestamp_transcription_model
from pipeline_core.dashboard import DASHBOARD_HTML, build_dashboard_state
from pipeline_core.creative_quality import heuristic_quality, normalize_quality_result
from pipeline_core.rights import update_source_rights, load_rights_state
from pipeline_core.studio_settings import load_studio_settings, update_studio_settings
from pipeline_core.review_state import load_review_state, update_review_item
from pipeline_core.models import VideoCandidate
from pipeline_core.product import parse_product_html, product_id_from_url
from pipeline_core.ranking import rank_candidates
from pipeline_core.search_plan import build_search_plan
from pipeline_core.storage import ProductWorkspace

URL = "https://shop.tiktok.com/us/pdp/elroel-blanc-cover-cream-stick-v-color-changing-foundation/1732244344172417451?source=ecommerce_mall"
FIXTURE = Path(__file__).resolve().parents[1] / "testdata" / "elroel_shop_fixture.html"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.product = parse_product_html(URL, FIXTURE.read_text(encoding="utf-8"))

    def test_product_id(self):
        self.assertEqual(product_id_from_url(URL), "1732244344172417451")

    def test_elroel_parse(self):
        self.assertEqual(self.product.brand, "ELROEL")
        self.assertIn("Blanc Cover Cream Stick", self.product.title)
        self.assertIn("color-changing", self.product.description.lower())

    def test_search_plan_contains_high_signal_queries(self):
        plan = build_search_plan(self.product)
        joined = " | ".join(plan.queries).lower()
        self.assertIn("elroel", joined)
        self.assertIn("color changing foundation", joined)
        self.assertTrue(any(tag.startswith("#elroel") for tag in plan.hashtags))

    def test_ranking_prefers_matching_caption(self):
        plan = build_search_plan(self.product)
        candidates = [
            VideoCandidate(
                url="https://www.tiktok.com/@a/video/1",
                video_id="1",
                caption="ELROEL color changing foundation stick demo with built in brush",
                discovered_by=["ELROEL color changing foundation"],
            ),
            VideoCandidate(
                url="https://www.tiktok.com/@b/video/2",
                video_id="2",
                caption="cute kitchen storage container organization",
                discovered_by=["foundation"],
            ),
        ]
        ranked = rank_candidates(self.product, plan, candidates)
        self.assertEqual(ranked[0].video_id, "1")
        self.assertGreater(ranked[0].combined_score, ranked[1].combined_score)

    def test_timestamp_transcription_model_compatibility(self):
        self.assertEqual(resolve_timestamp_transcription_model("whisper-1"), "whisper-1")
        self.assertEqual(resolve_timestamp_transcription_model("gpt-4o-mini-transcribe"), "whisper-1")
        self.assertEqual(resolve_timestamp_transcription_model("gpt-transcribe"), "whisper-1")

    def test_workspace_roundtrip(self):
        plan = build_search_plan(self.product)
        with tempfile.TemporaryDirectory() as tmp:
            ws = ProductWorkspace(Path(tmp), self.product).ensure()
            ws.save_product(self.product)
            ws.save_search_plan(plan)
            self.assertTrue(ws.product_json.exists())
            saved = json.loads(ws.search_plan_json.read_text(encoding="utf-8"))
            self.assertGreater(len(saved["queries"]), 3)


    def test_dashboard_state_summarizes_workspace(self):
        plan = build_search_plan(self.product)
        with tempfile.TemporaryDirectory() as tmp:
            ws = ProductWorkspace(Path(tmp), self.product).ensure()
            ws.save_product(self.product)
            ws.save_search_plan(plan)
            raw = ws.raw_dir / "123.mp4"
            raw.write_bytes(b"fake-mp4")
            candidate = VideoCandidate(
                url="https://www.tiktok.com/@creator/video/123",
                video_id="123",
                creator="creator",
                caption="ELROEL demo",
                discovered_by=["ELROEL demo"],
                combined_score=0.91,
                download_status="downloaded",
                local_path=str(raw),
            )
            ws.save_candidates([candidate])
            prepare_analysis_jobs(self.product, [candidate], ws.analysis_dir)
            payload = json.loads((ws.analysis_dir / "123.json").read_text(encoding="utf-8"))
            payload.update({
                "status": "complete",
                "duration_seconds": 8.0,
                "video_relevance_score": 0.95,
                "segments": [
                    {"start": 0.0, "end": 1.8, "role": "hook", "score": 0.9, "product_match_score": 0.8},
                    {"start": 2.0, "end": 5.5, "role": "demo", "score": 0.88, "product_match_score": 0.95},
                ],
            })
            (ws.analysis_dir / "123.json").write_text(json.dumps(payload), encoding="utf-8")
            ad = ws.ads_dir / "ad_001_direct_response.mp4"
            ad.write_bytes(b"fake-ad")
            (ws.ads_dir / "ad_plans.json").write_text(json.dumps([{
                "name": "ad_001_direct_response",
                "template": "direct_response",
                "duration_seconds": 7.0,
                "segments": [{
                    "video_id": "123", "creator": "creator",
                    "start": 0.0, "end": 1.8, "role": "hook",
                    "tiktok_url": "https://www.tiktok.com/@creator/video/123",
                }],
                "output_path": str(ad),
            }]), encoding="utf-8")
            state = build_dashboard_state(self.product, ws)
            self.assertEqual(state["stats"]["discovered"], 1)
            self.assertEqual(state["stats"]["downloaded"], 1)
            self.assertEqual(state["stats"]["analysis_complete"], 1)
            self.assertEqual(state["stats"]["segments"], 2)
            self.assertEqual(state["stats"]["ads_rendered"], 1)
            self.assertEqual(state["role_counts"]["hook"], 1)
            self.assertTrue(state["candidates"][0]["media_url"].startswith("/file/raw/"))
            self.assertEqual(state["ads"][0]["source_count"], 1)
            self.assertTrue(state["ads"][0]["segments"][0]["source_media_url"].startswith("/file/raw/"))
            self.assertEqual(state["ads"][0]["segments"][0]["source_creator"], "creator")

    def test_dashboard_html_exposes_single_workflow_and_ad_provenance(self):
        self.assertIn("NEXTPLAY CREATIVE WORKFLOW", DASHBOARD_HTML)
        self.assertIn("Find product videos", DASHBOARD_HTML)
        self.assertIn("Review AI-selected moments", DASHBOARD_HTML)
        self.assertIn("Source videos used in this ad", DASHBOARD_HTML)
        self.assertIn("Watch cut", DASHBOARD_HTML)
        self.assertIn("Edited ads", DASHBOARD_HTML)

    def test_analysis_job_upgrade_preserves_existing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "1.mp4"
            raw.write_bytes(b"not-used-in-this-test")
            candidate = VideoCandidate(
                url="https://www.tiktok.com/@a/video/1",
                video_id="1",
                download_status="downloaded",
                local_path=str(raw),
            )
            analysis = Path(tmp) / "analysis"
            analysis.mkdir()
            old = {
                "schema_version": 1,
                "status": "pending",
                "product": {"title": self.product.title},
                "source": {"video_id": "1", "local_path": str(raw)},
                "segments": [],
            }
            (analysis / "1.json").write_text(json.dumps(old), encoding="utf-8")
            prepare_analysis_jobs(self.product, [candidate], analysis)
            upgraded = json.loads((analysis / "1.json").read_text(encoding="utf-8"))
            self.assertEqual(upgraded["schema_version"], 2)
            self.assertIn("product_match_score", upgraded["instructions"]["segment_shape"])

    def test_review_state_persists_and_dashboard_merges_it(self):
        plan = build_search_plan(self.product)
        with tempfile.TemporaryDirectory() as tmp:
            ws = ProductWorkspace(Path(tmp), self.product).ensure()
            ws.save_product(self.product)
            ws.save_search_plan(plan)
            raw = ws.raw_dir / "123.mp4"
            raw.write_bytes(b"fake-mp4")
            candidate = VideoCandidate(
                url="https://www.tiktok.com/@creator/video/123",
                video_id="123", creator="creator", combined_score=0.9,
                download_status="downloaded", local_path=str(raw),
            )
            ws.save_candidates([candidate])
            prepare_analysis_jobs(self.product, [candidate], ws.analysis_dir)
            payload = json.loads((ws.analysis_dir / "123.json").read_text(encoding="utf-8"))
            payload.update({
                "status": "complete", "duration_seconds": 5.0, "video_relevance_score": 0.9,
                "segments": [{"start": 0, "end": 2, "role": "hook", "score": 0.9, "product_match_score": 0.9}],
            })
            (ws.analysis_dir / "123.json").write_text(json.dumps(payload), encoding="utf-8")
            update_review_item(ws, kind="segments", item_id="123:0", status="approved", favorite=True)
            saved = load_review_state(ws)
            self.assertEqual(saved["segments"]["123:0"]["status"], "approved")
            state = build_dashboard_state(self.product, ws)
            self.assertTrue(state["segments"][0]["review"]["favorite"])
            self.assertEqual(state["review_summary"]["segments_approved"], 1)



class AnalysisAndAdTests(unittest.TestCase):
    def test_ad_plan_generation_from_standardized_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = root / "analysis"
            ads = root / "ads"
            analysis.mkdir()
            payload = {
                "status": "complete",
                "video_relevance_score": 0.9,
                "source": {"local_path": "/tmp/source.mp4", "video_id": "1"},
                "segments": [
                    {"start": 0, "end": 2, "role": "hook", "score": 0.9, "product_match_score": 0.8},
                    {"start": 2, "end": 7, "role": "demo", "score": 0.8, "product_match_score": 0.9},
                    {"start": 7, "end": 9, "role": "cta", "score": 0.7, "product_match_score": 0.7},
                ],
            }
            (analysis / "1.json").write_text(json.dumps(payload), encoding="utf-8")
            plans = build_ad_plans(analysis, ads, count=1)
            self.assertEqual(len(plans), 1)
            self.assertEqual([s["role"] for s in plans[0]["segments"]], ["hook", "demo", "cta"])


    def test_review_aware_ad_builder_skips_rejected_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = root / "analysis"
            ads = root / "ads"
            analysis.mkdir()
            payload = {
                "status": "complete",
                "video_relevance_score": 0.9,
                "source": {"local_path": "/tmp/source.mp4", "video_id": "1"},
                "segments": [
                    {"start": 0, "end": 2, "role": "hook", "score": 0.99, "product_match_score": 0.9},
                    {"start": 2, "end": 4, "role": "hook", "score": 0.8, "product_match_score": 0.8},
                    {"start": 4, "end": 7, "role": "demo", "score": 0.9, "product_match_score": 0.9},
                    {"start": 7, "end": 9, "role": "cta", "score": 0.8, "product_match_score": 0.8},
                ],
            }
            (analysis / "1.json").write_text(json.dumps(payload), encoding="utf-8")
            review = {"segments": {"1:0": {"status": "rejected"}, "1:1": {"favorite": True}}, "ads": {}}
            plans = build_ad_plans(analysis, ads, count=1, review_state=review)
            self.assertEqual(plans[0]["segments"][0]["segment_id"], "1:1")

    def test_mock_analysis_and_ffmpeg_render(self):
        product = parse_product_html(URL, FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw"
            analysis_dir = root / "analysis"
            ads_dir = root / "ads"
            raw_dir.mkdir()
            video = raw_dir / "999.mp4"
            proc = subprocess.run(
                [
                    FFMPEG,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=180x320:rate=30",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=48000",
                    "-t",
                    "3.0",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(video),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            candidate = VideoCandidate(
                url="https://www.tiktok.com/@test/video/999",
                video_id="999",
                download_status="downloaded",
                local_path=str(video),
            )
            prepare_analysis_jobs(product, [candidate], analysis_dir)
            completed = analyze_jobs(analysis_dir, provider="mock")
            self.assertEqual(len(completed), 1)
            analyzed = json.loads((analysis_dir / "999.json").read_text(encoding="utf-8"))
            self.assertEqual(analyzed["status"], "complete")
            self.assertGreaterEqual(len(analyzed["segments"]), 2)
            plans = build_ad_plans(analysis_dir, ads_dir, count=1, max_duration=5)
            outputs = render_ad_plans(plans)
            self.assertEqual(len(outputs), 1)
            self.assertTrue(Path(outputs[0]).exists())
            self.assertGreater(Path(outputs[0]).stat().st_size, 1000)


class StudioUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.product = parse_product_html(URL, FIXTURE.read_text(encoding="utf-8"))

    def test_settings_and_rights_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = ProductWorkspace(Path(tmp), self.product).ensure()
            ws.save_product(self.product)
            ws.save_search_plan(build_search_plan(self.product))
            settings = update_studio_settings(ws, {"target_duration": 18, "captions_enabled": False, "require_cleared_rights": True})
            self.assertEqual(settings["target_duration"], 18.0)
            self.assertFalse(load_studio_settings(ws)["captions_enabled"])
            update_source_rights(ws, "123", status="approved_for_ads", note="licensed test")
            self.assertEqual(load_rights_state(ws)["sources"]["123"]["status"], "approved_for_ads")

    def test_caption_override_flows_into_ad_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis = root / "analysis"; ads = root / "ads"; analysis.mkdir()
            payload = {
                "status": "complete", "video_relevance_score": 0.95,
                "source": {"local_path": "/tmp/source.mp4", "video_id": "1", "creator": "creator"},
                "segments": [
                    {"start": 0, "end": 2, "role": "hook", "score": .95, "product_match_score": .8, "text": "old hook"},
                    {"start": 2, "end": 5, "role": "demo", "score": .9, "product_match_score": .95, "text": "demo"},
                    {"start": 5, "end": 7, "role": "cta", "score": .8, "product_match_score": .8, "text": "cta"},
                ],
            }
            (analysis / "1.json").write_text(json.dumps(payload), encoding="utf-8")
            review = {"segments": {"1:0": {"caption": "New edited hook caption."}}, "ads": {}}
            plans = build_ad_plans(analysis, ads, count=1, review_state=review)
            self.assertEqual(plans[0]["segments"][0]["text"], "New edited hook caption.")

    def test_ab_test_plans_are_marked_as_controlled_experiments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis = root / "analysis"; ads = root / "ads"; analysis.mkdir()
            for vid, offset in [("1", 0), ("2", 1), ("3", 2)]:
                payload = {
                    "status": "complete", "video_relevance_score": .9,
                    "source": {"local_path": f"/tmp/{vid}.mp4", "video_id": vid, "creator": f"c{vid}"},
                    "segments": [
                        {"start": 0, "end": 2, "role": "hook", "score": .95-offset*.03, "product_match_score": .8, "text": f"hook {vid}"},
                        {"start": 2, "end": 5, "role": "demo", "score": .9, "product_match_score": .95, "text": f"demo {vid}"},
                        {"start": 5, "end": 7, "role": "benefit", "score": .86, "product_match_score": .9, "text": f"benefit {vid}"},
                        {"start": 7, "end": 9, "role": "cta", "score": .8, "product_match_score": .8, "text": f"cta {vid}"},
                    ],
                }
                (analysis / f"{vid}.json").write_text(json.dumps(payload), encoding="utf-8")
            plans = build_ab_test_plans(analysis, ads, count=5)
            self.assertGreaterEqual(len(plans), 3)
            self.assertTrue(all(p["strategy"] == "ab_test" for p in plans))
            self.assertTrue(all(p.get("experiment_id") for p in plans))

    def test_rights_only_builder_blocks_uncleared_then_accepts_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis = root / "analysis"; ads = root / "ads"; analysis.mkdir()
            payload = {
                "status": "complete", "video_relevance_score": .95,
                "source": {"local_path": "/tmp/source.mp4", "video_id": "1", "creator": "creator"},
                "segments": [
                    {"start": 0, "end": 2, "role": "hook", "score": .95, "product_match_score": .8, "text": "hook"},
                    {"start": 2, "end": 5, "role": "demo", "score": .9, "product_match_score": .95, "text": "demo"},
                    {"start": 5, "end": 7, "role": "cta", "score": .8, "product_match_score": .8, "text": "cta"},
                ],
            }
            (analysis / "1.json").write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_ad_plans(analysis, ads, count=1, require_cleared_rights=True, rights_state={"sources": {}})
            plans = build_ad_plans(
                analysis, ads, count=1, require_cleared_rights=True,
                rights_state={"sources": {"1": {"status": "approved_for_ads"}}},
            )
            self.assertEqual(len(plans), 1)
            self.assertTrue(all(s["rights_status"] == "approved_for_ads" for s in plans[0]["segments"]))

    def test_voiceover_broll_plan_contains_external_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis = root / "analysis"; ads = root / "ads"; analysis.mkdir()
            roles_by_video = {
                "1": [("hook", 0, 3)],
                "2": [("demo", 0, 5)],
                "3": [("benefit", 0, 5)],
                "4": [("cta", 0, 3)],
            }
            for vid, roles in roles_by_video.items():
                payload = {
                    "status": "complete", "video_relevance_score": .95,
                    "source": {"local_path": f"/tmp/{vid}.mp4", "video_id": vid, "creator": f"c{vid}"},
                    "segments": [
                        {"start": st, "end": en, "role": role, "score": .9, "product_match_score": .9, "product_visible": True, "text": f"{role} words."}
                        for role, st, en in roles
                    ],
                }
                (analysis / f"{vid}.json").write_text(json.dumps(payload), encoding="utf-8")
            plans = build_ad_plans(analysis, ads, count=6, max_duration=20)
            broll = next(p for p in plans if p["template"] == "voiceover_broll")
            self.assertTrue(any(s.get("audio_path") for s in broll["segments"]))
            self.assertTrue(any(s.get("broll_segment_id") for s in broll["segments"]))

    def test_quality_verdict_is_normalized_to_numeric_score(self):
        fixed = normalize_quality_result({"overall": 6, "verdict": "usable"})
        self.assertEqual(fixed["verdict"], "rebuild")
        self.assertEqual(fixed["model_verdict"], "usable")

    def test_heuristic_finished_ad_quality(self):
        plan = {
            "target_duration_seconds": 20,
            "segments": [
                {"start": 0, "end": 3, "role": "hook", "product_match_score": .8, "product_visible": True, "video_id": "1", "creator": "a"},
                {"start": 3, "end": 8, "role": "demo", "product_match_score": .95, "product_visible": True, "video_id": "2", "creator": "b"},
                {"start": 8, "end": 12, "role": "benefit", "product_match_score": .9, "product_visible": True, "video_id": "3", "creator": "c"},
                {"start": 12, "end": 15, "role": "cta", "product_match_score": .8, "product_visible": True, "video_id": "4", "creator": "d"},
            ],
        }
        quality = heuristic_quality(plan)
        self.assertGreaterEqual(quality["overall"], 70)
        self.assertIn(quality["verdict"], {"strong", "usable", "rebuild"})
        self.assertIn("audio_continuity", quality)

    def test_polished_renderer_captions_headline_and_external_audio(self):
        from splice_videos import splice_videos
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_a = root / "a.mp4"; video_b = root / "b.mp4"; output = root / "out.mp4"
            for path, freq in [(video_a, 440), (video_b, 660)]:
                proc = subprocess.run([
                    FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=180x320:rate=30",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
                    "-t", "2.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)
                ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            segments = [
                {"video_path": str(video_a), "start": 0, "end": 1.5, "role": "hook", "text": "Watch this foundation change."},
                {"video_path": str(video_b), "start": 0, "end": 1.5, "role": "benefit", "text": "Blends into a dewy finish.", "audio_path": str(video_a), "audio_start": .5},
            ]
            splice_videos(segments, str(output), captions=True, headline="Color-changing foundation?", audio_normalize=True)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)

    def test_bitmap_overlay_fallback_without_ass(self):
        from splice_videos import splice_videos
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"; output = root / "bitmap.mp4"
            proc = subprocess.run([
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=180x320:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "2.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            old = os.environ.get("SCRAPER_FORCE_BITMAP_OVERLAYS")
            os.environ["SCRAPER_FORCE_BITMAP_OVERLAYS"] = "1"
            try:
                splice_videos([
                    {"video_path": str(video), "start": 0, "end": 2.0, "role": "hook", "text": "Bitmap caption fallback works."}
                ], str(output), captions=True, headline="No ASS required", audio_normalize=False)
            finally:
                if old is None:
                    os.environ.pop("SCRAPER_FORCE_BITMAP_OVERLAYS", None)
                else:
                    os.environ["SCRAPER_FORCE_BITMAP_OVERLAYS"] = old
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 1000)



class AdEditorTests(unittest.TestCase):
    def setUp(self):
        self.product = parse_product_html(URL, FIXTURE.read_text(encoding="utf-8"))

    def _video(self, path: Path, freq: int = 440, duration: float = 4.0):
        proc = subprocess.run([
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=180x320:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
            "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _workspace(self, root: Path):
        ws = ProductWorkspace(root, self.product).ensure()
        ws.save_product(self.product)
        ws.save_search_plan(build_search_plan(self.product))
        candidates = []
        for vid, freq in [("101", 440), ("202", 660)]:
            video = ws.raw_dir / f"{vid}.mp4"
            self._video(video, freq=freq, duration=4.0)
            candidate = VideoCandidate(
                url=f"https://www.tiktok.com/@c{vid}/video/{vid}",
                video_id=vid,
                creator=f"c{vid}",
                caption=f"source {vid}",
                download_status="downloaded",
                local_path=str(video),
                combined_score=.9,
            )
            candidates.append(candidate)
        ws.save_candidates(candidates)
        prepare_analysis_jobs(self.product, candidates, ws.analysis_dir)
        for vid, roles in {
            "101": [(0.0, 1.5, "hook", .95), (1.5, 3.8, "demo", .90)],
            "202": [(0.0, 2.2, "benefit", .92), (2.2, 4.0, "cta", .87)],
        }.items():
            path = ws.analysis_dir / f"{vid}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.update({
                "status": "complete", "duration_seconds": 4.0, "video_relevance_score": .95,
                "segments": [
                    {"start": st, "end": en, "role": role, "score": score, "product_match_score": .9, "product_visible": True, "text": f"{role} {vid}."}
                    for st, en, role, score in roles
                ],
            })
            path.write_text(json.dumps(payload), encoding="utf-8")
        parent = {
            "name": "ad_parent",
            "template": "direct_response",
            "strategy": "creative_mix",
            "duration_seconds": 3.7,
            "segments": [
                {"video_path": str(ws.raw_dir / "101.mp4"), "video_id": "101", "creator": "c101", "start": 0.0, "end": 1.5, "role": "hook", "segment_id": "101:0", "text": "hook 101."},
                {"video_path": str(ws.raw_dir / "202.mp4"), "video_id": "202", "creator": "c202", "start": 2.2, "end": 4.0, "role": "cta", "segment_id": "202:1", "text": "cta 202."},
            ],
            "output_path": str(ws.ads_dir / "ad_parent.mp4"),
            "render": {"captions_enabled": False, "headline_enabled": False, "audio_normalize": False},
        }
        render_ad_plans([parent])
        (ws.ads_dir / "ad_plans.json").write_text(json.dumps([parent]), encoding="utf-8")
        return ws

    def test_dashboard_html_has_non_destructive_editor_and_ai_iteration(self):
        self.assertIn("NEXTPLAY AD EDITOR", DASHBOARD_HTML)
        self.assertIn("Edit ad", DASHBOARD_HTML)
        self.assertIn("AI regenerate", DASHBOARD_HTML)
        self.assertIn("Source videos", DASHBOARD_HTML)
        self.assertIn("Other ads", DASHBOARD_HTML)
        self.assertIn("/api/editor/render", DASHBOARD_HTML)
        self.assertIn("/api/editor/ai", DASHBOARD_HTML)

    def test_dashboard_editor_is_timeline_first_with_direct_manipulation(self):
        for text in [
            "NEXTPLAY AD EDITOR · TIMELINE", "V1 video", "A1 audio",
            "Drag clips to reorder", "timelineClipNative", "timelineTrimHandle",
            "editorBeginTrim", "editorDropAt", "drag to timeline",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_dashboard_timeline_trim_handles_sit_above_drop_zones_and_capture_pointer(self):
        from pipeline_core.dashboard import DASHBOARD_HTML
        for token in [
            "timelineClipNative.selected{outline:2px solid var(--green);outline-offset:1px;z-index:20}",
            "timelineTrimHandle{position:absolute;top:0;bottom:0;width:16px",
            "touch-action:none", "setPointerCapture", "editorRefreshTimelineGeometry",
            "data-audio-index", "data-drop-index", "Trim headroom",
            "Direct edge trim", "timelineGapNative", "Close gaps",
        ]:
            self.assertIn(token, DASHBOARD_HTML)

    def test_dashboard_timeline_reorder_uses_full_clip_drop_targets(self):
        for text in [
            "editorClipDragOver", "editorClipDrop", "editorTimelineDragIndex",
            "editorReorder", "drop-before", "drop-after",
            "left half inserts before", "right half inserts after",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_editor_sanitizes_manual_cross_mix_and_clamps_source_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            clean = sanitize_editor_segments(ws, [
                {"video_id": "202", "start": 1.0, "end": 99.0, "gap_before": 0.75, "role": "demo", "text": "manual source"},
                {"segment_id": "101:0", "video_id": "101", "start": 0.2, "end": 1.2, "role": "hook"},
            ])
            self.assertEqual([x["video_id"] for x in clean], ["202", "101"])
            self.assertEqual(clean[0]["end"], 4.0)
            self.assertEqual(clean[0]["gap_before"], 0.75)
            self.assertEqual(clean[1]["segment_id"], "101:0")

    def test_render_editor_version_is_non_destructive_and_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            plan = render_editor_version(ws, "ad_parent", [
                {"segment_id": "101:0", "video_id": "101", "start": 0.0, "end": 1.7, "role": "hook"},
                {"segment_id": "101:1", "video_id": "101", "start": 1.5, "end": 3.8, "role": "demo"},
                {"segment_id": "202:0", "video_id": "202", "start": 0.0, "end": 2.2, "role": "benefit"},
                {"segment_id": "202:1", "video_id": "202", "start": 2.2, "end": 4.0, "role": "cta"},
            ], label="longer hook and cross-mix", render_patch={"captions_enabled": False, "headline_enabled": False, "audio_normalize": False})
            self.assertTrue(Path(plan["output_path"]).exists())
            self.assertEqual(plan["parent_ad"], "ad_parent")
            self.assertEqual(plan["strategy"], "manual_edit")
            self.assertTrue(plan["name"].startswith("ad_parent_edit_v"))
            plans = json.loads((ws.ads_dir / "ad_plans.json").read_text(encoding="utf-8"))
            self.assertEqual(plans[0]["name"], "ad_parent")
            self.assertEqual(plans[-1]["name"], plan["name"])
            self.assertEqual([s["video_id"] for s in plans[-1]["segments"]], ["101", "101", "202", "202"])

    def test_editor_true_edge_gap_is_versioned_and_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            plan = render_editor_version(ws, "ad_parent", [
                {"segment_id": "101:0", "video_id": "101", "start": 0.25, "end": 1.5, "gap_before": 0.5, "role": "hook"},
                {"segment_id": "202:1", "video_id": "202", "start": 2.2, "end": 4.0, "gap_before": 0.25, "role": "cta"},
            ], label="direct edge gaps", render_patch={"captions_enabled": False, "headline_enabled": False, "audio_normalize": False})
            self.assertTrue(Path(plan["output_path"]).exists())
            self.assertEqual(plan["segments"][0]["gap_before"], 0.5)
            self.assertEqual(plan["segments"][1]["gap_before"], 0.25)
            expected = 0.5 + (1.5 - 0.25) + 0.25 + (4.0 - 2.2)
            self.assertAlmostEqual(plan["duration_seconds"], expected, places=2)

    def test_dashboard_editor_has_undo_zoom_versions_cross_mix_and_ai_chat(self):
        for text in [
            "↶ Undo", "↷ Redo", "Timeline zoom", "−1f", "+1f", "VERSION HISTORY",
            "ADD / CROSS-MIX", "AI Creative Director", "/api/editor/restore", "/api/editor/ai/chat",
        ]:
            self.assertIn(text, DASHBOARD_HTML)

    def test_version_history_state_groups_non_destructive_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            v1 = render_editor_version(ws, "ad_parent", [
                {"segment_id": "101:0", "video_id": "101", "start": 0.0, "end": 1.5, "role": "hook"},
                {"segment_id": "202:1", "video_id": "202", "start": 2.2, "end": 4.0, "role": "cta"},
            ], label="first edit", render_patch={"captions_enabled": False, "headline_enabled": False, "audio_normalize": False})
            state = build_dashboard_state(self.product, ws)
            row = next(a for a in state["ads"] if a["name"] == v1["name"])
            self.assertEqual(row["version_root"], "ad_parent")
            self.assertGreaterEqual(row["version_count"], 2)
            self.assertTrue(any(x["name"] == "ad_parent" for x in row["version_family"]))

    def test_restore_previous_version_creates_new_render_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            v1 = render_editor_version(ws, "ad_parent", [
                {"segment_id": "101:0", "video_id": "101", "start": 0.0, "end": 1.7, "role": "hook"},
                {"segment_id": "202:1", "video_id": "202", "start": 2.2, "end": 4.0, "role": "cta"},
            ], label="changed hook", render_patch={"captions_enabled": False, "headline_enabled": False, "audio_normalize": False})
            restored = restore_ad_version(ws, v1["name"], "ad_parent")
            self.assertTrue(Path(restored["output_path"]).exists())
            self.assertEqual(restored["restored_from"], "ad_parent")
            self.assertEqual(restored["root_ad"], "ad_parent")
            self.assertEqual(restored["strategy"], "manual_restore")
            plans = json.loads((ws.ads_dir / "ad_plans.json").read_text(encoding="utf-8"))
            self.assertEqual(plans[0]["name"], "ad_parent")
            self.assertEqual(plans[-1]["name"], restored["name"])

    def test_ai_direction_fallback_asks_for_precision_then_accepts_specific_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            old = os.environ.pop("OPENAI_API_KEY", None)
            try:
                vague = refine_ai_direction(self.product, ws, "ad_parent", [{"role": "user", "content": "make it better"}])
                precise = refine_ai_direction(self.product, ws, "ad_parent", [{"role": "user", "content": "Keep the hook, replace the demo, add one result clip, and target 20 seconds."}])
            finally:
                if old is not None:
                    os.environ["OPENAI_API_KEY"] = old
            self.assertFalse(vague["ready"])
            self.assertIn("detail", vague["reply"].lower())
            self.assertTrue(precise["ready"])
            self.assertIn("replace the demo", precise["direction"].lower())

    def test_ai_iteration_can_use_current_unsaved_editor_recipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            draft = [
                {"segment_id": "101:0", "video_id": "101", "start": 0.0, "end": 1.5, "role": "hook"},
                {"segment_id": "101:1", "video_id": "101", "start": 1.5, "end": 3.8, "role": "demo"},
                {"segment_id": "202:1", "video_id": "202", "start": 2.2, "end": 4.0, "role": "cta"},
            ]
            old = os.environ.pop("OPENAI_API_KEY", None)
            try:
                plan = ai_iterate_ad(self.product, ws, "ad_parent", instruction="keep the hook and make another iteration", mode="iterate", base_segments=draft)
            finally:
                if old is not None:
                    os.environ["OPENAI_API_KEY"] = old
            self.assertTrue(plan["ai_iteration"]["based_on_editor_draft"])
            self.assertTrue(Path(plan["output_path"]).exists())

    def test_ai_iteration_has_offline_fallback_and_renders_specific_ad(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self._workspace(Path(tmp))
            old = os.environ.pop("OPENAI_API_KEY", None)
            try:
                plan = ai_iterate_ad(self.product, ws, "ad_parent", instruction="use a stronger demo", mode="iterate")
            finally:
                if old is not None:
                    os.environ["OPENAI_API_KEY"] = old
            self.assertTrue(Path(plan["output_path"]).exists())
            self.assertEqual(plan["parent_ad"], "ad_parent")
            self.assertEqual(plan["strategy"], "ai_iteration")
            self.assertEqual(plan["ai_iteration"]["provider"], "deterministic")



if __name__ == "__main__":
    unittest.main()
