"""HTTP client murni scrape: curl_cffi + impersonasi TLS Chrome, tanpa API key.

Kuat di kondisi tertentu:
  - pool proxy (rotasi otomatis + cooldown) saat IP langsung diblokir;
  - retry dengan backoff + hormati Retry-After pada 429/5xx;
  - cache TTL in-memory agar query berulang tidak memukul Google lagi;
  - cookie CONSENT disetel di awal agar tidak jatuh ke halaman consent;
  - satu lock request global: curl_cffi session tidak thread-safe.

Env tuning:
  GOOGLE_SCRAPE_PROXY_MODE     auto|off|always (default auto)
  GOOGLE_SCRAPE_MIN_INTERVAL   jeda antar-request detik (default 1.0)
  GOOGLE_SCRAPE_TIMEOUT        timeout request detik (default 25)
  GOOGLE_SCRAPE_RETRIES        jumlah percobaan via proxy (default 3)
  GOOGLE_SCRAPE_CACHE_TTL      TTL cache detik, 0 = matikan (default 600)
  GOOGLE_SCRAPE_CACHE_MAX      maks entri cache (default 256)
  GOOGLE_SCRAPE_IMPERSONATE    target impersonasi curl_cffi (default chrome120)
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import requests as cr

from .forensics import record as forensic_record, save_sample
from .proxies import Proxy, get_pool, proxy_mode

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
IMPERSONATE = os.environ.get("GOOGLE_SCRAPE_IMPERSONATE", "chrome120")
REFERER_DEFAULT = "https://www.google.com/"
CONSENT_COOKIE = "YES+cb"
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

# CA bundle untuk proxy HTTPS self-signed (mis. proxy lokal): di-set sebelum
# handle curl dibuat. Proxy publik dengan sertifikat valid tidak perlu ini.
_PROXY_CA = os.environ.get("GOOGLE_SCRAPE_PROXY_CA", "").strip()
if _PROXY_CA:
    os.environ.setdefault("CURL_CA_BUNDLE", _PROXY_CA)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MIN_INTERVAL = _env_float("GOOGLE_SCRAPE_MIN_INTERVAL", 1.0)
TIMEOUT = _env_int("GOOGLE_SCRAPE_TIMEOUT", 25)
RETRIES = max(1, _env_int("GOOGLE_SCRAPE_RETRIES", 3))
CACHE_TTL = _env_float("GOOGLE_SCRAPE_CACHE_TTL", 600.0)
CACHE_MAX = max(8, _env_int("GOOGLE_SCRAPE_CACHE_MAX", 256))
# Setelah raw HTTP kena blok/challenge, engine auto berhenti mencoba HTTP
# selama cooldown ini (browser yang menjalankan JS langsung dipakai).
HTTP_BLOCKED_TTL = _env_float("GOOGLE_SCRAPE_JS_COOLDOWN", 600.0)
STATE_FILE = Path(os.path.expanduser("~/.cache/google-scrape-mcp/http_state.json"))

_REQUEST_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
_SESSIONS: dict[str, cr.Session] = {}
_last_ts = 0.0
# Cooldown per-family endpoint: {family: {"until","reason","level","ts"}}
_HTTP_BLOCKED: dict = {}
_BLOCK_LEVEL_DECAY = 3600.0


@dataclass
class _CachedResponse:
    text: str
    status_code: int
    url: str
    headers: dict
    from_cache: bool = True


class _Cache:
    def __init__(self, ttl: float, maxsize: int):
        self._ttl = ttl
        self._max = maxsize
        self._data: OrderedDict[str, tuple[float, _CachedResponse]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> _CachedResponse | None:
        if self._ttl <= 0:
            return None
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            ts, resp = entry
            if time.time() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return resp

    def put(self, key: str, resp) -> None:
        if self._ttl <= 0 or resp.status_code != 200:
            return
        cached = _CachedResponse(text=resp.text, status_code=resp.status_code,
                                 url=str(resp.url),
                                 headers={k.lower(): v for k, v in resp.headers.items()})
        with self._lock:
            self._data[key] = (time.time(), cached)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "ttl_s": self._ttl,
                    "max": self._max}


_CACHE = _Cache(CACHE_TTL, CACHE_MAX)


def cache_stats() -> dict:
    return _CACHE.stats()


def proxy_stats() -> dict:
    return get_pool().stats()


def _cache_key(url: str, params: dict | None, ajax: bool) -> str:
    if not params:
        return f"{url}|{'ajax' if ajax else ''}"
    items = sorted((k, str(v)) for k, v in params.items())
    return f"{url}?{urllib.parse.urlencode(items)}|{'ajax' if ajax else ''}"


def _session_for(proxy: Proxy | None) -> cr.Session:
    key = proxy.key if proxy else ""
    with _SESSION_LOCK:
        session = _SESSIONS.get(key)
        if session is not None:
            return session
        kwargs = {"impersonate": IMPERSONATE, "trust_env": False}
        if proxy:
            kwargs["proxy"] = proxy.url
        s = cr.Session(**kwargs)
        s.headers.update(
            {
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
        )
        try:
            s.cookies.set("CONSENT", CONSENT_COOKIE, domain=".google.com")
        except Exception:
            pass
        try:
            s.get("https://www.google.com/", timeout=min(TIMEOUT, 15))
        except Exception:
            pass
        _SESSIONS[key] = s
        return s


def _session_singleton() -> cr.Session:
    """Sesi direct (tanpa proxy) — dipertahankan untuk pemakai lama (parsers)."""
    return _session_for(None)


def _clean_session() -> cr.Session:
    """Sesi tanpa warm-up/cookie lama. Dipakai saat request membawa cookies
    bootstrap: cookie sesi lama yang bentrok membuat Google menolak
    (terbukti eksperimen: sesi warm-up + cookie segar = js_challenge)."""
    with _SESSION_LOCK:
        session = _SESSIONS.get("__clean__")
        if session is None:
            session = cr.Session(impersonate=IMPERSONATE, trust_env=False)
            session.headers.update(
                {
                    "User-Agent": BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                }
            )
            _SESSIONS["__clean__"] = session
        return session


def _throttle() -> None:
    global _last_ts
    wait = MIN_INTERVAL - (time.time() - _last_ts)
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.6))
    _last_ts = time.time()


def _backoff(attempt: int, retry_after: float | None = None) -> None:
    if retry_after is not None:
        time.sleep(min(retry_after, 10.0))
        return
    time.sleep(min(0.4 * (2 ** attempt), 5.0) + random.uniform(0, 0.4))


def _retry_after(resp) -> float | None:
    try:
        raw = resp.headers.get("retry-after")
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _do_request(proxy: Proxy | None, url: str, params: dict | None,
                timeout: int, referer: str, ajax: bool,
                cookies: dict | None = None):
    if cookies and proxy is None:
        session = _clean_session()
        try:  # buang cookie google lama agar tidak bentrok dengan bootstrap
            session.cookies.clear(domain=".google.com")
        except Exception:
            pass
    else:
        session = _session_for(proxy)
    headers = {"Referer": referer}
    # Koherensi metadata navigasi: dari google.com ke google.com adalah
    # same-origin (temuan riset), bukan "none" seperti kunjungan langsung.
    try:
        from_host = urllib.parse.urlsplit(referer).netloc.lower()
        to_host = urllib.parse.urlsplit(url).netloc.lower()
        if from_host and to_host and (from_host.endswith(".google.com") or from_host == "google.com") \
                and (to_host.endswith(".google.com") or to_host == "google.com"):
            headers["Sec-Fetch-Site"] = "same-origin"
    except Exception:
        pass
    if ajax:
        headers.update({"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"})
    kwargs = {"timeout": timeout, "headers": headers}
    if proxy:
        kwargs["proxy"] = proxy.url
    if cookies:
        kwargs["cookies"] = cookies
    with _REQUEST_LOCK:
        _throttle()
        return session.get(url, params=params, **kwargs)


def fetch(url, params=None, timeout: int = TIMEOUT,
          referer: str = REFERER_DEFAULT, ajax: bool = False,
          proxy_mode_override: str | None = None,
          fresh: bool = False, retries: int | None = None,
          cookies: dict | None = None):
    """GET dengan throttle, cache TTL, retry/backoff, dan rotasi proxy.

    Mode proxy: auto (direct dulu, lanjut proxy bila gagal/blok),
    off (direct saja), always (mulai dari proxy; fallback direct bila
    pool kosong). Respons blok terakhir tetap dikembalikan apa adanya
    supaya pemanggil bisa memutuskan fallback browser.
    """
    mode = (proxy_mode_override or proxy_mode()).lower()
    key = _cache_key(url, params, ajax)
    if not fresh:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    pool = get_pool()
    attempts: list[Proxy | None] = []
    if mode != "always":
        attempts.append(None)
    if mode != "off" and pool.size():
        for _ in range(RETRIES if retries is None else max(1, retries)):
            candidate = pool.acquire()
            if candidate is not None:
                attempts.append(candidate)
    if mode == "always":
        attempts.append(None)  # proxy gagal semua → last-ditch direct
    if not attempts:
        attempts = [None]

    last_resp = None
    last_err: Exception | None = None
    for idx, proxy in enumerate(attempts):
        try:
            resp = _do_request(proxy, url, params, timeout, referer, ajax,
                               cookies=cookies if proxy is None else None)
        except Exception as exc:  # network / TLS / proxy connect failure
            last_err = exc
            if proxy:
                pool.report_failure(proxy)
            if idx + 1 < len(attempts):
                _backoff(idx)
            continue

        if is_blocked_page(resp.text) or resp.status_code in RETRY_STATUS:
            reason = _block_reason(resp) or "blocked"
            engine = "proxy" if proxy else "http"
            forensic_record(reason, engine, endpoint_label(url), str(resp.url),
                            resp.status_code)
            save_sample(reason, engine, endpoint_label(url), str(resp.url),
                        resp.status_code, resp.text)
            if proxy is None:
                note_http_blocked(reason, endpoint_family(url))
            last_resp = resp
            if proxy:
                pool.report_failure(proxy)
            if idx + 1 < len(attempts):
                _backoff(idx, _retry_after(resp))
            continue

        if proxy:
            pool.report_success(proxy)
        else:
            note_http_ok(endpoint_family(url))
        forensic_record("ok", "http", endpoint_label(url), str(resp.url),
                        resp.status_code)
        _CACHE.put(key, resp)
        return resp

    if last_resp is not None:
        return last_resp
    raise last_err if last_err else RuntimeError("request failed")


def endpoint_label(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return (parts.netloc + parts.path)[:80]


def classify_html(html: str, status: int | None = None) -> str:
    """Taksonomi blokir dari isi/HTTP status: js_challenge, captcha, sorry,
    http_429, consent, atau '' (bukan blokir).

    PENTING: jangan pakai substring mentah seperti "/sorry/index" — string itu
    muncul di bundle JS SERP normal (Google's anti-bot JS). Pakai marker
    struktural: form action="/sorry...", id="captcha-form", atau kalimat
    "unusual traffic" (yang hanya ada di halaman sorry).
    """
    if not html:
        return ""
    if is_js_challenge(html):
        return "js_challenge"
    low = html.lower()
    if ('id="captcha-form"' in html or 'action="/sorry' in html
            or "our systems have detected unusual traffic" in low):
        return "captcha"
    if "consent.google.com" in low and "<h3" not in html:
        return "consent"
    if status in RETRY_STATUS:
        return f"http_{status}"
    return ""


def _block_reason(resp) -> str:
    return classify_html(resp.text, resp.status_code)


def head_location(url: str, timeout: int = 15, light: bool = False) -> str | None:
    """Resolve 302 murah via HEAD (dipakai untuk /goto Google).

    Bila cookie bootstrap tersedia, pakai sesi bersih + cookie itu — token
    /goto terikat sesi yang menghasilkan SERP-nya. light=True melewati
    throttle global (dipakai untuk beberapa /goto sekaligus)."""
    cookies = bootstrap_cookies() if bootstrap_valid() else None
    if cookies:
        session = _clean_session()
        try:
            session.cookies.clear(domain=".google.com")
        except Exception:
            pass
    else:
        session = _session_singleton()
    with _REQUEST_LOCK:
        if not light:
            _throttle()
        resp = session.head(url, timeout=timeout, allow_redirects=False,
                            cookies=cookies)
        return resp.headers.get("location")


def is_js_challenge(html: str) -> bool:
    """Halaman challenge Google: shell JS / 'not a robot' / Scholar loading.

    Guard ukuran penting: shell "enable JavaScript" hanya ~90-100 KB; halaman
    hasil besar (images/shopping/AI mode) memang tanpa <h3> dan sering memuat
    string enablejs di bundle JS-nya — jangan sampai salah disebut challenge.
    """
    if ("enablejs" in html and len(html) < 250_000
            and "<h3" not in html and "gs_rt" not in html):
        return True
    low = html.lower()
    if "gs_rt" not in html:
        if "not a robot" in low and "javascript" in low:
            return True
        if "can't perform the operation now" in low:
            return True
    return False


def _load_blocked_state() -> None:
    global _HTTP_BLOCKED
    try:
        data = json.loads(STATE_FILE.read_text())
        families = data.get("families")
        if isinstance(families, dict):
            now = time.time()
            _HTTP_BLOCKED = {k: v for k, v in families.items()
                             if isinstance(v, dict)
                             and float(v.get("until", 0)) > now}
        elif float(data.get("until", 0)) > time.time():
            _HTTP_BLOCKED = {"search": {  # format lama
                "until": float(data["until"]),
                "reason": str(data.get("reason", "")),
                "level": int(data.get("level", 0)), "ts": time.time()}}
    except Exception:
        pass


BROWSER_STATE_FILE = Path.home() / ".cache" / "google-scrape-mcp" / "browser_state.json"
BROWSER_BLOCKED_TTL = _env_float("GOOGLE_SCRAPE_BROWSER_COOLDOWN", 180.0)
_BROWSER_BLOCK: dict = {}
_BROWSER_LOCK = threading.RLock()


def _load_browser_state() -> None:
    global _BROWSER_BLOCK
    try:
        data = json.loads(BROWSER_STATE_FILE.read_text())
        if float(data.get("until", 0)) > time.time():
            _BROWSER_BLOCK = data
    except Exception:
        pass


def _save_browser_state() -> None:
    try:
        if not _BROWSER_BLOCK:
            BROWSER_STATE_FILE.unlink(missing_ok=True)
            return
        BROWSER_STATE_FILE.write_text(json.dumps(_BROWSER_BLOCK))
    except Exception:
        pass


def note_browser_blocked(kind: str = "blocked") -> None:
    """Cooldown browser adaptif: blokir browser berarti rate-limit sungguhan.
    Level naik tiap blokir beruntun dalam 1 jam (TTL * 2^(n-1), cap 1 jam)."""
    global _BROWSER_BLOCK
    with _BROWSER_LOCK:
        now = time.time()
        if now - float(_BROWSER_BLOCK.get("ts", 0)) > 3600:
            _BROWSER_BLOCK = {"level": 0}
        _BROWSER_BLOCK["level"] = int(_BROWSER_BLOCK.get("level", 0)) + 1
        _BROWSER_BLOCK["ts"] = now
        cooldown = min(BROWSER_BLOCKED_TTL * (2 ** (_BROWSER_BLOCK["level"] - 1)),
                       3600.0)
        _BROWSER_BLOCK["until"] = now + cooldown
        _BROWSER_BLOCK["reason"] = f"{kind} (level {_BROWSER_BLOCK['level']})"
        _save_browser_state()
        level = _BROWSER_BLOCK["level"]
    if level >= 2:
        # Profil terbakar: blokir beruntun walau sudah rehat → ganti identitas.
        try:
            from .browser import rotate_profile
            rotate_profile()
            invalidate_bootstrap()
        except Exception:
            pass


def note_browser_ok() -> None:
    global _BROWSER_BLOCK
    with _BROWSER_LOCK:
        if _BROWSER_BLOCK:
            _BROWSER_BLOCK = {}
            _save_browser_state()


def browser_blocked_active() -> bool:
    with _BROWSER_LOCK:
        return float(_BROWSER_BLOCK.get("until", 0)) > time.time()


def browser_blocked_left() -> float:
    with _BROWSER_LOCK:
        return max(0.0, float(_BROWSER_BLOCK.get("until", 0)) - time.time())


def browser_blocked_reason() -> str:
    with _BROWSER_LOCK:
        if float(_BROWSER_BLOCK.get("until", 0)) > time.time():
            return str(_BROWSER_BLOCK.get("reason", ""))
        return ""


BOOTSTRAP_FILE = Path.home() / ".cache" / "google-scrape-mcp" / "bootstrap_cookies.json"
BOOTSTRAP_TTL = _env_float("GOOGLE_SCRAPE_COOKIE_TTL", 300.0)
_BOOTSTRAP: dict = {"ts": 0.0, "cookies": {}}
_BOOTSTRAP_LOCK = threading.Lock()


def _load_bootstrap() -> None:
    global _BOOTSTRAP
    try:
        data = json.loads(BOOTSTRAP_FILE.read_text())
        if (isinstance(data.get("cookies"), dict) and data["cookies"]
                and time.time() - float(data.get("ts", 0)) <= BOOTSTRAP_TTL):
            _BOOTSTRAP = data
    except Exception:
        pass


def bootstrap_valid() -> bool:
    with _BOOTSTRAP_LOCK:
        return bool(_BOOTSTRAP.get("cookies")) and \
            (time.time() - float(_BOOTSTRAP.get("ts", 0)) <= BOOTSTRAP_TTL)


def bootstrap_cookies() -> dict:
    with _BOOTSTRAP_LOCK:
        return dict(_BOOTSTRAP.get("cookies") or {})


def bootstrap_info() -> dict:
    with _BOOTSTRAP_LOCK:
        cookies = _BOOTSTRAP.get("cookies") or {}
        ts = float(_BOOTSTRAP.get("ts", 0))
        return {"valid": bool(cookies) and (time.time() - ts <= BOOTSTRAP_TTL),
                "count": len(cookies),
                "age_s": round(time.time() - ts, 1) if cookies else None,
                "ttl_s": BOOTSTRAP_TTL}


def invalidate_bootstrap() -> None:
    global _BOOTSTRAP
    with _BOOTSTRAP_LOCK:
        _BOOTSTRAP = {"ts": 0.0, "cookies": {}}
    try:
        BOOTSTRAP_FILE.unlink()
    except Exception:
        pass


def refresh_bootstrap() -> bool:
    """Warm-up halaman google.com via browser persisten, lalu ambil cookies
    segar dari konteksnya. Cookies ini membuat raw HTTP /search lolos
    (temuan riset) — menghemat render penuh per pencarian."""
    global _BOOTSTRAP
    try:
        from .browser import fetch_rendered, get_cookies
    except Exception:
        return False
    res = fetch_rendered("https://www.google.com/", resolve_goto=False,
                         expect=("",), settle_ms=1500)
    if res.get("blocked") or res.get("challenge"):
        note_browser_blocked("warmup_blocked")
        return False
    if res.get("error"):
        return False
    cookies = get_cookies() or {}
    if not cookies:
        return False
    with _BOOTSTRAP_LOCK:
        _BOOTSTRAP = {"ts": time.time(), "cookies": cookies}
    try:
        BOOTSTRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOOTSTRAP_FILE.write_text(json.dumps(_BOOTSTRAP))
    except Exception:
        pass
    return True


def endpoint_family(url: str) -> str:
    """Keluarga endpoint untuk bucket cooldown: search | scholar | finance."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path or ""
    host = parts.netloc or ""
    if path.startswith("/scholar") or "scholar.google" in host:
        return "scholar"
    if "/finance/quote" in path:
        return "finance"
    if path.startswith("/search"):
        return "search"
    return ""


def _save_blocked_state() -> None:
    try:
        if not _HTTP_BLOCKED:
            STATE_FILE.unlink(missing_ok=True)
            return
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"families": _HTTP_BLOCKED}))
    except Exception:
        pass


