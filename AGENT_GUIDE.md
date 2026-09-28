# google-scrape-mcp — Agent Guide

Written for AI models/agents using the `google-scrape` tools (not for the
operator installing it). Every tool returns a JSON dict and never fabricates
results: when Google blocks, `status` says so explicitly.

## 1. Tool map

| Need | Tool | Notes |
|---|---|---|
| General / multi-tab search | `google_search` | `tab`: web, images, videos, news, books, shopping, scholar, patents, ai; `page` 1–10 |
| Direct synthesized answer | `google_ai_mode` | AI Mode (udm=50): `answer` + `sources`; browser-based, ~10–20s |
| Web organic results | `google_web_search` | featured snippet + related searches |
| Images | `google_image_search` | `image_url`, `page_url`, width/height |
| Video / Books | `google_video_search`, `google_books_search` | |
| Shopping | `google_shopping_search` | `title`, `price`, `was_price`, `merchant`, `rating` (product URL renders on click) |
| News | `google_news_search`, `google_news_homepage` | RSS, most reliable |
| Academic papers | `google_scholar_search` | `cluster_id`, `cited_by`, `pdf_url` |
| Citations | `google_scholar_cited_by` | often `blocked` (extra protection) — handle it, don't retry hard |
| Patents | `google_patents_search` | JSON XHR, stable |
| Stocks/forex/crypto | `google_finance_quote` | `USD-IDR`; `BBCA:IDX`, `AAPL:NASDAQ` |
| Translation | `google_translate` | |
| Autocomplete | `google_suggest` | great for keyword expansion |
| Daily trending | `google_trends_daily` | RSS per `geo` |
| Interest over time | `google_trends_interest` | `keywords` comma-separated, max 5; `timeframe` like `now 7-d`, `today 12-m`, `all` |
| Read any URL | `google_crawl` | title, meta, text, up to 50 links |
| Health check | `google_status` | endpoints + proxies + cache + cooldowns |
| Usage guide | `google_help` | this document, condensed |

## 2. Engine semantics (all search tools)

- `auto` (default) — fast HTTP path using bootstrap cookies; falls back to a
  JS-running browser automatically. On flagged IPs, `auto` may skip straight
  to the browser (see the `note` in the response).
- `http` — force direct HTTP (fast, but challenge-prone; avoid when
  `http_blocked.active` is true in `google_status`).
- `proxy` — force the proxy pool (uses bootstrap cookies too, so curated
  free proxies can return real results; expect ~10s and occasional fallback).
- `browser` — Camoufox headless render (~5–15s, most "human").

Never set `engine=http` repeatedly when the result is `blocked` — switch to
`auto`/`browser` instead, or wait.

## 3. Response contract

```json
{"status": "ok|blocked|rate_limited|limited|empty|error", "...": "..."}
```

- `ok` — success; read the result keys (`results` / `answer` / `quote` /
  `translation` / `suggestions` / `timeline` / `endpoints`).
- `blocked` / `rate_limited` — Google refused this session/IP. The `error`
  field explains. Do not loop; switch to an RSS tool (e.g.
  `google_news_search`) or wait a few minutes.
- `limited` — `google_trends_interest` only: widgetdata refused both HTTP and
  browser. Fall back to `google_trends_daily`.
- `empty` — the page loaded but Google returned no results.
- `error` — technical failure (browser launch, parsing, …). Read `error`.

Useful extra keys:
- `engine` — how the result was obtained: `http` | `cache` | `browser`.
  `http` often means the **bootstrap fast path** (note: "bootstrap cookies:
  HTTP langsung") at 0.3–3s. If it is rejected, the tool falls back to the
  browser automatically — no agent action needed.
- `cached: true` — served from the response cache (TTL 10 minutes).
- `note` — why `auto` chose a particular path.

Result shapes:
- Web/video/books: `title`, `url`, `cite`, `site`, `snippet`.
- Images: `image_url`, `page_url`, `title`, `thumbnail`, `width`, `height`.
- Scholar: `title`, `url`, `authors_venue`, `snippet`, `cited_by`,
  `cited_by_url`, `cluster_id`, `pdf_url`.
- AI Mode: `answer` (cleaned synthesized text) + `sources`.

## 4. Correct usage patterns

1. **Cheap and stable first**: `google_suggest` (expansion),
   `google_news_search` (RSS), `google_patents_search` (JSON),
   `google_trends_daily` (RSS).
2. For research: `google_search(tab="web")` → read the top 3–5 hits →
   `google_crawl(url)` on the relevant ones.
3. **Pagination**: `page` 1–10 (unified). `num` = results per batch (web max
   100, others 50). For scholar, `start` is a 0-based offset.
4. **News pagination** is client-side: fetch `page*num`, then slice.
5. **Do not hammer**: repeated identical queries within the cache window are
   usually served from cache. Space experiments; when `blocked`, stop for a
   few minutes.
6. **Trends**: if `limited`, don't retry — use `google_trends_daily`.
7. **cited_by**: if `blocked`, report it; the `cited_by` count and
   `cited_by_url` from `google_scholar_search` are still usable.
8. `google_crawl` accepts any http/https URL, not just Google results.

## 5. Example calls

```json
{"query": "postgresql autovacuum tuning", "tab": "web", "page": 1, "num": 10}
{"query": "mechanical keyboard", "num": 5, "engine": "auto"}
{"query": "graph neural network", "start": 0, "year_low": 2023}
{"cluster_id": "18445238210663890041", "num": 10}
{"query": "wireless charging", "page": 2}
{"ticker": "USD-IDR"}
{"ticker": "BBCA", "exchange": "IDX"}
{"keywords": "coffee, tea, matcha", "geo": "ID", "timeframe": "today 12-m"}
{"url": "https://example.com/article", "max_chars": 4000, "include_links": true}
{"query": "what is io_uring", "engine": "auto"}
```

## 6. AI Mode answers (for agents)

- `google_ai_mode(query)` returns `answer` (synthesized text) + `sources`.
- Best for "what is / how to / comparison" questions; still verify important
  claims via `google_search` + `google_crawl`.
- The page is JS-heavy: `engine=auto` uses the browser; the tool retries once
  if the answer has not streamed yet.
- If `status=blocked`, that is a session/IP rate-limit — wait a few minutes.

## 7. Not available / limitations

- Maps/local search, reverse image search, inline AI Overview, flights: not
  available.
- Google Shopping does not expose product URLs in the initial HTML.
- `google_scholar_cited_by` is often `blocked` from datacenter IPs.
- RSS/JSON tools (news, patents, trends daily, suggest, translate) are
  essentially never blocked — prefer them when reliability matters.

## 8. Relevant configuration for agents

- HTTP blocks are recorded (`http_blocked` in `google_status`); while a family
  is in cooldown, `auto` goes straight to the browser — that is not a failure.
- Bootstrap cookies live 5 minutes; search results may be cached (default
  TTL 10 minutes). Do not assume real-time freshness.
- Browser cooldowns are fail-fast: while active, `status=blocked` is returned
  immediately with the remaining seconds.
- Full environment variable reference: see `README.md`.
