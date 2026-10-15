from __future__ import annotations

import re

from .models import Product, SearchPlan

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "base", "by", "for", "from", "in", "is", "it", "of",
    "on", "or", "our", "the", "this", "to", "with", "white", "official", "store", "partner", "oz",
    "makeup", "product", "new", "pack", "full", "medium", "dual", "ended", "detachable", "finish",
    "coverage", "g", "ml", "into",
}

FEATURE_PATTERNS = [
    (r"color[- ]changing", "color changing foundation"),
    (r"foundation stick", "foundation stick"),
    (r"korean|k-beauty|k beauty", "korean foundation"),
    (r"dewy", "dewy foundation"),
    (r"collagen", "collagen foundation"),
    (r"brush", "foundation stick with brush"),
    (r"tone|skin tone", "skin tone matching foundation"),
    (r"coverage", "full coverage foundation stick"),
]

CATEGORY_HASHTAGS = [
    (r"foundation|concealer|makeup", ["#beautytok", "#makeuptiktok", "#foundationreview"]),
    (r"korean|k-beauty|k beauty", ["#kbeauty", "#koreanmakeup"]),
    (r"color[- ]changing", ["#colorchangingfoundation"]),
    (r"foundation stick", ["#foundationstick"]),
    (r"dewy", ["#dewyfoundation"]),
    (r"tiktok shop", ["#tiktokshop", "#tiktokshopfinds"]),
]


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]


def _unique(values: list[str], max_items: int | None = None) -> list[str]:
    seen = set()
    result = []
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            result.append(clean)
        if max_items and len(result) >= max_items:
            break
    return result


def _clean_title(title: str) -> str:
    title = re.sub(r"\[[^\]]+\]", " ", title)
    title = re.sub(r"\([^)]*\)", " ", title)
    title = re.sub(r"\b\d+(?:\.\d+)?\s*(?:oz|g|ml)\b", " ", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -|")
    return title


def _product_name(title: str, brand: str) -> str:
    """Keep the high-signal product name before the marketing-heavy dash clauses."""
    title = _clean_title(title)
    first_clause = re.split(r"\s+-\s+|\s+\|\s+", title, maxsplit=1)[0].strip()
    # TikTok titles often append a variant word such as White after the formal name.
    first_clause = re.sub(r"\s+White$", "", first_clause, flags=re.I)
    if brand and brand.lower() not in first_clause.lower():
        first_clause = f"{brand} {first_clause}".strip()
    return first_clause[:120]


def build_search_plan(product: Product) -> SearchPlan:
    title = _clean_title(product.title)
    brand = product.brand.strip()
    name = _product_name(product.title, brand)
    corpus = f"{product.title} {product.description}".lower()

    queries: list[str] = []
    if name:
        queries.append(name)
        queries.append(f"{name} review")
        queries.append(f"{name} demo")

    feature_queries: list[str] = []
    for pattern, phrase in FEATURE_PATTERNS:
        if re.search(pattern, corpus, flags=re.I):
            feature_queries.append(phrase)
            if brand:
                queries.append(f"{brand} {phrase}")

    terms = _unique(_tokens(f"{title} {product.description}"), max_items=24)
    if len(terms) >= 2:
        queries.append(" ".join(terms[:5]))

    queries.extend(feature_queries)
    base_subject = feature_queries[0] if feature_queries else (" ".join(terms[:3]) if terms else name)
    for suffix in ("review", "demo", "before and after", "viral", "tiktok shop"):
        if base_subject:
            queries.append(f"{brand + ' ' if brand else ''}{base_subject} {suffix}".strip())

    hashtags: list[str] = []
    if brand:
        hashtags.append("#" + re.sub(r"[^a-z0-9]", "", brand.lower()))
    if name:
        product_tag = re.sub(r"[^a-z0-9]", "", name.lower())
        if 3 < len(product_tag) <= 60:
            hashtags.append("#" + product_tag)
    for pattern, tags in CATEGORY_HASHTAGS:
        if re.search(pattern, corpus, flags=re.I):
            hashtags.extend(tags)
    for phrase in feature_queries[:4]:
        hashtags.append("#" + re.sub(r"[^a-z0-9]", "", phrase.lower()))

    relevance_terms = _unique(([brand, name] if brand else [name]) + terms + feature_queries, max_items=35)
    return SearchPlan(
        queries=_unique(queries, max_items=16),
        hashtags=_unique([tag for tag in hashtags if len(tag) > 2], max_items=14),
        relevance_terms=relevance_terms,
    )
