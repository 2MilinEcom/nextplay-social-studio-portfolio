import ssl
import time
import os
import platform as py_platform
import subprocess
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import certifi

# python.org builds on macOS ship without a CA bundle unless
# "Install Certificates.command" has been run.
ssl._create_default_https_context = lambda: ssl.create_default_context(
    cafile=certifi.where()
)

# Python 3.12+ has no stdlib distutils; load the local shim first.
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import undetected_chromedriver as uc
from undetected_chromedriver.patcher import Patcher
from selenium.common.exceptions import (
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# undetected-chromedriver 3.5.5 always picks mac-x64 on Darwin.
_orig_set_platform_name = Patcher._set_platform_name


def _set_platform_name(self):
    _orig_set_platform_name(self)
    if self.platform.endswith("darwin") and not self.is_old_chromedriver:
        if py_platform.machine() == "arm64":
            self.platform_name = "mac-arm64"


Patcher._set_platform_name = _set_platform_name

_orig_patch_exe = Patcher.patch_exe


def _patch_exe(self):
    _orig_patch_exe(self)
    subprocess.run(
        ["codesign", "--force", "--sign", "-", self.executable_path],
        check=False,
        capture_output=True,
    )


Patcher.patch_exe = _patch_exe


# =========================================================
# SETTINGS
# =========================================================

SEARCH_TERM = ""

# Your installed Chrome is version 151.
CHROME_MAJOR_VERSION = 153

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRAPER_DIR = PROJECT_DIR / "scraper"

LINKS_FILE = SCRAPER_DIR / "links.txt"
DOWNLOAD_DIR = Path.home() / "Downloads" / "tiktok-videos"
PROFILE_DIR = PROJECT_DIR / "selenium-profile"

# TIKTOK_SEARCH_URL = (
#    "https://www.tiktok.com/search?q="
#    + quote_plus(SEARCH_TERM)
#)

TIKTOK_SEARCH_URL = (
    "https://www.tiktok.com/@fall flowers"
)

SNAPTIK_URLS = [
    "https://snaptik.app/en3",
    "https://snaptik.kim/en2/",
]

# Instagram / SnapInsta settings.
INSTAGRAM_HIGHLIGHTS_FILE = SCRAPER_DIR / "instagram-highlights.txt"
INSTAGRAM_BASE_DOWNLOAD_DIR = Path.home() / "Downloads" / "instagram"

# SnapInsta currently supports Instagram profile URLs as well as
# individual Highlight URLs. If one mirror changes, the next one is tried.
SNAPINSTA_URLS = [
    "https://snapinsta.to/en",
    "https://snapinsta.co/",
]


# =========================================================
# BROWSER
# =========================================================

def _clear_chrome_profile_locks(profile_dir: Path):
    """Stale Singleton* files make Chrome say the profile failed to open."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile_dir / name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except OSError:
            pass


def init_browser(
    headless=False,
    reuse_profile=False,
    download_dir=None,
):
    download_dir = Path(download_dir or DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Never use the real Chrome profile or the copied selenium_profile
    # from the zip — both trigger "Something went wrong when opening your profile."
    profile_dir = PROFILE_DIR if reuse_profile else PROJECT_DIR / "selenium-ephemeral"
    profile_dir.mkdir(parents=True, exist_ok=True)
    _clear_chrome_profile_locks(profile_dir)

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-features=ChromeWhatsNewUI")
    options.add_argument(
        "--disable-blink-features=AutomationControlled"
    )
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    options.add_argument("--start-maximized")

    if headless:
        options.add_argument("--headless=new")

    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(
                download_dir
            ),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "profile.default_content_setting_values.popups": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_setting_values.ads": 2,
            "profile.default_content_setting_values.automatic_downloads": 1,
        },
    )

    cached_driver = (
        Path.home()
        / "Library/Application Support/undetected_chromedriver/undetected_chromedriver"
    )
    if cached_driver.exists() and py_platform.machine() == "arm64":
        try:
            with cached_driver.open("rb") as fh:
                magic = fh.read(4)
                cputype = int.from_bytes(fh.read(4), "little")
            if magic == b"\xcf\xfa\xed\xfe" and cputype == 0x01000007:
                cached_driver.unlink()
        except OSError:
            pass

    driver = uc.Chrome(
        version_main=CHROME_MAJOR_VERSION,
        options=options,
        user_data_dir=str(profile_dir),
        use_subprocess=True,
    )

    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(download_dir),
            },
        )
    except Exception:
        pass

    return driver


def set_download_dir(driver, download_dir):
    """Change Chrome's download folder while keeping the same browser."""
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(download_dir),
            },
        )
    except Exception:
        pass

    return download_dir


# =========================================================
# GENERAL BROWSER HELPERS
# =========================================================

def wait_for_page_ready(driver, timeout=30):
    WebDriverWait(
        driver,
        timeout,
    ).until(
        lambda current_driver: (
            current_driver.execute_script(
                "return document.readyState"
            )
            == "complete"
        )
    )


def suppress_click_popups(driver, element=None):
    """
    Blocks JavaScript pop-under windows for the next click.

    It also forces the selected button or link to remain
    inside the current tab.
    """

    try:
        driver.execute_script(
            """
            window.open = function () {
                return null;
            };

            if (arguments[0]) {
                arguments[0].removeAttribute("target");
            }
            """,
            element,
        )

    except Exception:
        pass


