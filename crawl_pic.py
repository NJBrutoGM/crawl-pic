#!/usr/bin/env python3
"""crawl-pic

Download a batch of pictures from a web page or from image-search results.

Two modes, chosen automatically from the target you pass:

  * URL mode  - give a page URL (http:// or https://) and the program scrapes
                every image it can find on that page.
  * Keyword mode - give search keywords and the program asks an image search
                engine for matching pictures.

Examples:
    python crawl_pic.py https://example.com/gallery -n 20
    python crawl_pic.py "red pandas" -n 15
    python crawl_pic.py "sunset over the ocean" -n 8 -o Downloads
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_COUNT = 10
DEFAULT_OUTPUT = "Downloads"
DEFAULT_TIMEOUT = 15.0
MIN_IMAGE_BYTES = 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Content-Type -> file extension.
EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".tiff", ".tif", ".svg", ".avif", ".ico",
}


def build_session() -> requests.Session:
    """Return a requests session with browser-like default headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def looks_like_url(text: str) -> bool:
    return bool(re.match(r"^https?://", text.strip(), re.IGNORECASE))


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if number < 1:
        raise argparse.ArgumentTypeError("count must be at least 1")
    return number


# --------------------------------------------------------------------------
# URL mode: scrape images off a page
# --------------------------------------------------------------------------

def extract_image_urls(page_url: str, session: requests.Session, timeout: float) -> list[str]:
    """Fetch a page and return the absolute URLs of images found on it."""
    response = session.get(
        page_url,
        timeout=timeout,
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    found: list[str] = []

    def add(raw: Optional[str]) -> None:
        if not raw:
            return
        raw = raw.strip()
        if not raw or raw.startswith("data:"):
            return
        found.append(urljoin(page_url, raw))

    # Social / preview images.
    for prop in ("og:image", "og:image:url", "twitter:image"):
        for tag in soup.find_all("meta", attrs={"property": prop}) + \
                   soup.find_all("meta", attrs={"name": prop}):
            add(tag.get("content"))

    # <img> tags, including common lazy-load attributes.
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr))
        for srcset in (img.get("srcset"), img.get("data-srcset")):
            if srcset:
                for candidate in srcset.split(","):
                    add(candidate.strip().split(" ")[0])

    # <source> tags inside <picture>.
    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for candidate in srcset.split(","):
                add(candidate.strip().split(" ")[0])

    # De-duplicate while keeping order.
    seen: set[str] = set()
    unique: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


# --------------------------------------------------------------------------
# Keyword mode: image search engines
# --------------------------------------------------------------------------

def ddg_image_urls(query: str, count: int, session: requests.Session, timeout: float) -> list[str]:
    """Collect image URLs from DuckDuckGo image search (no API key)."""
    page = session.get(
        "https://duckduckgo.com/",
        params={"q": query, "iax": "images", "ia": "images"},
        timeout=timeout,
    )
    match = re.search(r'vqd=["\']?([\d-]+)', page.text)
    if not match:
        return []
    vqd = match.group(1)

    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://duckduckgo.com/",
        "X-Requested-With": "XMLHttpRequest",
    }

    urls: list[str] = []
    seen: set[str] = set()
    next_token: Optional[str] = None

    for _ in range(10):  # cap pagination
        if len(urls) >= count:
            break
        params = {"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"}
        if next_token:
            params["next"] = next_token
        response = session.get("https://duckduckgo.com/i.js", params=params,
                               headers=headers, timeout=timeout)
        if response.status_code != 200:
            break
        try:
            data = response.json()
        except ValueError:
            break
        results = data.get("results") or []
        if not results:
            break
        for item in results:
            url = item.get("image")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        next_token = data.get("next")
        if not next_token:
            break
    return urls


def bing_image_urls(query: str, count: int, session: requests.Session, timeout: float) -> list[str]:
    """Collect image URLs from Bing image search (no API key)."""
    urls: list[str] = []
    seen: set[str] = set()
    first = 1

    while len(urls) < count and first < 200:
        response = session.get(
            "https://www.bing.com/images/search",
            params={"q": query, "form": "HDRSC2", "first": first, "count": 35},
            headers={"Referer": "https://www.bing.com/images/search"},
            timeout=timeout,
        )
        if response.status_code != 200:
            break
        raw = re.findall(r"murl&quot;:&quot;(.*?)&quot;", response.text)
        if not raw:
            raw = re.findall(r'"murl":"(.*?)"', response.text)
        if not raw:
            break
        added = 0
        for value in raw:
            url = html.unescape(value)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
                added += 1
        if added == 0:
            break
        first += len(raw)
    return urls