def note_http_blocked(reason: str = "blocked", family: str = "search") -> None:
    """Catat blokir raw HTTP + cooldown adaptif PER FAMILY.

    Blokir ke-n dalam 1 jam untuk family yang sama → cooldown
    TTL * 2^(n-1), maks 1 jam. Family lain tidak terpengaruh.
    """
    if not family:
        return
    now = time.time()
    entry = _HTTP_BLOCKED.get(family) or {"level": 0, "ts": 0.0}
    if now - float(entry.get("ts", 0)) > _BLOCK_LEVEL_DECAY:
        entry["level"] = 0
    entry["level"] = int(entry.get("level", 0)) + 1
    entry["ts"] = now
    cooldown = min(HTTP_BLOCKED_TTL * (2 ** (entry["level"] - 1)), 3600.0)
    entry["until"] = now + cooldown
    entry["reason"] = f"{reason} (level {entry['level']})"
    _HTTP_BLOCKED[family] = entry
    _save_blocked_state()


def note_http_ok(family: str = "search") -> None:
    """HTTP direct sukses lagi untuk family ini → reset cooldown-nya."""
    if family and family in _HTTP_BLOCKED:
        _HTTP_BLOCKED.pop(family, None)
        _save_blocked_state()


def http_blocked_active(family: str | None = None) -> bool:
    now = time.time()
    if family is not None:
        entry = _HTTP_BLOCKED.get(family)
        return bool(entry and float(entry.get("until", 0)) > now)
    return any(float(v.get("until", 0)) > now for v in _HTTP_BLOCKED.values())


