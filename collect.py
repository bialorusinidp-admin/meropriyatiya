#!/usr/bin/env python3
"""Робот, который собирает мероприятия с сайтов организаций.

Запускается GitHub Actions каждый день (.github/workflows/collect.yml).
Читает sources.json (откуда собирать) и manual.json (события, добавленные вручную),
пишет events.json — его и показывает приложение.

Если какой-то сайт не ответил или поменял вёрстку, события этого источника
берутся из прошлого events.json, чтобы подборка не опустела.
"""
import datetime as dt
import html
import json
import re
import sys
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("Europe/Warsaw")
NOW = dt.datetime.now(TZ)
TODAY = NOW.date()
KEEP_FROM = TODAY - dt.timedelta(days=60)
KEEP_TO = TODAY + dt.timedelta(days=150)
UA = {"User-Agent": "Mozilla/5.0 (compatible; prava-events-bot/1.0; +https://github.com)",
      "Accept-Language": "pl,en;q=0.8"}

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6, "lipca": 7,
    "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10, "pazdziernika": 10,
    "listopada": 11, "grudnia": 12,
}
DATE_PL_RE = re.compile(
    r"(\d{1,2})(?:\s*[–\-]\s*(\d{1,2}))?\s+(" + "|".join(MONTHS_PL) + r")\s+(\d{4})", re.I)


def log(*a):
    print(*a, file=sys.stderr)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=25, **kw)
    r.raise_for_status()
    return r


def clean(s):
    if not s:
        return ""
    s = BeautifulSoup(html.unescape(str(s)), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", s).strip()


def short(s, n=220):
    s = clean(s)
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def parse_dt(s):
    """'2026-11-19T18:00:00+01:00' / '2026-11-19 18:00:00' / '20261119T180000' / '20261119' -> (date, 'HH:MM' or '')."""
    if not s:
        return None, ""
    s = str(s).strip()
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})(?:[T ](\d{2}):?(\d{2}))?", s)
    if not m:
        return None, ""
    d = dt.date(int(m[1]), int(m[2]), int(m[3]))
    t = f"{m[4]}:{m[5]}" if m[4] else ""
    if t == "00:00":
        t = ""
    return d, t


def slug(s):
    s = re.sub(r"[^a-z0-9ąćęłńóśźżа-яё]+", "-", s.lower()).strip("-")
    return s[:50]


def make_event(src, title, start, time="", end=None, place="", url="", note=""):
    if not title or not start:
        return None
    return {
        "id": f"{src['id']}-{start.isoformat()}-{slug(title)[:30]}",
        "org": src.get("org", "other"),
        "title": clean(title),
        "start": start.isoformat(),
        "end": end.isoformat() if end and end != start else "",
        "time": time,
        "place": clean(place) or src.get("place_default", "Место уточните по ссылке"),
        "host": src.get("host", ""),
        "tags": list(src.get("tags", [])),
        "url": url,
        "note": short(note),
        "ok": True,
        "source": src["id"],
    }


# ---------- адаптеры ----------

def ad_tribe(src):
    """Сайты на WordPress с плагином The Events Calendar."""
    url = src["base"].rstrip("/") + "/wp-json/tribe/events/v1/events"
    params = {"start_date": TODAY.isoformat(), "end_date": KEEP_TO.isoformat(), "per_page": 50}
    out, pages = [], 0
    while url and pages < 5:
        j = get(url, params=params).json()
        params = None
        for e in j.get("events", []):
            d1, t1 = parse_dt(e.get("start_date"))
            d2, _ = parse_dt(e.get("end_date"))
            v = e.get("venue") or {}
            if isinstance(v, list):
                v = v[0] if v else {}
            place = ", ".join(x for x in [v.get("venue"), v.get("address"), v.get("city")] if x)
            if e.get("all_day"):
                t1 = ""
            ev = make_event(src, e.get("title"), d1, t1, d2, place, e.get("url", ""), e.get("excerpt") or e.get("description"))
            if ev:
                out.append(ev)
        url = j.get("next_rest_url")
        pages += 1
    return out


