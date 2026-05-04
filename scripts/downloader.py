#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests


class DownloadError(RuntimeError):
    pass


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("file_url")
    parser.add_argument("--outdir", default="downloads")
    parser.add_argument("--min-bytes", type=int, default=1024)
    return parser.parse_args()


def direct_download(url: str, outdir: Path) -> Path:
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()

    name = Path(urlparse(url).path).name or "file"
    out = outdir / name

    with out.open("wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                f.write(chunk)

    return out


def download(url: str, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    print("Direct download")
    try:
        return direct_download(url, outdir)
    except Exception as e:
        raise DownloadError(f"Direct download failed: {e}") from e


def validate(path: Path, min_bytes: int):
    size = path.stat().st_size
    print(f"File size: {size}")
    if size < min_bytes:
        raise DownloadError("File too small")


def write_output(path: Path):
    gh = os.getenv("GITHUB_OUTPUT")
    if not gh:
        return
    with open(gh, "a") as f:
        f.write(f"file_path={path}\n")
        f.write(f"file_name={path.name}\n")


def main():
    args = parse_args()

    outdir = Path(args.outdir).resolve()
    file = download(args.file_url, outdir)

    validate(file, args.min_bytes)
    write_output(file)

    print(f"\n🎉 Done: {file}")


if __name__ == "__main__":
    try:
        main()
    except DownloadError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
