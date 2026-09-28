"""Google Scrape MCP — pencarian Google murni scrape/crawl, tanpa API key.

Arsitektur: curl_cffi (impersonasi TLS Chrome) + BeautifulSoup/lxml, dengan
pool proxy rotasi + cooldown, retry/backoff, cache TTL, dan fallback Camoufox
headless (opsional) untuk halaman yang diblokir. Tool yang endpoint-nya
diblokir Google mengembalikan status transparan, bukan hasil palsu.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from fastmcp import FastMCP

from .client import (
    BLOCKED_MSG,
    start_bootstrap_keeper,
    blocked_families,
    bootstrap_cookies,
    bootstrap_info,
    bootstrap_valid,
    browser_blocked_active,
    browser_blocked_left,
    browser_blocked_reason,
    cache_stats,
    classify_html,
    clean_google_url,
    endpoint_family,
    endpoint_label,
    fetch,
    invalidate_bootstrap,
    note_browser_blocked,
    note_browser_ok,
    refresh_bootstrap,
    http_blocked_active,
    http_blocked_left,
    http_blocked_reason,
    is_blocked_page,
    proxy_stats,
)
from .parsers import (
    parse_ai_mode,
    parse_finance_quote,
    parse_fx_widget,
    parse_kurs_bi,
    parse_images,
    parse_news_live,
    parse_news_rss,
    parse_patents,
    parse_scholar,
    parse_serp_live,
    parse_shopping,
    parse_trends_rss,
    parse_web,
)
from .forensics import record as forensic_record, save_sample, stats as forensic_stats
from .proxies import get_pool

mcp = FastMCP("google-scrape")

SEARCH_BASE = "https://www.google.com/search"
SCHOLAR_BASE = "https://scholar.google.com/scholar"

_LOCALE = {"en": "en-US", "id": "id-ID"}

_ENGINE_DOC = (
    "engine: auto (HTTP cepat → proxy pool → browser) | http (direct saja) | "
    "proxy (paksa lewat pool proxy) | browser (langsung Camoufox headless)."
)


def _fast_surface(family: str, params: dict) -> bool:
    """Surface yang terbukti lolos lewat HTTP + cookie bootstrap (riset):
    web polos, tbm=vid/bks/isch/shop. Scholar (429), AI mode & news tab
    (butuh JS / tanpa markup HTTP) memakai jalur browser."""
    if family != "search":
        return False
    if "udm" in params and str(params["udm"]) != "14":
        return False
    return params.get("tbm") in (None, "vid", "bks", "isch", "shop")


def _proxy_mode_for(engine: str) -> str:
    engine = (engine or "auto").lower()
    if engine == "http":
        return "off"
    if engine == "proxy":
        return "always"
    return "auto"


def _block_status(status_code: int | None) -> str:
    return "rate_limited" if status_code == 429 else "blocked"


def _proxy_label(proxy) -> str:
    return f"{proxy.scheme}://{proxy.host}:{proxy.port}"


def _call_parser(parse_fn, html: str):
    """Panggil parser; matikan resolve /goto via HTTP di jalur browser
    (token /goto-nya sudah di-resolve in-page, jadi HEAD langsung dibuang)."""
    try:
        import inspect
        if "resolve_links" in inspect.signature(parse_fn).parameters:
            return parse_fn(html, resolve_links=False)
    except (TypeError, ValueError):
        pass
    return parse_fn(html)


_WAIT_FOR = {
    parse_scholar: "div.gs_ri",
    parse_ai_mode: "div.Rty6Hf",
    parse_news_live: "div.n0jPhd",
    parse_shopping: "div.gkQHve",
}
_WAIT_TEXT = {
    parse_ai_mode: "div.Rty6Hf",
}


def _browser_attempt(base: str, params: dict, hl: str, parse_fn,
                     query: str, label: str, proxy,
                     expect: tuple = ("<h3",)) -> dict:
    from .browser import fetch_rendered
    res = fetch_rendered(base, params=params,
                         locale=_LOCALE.get(hl, hl or "en-US"),
                         proxy=proxy, expect=expect,
                         wait_for=_WAIT_FOR.get(parse_fn),
                         wait_text=_WAIT_TEXT.get(parse_fn))
    out = {label: query, "engine": "browser",
           "http_status": res.get("http_status"), "url": res.get("url")}
    if res.get("error"):
        forensic_record("browser_error", "browser", endpoint_label(base),
                        str(res.get("url") or base), res.get("http_status"),
                        str(res["error"]))
        out.update({"status": "error", "error": res["error"], "results": []})
        return out
    if res.get("blocked"):
        out.update({"status": "blocked", "error": BLOCKED_MSG, "results": []})
        return out
    if res.get("challenge"):
        out.update({"status": "blocked",
                    "error": "Google menyajikan halaman 'enable JavaScript' dan JS-nya "
                             "belum selesai di sesi browser ini (challenge belum lolos). "
                             "Coba lagi — profil Camoufox biasanya lolos di percobaan "
                             "berikutnya.",
                    "results": []})
        return out
    try:
        parsed = _call_parser(parse_fn, res["html"])
    except Exception as e:
        out.update({"status": "error",
                    "error": f"Browser parse failed: {e}", "results": []})
        return out
    results_list = (parsed.get("results") if isinstance(parsed, dict)
                    else parsed)
    if not results_list and res.get("empty"):
        out.update({"status": "empty",
                    "error": "Browser merender tapi Google tidak menampilkan hasil "
                             "(kemungkinan query kosong/region).",
                    "results": []})
        return out
    out.update({"status": "ok"})
    if isinstance(parsed, dict):
        if isinstance(results_list, list):
            from .parsers import apply_resolved
            parsed["results"] = apply_resolved(results_list, res.get("resolved"))
        out.update(parsed)
    else:
        out.update({"results": parsed})
    return out


def _render_fallback(base: str, params: dict, hl: str, parse_fn,
                     query: str, label: str = "query",
                     expect: tuple = ("<h3",)) -> dict:
    """Fallback Camoufox headless; bila diblokir & ada proxy proven,
    ulangi sekali lewat proxy. Cooldown browser mencegah panggilan
    beruntun saat Google sedang rate-limit (fail-fast)."""
    if browser_blocked_active():
        return {label: query, "status": "blocked", "engine": "browser",
                "error": "Browser cooldown aktif "
                         f"({browser_blocked_reason()}, "
                         f"{int(browser_blocked_left())}s tersisa) — Google "
                         "rate-limit; tunggu atau pakai tool RSS.",
                "results": []}
    out = _browser_attempt(base, params, hl, parse_fn, query, label,
                           proxy=None, expect=expect)
    if out.get("status") == "ok":
        note_browser_ok()
    elif out.get("status") == "blocked":
        note_browser_blocked("render_blocked")
    if out.get("status") in ("blocked", "error"):
        pool = get_pool()
        if pool.proven_count() and out.get("status") == "blocked":
            proxy = pool.acquire_proven()
            if proxy:
                alt = _browser_attempt(base, params, hl, parse_fn, query,
                                       label, proxy=proxy.playwright(),
                                       expect=expect)
                if alt.get("status") == "ok":
                    pool.report_success(proxy)
                    alt["proxy"] = _proxy_label(proxy)
                    return alt
                pool.report_failure(proxy)
    return out


def _trim(out: dict, limit: int | None = None, label: str = "results") -> dict:
    if limit and isinstance(out.get(label), list):
        out[label] = out[label][:limit]
    return out


def _run_search(base: str, params: dict, render_params: dict, hl: str,
                query: str, engine: str, http_parse, render_parse,
                label: str = "query", limit: int | None = None,
                expect: tuple = ("<h3",)) -> dict:
    """Satu alur: HTTP (auto/proxy/http) → fallback browser."""
    engine = (engine or "auto").lower()
    if engine == "browser":
        out = _render_fallback(base, render_params, hl, render_parse, query,
                               label=label, expect=expect)
        return _trim(out, limit)
    family = endpoint_family(base)

    if engine == "auto" and family and _fast_surface(family, params):
        # Fast-path temuan riset: cookie segar dari warm-up browser membuat
        # raw HTTP /search lolos (~0.5-1 dtk vs render 5-15 dtk).
        if not bootstrap_valid():
            refresh_bootstrap()
        if bootstrap_valid():
            r = fetch(base, params=params,
                      cookies=bootstrap_cookies())
            cached = bool(getattr(r, "from_cache", False))
            out = {"status": "ok", "query": query, "http_status": r.status_code,
                   "url": str(r.url), "engine": "cache" if cached else "http"}
            if cached:
                out["cached"] = True
            if not (is_blocked_page(r.text) or r.status_code == 429):
                parsed = http_parse(r.text)
                if isinstance(parsed, dict):
                    out.update(parsed)
                else:
                    out["results"] = parsed
                empty_results = (isinstance(out.get("results"), list)
                                 and not out["results"]
                                 and "did not match" not in r.text)
                if not empty_results:
                    out["note"] = "bootstrap cookies: HTTP langsung"
                    return _trim(out, limit)
                # halaman ke-serve tapi markup hasil berbeda → render browser
                out = _render_fallback(base, render_params, hl, render_parse,
                                       query, label=label, expect=expect)
                out.setdefault("note", "hasil HTTP kosong → browser")
                return _trim(out, limit)
            plain_web = "tbm" not in params and "udm" not in params
            if plain_web:
                # cookie basi untuk surface utama → segarkan sekali lalu retry
                invalidate_bootstrap()
                if refresh_bootstrap():
                    r2 = fetch(base, params=params, cookies=bootstrap_cookies())
                    if not (is_blocked_page(r2.text) or r2.status_code == 429):
                        parsed2 = http_parse(r2.text)
                        out2 = {"status": "ok", "query": query,
                                "http_status": r2.status_code, "url": str(r2.url),
                                "engine": "http",
                                "note": "bootstrap di-refresh: HTTP langsung"}
                        if isinstance(parsed2, dict):
                            out2.update(parsed2)
                        else:
                            out2["results"] = parsed2
                        if not (isinstance(out2.get("results"), list)
                                and not out2["results"]
                                and "did not match" not in r2.text):
                            return _trim(out2, limit)
                    invalidate_bootstrap()
            out = _render_fallback(base, render_params, hl, render_parse,
                                   query, label=label, expect=expect)
            out.setdefault("note", "bootstrap ditolak untuk surface ini → browser")
            return _trim(out, limit)
        # browser tak tersedia → lanjut alur biasa (HTTP polos / cooldown)

    if engine == "auto" and http_blocked_active(family):
        # Raw HTTP family ini sedang diblokir/challenge; jalur manusia
        # (browser ber-JS) langsung dipakai tanpa membuang request.
        out = _render_fallback(base, render_params, hl, render_parse, query,
                               label=label, expect=expect)
        out["note"] = ("auto: raw HTTP diblokir "
                       f"({http_blocked_reason(family)}), langsung jalur browser "
                       f"({int(http_blocked_left(family))}s cooldown).")
        return _trim(out, limit)
    boot = bootstrap_cookies() if (family and bootstrap_valid()) else None
    r = fetch(base, params=params, proxy_mode_override=_proxy_mode_for(engine),
              cookies=boot)
    cached = bool(getattr(r, "from_cache", False))
    out = {"status": "ok", "query": query, "http_status": r.status_code,
           "url": str(r.url), "engine": "cache" if cached else "http"}
    if cached:
        out["cached"] = True
    if is_blocked_page(r.text) or r.status_code == 429:
        if engine in ("http", "proxy"):
            out.update({"status": _block_status(r.status_code),
                        "error": BLOCKED_MSG, "results": []})
            return out
        out = _render_fallback(base, render_params, hl, render_parse, query,
                               label=label, expect=expect)
        return _trim(out, limit)
    parsed = http_parse(r.text)
    if isinstance(parsed, dict):
        out.update(parsed)
    else:
        out["results"] = parsed
    return _trim(out, limit)


# ---------------------------------------------------------------- web ----
@mcp.tool()
def google_web_search(query: str, num: int = 10, start: int = 0,
                      hl: str = "en", gl: str = "us",
                      engine: str = "auto") -> dict:
    """Scrape Google Web Search: organic results, featured snippet, related searches.

    engine: auto (HTTP cepat, fallback browser bila diblokir) | http (tanpa
    browser) | proxy (paksa pool proxy) | browser (langsung Camoufox headless,
    tanpa proxy/API key).
    """
    num = max(1, min(num, 100))
    start = max(0, start)
    render_params = {"q": query, "num": num, "start": start,
                     "hl": hl, "gl": gl}
    params = {"q": query, "num": num, "start": start,
              "hl": hl, "gl": gl}
    return _run_search(SEARCH_BASE, params, render_params, hl, query, engine,
                       parse_web, parse_serp_live)


@mcp.tool()
def google_image_search(query: str, num: int = 20, page: int = 1,
                        hl: str = "en", gl: str = "us",
                        engine: str = "auto") -> dict:
    """Scrape Google Images (tbm=isch): direct image URLs, page URLs, sizes.

    engine: auto | http | proxy | browser (lihat google_web_search).
    """
    num = max(1, num)
    page = max(1, min(page, 10))
    params = {"q": query, "tbm": "isch", "hl": hl, "gl": gl,
              "ijn": page - 1, "start": (page - 1) * num}
    render_params = {"q": query, "tbm": "isch", "hl": hl, "gl": gl}
    return _run_search(SEARCH_BASE, params, render_params, hl, query, engine,
                       parse_images, parse_images, limit=num,
                       expect=("encrypted-tbn",))


@mcp.tool()
def google_video_search(query: str, num: int = 10, start: int = 0,
                        hl: str = "en", gl: str = "us",
                        engine: str = "auto") -> dict:
    """Scrape Google Video Search (tbm=vid): title, URL, snippet.

    engine: auto | http | proxy | browser (lihat google_web_search).
    """
    num = max(1, min(num, 50))
    start = max(0, start)
    render_params = {"q": query, "tbm": "vid", "num": num,
                     "hl": hl, "gl": gl}
    params = {"q": query, "tbm": "vid", "num": num, "start": start,
              "hl": hl, "gl": gl}
    return _run_search(SEARCH_BASE, params, render_params, hl, query, engine,
                       parse_web, parse_serp_live, limit=num)


@mcp.tool()
def google_books_search(query: str, num: int = 10, start: int = 0,
                        hl: str = "en", gl: str = "us",
                        engine: str = "auto") -> dict:
    """Scrape Google Books Search (tbm=bks): title, URL, snippet.

    engine: auto | http | proxy | browser (lihat google_web_search).
    """
    num = max(1, min(num, 50))
    start = max(0, start)
    render_params = {"q": query, "tbm": "bks", "num": num,
                     "hl": hl, "gl": gl}
    params = {"q": query, "tbm": "bks", "num": num, "start": start,
              "hl": hl, "gl": gl}
    return _run_search(SEARCH_BASE, params, render_params, hl, query, engine,
                       parse_web, parse_serp_live, limit=num)


@mcp.tool()
def google_shopping_search(query: str, num: int = 10, start: int = 0,
                           hl: str = "en", gl: str = "us",
                           engine: str = "auto") -> dict:
    """Scrape Google Shopping (tbm=shop): product title, URL, snippet.

    engine: auto | http | proxy | browser (lihat google_web_search).
    """
    num = max(1, min(num, 50))
    start = max(0, start)
    render_params = {"q": query, "tbm": "shop", "num": num,
                     "hl": hl, "gl": gl}
    params = {"q": query, "tbm": "shop", "num": num, "start": start,
              "hl": hl, "gl": gl}
    return _run_search(SEARCH_BASE, params, render_params, hl, query, engine,
                       parse_web, parse_shopping, limit=num,
                       expect=("encrypted-tbn",))


# ---------------------------------------------------------------- news ---
@mcp.tool()
def google_news_search(query: str, num: int = 20, hl: str = "en-US",
                       gl: str = "US", ceid: str = "US:en",
                       engine: str = "auto") -> dict:
    """Scrape Google News: RSS dulu (no block); fallback tab News renderan
    bila RSS gagal.

    engine: auto | http (RSS saja) | proxy | browser (tab News via Camoufox).
    """
    num = max(1, num)
    if engine == "browser":
        out = _render_fallback(
            SEARCH_BASE, {"q": query, "tbm": "nws", "hl": "en", "gl": "us"},
            "en", parse_news_live, query, expect=("n0jPhd", "<h3"))
        if out.get("status") == "ok":
            out["results"] = out["results"][:num]
        return out
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + f"&hl={hl}&gl={gl}&ceid={ceid}")
    r = fetch(url, referer="https://news.google.com/",
              proxy_mode_override=_proxy_mode_for(engine))
    out = {"status": "ok", "query": query, "http_status": r.status_code,
           "engine": "http"}
    if getattr(r, "from_cache", False):
        out["engine"] = "cache"
        out["cached"] = True
    try:
        out["results"] = parse_news_rss(r.text)[:num]
    except Exception as e:
        if engine in ("http", "proxy"):
            out.update({"status": "error", "error": f"RSS parse failed: {e}",
                        "results": []})
            return out
        out = _render_fallback(
            SEARCH_BASE, {"q": query, "tbm": "nws", "hl": "en", "gl": "us"},
            "en", parse_news_live, query, expect=("n0jPhd", "<h3"))
        if out.get("status") == "ok":
            out["results"] = out["results"][:num]
    return out


@mcp.tool()
def google_news_homepage(hl: str = "en-US", gl: str = "US",
                         ceid: str = "US:en") -> dict:
    """Scrape Google News homepage RSS: top headlines right now."""
    url = f"https://news.google.com/rss?hl={hl}&gl={gl}&ceid={ceid}"
    r = fetch(url, referer="https://news.google.com/")
    out = {"status": "ok", "http_status": r.status_code}
    try:
        out["results"] = parse_news_rss(r.text)
    except Exception as e:
        out.update({"status": "error", "error": f"RSS parse failed: {e}", "results": []})
    return out


# -------------------------------------------------------------- scholar --
@mcp.tool()
def google_scholar_search(query: str, num: int = 10, start: int = 0,
                          hl: str = "en", year_low: int = 0,
                          year_high: int = 0, engine: str = "auto") -> dict:
    """Scrape Google Scholar: papers, authors/venue, citations, PDF links.

    start: 0-based result offset (unified google_search computes it from page).
    engine: auto | http | proxy | browser (lihat google_web_search).
    """
    num = max(1, min(num, 20))
    start = max(0, start)
    render_params = {"q": query, "hl": hl, "num": num, "start": start}
    params = {"q": query, "hl": hl, "num": num, "start": start}
    if year_low:
        params["as_ylo"] = year_low
        render_params["as_ylo"] = year_low
    if year_high:
        params["as_yhi"] = year_high
        render_params["as_yhi"] = year_high
    return _run_search(SCHOLAR_BASE, params, render_params, hl, query, engine,
                       parse_scholar, parse_scholar, expect=("gs_rt",))


@mcp.tool()
def google_scholar_cited_by(cluster_id: str, num: int = 10,
                            start: int = 0, hl: str = "en",
                            engine: str = "auto") -> dict:
    """Scrape papers citing a Scholar cluster (from cluster_id in scholar results).

    engine: auto | http | proxy | browser (lihat google_web_search).
    """
    params = {"cites": cluster_id, "hl": hl, "as_sdt": "0,5",
              "sciodt": "0,5", "num": max(1, min(num, 20)), "start": max(0, start)}
    return _run_search(SCHOLAR_BASE, params, params, hl, cluster_id, engine,
                       parse_scholar, parse_scholar, label="cluster_id",
                       expect=("gs_rt",))


# -------------------------------------------------------------- patents --
@mcp.tool()
def google_patents_search(query: str, num: int = 10, page: int = 1) -> dict:
    """Scrape Google Patents XHR JSON: title, number, inventors, dates, PDF.

    page: 1-based (1 = first page).
    NOTE: page number lives INSIDE the url= query string (page=N), not as
    an outer param.
    """
    page = max(1, page) - 1  # 1-based -> 0-based for the API
    inner = f"q={urllib.parse.quote(query)}&dups=language"
    if page:
        inner += f"&page={page}"
    url = ("https://patents.google.com/xhr/query?url="
           + urllib.parse.quote(inner, safe="") + "&exp=")
    r = fetch(url, referer="https://patents.google.com/", ajax=True)
    out = {"status": "ok", "query": query, "http_status": r.status_code}
    try:
        parsed = parse_patents(json.loads(r.text))
        parsed["results"] = parsed["results"][: max(1, num)]
        out.update(parsed)
    except Exception as e:
        out.update({"status": "error", "error": f"Patents parse failed: {e}", "results": []})
    return out


# -------------------------------------------------------------- finance --
def _normalize_finance_symbol(ticker: str, exchange: str = "") -> str:
    """Terima USDIDR, USD/IDR, usd-idr, BBCA + IDX, dll -> simbol kanonik.

    Google Finance memakai 'USD-IDR' untuk forex dan 'BBCA:IDX' untuk saham;
    'USDIDR' polos adalah kesalahan paling umum dan sebelumnya bikin error.
    """
    t = (ticker or "").strip().replace(" ", "").replace("/", "-")
    if exchange:
        return t if ":" in t else f"{t}:{exchange.strip()}"
    if ":" in t:
        return t
    up = t.upper()
    if len(up) == 6 and up.isalpha():
        return f"{up[:3]}-{up[3:]}"
    return up


@mcp.tool()
def google_finance_quote(ticker: str, exchange: str = "",
                         hl: str = "en") -> dict:
    """Scrape Google Finance quote: saham, forex, kripto.

    Ticker: 'BBCA' + exchange 'IDX', 'AAPL' + 'NASDAQ', atau pair forex
    'USD-IDR' (USDIDR/USD/IDR juga diterima, dinormalisasi otomatis).
    """
    symbol = _normalize_finance_symbol(ticker, exchange)
    url = f"https://www.google.com/finance/quote/{urllib.parse.quote(symbol)}?hl={hl}"
    r = fetch(url)
    out = {"status": "ok", "symbol": symbol, "http_status": r.status_code,
           "url": str(r.url)}
    if "captcha" in r.text.lower()[:2000] and len(r.text) < 20000:
        out.update({"status": "blocked", "error": BLOCKED_MSG})
        return out
    quote = parse_finance_quote(r.text)
    if quote:
        out["quote"] = quote
        return out

    # Fallback otomatis: layout finance berubah -> jangan langsung menyerah.
    pair = re.fullmatch(r"([A-Z]{3})-([A-Z]{3})", symbol)
    if pair:
        fb = google_fx_rate(pair.group(1), pair.group(2), hl=hl)
        fx = fb.get("fx_rate")
        if fb.get("status") == "ok" and fx:
            out.update({"status": "ok", "fallback": "serp_fx_widget",
                        "note": "Halaman finance tidak bisa diparse; kurs "
                                "diambil dari widget konverter SERP.",
                        "quote": {"symbol": symbol,
                                  "pair": f"{pair.group(1)}/{pair.group(2)}",
                                  "price": fx.get("rate"),
                                  "source": "serp_fx_widget",
                                  "formatted": fx.get("formatted")}})
            return out

    fb = google_web_search(f"{symbol} price", num=3)
    if fb.get("status") == "ok" and fb.get("results"):
        out.update({"status": "ok", "fallback": "web_search",
                    "note": "Finance page tidak bisa diparse; ini hasil "
                            "pencarian web, bukan quote terstruktur.",
                    "results": fb["results"]})
        return out

    out.update({"status": "error",
                "error": f"Quote data block not found untuk simbol "
                         f"'{symbol}', dan fallback (fx widget / web search) "
                         f"juga tidak menghasilkan. Pakai format 'USD-IDR' "
                         f"(forex) atau 'BBCA:IDX' (saham), atau "
                         f"google_kurs_bi untuk kurs resmi BI.",
                "symbol_used": symbol, "url": out.get("url")})
    return out


@mcp.tool()
def google_fx_rate(base: str = "USD", quote: str = "IDR", amount: float = 1.0,
                   hl: str = "en", gl: str = "us", engine: str = "auto") -> dict:
    """Kurs langsung dari widget konverter SERP (mis. "1 USD to IDR").

    Hasil: fx_rate {rate, from, to, formatted} + results biasa. Lebih tahan
    terhadap perubahan layout daripada halaman finance, karena membaea widget
    konverter yang muncul di SERP. Cocok untuk kurs cepat; untuk kurs resmi BI
    pakai google_kurs_bi.
    """
    try:
        amount_txt = f"{float(amount):g}"
    except (TypeError, ValueError):
        amount_txt = "1"
    query = f"{amount_txt} {base.strip().upper()} to {quote.strip().upper()}"

    def parse(html: str):
        parsed = parse_web(html)
        fx = parse_fx_widget(html)
        if fx:
            parsed["fx_rate"] = fx
        return parsed

    out = _run_search(SEARCH_BASE, {"q": query, "hl": hl, "gl": gl},
                      {"q": query, "hl": hl, "gl": gl}, hl, query, engine,
                      parse, parse, expect=("DFlfde", "<h3"))
    if out.get("status") == "ok" and not out.get("fx_rate"):
        # Fallback: halaman finance (forex pair) sebagai sumber kurs.
        b, q = base.strip().upper(), quote.strip().upper()
        if re.fullmatch(r"[A-Z]{3}", b) and re.fullmatch(r"[A-Z]{3}", q):
            fb = google_finance_quote(f"{b}-{q}", hl=hl)
            piece = (fb.get("quote") or {}).get("price")
            if fb.get("status") == "ok" and piece:
                out.update({"status": "ok", "fallback": "finance_quote",
                            "note": "Widget SERP kosong; kurs dari halaman "
                                    "Google Finance.",
                            "fx_rate": {"rate": piece, "from": b, "to": q}})
                return out
        out["status"] = "empty"
        out["error"] = ("Widget konverter tidak muncul di SERP dan fallback "
                        "finance juga kosong; coba google_kurs_bi untuk kurs "
                        "resmi BI.")
    return out


BI_KURS_URL = ("https://www.bi.go.id/id/statistik/informasi-kurs/"
               "transaksi-bi/default.aspx")


@mcp.tool()
def google_kurs_bi(currency: str = "", engine: str = "auto") -> dict:
    """Kurs Transaksi Bank Indonesia (Jual/Beli) dari tabel resmi BI.

    currency opsional: 'USD', 'EUR', dst (kosong = semua). Halaman BI
    JS-heavy, jadi engine auto memakai browser; engine 'http' dicoba dulu
    sebagai jalur murah.
    """
    want = (currency or "").strip().upper()

    def parse(html: str):
        parsed = parse_kurs_bi(html)
        if want:
            parsed["rates"] = [r for r in parsed["rates"]
                               if r["currency"] == want]
        return parsed

    out = {"source": "Bank Indonesia (Kurs Transaksi)"}
    if engine == "http":
        r = fetch(BI_KURS_URL)
        parsed = parse(r.text)
        out.update(parsed)
        out["engine"] = "http"
        out["http_status"] = r.status_code
    else:
        rendered = _render_fallback(BI_KURS_URL, {}, "id", parse,
                                    "kurs-bi", expect=("Kurs Jual", "USD"))
        out.update(rendered)
        if not out.get("rates"):
            # Fallback: coba jalur HTTP polos sebelum menyerah.
            try:
                r = fetch(BI_KURS_URL)
                http_parsed = parse(r.text)
                if http_parsed.get("rates"):
                    out.update(http_parsed)
                    out["engine"] = "http"
                    out["fallback"] = "http"
            except Exception:
                pass
    if not out.get("rates"):
        out["status"] = out.get("status", "ok")
        if out.get("status") == "ok":
            out["status"] = "empty"
            out["error"] = ("Tabel kurs BI tidak ditemukan"
                            + (f" untuk {want}" if want else "")
                            + ". Halaman BI kadang berubah struktur.")
        return out
    out["status"] = "ok"
    return out


# ------------------------------------------------------------ translate --
@mcp.tool()
def google_translate(text: str, target: str = "en",
                     source: str = "auto") -> dict:
    """Translate text via Google's unofficial gtx endpoint (no key)."""
    params = {"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": text}
    r = fetch("https://translate.googleapis.com/translate_a/single",
              params=params, referer="https://translate.google.com/")
    out = {"status": "ok", "source": source, "target": target,
           "http_status": r.status_code}
    try:
        data = json.loads(r.text)
        out["detected_source"] = data[2] if len(data) > 2 else source
        out["translation"] = "".join(seg[0] for seg in data[0] if seg and seg[0])
    except Exception as e:
        out.update({"status": "error", "error": f"Translate parse failed: {e}"})
    return out


