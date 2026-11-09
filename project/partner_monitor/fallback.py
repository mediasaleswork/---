"""Conservative campaign matching that works without an AI quota/key.

Brand/domain alone is not sufficient. Uncertain publications stay queued.
"""
import json
import re

from .briefs import _fields
from .message_content import combined_content
from .parsing import MatchResult, match_ad, _normalize


def offline_match(messages, placement):
    content = combined_content(messages)
    media = set(json.loads(placement.get("media_keys") or "[]"))
    if media.intersection(content.media):
        return MatchResult(True, "offline:тот же согласованный Telegram-материал")
    fields = _fields(placement["source_text"])
    explicit = fields.get("искать") or fields.get("ключевые слова")
    if explicit:
        cues = [p.strip() for p in re.split(r"[;\n]", explicit) if p.strip()]
        # A single bank name is not an advertisement identity.
        cues = [p for p in cues if len(_normalize(p)) >= 10 and len(re.findall(r"\w+", _normalize(p))) >= 2]
    else:
        cues = [p for p in json.loads(placement.get("keywords") or "[]") if len(_normalize(p)) >= 60]
    result = match_ad(content.text + "\n" + "\n".join(content.urls), cues, [])
    return MatchResult(result.matched, "offline:совпадает согласованная фраза" if result.matched else "")
