from __future__ import annotations

import os
import platform
import re
import shutil
import ssl
import subprocess
from pathlib import Path

try:
    import certifi
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass


def detect_chrome_major() -> int | None:
    candidates: list[list[str]] = []
    system = platform.system()
    if system == "Darwin":
        candidates.append(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"])
        candidates.append(["/Applications/Chromium.app/Contents/MacOS/Chromium", "--version"])
    elif system == "Windows":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append([str(Path(base) / "Google/Chrome/Application/chrome.exe"), "--version"])
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            executable = shutil.which(name)
            if executable:
                candidates.append([executable, "--version"])

    for command in candidates:
        try:
            output = subprocess.check_output(command, stderr=subprocess.DEVNULL, text=True, timeout=5)
        except Exception:
            continue
        match = re.search(r"(\d+)\.", output)
        if match:
            return int(match.group(1))
    return None


def _patch_undetected_chromedriver_for_apple_silicon() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return
    import undetected_chromedriver as uc
    from undetected_chromedriver.patcher import Patcher

    if getattr(Patcher, "_product_pipeline_patched", False):
        return

    original_set_platform = Patcher._set_platform_name
    original_patch_exe = Patcher.patch_exe

    def set_platform_name(self):
        original_set_platform(self)
        if self.platform.endswith("darwin") and not self.is_old_chromedriver:
            self.platform_name = "mac-arm64"

    def patch_exe(self):
        result = original_patch_exe(self)
        subprocess.run(
            ["codesign", "--force", "--sign", "-", self.executable_path],
            check=False,
            capture_output=True,
        )
        return result

    Patcher._set_platform_name = set_platform_name
    Patcher.patch_exe = patch_exe
    Patcher._product_pipeline_patched = True
    del uc


def clear_profile_locks(profile_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = profile_dir / name
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except OSError:
            pass


def init_browser(
    profile_dir: Path,
    download_dir: Path,
    *,
    headless: bool = False,
):
    try:
        _patch_undetected_chromedriver_for_apple_silicon()
        import undetected_chromedriver as uc
    except ImportError as exc:
        raise RuntimeError(
            "Browser automation dependencies are missing. Run: pip install -r requirements.txt"
        ) from exc

    profile_dir = Path(profile_dir).expanduser().resolve()
    download_dir = Path(download_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    clear_profile_locks(profile_dir)

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    if headless:
        options.add_argument("--headless=new")

    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "profile.default_content_setting_values.popups": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_setting_values.automatic_downloads": 1,
        },
    )

    kwargs = {
        "options": options,
        "user_data_dir": str(profile_dir),
        "use_subprocess": True,
    }
    major = detect_chrome_major()
    if major:
        kwargs["version_main"] = major

    driver = uc.Chrome(**kwargs)
    set_download_dir(driver, download_dir)
    return driver


def set_download_dir(driver, download_dir: Path) -> None:
    download_dir = Path(download_dir).expanduser().resolve()
    download_dir.mkdir(parents=True, exist_ok=True)
    try:
        driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(download_dir)},
        )
    except Exception:
        pass
