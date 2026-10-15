from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from .browser import set_download_dir
from .models import VideoCandidate

SNAPTIK_URLS = ("https://snaptik.app/en3", "https://snaptik.kim/en2/")


def _download_files(download_dir: Path) -> set[Path]:
    download_dir.mkdir(parents=True, exist_ok=True)
    return {p for p in download_dir.iterdir() if p.is_file()}


def _wait_for_download(download_dir: Path, before: set[Path], timeout: int = 75) -> Path | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _download_files(download_dir)
        new = current - before
        incomplete = [p for p in new if p.name.endswith((".crdownload", ".tmp", ".part"))]
        complete = [p for p in new if p not in incomplete]
        if complete and not incomplete:
            return max(complete, key=lambda p: p.stat().st_mtime)
        time.sleep(1)
    return None


def _keep_provider_tab(driver, preferred=None):
    handles = list(driver.window_handles)
    if not handles:
        return None
    provider_handles = []
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            if "snaptik." in (driver.current_url or "").lower():
                provider_handles.append(handle)
        except Exception:
            continue
    keep = preferred if preferred in provider_handles else (provider_handles[-1] if provider_handles else handles[0])
    for handle in handles:
        if handle == keep:
            continue
        try:
            driver.switch_to.window(handle)
            driver.close()
        except Exception:
            pass
    driver.switch_to.window(keep)
    return keep


def _suppress_popups(driver, element=None):
    try:
        driver.execute_script(
            "window.open=function(){return null}; if(arguments[0]) arguments[0].removeAttribute('target');",
            element,
        )
    except Exception:
        pass


def _find_input(driver, timeout=15):
    from selenium.webdriver.common.by import By
    deadline = time.time() + timeout
    selectors = [
        (By.NAME, "page"),
        (By.CSS_SELECTOR, 'input[name="page"]'),
        (By.CSS_SELECTOR, 'input[placeholder*="TikTok" i]'),
        (By.CSS_SELECTOR, 'input[placeholder*="link" i]'),
        (By.CSS_SELECTOR, 'input[type="text"]'),
    ]
    while time.time() < deadline:
        for by, selector in selectors:
            for element in driver.find_elements(by, selector):
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except Exception:
                    continue
        time.sleep(0.5)
    raise TimeoutError("Could not find SnapTik URL input")


def _open_provider(driver):
    last_error = None
    for url in SNAPTIK_URLS:
        try:
            driver.get(url)
            time.sleep(2)
            return _find_input(driver)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"No SnapTik mirror worked: {last_error}")


def _find_submit(driver, timeout=15):
    from selenium.webdriver.common.by import By
    deadline = time.time() + timeout
    while time.time() < deadline:
        buttons = driver.find_elements(By.XPATH, "//button[@type='submit'] | //button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'download')]")
        for button in buttons:
            try:
                if button.is_displayed() and button.is_enabled():
                    return button
            except Exception:
                pass
        time.sleep(0.5)
    raise TimeoutError("Could not find SnapTik submit button")


def _find_media_button(driver, timeout=45):
    from selenium.webdriver.common.by import By
    deadline = time.time() + timeout
    while time.time() < deadline:
        _keep_provider_tab(driver)
        elements = driver.find_elements(
            By.XPATH,
            "//*[self::a or self::button][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'download (no watermark)') and not(contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'download hd')) and not(contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'extension'))]",
        )
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                pass
        time.sleep(1)
    raise TimeoutError("SnapTik no-watermark download button did not appear")


def _media_kind(path: Path) -> str | None:
    try:
        size = path.stat().st_size
        if size < 10_000 or size > 1_500_000_000:
            return None
        head = path.read_bytes()[:16]
    except OSError:
        return None
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_one(driver, candidate: VideoCandidate, raw_dir: Path, quarantine_dir: Path) -> VideoCandidate:
    raw_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    set_download_dir(driver, raw_dir)
    candidate.error = None
    print(f"\nDownloading {candidate.url}")
    try:
        box = _open_provider(driver)
        box.click()
        box.clear()
        box.send_keys(candidate.url)
        submit = _find_submit(driver)
        _suppress_popups(driver, submit)
        handle = driver.current_window_handle
        try:
            submit.click()
        except Exception:
            driver.execute_script("arguments[0].click()", submit)
        time.sleep(2)
        _keep_provider_tab(driver, preferred=handle)

        media_button = _find_media_button(driver)
        before = _download_files(raw_dir)
        _suppress_popups(driver, media_button)
        handle = driver.current_window_handle
        try:
            media_button.click()
        except Exception:
            driver.execute_script("arguments[0].click()", media_button)
        time.sleep(1)
        _keep_provider_tab(driver, preferred=handle)
        downloaded = _wait_for_download(raw_dir, before)
        if not downloaded:
            raise TimeoutError("Browser did not produce a completed media file")

        kind = _media_kind(downloaded)
        if not kind:
            quarantine_target = quarantine_dir / downloaded.name
            shutil.move(str(downloaded), quarantine_target)
            raise ValueError(f"Downloaded file failed media validation and was quarantined: {quarantine_target}")

        target = raw_dir / f"{candidate.video_id}.{kind}"
        if downloaded.resolve() != target.resolve():
            if target.exists():
                target.unlink()
            os.replace(downloaded, target)
        candidate.local_path = str(target)
        candidate.sha256 = _sha256(target)
        candidate.download_status = "downloaded"
        return candidate
    except Exception as exc:
        candidate.download_status = "failed"
        candidate.error = f"{type(exc).__name__}: {exc}"
        print(f"  failed: {candidate.error}")
        return candidate


def download_ranked(
    driver,
    candidates: list[VideoCandidate],
    raw_dir: Path,
    quarantine_dir: Path,
    *,
    min_score: float = 0.45,
    limit: int | None = None,
    excluded_ids: set[str] | None = None,
) -> list[VideoCandidate]:
    excluded_ids = set(excluded_ids or set())
    eligible = [c for c in candidates if c.combined_score >= min_score and c.download_status != "downloaded" and c.video_id not in excluded_ids]
    if limit is not None:
        eligible = eligible[:limit]
    print(f"\nDownload queue: {len(eligible)} candidate(s) at score >= {min_score:.2f}")
    for index, candidate in enumerate(eligible, start=1):
        print(f"[{index}/{len(eligible)}] score={candidate.combined_score:.3f}")
        download_one(driver, candidate, raw_dir, quarantine_dir)
    return candidates
