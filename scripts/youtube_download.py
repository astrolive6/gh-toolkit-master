#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

import yt_dlp


class YouTubeDownloadError(RuntimeError):
    pass


VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi"}


def parse_args():
    p = argparse.ArgumentParser(description="Download a YouTube video and zip it for release.")
    p.add_argument("youtube_url", help="YouTube watch or youtu.be URL")
    p.add_argument("--outdir", default="downloads/youtube", type=Path)
    p.add_argument(
        "--cookies",
        type=Path,
        metavar="FILE",
        help="Netscape-format cookies file (often required in CI; export from a logged-in browser).",
    )
    return p.parse_args()


def cookies_path_from_env_or_args(args) -> Path | None:
    if args.cookies:
        return args.cookies
    env = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    return Path(env) if env else None


def safe_zip_basename(title: str, video_id: str) -> str:
    base = title or "video"
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(". ") or "video"
    if len(base) > 80:
        base = base[:80].rstrip()
    return f"{base}-{video_id}"


def resolve_downloaded_path(outdir: Path, info: dict) -> Path:
    for key in ("filepath", "_filename"):
        p = info.get(key)
        if p:
            path = Path(p)
            if path.is_file():
                return path.resolve()

    for rd in info.get("requested_downloads") or ():
        fp = rd.get("filepath")
        if fp:
            path = Path(fp)
            if path.is_file():
                return path.resolve()

    vid = info.get("id")
    if vid:
        candidates = [p for p in outdir.glob(f"{vid}.*") if p.suffix.lower() in VIDEO_EXTENSIONS]
        if candidates:
            return max(candidates, key=lambda x: x.stat().st_size).resolve()

    raise YouTubeDownloadError("Could not determine downloaded file path.")


def write_github_output(zip_path: Path):
    gh = os.getenv("GITHUB_OUTPUT")
    if not gh:
        return
    with open(gh, "a", encoding="utf-8") as f:
        f.write(f"file_path={zip_path}\n")
        f.write(f"file_name={zip_path.name}\n")


def main():
    args = parse_args()
    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    ydl_opts: dict = {
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "web"],
            }
        },
        "remote_components": ["ejs:github"],
    }

    deno_path = shutil.which("deno")
    if deno_path:
        ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}

    cookie_path = cookies_path_from_env_or_args(args)
    if cookie_path is not None:
        cookie_path = cookie_path.expanduser().resolve()
        if not cookie_path.is_file():
            raise YouTubeDownloadError(f"Cookies file not found: {cookie_path}")
        ydl_opts["cookiefile"] = str(cookie_path)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(args.youtube_url, download=True)

    if not isinstance(info, dict):
        raise YouTubeDownloadError("Unexpected response from yt-dlp.")

    video_path = resolve_downloaded_path(outdir, info)
    size = video_path.stat().st_size
    if size < 1024:
        raise YouTubeDownloadError("Downloaded file is too small; download may have failed.")

    title = info.get("title") or ""
    vid = info.get("id") or "unknown"
    zip_name = safe_zip_basename(str(title), str(vid)) + ".zip"
    zip_path = outdir / zip_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(video_path, arcname=video_path.name)

    try:
        video_path.unlink()
    except OSError as e:
        print(f"Warning: could not remove temp video file {video_path}: {e}", file=sys.stderr)

    write_github_output(zip_path.resolve())
    print(f"\nDone: {zip_path.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except YouTubeDownloadError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        print(f"\nDownload error: {e}", file=sys.stderr)
        if "not a bot" in msg.lower() or "sign in" in msg.lower():
            print(
                "\nYouTube often blocks datacenter IPs. Add a Netscape cookies file:\n"
                "  • Locally: pass --cookies /path/to/cookies.txt\n"
                "  • GitHub Actions: create repo secret YOUTUBE_COOKIES_B64 (base64 of cookies.txt)\n"
                "  Export cookies while logged into YouTube: "
                "https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies",
                file=sys.stderr,
            )
        sys.exit(1)