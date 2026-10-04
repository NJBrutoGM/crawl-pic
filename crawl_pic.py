#!/usr/bin/env python3
"""crawl-pic

Download pictures from a web page or from image-search results.

Pick a source with one of the two flags, then let it run:

  --url URL            scrape every image on that page
  --keywords TEXT      pull matching images from an image search engine

The program keeps going until it runs out of fresh candidates or you tell it to
stop: type  q  and press Enter at any point. Images it has already downloaded
(before, or during this run) are never fetched again.

Examples:
    python crawl_pic.py --url https://example.com/gallery
    python crawl_pic.py --keywords "red pandas"
    python crawl_pic.py --keywords "sunset" -n 5
    python crawl_pic.py --keywords "cats" > log.txt   # quiet run, stops when done
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import select
import sys
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_OUTPUT = "Downloads"
DEFAULT_TIMEOUT = 15.0
MIN_IMAGE_BYTES = 1024

# Safety caps so a broken page or an ever-growing result feed can't loop forever.
MAX_SEARCH_CANDIDATES = 500
MAX_DDG_PAGES = 20
MAX_BING_PAGES = 15

SEEN_FILENAME = ".crawl_pic_seen.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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

# Set once stdin reports EOF so we stop polling it.
_STDIN_EXHAUSTED = False


# --------------------------------------------------------------------------
# Download memory: never fetch an image we already have
# --------------------------------------------------------------------------

def sha256_of_file(path: str) -> Optional[str]:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class SeenStore:
    """Remembers source URLs and content hashes of images already downloaded.

    URLs let us skip a candidate without touching the network; hashes catch
    images that reappear under a different URL or filename.
    """

    def __init__(self, output_dir: str) -> None:
        self.path = os.path.join(output_dir, SEEN_FILENAME)
        self.urls: set[str] = set()
        self.hashes: set[str] = set()
        self._load_ledger()

    def _load_ledger(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.urls = {str(u) for u in data.get("urls", [])}
            self.hashes = {str(h) for h in data.get("hashes", [])}

    def index_existing_files(self, output_dir: str) -> int:
        """Hash files already sitting in the output folder. Returns how many."""
        count = 0
        try:
            entries = os.listdir(output_dir)
        except OSError:
            return 0
        for name in entries:
            if name.startswith("."):
                continue
            path = os.path.join(output_dir, name)
            if not os.path.isfile(path):
                continue
            digest = sha256_of_file(path)
            if digest:
                self.hashes.add(digest)
                count += 1
        return count

    def has_url(self, url: str) -> bool:
        return url in self.urls

    def has_hash(self, digest: str) -> bool:
        return digest in self.hashes

    def remember(self, url: str, digest: Optional[str] = None) -> None:
        self.urls.add(url)
        if digest:
            self.hashes.add(digest)

    def save(self) -> None:
        payload = {"urls": sorted(self.urls), "hashes": sorted(self.hashes)}
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except OSError as exc:
            print(f"warning: could not save download memory: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# "Press q to stop" support
# --------------------------------------------------------------------------

def _stdin_ready() -> bool:
    global _STDIN_EXHAUSTED
    if _STDIN_EXHAUSTED:
        return False
    stream = sys.stdin
    if stream is None or stream.closed:
        _STDIN_EXHAUSTED = True
        return False
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            _STDIN_EXHAUSTED = True
            return False
        return msvcrt.kbhit()
    try:
        ready, _, _ = select.select([stream], [], [], 0)
    except (ValueError, OSError):
        _STDIN_EXHAUSTED = True
        return False
    return bool(ready)


def quit_requested() -> bool:
    """True if the user typed 'q' (followed by Enter) since the last check."""
    global _STDIN_EXHAUSTED
    if not _stdin_ready():
        return False
    try:
        line = sys.stdin.readline()
    except (EOFError, OSError, ValueError):
        _STDIN_EXHAUSTED = True
        return False
    if line == "":  # EOF - stop polling so we don't spin
        _STDIN_EXHAUSTED = True
        return False
    return line.strip().lower().startswith("q")


def announce_stop_habit(interactive: bool) -> None:
    if interactive:
        print("Press 'q' + Enter at any time to stop.\n")


# --------------------------------------------------------------------------
# Candidate discovery
# --------------------------------------------------------------------------

def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


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

    for prop in ("og:image", "og:image:url", "twitter:image"):
        for tag in soup.find_all("meta", attrs={"property": prop}) + \
                   soup.find_all("meta", attrs={"name": prop}):
            add(tag.get("content"))

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            add(img.get(attr))
        for srcset in (img.get("srcset"), img.get("data-srcset")):
            if srcset:
                for candidate in srcset.split(","):
                    add(candidate.strip().split(" ")[0])

    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            for candidate in srcset.split(","):
                add(candidate.strip().split(" ")[0])

    seen: set[str] = set()
    unique: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def ddg_image_urls(query: str, session: requests.Session, timeout: float,
                   limit: int = MAX_SEARCH_CANDIDATES) -> list[str]:
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

    for _ in range(MAX_DDG_PAGES):
        if len(urls) >= limit:
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


def bing_image_urls(query: str, session: requests.Session, timeout: float,
                    limit: int = MAX_SEARCH_CANDIDATES) -> list[str]:
    """Collect image URLs from Bing image search (no API key)."""
    urls: list[str] = []
    seen: set[str] = set()
    first = 1
    pages = 0

    while len(urls) < limit and pages < MAX_BING_PAGES:
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
        pages += 1
    return urls


SEARCH_ENGINES = {
    "ddg": ddg_image_urls,
    "bing": bing_image_urls,
}


def search_images(query: str, engine: str, session: requests.Session,
                  timeout: float) -> tuple[list[str], Optional[str]]:
    order = {"auto": ["ddg", "bing"], "ddg": ["ddg"], "bing": ["bing"]}[engine]
    for name in order:
        print(f"Searching {name} for: {query!r}")
        try:
            urls = SEARCH_ENGINES[name](query, session, timeout)
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
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in EXT_BY_CONTENT_TYPE:
        return EXT_BY_CONTENT_TYPE[mime]
    path_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if path_ext in IMAGE_EXTENSIONS:
        return path_ext
    if mime.startswith("image/"):
        return "." + mime.split("/", 1)[1]
    return None


def next_image_index(output_dir: str) -> int:
    """Continue numbering after the files that are already in the folder."""
    highest = 0
    try:
        entries = os.listdir(output_dir)
    except OSError:
        return 1
    for name in entries:
        match = re.fullmatch(r"image_(\d+)\.[A-Za-z0-9]+", name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def download_images(urls: Iterable[str], output_dir: str, session: requests.Session,
                    timeout: float, seen: SeenStore, count: Optional[int] = None,
                    min_bytes: int = MIN_IMAGE_BYTES,
                    referer: Optional[str] = None) -> tuple[list[str], int, bool]:
    """Download fresh images until candidates run out or the user quits.

    Returns (saved paths, duplicates skipped, quit_early).
    """
    os.makedirs(output_dir, exist_ok=True)
    saved: list[str] = []
    duplicates = 0
    index = next_image_index(output_dir)
    interactive = sys.stdin is not None and sys.stdin.isatty()

    # --count only paces an interactive run: how often to check in with the user.
    # It never limits how many images are downloaded, in any mode.
    batch = count if (count and interactive) else None

    for url in urls:
        # Already fetched on an earlier run: skip without touching the network.
        if seen.has_url(url):
            duplicates += 1
            print(f"  skip (already downloaded): {url}")
            continue

        if quit_requested():
            print("\nStopped on request.")
            return saved, duplicates, True

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

        digest = hashlib.sha256(data).hexdigest()

        # Same picture under a different URL or filename: remember and move on.
        if seen.has_hash(digest):
            seen.remember(url, digest)
            duplicates += 1
            print(f"  skip (identical image already saved): {url}")
            continue

        if len(data) < min_bytes:
            print(f"  skip (too small, {len(data)} bytes): {url}")
            continue

        extension = guess_extension(content_type, url)
        if not extension:
            print(f"  skip (unknown image type): {url}")
            continue

        filename = f"image_{index:03d}{extension}"
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as handle:
            handle.write(data)
        seen.remember(url, digest)
        seen.save()
        index += 1
        saved.append(path)

        print(f"  [{len(saved)}] saved {filename}  <- {url}")

        if batch and len(saved) % batch == 0:
            print(f"\n--- {len(saved)} image(s) saved so far. ---")
            print("--- Press Enter to keep going, or 'q' + Enter to stop ---")
            try:
                answer = input()
            except (EOFError, KeyboardInterrupt):
                print()
                return saved, duplicates, True
            if answer.strip().lower().startswith("q"):
                return saved, duplicates, True

    return saved, duplicates, False


# --------------------------------------------------------------------------

def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if number < 1:
        raise argparse.ArgumentTypeError("count must be at least 1")
    return number


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crawl_pic.py",
        description=(
            "Download pictures from a web page or from image-search results. "
            "Runs until it runs out of new images or you press 'q' + Enter."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python crawl_pic.py --url https://example.com/gallery\n"
            "  python crawl_pic.py --keywords \"red pandas\"\n"
            "  python crawl_pic.py --keywords \"sunset\" -n 5\n"
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="page URL to scrape images from")
    source.add_argument("--keywords", metavar="KEYWORDS",
                        help="keywords to search for images")
    parser.add_argument("-n", "--count", type=positive_int, default=None,
                        help="check in every N images while running interactively; "
                             "ignored when not attached to a terminal. It never caps "
                             "the download. Default: never check in.")
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
    os.makedirs(args.output, exist_ok=True)

    print(f"Checking {args.output!r} for images you already have ...")
    seen = SeenStore(args.output)
    known_files = seen.index_existing_files(args.output)
    print(f"  {known_files} existing file(s) indexed, "
          f"{len(seen.urls)} source URL(s) remembered")

    referer: Optional[str] = None

    if args.url:
        print(f"Fetching page: {args.url}")
        try:
            urls = extract_image_urls(args.url, session, args.timeout)
        except requests.RequestException as exc:
            print(f"error: could not fetch page: {exc}", file=sys.stderr)
            seen.save()
            return 1
        print(f"Found {len(urls)} candidate image(s) on the page")
        referer = args.url
    else:
        urls, _ = search_images(args.keywords, args.engine, session, args.timeout)

    if not urls:
        seen.save()
        print("error: no image URLs found.", file=sys.stderr)
        return 1

    announce_stop_habit(sys.stdin is not None and sys.stdin.isatty())

    saved, duplicates, quit_early = download_images(
        urls, args.output, session, timeout=args.timeout, seen=seen,
        count=args.count, min_bytes=args.min_bytes, referer=referer,
    )
    seen.save()

    print()
    print(f"New images saved : {len(saved)}")
    print(f"Duplicates skipped: {duplicates}")
    print(f"Location         : {os.path.abspath(args.output)}")
    if quit_early:
        print("(stopped early on request)")
    if not saved and duplicates:
        print("Nothing new: every candidate was already downloaded.")

    return 0 if saved else 1


if __name__ == "__main__":
    try:
        status = main()
    except KeyboardInterrupt:
        # Images already saved stay on disk; the memory file was written too.
        print("\nInterrupted.", file=sys.stderr)
        status = 130
    raise SystemExit(status)