def keep_snaptik_tab(
    driver,
    preferred_handle=None,
):
    """
    Keeps one SnapTik tab and closes advertisement tabs.

    Returns the browser handle that was kept.
    """

    time.sleep(1)

    try:
        handles = list(driver.window_handles)
    except Exception:
        return None

    if not handles:
        return None

    snaptik_handles = []

    for handle in handles:
        try:
            driver.switch_to.window(handle)

            current_url = (
                driver.current_url or ""
            ).lower()

            if "snaptik." in current_url:
                snaptik_handles.append(handle)

        except (
            NoSuchWindowException,
            WebDriverException,
        ):
            continue

    if preferred_handle in snaptik_handles:
        keep_handle = preferred_handle

    elif snaptik_handles:
        keep_handle = snaptik_handles[-1]

    elif preferred_handle in handles:
        keep_handle = preferred_handle

    else:
        keep_handle = handles[0]

    for handle in list(handles):
        if handle == keep_handle:
            continue

        try:
            driver.switch_to.window(handle)
            driver.close()

        except Exception:
            pass

    try:
        driver.switch_to.window(keep_handle)

    except Exception:
        return None

    return keep_handle


# =========================================================
# TIKTOK LINK SCRAPER
# =========================================================

def login_and_scrape_links():
    print("🔍 Launching TikTok...")

    driver = init_browser(
        headless=False,
        reuse_profile=True,
    )

    try:
        driver.get(TIKTOK_SEARCH_URL)

        wait_for_page_ready(driver)

        time.sleep(3)

        input(
            "\n👉 Log into TikTok if needed, "
            "then press ENTER here..."
        )

        print(
            "\n📜 Scrolling and collecting "
            "video links..."
        )

        collected_links = set()

        for scroll_number in range(10):
            video_elements = driver.find_elements(
                By.CSS_SELECTOR,
                'a[href*="/video/"]',
            )

            for element in video_elements:
                try:
                    href = element.get_attribute(
                        "href"
                    )

                    if href and "/video/" in href:
                        clean_link = href.split("?")[0]

                        collected_links.add(
                            clean_link
                        )

                except Exception:
                    continue

            print(
                f"Scroll {scroll_number + 1}/10: "
                f"{len(collected_links)} "
                f"link(s) found"
            )

            driver.execute_script(
                "window.scrollBy(0, 1800);"
            )

            time.sleep(2)

        SCRAPER_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        sorted_links = sorted(
            collected_links
        )

        with LINKS_FILE.open(
            "w",
            encoding="utf-8",
        ) as file:
            for link in sorted_links:
                file.write(link + "\n")

        print(
            f"\n✅ Saved {len(sorted_links)} "
            f"link(s) to:\n"
            f"{LINKS_FILE}"
        )

        input(
            "\nPress ENTER to close TikTok..."
        )

    except Exception as error:
        print(
            f"❌ TikTok scraping error: "
            f"{type(error).__name__}: "
            f"{error}"
        )

    finally:
        try:
            driver.quit()

        except Exception:
            pass


# =========================================================
# DOWNLOAD HELPERS
# =========================================================

def get_download_files(download_dir=None):
    download_dir = Path(download_dir or DOWNLOAD_DIR)
    download_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return {
        path
        for path in download_dir.iterdir()
        if path.is_file()
    }


def wait_for_download(
    files_before,
    timeout=75,
    download_dir=None,
):
    deadline = time.time() + timeout

    while time.time() < deadline:
        current_files = get_download_files(download_dir)

        new_files = (
            current_files - files_before
        )

        incomplete_files = [
            path
            for path in new_files
            if path.name.endswith(
                (".crdownload", ".tmp")
            )
        ]

        completed_files = [
            path
            for path in new_files
            if not path.name.endswith(
                (".crdownload", ".tmp")
            )
        ]

        if (
            completed_files
            and not incomplete_files
        ):
            return max(
                completed_files,
                key=lambda path: (
                    path.stat().st_mtime
                ),
            )

        time.sleep(1)

    return None


def save_debug_files(driver, prefix="snaptik-debug"):
    timestamp = int(time.time())

    screenshot_path = (
        PROJECT_DIR
        / f"{prefix}-{timestamp}.png"
    )

    html_path = (
        PROJECT_DIR
        / f"{prefix}-{timestamp}.html"
    )

    try:
        driver.save_screenshot(
            str(screenshot_path)
        )

        html_path.write_text(
            driver.page_source,
            encoding="utf-8",
        )

        print(
            f"🧪 Screenshot:\n"
            f"{screenshot_path}"
        )

        print(
            f"🧪 HTML:\n"
            f"{html_path}"
        )

    except Exception as error:
        print(
            f"⚠️ Could not save debug files: "
            f"{error}"
        )


# =========================================================
# SNAP TIK PAGE ELEMENTS
# =========================================================

