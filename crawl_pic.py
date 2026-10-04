#!/usr/bin/env python3
"""crawl-pic

Download pictures from a web page or from image-search results.

Pick a source with one of the two flags, then let it run:

  --url URL            scrape every image on that page
  --keywords TEXT      pull matching images from an image search engine

The program keeps going until it runs out of fresh candidates or you tell it to
stop: type  q  and press Enter at any point. Images it has already downloaded
(before, or during this run) are never fetched again -- not even as a resized
copy, which perceptual hashing catches.

It crawls politely: robots.txt is obeyed, requests to a host are spaced out, and
a host that pushes back gets a longer gap. It identifies itself honestly rather
than pretending to be a browser (override with the CRAWL_PIC_UA env var).

Examples:
    python crawl_pic.py --url https://example.com/gallery
    python crawl_pic.py --keywords "red pandas"
    python crawl_pic.py --keywords "sunset" -n 5
    python crawl_pic.py --keywords "cats" > log.txt   # quiet run, stops when done
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import io
import json
import os
import random
import re
import select
import sys
import time
import urllib.robotparser
from typing import Callable, Iterable, NamedTuple, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

DEFAULT_OUTPUT = "Downloads"
DEFAULT_TIMEOUT = 15.0
DEFAULT_DELAY = 0.2
MIN_IMAGE_BYTES = 1024

# Safety caps so a broken page or an ever-growing result feed can't loop forever.
MAX_SEARCH_CANDIDATES = 500
# Openverse serves anonymous callers 12 pages of 20; page 13 answers 401.
MAX_OPENVERSE_PAGES = 12
MAX_BING_PAGES = 15

# Refuse to pull absurdly large files into memory, whatever the server claims.
MAX_IMAGE_BYTES = 50 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024

# Retry transient failures instead of dropping a good URL on the floor.
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 0.8
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Per-host politeness that adapts: a fixed delay still gets you throttled by
# image CDNs, so refusals widen the gap for that host and successes narrow it.
PACER_MAX_DELAY = 8.0
PACER_PENALISE_ON = frozenset({403, 429, 503})

# Openverse's documented anonymous budget is 20 requests/minute, 200/day.
OPENVERSE_ENDPOINT = "https://api.openverse.org/v1/images/"
OPENVERSE_PAGE_SIZE = 20          # larger anonymous page_size is rejected (401)
OPENVERSE_MIN_INTERVAL = 3.1      # stay under 20 requests per minute

# Two images are the same picture if their dHash bits differ by no more than this
# out of 64. Catches resized / re-encoded copies that SHA-256 never will.
PERCEPTUAL_MAX_DISTANCE = 10

ROBOTS_AGENT = "crawl-pic"
SEEN_FILENAME = ".crawl_pic_seen.json"

# Identify the tool honestly instead of impersonating a browser: crawlers that
# lie about who they are are the reason sites block them in the first place.
# Set CRAWL_PIC_UA to override it (e.g. to add a real contact address, which is
# what robots.txt asks of a well-behaved crawler).
USER_AGENT = os.environ.get("CRAWL_PIC_UA", "crawl-pic/2.0 (+https://github.com/crawl-pic)")

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
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/jxl": ".jxl",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".tiff", ".tif", ".svg", ".avif", ".heic", ".heif", ".jxl", ".ico",
}

# Pillow's own format names -> the extension we save under. Trusting the
# server's Content-Type (or the URL) is how you end up with a .jpg that is
# really a PNG - and how HTML error pages get saved as images.
EXT_BY_IMAGE_FORMAT = {
    "JPEG": ".jpg", "JPEG2000": ".jp2", "PNG": ".png", "GIF": ".gif",
    "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tiff", "ICO": ".ico",
    "AVIF": ".avif", "HEIF": ".heif", "HEIC": ".heic", "JPEG_XL": ".jxl",
    "PPM": ".ppm", "TIFF_LZMA": ".tiff",
}

# Set once stdin reports EOF so we stop polling it.
_STDIN_EXHAUSTED = False


# --------------------------------------------------------------------------
# Download memory: never fetch an image we already have
# --------------------------------------------------------------------------

def sha256_of_file(path: str, algorithm: str = "sha256") -> Optional[str]:
    """Hash a file. `algorithm='perceptual'` returns its dHash instead."""
    if algorithm == "perceptual":
        try:
            with open(path, "rb") as handle:
                return perceptual_hash(handle.read())
        except OSError:
            return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class SeenStore:
    """Remembers what we already fetched, so nothing is asked for twice.

    URLs let us skip a candidate without touching the network; hashes catch
    images that reappear under a different URL or filename; perceptual hashes
    catch the same picture at a different size. URLs we judged to be junk (a
    tracking pixel, an HTML error page) go in `rejected` for the same reason:
    re-downloading them on every run wastes a request and can never produce a
    different verdict.
    """

    def __init__(self, output_dir: str) -> None:
        self.path = os.path.join(output_dir, SEEN_FILENAME)
        self.urls: set[str] = set()
        self.hashes: set[str] = set()
        self.perceptual: set[str] = set()
        self.rejected: set[str] = set()
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
            self.perceptual = {str(p) for p in data.get("perceptual", [])}
            self.rejected = {str(r) for r in data.get("rejected", [])}

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
            phash = sha256_of_file(path, algorithm="perceptual")
            if phash:
                self.perceptual.add(phash)
            count += 1
        return count

    def has_url(self, url: str) -> bool:
        return url in self.urls

    def is_rejected(self, url: str) -> bool:
        return url in self.rejected

    def has_hash(self, digest: str) -> bool:
        return digest in self.hashes

    def near_duplicate(self, phash: str) -> Optional[str]:
        """Return the stored hash of a visually matching image, if any."""
        if not phash:
            return None
        for known in self.perceptual:
            if hash_distance(phash, known) <= PERCEPTUAL_MAX_DISTANCE:
                return known
        return None

    def remember(self, url: str, digest: Optional[str] = None,
                 phash: Optional[str] = None) -> None:
        self.urls.add(url)
        self.rejected.discard(url)
        if digest:
            self.hashes.add(digest)
        if phash:
            self.perceptual.add(phash)

    def reject(self, url: str) -> None:
        """Note a URL whose content can never be saved, so we stop asking."""
        self.rejected.add(url)
        self.urls.discard(url)

    def save(self) -> None:
        payload = {
            "urls": sorted(self.urls),
            "hashes": sorted(self.hashes),
            "perceptual": sorted(self.perceptual),
            "rejected": sorted(self.rejected),
        }
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


def make_stop_detector() -> Callable[[], bool]:
    """Return a 'stop' check that latches once the user has asked to quit.

    Reading 'q' consumes that line, so the next poll would see end-of-input and
    report "nothing typed" - which would quietly un-cancel a run stopped during
    the search. Latching keeps one 'q' meaning stop for the rest of the run.
    """
    state = {"stop": False}

    def requested() -> bool:
        if not state["stop"]:
            state["stop"] = quit_requested()
        return state["stop"]

    return requested


# --------------------------------------------------------------------------
# Politeness: who we are, how often we knock, and what robots.txt allows
# --------------------------------------------------------------------------

class Candidate(NamedTuple):
    """An image URL, plus credit when the search engine gave us one."""

    url: str
    credit: Optional[str] = None


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def retry_after_seconds(response: requests.Response) -> Optional[float]:
    """Honour Retry-After, which is seconds or an HTTP-date (RFC 9110)."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def request_with_retry(session: requests.Session, method: str, url: str,
                       *, timeout: float, attempts: int = RETRY_ATTEMPTS,
                       stream: bool = False, headers: Optional[dict] = None,
                       **kwargs) -> requests.Response:
    """GET/POST with exponential backoff + jitter on transient failures.

    429/5xx/timeouts are worth another try; a 404 or a 403 is not, so those come
    straight back and the caller decides what to do with them.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, timeout=timeout,
                                       stream=stream, headers=headers, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                raise
        else:
            if response.status_code not in RETRY_STATUSES or attempt == attempts:
                return response
            wait = retry_after_seconds(response)
            response.close()
            if wait is None:
                # Full jitter: spreads retries out instead of synchronising
                # every worker into the same thundering herd.
                wait = random.uniform(0, RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            print(f"  retry {attempt}/{attempts - 1} after HTTP "
                  f"{response.status_code} ({wait:.1f}s): {url}")
            time.sleep(wait)
            continue
        # Exception path: exponential backoff before the next try.
        wait = random.uniform(0, RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
        print(f"  retry {attempt}/{attempts - 1} after "
              f"{last_error.__class__.__name__} ({wait:.1f}s): {url}")
        time.sleep(wait)
    raise last_error  # pragma: no cover - loop always returns or raises


class HostPacer:
    """Keeps a polite gap between requests to the same host, and learns.

    A flat delay is a compromise: fast enough for a local site, too fast for a
    CDN that starts answering 403 when it sees a burst. Doubling the gap after a
    refusal and easing it back after a success keeps us welcome without making
    every download crawl.
    """

    def __init__(self, base: float, ceiling: float = PACER_MAX_DELAY) -> None:
        self.base = base
        self.ceiling = ceiling
        self._delays: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def wait_for(self, url: str) -> None:
        host = urlparse(url).netloc
        delay = self._delays.get(host, self.base)
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last.get(host, 0.0)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last[host] = time.monotonic()

    def penalise(self, url: str) -> None:
        host = urlparse(url).netloc
        current = self._delays.get(host, self.base)
        if current < self.ceiling:
            self._delays[host] = min(self.ceiling, max(current * 2, self.base * 2, 1.0))

    def reward(self, url: str) -> None:
        host = urlparse(url).netloc
        current = self._delays.get(host)
        if current is not None and current > self.base:
            self._delays[host] = max(self.base, current / 2)


class RobotsGate:
    """Checks candidates against each host's robots.txt, caching per host.

    Unreachable robots.txt means "allowed", which is what the standard asks for.
    A declared crawl-delay is respected unless the caller sets --delay.
    """

    def __init__(self, session: requests.Session, timeout: float,
                 enabled: bool = True) -> None:
        self.session = session
        self.timeout = timeout
        self.enabled = enabled
        self._parsers: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def _parser_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                response = self.session.get(parser.url, timeout=self.timeout)
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    parser = None       # no rules published -> nothing to obey
            except requests.RequestException:
                parser = None
            self._parsers[origin] = parser
        return self._parsers[origin]

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parser = self._parser_for(url)
        if parser is None:
            return True
        return parser.can_fetch(ROBOTS_AGENT, url)

    def crawl_delay(self, url: str) -> Optional[float]:
        if not self.enabled:
            return None
        parser = self._parser_for(url)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(ROBOTS_AGENT)
        except (AttributeError, TypeError):   # some parsers return str or None
            return None
        return float(delay) if isinstance(delay, (int, float)) else None


# --------------------------------------------------------------------------
# Candidate discovery
# --------------------------------------------------------------------------


def extract_image_urls(page_url: str, session: requests.Session, timeout: float) -> list[Candidate]:
    """Fetch a page and return the absolute URLs of images found on it."""
    response = request_with_retry(
        session, "GET", page_url, timeout=timeout,
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
    unique: list[Candidate] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            unique.append(Candidate(url))
    return unique


def openverse_image_urls(query: str, session: requests.Session, timeout: float,
                         limit: int = MAX_SEARCH_CANDIDATES,
                         should_stop: Optional[Callable[[], bool]] = None) -> list[Candidate]:
    """Collect image URLs from the Openverse API (no API key needed).

    Openverse indexes openly licensed and public-domain works and publishes a
    documented JSON API for exactly this purpose, so results can be reused
    legally and the service is built for anonymous callers. Requests are paced
    to stay inside the published 20-per-minute budget.
    """
    urls: list[Candidate] = []
    seen: set[str] = set()
    page = 1

    while len(urls) < limit and page <= MAX_OPENVERSE_PAGES:
        if should_stop and should_stop():
            break
        if page > 1:
            time.sleep(OPENVERSE_MIN_INTERVAL)
        try:
            response = request_with_retry(
                session, "GET", OPENVERSE_ENDPOINT, timeout=timeout,
                params={"q": query, "page_size": OPENVERSE_PAGE_SIZE, "page": page},
            )
        except requests.RequestException as exc:
            print(f"  openverse request failed: {exc}")
            break
        if response.status_code == 401:
            # Anonymous callers stop at 12 pages; this is the limit, not an error.
            print(f"  openverse: anonymous access limit reached at page {page}")
            break
        if response.status_code != 200:
            print(f"  openverse returned HTTP {response.status_code}")
            break
        try:
            results = response.json().get("results") or []
        except ValueError:
            break
        if not results:
            break
        for item in results:
            url = item.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            license_name = item.get("license")
            creator = item.get("creator")
            credit = " ".join(str(p) for p in (license_name, creator) if p) or None
            urls.append(Candidate(url, credit))
        page += 1
    return urls


def bing_image_urls(query: str, session: requests.Session, timeout: float,
                    limit: int = MAX_SEARCH_CANDIDATES,
                    should_stop: Optional[Callable[[], bool]] = None) -> list[Candidate]:
    """Collect image URLs from Bing image search (no API key)."""
    urls: list[Candidate] = []
    seen: set[str] = set()
    first = 1
    pages = 0

    while len(urls) < limit and pages < MAX_BING_PAGES:
        if should_stop and should_stop():
            break
        if pages:
            time.sleep(0.5)   # Bing does not advertise a budget; be gentle
        try:
            response = request_with_retry(
                session, "GET", "https://www.bing.com/images/search", timeout=timeout,
                params={"q": query, "form": "HDRSC2", "first": first, "count": 35},
                headers={"Referer": "https://www.bing.com/images/search"},
            )
        except requests.RequestException as exc:
            print(f"  bing request failed: {exc}")
            break
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
                urls.append(Candidate(url))
                added += 1
        if added == 0:
            break
        first += len(raw)
        pages += 1
    return urls


SEARCH_ENGINES = {
    "openverse": openverse_image_urls,
    "bing": bing_image_urls,
}


def search_images(query: str, engine: str, session: requests.Session,
                  timeout: float,
                  should_stop: Optional[Callable[[], bool]] = None
                  ) -> tuple[list[Candidate], Optional[str], bool]:
    """Search for candidate images. Returns (candidates, engine, stopped early)."""
    order = {"auto": ["openverse", "bing"], "openverse": ["openverse"],
             "bing": ["bing"]}[engine]
    for name in order:
        if should_stop and should_stop():
            return [], None, True
        print(f"Searching {name} for: {query!r}")
        try:
            found = SEARCH_ENGINES[name](query, session, timeout, should_stop=should_stop)
        except requests.RequestException as exc:
            print(f"  {name} request failed: {exc}")
            found = []
        if found:
            print(f"  found {len(found)} candidate image(s) via {name}")
            if should_stop and should_stop():
                return found, name, True
            return found, name, False
        print(f"  {name} returned no usable results")
    return [], None, False


# --------------------------------------------------------------------------
# Downloading
# --------------------------------------------------------------------------

def detect_image_extension(data: bytes, content_type: str, url: str) -> Optional[str]:
    """Work out the real format from the bytes, not from what the server claims.

    Returns the extension to save under, or None when the payload is not an
    image we can actually open.
    """
    # SVG is text; Pillow cannot open it, so detect it by shape instead.
    head = data[:512].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048]):
        return ".svg"

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()          # rejects truncated / corrupt files
            image_format = image.format
    except Exception:
        image_format = None

    if image_format and image_format in EXT_BY_IMAGE_FORMAT:
        return EXT_BY_IMAGE_FORMAT[image_format]

    # Not something Pillow recognises. Fall back to the declared type so a
    # format we cannot decode (or a server that lies usefully) still saves.
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in EXT_BY_CONTENT_TYPE:
        return EXT_BY_CONTENT_TYPE[mime]
    path_ext = os.path.splitext(urlparse(url).path)[1].lower()
    if path_ext in IMAGE_EXTENSIONS:
        return path_ext
    return None


def perceptual_hash(data: bytes) -> Optional[str]:
    """64-bit difference hash of an image, as hex.

    SHA-256 only matches byte-identical files, so the same photo downloaded at
    two sizes counts as two different images. dHash survives resizing and
    re-encoding, which is what search results actually hand you.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format == "GIF" and getattr(image, "n_frames", 1) > 1:
                image.seek(0)
            small = image.convert("L").resize((9, 8))
            pixels = small.tobytes()
    except Exception:
        return None
    bits = 0
    index = 0
    for _row in range(8):
        for _col in range(8):
            if pixels[index] > pixels[index + 1]:
                bits |= 1 << index
            index += 1
    return f"{bits:016x}"


