"""Parser HTML/JSON Google murni scrape (tanpa API key)."""
from __future__ import annotations

import html as ihtml
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .client import clean_google_url


def reconstruct_url(cite_text: str | None) -> str | None:
    """Rekonstruksi URL dari teks cite Google ('host › path › page').

    Mengembalikan None bila host bukan domain valid (teks semu seperti
    '1,5 rb+ suka · ...', cite terpotong '...') agar pemanggil pakai
    fallback resolve /goto.
    """
    if not cite_text:
        return None
    parts = [p.strip() for p in cite_text.split("›")]
    if any("..." in p for p in parts):
        return None
    host = parts[0].rstrip("/")
    if " " in host or "." not in host:
        return None
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,}",
                         host):
        return None
    if not host.startswith("http"):
        host = "https://" + host
    path = "/".join(p.replace(" ", "_") for p in parts[1:] if p)
    return host + ("/" + path if path else "")


_GOTO_CACHE: dict[str, str | None] = {}


def apply_resolved(results: list, resolved: dict | None) -> list:
    """Ganti URL /goto opaque dengan URL asli dari map resolve in-browser.

    Map berasal dari request-context browser yang SAMA dengan render
    (token /goto terikat sesi & cepat kedaluwarsa). Cocokkan via token
    url= agar robust terhadap perbedaan &amp;/&.
    """
    if not resolved:
        return results
    by_token = {}
    for abs_url, target in resolved.items():
        m = re.search(r"url=([^&]+)", abs_url)
        if m:
            by_token[m.group(1)] = target
    for item in results:
        u = item.get("url") or ""
        if "/goto?url=" in u:
            m = re.search(r"url=([^&]+)", u)
            if m and m.group(1) in by_token:
                item["url"] = by_token[m.group(1)]
    return results


def resolve_goto(goto_url: str) -> str | None:
    """/goto Google adalah 302 ke URL asli — resolve murah via HEAD tanpa JS."""
    if goto_url in _GOTO_CACHE:
        return _GOTO_CACHE[goto_url]
    try:
        from .client import head_location
        loc = head_location(goto_url, light=True)
        _GOTO_CACHE[goto_url] = loc
        return loc
    except Exception:
        _GOTO_CACHE[goto_url] = None
        return None


_AI_PREFIX = re.compile(r"^Disalin\s+Salin\s+Edit\s+")
_AI_TRAILING = re.compile(r"\s*(?:Dibagikan\s+\d+\s+file.*|Tampilkan draf.*|Salin)+$")
_AI_STOP = re.compile(r"\s*(?:Salin link|Tidak dapat menyalin|Gagal menyalin|"
                      r"Disalin ke papan klip|Coba lagi nanti|"
                      r"AI dapat membuat kesalahan|Bagikan|Respons baik|"
                      r"Respons buruk|Selengkapnya|Tentang respons ini)")


def parse_ai_mode(html: str, query: str = ""):
    """Parser Google Mode AI (udm=50): teks jawaban + sumber.

    Jawaban ada di div.Rty6Hf (chat bubble). Sumber dirender sebagai link
    bila jawabannya mengutip; kalau belum dimuat, sources kosong.
    """
    soup = BeautifulSoup(html, "lxml")
    container = soup.select_one("div.Rty6Hf") or soup.select_one("div.tonYlb.Uphzyf")
    if container is None:
        node = soup.find(string=re.compile(r"Balasan Mode AI"))
        container = node.find_parent("div") if node else None
    if container is None:
        return {"answer": None, "sources": [], "results": []}

    text = re.sub(r"\s+", " ", container.get_text(" ", strip=True))
    text = _AI_PREFIX.sub("", text)
    prefix = f"{query} Balasan Mode AI untuk {query}"
    if query and text.startswith(prefix):
        text = text[len(prefix):].lstrip()
    else:
        m = re.search(r"Balasan Mode AI untuk .{0,300}?(?=[A-Z0-9])", text)
        if m:
            text = text[m.end():]
    text = _AI_TRAILING.sub("", text).strip()
    stop = _AI_STOP.search(text)
    if stop:
        text = text[:stop.start()]
    text = re.sub(r"\s+([.,;:)])", r"\1", text).strip()
    if len(text) < 10:
        # baru bubble pertanyaan; jawaban belum termuat (streaming)
        text = ""

    sources, seen = [], set()
    scope = container.parent or container
    for a in scope.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        netloc = urllib.parse.urlsplit(href).netloc
        if re.search(r"(^|\.)google\.", netloc):
            continue
        if href in seen:
            continue
        seen.add(href)
        sources.append({"title": a.get_text(" ", strip=True)[:120], "url": href})
    return {"answer": text or None, "sources": sources[:10],
            "results": sources[:10]}


