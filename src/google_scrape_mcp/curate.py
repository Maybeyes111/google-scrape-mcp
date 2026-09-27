"""Kurasi pool proxy: uji liveness + kemampuan `/search` (bukan cuma connect).

Skor per proxy:
  search_ok   — /search balas ok (proxy benar-benar berguna untuk HTTP)
  suggest_ok  — live, tapi /search tetap/js_challenge (connect jalan, search tidak)
  dead        — gagal connect/timeout

Output:
  ~/.cache/google-scrape-mcp/proxies_curated.txt  (hanya search_ok; fallback suggest_ok)
  ~/.cache/google-scrape-mcp/proxy_report.json    (semua hasil, untuk audit)

Contoh:
  python3 -m google_scrape_mcp.curate --limit 60 --workers 8
  python3 -m google_scrape_mcp.curate --files ~/my-proxies.txt --limit 100
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import requests as cr

from . import client
from .proxies import Proxy, load_proxies

SUGGEST = "https://suggestqueries.google.com/complete/search"
SEARCH = "https://www.google.com/search"
CURATED = Path.home() / ".cache" / "google-scrape-mcp" / "proxies_curated.txt"
REPORT = Path.home() / ".cache" / "google-scrape-mcp" / "proxy_report.json"
SEARCH_HEADERS = {
    "Referer": "https://www.google.com/",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
}


def check(proxy: Proxy, timeout: float) -> dict:
    out = {"proxy": proxy.key, "scheme": proxy.scheme, "live": False,
           "suggest": None, "search": None, "kind": None, "ms": 0}
    t0 = time.time()
    session = cr.Session(impersonate="chrome136", trust_env=False, proxy=proxy.url)
    try:
        r = session.get(SUGGEST, params={"client": "firefox",
                                         "q": "proxy check", "hl": "en"},
                        timeout=timeout,
                        headers={"Referer": "https://www.google.com/"})
        out["live"] = r.status_code == 200
        out["suggest"] = r.status_code
    except Exception as exc:
        out["ms"] = int((time.time() - t0) * 1000)
        out["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        return out
    try:
        r2 = session.get(SEARCH, params={"q": "proxy check", "hl": "en",
                                         "gl": "us"},
                         timeout=timeout, headers=SEARCH_HEADERS)
        kind = client.classify_html(r2.text, r2.status_code)
        out["search"] = r2.status_code
        out["kind"] = kind or "ok"
        out["bytes"] = len(r2.text)
        if kind:
            client.forensic_record(kind, "curate", client.endpoint_label(SEARCH),
                                   str(r2.url), r2.status_code)
    except Exception as exc:
        out["kind"] = "error"
        out["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Kurasi proxy google-scrape")
    ap.add_argument("--files", default="",
                    help="path file proxy, dipisah ':' (default: sumber pool)")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--json", default=str(REPORT))
    args = ap.parse_args()

    if args.files:
        os.environ["GOOGLE_SCRAPE_PROXY_FILES"] = args.files
        os.environ.pop("GOOGLE_SCRAPE_PROXIES", None)
    proxies = load_proxies()[:args.limit]
    print(f"menguji {len(proxies)} proxy (timeout {args.timeout}s, "
          f"{args.workers} worker)...", flush=True)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, p, args.timeout): p for p in proxies}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            mark = {"ok": "SEARCH-OK", "dead": "dead", None: "no-search"}.get(
                "ok" if res.get("kind") == "ok" else
                ("dead" if not res["live"] else None), res.get("kind"))
            print(f"  [{i:>3}/{len(proxies)}] {mark:9} {res['proxy'][:52]:52} "
                  f"{res['ms']:>5}ms", flush=True)

    search_ok = [r for r in results if r.get("kind") == "ok"]
    live = [r for r in results if r["live"] and r.get("kind") != "ok"]
    dead = [r for r in results if not r["live"]]

    chosen = search_ok or live
    CURATED.parent.mkdir(parents=True, exist_ok=True)
    lines = [r["proxy"] for r in chosen]
    CURATED.write_text("\n".join(lines) + ("\n" if lines else ""))
    Path(args.json).write_text(json.dumps(
        {"ts": time.time(), "tested": len(results), "search_ok": len(search_ok),
         "live_but_blocked": len(live), "dead": len(dead), "curated": len(chosen),
         "results": results}, indent=1))

    print(f"\nRINGKASAN: search_ok={len(search_ok)} live_tapi_blocked={len(live)} "
          f"dead={len(dead)} | {time.time()-t0:.0f}s")
    print(f"kurasi ditulis: {CURATED} ({len(chosen)} baris"
          f"{' — hanya suggest_ok, /search tetap diblokir' if not search_ok and chosen else ''})")
    print(f"laporan: {args.json}")
    if search_ok:
        print("saran: biarkan pool default membacanya (sudah masuk DEFAULT_FILES).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
