"""Contract-wide counts across creatives. Unavailable posts remain worked."""
import json
from datetime import datetime

from .parsing import parse_partner_references


def post_identity(url):
    refs = parse_partner_references(url)
    if len(refs) == 1 and refs[0].message_id:
        return (refs[0].username or refs[0].private_channel_id, refs[0].message_id)
    return url


def contract_summary(partners, posts, successful_pictures):
    result = []
    for partner in partners:
        briefs = {b["id"]: b for b in partner.get("placements", [])}
        for plan in partner.get("contract_plans", []):
            relevant = [p for p in posts if p["partner_id"] == partner["id"] and any(
                briefs.get(i, {}).get("contract_key") == plan["contract_key"] for i in p.get("placement_ids", []))]
            baseline = {post_identity(u) for u in json.loads(plan["baseline_urls"])}
            def baseline_contains(post):
                identity = post_identity(post["url"])
                aliases = {identity}
                if isinstance(identity, tuple):
                    aliases.update((identity[0], mid) for mid in json.loads(post.get("message_ids") or "[]"))
                return bool(aliases.intersection(baseline))
            new = {post_identity(p["url"]) for p in relevant if not baseline_contains(p)
                   and (not plan.get("baseline_until") or datetime.fromisoformat(p["published_at"]) > datetime.fromisoformat(plan["baseline_until"]))}
            total = plan["baseline_count"] + len(new)
            result.append(dict(client=plan["client_name"], channel=plan["title"], language=plan["language"],
                partner_id=partner["id"], post_ids={p["id"] for p in relevant},
                plan=plan["planned_posts"], total=total, remaining=max(0, plan["planned_posts"] - total),
                in_database=len({p["id"] for p in relevant}), unavailable=sum(p["status"] in {"unavailable", "deleted"} for p in relevant),
                pictures=sum(p["id"] in successful_pictures for p in relevant),
                last=max((p["published_at"] for p in relevant), default=None),
                url=f"https://t.me/{partner['username']}", contract=plan["contract_key"], baseline=plan["baseline_count"]))
    return result
