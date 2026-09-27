"""Pool proxy untuk google-scrape: parse, rotasi round-robin, cooldown.

Sumber proxy (urutan prioritas):
  1. env GOOGLE_SCRAPE_PROXIES  — daftar inline (pisah koma/spasi/baris baru)
     atau path file (bila diawali / atau ~ atau berakhiran .txt).
  2. env GOOGLE_SCRAPE_PROXY_FILES — path file, dipisah ':'.
  3. File default yang ada di disk (lihat DEFAULT_FILES).

Format baris yang diterima:
  http://host:port
  socks5://host:port
  http://user:pass@host:port
  host:port
  host:port:user:pass          (format export umum)

Env lain:
  GOOGLE_SCRAPE_PROXY_MODE     auto (default) | off | always
  GOOGLE_SCRAPE_PROXY_COOLDOWN detik cooldown setelah gagal (default 600)
"""
from __future__ import annotations

import os
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FILES = (
    "~/.cache/google-scrape-mcp/proxies_curated.txt",
    "~/.config/google-scrape/proxies.txt",
)
MAX_PER_FILE = 500

SCHEMES = {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}


@dataclass(frozen=True)
class Proxy:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def key(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def server(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        if not self.username:
            return self.server
        user = urllib.parse.quote(self.username, safe="")
        pwd = urllib.parse.quote(self.password, safe="")
        return f"{self.scheme}://{user}:{pwd}@{self.host}:{self.port}"

    def playwright(self) -> dict:
        spec = {"server": self.server}
        if self.username:
            spec["username"] = self.username
            spec["password"] = self.password
        return spec


def parse_proxy(line: str) -> Proxy | None:
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        parts = urllib.parse.urlsplit(line)
        scheme = (parts.scheme or "http").lower()
        if scheme == "socks5h":
            scheme = "socks5"
        if scheme == "socks4a":
            scheme = "socks4"
        if scheme not in SCHEMES:
            return None
        host, port = parts.hostname, parts.port
        user = urllib.parse.unquote(parts.username or "")
        pwd = urllib.parse.unquote(parts.password or "")
    else:
        chunks = line.split(":")
        if len(chunks) == 2:
            host, port_s, user, pwd = chunks[0], chunks[1], "", ""
        elif len(chunks) == 4:
            host, port_s, user, pwd = chunks
        else:
            return None
        try:
            port = int(port_s)
        except ValueError:
            return None
        scheme = "http"
    if not host or not isinstance(port, int) or not (0 < port < 65536):
        return None
    if " " in host:
        return None
    return Proxy(scheme=scheme, host=host, port=port, username=user, password=pwd)


def _load_file(path: str) -> list[Proxy]:
    out: list[Proxy] = []
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return out
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= MAX_PER_FILE:
                    break
                proxy = parse_proxy(line)
                if proxy:
                    out.append(proxy)
    except OSError:
        pass
    return out


def _env_paths() -> tuple[str, str]:
    return (os.environ.get("GOOGLE_SCRAPE_PROXIES", "").strip(),
            os.environ.get("GOOGLE_SCRAPE_PROXY_FILES", "").strip())


def load_proxies() -> list[Proxy]:
    raw, files_env = _env_paths()
    entries: list[Proxy] = []
    if raw:
        if raw.startswith(("/", "~")) or raw.lower().endswith(".txt"):
            entries += _load_file(raw)
        else:
            for token in raw.replace(",", " ").split():
                proxy = parse_proxy(token)
                if proxy:
                    entries.append(proxy)
    if raw or files_env:
        for path in [p for p in files_env.split(":") if p.strip()]:
            entries += _load_file(path.strip())
    else:
        for path in DEFAULT_FILES:
            entries += _load_file(path)
    seen, unique = set(), []
    for proxy in entries:
        if proxy.key not in seen:
            seen.add(proxy.key)
            unique.append(proxy)
    return unique


class ProxyPool:
    """Round-robin pool dengan cooldown per proxy yang sedang gagal."""

    def __init__(self, entries: list[Proxy], cooldown: float | None = None):
        self._entries = list(entries)
        self._cooldown = cooldown if cooldown is not None else float(
            os.environ.get("GOOGLE_SCRAPE_PROXY_COOLDOWN", "600"))
        self._index = 0
        self._failed_until: dict[str, float] = {}
        self._successes: dict[str, int] = {}
        self._lock = threading.Lock()
        self.report_failure_hook = None  # reserved

    @property
    def cooldown(self) -> float:
        return self._cooldown

    def size(self) -> int:
        return len(self._entries)

    def available(self) -> bool:
        with self._lock:
            now = time.time()
            return any(self._failed_until.get(p.key, 0) <= now
                       for p in self._entries)

    def acquire(self) -> Proxy | None:
        """Ambil proxy siap pakai berikutnya, atau None bila semua cooldown."""
        with self._lock:
            now = time.time()
            n = len(self._entries)
            for step in range(n):
                proxy = self._entries[(self._index + step) % n]
                if self._failed_until.get(proxy.key, 0) <= now:
                    self._index = (self._index + step + 1) % max(n, 1)
                    return proxy
        return None

    def acquire_proven(self) -> Proxy | None:
        """Ambil proxy yang pernah sukses (bukan cuma connect) — dipakai untuk
        fallback browser agar tidak membuang 1-2 menit di proxy mati."""
        with self._lock:
            now = time.time()
            n = len(self._entries)
            for step in range(n):
                proxy = self._entries[(self._index + step) % n]
                if self._successes.get(proxy.key, 0) > 0 and \
                        self._failed_until.get(proxy.key, 0) <= now:
                    self._index = (self._index + step + 1) % max(n, 1)
                    return proxy
        return None

    def proven_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._entries if self._successes.get(p.key, 0) > 0)

    def report_failure(self, proxy: Proxy) -> None:
        with self._lock:
            jitter = random.uniform(0, self._cooldown * 0.1)
            self._failed_until[proxy.key] = time.time() + self._cooldown + jitter

    def report_success(self, proxy: Proxy) -> None:
        with self._lock:
            self._failed_until.pop(proxy.key, None)
            self._successes[proxy.key] = self._successes.get(proxy.key, 0) + 1

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            ready = sum(1 for p in self._entries
                        if self._failed_until.get(p.key, 0) <= now)
            return {"total": len(self._entries), "ready": ready,
                    "cooling": len(self._entries) - ready,
                    "proven": sum(1 for p in self._entries
                                  if self._successes.get(p.key, 0) > 0),
                    "cooldown_s": self._cooldown,
                    "mode": proxy_mode()}


_POOL: ProxyPool | None = None
_POOL_LOCK = threading.Lock()


def get_pool() -> ProxyPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ProxyPool(load_proxies())
        return _POOL


def reset_pool() -> ProxyPool:
    """Force reload (dipakai test / setelah file proxy diperbarui)."""
    global _POOL
    with _POOL_LOCK:
        _POOL = ProxyPool(load_proxies())
        return _POOL


def proxy_mode() -> str:
    mode = os.environ.get("GOOGLE_SCRAPE_PROXY_MODE", "auto").strip().lower()
    return mode if mode in ("auto", "off", "always") else "auto"