def find_snaptik_input(
    driver,
    timeout=12,
):
    selectors = [
        (
            By.NAME,
            "page",
        ),
        (
            By.CSS_SELECTOR,
            'input[name="page"]',
        ),
        (
            By.CSS_SELECTOR,
            'input[placeholder*="TikTok" i]',
        ),
        (
            By.CSS_SELECTOR,
            'input[placeholder*="link" i]',
        ),
        (
            By.CSS_SELECTOR,
            'input[type="text"]',
        ),
    ]

    deadline = time.time() + timeout

    while time.time() < deadline:
        for by_type, selector in selectors:
            try:
                elements = driver.find_elements(
                    by_type,
                    selector,
                )

                for element in elements:
                    if (
                        element.is_displayed()
                        and element.is_enabled()
                    ):
                        return element

            except Exception:
                continue

        time.sleep(0.5)

    raise TimeoutException(
        "Could not find the SnapTik "
        "TikTok-link input."
    )


def open_snaptik_home(driver):
    last_error = None

    for url in SNAPTIK_URLS:
        try:
            driver.get(url)

            wait_for_page_ready(
                driver,
                timeout=30,
            )

            time.sleep(2)

            input_box = find_snaptik_input(
                driver,
                timeout=10,
            )

            return input_box

        except Exception as error:
            last_error = error
            continue

    raise TimeoutException(
        "Could not open a working "
        f"SnapTik page: {last_error}"
    )


def find_first_download_button(
    driver,
    timeout=15,
):
    """
    Finds the first white Download button next
    to the TikTok URL input.
    """

    xpath = (
        "//button["
        "@type='submit' or "
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')="
        "'download'"
        "]"
    )

    return WebDriverWait(
        driver,
        timeout,
    ).until(
        EC.element_to_be_clickable(
            (By.XPATH, xpath)
        )
    )


def find_real_download_button(
    driver,
    timeout=45,
):
    """
    Finds only the blue:
        Download (No Watermark)

    Ignores:
        Download Extension
        Download HD (No Watermark)
        Watch Ad
    """

    lowercase_text = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')"
    )

    xpath = (
        "//*[self::a or self::button]["
        f"contains("
        f"{lowercase_text}, "
        f"'download (no watermark)'"
        f")"
        f" and not(contains("
        f"{lowercase_text}, "
        f"'download hd'"
        f"))"
        f" and not(contains("
        f"{lowercase_text}, "
        f"'extension'"
        f"))"
        f" and not(contains("
        f"{lowercase_text}, "
        f"'watch ad'"
        f"))"
        "]"
    )

    deadline = time.time() + timeout

    while time.time() < deadline:
        keep_snaptik_tab(driver)

        try:
            elements = driver.find_elements(
                By.XPATH,
                xpath,
            )

            for element in elements:
                try:
                    if (
                        element.is_displayed()
                        and element.is_enabled()
                    ):
                        return element

                except StaleElementReferenceException:
                    continue

        except WebDriverException:
            raise

        time.sleep(1)

    raise TimeoutException(
        "The blue 'Download (No Watermark)' "
        "button did not appear."
    )


# =========================================================
# SNAP TIK DOWNLOADER
# =========================================================

def download_from_snaptik(
    driver,
    tiktok_link,
):
    print(
        f"\n🎬 Downloading from SnapTik:\n"
        f"{tiktok_link}"
    )

    try:
        # Step 1:
        # Open SnapTik and enter the TikTok URL.
        input_box = open_snaptik_home(driver)

        input_box.click()
        input_box.clear()
        input_box.send_keys(tiktok_link)

        # Step 2:
        # Click the first white Download button.
        first_download_button = (
            find_first_download_button(driver)
        )

        driver.execute_script(
            """
            arguments[0].scrollIntoView({
                block: "center"
            });
            """,
            first_download_button,
        )

        suppress_click_popups(
            driver,
            first_download_button,
        )

        first_handle = (
            driver.current_window_handle
        )

        print(
            "➡️ Clicking the first "
            "Download button..."
        )

        try:
            first_download_button.click()

        except Exception:
            driver.execute_script(
                "arguments[0].click();",
                first_download_button,
            )

        time.sleep(2)

        kept_handle = keep_snaptik_tab(
            driver,
            preferred_handle=first_handle,
        )

        if kept_handle is None:
            raise WebDriverException(
                "SnapTik tab disappeared after "
                "the first click."
            )

        # Step 3:
        # Wait for the results screen.
        print(
            "⏳ Waiting for the blue "
            "'Download (No Watermark)' "
            "button..."
        )

        real_download_button = (
            find_real_download_button(
                driver,
                timeout=45,
            )
        )

        print(
            "✅ Found the blue "
            "'Download (No Watermark)' "
            "button."
        )

        files_before = get_download_files()

        # Step 4:
        # Click only the blue no-watermark button.
        for click_attempt in range(1, 3):
            try:
                if click_attempt > 1:
                    real_download_button = (
                        find_real_download_button(
                            driver,
                            timeout=15,
                        )
                    )

                driver.execute_script(
                    """
                    arguments[0].scrollIntoView({
                        block: "center"
                    });
                    """,
                    real_download_button,
                )

                suppress_click_popups(
                    driver,
                    real_download_button,
                )

                current_handle = (
                    driver.current_window_handle
                )

                button_text = (
                    real_download_button
                    .text
                    .strip()
                )

                print(
                    f"⬇️ Clicking: "
                    f"{button_text or 'Download (No Watermark)'}"
                )

                try:
                    real_download_button.click()

                except Exception:
                    driver.execute_script(
                        "arguments[0].click();",
                        real_download_button,
                    )

                time.sleep(2)

                keep_snaptik_tab(
                    driver,
                    preferred_handle=current_handle,
                )

                print(
                    "⏳ Waiting for the "
                    "video file..."
                )

                downloaded_file = (
                    wait_for_download(
                        files_before,
                        timeout=45,
                    )
                )

                if downloaded_file:
                    print(
                        "✅ Downloaded "
                        "successfully:\n"
                        f"{downloaded_file}"
                    )

                    return True

                print(
                    f"⚠️ No file detected "
                    f"after click "
                    f"{click_attempt}/2."
                )

            except StaleElementReferenceException:
                continue

        print(
            "❌ The blue download button "
            "was clicked, but no new video "
            "file appeared."
        )

        try:
            print(
                f"Current page: "
                f"{driver.current_url}"
            )

        except Exception:
            pass

        save_debug_files(driver)

        return False

    except TimeoutException as error:
        print(
            f"❌ SnapTik timed out: "
            f"{error}"
        )

        try:
            print(
                f"Current page: "
                f"{driver.current_url}"
            )

            print(
                f"Page title: "
                f"{driver.title}"
            )

        except Exception:
            pass

        save_debug_files(driver)

        return False

    except WebDriverException:
        raise

    except Exception as error:
        print(
            f"❌ Download error: "
            f"{type(error).__name__}: "
            f"{error}"
        )

        save_debug_files(driver)

        return False


