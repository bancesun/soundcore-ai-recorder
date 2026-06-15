#!/usr/bin/env python3
"""Open Soundcore in a persistent browser profile and collect downloads."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".soundcore_manifest.json"
INBOX = ROOT / "downloads" / "inbox"
PROFILE = ROOT / ".browser-profile" / "soundcore"
URL = "https://ai.soundcore.com/home"


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def processed_dates() -> set[str]:
    if not MANIFEST.exists():
        return set()
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return set(data.get("dates", {}).keys())


def processed_recording_stems() -> set[str]:
    if not MANIFEST.exists():
        return set()
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    stems: set[str] = set()
    for entry in data.get("files", {}).values():
        original_name = entry.get("original_name")
        audio_archive = entry.get("audio_archive")
        if original_name:
            stems.add(Path(original_name).stem)
        if audio_archive:
            stems.add(Path(audio_archive).stem)
    return stems


def missing_dates() -> list[str]:
    tz = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))
    lookback = int(os.environ.get("LOOKBACK_DAYS", "14"))
    today = datetime.now(tz).date()
    done = processed_dates()
    dates = [(today - timedelta(days=offset)).isoformat() for offset in range(lookback)]
    return [date for date in reversed(dates) if date not in done]


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w ._()-]+", "_", name, flags=re.UNICODE).strip()
    return cleaned or "soundcore-recording"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def export_audio(page, title: str) -> Path | None:
    expected_stem = safe_filename(title.replace(":", "_"))
    if expected_stem in processed_recording_stems():
        print(f"Already archived: {title}")
        return None
    if list(INBOX.glob(f"{expected_stem}.*")):
        print(f"Already downloaded: {title}")
        return None

    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    file_title = page.locator(".file-title").filter(has_text=title)
    file_title.click(timeout=20000)
    page.wait_for_timeout(3500)
    page.locator("button.ant-dropdown-trigger").first.click(timeout=20000)
    page.wait_for_timeout(800)
    page.get_by_text("Export Audio", exact=True).click(timeout=10000)
    page.wait_for_timeout(1000)

    with page.expect_download(timeout=90000) as download_info:
        page.locator(".ant-modal-content").get_by_text("Export", exact=True).click(timeout=10000)
    download = download_info.value
    target = unique_path(INBOX / download.suggested_filename)
    download.save_as(str(target))
    print(f"Downloaded: {target}")
    return target


def main() -> None:
    load_env()
    INBOX.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)

    missing = missing_dates()
    if missing:
        print("Missing dates to download/process:")
        for date in missing:
            print(f"  - {date}")
    else:
        print("No missing dates in the current lookback window.")

    print(f"\nDownloads will be saved to: {INBOX}")
    print("Opening Soundcore with the saved Chrome profile.\n")

    with sync_playwright() as pw:
        channel = os.environ.get("SOUNDCORE_BROWSER_CHANNEL", "chrome").strip() or None
        headless = os.environ.get("SOUNDCORE_HEADLESS", "false").lower() in {"1", "true", "yes"}
        print(f"Browser channel: {channel or 'playwright default chromium'}")
        context = pw.chromium.launch_persistent_context(
            str(PROFILE),
            accept_downloads=True,
            downloads_path=str(INBOX),
            headless=headless,
            channel=channel,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            if "Log In" in page.locator("body").inner_text(timeout=10000):
                print("Login is required. Complete Apple login in the opened Chrome window, then run this again.")
                return
            titles = [
                title.strip()
                for title in page.locator(".file-title").all_inner_texts()
                if title.strip()
            ]
            if not titles:
                print("No Soundcore files found on the page.")
                return
            print("Found Soundcore files:")
            for title in titles:
                print(f"  - {title}")
            for title in titles:
                try:
                    export_audio(page, title)
                except TimeoutError as error:
                    print(f"Could not export {title}: {error}")
        finally:
            context.close()


if __name__ == "__main__":
    main()
