"""Camoufox headless backend — render Firefox asli yang lolos bot-check Google.

Arsitektur:
  - Satu worker thread memegang **browser persisten** (user_data_dir nyata)
    sehingga cookies (NID/AEC) menua seperti pengunjung beneran; job antar
    request diberi jeda 2.5-6 dtk supaya ritmenya manusiawi.
  - Jalur proxy memakai worker terpisah dengan browser sekali-pakai
    (profil tidak dicampur antar IP).
  - Halaman "enable JavaScript" / "not a robot" dijawab dengan MENJALANKAN
    JS-nya: tunggu redirect, reload bila perlu, klik consent, gerak mouse.
  - Self-heal: bila install Camoufox hilang, `camoufox fetch` dijalankan
    sekali lalu launch diulang.
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from .client import classify_html, endpoint_label, is_js_challenge
from .forensics import record as forensic_record, save_sample

BASE = Path.home() / ".cache" / "google-scrape-mcp"
PROFILE = BASE / "cfx-profile"
_LAUNCH = dict(headless=True, os="windows", exclude_addons=["ublock-origin"])

WARM_TTL = float(os.environ.get("GOOGLE_SCRAPE_WARM_TTL", "600"))
try:
    _gap_lo, _gap_hi = (float(x) for x in
                        os.environ.get("GOOGLE_SCRAPE_JOB_GAP", "1.5-3.5").split("-"))
except Exception:
    _gap_lo, _gap_hi = 1.5, 3.5
_WARM_LOCK = threading.Lock()
_WARM_STATE: dict[str, float] = {}
_SELFHEAL_LOCK = threading.Lock()
_SELFHEAL_LAST = 0.0
_SELFHEAL_COOLDOWN = 1800.0

_CONSENT_SELECTORS = ("#L2AGLb", "#W0wltc", 'button[aria-label="Reject all"]',
                      'button[aria-label="Tolak semua"]')
_CONSENT_RE = re.compile(r"consent\.google\.com")


def _is_google_domain(domain: str) -> bool:
    domain = (domain or "").lstrip(".").lower()
    return domain in ("google.com", "google.co.id") or \
        domain.endswith(".google.com") or domain.endswith(".google.co.id")


def _is_challenge(html: str, expect: tuple[str, ...] = ("<h3",)) -> bool:
    """Challenge = shell JS/'not a robot' TANPA marker hasil yang diharapkan.

    Tiap jenis halaman punya marker hasilnya sendiri (web: <h3>, images:
    encrypted-tbn, news: n0jPhd, scholar: gs_rt) supaya halaman hasil yang
    sah tidak salah dianggap challenge.
    """
    if any(marker in html for marker in expect):
        return False
    return is_js_challenge(html)


def available() -> tuple[bool, str]:
    try:
        import camoufox  # noqa: F401
        return True, ""
    except ImportError:
        return False, "pip install camoufox"


def _launch_error(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    hint = ""
    if "not installed" in msg.lower() or "not found" in msg.lower():
        hint = " Jalankan `camoufox fetch` untuk memasang browser."
    return f"Browser launch failed ({exc.__class__.__name__}): {msg[:300]}.{hint}"


def _selfheal_install() -> tuple[bool, str]:
    """`camoufox fetch` sekali kalau browser hilang (dengan cooldown)."""
    global _SELFHEAL_LAST
    if os.environ.get("GOOGLE_SCRAPE_NO_SELFHEAL"):
        return False, "selfheal dimatikan (GOOGLE_SCRAPE_NO_SELFHEAL)"
    with _SELFHEAL_LOCK:
        if time.time() - _SELFHEAL_LAST < _SELFHEAL_COOLDOWN:
            return False, "selfheal baru saja dicoba"
        _SELFHEAL_LAST = time.time()
    exe = shutil.which("camoufox") or str(Path.home() / ".local/bin/camoufox")
    if not Path(exe).exists():
        return False, "CLI camoufox tidak ditemukan"
    try:
        proc = subprocess.run([exe, "fetch"], capture_output=True, text=True,
                              timeout=900)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"
    if proc.returncode == 0:
        return True, "camoufox fetch selesai"
    return False, (proc.stderr or proc.stdout or "").strip()[-200:]


def _clean_stale_locks(profile: Path) -> None:
    """Hapus lock profil Firefox yang basi (sisa proses yang mati).

    lock berisi symlink `hostname:+PID`; kalau PID-nya sudah tidak ada,
    Firefox/Playwright menolak launch sampai lock dihapus.
    """
    lock = profile / "lock"
    parent = profile / ".parentlock"
    alive = False
    try:
        if lock.is_symlink():
            target = os.readlink(str(lock))
            m = re.search(r":\+?(\d+)$", target)
            if m and Path(f"/proc/{m.group(1)}").exists():
                alive = True
    except Exception:
        pass
    if not alive:
        for path in (lock, parent):
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
            except Exception:
                pass


def _dismiss_consent(pg) -> None:
    """Klik dialog consent kalau muncul — manusia juga mengklik."""
    for selector in _CONSENT_SELECTORS:
        try:
            button = pg.query_selector(selector)
            if button and button.is_visible():
                button.click(timeout=2500)
                pg.wait_for_timeout(1200)
                return
        except Exception:
            continue


def _humanize(pg) -> None:
    """Satu gerakan mouse + scroll kecil — cukup manusiawi, tidak lambat."""
    try:
        pg.mouse.move(random.randint(80, 700), random.randint(80, 500),
                      steps=random.randint(3, 6))
        pg.wait_for_timeout(random.randint(60, 150))
        pg.mouse.wheel(0, random.randint(200, 500))
        pg.wait_for_timeout(random.randint(100, 220))
    except Exception:
        pass


def _warm_state_key(profile: Path) -> str:
    return str(profile)


def _mark_warm(key: str) -> None:
    with _WARM_LOCK:
        _WARM_STATE[key] = time.time()


def _is_warm(key: str) -> bool:
    with _WARM_LOCK:
        return (time.time() - _WARM_STATE.get(key, 0.0)) <= WARM_TTL


def _settle_content(pg, settle_ms: int, expect: tuple[str, ...],
                    max_rounds: int = 3) -> tuple[str, str, int]:
    """Tunggu sampai JS challenge selesai (halaman redirect sendiri)."""
    html, final = pg.content(), pg.url
    rounds = 0
    for i in range(max_rounds):
        if not _is_challenge(html, expect):
            break
        pg.wait_for_timeout(settle_ms)
        html, final = pg.content(), pg.url
        rounds += 1
        if _is_challenge(html, expect) and i < max_rounds - 1:
            try:
                pg.reload(timeout=30000, wait_until="domcontentloaded")
            except Exception:
                pass
    return html, final, rounds


def _resolve_goto(pg, final: str) -> dict:
    resolved: dict[str, str] = {}
    try:
        hrefs = pg.evaluate(
            "() => [...document.querySelectorAll("
            "'a[href^=\"/goto\"]')].slice(0, 40).map(a => a.href)")
        for href in hrefs or []:
            try:
                rr = pg.request.get(href, timeout=10000, max_redirects=0,
                                    headers={"Referer": final})
                loc = rr.headers.get("location")
                if loc:
                    resolved[href] = loc
            except Exception:
                pass
    except Exception:
        pass
    return resolved


def _wait_text_stable(pg, selector: str, max_ms: int = 15000,
                      min_len: int = 120) -> None:
    """Tunggu sampai teks elemen berhenti tumbuh (streaming selesai)."""
    deadline = time.time() + max_ms / 1000.0
    last = -1
    stable = 0
    while time.time() < deadline:
        try:
            text = pg.inner_text(selector)
        except Exception:
            text = ""
        length = len(text or "")
        if length >= min_len and length == last:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = length
        pg.wait_for_timeout(450)


def _render_job(browser, full: str, locale: str, timeout_ms: int,
                settle_ms: int, warm_url: str, resolve_goto: bool,
                expect: tuple[str, ...], wait_for: str | None = None,
                wait_text: str | None = None,
                do_warm: bool = True) -> dict:
    pg = browser.new_page()
    try:
        if do_warm and warm_url:
            try:
                pg.goto(warm_url, timeout=timeout_ms,
                        wait_until="domcontentloaded")
                pg.wait_for_timeout(1200 + random.randint(0, 1000))
                _dismiss_consent(pg)
            except Exception:
                pass
        resp = pg.goto(full, timeout=timeout_ms, wait_until="domcontentloaded")
        status = resp.status if resp else None
        if wait_for:
            try:
                pg.wait_for_selector(wait_for, timeout=min(15000, timeout_ms))
            except Exception:
                pass
        if wait_text:
            _wait_text_stable(pg, wait_text)
        _humanize(pg)
        html, final, js_rounds = _settle_content(pg, settle_ms, expect)
        resolved = _resolve_goto(pg, final) if resolve_goto else {}
    finally:
        pg.close()
    blocked = ("/sorry/" in final or 'id="captcha-form"' in html
               or bool(_CONSENT_RE.search(final)))
    challenge = _is_challenge(html, expect)
    if blocked or challenge:
        kind = classify_html(html, status) or ("challenge" if challenge else "blocked")
        label = endpoint_label(str(final or full))
        forensic_record(kind, "browser", label, str(final or full), status)
        save_sample(kind, "browser", label, str(final or full), status, html)
    empty = (not challenge and not any(m in html for m in expect)
             and len(html) < 200_000)
    return {"url": final, "html": html, "http_status": status,
            "blocked": blocked, "challenge": challenge, "empty": empty,
            "js_rounds": js_rounds, "resolved": resolved}


_TRENDS_JS = """
async (args) => {
  const strip = (t) => t.startsWith(")]}'") ? t.slice(5) : t;
  try {
    const r1 = await fetch(args.explore, {credentials: "include"});
    if (!r1.ok) return {stage: "explore", status: r1.status, error: true};
    let payload;
    try { payload = JSON.parse(strip(await r1.text())); }
    catch (e) { return {stage: "explore-json", status: r1.status, error: true}; }
    const ts = (payload.widgets || []).find(w => w.id === "TIMESERIES");
    if (!ts) return {stage: "widget", status: 200, error: true};
    // Kunci: kirim ts.request VERBATIM (termasuk userConfig) — jika
    // field-nya dirakit ulang, widgetdata membalas 401.
    const url = "https://trends.google.com/trends/api/widgetdata/multiline"
      + "?hl=" + encodeURIComponent(args.hl) + "&tz=" + args.tz
      + "&token=" + encodeURIComponent(ts.token || "")
      + "&req=" + encodeURIComponent(JSON.stringify(ts.request));
    const r2 = await fetch(url, {credentials: "include"});
    const text = await r2.text();
    if (!r2.ok) {
      return {stage: "widgetdata", status: r2.status, error: true,
              head: text.slice(0, 120)};
    }
    return {stage: "ok", status: r2.status, text: text};
  } catch (e) {
    return {stage: "exception", status: 0, error: true, message: String(e)};
  }
}
"""


def _warm_job(browser) -> dict:
    """Satu job: kunjungi google.com lalu kembalikan cookies konteks.

    Dipakai bootstrap fast-path — menggabungkan warm-up dan pengambilan
    cookie agar tidak ada dua job + dua jeda antrean.
    """
    pg = browser.new_page()
    try:
        resp = pg.goto("https://www.google.com/", timeout=30000,
                       wait_until="domcontentloaded")
        pg.wait_for_timeout(1200 + random.randint(0, 800))
        _dismiss_consent(pg)
        html = pg.content() or ""
        status = resp.status if resp else None
    finally:
        pg.close()
    cookies = {}
    try:
        for cookie in browser.cookies() or []:
            domain = str(cookie.get("domain") or "")
            if _is_google_domain(domain) and cookie.get("name"):
                cookies.setdefault(str(cookie["name"]),
                                   str(cookie.get("value") or ""))
    except Exception:
        pass
    blocked = ("/sorry/" in (pg.url if hasattr(pg, "url") else "")
               or 'id="captcha-form"' in html
               or bool(_CONSENT_RE.search(pg.url if hasattr(pg, "url") else "")))
    return {"cookies": cookies, "blocked": blocked, "http_status": status}


def _trends_job(browser, explore_url: str, hl: str, tz: int,
                timeout_ms: int, do_warm: bool = True) -> dict:
    pg = browser.new_page()
    try:
        if do_warm:
            try:
                pg.goto("https://trends.google.com/", timeout=timeout_ms,
                        wait_until="domcontentloaded")
                pg.wait_for_timeout(1500)
            except Exception:
                pass
        result = pg.evaluate(_TRENDS_JS,
                             {"explore": explore_url, "hl": hl, "tz": tz})
    finally:
        pg.close()
    if not isinstance(result, dict):
        return {"error": "unexpected browser result", "stage": "js"}
    if result.get("error"):
        return {"error": f"trends widget ditolak di browser (stage "
                         f"{result.get('stage')}, HTTP {result.get('status')}"
                         + (f", {result['head']!r} " if result.get("head") else "")
                         + ")", "stage": result.get("stage")}
    return {"text": result.get("text") or "", "stage": "ok"}


class _Worker:
    """Queue job browser. `persistent=True` memakai satu browser+profil
    sepanjang umur proses; `False` meluncurkan browser sekali-pakai
    (dipakai untuk jalur proxy agar profil tidak dicampur antar IP)."""

    def __init__(self, name: str, persistent: bool, profile: Path,
                 idle_s: float = 600.0):
        self.name = name
        self.persistent = persistent
        self.profile = profile
        self.idle_s = idle_s
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last = 0.0

    def stop(self, timeout: float = 20.0) -> None:
        """Minta loop berhenti (menutup browser). Thread akan exit; submit
        berikutnya menghidupkannya lagi."""
        self._q.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def submit(self, fn, *, proxy: dict | None = None, locale: str = "en-US",
               timeout: float = 300.0, priority: bool = False,
               warm_override: bool | None = None,
               marks_warm: bool = False):
        self._ensure()
        box: dict = {"done": threading.Event()}
        self._q.put({"fn": fn, "proxy": proxy, "locale": locale, "box": box,
                     "priority": priority, "warm_override": warm_override,
                     "marks_warm": marks_warm})
        if not box["done"].wait(timeout):
            raise TimeoutError(f"{self.name} browser job timeout {timeout:.0f}s")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _ensure(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=self.name,
                                            daemon=True)
            self._thread.start()

    def _gap(self) -> None:
        wait = random.uniform(_gap_lo, _gap_hi) - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _drain(self, exc: Exception) -> None:
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                return
            if job is None:
                continue
            job["box"]["error"] = exc
            job["box"]["done"].set()

    def _run(self) -> None:
        from camoufox.sync_api import Camoufox
        try:
            if self.persistent:
                self.profile.mkdir(parents=True, exist_ok=True)
                try:
                    _clean_stale_locks(self.profile)
                    with Camoufox(locale="en-US", persistent_context=True,
                                  user_data_dir=str(self.profile),
                                  **_LAUNCH) as browser:
                        self._loop_with_browser(browser)
                except Exception as exc:
                    msg = str(exc).lower()
                    if any(k in msg for k in ("in use", "lock", "profile",
                                              "already running", "parentlock")):
                        # bersihkan lock lalu coba sekali lagi; kalau tetap
                        # gagal, jalan ephemeral (tanpa profil).
                        _clean_stale_locks(self.profile)
                        try:
                            with Camoufox(locale="en-US",
                                          persistent_context=True,
                                          user_data_dir=str(self.profile),
                                          **_LAUNCH) as browser:
                                self._loop_with_browser(browser)
                        except Exception:
                            with Camoufox(locale="en-US", **_LAUNCH) as browser:
                                self._loop_with_browser(browser)
                    else:
                        raise
            else:
                self._loop_ephemeral()
        except Exception as exc:
            self._drain(exc)

    def _do_warm(self, job) -> bool:
        override = job.get("warm_override")
        if override is not None:
            return bool(override)
        if not self.persistent:
            return True
        return not _is_warm(_warm_state_key(self.profile))

    def _loop_with_browser(self, browser) -> None:
        while True:
            try:
                job = self._q.get(timeout=self.idle_s)
            except queue.Empty:
                break
            if job is None:
                break
            if not job.get("priority"):
                self._gap()
            box = job["box"]
            do_warm = self._do_warm(job)
            try:
                box["result"] = job["fn"](browser, do_warm)
            except TypeError:
                box["result"] = job["fn"](browser)
            except Exception as exc:
                box["error"] = exc
            finally:
                if do_warm or job.get("marks_warm"):
                    _mark_warm(_warm_state_key(self.profile))
                self._last = time.time()
                box["done"].set()

    def _loop_ephemeral(self) -> None:
        from camoufox.sync_api import Camoufox
        while True:
            try:
                job = self._q.get(timeout=self.idle_s)
            except queue.Empty:
                break
            if job is None:
                break
            if not job.get("priority"):
                self._gap()
            box = job["box"]
            try:
                kwargs = dict(_LAUNCH)
                if job["proxy"]:
                    kwargs["proxy"] = job["proxy"]
                with Camoufox(locale=job["locale"], **kwargs) as browser:
                    try:
                        box["result"] = job["fn"](browser, True)
                    except TypeError:
                        box["result"] = job["fn"](browser)
            except Exception as exc:
                box["error"] = exc
            finally:
                self._last = time.time()
                box["done"].set()


_DIRECT = _Worker("gsmcp-direct", persistent=True, profile=PROFILE)
_PROXY = _Worker("gsmcp-proxy", persistent=False, profile=BASE / "cfx-proxy")


MAX_BURNED_PROFILES = 2


def rotate_profile() -> str:
    """Tutup browser persisten dan ganti profilnya dengan yang baru.

    Dipakai saat profil lama 'terbakar' (blokir beruntun walau IP sudah
    rehat). Profil lama diarsipkan sebagai cfx-profile.burned-<ts>; hanya
    MAX_BURNED_PROFILES terbaru yang disimpan.
    """
    _DIRECT.stop()
    dest = ""
    if PROFILE.exists():
        dest = str(PROFILE.parent /
                   f"cfx-profile.burned-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            PROFILE.rename(dest)
        except Exception:
            dest = ""
    burned = sorted(PROFILE.parent.glob("cfx-profile.burned-*"))
    for old in burned[:-MAX_BURNED_PROFILES]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def get_cookies() -> dict:
    """Cookies konteks persisten (name->value, domain google) — dipakai untuk
    bootstrap HTTP fast-path di client."""
    ok, _ = available()
    if not ok:
        return {}
    def job(browser) -> dict:
        try:
            items = browser.cookies()
        except Exception:
            return {}
        out = {}
        for cookie in items or []:
            domain = str(cookie.get("domain") or "")
            if _is_google_domain(domain) and cookie.get("name"):
                out.setdefault(str(cookie["name"]), str(cookie.get("value") or ""))
        return out
    try:
        return _DIRECT.submit(job, timeout=60) or {}
    except Exception:
        return {}


def fetch_rendered(url: str, params: dict | None = None,
                   locale: str = "en-US", timeout_ms: int = 45000,
                   settle_ms: int = 4000,
                   warm_url: str = "https://www.google.com/",
                   resolve_goto: bool = True,
                   proxy: dict | None = None,
                   expect: tuple[str, ...] = ("<h3",),
                   wait_for: str | None = None,
                   wait_text: str | None = None,
                   priority: bool = False,
                   warm_override: bool | None = None,
                   _retried: bool = False) -> dict:
    """Render halaman di browser (persisten untuk direct, sekali-pakai untuk
    proxy). `expect` = marker hasil per jenis halaman (lihat _is_challenge).
    Balikan: url/html/http_status + flag blocked/challenge/empty."""
    ok, msg = available()
    if not ok:
        return {"error": f"Browser backend butuh paket camoufox: {msg}"}
    full = url
    if params:
        full = url + "?" + urllib.parse.urlencode(params)

    def job(browser, do_warm: bool = True) -> dict:
        return _render_job(browser, full, locale, timeout_ms, settle_ms,
                           warm_url, resolve_goto, expect, wait_for,
                           wait_text, do_warm)

    worker = _PROXY if proxy else _DIRECT
    wait = (timeout_ms / 1000.0) * 3 + 60
    try:
        return worker.submit(job, proxy=proxy, locale=locale, timeout=wait,
                             priority=priority, warm_override=warm_override)
    except Exception as exc:
        if not _retried and "not installed" in str(exc).lower():
            healed, _info = _selfheal_install()
            if healed:
                return fetch_rendered(url, params=params, locale=locale,
                                      timeout_ms=timeout_ms,
                                      settle_ms=settle_ms, warm_url=warm_url,
                                      resolve_goto=resolve_goto, proxy=proxy,
                                      expect=expect, wait_for=wait_for,
                                      wait_text=wait_text, priority=priority,
                                      warm_override=warm_override,
                                      _retried=True)
        return {"error": _launch_error(exc)}


def warm_homepage_and_cookies() -> dict:
    """Bootstrap cepat: satu job (prioritas, tanpa jeda) yang membuka
    google.com dan langsung mengembalikan cookies konteks."""
    ok, _msg = available()
    if not ok:
        return {"error": "camoufox tidak tersedia"}
    try:
        return _DIRECT.submit(lambda browser, do_warm: _warm_job(browser),
                              timeout=90, priority=True,
                              warm_override=False, marks_warm=True)
    except Exception as exc:
        return {"error": _launch_error(exc)}


def fetch_trends_series(keywords: list[str], geo: str = "",
                        timeframe: str = "today 12-m", hl: str = "en-US",
                        tz: int = 0, locale: str = "en-US",
                        timeout_ms: int = 45000,
                        proxy: dict | None = None,
                        _retried: bool = False) -> dict:
    """Ambil widgetdata/multiline Trends dari konteks browser (verbatim
    request). Balikan {"text": <JSON mentah>} atau {"error": ..., "stage": ...}."""
    ok, msg = available()
    if not ok:
        return {"error": f"Browser backend butuh paket camoufox: {msg}"}

    req = {"comparisonItem": [{"keyword": k, "geo": geo, "time": timeframe}
                              for k in keywords],
           "category": 0, "property": ""}
    explore_url = ("https://trends.google.com/trends/api/explore"
                   + "?hl=" + hl + "&tz=" + str(tz)
                   + "&req=" + urllib.parse.quote(json.dumps(req)))

    def job(browser, do_warm: bool = True) -> dict:
        return _trends_job(browser, explore_url, hl, tz, timeout_ms, do_warm)

    worker = _PROXY if proxy else _DIRECT
    wait = (timeout_ms / 1000.0) * 3 + 60
    try:
        return worker.submit(job, proxy=proxy, locale=locale, timeout=wait,
                             priority=True)
    except Exception as exc:
        if not _retried and "not installed" in str(exc).lower():
            healed, _info = _selfheal_install()
            if healed:
                return fetch_trends_series(keywords, geo=geo,
                                           timeframe=timeframe, hl=hl, tz=tz,
                                           locale=locale, timeout_ms=timeout_ms,
                                           proxy=proxy, _retried=True)
        return {"error": _launch_error(exc), "stage": "launch"}