# =========================================================
# MAIN DOWNLOAD LOOP
# =========================================================

def download_all_links():
    print("\n📁 Starting downloads...")

    if not LINKS_FILE.exists():
        print(
            f"❌ No links file found:\n"
            f"{LINKS_FILE}"
        )

        return

    with LINKS_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        links = [
            line.strip()
            for line in file
            if line.strip()
        ]

    # Remove duplicates while keeping order.
    links = list(dict.fromkeys(links))

    if not links:
        print("❌ links.txt is empty.")

        return

    print(
        f"🔗 Found {len(links)} "
        f"unique link(s)."
    )

    driver = None
    successful = 0
    failed = 0

    try:
        driver = init_browser(
            headless=False,
            reuse_profile=False,
        )

        for index, link in enumerate(
            links,
            start=1,
        ):
            print("\n" + "=" * 60)

            print(
                f"CURRENT: "
                f"{index}/{len(links)}"
            )

            completed = False

            for browser_attempt in range(1, 3):
                try:
                    completed = (
                        download_from_snaptik(
                            driver,
                            link,
                        )
                    )

                    break

                except WebDriverException as error:
                    print(
                        "❌ Chrome session failed "
                        f"(attempt "
                        f"{browser_attempt}/2):\n"
                        f"{error}"
                    )

                    try:
                        driver.quit()

                    except Exception:
                        pass

                    if browser_attempt == 2:
                        break

                    print(
                        "🔄 Restarting Chrome..."
                    )

                    time.sleep(3)

                    driver = init_browser(
                        headless=False,
                        reuse_profile=False,
                    )

            if completed:
                successful += 1

            else:
                failed += 1

            time.sleep(2)

    finally:
        if driver is not None:
            try:
                driver.quit()

            except Exception:
                pass

    print("\n" + "=" * 60)

    print("📊 DOWNLOAD SUMMARY")
    print(f"✅ Successful: {successful}")
    print(f"❌ Failed: {failed}")
    print(
        f"📁 Download folder: "
        f"{DOWNLOAD_DIR}"
    )


# =========================================================
# INSTAGRAM PROFILE / HIGHLIGHT HELPERS
# =========================================================

def normalize_instagram_profile_url(profile_url):
    """Return a canonical Instagram profile URL and its username."""
    profile_url = profile_url.strip()

    if profile_url.startswith("@"):
        username = profile_url[1:].strip("/")
        return (
            f"https://www.instagram.com/{username}/?hl=en",
            username,
        )

    if "://" not in profile_url:
        if profile_url.startswith("instagram.com/"):
            profile_url = "https://www." + profile_url
        elif profile_url.startswith("www.instagram.com/"):
            profile_url = "https://" + profile_url
        else:
            username = profile_url.strip("/")
            return (
                f"https://www.instagram.com/{username}/?hl=en",
                username,
            )

    parsed = urlparse(profile_url)
    path_parts = [part for part in parsed.path.split("/") if part]

    if not path_parts:
        raise ValueError("Could not find an Instagram username in that URL.")

    if path_parts[0] in {"stories", "p", "reel", "reels"}:
        raise ValueError(
            "Please enter an Instagram PROFILE URL, not a post, reel, "
            "story, or highlight URL."
        )

    username = path_parts[0].lstrip("@")
    canonical_url = f"https://www.instagram.com/{username}/?hl=en"

    return canonical_url, username


def instagram_highlight_id(highlight_url):
    parsed = urlparse(highlight_url)
    parts = [part for part in parsed.path.split("/") if part]

    try:
        index = parts.index("highlights")
        return parts[index + 1]
    except (ValueError, IndexError):
        return "unknown-highlight"


