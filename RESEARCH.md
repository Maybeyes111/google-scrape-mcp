# Fighting Google's Blocks — Research Notes

This document records the methodology, measurements, and the policies derived
from them. Everything here comes from **measured probes** on a real machine,
not guesses.

## 1. Method

- **Block taxonomy** (`client.classify_html`): `js_challenge`, `captcha`,
  `sorry`/`unusual_traffic`, `http_429`, `consent`, `error`.
- **Forensics** (`forensics.py`): every block stores an HTML sample + metadata
  under `~/.cache/google-scrape-mcp/forensics/` (rolling 200 samples) plus
  counters in `block_stats.json`.
- **Probe harness** (`python3 -m google_scrape_mcp.probe --quick|--full|--tls|--analyze`):
  runs an endpoint/engine matrix with human-like spacing (default 8s,
  jittered 0.7–1.3×), printing a table + JSON.

Methodology lesson #1: raw substring classification is dangerous. The string
`"/sorry/index"` appears **6 times** inside a normal SERP's JS bundle (Google's
anti-bot JS literally checks `indexOf("/sorry/index")`). Classification must
use structural markers: `action="/sorry`, `id="captcha-form"`, or the visible
phrase "our systems have detected unusual traffic".

Methodology lesson #2: never trust a status without a sample. A filename bug
(endpoints contain `/`) silently dropped forensic samples; once fixed,
ground truth proved a 1.7 MB page with 10 `<h3>` results was a normal SERP
that had been misreported as `captcha`.

Methodology lesson #3: size guards matter. The "enable JavaScript" shell is
only ~90–100 KB; result pages without `<h3>` (images/shopping/AI Mode) were
being misclassified as challenges until a `len(html) < 250_000` guard was
added.

## 2. Probe data (quick run, 12 probes, 8s spacing)

| Probe | Engine | Result | Note |
|---|---|---|---|
| suggest | HTTP | ok 262ms | RSS/JSON, never blocked |
| news RSS | HTTP | ok 532ms | never blocked |
| patents XHR | HTTP | ok 1.1s | never blocked |
| trends daily RSS | HTTP | ok 509ms | never blocked |
| finance quote | HTTP | ok 330ms | large page, stable |
| **search, minimal params** | HTTP | **js_challenge** 92 KB | JS shell, 69ms |
| **search + `source=hp`** | HTTP | **js_challenge** | "human" params don't help |
| **search + profile NID cookie** | HTTP | **js_challenge** | even a real browser-issued NID doesn't help raw HTTP |
| **search** | browser | **ok** 1.98 MB | 14.2s |
| **search #2 (8s later)** | browser | **ok** 847 KB | 5.2s — session trusted |
| **AI Mode (udm=50)** | browser | **ok** 1.9 MB | 6.9s |
| scholar search | browser | **ok** 177 KB | 4.6s |

Summary: **9 ok, 3 js_challenge** — all blocks were HTTP-only.

## 3. Key findings

1. **Raw HTTP `/search` is always challenged** from this IP — with minimal
   params, `source=hp`, or a real NID cookie from a passing browser profile.
   The HTTP fast path for `/search` is effectively dead on flagged IPs; only
   running JavaScript (the browser) works.
2. **Cookies do not replace fingerprint/TLS+IP.** A valid NID does not rescue
   curl_cffi requests.
3. **Persistent browser + human pacing passes** for web, AI Mode, and scholar.
   Blocks that appeared during aggressive testing **decayed on their own**
   once traffic stopped — consistent with rate limiting, not a ban.
4. **RSS/JSON surfaces are stable**: suggest, news, patents, trends, finance,
   translate. These are the most reliable path for agents.
5. **TLS target is not the differentiator**: chrome120/142/150, firefox147 and
   safari260 all get the same js_challenge for raw HTTP.
6. **The proxy pool was mostly dead** (datacenter); `engine=proxy` produced no
   usable search results until curation.

## 3b. Breakthrough: cookie bootstrap

Further experiments found a legitimate fast path:

1. **A homepage warm-up alone** (open `google.com` in the persistent browser,
   no SERP render) mints fresh session cookies (`NID`, `AEC`, `SNID`, `GSP`,
   `DV`, `SEARCH_SAMESITE`, `__Secure-STRP`).
2. HTTP `/search` **with those cookies** → `ok`, 0.4–0.6s, full SERP
   (h3=8–10). Valid for ≥2 minutes and **portable across sessions/processes**.
3. The key is not just the cookies: navigation metadata must be coherent
   (`Referer: google.com` + `Sec-Fetch-Site: same-origin`), and the **session
   jar must be clean** — a warmed-up session carries older cookies that
   conflict and cause rejection (test: warm session + bootstrap cookies =
   js_challenge; clean session + bootstrap cookies = ok).
