"""Conservative campaign matching that works without an AI quota/key.

Brand/domain alone is not sufficient. campaign_local also checks text copies,
exact video destinations and image fingerprints while a provider is unavailable.
"""
import json
import re

from .briefs import _fields
from .message_content import combined_content
from .parsing import MatchResult, match_ad, _normalize


def offline_match(messages, placement):
    content = combined_content(messages)
    media = set(json.loads(placement.get("media_keys") or "[]"))
    if placement.get("ad_type") == "рекламный материал":
        source = _normalize(placement["source_text"])
        actual = _normalize(content.text)
        if media.intersection(content.media) and source == actual:
            return MatchResult(True, "offline:совпадают текст и согласованное медиа")
        if not media and len(source) >= 60 and match_ad(content.text, [placement["source_text"]], []).matched:
            return MatchResult(True, "offline:совпадает полный текст рекламного материала")
        return MatchResult(False, "")
    if media.intersection(content.media):
        return MatchResult(True, "offline:тот же согласованный Telegram-материал")
    fields = {} if placement.get("ad_type") == "рекламный материал" else _fields(placement["source_text"])
    explicit = fields.get("искать") or fields.get("ключевые слова")
    if explicit:
        cues = [p.strip() for p in re.split(r"[;\n]", explicit) if p.strip()]
        # A single bank name is not an advertisement identity.
        cues = [p for p in cues if len(_normalize(p)) >= 10 and len(re.findall(r"\w+", _normalize(p))) >= 2]
    else:
        cues = [p for p in json.loads(placement.get("keywords") or "[]") if len(_normalize(p)) >= 60]
    result = match_ad(content.text + "\n" + "\n".join(content.urls), cues, [])
    return MatchResult(result.matched, "offline:совпадает согласованная фраза" if result.matched else "")