def collect_visible_instagram_highlights(driver, collected):
    """Collect currently rendered /stories/highlights/ links."""
    elements = driver.find_elements(
        By.CSS_SELECTOR,
        'a[href*="/stories/highlights/"]',
    )

    added = 0

    for element in elements:
        try:
            href = element.get_attribute("href")
            if not href:
                continue

            full_url = urljoin("https://www.instagram.com", href)
            clean_url = full_url.split("?")[0]

            if "/stories/highlights/" not in clean_url:
                continue

            name = (element.text or "").strip()

            if not name:
                name = (element.get_attribute("aria-label") or "").strip()

            if not name:
                try:
                    image = element.find_element(By.TAG_NAME, "img")
                    name = (image.get_attribute("alt") or "").strip()
                except Exception:
                    pass

            if clean_url not in collected:
                collected[clean_url] = name or instagram_highlight_id(clean_url)
                added += 1

        except (StaleElementReferenceException, WebDriverException):
            continue

    return added


def scroll_instagram_highlight_tray(driver):
    """Move an overflowing Highlight tray to the right if one is present."""
    try:
        return bool(
            driver.execute_script(
                r"""
                const links = Array.from(
                    document.querySelectorAll(
                        'a[href*="/stories/highlights/"]'
                    )
                );

                for (const link of links) {
                    let node = link.parentElement;

                    while (node && node !== document.body) {
                        if (node.scrollWidth > node.clientWidth + 8) {
                            const before = node.scrollLeft;
                            node.scrollLeft = Math.min(
                                node.scrollLeft + Math.max(node.clientWidth * 0.8, 250),
                                node.scrollWidth
                            );
                            node.dispatchEvent(new Event('scroll', {bubbles: true}));

                            if (node.scrollLeft > before + 1) {
                                return true;
                            }
                        }

                        node = node.parentElement;
                    }
                }

                return false;
                """
            )
        )
    except Exception:
        return False


def click_instagram_highlight_next(driver):
    """Fallback for layouts that expose a Next arrow instead of scrollLeft."""
    xpath = (
        "//*[self::button or @role='button']["
        "@aria-label='Next' or .//*[@aria-label='Next']]"
    )

    try:
        buttons = driver.find_elements(By.XPATH, xpath)
    except Exception:
        return False

    for button in buttons:
        try:
            if button.is_displayed() and button.is_enabled():
                driver.execute_script("arguments[0].click();", button)
                return True
        except Exception:
            continue

    return False


def scrape_instagram_highlights(profile_url):
    """
    Open an Instagram profile and discover every Highlight URL exposed by
    the profile's Highlight tray.
    """
    profile_url, username = normalize_instagram_profile_url(profile_url)

    print("\n⭐ Launching Instagram to collect Highlights...")
    print(f"Profile: @{username}")

    driver = init_browser(
        headless=False,
        reuse_profile=True,
        download_dir=INSTAGRAM_BASE_DOWNLOAD_DIR / username,
    )

    collected = {}

    try:
        driver.get(profile_url)
        wait_for_page_ready(driver, timeout=30)
        time.sleep(4)

        input(
            "\n👉 Log into Instagram if needed. Make sure the profile "
            "and its Highlight circles are visible, then press ENTER..."
        )

        unchanged_rounds = 0
        previous_count = -1

        for scan_number in range(1, 31):
            collect_visible_instagram_highlights(driver, collected)

            print(
                f"⭐ Highlight scan {scan_number}: "
                f"{len(collected)} unique link(s) found"
            )

            if len(collected) == previous_count:
                unchanged_rounds += 1
            else:
                unchanged_rounds = 0

            previous_count = len(collected)

            moved = scroll_instagram_highlight_tray(driver)

            if not moved:
                moved = click_instagram_highlight_next(driver)

            if unchanged_rounds >= 3 and not moved:
                break

            time.sleep(1)

        SCRAPER_DIR.mkdir(parents=True, exist_ok=True)

        with INSTAGRAM_HIGHLIGHTS_FILE.open(
            "w",
            encoding="utf-8",
        ) as file:
            for highlight_url in collected:
                file.write(highlight_url + "\n")

        print("\n" + "=" * 60)
        print(f"⭐ Found {len(collected)} Instagram Highlight(s)")

        for index, (url, name) in enumerate(collected.items(), start=1):
            print(f"{index}. {name}")
            print(f"   {url}")

        print(
            f"\n✅ Saved Highlight URLs to:\n"
            f"{INSTAGRAM_HIGHLIGHTS_FILE}"
        )

        if not collected:
            print(
                "\n⚠️ No Highlight links were found. If the account has "
                "Highlights, confirm you are logged in and that the circles "
                "are visible on the profile before pressing ENTER."
            )

        return list(collected.keys())

    except Exception as error:
        print(
            f"❌ Instagram Highlight scraping error: "
            f"{type(error).__name__}: {error}"
        )
        save_debug_files(driver, prefix="instagram-debug")
        return []

    finally:
        try:
            driver.quit()
        except Exception:
            pass


# =========================================================
# SNAPINSTA PAGE HELPERS
# =========================================================

