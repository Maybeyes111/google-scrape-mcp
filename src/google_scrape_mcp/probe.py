"""Harness riset blokir Google — ukur jenis blokir per endpoint/engine.

Contoh:
  python3 -m google_scrape_mcp.probe --quick
  python3 -m google_scrape_mcp.probe --full --json /tmp/probe.json
  python3 -m google_scrape_mcp.probe --only search_http --repeat 3 --spacing 20

Semua permintaan diberi jeda (default 6-10 dtk, acak) supaya tidak
memperparah rate-limit. Hasil: tabel ringkas + JSON lengkap.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests as cr

from . import browser as browser_mod
from . import client
from .parsers import parse_news_rss, parse_patents, parse_scholar, parse_serp_live

SEARCH = "https://www.google.com/search"
SUGGEST = "https://suggestqueries.google.com/complete/search"
PROFILE_COOKIES = Path.home() / ".cache" / "google-scrape-mcp" / "cfx-profile" / "cookies.sqlite"


def _profile_cookies() -> dict:
    """Baca cookies profil Firefox (copy dulu agar tidak mengunci DB)."""
    if not PROFILE_COOKIES.exists():
        return {}
    tmp = Path(tempfile.mkdtemp()) / "cookies.sqlite"
    try:
        shutil.copy2(PROFILE_COOKIES, tmp)
        con = sqlite3.connect(f"file:{tmp}?immutable=1", uri=True)
        rows = con.execute("select name, value, host from moz_cookies").fetchall()
        con.close()
        out = {}
        for name, value, host in rows:
            if host.endswith("google.com") or host.endswith("google.co.id"):
                out.setdefault(name, value)
        return out
    except Exception:
        return {}
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _probe_http(label: str, url: str, params: dict, cookies: dict | None = None,
                extra_headers: dict | None = None, method: str = "GET") -> dict:
    t0 = time.time()
    result = {"name": label, "engine": "http", "url": url}
    try:
        session = client._session_for(None)
        headers = {"Referer": "https://www.google.com/"}
        headers.update(extra_headers or {})
        with client._REQUEST_LOCK:
            client._throttle()
            resp = session.get(url, params=params, headers=headers, timeout=25,
                               cookies=cookies or None)
        kind = client.classify_html(resp.text, resp.status_code)
        result.update({"kind": kind or "ok", "http": resp.status_code,
                       "bytes": len(resp.text),
                       "ms": int((time.time() - t0) * 1000),
                       "final": str(resp.url)[:140]})
        if kind:
            client.forensic_record(kind, "probe-http", client.endpoint_label(url),
                                   str(resp.url), resp.status_code)
            client.save_sample(kind, "probe-http", client.endpoint_label(url),
                               str(resp.url), resp.status_code, resp.text)
    except Exception as exc:
        result.update({"kind": "error", "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                       "ms": int((time.time() - t0) * 1000)})
    return result


def _probe_browser(label: str, url: str, params: dict,
                   expect: tuple[str, ...]) -> dict:
    t0 = time.time()
    res = browser_mod.fetch_rendered(url, params=params, resolve_goto=False,
                                     expect=expect)
    if res.get("error"):
        return {"name": label, "engine": "browser", "kind": "error",
                "error": str(res["error"])[:140],
                "ms": int((time.time() - t0) * 1000)}
    kind = client.classify_html(res.get("html") or "", res.get("http_status"))
    if not kind:
        if res.get("challenge"):
            kind = "challenge"
        elif res.get("blocked"):
            kind = "blocked"
    return {"name": label, "engine": "browser", "kind": kind or "ok",
            "http": res.get("http_status"), "bytes": len(res.get("html") or ""),
            "js_rounds": res.get("js_rounds"),
            "ms": int((time.time() - t0) * 1000),
            "final": str(res.get("url") or "")[:140]}


def _probe_proxy_http(label: str, url: str, params: dict) -> dict:
    t0 = time.time()
    try:
        resp = client.fetch(url, params=params, proxy_mode_override="always",
                            fresh=True, retries=1)
        kind = client.classify_html(resp.text, resp.status_code)
        if kind:
            client.forensic_record(kind, "probe-proxy", client.endpoint_label(url),
                                   str(resp.url), resp.status_code)
        return {"name": label, "engine": "proxy-http", "kind": kind or "ok",
                "http": resp.status_code, "bytes": len(resp.text),
                "ms": int((time.time() - t0) * 1000),
                "final": str(resp.url)[:140]}
    except Exception as exc:
        return {"name": label, "engine": "proxy-http", "kind": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                "ms": int((time.time() - t0) * 1000)}


Q = "postgresql vacuum tuning"


def probes_quick() -> list:
    return [
        ("suggest_http", lambda: _probe_http(
            "suggest_http", SUGGEST, {"client": "firefox", "q": Q, "hl": "en"})),
        ("news_rss", lambda: _probe_http(
            "news_rss", "https://news.google.com/rss", {"hl": "en-US", "gl": "US", "ceid": "US:en"})),
        ("patents_json", lambda: _probe_http(
            "patents_json", "https://patents.google.com/xhr/query",
            {"url": "q=vacuum&dups=language", "exp": ""}, method="GET")),
        ("trends_daily", lambda: _probe_http(
            "trends_daily", "https://trends.google.com/trending/rss", {"geo": "US"})),
        ("finance_http", lambda: _probe_http(
            "finance_http", "https://www.google.com/finance/quote/USD-IDR", {})),
        ("search_http_minimal", lambda: _probe_http(
            "search_http_minimal", SEARCH, {"q": Q, "hl": "en", "gl": "us"})),
        ("search_http_source_hp", lambda: _probe_http(
            "search_http_source_hp", SEARCH,
            {"q": Q, "hl": "en", "gl": "us", "source": "hp"})),
        ("search_http_cookies", lambda: _probe_http(
            "search_http_cookies", SEARCH, {"q": Q, "hl": "en", "gl": "us"},
            cookies=_profile_cookies())),
        ("search_browser", lambda: _probe_browser(
            "search_browser", SEARCH, {"q": Q, "hl": "en", "gl": "us"}, ("<h3",))),
        ("search_browser_2", lambda: _probe_browser(
            "search_browser_2", SEARCH, {"q": Q + " 2", "hl": "en", "gl": "us"}, ("<h3",))),
        ("ai_mode_browser", lambda: _probe_browser(
            "ai_mode_browser", SEARCH, {"q": "apa itu vacuum", "udm": "50",
                                        "hl": "id", "gl": "id"},
            ("Balasan Mode AI", "Rty6Hf"))),
        ("scholar_browser", lambda: _probe_browser(
            "scholar_browser", "https://scholar.google.com/scholar",
            {"q": "postgresql vacuum", "hl": "en"}, ("gs_rt",))),
    ]


SEARCH_HEADERS = {
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}


def _probe_tls(target: str):
    """Uji apakah target impersonasi TLS tertentu mengubah hasil /search."""
    def run() -> dict:
        t0 = time.time()
        try:
            session = cr.Session(impersonate=target, trust_env=False)
            resp = session.get(SEARCH, params={"q": "tls target probe",
                                               "hl": "en", "gl": "us"},
                               timeout=25, headers=SEARCH_HEADERS)
            kind = client.classify_html(resp.text, resp.status_code)
            return {"name": f"tls_{target}", "engine": f"http/{target}",
                    "kind": kind or "ok", "http": resp.status_code,
                    "bytes": len(resp.text),
                    "ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            return {"name": f"tls_{target}", "engine": f"http/{target}",
                    "kind": "error", "error": str(exc)[:120],
                    "ms": int((time.time() - t0) * 1000)}
    return run


def _probe_replay() -> dict:
    """Ambil parameter ei/iflsig/mstk dari halaman asli lalu replay via HTTP
    dengan cookie profil — meniru percakapan halaman yang sah."""
    t0 = time.time()
    res = browser_mod.fetch_rendered(
        SEARCH, params={"q": "replay probe", "hl": "en", "gl": "us"},
        resolve_goto=False, expect=("<h3",))
    final = res.get("url") or ""
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(
        urllib.parse.urlsplit(final).query).items()}
    ei = qs.get("ei") or qs.get("sei") or ""
    iflsig, mstk = qs.get("iflsig", ""), qs.get("mstk", "")
    if res.get("error") or not final.startswith("http"):
        return {"name": "search_http_replay", "engine": "http-replay",
                "kind": "error",
                "error": f"browser gagal / url kosong ({str(res.get('error'))[:60]})",
                "ms": int((time.time() - t0) * 1000)}
    cookies = _profile_cookies()
    try:
        session = cr.Session(impersonate="chrome136", trust_env=False)
        resp = session.get(final, timeout=25, headers=SEARCH_HEADERS,
                           cookies=cookies or None)
        kind = client.classify_html(resp.text, resp.status_code)
        if kind:
            client.forensic_record(kind, "probe-replay",
                                   client.endpoint_label(SEARCH), str(resp.url),
                                   resp.status_code)
            client.save_sample(kind, "probe-replay",
                               client.endpoint_label(SEARCH), str(resp.url),
                               resp.status_code, resp.text)
        return {"name": "search_http_replay", "engine": "http-replay",
                "kind": kind or "ok", "http": resp.status_code,
                "bytes": len(resp.text),
                "ei": bool(ei), "iflsig": bool(iflsig), "mstk": bool(mstk),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as exc:
        return {"name": "search_http_replay", "engine": "http-replay",
                "kind": "error", "error": str(exc)[:120],
                "ms": int((time.time() - t0) * 1000)}


def probes_tls() -> list:
    return [(f"tls_{t}", _probe_tls(t)) for t in
            ("chrome120", "chrome142", "chrome150", "firefox147", "safari260")] + \
           [("search_http_replay", _probe_replay)]


def _family(endpoint: str) -> str:
    if "search" in endpoint:
        return "search"
    if "scholar" in endpoint:
        return "scholar"
    return endpoint


def analyze_decay() -> int:
    """Analisis offline: berapa lama blokir mereda (blokir -> ok berikutnya)."""
    from .forensics import STATS_FILE
    try:
        data = json.loads(STATS_FILE.read_text())
    except Exception:
        print("belum ada block_stats.json")
        return 1
    events = sorted(data.get("events", []), key=lambda e: e["ts"])
    rows = []
    for i, event in enumerate(events):
        if event.get("kind") == "ok":
            continue
        for nxt in events[i + 1:]:
            if (nxt.get("kind") == "ok"
                    and nxt.get("engine") == event.get("engine")
                    and _family(nxt.get("endpoint", "")) == _family(event.get("endpoint", ""))):
                rows.append((event.get("kind"), event.get("engine"),
                             _family(event.get("endpoint", "")),
                             round(nxt["ts"] - event["ts"], 1)))
                break
    if not rows:
        print(f"tidak ada pasangan blokir->ok di {len(events)} event "
              "(blokir HTTP /search memang tak pernah pulih di IP ini).")
        print("event terakhir:", json.dumps(events[-3:], indent=1)[:600])
        return 0
    print(f"{'kind':14} {'engine':8} {'family':8} {'pulih_dalam_s':>12}")
    for kind, engine, fam, delta in rows[-20:]:
        print(f"{kind:14} {engine:8} {fam:8} {delta:>12}")
    return 0


def probes_full() -> list:
    extra = [
        ("search_http_udm14", lambda: _probe_http(
            "search_http_udm14", SEARCH,
            {"q": Q, "hl": "en", "gl": "us", "udm": "14"})),
        ("search_http_legacy_params", lambda: _probe_http(
            "search_http_legacy_params", SEARCH,
            {"q": Q, "hl": "en", "gl": "us", "pws": 0, "filter": 0,
             "asearch": "arc"})),
        ("images_browser", lambda: _probe_browser(
            "images_browser", SEARCH, {"q": "mechanical keyboard", "tbm": "isch",
                                       "hl": "en", "gl": "us"},
            ("encrypted-tbn",))),
        ("shopping_browser", lambda: _probe_browser(
            "shopping_browser", SEARCH, {"q": "mechanical keyboard", "tbm": "shop",
                                         "hl": "en", "gl": "us"},
            ("encrypted-tbn",))),
        ("search_proxy_http", lambda: _probe_proxy_http(
            "search_proxy_http", SEARCH, {"q": Q, "hl": "en", "gl": "us"})),
    ]
    return probes_quick()[:-2] + extra + probes_quick()[-2:]


def main() -> int:
    ap = argparse.ArgumentParser(description="Riset blokir google-scrape")
    ap.add_argument("--quick", action="store_true", help="set probe ringkas (default)")
    ap.add_argument("--full", action="store_true", help="set probe lengkap")
    ap.add_argument("--tls", action="store_true",
                    help="eksperimen TLS target + replay ei/iflsig")
    ap.add_argument("--analyze", action="store_true",
                    help="analisis offline decay blokir dari block_stats.json")
    ap.add_argument("--only", default="", help="filter nama probe (substring)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--spacing", type=float, default=8.0,
                    help="jeda antar probe dtk (default 8, diacak 0.7-1.3x)")
    ap.add_argument("--json", default="", help="tulis hasil JSON ke path ini")
    args = ap.parse_args()

    if args.analyze:
        return analyze_decay()
    if args.tls:
        seq = probes_tls()
    elif args.full:
        seq = probes_full()
    else:
        seq = probes_quick()
    if args.only:
        seq = [p for p in seq if args.only in p[0]]
    results = []
    for rep in range(max(1, args.repeat)):
        for name, fn in seq:
            res = fn()
            res["repeat"] = rep
            results.append(res)
            kind = res.get("kind", "?")
            mark = "OK " if kind == "ok" else "!! "
            extra = res.get("error") or res.get("final", "")
            print(f"{mark}{name:26} {kind:14} http={res.get('http', '-')!s:5} "
                  f"{res.get('bytes', 0):>8}b {res.get('ms', 0):>6}ms {str(extra)[:60]}",
                  flush=True)
            if args.spacing > 0 and not (rep == args.repeat - 1 and (name, fn) == seq[-1]):
                time.sleep(args.spacing * random.uniform(0.7, 1.3))

    summary: dict = {}
    for r in results:
        summary[r.get("kind", "?")] = summary.get(r.get("kind", "?"), 0) + 1
    print("\nRINGKASAN:", json.dumps(summary), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps({"ts": time.time(),
                                               "results": results,
                                               "summary": summary}, indent=1))
        print("JSON:", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