def http_blocked_left(family: str | None = None) -> float:
    now = time.time()
    if family is not None:
        entry = _HTTP_BLOCKED.get(family) or {}
        return max(0.0, float(entry.get("until", 0)) - now)
    return max((float(v.get("until", 0)) - now for v in _HTTP_BLOCKED.values()),
               default=0.0)


def http_blocked_reason(family: str | None = None) -> str:
    if family is not None:
        entry = _HTTP_BLOCKED.get(family) or {}
        return str(entry.get("reason", "")) if http_blocked_active(family) else ""
    for fam, entry in _HTTP_BLOCKED.items():
        if float(entry.get("until", 0)) > time.time():
            return f"{fam}: {entry.get('reason', '')}"
    return ""


def block_level(family: str | None = None) -> int:
    if family is not None:
        return int((_HTTP_BLOCKED.get(family) or {}).get("level", 0))
    return max((int(v.get("level", 0)) for v in _HTTP_BLOCKED.values()),
               default=0)


def blocked_families() -> dict:
    now = time.time()
    return {k: {"until": v.get("until"), "reason": v.get("reason"),
                "level": v.get("level")}
            for k, v in _HTTP_BLOCKED.items()
            if float(v.get("until", 0)) > now}


_load_blocked_state()
_load_bootstrap()
_load_browser_state()


