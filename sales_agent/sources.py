from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "MediasalesResearch/1.0 (+public-news-research)"}
SIGNALS = re.compile(r"запуск|откр[ыи]|нов[а-яё]* (?:продукт|филиал|магазин|услуг|бренд|рынок)|"
    r"вых[оа]д|выш[её]л|расшир|инвест|привлек|привлёк|партн[её]р|представ|выставк|"
    r"фестивал|конференц|ребренд|франшиз|производств|startap|launch|open|yangi|ochild|"
    r"ishga tush|hamkor|kengay|нов[а-яё]* (?:карт|кредит|вклад|сервис)", re.I)
NEGATIVE = re.compile(r"(?:скончал|погиб|убийств|теракт|авари[яи]|уголовн|похорон)", re.I)


def fetch(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS sources are supported")
    import ipaddress
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        ip = None
    if parsed.hostname in {"localhost", "metadata.google.internal"} or ip and not ip.is_global:
        raise ValueError("Non-public source")
    with requests.get(url, headers=HEADERS, timeout=(10, 35), stream=True, allow_redirects=False) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("Source moved; update its configured URL")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 3_000_000:
                raise ValueError("Source exceeds size limit")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8-sig", errors="replace")


def clean(value):
    return re.sub(r"\s+", " ", BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)).strip()


def parse_date(value):
    try:
        date = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        date = parsedate_to_datetime(value)
    if date.tzinfo is None:
        raise ValueError("Source date has no timezone")
    return date.astimezone(timezone.utc).isoformat()


def parse_feed(raw, origin):
    root = ET.fromstring(raw)
    result = []
    for node in [*root.findall(".//item"), *root.findall("{http://www.w3.org/2005/Atom}entry")]:
        fields = {child.tag.split("}")[-1]: child for child in node}
        def value(*names, fields=fields):
            return next((fields[name].text for name in names if name in fields and fields[name].text), "")
        link = value("link").strip() or next((child.get("href","") for child in node
            if child.tag.split("}")[-1]=="link" and child.get("rel","alternate")=="alternate"), "")
        if not link.startswith("https://"):
            continue
        try:
            date = parse_date(value("pubDate", "published", "updated", "date"))
        except (ValueError, TypeError, OverflowError):
            continue
        title = clean(value("title"))
        body = value("encoded", "description", "summary", "content")
        links = [a.get("href") for a in BeautifulSoup(body, "html.parser").select("a[href]") if a.get("href", "").startswith("https://")]
        result.append(dict(url=link, title=title, text=title + ". " + clean(body), published=date, origin=origin, links=links))
    return result


def telegram_page(raw, channel):
    soup = BeautifulSoup(raw, "html.parser")
    result = []
    for node in soup.select(".tgme_widget_message[data-post]"):
        post = node.get("data-post", "")
        text = node.select_one(".tgme_widget_message_text")
        time = node.select_one(".tgme_widget_message_date time[datetime]") or node.select_one("time[datetime]")
        if not time or not re.fullmatch(re.escape(channel) + r"/\d+", post, flags=re.I):
            continue
        try:
            published = parse_date(time.get("datetime", ""))
        except (ValueError, TypeError, OverflowError):
            continue
        body = clean(str(text)) if text else ""
        result.append(dict(url="https://t.me/" + post, title=body[:180], text=body, published=published,
            origin="Telegram @" + channel, links=[a["href"] for a in text.select("a[href]") if a["href"].startswith("https://")] if text else []))
    before = None
    for anchor in soup.select('a[href*="before="]'):
        match = re.fullmatch(r'(?:https://t\.me)?/s/'+re.escape(channel)+r'\?before=(\d+)', anchor['href'], re.I)
        if match:
            before = int(match[1])
            break
    return result, before


def parse_telegram(raw, channel):
    return [item for item in telegram_page(raw, channel)[0] if item["text"]]


def save_items(store, items, since, until):
    accepted = 0
    for item in items:
        if item["text"] and since <= datetime.fromisoformat(item["published"]) <= until:
            store.add_source(item)
            accepted += 1
    return accepted


def collect_channel(store, url, channel, since, until):
    key = 'telegram_history:' + channel.lower()
    state = json.loads(store.state(key, '{}'))
    head = state.get('head', 0)
    tip = state.get('tip', 0)
    resume = state.get('before')
    total = accepted = 0
    before = None
    # Always poll the current page. Walk older pages incrementally across restarts.
    for _ in range(3):
        items, older = telegram_page(fetch(url if before is None else f'{url}?before={before}'), channel)
        if not items:
            raise ValueError("Telegram page contains no dated posts")
        ids = [int(item["url"].rsplit('/', 1)[1]) for item in items]
        total += len(items)
        accepted += save_items(store, items, since, until)
        if before is None and resume is not None:
            before = resume
            continue
        if before is None:
            tip = max(ids)
        elif min(ids) >= before or (older is not None and older >= before):
            raise ValueError("Telegram history did not advance")
        if older is None or older <= head or max(datetime.fromisoformat(i['published']) for i in items) < since:
            store.set_state(key, json.dumps({'head': max(head, tip)}))
            return total, accepted
        before = older
        store.set_state(key, json.dumps({'head': head, 'tip': tip, 'before': before}))
    return total, accepted


def collect(cfg, store, now):
    since = now - timedelta(days=cfg.lookback_days)
    sources = [(url, "rss", url) for url in cfg.feeds]
    for channel in cfg.channels:
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", channel):
            sources.append(("https://t.me/s/" + channel, "telegram", channel))
    total = 0
    for url, kind, label in sources:
        try:
            until = now + timedelta(minutes=10)
            if kind == 'telegram':
                count, accepted = collect_channel(store, url, label, since, until)
            else:
                items = parse_feed(fetch(url), label)
                if not items:
                    raise ValueError("No dated RSS items")
                count, accepted = len(items), save_items(store, items, since, until)
            store.source_health(url, "ok", f"Прочитано {count}, в периоде {accepted}")
            total += accepted
        except Exception as exc:
            store.source_health(url, "error", type(exc).__name__)
    return total


def relevant(item):
    from .rules import EVENTS,is_advertisement
    return (is_advertisement(item['text']) or any(pattern.search(item['text']) for _,pattern in EVENTS)) and not NEGATIVE.search(item["title"])
