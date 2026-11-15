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



NGO_DATE_RE = re.compile(
    r"(\d{1,2}) (" + "|".join(MONTHS_PL) + r") (\d{4})(?:,\s*(\d{1,2}:\d{2}))?"
    r"(?:\s*[-–]\s*(\d{1,2}) (" + "|".join(MONTHS_PL) + r") (\d{4})(?:,\s*(\d{1,2}:\d{2}))?)?", re.I)


def ad_ngo_pl(src):
    """Szkolenia i wydarzenia z ngo.pl (lista dla Warszawy)."""
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")
    items = {}
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
        if not re.search(r"wydarzenia\.ngo\.pl/\d+-[^/]+\.html$", href):
            continue
        it = items.setdefault(href, {"title": "", "d1": None, "d2": None})
        t = clean(a.get_text(" "))
        if len(t) > len(it["title"]):
            it["title"] = t
        if not it["d1"]:
            node = a
            for _ in range(4):
                node = node.parent
                if node is None:
                    break
                m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4}))?", node.get_text(" "))
                if m:
                    it["d1"] = dt.date(int(m[3]), int(m[2]), int(m[1]))
                    if m[4]:
                        it["d2"] = dt.date(int(m[6]), int(m[5]), int(m[4]))
                    break
    out = []
    for href, it in list(items.items())[: src.get("max", 45)]:
        title, d1, d2, t1, online, note = it["title"], it["d1"], it["d2"], "", False, ""
        try:
            page = BeautifulSoup(get(href).text, "html.parser")
            og = page.find("meta", property="og:title")
            if og and og.get("content"):
                title = re.sub(r"\s*-\s*Wydarzenie\s*$", "", og["content"]).strip()
            text = page.get_text(" ")
            m = NGO_DATE_RE.search(text)
            if m:
                d1 = dt.date(int(m[3]), MONTHS_PL[m[2].lower()], int(m[1]))
                t1 = m[4] or ""
                d2 = dt.date(int(m[7]), MONTHS_PL[m[6].lower()], int(m[5])) if m[5] else None
            online = bool(re.search(r"\b(online|on-line|zoom|webinar|webinarium|teams)\b", (title + " " + text[:6000]).lower()))
            desc = page.find("meta", attrs={"name": "description"}) or page.find("meta", property="og:description")
            note = desc["content"] if desc and desc.get("content") else ""
        except Exception as e:
            log("   ngo.pl: страница недоступна:", href, e)
        if not (title and d1):
            continue
        is_course = bool(d2 and (d2 - d1).days > src.get("max_days", 21)) or bool(re.search(r"\b(kurs|rekrutacj|nabór|akademi)", title.lower()))
        scwo = "scwo" in title.lower()
        tags = ["НКО"] + (["онлайн"] if online else []) + (["бесплатно"] if re.search(r"bezpłatn|darmow", (title + " " + note).lower()) else [])
        sub = {**src, "org": "scwo" if scwo else src.get("org", "ngopl"), "host": "SCWO" if scwo else src.get("host", "ngo.pl"),
               "tags": tags, "place_default": "Онлайн" if online else "Варшава"}
        ev = make_event(sub, title, d1, t1, d2, "", href, note)
        if ev:
            if is_course:
                ev["kind"] = "course"
            out.append(ev)
    return out


