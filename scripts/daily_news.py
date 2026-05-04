#!/usr/bin/env python3
"""Download daily news items from RSS feeds and capture screenshots.

Sites protected by Cloudflare Turnstile, Vercel checkpoint, etc. cannot be
screenshot reliably from headless CI. This script detects those pages (so you
do not archive challenge screenshots), applies light browser hardening, and
supports per-source skip_screenshots in the sources JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
from dateutil import parser as date_parser
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LOGGER = logging.getLogger("daily_news")

# Substrings often present on Cloudflare Turnstile / Vercel checkpoint pages (headless gets stuck here).
_ANTIBOT_MARKERS = (
    "verify you are human",
    "performing security verification",
    "failed to verify your browser",
    "vercel security checkpoint",
    "checking your browser before accessing",
    "just a moment",
    "enable javascript and cookies to continue",
    "cf-turnstile",
    "challenges.cloudflare.com",
)

_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, "webdriver", { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
"""


def slugify(value: str, max_len: int = 80) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return (value[:max_len]).strip("-") or "news-item"


def load_sources(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("Sources file must contain a list.")
    return data


def html_to_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_publish_date(entry: Any) -> str | None:
    for key in ("published", "updated", "created"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = date_parser.parse(raw)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            continue
    return None


def _looks_like_antibot_challenge(page) -> bool:
    """Heuristic: many sites return a challenge HTML shell to headless automation."""
    try:
        url = (page.url or "").lower()
        if "__cf_chl" in url or "challenges.cloudflare.com" in url:
            return True
        title = (page.title() or "").lower()
        blob = title
        try:
            blob += "\n" + (page.inner_text("body", timeout=5000) or "").lower()
        except PlaywrightTimeoutError:
            pass
        if any(marker in blob for marker in _ANTIBOT_MARKERS):
            return True
        try:
            html = page.content().lower()
        except Exception:
            return False
        return "cf-turnstile" in html or "challenges.cloudflare.com" in html
    except Exception:
        return False


def capture_screenshot(
    page,
    url: str,
    destination: Path,
    timeout_ms: int,
    post_load_wait_ms: int,
) -> str | None:
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if post_load_wait_ms > 0:
            page.wait_for_timeout(post_load_wait_ms)
        if _looks_like_antibot_challenge(page):
            LOGGER.warning(
                "Skipped screenshot (anti-bot / challenge page). Use RSS or skip_screenshots for this source: %s",
                url,
            )
            return None
        page.screenshot(path=str(destination), full_page=True)
        return str(destination.name)
    except PlaywrightTimeoutError:
        LOGGER.warning("Timed out while loading URL for screenshot: %s", url)
        return None
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed screenshot for %s (%s)", url, exc)
        return None


def fetch_news(
    sources: list[dict[str, Any]],
    output_dir: Path,
    max_items_per_source: int,
    screenshot_limit: int | None,
    timeout_ms: int,
    post_load_wait_ms: int,
    use_stealth: bool,
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result: dict[str, Any] = {
        "generated_at": timestamp,
        "sources": [],
    }

    screenshot_count = 0
    screenshot_limit_reached_logged = False
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch_opts: dict[str, Any] = {"headless": True}
        if use_stealth:
            launch_opts["args"] = ["--disable-blink-features=AutomationControlled"]
        browser = p.chromium.launch(**launch_opts)
        context_kwargs: dict[str, Any] = {"viewport": {"width": 1440, "height": 900}}
        if use_stealth:
            context_kwargs.update(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )
        context = browser.new_context(**context_kwargs)
        if use_stealth:
            context.add_init_script(_STEALTH_INIT_SCRIPT)
        page = context.new_page()

        for source in sources:
            source_name = source.get("name", "unknown-source")
            rss_url = source.get("rss")
            site_url = source.get("url")
            skip_screenshots = bool(source.get("skip_screenshots", False))

            source_data: dict[str, Any] = {
                "name": source_name,
                "rss": rss_url,
                "url": site_url,
                "items": [],
            }

            if rss_url:
                LOGGER.info("Fetching source: %s", source_name)
                feed = feedparser.parse(rss_url)
                entries = feed.entries[:max_items_per_source]
            elif site_url:
                LOGGER.info("Capturing homepage source: %s", source_name)
                entries = [{"title": f"Homepage - {source_name}", "link": site_url, "summary": "Homepage snapshot"}]
            else:
                LOGGER.warning("Source %s missing RSS and URL; skipping.", source_name)
                continue

            for entry in entries:
                title = entry.get("title", "Untitled")
                link = entry.get("link")
                summary = entry.get("summary", "")
                published_at = parse_publish_date(entry)

                item: dict[str, Any] = {
                    "title": title,
                    "link": link,
                    "published_at": published_at,
                    "summary": summary,
                    "screenshot": None,
                }

                has_screenshot_budget = screenshot_limit is None or screenshot_count < screenshot_limit
                if link and has_screenshot_budget and not skip_screenshots:
                    filename = f"{screenshot_count + 1:03d}-{slugify(title)}.png"
                    filepath = screenshots_dir / filename
                    screenshot_name = capture_screenshot(
                        page,
                        link,
                        filepath,
                        timeout_ms,
                        post_load_wait_ms,
                    )
                    if screenshot_name:
                        item["screenshot"] = f"screenshots/{screenshot_name}"
                        screenshot_count += 1
                elif (
                    link
                    and not skip_screenshots
                    and screenshot_limit is not None
                    and not screenshot_limit_reached_logged
                ):
                    LOGGER.info(
                        "Screenshot limit reached (%s). Increase --max-screenshots to capture more images.",
                        screenshot_limit,
                    )
                    screenshot_limit_reached_logged = True

                source_data["items"].append(item)

            result["sources"].append(source_data)

        context.close()
        browser.close()

    return result


def write_markdown(data: dict[str, Any], output_path: Path) -> None:
    lines = [
        "# Daily News",
        "",
        f"_Generated at: {data.get('generated_at', 'unknown')}_",
        "",
    ]

    for source in data.get("sources", []):
        source_name = source.get("name", "Unknown Source")
        source_url = source.get("url")
        lines.append(f"## {source_name}")
        if source_url:
            lines.append(f"- Source: [{source_url}]({source_url})")

        items = source.get("items", [])
        if not items:
            lines.append("- _No items found._")
            lines.append("")
            continue

        for item in items:
            title = item.get("title", "Untitled")
            link = item.get("link")
            published_at = item.get("published_at")
            summary = html_to_text(item.get("summary", ""))
            screenshot = item.get("screenshot")

            if link:
                lines.append(f"- **[{title}]({link})**")
            else:
                lines.append(f"- **{title}**")
            if published_at:
                lines.append(f"  - Published (UTC): `{published_at}`")
            if summary:
                lines.append(f"  - Summary: {summary}")
            if screenshot:
                lines.append(f"  - Screenshot: ![]({screenshot})")

        lines.append("")

    output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default="news_sources.json", help="Path to sources JSON file")
    parser.add_argument("--output", default="artifacts", help="Directory where output is written")
    parser.add_argument("--max-items", type=int, default=5, help="Maximum RSS items per source")
    parser.add_argument(
        "--max-screenshots",
        type=int,
        default=0,
        help="Maximum screenshots per run (0 means unlimited)",
    )
    parser.add_argument("--timeout-ms", type=int, default=20000, help="Page load timeout in milliseconds")
    parser.add_argument(
        "--post-load-wait-ms",
        type=int,
        default=10000,
        help="Extra wait after domcontentloaded before screenshot (allows JS to render)",
    )
    stealth_group = parser.add_mutually_exclusive_group()
    stealth_group.add_argument(
        "--stealth",
        dest="stealth",
        action="store_true",
        default=True,
        help="Use light anti-detection context tweaks (default: on)",
    )
    stealth_group.add_argument(
        "--no-stealth",
        dest="stealth",
        action="store_false",
        help="Disable stealth init script and extra launch args",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    output_root = Path(args.output)
    day_dir = output_root / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    screenshots_dir = day_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    sources = load_sources(Path(args.sources))
    screenshot_limit = None if args.max_screenshots <= 0 else args.max_screenshots

    data = fetch_news(
        sources=sources,
        output_dir=day_dir,
        max_items_per_source=args.max_items,
        screenshot_limit=screenshot_limit,
        timeout_ms=args.timeout_ms,
        post_load_wait_ms=args.post_load_wait_ms,
        use_stealth=args.stealth,
    )

    output_file = day_dir / "news.json"
    with output_file.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)

    markdown_file = day_dir / "news.md"
    write_markdown(data, markdown_file)

    LOGGER.info("Saved data to %s", output_file)
    LOGGER.info("Saved markdown to %s", markdown_file)


if __name__ == "__main__":
    main()