def _resolve_many(gotos: list[str], cap: int = 12) -> dict:
    """Resolve beberapa /goto sekaligus (paralel, ringan) — jalur HTTP
    fast-path menghasilkan sampai ~10 link per SERP; sekuensial terlalu
    lambat untuk jalur cepat."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .client import head_location

    out: dict[str, str | None] = {}
    targets = list(dict.fromkeys(gotos))[:cap]
    if not targets:
        return out
    with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
        futures = {pool.submit(head_location, g, 15, True): g
                   for g in targets}
        for fut in as_completed(futures):
            goto = futures[fut]
            try:
                out[goto] = fut.result()
            except Exception:
                out[goto] = None
    return out


def _extract_serp_result(h3, base: str, resolve_links: bool):
    """Ambil satu hasil dari sebuah h3 SERP — layout-agnostic.

    Web memakai div.wHYlTd.Ww4FFb, Books memakai div.b8lM7/a.zReHs, dsb;
    h3.LC20lb + anchor + cite adalah irisan yang stabil di semuanya.
    """
    title = h3.get_text(" ", strip=True)
    if not title or title in ("Mode AI membalas:",):
        return None
    a = h3.find_parent("a", href=True)
    if a is None:
        a = h3.find("a", href=True)
    goto = None
    if a is not None and a.get("href", "").startswith("/goto"):
        goto = base + a["href"]
    cite = h3.find_next("cite")
    if cite is None and a is not None:
        cite = a.find("cite")
    cite_txt = cite.get_text("", strip=True) if cite else None
    url = None
    if not goto and a is not None:
        href = a.get("href") or ""
        if href.startswith("/url?"):
            url = clean_google_url(href)
        elif href.startswith("http"):
            url = href
    if not url and not goto:
        url = reconstruct_url(cite_txt)
    if not url and not goto:
        return None
    if cite_txt and "·" in cite_txt and "http" not in cite_txt and not goto:
        return None  # pseudo-cite (views/time) tanpa anchor nyata
    snippet = ""
    node = h3
    for _ in range(3):
        node = node.parent
        if node is None:
            break
        snip_el = node.select_one("div.VwiC3b") if hasattr(node, "select_one") else None
        if snip_el is not None:
            snippet = snip_el.get_text(" ", strip=True)
            break
        txt = node.get_text(" ", strip=True)
        if len(txt) > len(title) + 40 and len(txt) < 2000:
            snippet = txt
            if snippet.startswith(title):
                snippet = snippet[len(title):].strip(" -–—|·")
            break
    site = None
    for sel in ("span.VuuXrf", "span.ylgVCe"):
        site = h3.find_next(sel)
        if site is not None:
            break
    item = {"title": title, "url": url or goto, "cite": cite_txt,
            "site": site.get_text(strip=True) if site else "",
            "snippet": snippet[:800]}
    if goto and not url:
        item["_goto"] = goto
    return item


def parse_serp_live(html: str, base: str = "https://www.google.com",
                    resolve_links: bool = True):
    """Parser SERP hasil render browser (markup Google 2025+).

    Iterasi h3.LC20lb (web/books/video/shopping) dengan ekstraksi generik;
    link /goto opaque → URL dari cite bila utuh, else resolve 302.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_urls: set[str] = set()
    for h3 in soup.find_all("h3"):
        classes = h3.get("class") or []
        if "LC20lb" not in classes and not h3.find_parent("a", href=True):
            continue
        item = _extract_serp_result(h3, base, resolve_links)
        if not item:
            continue
        key = (item["url"].split("?", 1)[0].rstrip("/").lower(),
               item["title"].lower())
        if key in seen_urls:
            continue
        seen_urls.add(key)
        results.append(item)

    if resolve_links:
        pending = [it["_goto"] for it in results if it.get("_goto")]
        resolved = _resolve_many(pending) if pending else {}
        for it in results:
            goto = it.pop("_goto", None)
            if goto:
                it["url"] = resolved.get(goto) or goto
    else:
        for it in results:
            it.pop("_goto", None)
    related = []
    for a in soup.find_all("a", href=re.compile(r"/search\?.*q=")):
        txt = a.get_text(" ", strip=True)
        if txt and len(txt) < 120 and txt not in related:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query)
            if qs.get("q"):
                related.append(txt)
    m = re.search(r"About ([\d,\.]+) results", soup.get_text(" ", strip=True))
    featured = None
    el = soup.select_one("div.xpdopen")
    if el and len(el.get_text(strip=True)) > 30:
        featured = el.get_text(" ", strip=True)[:2000]
    return {"results": results, "related_searches": related[:10],
            "total_results": m.group(1) if m else None,
            "featured_snippet": featured}


