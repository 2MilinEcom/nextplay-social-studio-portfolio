from __future__ import annotations

import html as html_lib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .models import Product


def canonicalize_tiktok_shop_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path.rstrip("/"), "", ""))


def product_id_from_url(url: str) -> str:
    match = re.search(r"/(\d{12,})/?(?:\?|$)", url)
    if not match:
        raise ValueError("Could not find a TikTok Shop product ID in the URL.")
    return match.group(1)


def _clean_text(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _guess_brand(title: str) -> str:
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9&.'-]*", cleaned)
    if not tokens:
        return ""
    first = tokens[0]
    if first.upper() == first and len(first) > 2:
        return first
    # A title often starts with a brand in title case; keep it conservative.
    return first if len(tokens) >= 3 else ""


def _extract_meta(html: str, key: str) -> str:
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(key)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(key)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(key)}["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.I | re.S)
        if match:
            return _clean_text(match.group(1))
    return ""


def parse_product_html(url: str, html: str) -> Product:
    canonical = canonicalize_tiktok_shop_url(url)
    product_id = product_id_from_url(canonical)
    title = _extract_meta(html, "og:title") or _extract_meta(html, "twitter:title")
    description = _extract_meta(html, "og:description") or _extract_meta(html, "description")
    image = _extract_meta(html, "og:image") or _extract_meta(html, "twitter:image")

    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
        if match:
            title = _clean_text(re.sub(r"<[^>]+>", "", match.group(1)))

    # TikTok/modern ecommerce pages often embed product data as JSON.
    if not title or not image:
        for match in re.finditer(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html, flags=re.I | re.S):
            try:
                data = json.loads(html_lib.unescape(match.group(1)).strip())
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("@type", "")).lower() != "product":
                    continue
                title = title or _clean_text(str(item.get("name", "")))
                description = description or _clean_text(str(item.get("description", "")))
                raw_image = item.get("image")
                if not image:
                    if isinstance(raw_image, str):
                        image = raw_image
                    elif isinstance(raw_image, list) and raw_image:
                        image = str(raw_image[0])

    if not title:
        # The slug is preferable to returning an empty title.
        slug = urlsplit(canonical).path.split("/")[-2] if "/" in urlsplit(canonical).path else product_id
        title = slug.replace("-", " ").title()

    return Product(
        source_url=canonical,
        product_id=product_id,
        title=title,
        brand=_guess_brand(title),
        description=description,
        image_urls=[image] if image else [],
        metadata={"ingest_method": "html"},
    )


def fetch_product_with_http(url: str, timeout: int = 25) -> Product:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is required. Run pip install -r requirements.txt") from exc

    response = requests.get(
        canonicalize_tiktok_shop_url(url),
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    product = parse_product_html(url, response.text)
    if "security check" in product.title.lower() or len(product.title) < 4:
        raise RuntimeError("TikTok Shop returned a security/interstitial page.")
    return product


def fetch_product_with_browser(url: str, driver, *, interactive: bool = True) -> Product:
    driver.get(canonicalize_tiktok_shop_url(url))
    try:
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(driver, 30).until(lambda d: d.execute_script("return document.readyState") == "complete")
    except Exception:
        pass

    if interactive:
        print("\nTikTok Shop opened in Chromium.")
        print("If TikTok shows a login/security check, complete it in the browser.")
        input("Press ENTER once the product page is visible...")

    html = driver.page_source
    product = parse_product_html(url, html)
    product.metadata["ingest_method"] = "browser"

    # Prefer data from the rendered DOM when meta tags were hidden/changed.
    try:
        rendered = driver.execute_script(
            """
            const metas = Object.fromEntries([...document.querySelectorAll('meta')].map(m => [m.getAttribute('property') || m.getAttribute('name'), m.content]));
            const images = [...document.images]
              .filter(i => i.naturalWidth >= 250 && i.naturalHeight >= 250)
              .map(i => ({src: i.currentSrc || i.src, alt: i.alt || '', area: i.naturalWidth * i.naturalHeight}))
              .sort((a,b) => b.area - a.area);
            return {title: metas['og:title'] || document.title || '', description: metas['og:description'] || metas['description'] || '', image: metas['og:image'] || (images[0] && images[0].src) || '', images};
            """
        )
        if rendered:
            if rendered.get("title") and "security check" not in rendered["title"].lower():
                product.title = _clean_text(rendered["title"])
                product.brand = _guess_brand(product.title)
            if rendered.get("description"):
                product.description = _clean_text(rendered["description"])
            urls = []
            if rendered.get("image"):
                urls.append(rendered["image"])
            for image in rendered.get("images", [])[:12]:
                src = image.get("src")
                if src and src not in urls:
                    urls.append(src)
            product.image_urls = urls
    except Exception:
        pass
    return product


def save_primary_image(product: Product, destination: Path, *, driver=None) -> str | None:
    if not product.image_urls:
        return None
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = product.image_urls[0]

    try:
        import requests
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": product.source_url}, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise ValueError(f"Expected an image, received {content_type!r}")
        destination.write_bytes(response.content)
        product.primary_image_path = str(destination)
        return str(destination)
    except Exception:
        pass

    if driver is not None:
        try:
            driver.get(url)
            element = driver.find_element("tag name", "img")
            element.screenshot(str(destination.with_suffix(".png")))
            product.primary_image_path = str(destination.with_suffix(".png"))
            return product.primary_image_path
        except Exception:
            return None
    return None


def load_fixture(path: str | Path, source_url: str) -> Product:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["source_url"] = canonicalize_tiktok_shop_url(source_url)
        data["product_id"] = product_id_from_url(source_url)
        return Product.from_dict(data)
    return parse_product_html(source_url, path.read_text(encoding="utf-8"))