def is_blocked_page(html: str) -> bool:
    """Deteksi halaman sorry/captcha/consent/shell JS — pakai marker
    struktural, BUKAN substring mentah yang muncul di bundle JS SERP."""
    if is_js_challenge(html):
        return True
    low = html.lower()
    if 'id="captcha-form"' in html or 'action="/sorry' in html:
        return True
    if "our systems have detected unusual traffic" in low:
        return True
    if ("consent.google.com" in low or "before you continue to google" in low) \
            and "<h3" not in html:
        return True
    return False


def clean_google_url(href: str | None) -> str | None:
    """Ubah /url?q=<target> menjadi URL asli; teruskan http(s) apa adanya."""
    if not href:
        return None
    if href.startswith("/url?"):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        return qs.get("q", [None])[0]
    if href.startswith("http"):
        return href
    return None


BLOCKED_MSG = (
    "Google menampilkan halaman sorry/captcha/consent atau shell JS kosong "
    "(bot-check / rate-limit dari IP ini, bukan error kode). Opsi: "
    "engine='browser' (Camoufox headless), tunggu beberapa saat, isi pool "
    "proxy (GOOGLE_SCRAPE_PROXIES / ~/.config/google-scrape/proxies.txt), "
    "atau pakai tool yang tidak diblokir: google_news_search, "
    "google_scholar_search, google_patents_search, google_suggest, "
    "google_trends_daily."
)