def hash_distance(first: str, second: str) -> int:
    try:
        return bin(int(first, 16) ^ int(second, 16)).count("1")
    except ValueError:
        return PERCEPTUAL_MAX_DISTANCE + 1


def read_capped(response: requests.Response, cap: int) -> Optional[bytes]:
    """Read at most `cap` bytes off the wire. None if the body is larger.

    Streams in chunks so an oversized (or dishonest) Content-Length can't
    exhaust memory; everything else is left to the caller's checks.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(READ_CHUNK_BYTES):
        if not chunk:
            continue
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


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


def download_images(urls: Iterable[Candidate], output_dir: str,
                    session: requests.Session,
                    timeout: float, seen: SeenStore, count: Optional[int] = None,
                    min_bytes: int = MIN_IMAGE_BYTES,
                    referer: Optional[str] = None,
                    should_stop: Optional[Callable[[], bool]] = None,
                    robots: Optional[RobotsGate] = None,
                    delay: float = DEFAULT_DELAY) -> tuple[list[str], int, bool]:
    """Download fresh images until candidates run out or the user quits.

    Returns (saved paths, duplicates skipped, quit_early).
    """
    os.makedirs(output_dir, exist_ok=True)
    saved: list[str] = []
    duplicates = 0
    index = next_image_index(output_dir)
    interactive = sys.stdin is not None and sys.stdin.isatty()
    pacer = HostPacer(delay)

    # --count only paces an interactive run: how often to check in with the user.
    # It never limits how many images are downloaded, in any mode.
    batch = count if (count and interactive) else None

    for candidate in urls:
        url = candidate.url if isinstance(candidate, Candidate) else str(candidate)
        credit = candidate.credit if isinstance(candidate, Candidate) else None

        # Check first, so 'q' is honoured even when everything is a duplicate.
        if (should_stop or quit_requested)():
            print("\nStopped on request.")
            return saved, duplicates, True

        # Already fetched on an earlier run: skip without touching the network.
        if seen.has_url(url):
            duplicates += 1
            print(f"  skip (already downloaded): {url}")
            continue

        # Already judged junk on an earlier run; the verdict can't change.
        if seen.is_rejected(url):
            print(f"  skip (previously rejected): {url}")
            continue

        if robots is not None and not robots.allowed(url):
            print(f"  skip (disallowed by robots.txt): {url}")
            continue

        # Pacing: never hammer a host. A robots.txt crawl-delay wins if larger.
        pacer.base = delay
        if robots is not None:
            declared = robots.crawl_delay(url)
            if declared is not None and declared > pacer.base:
                pacer.base = declared
        pacer.wait_for(url)

        headers = {"Referer": referer} if referer else {}
        try:
            with request_with_retry(session, "GET", url, timeout=timeout,
                                    stream=True, headers=headers) as response:
                if response.status_code != 200:
                    if response.status_code in PACER_PENALISE_ON:
                        pacer.penalise(url)
                    # Not remembered: a 403/429 today may well work tomorrow.
                    print(f"  skip (HTTP {response.status_code}): {url}")
                    continue
                pacer.reward(url)
                content_type = response.headers.get("Content-Type", "")
                if not content_type.lower().startswith("image/"):
                    print(f"  skip (not an image: {content_type or 'unknown type'}): {url}")
                    seen.reject(url)
                    continue
                data = read_capped(response, MAX_IMAGE_BYTES)
        except requests.RequestException as exc:
            # Transient by nature, so this one is deliberately not remembered.
            print(f"  skip (request error: {exc.__class__.__name__}): {url}")
            continue

        if data is None:
            print(f"  skip (larger than {MAX_IMAGE_BYTES} bytes): {url}")
            seen.reject(url)
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
            seen.reject(url)
            continue

        # Real format from the bytes; Content-Type and URL only as a fallback.
        extension = detect_image_extension(data, content_type, url)
        if not extension:
            print(f"  skip (not a readable image): {url}")
            seen.reject(url)
            continue

        phash = perceptual_hash(data)
        twin = seen.near_duplicate(phash) if phash else None
        if twin:
            seen.remember(url, digest, phash)
            duplicates += 1
            print(f"  skip (same picture as an earlier download, different size): {url}")
            continue

        filename = f"image_{index:03d}{extension}"
        path = os.path.join(output_dir, filename)
        with open(path, "wb") as handle:
            handle.write(data)
        seen.remember(url, digest, phash)
        seen.save()
        index += 1
        saved.append(path)

        credit_note = f"  [{credit}]" if credit else ""
        print(f"  [{len(saved)}] saved {filename}  <- {url}{credit_note}")

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


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if number < 0:
        raise argparse.ArgumentTypeError("size cannot be negative")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not number > 0:
        raise argparse.ArgumentTypeError("timeout must be greater than 0")
    return number


def non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if number < 0:
        raise argparse.ArgumentTypeError("delay cannot be negative")
    return number


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="crawl_pic.py",
        description=(
            "Download pictures from a web page or from image-search results. "
            "Runs until it runs out of new images or you press 'q' + Enter. "
            "Obeys robots.txt and paces its requests."
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
    parser.add_argument("--engine", choices=["auto", "openverse", "bing"], default="auto",
                        help="image search engine for keyword mode: auto (Openverse "
                             "then Bing), openverse (openly-licensed works, no key) "
                             "or bing (default: auto)")
    parser.add_argument("--timeout", type=positive_float, default=DEFAULT_TIMEOUT,
                        help=f"per-request timeout in seconds (default: {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--min-bytes", type=non_negative_int, default=MIN_IMAGE_BYTES,
                        help=f"skip files smaller than this (default: {MIN_IMAGE_BYTES})")
    parser.add_argument("--delay", type=non_negative_float, default=DEFAULT_DELAY,
                        help="seconds to wait between requests to the same host "
                             f"(default: {DEFAULT_DELAY:g}). 0 removes the base "
                             "delay, but a host that answers 403/429 is still "
                             "backed off. A robots.txt crawl-delay, when larger, "
                             "takes precedence.")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="do not check robots.txt before downloading (not recommended)")
    args = parser.parse_args(argv)

    session = build_session()

    if os.path.exists(args.output) and not os.path.isdir(args.output):
        print(f"error: output path {args.output!r} exists and is not a folder.",
              file=sys.stderr)
        return 1
    try:
        os.makedirs(args.output, exist_ok=True)
    except OSError as exc:
        print(f"error: could not create output folder {args.output!r}: {exc}",
              file=sys.stderr)
        return 1

    print(f"Checking {args.output!r} for images you already have ...")
    seen = SeenStore(args.output)
    known_files = seen.index_existing_files(args.output)
    print(f"  {known_files} existing file(s) indexed, "
          f"{len(seen.urls)} source URL(s) remembered")

    referer: Optional[str] = None
    stopped_early = False
    robots = RobotsGate(session, args.timeout, enabled=not args.ignore_robots)
    stop_requested = make_stop_detector()
    # Searching can take a while, so tell the user how to stop *before* it starts.
    announce_stop_habit(sys.stdin is not None and sys.stdin.isatty())

    if args.url:
        print(f"Fetching page: {args.url}")
        try:
            candidates = extract_image_urls(args.url, session, args.timeout)
        except requests.RequestException as exc:
            print(f"error: could not fetch page: {exc}", file=sys.stderr)
            seen.save()
            return 1
        print(f"Found {len(candidates)} candidate image(s) on the page")
        referer = args.url
    else:
        candidates, _engine, stopped_early = search_images(
            args.keywords, args.engine, session, args.timeout,
            should_stop=stop_requested,
        )

    # A stop during the search ends the run: downloading the candidates we
    # happened to collect first would be the opposite of what was asked for.
    if stopped_early or stop_requested():
        print("\nStopped on request.")
        saved, duplicates, quit_early = [], 0, True
    else:
        if not candidates:
            seen.save()
            print("error: no image URLs found.", file=sys.stderr)
            return 1

        saved, duplicates, quit_early = download_images(
            candidates, args.output, session, timeout=args.timeout, seen=seen,
            count=args.count, min_bytes=args.min_bytes, referer=referer,
            should_stop=stop_requested, robots=robots, delay=args.delay,
        )
    seen.save()

    print()
    print(f"New images saved : {len(saved)}")
    print(f"Duplicates skipped: {duplicates}")
    print(f"Location         : {os.path.abspath(args.output)}")
    if quit_early:
        print("(stopped early on request)")
    if not saved and duplicates and not quit_early:
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