def keep_snapinsta_tab(driver, preferred_handle=None):
    """Keep the SnapInsta tab and close obvious popup/ad tabs."""
    time.sleep(1)

    try:
        handles = list(driver.window_handles)
    except Exception:
        return None

    if not handles:
        return None

    snapinsta_handles = []

    for handle in handles:
        try:
            driver.switch_to.window(handle)
            current_url = (driver.current_url or "").lower()

            if "snapinsta." in current_url or "snap-insta." in current_url:
                snapinsta_handles.append(handle)
        except (NoSuchWindowException, WebDriverException):
            continue

    if preferred_handle in snapinsta_handles:
        keep_handle = preferred_handle
    elif snapinsta_handles:
        keep_handle = snapinsta_handles[-1]
    elif preferred_handle in handles:
        keep_handle = preferred_handle
    else:
        keep_handle = handles[0]

    for handle in list(handles):
        if handle == keep_handle:
            continue

        try:
            driver.switch_to.window(handle)
            driver.close()
        except Exception:
            pass

    try:
        driver.switch_to.window(keep_handle)
    except Exception:
        return None

    return keep_handle


def find_snapinsta_input(driver, timeout=15):
    selectors = [
        (By.CSS_SELECTOR, 'input[placeholder*="Instagram" i]'),
        (By.CSS_SELECTOR, 'input[placeholder*="URL" i]'),
        (By.CSS_SELECTOR, 'input[name="url"]'),
        (By.CSS_SELECTOR, 'input[name="link"]'),
        (By.CSS_SELECTOR, 'input[type="text"]'),
    ]

    deadline = time.time() + timeout

    while time.time() < deadline:
        for by_type, selector in selectors:
            try:
                elements = driver.find_elements(by_type, selector)

                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        return element
            except Exception:
                continue

        time.sleep(0.5)

    raise TimeoutException("Could not find the SnapInsta Instagram URL input.")


def open_snapinsta_home(driver):
    last_error = None

    for url in SNAPINSTA_URLS:
        try:
            driver.get(url)
            wait_for_page_ready(driver, timeout=30)
            time.sleep(2)

            input_box = find_snapinsta_input(driver, timeout=10)
            return input_box
        except Exception as error:
            last_error = error
            continue

    raise TimeoutException(
        "Could not open a working SnapInsta page: "
        f"{last_error}"
    )


def find_snapinsta_submit_button(driver, input_box, timeout=15):
    """Find the main Download button associated with the URL form."""
    try:
        form = input_box.find_element(By.XPATH, "ancestor::form[1]")
        buttons = form.find_elements(
            By.XPATH,
            ".//*[self::button or self::input]["
            "@type='submit' or "
            "contains(translate(@value, "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
            "'download') or "
            "contains(translate(normalize-space(.), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
            "'download')]",
        )

        for button in buttons:
            if button.is_displayed() and button.is_enabled():
                return button
    except Exception:
        pass

    lowercase_text = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')"
    )

    xpath = (
        "//*[self::button or self::input]["
        "@type='submit' or "
        f"contains({lowercase_text}, 'download') or "
        "contains(translate(@value, "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'download')]"
    )

    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xpath))
    )


def get_snapinsta_media_buttons(driver):
    """Return only result buttons for photos/videos, never the avatar."""
    lowercase_text = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')"
    )

    xpath = (
        "//*[self::a or self::button]["
        f"contains({lowercase_text}, 'download photo') or "
        f"contains({lowercase_text}, 'download video')"
        "]"
    )

    results = []

    try:
        elements = driver.find_elements(By.XPATH, xpath)
    except Exception:
        return results

    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                results.append(element)
        except StaleElementReferenceException:
            continue

    return results


def click_snapinsta_load_more(driver):
    """Click a results-page Load More/Show More button if one exists."""
    lowercase_text = (
        "translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')"
    )

    xpath = (
        "//*[self::button or self::a]["
        f"{lowercase_text}='load more' or "
        f"{lowercase_text}='show more' or "
        f"{lowercase_text}='see more'"
        "]"
    )

    try:
        elements = driver.find_elements(By.XPATH, xpath)
    except Exception:
        return False

    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    element,
                )
                driver.execute_script("arguments[0].click();", element)
                return True
        except Exception:
            continue

    return False


def expand_snapinsta_results(driver, max_rounds=40):
    """Scroll/click through lazy-loaded profile results until they stop growing."""
    previous_count = -1
    unchanged_rounds = 0

    for _ in range(max_rounds):
        buttons = get_snapinsta_media_buttons(driver)
        count = len(buttons)

        if count == previous_count:
            unchanged_rounds += 1
        else:
            unchanged_rounds = 0

        previous_count = count

        clicked_more = click_snapinsta_load_more(driver)

        try:
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
        except Exception:
            pass

        if unchanged_rounds >= 3 and not clicked_more:
            break

        time.sleep(1.25)

    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass

    return len(get_snapinsta_media_buttons(driver))


def wait_for_snapinsta_results(driver, timeout=60):
    deadline = time.time() + timeout

    while time.time() < deadline:
        keep_snapinsta_tab(driver)

        buttons = get_snapinsta_media_buttons(driver)
        if buttons:
            return buttons

        time.sleep(1)

    raise TimeoutException(
        "SnapInsta did not return any Download Photo/Download Video buttons."
    )