SEARCH_ENGINES = {
    "ddg": ddg_image_urls,
    "bing": bing_image_urls,
}


def search_images(query: str, count: int, engine: str,
                  session: requests.Session, timeout: float) -> tuple[list[str], Optional[str]]:
    """Try the requested engine (and, in auto mode, fall back) until one returns results."""
    order = {"auto": ["ddg", "bing"], "ddg": ["ddg"], "bing": ["bing"]}[engine]
    for name in order:
        print(f"Searching {name} for: {query!r}")
        try:
            urls = SEARCH_ENGINES[name](query, count, session, timeout)
        except requests.RequestException as exc:
            print(f"  {name} request failed: {exc}")
            urls = []
        if urls:
            print(f"  found {len(urls)} candidate image(s) via {name}")
            return urls, name
        print(f"  {name} returned no usable results")
    return [], None


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

def guess_extension(content_type: str, url: str) -> Optional[str]:
    """Pick a file extension from the Content-Type, falling back to the URL path."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in EXT_BY_CONTENT_TYPE:
        return EXT_BY_CONTENT_TYPE[mime]
    path_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if path_ext in IMAGE_EXTENSIONS:
        return path_ext
    if mime.startswith("image/"):
        return "." + mime.split("/", 1)[1]
    return None


def download_images(urls: Iterable[str], count: int, output_dir: str,
                    session: requests.Session, timeout: float,
                    min_bytes: int = MIN_IMAGE_BYTES,
                    referer: Optional[str] = None) -> list[str]:
    """Download up to `count` images into `output_dir`; return the saved paths."""
    os.makedirs(output_dir, exist_ok=True)
    saved: list[str] = []
    index = 0

    for url in urls:
        if len(saved) >= count:
            break
        headers = {"Referer": referer} if referer else {}
        try:
            with session.get(url, timeout=timeout, stream=True, headers=headers) as response:
                if response.status_code != 200:
                    print(f"  skip (HTTP {response.status_code}): {url}")
                    continue
                content_type = response.headers.get("Content-Type", "")
                if not content_type.lower().startswith("image/"):
                    print(f"  skip (not an image: {content_type or 'unknown type'}): {url}")
                    continue
                data = response.content
        except requests.RequestException as exc:
            print(f"  skip (request error: {exc.__class__.__name__}): {url}")
            continue

        if len(data) < min_bytes:
            print(f"  skip (too small, {len(data)} bytes): {url}")
            continue

        extension = guess_extension(content_type, url)
        if not extension:
            print(f"  skip (unknown image type): {url}")
            continue

        index += 1
        filename = f"image_{index:03d}{extension}"
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as handle:
            handle.write(data)
        saved.append(path)
        print(f"  [{len(saved)}/{count}] saved {filename}  <- {url}")

    return saved


# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crawl_pic.py",
        description="Download pictures from a web page URL or from search keywords.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python crawl_pic.py https://example.com/gallery -n 20\n"
            "  python crawl_pic.py \"red pandas\" -n 15\n"
        ),
    )
    parser.add_argument("target", help="A page URL (http/https) OR search keywords")
    parser.add_argument("-n", "--count", type=positive_int, default=DEFAULT_COUNT,
                        help=f"number of images to download (default: {DEFAULT_COUNT})")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                        help=f"output folder (default: {DEFAULT_OUTPUT!r})")
    parser.add_argument("--engine", choices=["auto", "ddg", "bing"], default="auto",
                        help="image search engine for keyword mode (default: auto)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--min-bytes", type=int, default=MIN_IMAGE_BYTES,
                        help=f"skip files smaller than this (default: {MIN_IMAGE_BYTES})")
    args = parser.parse_args(argv)

    session = build_session()
    referer: Optional[str] = None

    if looks_like_url(args.target):
        print(f"Fetching page: {args.target}")
        try:
            urls = extract_image_urls(args.target, session, args.timeout)
        except requests.RequestException as exc:
            print(f"error: could not fetch page: {exc}", file=sys.stderr)
            return 1
        print(f"Found {len(urls)} candidate image(s) on the page")
        referer = args.target
    else:
        urls, _ = search_images(args.target, args.count, args.engine, session, args.timeout)

    if not urls:
        print("error: no image URLs found.", file=sys.stderr)
        return 1

    print(f"Downloading up to {args.count} image(s) into {args.output!r} ...")
    saved = download_images(
        urls, args.count, args.output, session,
        timeout=args.timeout, min_bytes=args.min_bytes, referer=referer,
    )

    if not saved:
        print("error: no images could be downloaded.", file=sys.stderr)
        return 1

    print(f"Done. Saved {len(saved)} image(s) to {os.path.abspath(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
