from __future__ import annotations

import html as html_lib
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from .models import SearchPlan, VideoCandidate


VIDEO_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9._-]+/video/\d+")


def video_id_from_url(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else re.sub(r"\W+", "", url)[-20:]


def creator_from_url(url: str) -> str:
    match = re.search(r"tiktok\.com/@([^/]+)/video/", url)
    return f"@{match.group(1)}" if match else ""


def _normalize_video_url(url: str) -> str:
    url = html_lib.unescape((url or "").strip())
    url = url.replace("\\u002F", "/").replace("\\/", "/")
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = "https://www.tiktok.com" + url
    return url.split("?")[0]


def _collect_visible_cards(driver) -> list[dict]:
    """Collect video anchors from the currently rendered DOM.

    TikTok changes card wrappers often, so deliberately avoid depending on a
    specific data-e2e value. We look at every anchor and walk upward for text
    and thumbnails.
    """
    try:
        rows = driver.execute_script(
            """
            const out = [];
            const seen = new Set();
            for (const a of document.querySelectorAll('a[href]')) {
              const href = a.href || a.getAttribute('href') || '';
              if (!href.includes('/video/')) continue;
              if (seen.has(href)) continue;
              seen.add(href);
              let card = a;
              for (let i = 0; i < 6 && card?.parentElement; i++) {
                const txt = (card.innerText || '').trim();
                if (txt.length >= 20) break;
                card = card.parentElement;
              }
              const img = card?.querySelector?.('img') || a.querySelector?.('img');
              out.push({
                href,
                text: (card?.innerText || a.innerText || '').slice(0, 1800),
                thumb: img?.currentSrc || img?.src || ''
              });
            }
            return out;
            """
        ) or []
        return rows
    except Exception:
        return []


def _collect_from_page_source(driver) -> list[dict]:
    """Fallback for links present in TikTok's hydration JSON but not the DOM."""
    try:
        source = driver.page_source or ""
    except Exception:
        return []
    source = html_lib.unescape(source)
    source = source.replace("\\u002F", "/").replace("\\/", "/")
    urls = []
    seen = set()
    for match in VIDEO_RE.finditer(source):
        url = _normalize_video_url(match.group(0))
        if url and url not in seen:
            seen.add(url)
            urls.append({"href": url, "text": "", "thumb": ""})
    return urls


def _page_state(driver) -> str:
    try:
        title = driver.title or ""
        url = driver.current_url or ""
        text = (driver.find_element("tag name", "body").text or "")[:2500].lower()
    except Exception:
        return "unknown"
    markers = []
    for needle, label in (
        ("captcha", "CAPTCHA"),
        ("verify", "verification"),
        ("log in", "login"),
        ("something went wrong", "error"),
        ("too many attempts", "rate-limit"),
    ):
        if needle in text:
            markers.append(label)
    suffix = f"; possible state: {', '.join(markers)}" if markers else ""
    return f"title={title!r}; url={url!r}{suffix}"


def _save_debug(driver, debug_dir: Path | None, query_number: int, query: str) -> None:
    if debug_dir is None:
        return
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", query).strip("-")[:60] or "search"
    stem = f"{query_number:02d}-{safe}"
    try:
        driver.save_screenshot(str(debug_dir / f"{stem}.png"))
    except Exception:
        pass
    try:
        (debug_dir / f"{stem}.html").write_text(driver.page_source or "", encoding="utf-8")
    except Exception:
        pass


def _open_search(driver, query: str) -> None:
    # /search?q= is the least specialized public search route. TikTok can
    # redirect it internally to a tab-specific URL after hydration.
    driver.get("https://www.tiktok.com/search?q=" + quote_plus(query.lstrip("#")))
    time.sleep(4.0)


def discover_tiktok_videos(
    driver,
    plan: SearchPlan,
    *,
    scrolls_per_query: int = 6,
    max_per_query: int = 40,
    pause_seconds: float = 1.8,
    debug_dir: Path | None = None,
) -> list[VideoCandidate]:
    by_url: dict[str, VideoCandidate] = {}
    search_inputs = [(q, q) for q in plan.queries] + [(tag, tag) for tag in plan.hashtags]

    for index, (label, query) in enumerate(search_inputs, start=1):
        print(f"\n[{index}/{len(search_inputs)}] TikTok search: {label}")
        _open_search(driver, query)
        found_this_query: set[str] = set()

        for scroll_index in range(scrolls_per_query):
            rows = _collect_visible_cards(driver)
            if not rows:
                rows = _collect_from_page_source(driver)

            for row in rows:
                href = _normalize_video_url(row.get("href") or "")
                if "/video/" not in href:
                    continue
                found_this_query.add(href)
                candidate = by_url.get(href)
                if candidate is None:
                    candidate = VideoCandidate(
                        url=href,
                        video_id=video_id_from_url(href),
                        creator=creator_from_url(href),
                        caption=(row.get("text") or "").strip(),
                        thumbnail_url=row.get("thumb") or "",
                    )
                    by_url[href] = candidate
                else:
                    if len(row.get("text") or "") > len(candidate.caption):
                        candidate.caption = (row.get("text") or "").strip()
                    candidate.thumbnail_url = candidate.thumbnail_url or (row.get("thumb") or "")
                if label not in candidate.discovered_by:
                    candidate.discovered_by.append(label)

            print(f"  scroll {scroll_index + 1}/{scrolls_per_query}: {len(found_this_query)} unique videos")
            if len(found_this_query) >= max_per_query:
                break
            driver.execute_script(
                "window.scrollBy(0, Math.max(document.documentElement.clientHeight * 2.5, 1900));"
            )
            time.sleep(pause_seconds)

        if not found_this_query:
            print("  WARNING: no video links were detected on this search page.")
            print("  " + _page_state(driver))
            _save_debug(driver, debug_dir, index, query)
            if debug_dir is not None:
                print(f"  saved debug HTML/screenshot to: {debug_dir}")

    return list(by_url.values())


def merge_candidates(existing: list[VideoCandidate], new: list[VideoCandidate]) -> list[VideoCandidate]:
    by_url = {c.url: c for c in existing}
    for candidate in new:
        current = by_url.get(candidate.url)
        if not current:
            by_url[candidate.url] = candidate
            continue
        for query in candidate.discovered_by:
            if query not in current.discovered_by:
                current.discovered_by.append(query)
        if len(candidate.caption) > len(current.caption):
            current.caption = candidate.caption
        current.thumbnail_url = current.thumbnail_url or candidate.thumbnail_url
    return list(by_url.values())