# -------------------------------------------------------------- suggest --
@mcp.tool()
def google_suggest(query: str, hl: str = "en") -> dict:
    """Google autocomplete suggestions (no block)."""
    params = {"client": "firefox", "q": query, "hl": hl}
    r = fetch("https://suggestqueries.google.com/complete/search",
              params=params, referer="https://www.google.com/")
    out = {"status": "ok", "query": query, "http_status": r.status_code}
    try:
        data = json.loads(r.text)
        out["suggestions"] = data[1] if len(data) > 1 else []
    except Exception as e:
        out.update({"status": "error", "error": f"Suggest parse failed: {e}",
                    "suggestions": []})
    return out


# --------------------------------------------------------------- trends --
@mcp.tool()
def google_trends_daily(geo: str = "US", hl: str = "en-US") -> dict:
    """Google daily trending searches RSS (no block): topic, traffic, links."""
    url = f"https://trends.google.com/trending/rss?geo={urllib.parse.quote(geo)}"
    r = fetch(url, referer="https://trends.google.com/")
    out = {"status": "ok", "geo": geo, "http_status": r.status_code}
    try:
        out["results"] = parse_trends_rss(r.text)
    except Exception as e:
        out.update({"status": "error", "error": f"Trends parse failed: {e}",
                    "results": []})
    return out


