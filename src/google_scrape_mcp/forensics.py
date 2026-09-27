"""Forensik & statistik blokir Google + kebijakan cooldown adaptif.

Yang disimpan (semua lokal di ~/.cache/google-scrape-mcp/):
  forensics/<ts>-<kind>-<engine>.html   — sampel halaman blokir (rolling)
  forensics/<ts>-...json               — metadata: endpoint, params, status,
                                         headers terpilih, durasi profil
  block_stats.json                     — counter per (kind, engine) + event
                                         log terakhir (untuk google_status)
  http_state.json                      — state cooldown HTTP adaptif

Taksonomi blokir (lihat client._block_reason):
  js_challenge | captcha | sorry | http_429 | consent | empty
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

BASE = Path.home() / ".cache" / "google-scrape-mcp"
FORENSICS_DIR = BASE / "forensics"
STATS_FILE = BASE / "block_stats.json"
MAX_SAMPLES = 200
MAX_EVENTS = 500

_LOCK = threading.Lock()


def _enabled() -> bool:
    value = os.environ.get("GOOGLE_SCRAPE_FORENSICS", "1").strip().lower()
    return value not in ("0", "off", "false", "no")


def _load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text())
    except Exception:
        return {"blocks": {}, "successes": {}, "events": []}


def _save_stats(stats: dict) -> None:
    try:
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(stats, indent=1))
    except Exception:
        pass


def record(kind: str, engine: str, endpoint: str, url: str,
           status: int | None, note: str = "") -> None:
    """Catat satu event blokir/sukses ke statistik + event log."""
    with _LOCK:
        stats = _load_stats()
        bucket = "blocks" if kind != "ok" else "successes"
        key = f"{kind}|{engine}"
        stats.setdefault(bucket, {})
        stats[bucket][key] = stats[bucket].get(key, 0) + 1
        stats.setdefault("events", []).append({
            "ts": time.time(), "kind": kind, "engine": engine,
            "endpoint": endpoint, "url": url[:160], "http": status,
            "note": note[:120]})
        stats["events"] = stats["events"][-MAX_EVENTS:]
        _save_stats(stats)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-")[:60]


def save_sample(kind: str, engine: str, endpoint: str, url: str,
                status: int | None, html: str, meta: dict | None = None) -> str:
    """Simpan halaman blokir + metadata untuk analisis mendalam."""
    if not _enabled() or not html:
        return ""
    try:
        FORENSICS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = FORENSICS_DIR / f"{stamp}-{_slug(kind)}-{_slug(engine)}-{_slug(endpoint)}"
        base.with_suffix(".html").write_text(html[:2_000_000], encoding="utf-8",
                                             errors="replace")
        payload = {"kind": kind, "engine": engine, "endpoint": endpoint,
                   "url": url, "http": status, "ts": time.time(),
                   "bytes": len(html), "meta": meta or {}}
        base.with_suffix(".json").write_text(json.dumps(payload, indent=1))
        samples = sorted(FORENSICS_DIR.glob("*.html"))
        for old in samples[:-MAX_SAMPLES]:
            for ext in (".html", ".json"):
                try:
                    old.with_suffix(ext).unlink()
                except Exception:
                    pass
        return str(base)
    except Exception:
        return ""


def stats() -> dict:
    with _LOCK:
        stats = _load_stats()
    events = stats.get("events", [])
    return {"blocks": stats.get("blocks", {}),
            "successes": stats.get("successes", {}),
            "recent": [e for e in events[-12:]],
            "forensics_dir": str(FORENSICS_DIR)}