def ad_ics(src):
    """Любой календарь в формате iCal (.ics)."""
    text = get(src["url"]).text
    text = re.sub(r"\r?\n[ \t]", "", text)  # склеиваем перенесённые строки
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        f = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            f.setdefault(k.split(";")[0].upper(), v.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";"))
        d1, t1 = parse_dt(f.get("DTSTART"))
        d2, _ = parse_dt(f.get("DTEND"))
        if d2 and "VALUE=DATE" in block and d2 > d1:
            d2 = d2 - dt.timedelta(days=1)
        ev = make_event(src, f.get("SUMMARY"), d1, t1, d2, f.get("LOCATION", ""), f.get("URL", ""), f.get("DESCRIPTION", ""))
        if ev:
            out.append(ev)
    return out


def iter_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                if "@graph" in x:
                    stack.extend(x["@graph"] if isinstance(x["@graph"], list) else [x["@graph"]])
                yield x


def event_from_page(src, url, page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for x in iter_jsonld(soup):
        types = x.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t and "Event" in str(t) for t in types):
            d1, t1 = parse_dt(x.get("startDate"))
            d2, _ = parse_dt(x.get("endDate"))
            loc = x.get("location") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            place = ""
            if isinstance(loc, dict):
                addr = loc.get("address")
                if isinstance(addr, dict):
                    addr = ", ".join(v for v in [addr.get("streetAddress"), addr.get("addressLocality")] if v)
                place = ", ".join(v for v in [loc.get("name"), addr if isinstance(addr, str) else ""] if v)
            return make_event(src, x.get("name"), d1, t1, d2, place, x.get("url") or url, x.get("description"))
    if not src.get("heuristic"):
        return None
    # запасной вариант: заголовок страницы и первая польская дата в тексте
    og = soup.find("meta", property="og:title")
    title = og["content"] if og and og.get("content") else (soup.h1.get_text() if soup.h1 else "")
    main = soup.find("main") or soup.find("article") or soup.body or soup
    m = DATE_PL_RE.search(main.get_text(" "))
    if not (title and m):
        return None
    y, mon = int(m[4]), MONTHS_PL[m[3].lower()]
    d1 = dt.date(y, mon, int(m[1]))
    d2 = dt.date(y, mon, int(m[2])) if m[2] else None
    tm = re.search(r"(?:g\.|godz\.?)\s*(\d{1,2})[.:](\d{2})", main.get_text(" "))
    t1 = f"{int(tm[1]):02d}:{tm[2]}" if tm else ""
    desc = soup.find("meta", attrs={"name": "description"})
    return make_event(src, title, d1, t1, d2, "", url, desc["content"] if desc and desc.get("content") else "")


def ad_jsonld_list(src):
    """Страница-список -> ссылки на события -> разметка schema.org Event на каждой."""
    nxt = (TODAY.replace(day=1) + dt.timedelta(days=32))
    links, loaded = [], 0
    for p in src["pages"]:
        for y, m in [(TODAY.year, TODAY.month), (nxt.year, nxt.month)]:
            url = p.replace("{Y}", str(y)).replace("{M}", f"{m:02d}")
            try:
                soup = BeautifulSoup(get(url).text, "html.parser")
            except Exception as e:
                log("   страница списка недоступна:", url, e)
                continue
            loaded += 1
            for a in soup.find_all("a", href=True):
                href = requests.compat.urljoin(url, a["href"]).split("#")[0]
                if re.search(src["link_re"], href) and href not in links:
                    links.append(href)
            if "{Y}" not in p:
                break
    if not loaded:
        raise RuntimeError("ни одна страница списка не открылась")
    out = []
    for href in links[: src.get("max", 40)]:
        try:
            ev = event_from_page(src, href, get(href).text)
            if ev:
                out.append(ev)
        except Exception as e:
            log("   событие недоступно:", href, e)
    return out