def _series_from_trends_data(data: dict, kws: list[str]) -> tuple[list, dict]:
    series = []
    for line in data.get("default", {}).get("timelineData", []):
        entry = {"time": line.get("formattedTime")}
        for i, v in enumerate(line.get("value", [])):
            entry[kws[i] if i < len(kws) else f"kw{i}"] = v
        series.append(entry)
    averages = dict(zip(kws, data.get("default", {}).get("averages", [])))
    return series, averages


def _trends_browser(kws: list[str], geo: str, timeframe: str, hl: str,
                    tz: int) -> dict:
    """Fallback browser: buka halaman explore, tangkap response widgetdata."""
    from .browser import fetch_trends_series
    pool = get_pool()
    attempts: list = [None]
    if pool.available():
        proxy = pool.acquire()
        if proxy:
            attempts.append(proxy)
    last_err = "tidak ada percobaan"
    for proxy in attempts:
        res = fetch_trends_series(kws, geo=geo, timeframe=timeframe, hl=hl,
                                  tz=tz,
                                  proxy=proxy.playwright() if proxy else None)
        if res.get("error"):
            last_err = res["error"]
            if proxy:
                pool.report_failure(proxy)
            continue
        if proxy:
            pool.report_success(proxy)
        text = res.get("text") or ""
        try:
            data = json.loads(text[5:] if text.startswith(")]}'") else text)
        except Exception as e:
            last_err = f"parse: {e}"
            continue
        series, averages = _series_from_trends_data(data, kws)
        out = {"status": "ok", "engine": "browser", "keywords": kws,
               "averages": averages, "timeline": series}
        if proxy:
            out["proxy"] = _proxy_label(proxy)
        return out
    return {"status": "limited",
            "error": f"Trends ditolak HTTP & browser (bot-check). {last_err}. "
                     f"Pakai google_trends_daily (RSS) sebagai fallback.",
            "results": []}


