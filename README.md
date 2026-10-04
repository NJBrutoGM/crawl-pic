# crawl-pic

Download pictures from a web page or from image-search results.

Give it a source, and it keeps pulling images until it runs out of new ones —
or until you press `q` to stop. Anything it has already downloaded is never
fetched again.

No API keys, no accounts — just `requests` and `beautifulsoup4`.

## Features

- **Two sources, one flag each.** `--url` scrapes a page; `--keywords` searches
  for images. Exactly one is required, and they can't be combined.
- **Runs until you stop it.** Type `q` + Enter at any point to quit with
  everything saved so far.
- **Never downloads the same image twice.** A memory file plus content hashing
  means re-running the same command is safe and fast.
- **Continues numbering.** New files pick up after the images already in the
  folder, so nothing is overwritten.
- **Skips junk.** Non-image responses, error pages, and tiny tracking pixels are
  filtered out instead of saved as broken files.
- **Correct extensions.** The extension comes from the response's `Content-Type`,
  not the URL, so you don't get `.jpg` files that are really PNGs.
- **Engine fallback.** DuckDuckGo first, Bing automatically if DDG returns
  nothing.

## Requirements

- Python 3.8+
- `requests`
- `beautifulsoup4`

## Setup

The fastest route — installs the two dependencies and automatically falls back
to a local `.venv` if your Python is externally managed (PEP 668):

```bash
./quick-setup.sh        # install into the current environment
./quick-setup.sh --venv # create and use a local .venv instead
```

If you prefer plain pip:

```bash
pip install -r requirements.txt
```

`quick-setup.sh` honours `PYTHON=/path/to/python3` if you need a specific
interpreter.

## Usage

Pass exactly one source flag. It runs until the images run out or you stop it.

```bash
# Scrape every image off a page
python crawl_pic.py --url https://example.com/gallery

# Search for pictures by keyword
python crawl_pic.py --keywords "red pandas"
```

Press `q` and hit Enter whenever you want to stop:

```
$ python crawl_pic.py --keywords "kittens"
Press 'q' + Enter at any time to stop.

  [1] saved image_001.jpg  <- https://example.com/cat1.jpg
  [2] saved image_002.png  <- https://example.com/cat2.png
q

Stopped on request.

New images saved : 2
Duplicates skipped: 0
Location         : /home/you/Downloads
(stopped early on request)
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--url URL` | – | Page to scrape images from. |
| `--keywords TEXT` | – | Keywords to search for images. |
| `-n`, `--count` | none | Check in every N images while running interactively. Never caps the download. |
| `-o`, `--output` | `Downloads` | Output folder, created if missing. |
| `--engine` | `auto` | Keyword-mode search engine: `auto`, `ddg`, or `bing`. |
| `--timeout` | `15` | Per-request timeout, in seconds. |
| `--min-bytes` | `1024` | Skip any file smaller than this. |
| `-h`, `--help` | – | Show usage and exit. |

`--url` and `--keywords` are mutually exclusive, and one of them is required.

### What `-n/--count` does

`-n` only controls **how often the program checks in with you**. It never limits
how many images are downloaded, in any mode — the run ends when the candidates
run out, when you press `q`, or on `Ctrl-C`.

- **Interactive (a terminal):** after every `n` images it pauses with
  `Press Enter to keep going, or 'q' + Enter to stop`.
- **Non-interactive (piped/redirected):** there is nobody to prompt, so `-n` is
  ignored entirely and the run continues until the images run out.

```bash
# In a terminal: every 5 images it asks whether to continue
python crawl_pic.py --keywords "sunset" -n 5

# Piped: -n has no effect, it downloads everything it can find
python crawl_pic.py --keywords "sunset" -n 5 > log.txt
```

### Stopping it

The program watches stdin for a line starting with `q`, checked between
downloads. That works whether you're sitting at a terminal or piping input in,
so `(sleep 10; echo q) | python crawl_pic.py --keywords cats` also stops early.
`Ctrl-C` works too: it exits immediately with status `130` and prints no summary.
Either way, images already saved stay on disk.

## How it works

### URL mode

`--url` fetches the page and collects image URLs from:

- `<meta>` social/preview tags: `og:image`, `og:image:url`, `twitter:image`
- `<img>` tags — `src`, plus common lazy-load attributes (`data-src`,
  `data-original`, `data-lazy-src`, `data-url`)
- `srcset` / `data-srcset` candidates on `<img>`, and `srcset` on `<source>`

Relative URLs are resolved against the page URL, and duplicates are removed
while preserving order.

### Keyword mode

`--keywords` sends the query to an image search engine:

- **`ddg`** — DuckDuckGo image search. Fetches a `vqd` token from the search
  page, then queries its JSON endpoint, paginating as it goes.
- **`bing`** — Bing image search. Parses the `murl` entries out of the results
  HTML, paginating as it goes.
- **`auto`** — tries DuckDuckGo, then falls back to Bing if DDG yields nothing.
  DDG blocks many datacenter/VPS IPs, so the fallback is what usually fires on a
  server.

Pagination stops when the engine runs out of results or hits a safety cap
(500 candidates, 20 DDG pages, 15 Bing pages).

### Downloading

Each candidate is fetched with a browser-like `User-Agent` and a `Referer`
pointing at the source page. A candidate is **skipped** when:

- its URL is already in the download memory (no network request at all)
- the request fails or returns a non-`200` status
- the `Content-Type` isn't `image/*`
- its bytes match an image already on disk
- the file is smaller than `--min-bytes`

Otherwise it's saved as `image_001.jpg`, `image_002.png`, etc., numbered from
the highest index already in the folder.

## Download memory

The program keeps a `.crawl_pic_seen.json` file *inside the output folder*:

```json
{
  "urls": ["https://example.com/cat1.jpg"],
  "hashes": ["9f2c..."]
}
```

- **`urls`** — source URLs already fetched. Matching a candidate here skips it
  without any network request, so re-running a command costs almost nothing.
- **`hashes`** — SHA-256 of every saved image. Catches the same picture arriving
  under a different URL or filename.

On startup the folder itself is hashed as well, so the memory stays accurate even
if you delete the JSON file or add images by hand. Because the memory lives in
the output folder, pointing `-o` at a fresh folder starts you with a clean slate.

## Output

```
Downloads/
├── .crawl_pic_seen.json   # download memory (hidden)
├── image_001.jpg
├── image_002.png
└── image_003.webp
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | At least one new image was saved. |
| `1` | Nothing new to save, no image URLs found, or the page couldn't be fetched. |
| `2` | Bad command-line arguments. |

## Notes and limitations

- **URL mode downloads everything it finds**, including site logos, icons, and
  spacer images. Raise `--min-bytes` to skip more of them.
- **Search-engine markup changes.** Keyword mode parses HTML rather than using
  an official API, so a redesign on the engine's side can break it. Try
  `--engine bing` or `--engine ddg` to switch. `auto` already covers this.
- **Search relevance is whatever the engine gives you.** Without an API, results
  can include unrelated or logo-ish images.
- **Some hosts block hotlinking** and return `403` even for a valid image URL;
  those candidates are skipped and the program moves on.
- **The download memory only grows.** Deleting `.crawl_pic_seen.json` doesn't
  re-download anything, because the folder itself is hashed on startup.
- Only images linked from the given page are considered — the crawler does not
  recurse into other pages.
- Downloading images may be subject to the source site's terms and copyright.
  Check before reusing anything.

## Project layout

```
crawl_pic.py      # the program
quick-setup.sh    # dependency installer
requirements.txt  # runtime dependencies
```
