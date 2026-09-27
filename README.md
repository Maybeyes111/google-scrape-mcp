# google-scrape-mcp

MCP server for **Google Search via pure scraping — no API key required**.
Two engines work together: a fast HTTP path (`curl_cffi` with real Chrome TLS
impersonation) and a **Camoufox headless browser** fallback that actually runs
JavaScript when Google challenges the request.

Built for agents that need Google results **reliably and honestly**: every
response carries an explicit `status`, and the server never fabricates results.

## Features

- **Unified search tool + 9 tabs**: web, images, videos, news, books,
  shopping, scholar, patents, and AI Mode.
- **Cookie bootstrap fast path**: a browser homepage warm-up mints fresh
  session cookies; subsequent HTTP searches pass in **0.3–2.9s** (vs 5–15s
  full browser render). Cookies are cached for 5 minutes.
- **Browser fallback that obeys JS**: persistent Camoufox profile, human-like
  pacing, consent handling, challenge wait/reload — everything a real visitor
  does.
- **Profile rotation**: when a browser identity gets burned (repeated blocks),
  it is archived and a fresh profile takes over automatically.
- **Adaptive cooldowns**: HTTP is retried per endpoint family with escalating
  backoff; browser blocks trigger a fail-fast cooldown so a rate-limit period
  does not cost 100s per call.
- **Proxy pool**: rotation, per-proxy cooldown, proven-only usage for browser
  fallback, plus a curation tool that validates proxies against `/search`.
- **Forensics & research harness**: blocked pages are sampled to disk with
  metadata; `probe` measures block types per endpoint/engine so you can tune
  policies with data instead of guesses.
- **RSS/JSON surfaces** (news, patents, trends, suggest, translate, finance)
  that are essentially never blocked and are preferred for reliability.

## Install

```bash
pip install -e .            # HTTP engine only
pip install -e ".[browser]" # + Camoufox browser engine
camoufox fetch              # download the Camoufox browser once
```

Core dependencies: `fastmcp`, `curl_cffi`, `beautifulsoup4`, `lxml`.
Optional: `camoufox` (browser engine).

## Run

```bash
google-scrape-mcp           # stdio transport (for MCP clients)
```

MCP client config:

```json
{
  "mcpServers": {
    "google-scrape": {
      "command": "google-scrape-mcp"
    }
  }
}
```

## Tools (20)

| Tool | Source | Notes |
|---|---|---|
| `google_search` | **unified**: `tab` = web/images/videos/news/books/shopping/scholar/patents/ai, `page` 1–10 | dispatcher to the tools below |
| `google_web_search` | organic results, featured snippet, related searches | HTTP fast path, browser fallback |
| `google_image_search` | direct image URLs, page URLs, dimensions | fast path OK |
| `google_video_search` | `tbm=vid` | fast path OK |
| `google_books_search` | `tbm=bks` | fast path OK |
| `google_shopping_search` | product title, price, was-price, merchant, rating | fast path when possible, browser fallback |
| `google_news_search` / `google_news_homepage` | News RSS | always live, never blocked |
| `google_scholar_search` | papers, authors/venue, citations, PDF links | HTTP 429 → browser |
| `google_scholar_cited_by` | Scholar `cites=` | extra protection: often `blocked` (reported honestly) |
| `google_patents_search` | Patents XHR JSON | always live |
| `google_finance_quote` | stocks/forex/crypto (`USD-IDR`, `BBCA:IDX`) | always live |
| `google_translate` | unofficial `gtx` endpoint | always live |
| `google_suggest` | autocomplete | always live |
| `google_trends_daily` | Trends RSS | always live |
| `google_trends_interest` | explore → multiline widgetdata | HTTP often 401 → browser fallback |
| `google_ai_mode` | AI Mode (`udm=50`): synthesized `answer` + `sources` | browser (JS streams the answer) |
| `google_crawl` | read any URL: title, meta, text, outbound links | live |
| `google_help` | agent usage guide (same as `AGENT_GUIDE.md`) | live |
| `google_status` | health check: endpoints, proxies, cache, cooldowns | live |

All tools return `{"status": "ok" | "blocked" | "rate_limited" | "limited" | "empty" | "error", ...}`.
Blocked means blocked — no fake results.

> **AI agents**: read [`AGENT_GUIDE.md`](AGENT_GUIDE.md), or call the
> `google_help` tool at runtime for the same guidance.

## Engines

Every search tool accepts `engine`:

- `auto` (default) — HTTP fast path with bootstrap cookies, then browser.
- `http` — direct HTTP only (challenge-prone on flagged IPs).
- `proxy` — force the proxy pool.
- `browser` — Camoufox headless render (~5–15s).

### Cookie bootstrap (the fast path)

1. A browser warm-up visits `google.com` (persistent profile) and mints fresh
   session cookies (`NID`, `AEC`, `SNID`, `GSP`, …).
2. Those cookies are sent over plain HTTP with coherent navigation metadata
   (`Referer` + `Sec-Fetch-Site: same-origin`) from a *clean* session jar.
3. Surfaces that pass over HTTP: **web, images, videos, books, shopping**.
   Scholar (429) and AI Mode (answer is streamed by JS) stay on the browser.

Cookies are cached in `~/.cache/google-scrape-mcp/bootstrap_cookies.json`
with a 5-minute TTL (`GOOGLE_SCRAPE_COOKIE_TTL`).

## Performance

Measured on a residential/flagged IP (v0.8.0):