def ad_rpo_patronaty(src):
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(src["url"], a["href"])
        if "/pl/content/patronaty-rpo-" in href and href not in links:
            links.append(href)
    out = []
    for href in links[:25]:
        try:
            page = BeautifulSoup(get(href).text, "html.parser")
        except Exception as e:
            log("   патронат недоступен:", href, e)
            continue
        title = page.h1.get_text(" ") if page.h1 else ""
        title = re.sub(r"\s*[-–]?\s*pod Honorowym Patronatem (RPO|Rzecznika Praw Obywatelskich)\.?\s*$", "", clean(title), flags=re.I)
        body = (page.find("article") or page.find("main") or page).get_text(" ")
        m = DATE_PL_RE.search(body)
        if not m:
            continue
        y, mon = int(m[4]), MONTHS_PL[m[3].lower()]
        d1 = dt.date(y, mon, int(m[1]))
        d2 = dt.date(y, mon, int(m[2])) if m[2] else None
        ev = make_event(src, title, d1, "", d2, "", href, "")
        if ev:
            out.append(ev)
    return out


def ad_rpo_news(src):
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(src["url"], a["href"])
        title = clean(a.get_text(" "))
        if "/pl/content/" not in href or len(title) < 25 or href in seen:
            continue
        node, date = a, None
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", node.get_text(" "))
            if m:
                date = dt.date(int(m[1]), int(m[2]), int(m[3]))
                break
        if not date:
            continue
        seen.add(href)
        months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
        out.append({"date": f"{date.day} {months[date.month - 1]}", "iso": date.isoformat(), "title": title, "url": href})
        if len(out) >= 6:
            break
    return out


ADAPTERS = {"tribe": ad_tribe, "ics": ad_ics, "jsonld_list": ad_jsonld_list,
            "rpo_patronaty": ad_rpo_patronaty, "rpo_news": ad_rpo_news}


# ---------- сборка ----------

def norm_key(e):
    t = re.sub(r"[^a-z0-9ąćęłńóśźżа-яё]", "", e["title"].lower())[:40]
    return (t, e["start"])


def is_dup(a, b):
    ka, kb = norm_key(a), norm_key(b)
    if ka == kb:
        return True
    if a["start"] != b["start"] or a.get("org") != b.get("org"):
        return False
    ta, tb = ka[0], kb[0]
    return bool(ta and tb) and (ta[:10] in tb or tb[:10] in ta)


def run_source(src):
    try:
        return ADAPTERS[src["type"]](src)
    except Exception as e:
        log(f"   {src['id']} ({src['type']}): {e}")
        fb = src.get("fallback")
        if fb:
            merged = {**src, **fb}
            try:
                return ADAPTERS[fb["type"]](merged)
            except Exception as e2:
                log(f"   {src['id']} запасной способ ({fb['type']}): {e2}")
        return None


def main():
    sources = json.load(open("sources.json", encoding="utf-8"))["sources"]
    manual = json.load(open("manual.json", encoding="utf-8"))["events"]
    try:
        prev = json.load(open("events.json", encoding="utf-8"))
    except Exception:
        prev = {"events": [], "rpo_news": []}

    collected, news, status = [], prev.get("rpo_news", []), {}
    for src in sources:
        if src.get("disabled"):
            continue
        log(f"→ {src['id']}")
        res = run_source(src)
        if src["type"] == "rpo_news":
            if res:
                news = res
            status[src["id"]] = f"ok: {len(res)}" if res else "ошибка, оставлены прошлые новости"
            continue
        if res is None:
            old = [e for e in prev.get("events", []) if e.get("source") == src["id"]]
            collected += old
            status[src["id"]] = f"ошибка, оставлено прошлых: {len(old)}"
        else:
            res = [e for e in res if KEEP_FROM <= dt.date.fromisoformat(e["start"]) <= KEEP_TO]
            collected += res
            status[src["id"]] = f"ok: {len(res)}"
        log("   ", status[src["id"]])

    for e in manual:
        e.setdefault("source", "manual")
    events = []
    for e in manual + collected:  # ручные события важнее собранных
        if any(is_dup(e, x) for x in events):
            continue
        events.append(e)
    events.sort(key=lambda e: e["start"])

    json.dump({"updated": NOW.isoformat(timespec="minutes"), "status": status, "events": events, "rpo_news": news},
              open("events.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"Готово: {len(events)} событий")


if __name__ == "__main__":
    main()
