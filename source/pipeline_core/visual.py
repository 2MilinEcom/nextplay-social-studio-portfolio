from __future__ import annotations

import math
from pathlib import Path

from .models import VideoCandidate


class ClipVisualRanker:
    """Optional CLIP scorer. Heavy dependencies are intentionally lazy-loaded."""

    def __init__(self):
        try:
            import torch
            import open_clip
        except ImportError as exc:
            raise RuntimeError(
                "Visual ranking requires the optional AI dependencies: pip install -r requirements-ai.txt"
            ) from exc
        self.torch = torch
        self.open_clip = open_clip
        mps = getattr(torch.backends, "mps", None)
        self.device = "cuda" if torch.cuda.is_available() else ("mps" if mps and mps.is_available() else "cpu")
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        self.model = self.model.to(self.device).eval()

    def _embed(self, image_path: Path):
        from PIL import Image
        image = Image.open(image_path).convert("RGB")
        tensor = self.preprocess(image).unsqueeze(0).to(self.device)
        with self.torch.no_grad():
            feat = self.model.encode_image(tensor)
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat

    def compare(self, reference: Path, candidate: Path) -> float:
        a = self._embed(reference)
        b = self._embed(candidate)
        return float((a @ b.T).item())


def calibrated_clip_score(raw_similarity: float) -> float:
    """Map a CLIP cosine score to 0..1 for ranking; raw score is retained separately."""
    # This is a ranking calibration, not a probability of product identity.
    value = 1.0 / (1.0 + math.exp(-12.0 * (raw_similarity - 0.28)))
    return max(0.0, min(1.0, value))


def fetch_thumbnail(candidate: VideoCandidate, destination: Path) -> Path | None:
    if not candidate.thumbnail_url:
        return None
    try:
        import requests
        response = requests.get(
            candidate.thumbnail_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.tiktok.com/"},
            timeout=20,
        )
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("image/"):
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination
    except Exception:
        return None


def apply_visual_scores(reference_image: Path, candidates: list[VideoCandidate], thumbnails_dir: Path, *, limit: int = 120) -> list[VideoCandidate]:
    reference_image = Path(reference_image)
    if not reference_image.exists():
        raise FileNotFoundError(f"Product reference image not found: {reference_image}")
    ranker = ClipVisualRanker()
    # Text ordering keeps the expensive pass focused on plausible candidates.
    ordered = sorted(candidates, key=lambda c: -c.text_score)
    completed = 0
    for candidate in ordered:
        if completed >= limit:
            break
        if not candidate.thumbnail_url:
            continue
        thumb = thumbnails_dir / f"{candidate.video_id}.jpg"
        if not thumb.exists():
            fetched = fetch_thumbnail(candidate, thumb)
            if not fetched:
                continue
        try:
            raw = ranker.compare(reference_image, thumb)
        except Exception:
            continue
        candidate.visual_raw_score = round(raw, 5)
        candidate.visual_score = round(calibrated_clip_score(raw), 4)
        completed += 1
    return candidates
