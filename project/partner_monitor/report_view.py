"""One report model shared by the automated workbook and its visual preview."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo


def stamp(value):
    if not value:
        return None
    date = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return date if date.tzinfo else date.replace(tzinfo=timezone.utc)


def local(value, tz):
    return stamp(value).astimezone(tz).replace(tzinfo=None).isoformat() if value else None


def ordered(rows, field="checked_at"):
    return sorted(rows, key=lambda r: (stamp(r.get(field)) or stamp(r.get("checked_at")) or datetime.min.replace(tzinfo=timezone.utc), int(r.get("id", 0))))


def current_picture(checks):
    valid = [s for s in checks if s.get("screenshot_path") and Path(s["screenshot_path"]).is_file()
             and s.get("screenshot_status") not in {"error", "failed", "invalid", "unavailable", "pending"}]
    return ordered(valid, "screenshot_at")[-1] if valid else None


def build_view(partners, posts, snapshots, timezone_name="Asia/Tashkent", month=None):
    if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must have the form YYYY-MM")
    tz = ZoneInfo(timezone_name)
    partner_by_id = {p["id"]: p for p in partners}
    brief_by_id = {b["id"]: b for p in partners for b in p.get("placements", [])}
    checks = defaultdict(list)
    for snapshot in ordered(snapshots):
        checks[snapshot["post_id"]].append(snapshot)
    groups = {}
    for partner in partners:
        for brief in partner.get("placements", []) or ([] if partner.get("contract_plans") else [{}]):
            client = brief.get("client_name") or "Клиент не указан"
            key = (client.casefold(), partner["id"])
            group = groups.setdefault(key, {"client": client, "partner_id": partner["id"],
                "channel": partner.get("title") or partner.get("username") or "Канал не указан",
                "channel_url": f"https://t.me/{partner['username']}" if partner.get("username") else partner.get("source_url") or "",
                "languages": set(), "plans": [], "unknown_plan": False, "post_ids": set(), "visible": []})
            if brief.get("language"):
                group["languages"].add(brief["language"])
            if brief.get("status") not in {"dismissed", "needs_review"} and brief.get("planned_posts") is not None:
                group["plans"].append(brief["planned_posts"])
            else:
                group["unknown_plan"] = True
    records = []
    for post in sorted(posts, key=lambda p: (stamp(p["published_at"]), p["id"])):
        partner = partner_by_id.get(post["partner_id"], {})
        briefs = [brief_by_id[i] for i in post.get("placement_ids", []) if i in brief_by_id]
        clients = sorted({b.get("client_name") or "Клиент не указан" for b in briefs}) or ["Клиент не указан"]
        for client in clients:
            key = (client.casefold(), post["partner_id"])
            if key not in groups:
                groups[key] = {"client": client, "partner_id": post["partner_id"], "channel": partner.get("title") or partner.get("username") or "Канал не указан",
                    "channel_url": f"https://t.me/{partner['username']}" if partner.get("username") else partner.get("source_url") or "",
                    "languages": set(), "plans": [], "unknown_plan": True, "post_ids": set(), "visible": []}
            groups[key]["post_ids"].add(post["id"])
        published = stamp(post["published_at"])
        if month and published.astimezone(tz).strftime("%Y-%m") != month:
            continue
        history = checks[post["id"]]
        latest = history[-1] if history else {}
        picture = current_picture(history)
        state = {"active": "Доступен", "unavailable": "Недоступен", "deleted": "Удалён (подтверждено)", "demo": "Пример"}.get(post.get("status"), "Требует проверки")
        if post.get("status") == "active" and partner.get("status") in {"private", "unavailable", "identity_changed"}:
            state = "Доступ к каналу не подтверждён"
        unavailable = state in {"Недоступен", "Удалён (подтверждено)", "Доступ к каналу не подтверждён"}
        durations = sorted({b["retention_hours"] for b in briefs if b.get("retention_hours")})
        until = published + timedelta(hours=durations[0]) if len(durations) == 1 else None
        picture_note = ""
        if not picture:
            picture_note = latest.get("screenshot_error") or "Скриншот ещё не получен"
        elif latest.get("id") != picture.get("id"):
            picture_note = "Показан последний успешный снимок. Более поздний снимок недоступен."
        is_card = bool(picture and str(picture.get("screenshot_error") or "").startswith("Карточка закрытого поста"))
        if is_card:
            picture_note = ("Карточка закрытого поста из данных Telegram. " + picture_note).strip()
        if post.get("evidence_note"):
            picture_note = (post["evidence_note"] + " " + picture_note).strip()
        record = {"id": post["id"], "client": "; ".join(clients), "channel": partner.get("title") or partner.get("username") or "Канал не указан",
            "channel_url": f"https://t.me/{partner['username']}" if partner.get("username") else partner.get("source_url") or "",
            "languages": ", ".join(sorted({b["language"] for b in briefs if b.get("language")})),
            "ad": "; ".join(sorted({b["name"] for b in briefs if b.get("name")})),
            "ad_type": "; ".join(sorted({b["ad_type"] for b in briefs if b.get("ad_type")})),
            "url": post.get("url") or "", "month": published.astimezone(tz).strftime("%Y-%m"),
            "published": local(published, tz), "checked": local(post.get("last_checked_at") or latest.get("checked_at"), tz),
            "views_checked": local(latest.get("checked_at"), tz), "views": latest.get("views"),
            "durations": durations, "until": local(until, tz), "state": state, "unavailable": unavailable,
            "unavailable_since": local(post.get("unavailable_since"), tz),
            "picture_path": picture["screenshot_path"] if picture else None,
            "picture_at": local(picture.get("screenshot_at") or picture.get("checked_at"), tz) if picture else None,
            "picture_views": picture.get("views") if picture else None, "picture_note": picture_note,
            "picture_kind": "card" if is_card else "screenshot",
            "error": post.get("last_error") or partner.get("last_error") or "",
            "placement_ids": post.get("placement_ids", []), "text": post.get("text") or ""}
        records.append(record)
        for client in clients:
            groups[(client.casefold(), post["partner_id"])]["visible"].append(record)
    summary = []
    for group in sorted(groups.values(), key=lambda g: (g["client"].casefold(), g["channel"].casefold(), g["partner_id"])):
        visible = group.pop("visible")
        plan = sum(group.pop("plans")) if not group["unknown_plan"] else None
        total = len(group.pop("post_ids"))
        group.update({"plan": plan, "total": total, "remaining": max(0, plan - total) if plan is not None else None,
            "visible_count": len(visible), "unavailable_count": sum(p["unavailable"] for p in visible),
            "picture_count": sum(bool(p["picture_path"]) for p in visible), "languages": ", ".join(sorted(group["languages"])),
            "last_published": max((p["published"] for p in visible), default=None), "first_post_id": visible[0]["id"] if visible else None})
        summary.append(group)
    if any(p.get("contract_plans") for p in partners):
        from .contracts import contract_summary
        contracted = {(plan["client_name"].casefold(),p["id"]) for p in partners for plan in p.get("contract_plans", [])}
        summary = [g for g in summary if (g["client"].casefold(),g["partner_id"]) not in contracted]
        pictures = {r["id"] for r in records if r["picture_path"]}
        # contract_summary counts the entire contract, while the visible post
        # rows and their availability still respect the selected report month.
        for item in contract_summary(partners, posts, pictures):
            pid = item["partner_id"]
            visible = [r for r in records if r["id"] in item["post_ids"]]
            summary.append(dict(client=item["client"], partner_id=pid, channel=item["channel"],
                channel_url=item["url"], languages=item["language"], plan=item["plan"],
                total=item["total"], remaining=item["remaining"], visible_count=len(visible),
                unavailable_count=sum(r["unavailable"] for r in visible),
                picture_count=sum(bool(r["picture_path"]) for r in visible),
                last_published=max((r["published"] for r in visible), default=None),
                first_post_id=visible[0]["id"] if visible else None))
        summary.sort(key=lambda g: (g["client"].casefold(),g["channel"].casefold(),g["partner_id"]))
    return {"timezone": timezone_name, "month": month, "summary": summary, "records": records,
            "unavailable": [p for p in records if p["unavailable"]]}