def ad_ngo_jobs(src):
    """Oferty pracy z ogloszenia.ngo.pl (Organizacja oferuje pracę, Warszawa)."""
    links, loaded = [], 0
    for n in range(1, src.get("pages", 3) + 1):
        url = src["url"] + ("" if n == 1 else ("&" if "?" in src["url"] else "?") + f"page={n}")
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except Exception as e:
            log("   ngo.pl praca: страница списка недоступна:", url, e)
            continue
        loaded += 1
        for a in soup.find_all("a", href=True):
            href = requests.compat.urljoin(url, a["href"]).split("#")[0].split("?")[0]
            if re.search(r"ogloszenia\.ngo\.pl/\d+-[^/]+\.html$", href) and href not in links:
                links.append(href)
    if not loaded:
        raise RuntimeError("список вакансий не открылся")
    out = []
    for href in links[: src.get("max", 45)]:
        try:
            page = BeautifulSoup(get(href).text, "html.parser")
        except Exception as e:
            log("   вакансия недоступна:", href, e)
            continue
        og = page.find("meta", property="og:title")
        title = re.sub(r"\s*-\s*Ogłoszenie\s*$", "", og["content"]).strip() if og and og.get("content") else clean(page.find("h2").get_text(" ") if page.find("h2") else "")
        text = re.sub(r"\s+", " ", page.get_text(" "))
        mv = re.search(r"Ważność ogłoszenia:\s*od\s+(\d{1,2}) (\w+) (\d{4})\s+do\s+(\d{1,2}) (\w+) (\d{4})", text)
        posted = deadline = None
        if mv and mv[2].lower() in MONTHS_PL and mv[5].lower() in MONTHS_PL:
            posted = dt.date(int(mv[3]), MONTHS_PL[mv[2].lower()], int(mv[1]))
            deadline = dt.date(int(mv[6]), MONTHS_PL[mv[5].lower()], int(mv[4]))
        else:
            m = DATE_PL_RE.search(text)
            if m:
                posted = dt.date(int(m[4]), MONTHS_PL[m[3].lower()], int(m[1]))
        if not (title and posted):
            continue
        if deadline and deadline < TODAY:
            continue
        if not deadline and posted < TODAY - dt.timedelta(days=45):
            continue
        place = "Вся Польша / удалённо" if re.search(r"Cała Polska|zdaln", text[:3000]) else "Варшава"
        bits = []
        for label in ("Wynagrodzenie", "Rodzaj umowy", "Wymiar"):
            mm = re.search(label + r":\s*(.{3,80}?)(?=\s+[A-ZŁŚŻĆ][a-ząćęłńóśźż ]{2,30}:|\s{2,}|\s\.|$)", text)
            if mm:
                bits.append(mm[1].strip(" .·-"))
        desc = page.find("meta", attrs={"name": "description"}) or page.find("meta", property="og:description")
        note = (" · ".join(bits) + (". " if bits else "")) + (desc["content"] if desc and desc.get("content") else "")
        org = re.search(r"((?:Fundacja|Stowarzyszenie|Polska Akcja|Polski Czerwony|Instytut|Centrum|Akademia|Mazowiecki|Dom|Ośrodek|Związek|Towarzystwo)[^.,:;!?()\n]{3,70})", text[text.find(title) + len(title):][:1500] if title in text else "")
        host = re.split(r"\s+(?:poszukuje|zatrudni|prowadzi|ogłasza|szuka|jest|zaprasza|działa|od\s)", clean(org[1]))[0] if org else ""
        sub = {**src, "host": host, "tags": ["вакансия"]}
        ev = make_event(sub, title, posted, "", deadline, place, href, note)
        if ev:
            ev["kind"] = "job"
            out.append(ev)
    return out


MONTHS_ALL = dict(MONTHS_PL)
MONTHS_ALL.update({
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
})
TG_DATE_RE = re.compile(
    r"(\d{1,2})(?:\s*[–\-]\s*(\d{1,2}))?\s+(" + "|".join(MONTHS_ALL) + r")(?:\s+(\d{4}))?", re.I)
