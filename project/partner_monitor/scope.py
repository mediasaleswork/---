"""Validated channel scope and contract plans; no Telegram calls."""
import re
from datetime import datetime, timezone

from .parsing import PartnerReference, parse_partner_references


def target_key(ref):
    return ref.username or (f"c:{ref.private_channel_id}" if ref.private_channel_id else None)


def target_reference(name):
    if name.startswith("c:") and name[2:].isdigit():
        cid = int(name[2:])
        return PartnerReference(None, source_url=f"https://t.me/c/{cid}", private=True, private_channel_id=cid)
    return PartnerReference(name, source_url=f"https://t.me/{name}")


def channel_ref(value):
    refs = parse_partner_references(value if value.startswith(("@", "http", "t.me/")) else "@" + value)
    if len(refs) != 1 or refs[0].message_id or not refs[0].username:
        raise ValueError("Укажите публичный канал: @username или https://t.me/username")
    return refs[0]


def read_scope(data):
    for field in ("test_mode", "conversation_mode"):
        if type(data.get(field, False)) is not bool:
            raise ValueError(f"{field}: ожидается true или false")
    namespace = data.get("data_namespace", "")
    if not isinstance(namespace, str) or namespace and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", namespace):
        raise ValueError("data_namespace: только латиница, цифры, _ и -")
    default_client = data.get("default_client", "")
    if not isinstance(default_client, str):
        raise ValueError("default_client: ожидается строка")
    allowed = data.get("allowed_channels", [])
    plans = data.get("channel_plans", [])
    if not isinstance(allowed, list) or any(not isinstance(v, str) for v in allowed):
        raise ValueError("allowed_channels: ожидается список каналов")
    if not isinstance(plans, list):
        raise ValueError("channel_plans: ожидается список планов")
    normalized = []
    seen = set()
    for plan in plans:
        if not isinstance(plan, dict) or set(plan) - {"contract", "client", "channel", "title", "aliases", "language", "planned_posts", "baseline_urls", "baseline_count", "baseline_until"}:
            raise ValueError("channel_plans: неизвестное поле плана")
        p = dict(plan)
        for key in ("contract", "client", "channel"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise ValueError(f"channel_plans: заполните {key}")
        p["channel"] = channel_ref(p["channel"]).username
        identity = (p["contract"], p["channel"])
        if identity in seen:
            raise ValueError("channel_plans: повтор договора и канала")
        seen.add(identity)
        if type(p.get("planned_posts")) is not int or not 0 <= p["planned_posts"] <= 100000:
            raise ValueError("planned_posts: целое число от 0 до 100000")
        for field in ("aliases", "baseline_urls"):
            if not isinstance(p.get(field, []), list) or any(not isinstance(v, str) or not v.strip() for v in p.get(field, [])):
                raise ValueError(f"{field}: список непустых строк")
        for field in ("title", "language"):
            if not isinstance(p.get(field, ""), str):
                raise ValueError(f"{field}: ожидается строка")
        baseline = p.get("baseline_count", len(set(p.get("baseline_urls", []))))
        p["baseline_count"] = baseline
        if type(baseline) is not int or not len(set(p.get("baseline_urls", []))) <= baseline <= p["planned_posts"]:
            raise ValueError("baseline_count: от числа исходных ссылок до общего плана")
        if p.get("baseline_until"):
            try:
                date = datetime.fromisoformat(p["baseline_until"])
                if date.tzinfo is None:
                    raise ValueError()
                p["baseline_until"] = date.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise ValueError("baseline_until: полная дата и время с часовым поясом") from None
        if baseline > len(set(p.get("baseline_urls", []))) and not p.get("baseline_until"):
            raise ValueError("Для неполного списка прошлых выходов задайте baseline_until — границу исходного учёта")
        for url in p.get("baseline_urls", []):
            refs = parse_partner_references(url)
            if len(refs) != 1 or refs[0].username != p["channel"] or not refs[0].message_id:
                raise ValueError("baseline_urls: ссылки на посты соответствующего канала")
        normalized.append(p)
    return dict(conversation_mode=data.get("conversation_mode", False),
                test_mode=data.get("test_mode", False), default_client=default_client,
                allowed_channels=tuple(dict.fromkeys(channel_ref(v).username for v in allowed)),
                channel_plans=tuple(normalized), data_namespace=namespace)