def download_snapinsta_result_media(driver, download_dir):
    """Click every Download Photo / Download Video result on the page."""
    download_dir = set_download_dir(driver, download_dir)

    total = expand_snapinsta_results(driver)

    if total == 0:
        raise TimeoutException(
            "SnapInsta returned a page, but no downloadable photo/video "
            "buttons were found."
        )

    print(f"📦 SnapInsta returned {total} media item(s).")

    successful = 0
    failed = 0

    for index in range(total):
        # Re-find each time because button references can become stale after clicks.
        buttons = get_snapinsta_media_buttons(driver)

        if index >= len(buttons):
            print(
                f"⚠️ Result list changed before item {index + 1}/{total}."
            )
            failed += total - index
            break

        button = buttons[index]

        try:
            label = (button.text or "Download media").strip()
            files_before = get_download_files(download_dir)

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                button,
            )
            time.sleep(0.4)

            current_handle = driver.current_window_handle
            page_before_click = driver.current_url

            print(f"⬇️ {index + 1}/{total}: {label}")

            # Keep the result in the current tab and suppress pop-under ads.
            suppress_click_popups(driver, button)

            try:
                button.click()
            except Exception:
                driver.execute_script("arguments[0].click();", button)

            time.sleep(2)
            keep_snapinsta_tab(driver, preferred_handle=current_handle)

            downloaded_file = wait_for_download(
                files_before,
                timeout=45,
                download_dir=download_dir,
            )

            if downloaded_file:
                successful += 1
                print(f"✅ Saved: {downloaded_file.name}")
                continue

            failed += 1
            print("⚠️ No new local file was detected for this item.")

            # If the link navigated away instead of downloading, return to results.
            try:
                if driver.current_url != page_before_click:
                    driver.back()
                    wait_for_page_ready(driver, timeout=20)
                    time.sleep(1)
            except Exception:
                pass

        except (StaleElementReferenceException, WebDriverException) as error:
            failed += 1
            print(
                f"⚠️ Could not download item {index + 1}: "
                f"{type(error).__name__}: {error}"
            )

    return successful, failed


def download_from_snapinsta(
    driver,
    instagram_url,
    download_dir,
    label="Instagram media",
):
    print("\n" + "=" * 60)
    print(f"📸 {label}")
    print(instagram_url)

    try:
        set_download_dir(driver, download_dir)
        input_box = open_snapinsta_home(driver)

        input_box.click()
        input_box.clear()
        input_box.send_keys(instagram_url)

        submit_button = find_snapinsta_submit_button(
            driver,
            input_box,
            timeout=15,
        )

        first_handle = driver.current_window_handle

        print("➡️ Sending URL to SnapInsta...")

        try:
            submit_button.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_button)

        time.sleep(3)
        kept_handle = keep_snapinsta_tab(
            driver,
            preferred_handle=first_handle,
        )

        if kept_handle is None:
            raise WebDriverException(
                "SnapInsta tab disappeared after submitting the URL."
            )

        print("⏳ Waiting for SnapInsta results...")
        wait_for_snapinsta_results(driver, timeout=60)

        successful, failed = download_snapinsta_result_media(
            driver,
            download_dir,
        )

        print(
            f"✅ Finished {label}: "
            f"{successful} downloaded, {failed} failed."
        )

        return successful > 0 and failed == 0

    except TimeoutException as error:
        print(f"❌ SnapInsta timed out: {error}")
        try:
            print(f"Current page: {driver.current_url}")
        except Exception:
            pass
        save_debug_files(driver, prefix="snapinsta-debug")
        return False

    except WebDriverException:
        raise

    except Exception as error:
        print(
            f"❌ SnapInsta error: "
            f"{type(error).__name__}: {error}"
        )
        save_debug_files(driver, prefix="snapinsta-debug")
        return False


# =========================================================
# INSTAGRAM DOWNLOAD FLOWS
# =========================================================

def read_saved_instagram_highlights():
    if not INSTAGRAM_HIGHLIGHTS_FILE.exists():
        print(
            f"❌ No saved Instagram Highlight file found:\n"
            f"{INSTAGRAM_HIGHLIGHTS_FILE}"
        )
        return []

    with INSTAGRAM_HIGHLIGHTS_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        links = [line.strip() for line in file if line.strip()]

    return list(dict.fromkeys(links))