TG_NUM_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def ad_telegram(src):
    """Публичный Telegram-канал через веб-превью t.me/s/<канал>: посты с датой будущего события."""
    url = f"https://t.me/s/{src['channel']}"
    soup = BeautifulSoup(get(url).text, "html.parser")
    msgs = soup.select(".tgme_widget_message")
    if not msgs:
        raise RuntimeError("в превью канала нет постов")
    out = []
    for m in msgs:
        tnode = m.select_one(".tgme_widget_message_text")
        if not tnode:
            continue
        for br in tnode.find_all("br"):
            br.replace_with("\n")
        text = tnode.get_text()
        timetag = m.select_one("time[datetime]")
        posted = dt.date.fromisoformat(timetag["datetime"][:10]) if timetag else TODAY
        link = m.select_one("a.tgme_widget_message_date")
        href = link["href"] if link and link.get("href") else url
        d1 = d2 = None
        md = TG_DATE_RE.search(text)
        if md:
            mon = MONTHS_ALL[md[3].lower()]
            year = int(md[4]) if md[4] else posted.year
            try:
                d1 = dt.date(year, mon, int(md[1]))
                if not md[4] and d1 < posted - dt.timedelta(days=7):
                    d1 = d1.replace(year=year + 1)
                if md[2]:
                    d2 = d1.replace(day=int(md[2]))
            except ValueError:
                d1 = None
        if not d1:
            mn = TG_NUM_DATE_RE.search(text)
            if mn and 1 <= int(mn[2]) <= 12:
                year = int(mn[3]) + (2000 if mn[3] and len(mn[3]) == 2 else 0) if mn[3] else posted.year
                try:
                    d1 = dt.date(year, int(mn[2]), int(mn[1]))
                    if not mn[3] and d1 < posted - dt.timedelta(days=7):
                        d1 = d1.replace(year=year + 1)
                except ValueError:
                    d1 = None
        if not d1 or d1 < posted - dt.timedelta(days=1):
            continue  # пост без будущей даты — не мероприятие
        after = text[md.end():] if md else text
        tm = TIME_RE.search(after[:120]) or TIME_RE.search(text)
        t1 = f"{int(tm[1]):02d}:{tm[2]}" if tm else ""
        lines = [re.sub(r"^[^\wА-Яа-яЁёІіЇїЄєҐґ«\"]+", "", l).strip() for l in text.splitlines()]
        lines = [l for l in lines if len(l) > 3]
        if not lines:
            continue
        title = lines[0][:120]
        note = " ".join(lines[1:])[:400]
        low = text.lower()
        online = bool(re.search(r"онлайн|online|zoom|teams|вебінар|вебинар|webinar", low))
        place = "Онлайн" if online else ("Варшава" if re.search(r"варшав|warszaw", low) else src.get("place_default", "Место уточните по ссылке"))
        tags = list(src.get("tags", [])) + (["онлайн"] if online else []) + (["бесплатно"] if re.search(r"безкоштовн|безоплатн|бесплатн|bezpłatn", low) else [])
        ev = make_event({**src, "tags": tags}, title, d1, t1, d2, place, href, note)
        if ev:
            if re.search(r"\bкурс|навчальн[аи] програм", low[:300]):
                ev["kind"] = "course"
            out.append(ev)
    return out