4. Surfaces that pass the fast path: **plain web, `tbm=vid`, `tbm=bks`,
   `tbm=isch`, `tbm=shop`**. Scholar HTTP = 429 (stays on browser). AI Mode
   HTTP only contains the question bubble (the answer is streamed via JS) →
   stays on browser. The News tab HTTP page is parseable, but RSS is cheaper.
5. Measured after integration: web 0.3–0.7s, images 0.9–1.6s, video/books
   1.4–2.9s — versus 5–15s for a full browser render. Bootstrap cookies are
   cached in `bootstrap_cookies.json` with a 5-minute TTL
   (`GOOGLE_SCRAPE_COOKIE_TTL`).

Fallback policy: if the HTTP result is empty (different markup) → render in
the browser; if rejected on the primary surface → invalidate bootstrap and
retry once after a refresh; if rejected on another surface → keep the cookies
and send only that surface to the browser.

## 3c. Burned browser profile → identity rotation

After a long test session, the same persistent profile started being rejected
continuously (browser render blocked even after a 5-minute rest). Test: move
the profile away → a fresh profile passes immediately (search h3=8, 477 KB).
So blocks can stick to a **profile identity**, not only to the IP.

Policy: `note_browser_blocked` at level ≥ 2 (consecutive blocks within an
hour) triggers `rotate_profile()`: the persistent browser is closed, the old
profile is archived as `cfx-profile.burned-<ts>` (latest 2 kept), bootstrap
cookies are invalidated, and the next call runs with a fresh identity.

Fail-fast addition: an adaptive `browser_cooldown` (180s × 2^(n-1), capped at
1 hour) means a rate-limit period no longer costs ~100s per call — calls
return `status=blocked` immediately with the remaining time until the
cooldown expires.

## 4. Implemented policies

- **Adaptive HTTP cooldown per endpoint family** (`search`, `scholar`,
  `finance`): the n-th block within an hour → `600s × 2^(n-1)` (capped at 1h).
  Reset when the family's HTTP path succeeds again.
- **Endpoint gating**: successful RSS/JSON requests (suggest, etc.) do **not**
  reset the `/search` cooldown — earlier this caused pointless HTTP retries.
- **engine=auto**: when a family is in cooldown → go straight to the browser
  (JS), with a transparent `note` in the output.
- **Cookie bootstrap fast path** for supported surfaces (see 3b).
- **Profile rotation + browser cooldown** (see 3c).
- **Forensics**: block samples + counters exposed via `google_status`.
- **Probe harness** for continuous measurement.

## 4b. Speed work (v0.8.0)

Measured before → after on the same machine:

| Operation | Before | After |
|---|---|---|
| Browser homepage warm-up + cookies | 16.4s | **5.7s** |
| Web search end-to-end | ~1.0s | **0.34s** |
| Scholar search | 11.3s | **4.2–4.4s** |
| AI Mode answer | 11.1s / truncated prefix | **~10.8s / complete (2.4k chars)** |
| SERP parse + `/goto` resolution | sequential | **parallel, ~0.5s** |

Mechanisms:

1. Warm-up and cookie retrieval merged into **one priority browser job**
   (previously two jobs, each paying a 2.5–6s human gap).
2. **Adaptive warm skip** (`GOOGLE_SCRAPE_WARM_TTL=600`): the homepage visit is
   only done when the profile has not been warmed recently.
3. **Event waits instead of sleeps**: `wait_for` selectors (scholar `div.gs_ri`,
   shopping `div.gkQHve`, news `div.n0jPhd`) and text-stability polling for the
   AI Mode answer (`div.Rty6Hf`).
4. **Parallel `/goto` resolution** with 6 threads (cap 12 links) — the search
   URL itself is accurate again instead of an opaque token.
5. **Background bootstrap keeper**: refreshes cookies while tools are in use
   (15-minute activity window), so user-facing calls rarely pay 5.7s.
6. Job gap reduced to 1.5–3.5s (`GOOGLE_SCRAPE_JOB_GAP`).

Bug fixed along the way: the AI Mode retry path used the original `engine`
(`auto`) instead of the effective one, so a truncated first render was retried
over HTTP and returned only the question bubble.

## 5. Open research items

1. **Residential proxy curation**: run `curate` against a good residential
   pool and re-measure `search_proxy_http`; a clean proxy could revive the
   low-latency HTTP path without the browser.
2. **Decay measurement**: how long do browser blocks take to clear after a
   captcha (repeat probes with 5/15/30-minute gaps).
3. **TLS target experiments**: newer impersonation targets were tested
   (`chrome142/150`) and made no difference for raw HTTP; re-test if Google's
   detection changes.
4. **Parameter replay**: `ei`/`sei`, `iflsig`, `mstk` replay is already used by
   the fast path; explore page-context conversation replays next.
5. **Per-surface profiles**: measure whether AI Mode benefits from a profile
   that is not shared with scholar, etc.