@mcp.tool()
def google_trends_interest(keywords: str, geo: str = "", timeframe: str = "today 12-m",
                           hl: str = "en-US", tz: int = 0,
                           engine: str = "auto") -> dict:
    """Google Trends interest-over-time via internal explore API (no key).

    keywords: comma-separated, max 5 (e.g. "opencode, cursor, windsurf").
    timeframe: e.g. 'now 7-d', 'today 12-m', 'today 5-y', 'all'.
    engine: auto (HTTP → fallback browser) | http (HTTP saja) | browser.

    NOTE: endpoint widgetdata Trends sering menolak request non-browser
    (HTTP 400/401) walau token explore valid — bila itu terjadi, engine
    auto memakai Camoufox: fetch dijalankan dari dalam halaman Trends
    sehingga cookies/fingerprint ikut. Bila tetap gagal, status "limited".
    """
    engine = (engine or "auto").lower()
    kws = [k.strip() for k in keywords.split(",") if k.strip()][:5]
    if not kws:
        return {"status": "error", "error": "Provide at least one keyword."}
    geo_obj = "" if not geo else geo
    req = {"comparisonItem": [{"keyword": k, "geo": geo_obj, "time": timeframe} for k in kws],
           "category": 0, "property": ""}
    explore_url = "https://trends.google.com/trends/api/explore"
    full_explore_url = (explore_url + "?hl=" + hl + "&tz=" + str(tz)
                        + "&req=" + urllib.parse.quote(json.dumps(req)))

    if engine == "browser":
        return _trends_browser(kws, geo, timeframe, hl, tz)

    r = fetch(full_explore_url, referer="https://trends.google.com/",
              proxy_mode_override=_proxy_mode_for(engine))
    if r.status_code == 429 and engine == "http":
        return {"status": "rate_limited",
                "error": "Google Trends menolak request (HTTP 429, rate-limit sementara). "
                         "Tunggu 1-5 menit lalu coba lagi.",
                "results": []}
    if r.status_code != 200 or not (r.text.startswith(")]}'")
                                    or r.text.lstrip().startswith("{")):
        if engine == "http":
            return {"status": "error",
                    "error": f"Explore API HTTP {r.status_code}", "results": []}
        return _trends_browser(kws, geo, timeframe, hl, tz)
    try:
        payload = json.loads(r.text[5:] if r.text.startswith(")]}'") else r.text)
        widgets = {w["id"]: w for w in payload.get("widgets", [])}
        ts = widgets.get("TIMESERIES")
        if not ts:
            return {"status": "error", "error": "No TIMESERIES widget returned.",
                    "results": []}
        rq = ts["request"]
        token = ts.get("token", "")
        r2 = fetch("https://trends.google.com/trends/api/widgetdata/multiline",
                   params={"hl": hl, "tz": str(tz), "token": token,
                           "req": json.dumps({"time": rq["time"], "resolution": rq.get("resolution", "WEEK"),
                                              "locale": hl, "comparisonItem": rq["comparisonItem"],
                                              "requestOptions": {"property": "", "backend": "IZG",
                                                                 "category": 0}})},
                   referer="https://trends.google.com/")
        if r2.status_code != 200 or not (r2.text.startswith(")]}'")
                                         or r2.text.lstrip().startswith("{")):
            if engine == "http":
                return {"status": "limited",
                        "error": f"Google Trends widgetdata menolak request non-browser "
                                 f"(HTTP {r2.status_code}). Pakai google_trends_daily (RSS) "
                                 f"sebagai fallback.",
                        "results": []}
            return _trends_browser(kws, geo, timeframe, hl, tz)
        try:
            data = json.loads(r2.text[5:] if r2.text.startswith(")]}'") else r2.text)
        except Exception:
            if engine == "http":
                return {"status": "limited",
                        "error": "Google Trends widgetdata mengembalikan non-JSON "
                                 "(proteksi bot). Pakai google_trends_daily (RSS) "
                                 "sebagai fallback.",
                        "results": []}
            return _trends_browser(kws, geo, timeframe, hl, tz)
        series, averages = _series_from_trends_data(data, kws)
        return {"status": "ok", "keywords": kws, "averages": averages,
                "timeline": series}
    except Exception as e:
        if engine == "http":
            return {"status": "error", "error": f"Trends parse failed: {e}",
                    "results": []}
        return _trends_browser(kws, geo, timeframe, hl, tz)