def download_instagram_profile(profile_url):
    profile_url, username = normalize_instagram_profile_url(profile_url)
    download_dir = (
        INSTAGRAM_BASE_DOWNLOAD_DIR
        / username
        / "Posts-Reels"
    )

    driver = None

    try:
        driver = init_browser(
            headless=False,
            reuse_profile=False,
            download_dir=download_dir,
        )

        # Give the user a chance to log into Instagram manually before
        # the downloader continues. Keep this SAME browser window open:
        # the login session stays available while we later navigate to SnapInsta.
        print("\n🔐 Opening Instagram before the download starts...")
        driver.get(profile_url)
        wait_for_page_ready(driver, timeout=30)
        time.sleep(3)

        input(
            "\n👉 Log into Instagram in the Chrome window if needed. "
            "Once you are fully logged in and can see the profile, "
            "press ENTER here to continue..."
        )

        print("\n✅ Continuing to SnapInsta...")

        return download_from_snapinsta(
            driver,
            profile_url,
            download_dir,
            label=f"@{username} profile posts/reels",
        )

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def download_instagram_highlights(profile_url, highlight_links=None):
    _, username = normalize_instagram_profile_url(profile_url)

    if highlight_links is None:
        highlight_links = read_saved_instagram_highlights()

    highlight_links = list(dict.fromkeys(highlight_links))

    if not highlight_links:
        print("❌ No Instagram Highlight links to download.")
        return

    base_dir = (
        INSTAGRAM_BASE_DOWNLOAD_DIR
        / username
        / "Highlights"
    )

    driver = None
    successful = 0
    failed = 0

    try:
        driver = init_browser(
            headless=False,
            reuse_profile=False,
            download_dir=base_dir,
        )

        for index, highlight_url in enumerate(highlight_links, start=1):
            highlight_id = instagram_highlight_id(highlight_url)
            highlight_dir = base_dir / highlight_id

            print("\n" + "#" * 60)
            print(
                f"⭐ HIGHLIGHT {index}/{len(highlight_links)} "
                f"({highlight_id})"
            )

            completed = False

            for browser_attempt in range(1, 3):
                try:
                    completed = download_from_snapinsta(
                        driver,
                        highlight_url,
                        highlight_dir,
                        label=f"Highlight {highlight_id}",
                    )
                    break

                except WebDriverException as error:
                    print(
                        "❌ Chrome session failed "
                        f"(attempt {browser_attempt}/2):\n{error}"
                    )

                    try:
                        driver.quit()
                    except Exception:
                        pass

                    if browser_attempt == 2:
                        break

                    print("🔄 Restarting Chrome...")
                    time.sleep(3)

                    driver = init_browser(
                        headless=False,
                        reuse_profile=False,
                        download_dir=highlight_dir,
                    )

            if completed:
                successful += 1
            else:
                failed += 1

            time.sleep(2)

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    print("\n" + "=" * 60)
    print("📊 INSTAGRAM HIGHLIGHT SUMMARY")
    print(f"✅ Successful Highlights: {successful}")
    print(f"❌ Failed Highlights: {failed}")
    print(f"📁 Download folder: {base_dir}")


def download_full_instagram_account(profile_url):
    """
    1. Scrape Highlight URLs from Instagram.
    2. Send the original profile URL to SnapInsta for posts/reels.
    3. Send every discovered Highlight URL to SnapInsta separately.
    """
    profile_url, username = normalize_instagram_profile_url(profile_url)

    print("\n" + "=" * 60)
    print(f"📸 FULL INSTAGRAM DOWNLOAD FOR @{username}")
    print("=" * 60)

    highlight_links = scrape_instagram_highlights(profile_url)

    print("\n📸 Downloading profile posts/reels through SnapInsta...")
    download_instagram_profile(profile_url)

    if highlight_links:
        print("\n⭐ Downloading discovered Highlights through SnapInsta...")
        download_instagram_highlights(
            profile_url,
            highlight_links=highlight_links,
        )
    else:
        print("\n⚠️ No Highlights were discovered, so only the profile was downloaded.")


# =========================================================
# MENUS
# =========================================================

def tiktok_menu():
    print("\nTikTok options:")
    print("1. Download links already in links.txt")
    print("2. Scrape TikTok links")
    print("3. Scrape links, then download them")

    choice = input("\nEnter 1, 2, or 3: ").strip()

    if choice == "1":
        download_all_links()
    elif choice == "2":
        login_and_scrape_links()
    elif choice == "3":
        login_and_scrape_links()
        download_all_links()
    else:
        print("❌ Invalid TikTok choice.")


def instagram_menu():
    print("\nInstagram options:")
    print("1. Download profile posts/reels through SnapInsta")
    print("2. Scrape Highlight URLs only")
    print("3. Download saved Highlight URLs")
    print("4. Scrape Highlights, then download those Highlights")
    print("5. Download EVERYTHING: profile posts/reels + Highlights")

    choice = input("\nEnter 1, 2, 3, 4, or 5: ").strip()

    if choice not in {"1", "2", "3", "4", "5"}:
        print("❌ Invalid Instagram choice.")
        return

    profile_url = input(
        "\nInstagram profile URL or @username: "
    ).strip()

    try:
        profile_url, username = normalize_instagram_profile_url(profile_url)
    except ValueError as error:
        print(f"❌ {error}")
        return

    print(f"\n📸 Using Instagram profile: @{username}")
    print(profile_url)

    if choice == "1":
        download_instagram_profile(profile_url)

    elif choice == "2":
        scrape_instagram_highlights(profile_url)

    elif choice == "3":
        download_instagram_highlights(profile_url)

    elif choice == "4":
        highlight_links = scrape_instagram_highlights(profile_url)
        if highlight_links:
            download_instagram_highlights(
                profile_url,
                highlight_links=highlight_links,
            )

    elif choice == "5":
        download_full_instagram_account(profile_url)


# =========================================================
# PROGRAM START
# =========================================================

def main():
    print("\nSOCIAL MEDIA SCRAPER / DOWNLOADER")
    print("1. TikTok")
    print("2. Instagram")

    platform_choice = input(
        "\nChoose a platform (1 or 2): "
    ).strip()

    if platform_choice == "1":
        tiktok_menu()
    elif platform_choice == "2":
        instagram_menu()
    else:
        print("❌ Invalid platform choice. Please run the program again.")


if __name__ == "__main__":
    main()