def parse_news_live(html: str, base: str = "https://www.google.com",
                    resolve_links: bool = True):
    """Parser tab News hasil render browser: div.n0jPhd (judul) di dalam
    anchor /goto + info publisher/waktu di sekitarnya."""
    soup = BeautifulSoup(html, "lxml")
    results, seen = [], set()
    for h in soup.select("div.n0jPhd"):
        title = h.get_text(" ", strip=True)
        if not title or title in seen:
            continue
        a = h.find_parent("a", href=True)
        goto = base + a["href"] if a and a["href"].startswith("/goto") else None
        url = resolve_goto(goto) if (goto and resolve_links) else goto
        meta = ""
        node = h.parent
        for _ in range(4):
            node = node.parent if node else None
            if node is None:
                break
            txt = node.get_text(" ", strip=True)
            if len(txt) > len(title) + 10:
                meta = txt[:300]
                break
        seen.add(title)
        results.append({"title": title, "url": url, "meta": meta})
    return results


def parse_web(html: str, base: str = "https://www.google.com"):
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen = set()
    for h3 in soup.find_all("h3"):
        title = h3.get_text(" ", strip=True)
        if not title:
            continue
        a = h3.find_parent("a", href=True)
        if a is None:
            a = h3.find("a", href=True)
        url = clean_google_url(a["href"] if a else None)
        if not url or url in seen or url.startswith("https://support.google.com"):
            continue
        node = h3
        snippet = ""
        for _ in range(3):
            node = node.parent
            if node is None:
                break
            txt = node.get_text(" ", strip=True)
            if len(txt) > len(title) + 40 and len(txt) < 2000:
                snippet = txt
                if snippet.startswith(title):
                    snippet = snippet[len(title):].strip(" -–—|·")
                break
        seen.add(url)
        results.append({"title": title, "url": url, "snippet": snippet})
    # related searches
    related = []
    for a in soup.find_all("a", href=re.compile(r"/search\?.*q=")):
        txt = a.get_text(" ", strip=True)
        if txt and len(txt) < 120 and txt not in related and txt not in [r["title"] for r in results]:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query)
            if qs.get("q"):
                related.append(txt)
    related = related[:10]
    # total results
    total = None
    m = re.search(r"About ([\d,\.]+) results", soup.get_text(" ", strip=True))
    if m:
        total = m.group(1)
    # knowledge: featured snippet box text
    featured = None
    for sel in ["div.xpdopen", "div.hgKElc", "span.hgKElc"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 30:
            featured = el.get_text(" ", strip=True)[:2000]
            break
    if not results:
        # SERP modern (2025+) tidak lagi memakai markup h3>a klasik;
        # pakai parser markup baru supaya jalur HTTP tetap menghasilkan.
        modern = parse_serp_live(html, base)
        if modern.get("results"):
            return modern
    return {"results": results, "related_searches": related,
            "total_results": total, "featured_snippet": featured}


def parse_images(html: str):
    """Coba rg_meta klasik lalu AF_initData (ou = original, tu = thumb, pt = title, ru = page)."""
    results, seen = [], set()
    for m in re.finditer(r'class="rg_meta[^>]*>([^<]+)<', html):
        try:
            d = json.loads(ihtml.unescape(m.group(1)))
            ou = d.get("ou")
            if ou and ou not in seen:
                seen.add(ou)
                results.append({"image_url": ou, "page_url": d.get("ru"),
                                "title": d.get("pt"), "thumbnail": d.get("tu"),
                                "width": d.get("ow"), "height": d.get("oh")})
        except Exception:
            pass
    if not results:
        for m in re.finditer(r'\["(https://[^"]+\.(?:jpg|jpeg|png|webp|gif)[^"]*)",(\d+),(\d+)\]', html):
            ou = m.group(1)
            if ou not in seen:
                seen.add(ou)
                results.append({"image_url": ou, "page_url": None, "title": None,
                                "thumbnail": None, "width": int(m.group(2)),
                                "height": int(m.group(3))})
    return results


_PRICE_RE = re.compile(r"(?:US\$|U\$|\$|Rp)\s?[\d.,]+")


def parse_shopping(html: str):
    """Parser tab Shopping (udm=28, UI 2025+): kartu div.gkQHve + kontainer
    div.MUWJ8c berisi harga/merchant/rating. URL produk di-render via JS
    saat diklik, jadi tidak tersedia di HTML awal."""
    soup = BeautifulSoup(html, "lxml")
    results, seen = [], set()
    for card in soup.select("div.gkQHve"):
        title = card.get_text(" ", strip=True)
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        container = card.find_parent("div", class_="MUWJ8c") or card.parent
        info = container.get_text(" ", strip=True) if container else title
        if info.startswith(title):
            info = info[len(title):].strip()
        prices = _PRICE_RE.findall(info)
        merchant = None
        tail = _PRICE_RE.split(info)[-1] if prices else info
        tail = re.split(r"(Free delivery|Gratis ongkir|Free shipping)", tail)[0]
        tail = tail.strip(" -–—|·")
        if 2 < len(tail) < 80:
            merchant = tail
        rating = re.search(r"(\d[.,]\d)\s*\((\d[\d.,]*)\)", info)
        results.append({
            "title": title,
            "url": None,
            "price": prices[0] if prices else None,
            "was_price": prices[1] if len(prices) > 1 else None,
            "merchant": merchant,
            "rating": rating.group(1) if rating else None,
            "reviews": rating.group(2) if rating else None,
            "info": info[:300],
        })
    return results


def parse_news_rss(xml_text: str):
    root = ET.fromstring(xml_text)
    items = []
    for it in root.iter("item"):
        def txt(tag):
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        items.append({"title": txt("title"), "url": txt("link"),
                      "published": txt("pubDate"), "source": "",
                      "snippet": re.sub(r"<[^>]+>", " ", txt("description")).strip()[:500]})
        src = it.find("source")
        if src is not None and src.text:
            items[-1]["source"] = src.text.strip()
    return items


def parse_scholar(html: str, base: str = "https://scholar.google.com"):
    soup = BeautifulSoup(html, "lxml")
    results = []
    for div in soup.select("div.gs_ri"):
        h3 = div.select_one("h3.gs_rt")
        if not h3:
            continue
        title = h3.get_text(" ", strip=True)
        link = None
        a = h3.find("a", href=True)
        if a:
            link = a["href"] if a["href"].startswith("http") else base + a["href"]
        meta = div.select_one("div.gs_a")
        meta_txt = meta.get_text(" ", strip=True) if meta else ""
        snippet = div.select_one("div.gs_rs")
        snippet_txt = snippet.get_text(" ", strip=True) if snippet else ""
        cited_by = None
        cited_url = None
        pdf_url = None
        for x in div.select("div.gs_fl a"):
            t = x.get_text(strip=True)
            if t.startswith("Cited by"):
                mm = re.search(r"\d+", t)
                cited_by = int(mm.group()) if mm else None
                cited_url = base + x["href"] if x["href"].startswith("/") else x["href"]
            if t == "[PDF]" or (x.get("href", "").lower().endswith(".pdf")):
                pdf_url = x["href"] if x["href"].startswith("http") else base + x["href"]
        cluster = None
        if cited_url:
            mm = re.search(r"cites=(\d+)", cited_url)
            if mm:
                cluster = mm.group(1)
        results.append({"title": title, "url": link, "authors_venue": meta_txt,
                        "snippet": snippet_txt, "cited_by": cited_by,
                        "cited_by_url": cited_url, "cluster_id": cluster,
                        "pdf_url": pdf_url})
    return results


def parse_patents(data: dict):
    out = []
    res = (data.get("results") or {})
    clusters = res.get("cluster") or []
    for cl in clusters:
        for r in cl.get("result") or []:
            p = r.get("patent") or {}
            num = p.get("publication_number", "")
            out.append({
                "title": ihtml.unescape(re.sub(r"<[^>]+>", "", p.get("title") or "")).strip(),
                "publication_number": num,
                "url": f"https://patents.google.com/patent/{num}/en" if num else None,
                "snippet": ihtml.unescape(re.sub(r"<[^>]+>", "", p.get("snippet") or ""))[:500],
                "inventors": p.get("inventor"),
                "assignee": p.get("assignee"),
                "priority_date": p.get("priority_date"),
                "filing_date": p.get("filing_date"),
                "grant_date": p.get("grant_date"),
                "pdf_url": p.get("pdf"),
            })
    return {"total": res.get("total_num_results"), "results": out}


def parse_trends_rss(xml_text: str):
    root = ET.fromstring(xml_text)
    ns = {"ht": "https://trends.google.com/trending/rss"}
    items = []
    for it in root.iter("item"):
        def txt(tag, nsmap=None):
            el = it.find(tag, nsmap) if nsmap else it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        traffic = txt("ht:approx_traffic", ns)
        pic = it.find("ht:picture", ns)
        items.append({"title": txt("title"), "traffic": traffic,
                      "published": txt("pubDate"), "url": txt("link"),
                      "description": re.sub(r"<[^>]+>", " ", txt("description")).strip()[:400],
                      "picture": pic.text.strip() if pic is not None and pic.text else ""})
    return items


def _find_quote_entry(obj):
    """Cari entry quote di blob JSON.

    Equity: [mid, [ticker, exch], name, 0, ccy, [price, chg, pct, ...], ...].
    Forex/crypto: [mid, null, "USD / IDR", 3, null, [price, chg, pct, ...], ...].
    """
    if isinstance(obj, list):
        if (len(obj) > 7 and isinstance(obj[5], list) and obj[5]
                and isinstance(obj[5][0], (int, float))
                and isinstance(obj[2], str) and len(obj[2]) > 2):
            if isinstance(obj[1], list) and len(obj[1]) == 2:
                return obj
            if obj[1] is None:  # forex / crypto pair
                return obj
        for item in obj:
            found = _find_quote_entry(item)
            if found:
                return found
    return None


def _scan_pair_info(entry):
    """Cari simbol 'USD-IDR' dan list ['USD','IDR','United States Dollar',...]."""
    symbol = None
    pair = None

    def walk(o):
        nonlocal symbol, pair
        if isinstance(o, str):
            if symbol is None and re.fullmatch(r"[A-Z]{2,6}-[A-Z]{2,6}", o):
                symbol = o
        elif isinstance(o, list):
            if (pair is None and len(o) >= 4
                    and all(isinstance(x, str) for x in o[:4])
                    and re.fullmatch(r"[A-Z]{3}", o[0])
                    and re.fullmatch(r"[A-Z]{3}", o[1])):
                pair = o
            for x in o:
                walk(x)

    walk(entry)
    return symbol, pair


def parse_finance_quote(html: str):
    """Ambil quote dari blob AF_initData (ds:14 utama, fallback ds:8/ds:15).

    Equity entry: [mid, [ticker, exch], name, 0, ccy, [price, chg, pct, ...],
                   null, prev_close, ...].
    Forex entry:  [mid, null, "USD / IDR", 3, null, [price, chg, pct, ...],
                   null, prev_close, ...].
    """
    for key in ("ds:14", "ds:8", "ds:15"):
        m = re.search(r"key:\s*'" + key + r"'.*?data:", html, re.S)
        if not m:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(html[m.end():].lstrip())
        except Exception:
            continue
        entry = _find_quote_entry(data)
        if not entry:
            continue
        q = entry[5]
        after = entry[16] if len(entry) > 16 and isinstance(entry[16], list) and entry[16] else None
        if isinstance(entry[1], list):  # equity
            return {
                "ticker": entry[1][0], "exchange": entry[1][1],
                "name": entry[2], "currency": entry[4],
                "price": q[0], "change": q[1] if len(q) > 1 else None,
                "change_pct": q[2] if len(q) > 2 else None,
                "previous_close": entry[7] if len(entry) > 7 else None,
                "after_hours_price": after[0] if after else None,
                "after_hours_change": after[1] if after and len(after) > 1 else None,
                "after_hours_change_pct": after[2] if after and len(after) > 2 else None,
            }
        # forex / crypto pair
        symbol, pair = _scan_pair_info(entry)
        base = quote_ccy = None
        base_name = quote_name = None
        if pair:
            base, quote_ccy, base_name, quote_name = (pair + [None] * 4)[:4]
        return {
            "symbol": symbol, "pair": f"{base}/{quote_ccy}" if base else entry[2],
            "base": base, "quote_currency": quote_ccy,
            "base_name": base_name, "quote_name": quote_name,
            "name": entry[2],
            "price": q[0], "change": q[1] if len(q) > 1 else None,
            "change_pct": q[2] if len(q) > 2 else None,
            "previous_close": entry[7] if len(entry) > 7 else None,
        }
    return None