# -------------------------------------------------------------- ai mode --
@mcp.tool()
def google_ai_mode(query: str, hl: str = "en", gl: str = "us",
                   engine: str = "auto") -> dict:
    """Google Mode AI (udm=50): jawaban sintesis + sumber, cocok untuk
    pertanyaan langsung. Bentuk hasil: answer + sources.

    engine: auto | http | proxy | browser (lihat google_web_search).
    Halaman Mode AI hampir selalu butuh JS; engine auto akan memakai browser.
    """
    params = {"q": query, "udm": "50", "hl": hl, "gl": gl}
    # Jalur HTTP hanya memuat bubble pertanyaan (jawaban di-stream via JS),
    # jadi auto langsung memakai browser.
    engine_eff = "browser" if engine == "auto" else engine
    out = _run_search(SEARCH_BASE, dict(params), dict(params), hl, query,
                      engine_eff, parse_ai_mode, parse_ai_mode,
                      expect=("Balasan Mode AI", "Rty6Hf"))
    if out.get("status") == "ok" and not out.get("answer") and engine != "http":
        # kadang jawaban belum termuat saat render pertama — coba sekali lagi
        time.sleep(4)
        retry = _run_search(SEARCH_BASE, dict(params), dict(params), hl, query,
                            engine_eff, parse_ai_mode, parse_ai_mode,
                            expect=("Balasan Mode AI", "Rty6Hf"))
        retry["retried"] = True
        return retry
    return out


