"""Conservative extraction of placement briefs; source text is data, never code."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from zoneinfo import ZoneInfo

from .parsing import PartnerReference


@dataclass(frozen=True)
class Brief:
    name: str
    ad_type: str
    terms: str
    start: str
    end: str
    retention_hours: float | None
    keywords: tuple[str, ...]
    domains: tuple[str, ...]
    review_reason: str
    default_window: bool
    client_name: str = ""
    language: str = ""
    planned_posts: int | None = None


_DATE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)")
_HOURS = re.compile(r"(?<![\d.,])([0-9]+(?:[.,][0-9]+)?)\s*(?:час(?:а|ов)?|ч\.?|hours?|h)\b", re.I)
_FIELDS = {"клиент", "заказчик", "язык", "план публикаций", "количество публикаций", "название рекламы", "реклама", "бренд", "продукт", "услуга", "название", "тип рекламы", "тип", "формат",
           "условия", "искать", "ключевые слова", "текст рекламы", "домен", "домены", "сайт", "канал", "партнер",
           "длительность", "длительность размещения", "срок размещения", "на сколько часов", "начало", "начало публикации",
           "окончание", "конец", "опубликовать до", "период публикации", "размещение", "дата публикации", "дата", "период", "срок публикации"}


def parse_time(value: str, tz: str) -> datetime:
    """CLI timestamps: explicit full date, optionally time and UTC offset."""
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                pass
        if parsed is None:
            raise ValueError("Дата: YYYY-MM-DD HH:MM или DD.MM.YYYY HH:MM; год обязателен")
    if parsed.tzinfo is None:
        local = parsed.replace(tzinfo=ZoneInfo(tz))
        # Reject ambiguous/nonexistent local times rather than silently choose a fold.
        if local.utcoffset() != local.replace(fold=1).utcoffset():
            raise ValueError("Неоднозначное местное время; укажите UTC offset")
        parsed = local
    return parsed.astimezone(timezone.utc)


def _fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    pending = None
    for line in text.splitlines():
        line = re.sub(r"^\s*[-•*]\s*", "", line).strip()
        match = re.match(r"^([^:\t]{2,40})(?::|\t)\s*(.*)$", line)
        key = (match[1] if match else line.rstrip(":")).strip().casefold().replace("ё", "е")
        if key in _FIELDS:
            value = match[2].strip() if match else ""
            if value:
                result[key] = (result.get(key, "") + "\n" + value).strip()
            pending = key if not value or key in {"условия", "текст рекламы"} else None
        elif pending and line:
            result[pending] = (result.get(pending, "") + "\n" + line).strip()
            if pending not in {"условия", "текст рекламы"}:
                pending = None
    return result


def parse_brief(text: str, refs: list[PartnerReference], received: datetime,
                tz: str, default_days: int, fallback_keywords=(), fallback_domains=()) -> Brief:
    fields = _fields(text)
    reasons: list[str] = []
    client = fields.get("клиент") or fields.get("заказчик", "")
    language = fields.get("язык", "").upper()
    planned = fields.get("план публикаций") or fields.get("количество публикаций")
    planned_posts = None
    if planned:
        if re.fullmatch(r"\d+", planned) and 0 < int(planned) <= 100000:
            planned_posts = int(planned)
        else:
            reasons.append("План публикаций: укажите одно положительное целое число")
    if "\n" in client:
        reasons.append("В источнике несколько клиентов; разделите заявки")
    targets = {r.username or (f"c:{r.private_channel_id}" if r.private_channel_id else r.source_url) for r in refs}
    if len(targets) > 1:
        reasons.append("В одном сообщении несколько каналов: уточните условия для каждого")
    name = (fields.get("название рекламы") or fields.get("реклама") or fields.get("бренд")
            or fields.get("продукт") or fields.get("услуга") or fields.get("название", ""))
    if "\n" in name:
        reasons.append("В источнике несколько названий рекламы; разделите заявки")
    ad_type = fields.get("тип рекламы") or fields.get("тип") or fields.get("формат", "")
    terms = fields.get("условия", "")
    cues = fields.get("искать") or fields.get("ключевые слова")
    creative = fields.get("текст рекламы") if not cues else None
    if not cues:
        cues = name
    if creative:
        keywords = (creative,)
    elif cues:
        keywords = tuple(x.strip() for x in re.split(r"[;\n]", cues) if x.strip())
    else:
        keywords = tuple(fallback_keywords)
    domain_text = fields.get("домен") or fields.get("домены") or fields.get("сайт")
    domains = tuple(x.strip() for x in re.split(r"[,;\s]+", domain_text) if x.strip()) if domain_text else tuple(fallback_domains)
    if refs and not keywords and not domains and not all(r.message_id for r in refs):
        reasons.append("Нужен признак рекламы (Искать / Бренд / Название рекламы / Домен) или прямая ссылка на пост")

    retention = fields.get("длительность") or fields.get("длительность размещения") or fields.get("срок размещения") or fields.get("на сколько часов")
    if not retention:
        match = re.search(r"\b(?:на|размещение\s+на)\s+(\d+(?:[.,]\d+)?\s*(?:час(?:а|ов)?|ч\b))", text, re.I)
        retention = match[1] if match else ""
    retention_hours = None
    if retention:
        values = _HOURS.findall(retention)
        if len(values) == 1 and not re.search(r"\d", _HOURS.sub("", retention)):
            retention_hours = float(values[0].replace(",", "."))
        if retention_hours is None or not math.isfinite(retention_hours) or not 0 < retention_hours <= 8760:
            retention_hours = None
            reasons.append("Неясна длительность размещения; укажите, например, Длительность: 24 часа")

    received = received.astimezone(timezone.utc)
    start, end = received, received + timedelta(days=default_days)
    default_window = True
    start_value = fields.get("начало") or fields.get("начало публикации")
    end_value = fields.get("окончание") or fields.get("конец") or fields.get("опубликовать до")
    schedule = fields.get("период публикации") or fields.get("размещение") or fields.get("дата публикации") or fields.get("дата") or fields.get("период")
    deadline = fields.get("срок публикации")
    try:
        if start_value or end_value:
            default_window = False
            if start_value:
                start = parse_time(start_value, tz)
            if end_value:
                end = parse_time(end_value, tz)
            else:
                raise ValueError("Укажите окончание периода публикации")
        elif schedule:
            default_window = False
            dates = list(_DATE.finditer(schedule))
            remaining = _DATE.sub("", schedule)
            remaining = re.sub(r"\b(?:с|по|до)\b", "", remaining, flags=re.I)
            if len(dates) not in {1, 2} or re.search(r"[\dА-Яа-яA-Za-z]", remaining):
                raise ValueError("Неясный период публикации; укажите полные даты с годом")
            start = parse_time(dates[0][0], tz)
            end = parse_time(dates[-1][0], tz)
            if not re.search(r"\d:\d", dates[-1][0]):
                # A date-only end includes that entire local calendar day.
                end = (end.astimezone(ZoneInfo(tz)) + timedelta(days=1)).astimezone(timezone.utc)
        elif deadline:
            default_window = False
            values = _HOURS.findall(deadline)
            if len(values) != 1 or re.search(r"\d", _HOURS.sub("", deadline)) or not 0 < float(values[0].replace(",", ".")) <= 8760:
                raise ValueError("Срок публикации: укажите число часов")
            end = received + timedelta(hours=float(values[0].replace(",", ".")))
        elif re.search(r"\b(?:сегодня|завтра|послезавтра|в\s+пятницу|на\s+следующей)\b|\d{1,2}\.\d{1,2}(?!\d)", text, re.I):
            raise ValueError("Дата указана в свободном тексте; уточните поле Дата публикации с годом")
        if end <= start:
            raise ValueError("Окончание периода должно быть позже начала")
    except (ValueError, OverflowError) as exc:
        reasons.append(str(exc))
    return Brief(name, ad_type, terms, start.isoformat(), end.isoformat(), retention_hours,
                 keywords, domains, "; ".join(reasons), default_window, client, language, planned_posts)
