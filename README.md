# crawl-pic

Scrapes pictures from a web page, or downloads them from image-search results
for a set of keywords. Everything lands in a `Downloads` folder.

## Requirements

```bash
pip install -r requirements.txt
```

Requires Python 3.8+ and only `requests` + `beautifulsoup4` (no API keys).

## Usage

Pass either a page URL or some keywords as the only positional argument, plus
how many pictures you want with `-n`.

```bash
# From a web page
python crawl_pic.py https://example.com/gallery -n 20

# From search keywords
python crawl_pic.py "red pandas" -n 15

# Pick a folder / engine explicitly
python crawl_pic.py "sunset over the ocean" -n 8 -o Downloads --engine bing
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `target` | – | A page URL (`http://`/`https://`) **or** search keywords. |
| `-n`, `--count` | `10` | How many images to download. |
| `-o`, `--output` | `Downloads` | Output folder (created if missing). |
| `--engine` | `auto` | Image search engine for keyword mode: `auto`, `ddg`, or `bing`. |
| `--timeout` | `15` | Per-request timeout in seconds. |
| `--min-bytes` | `1024` | Skip files smaller than this. |

The mode is detected automatically: anything starting with `http://` or
`https://` is treated as a page to scrape; everything else is treated as search
keywords.

In `auto` mode the program tries DuckDuckGo image search first and falls back to
Bing if DuckDuckGo returns nothing (its image API blocks many datacenter IPs).