# -------------------------------------------------------------- unified --
TABS = ("web", "images", "videos", "news", "books", "shopping",
        "scholar", "patents", "ai")


@mcp.tool()
def google_search(query: str, tab: str = "web", page: int = 1,
                  num: int = 10, hl: str = "en", gl: str = "us",
                  engine: str = "auto") -> dict:
    """One search tool for all Google tabs + pagination (pages 1-10).

    tab: web | images | videos | news | books | shopping | scholar | patents | ai.
    page: 1-10 (each page = next result set; news slices the RSS feed).
    num: results per page.
    engine: auto (HTTP cepat, fallback browser bila diblokir) | http
    (tanpa browser) | proxy (paksa pool proxy) | browser (langsung Camoufox).
    """
    tab = (tab or "web").lower().strip()
    if tab not in TABS:
        return {"status": "error",
                "error": f"Unknown tab '{tab}'. Choose one of: {', '.join(TABS)}.",
                "results": []}
    page = max(1, min(page, 10))
    num = max(1, min(num, 50))
    start = (page - 1) * num

    if tab == "web":
        out = google_web_search(query, num=num, start=start, hl=hl, gl=gl,
                                engine=engine)
    elif tab == "images":
        out = google_image_search(query, num=num, page=page, hl=hl, gl=gl,
                                  engine=engine)
    elif tab == "videos":
        out = google_video_search(query, num=num, start=start, hl=hl, gl=gl,
                                  engine=engine)
    elif tab == "books":
        out = google_books_search(query, num=num, start=start, hl=hl, gl=gl,
                                  engine=engine)
    elif tab == "shopping":
        out = google_shopping_search(query, num=num, start=start, hl=hl,
                                     gl=gl, engine=engine)
    elif tab == "scholar":
        out = google_scholar_search(query, num=num, start=start, hl=hl,
                                    engine=engine)
    elif tab == "patents":
        out = google_patents_search(query, num=num, page=page)
    elif tab == "ai":
        out = google_ai_mode(query, hl=hl, gl=gl, engine=engine)
    elif tab == "news":
        # RSS has no server-side paging: fetch page*num, slice client-side.
        out = google_news_search(query, num=page * num, hl="en-US",
                                 gl="US", ceid="US:en", engine=engine)
        if out.get("status") == "ok":
            out["results"] = out["results"][start:start + num]
    else:  # pragma: no cover - guarded above
        return {"status": "error", "error": f"Unknown tab '{tab}'.",
                "results": []}
    out["tab"] = tab
    out["page"] = page
    out["per_page"] = num
    return out


