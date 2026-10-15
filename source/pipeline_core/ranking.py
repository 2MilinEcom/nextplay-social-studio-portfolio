from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from .models import Product, SearchPlan, VideoCandidate


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_text(product: Product, plan: SearchPlan, candidate: VideoCandidate) -> float:
    haystack = " ".join([candidate.caption, candidate.creator, *candidate.discovered_by]).lower()
    candidate_tokens = _tokens(haystack)
    product_tokens = _tokens(" ".join([product.title, product.brand, product.description, *plan.relevance_terms]))

    if not candidate_tokens:
        # The fact that it came from an exact search still provides a weak signal.
        return 0.25 if candidate.discovered_by else 0.0

    overlap = len(candidate_tokens & product_tokens) / max(1, min(len(candidate_tokens), len(product_tokens)))
    brand_hit = 1.0 if product.brand and product.brand.lower() in haystack else 0.0
    exact_query_hit = max(
        (SequenceMatcher(None, q.lower(), haystack).ratio() for q in candidate.discovered_by if q),
        default=0.0,
    )
    feature_hits = sum(1 for term in plan.relevance_terms if term.lower() in haystack)
    feature_score = 1.0 - math.exp(-feature_hits / 3.0)
    return _clamp(0.35 * overlap + 0.25 * brand_hit + 0.20 * exact_query_hit + 0.20 * feature_score)


def rank_candidates(product: Product, plan: SearchPlan, candidates: list[VideoCandidate]) -> list[VideoCandidate]:
    for candidate in candidates:
        candidate.text_score = round(score_text(product, plan, candidate), 4)
        if candidate.visual_score is None:
            candidate.combined_score = candidate.text_score
        else:
            candidate.combined_score = round(0.65 * candidate.text_score + 0.35 * candidate.visual_score, 4)
    return sorted(candidates, key=lambda c: (-c.combined_score, c.url))