| Operation | Before | Now |
|---|---|---|
| Web search (end-to-end) | ~1.0s | **0.3–0.7s** |
| Bootstrap warm-up | 16.4s | **5.7s** |
| Scholar search | 11.3s | **4.2–4.4s** |
| AI Mode (full answer) | 11.1s (sometimes truncated) | **~10.8s, complete** |
| `/goto` link resolution | sequential | **parallel, ~0.5s** |

Speed mechanisms:

- **One priority job** for warm-up + cookie retrieval (no queue gap, no second job).
- **Adaptive warm skip**: if the browser profile was warmed within
  `GOOGLE_SCRAPE_WARM_TTL` (default 600s), the homepage visit is skipped.
- **Content waits instead of fixed sleeps**: `wait_for` selectors for scholar/
  news/shopping and text-stability polling for AI Mode answers.
- **Parallel `/goto` resolution** (6 workers, cap 12 links).
- **Background bootstrap keeper**: while tools are being used (15-minute
  activity window), cookies are refreshed in the background so searches never
  pay the warm-up cost. Disable with `GOOGLE_SCRAPE_KEEPER=0`.
- **Human job gap** reduced to 1.5–3.5s (`GOOGLE_SCRAPE_JOB_GAP=min-max`).

## Proxy pool

Accepted line formats: `http://host:port`, `https://host:port`,
`socks5://host:port` (`socks5h` normalized), `socks4://host:port`, with
optional `user:pass@`, plus bare `host:port` and `host:port:user:pass`.

Sources (priority order):

1. `GOOGLE_SCRAPE_PROXIES` — inline list (comma/space/newline separated) or a
   file path (starting with `/`, `~`, or ending in `.txt`).
2. `GOOGLE_SCRAPE_PROXY_FILES` — colon-separated file paths.
3. Defaults when present: `~/.config/google-scrape/proxies.txt` and
   `~/.cache/google-scrape-mcp/proxies_curated.txt` (max 500 lines/file).

Failed proxies get a cooldown (`GOOGLE_SCRAPE_PROXY_COOLDOWN`, default 600s).
Browser fallback only uses proxies that have **succeeded before** (proven), so
dead datacenter pools don't waste minutes.

Curate your own pool (validates liveness *and* `/search` capability):

```bash
python3 -m google_scrape_mcp.curate --limit 100 --workers 8
```

## Environment variables

| Env | Default | Purpose |
|---|---|---|
| `GOOGLE_SCRAPE_PROXY_MODE` | `auto` | `auto` (direct→proxy), `off`, `always` |
| `GOOGLE_SCRAPE_PROXY_COOLDOWN` | `600` | failed-proxy cooldown (s) |
| `GOOGLE_SCRAPE_PROXY_CA` | — | extra CA bundle for self-signed HTTPS proxies |
| `GOOGLE_SCRAPE_MIN_INTERVAL` | `1.0` | min delay between requests (s) |
| `GOOGLE_SCRAPE_TIMEOUT` | `25` | request timeout (s) |
| `GOOGLE_SCRAPE_RETRIES` | `3` | proxy attempts per request |
| `GOOGLE_SCRAPE_CACHE_TTL` | `600` | response cache TTL (`0` disables) |
| `GOOGLE_SCRAPE_CACHE_MAX` | `256` | max cache entries |
| `GOOGLE_SCRAPE_IMPERSONATE` | `chrome120` | curl_cffi TLS target |
| `GOOGLE_SCRAPE_JS_COOLDOWN` | `600` | base HTTP cooldown after a block (doubles per level, capped at 1h) |
| `GOOGLE_SCRAPE_COOKIE_TTL` | `300` | bootstrap cookie lifetime (s) |
| `GOOGLE_SCRAPE_BROWSER_COOLDOWN` | `180` | base browser cooldown after a block (doubles per level) |
| `GOOGLE_SCRAPE_FORENSICS` | `1` | save blocked-page samples |
| `GOOGLE_SCRAPE_NO_SELFHEAL` | — | disable automatic `camoufox fetch` when the browser is missing |
| `GOOGLE_SCRAPE_WARM_TTL` | `600` | skip the browser homepage warm-up if warmed more recently than this (s) |
| `GOOGLE_SCRAPE_JOB_GAP` | `1.5-3.5` | human-like delay between browser jobs (min-max seconds) |
| `GOOGLE_SCRAPE_KEEPER` | `1` | background bootstrap keeper (`0` disables) |

## Research & forensics

- [`RESEARCH.md`](RESEARCH.md) — measured findings: why raw HTTP `/search`
  always gets a JS challenge, why cookies alone don't help, how the bootstrap
  fast path works, surface-by-surface results, profile burnout.
- `python3 -m google_scrape_mcp.probe --quick|--full|--tls|--analyze` —
  controlled block-measurement harness with human-like spacing.
- Blocked pages are sampled (HTML + metadata) under
  `~/.cache/google-scrape-mcp/forensics/`, with counters in
  `block_stats.json` (surfaced by `google_status`).

## Known limitations

- `google_scholar_cited_by` is frequently `blocked` (Google protects that
  endpoint harder). Use the `cited_by` count from `google_scholar_search`.
- Shopping product URLs are rendered on click; the tool returns title, price,
  was-price, merchant and rating.
- No Maps/local search, reverse image search, AI Overview (inline) or flights.
- Datacenter proxy pools are mostly useless for Google; prefer residential.

## License

MIT — see [LICENSE](LICENSE).