# ---------------------------------------------------------------- crawl --
@mcp.tool()
def google_crawl(url: str, max_chars: int = 8000,
                 include_links: bool = True) -> dict:
    """Crawl any URL (e.g. a search result): title, meta, text, outbound links."""
    r = fetch(url, referer="https://www.google.com/")
    from bs4 import BeautifulSoup
    ctype = r.headers.get("content-type", "") if hasattr(r, "headers") else ""
    out = {"status": "ok", "url": str(r.url), "http_status": r.status_code,
           "content_type": ctype}
    if "html" not in ctype and "<html" not in r.text[:2000].lower():
        out.update({"text": r.text[: max(500, max_chars)], "note": "Non-HTML content."})
        return out
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc = ""
    m = soup.find("meta", attrs={"name": "description"}) or \
        soup.find("meta", attrs={"property": "og:description"})
    if m and m.get("content"):
        desc = m["content"].strip()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)[: max(500, max_chars)]
    out.update({"title": title, "description": desc, "text": text})
    if include_links:
        links, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and href not in seen:
                seen.add(href)
                links.append({"text": a.get_text(" ", strip=True)[:120], "url": href})
                if len(links) >= 50:
                    break
        out["links"] = links
    return out


# ------------------------------------------------------------- help ----
_HELP = """google-scrape — quick guide for AI agents (full version: AGENT_GUIDE.md)

TOOL MAP
  google_search        unified 9 tabs: web/images/videos/news/books/shopping/
                       scholar/patents/ai (web/images/videos/books/shopping
                       usually take the HTTP+cookie fast path; scholar & ai
                       use the browser)
  google_web_search    organic results + related searches + featured snippet
  google_image_search  image_url, page_url, width/height
  google_video_search / google_books_search / google_shopping_search
  google_news_search, google_news_homepage   (RSS, most reliable)
  google_scholar_search (cluster_id, cited_by, pdf_url)
  google_scholar_cited_by (often blocked — report it, don't retry hard)
  google_patents_search (JSON)
  google_finance_quote  saham/forex/kripto: 'BBCA' + exchange IDX, 'USD-IDR'
                        (USDIDR / USD/IDR dinormalisasi otomatis)
  google_fx_rate         kurs cepat dari widget SERP: base, quote, amount
  google_kurs_bi         kurs resmi BI (Jual/Beli) dari tabel transaksi-bi
  google_translate, google_suggest, google_trends_daily, google_trends_interest
  google_ai_mode       AI Mode (udm=50) synthesized answer + sources
  google_crawl (read any URL)
  google_status        health + proxies + cache + cooldowns

ENGINES (all search tools)
  auto    = HTTP fast path with cookie bootstrap, browser fallback (default)
  http    = direct only (challenge-prone on flagged IPs)
  proxy   = force the proxy pool
  browser = Camoufox headless render (~5-15s)

RESPONSE CONTRACT
  status: ok | blocked | rate_limited | limited | empty | error
  useful keys: engine (http|cache|browser), cached:true, note (auto decisions)
  blocked/rate_limited = Google refuses this session/IP -> do not loop; switch
  to RSS tools (e.g. google_news_search) or wait a few minutes.
  limited (trends) -> fall back to google_trends_daily.
  bootstrap cookies live 5 minutes; repeated identical queries may be cached.

USAGE PATTERNS
  1. Cheap & stable first: suggest -> news RSS -> patents -> trends daily.
  2. Research: google_search(tab=web) -> google_crawl on relevant hits.
  3. Pagination: page 1-10 / num per batch / scholar start 0-based.
  4. Do not hammer; when blocked, stop and retry later.
  5. Shopping: price/was_price/merchant/rating (product URL renders on click).
  6. Trends limited -> google_trends_daily. cited_by blocked -> use the
     cited_by count from google_scholar_search.
"""