def ad_ngo_funds(src):
    """Открытые конкурсы грантов с fundusze.ngo.pl/aktualne."""
    items, loaded = {}, 0
    for n in range(1, src.get("pages", 3) + 1):
        url = src["url"] + ("" if n == 1 else ("&" if "?" in src["url"] else "?") + f"page={n}")
        try:
            soup = BeautifulSoup(get(url).text, "html.parser")
        except Exception as e:
            log("   fundusze.ngo.pl: страница недоступна:", url, e)
            continue
        loaded += 1
        for a in soup.find_all("a", href=True):
            href = requests.compat.urljoin(url, a["href"]).split("#")[0].split("?")[0]
            if not re.search(r"fundusze\.ngo\.pl/\d+-[^/]+\.html$", href) or href in items:
                continue
            node, card = a, None
            for _ in range(6):
                node = node.parent
                if node is None:
                    break
                if re.search(r"Nabór do|Data zakończenia", node.get_text(" ")):
                    card = node
                    break
            text = re.sub(r"\s+", " ", (card or a).get_text(" "))
            title = clean(a.get_text(" "))
            if len(title) < 8 and card:
                h = card.find(["h2", "h3", "h4"])
                title = clean(h.get_text(" ")) if h else title
            items[href] = (title, text)
    if not loaded:
        raise RuntimeError("список конкурсов не открылся")
    locs = [l.lower() for l in src.get("locations", ["Cała Polska", "Warszawa", "mazowieckie"])]
    out = []
    for href, (title, text) in items.items():
        md = re.search(r"Nabór do\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if not (title and md):
            continue
        deadline = dt.date(int(md[3]), int(md[2]), int(md[1]))
        if deadline < TODAY:
            continue
        ml = re.search(r"Lokalizacja\s+(.+?)\s+(?:Data|Nabór|Rodzaj)", text)
        loc = ml[1].strip() if ml else ""
        mt = re.search(r"Rodzaj funduszu\s+(\S+)", text)
        ftype = mt[1].strip() if mt else ""
        mb = re.search(r"Łączny budżet(?: funduszu)?\s+(?:Łączny budżet\s+)?([\d ,.]+\s*(?:mln|tys\.?)?\s*(?:PLN|zł|EUR|€)?)", text)
        budget = mb[1].strip() if mb else ""
        if locs and loc and not any(l in loc.lower() for l in locs) and ftype != "zagraniczny":
            continue
        note = " · ".join(x for x in [("бюджет " + budget) if budget else "", ftype] if x)
        ev = make_event({**src, "tags": ["грант"] + ([ftype] if ftype else [])}, title, TODAY, "", deadline, loc or "Польша", href, note)
        if ev:
            ev["kind"] = "grant"
            out.append(ev)
    return out


ADAPTERS = {"tribe": ad_tribe, "ics": ad_ics, "jsonld_list": ad_jsonld_list,
            "rpo_patronaty": ad_rpo_patronaty, "rpo_news": ad_rpo_news, "ngo_pl": ad_ngo_pl, "ngo_jobs": ad_ngo_jobs, "telegram": ad_telegram, "ngo_funds": ad_ngo_funds}


# ---------- сборка ----------

def norm_key(e):
    t = re.sub(r"[^a-z0-9ąćęłńóśźżа-яё]", "", e["title"].lower())[:40]
    return (t, e["start"])


def keep(e):
    start = dt.date.fromisoformat(e["start"])
    if e.get("kind") in ("course", "job", "grant"):
        end = dt.date.fromisoformat(e["end"]) if e.get("end") else start
        return end >= TODAY and start <= KEEP_TO
    return KEEP_FROM <= start <= KEEP_TO


def words(t):
    return {w for w in re.findall(r"[a-ząćęłńóśźżа-яё0-9]+", t.lower()) if len(w) > 3}


def is_dup(a, b):
    ka, kb = norm_key(a), norm_key(b)
    if ka == kb:
        return True
    if a.get("kind") == "grant" and b.get("kind") == "grant" and a.get("end") and a.get("end") == b.get("end"):
        wa, wb = words(a["title"]), words(b["title"])
        if ka[0][:12] == kb[0][:12] or (wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.5):
            return True
    if a["start"] != b["start"]:
        return False
    ta, tb = ka[0], kb[0]
    if ta and tb and (ta[:15] in tb or tb[:15] in ta):
        return True
    wa, wb = words(a["title"]), words(b["title"])
    if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.6:
        return True
    return a.get("org") == b.get("org") and bool(ta and tb) and (ta[:10] in tb or tb[:10] in ta)


def run_source(src):
    res = None
    try:
        res = ADAPTERS[src["type"]](src)
    except Exception as e:
        log(f"   {src['id']} ({src['type']}): {e}")
    fb = src.get("fallback")
    if fb and not res:  # основной способ упал или ничего не нашёл — пробуем запасной
        try:
            res2 = ADAPTERS[fb["type"]]({**src, **fb})
            if res2 or res is None:
                res = res2
        except Exception as e2:
            log(f"   {src['id']} запасной способ ({fb['type']}): {e2}")
    return res


def main():
    sources = json.load(open("sources.json", encoding="utf-8"))["sources"]
    manual = json.load(open("manual.json", encoding="utf-8"))["events"]
    try:
        prev = json.load(open("events.json", encoding="utf-8"))
    except Exception:
        prev = {"events": [], "rpo_news": []}

    collected, news, status, replaced = [], prev.get("rpo_news", []), {}, set()
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
            res = [e for e in res if keep(e)]
            collected += res
            if res:
                replaced |= set(src.get("replaces_ids", []))
            status[src["id"]] = f"ok: {len(res)}"
        log("   ", status[src["id"]])

    manual = [e for e in manual if e.get("id") not in replaced]  # старые ручные записи, которые теперь собираются сами
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