@mcp.tool()
def google_help() -> dict:
    """Agent-facing usage guide: tool map, engine semantics, status contract
    and safe usage patterns. Read this before experimenting."""
    return {"status": "ok", "guide": _HELP}


@mcp.tool()
def google_status() -> dict:
    """Health check: which Google endpoints are reachable from this IP right
    now, plus proxy pool, cache, cooldowns and forensic counters."""
    checks = {
        "web_search_page": SEARCH_BASE + "?q=test&hl=en",
        "suggest": "https://suggestqueries.google.com/complete/search?client=firefox&q=test",
        "news_rss": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
        "scholar": "https://scholar.google.com/scholar?q=test&hl=en",
        "patents": "https://patents.google.com/xhr/query?url=q%3Dtest%26dups%3Dlanguage&exp=",
        "trends_rss": "https://trends.google.com/trending/rss?geo=US",
        "translate": "https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=id&dt=t&q=test",
        "finance": "https://www.google.com/finance/quote/GOOGL:NASDAQ?hl=en",
    }
    result = {}
    for name, url in checks.items():
        try:
            r = fetch(url, proxy_mode_override="off", fresh=True)
            blocked = is_blocked_page(r.text) if "search" in name or "finance" in name else False
            result[name] = {"http": r.status_code, "bytes": len(r.text),
                            "blocked": blocked}
        except Exception as e:
            result[name] = {"http": None, "error": str(e)[:200], "blocked": None}

    pool = get_pool()
    proxy_probe = {"status": "no_pool"}
    if pool.size():
        try:
            r = fetch("https://suggestqueries.google.com/complete/search"
                      "?client=firefox&q=test", proxy_mode_override="always",
                      fresh=True, retries=1)
            proxy_probe = {"status": "ok", "http": r.status_code,
                           "bytes": len(r.text)}
        except Exception as e:
            proxy_probe = {"status": "error", "error": str(e)[:200]}

    return {"status": "ok", "endpoints": result,
            "proxy": {**proxy_stats(), "probe": proxy_probe},
            "cache": cache_stats(),
            "http_blocked": {"families": blocked_families()},
            "bootstrap": bootstrap_info(),
            "browser_cooldown": {"active": browser_blocked_active(),
                                 "reason": browser_blocked_reason(),
                                 "left_s": round(browser_blocked_left(), 1)},
            "forensics": {k: v for k, v in forensic_stats().items()
                          if k in ("blocks", "successes")},
            "forensics_recent": forensic_stats().get("recent", [])[-3:]}


def main() -> None:
    start_bootstrap_keeper()
    mcp.run()


if __name__ == "__main__":
    main()
