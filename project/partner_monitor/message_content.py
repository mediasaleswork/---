"""Read ordinary messages and Telegram rich-message carousels without OCR.

Only text, URLs and stable media IDs are retained. Access hashes, file
references and thumbnail bytes never enter the monitoring database.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Content:
    text: str
    urls: tuple[str, ...]
    media: tuple[str, ...]


def message_content(message) -> Content:
    texts = [getattr(message, "message", None) or ""]
    urls = [e.url for e in getattr(message, "entities", None) or [] if getattr(e, "url", None)]
    media = []
    for field in ("photo", "document"):
        value = getattr(message, field, None)
        if value and getattr(value, "id", None):
            media.append(f"{field}:{value.id}")
    rich = getattr(message, "rich_message", None)
    if rich:
        value = rich.to_dict() if hasattr(rich, "to_dict") else rich

        def walk(node):
            if isinstance(node, dict):
                kind = node.get("_", "")
                if kind == "TextPlain":
                    return node.get("text", "")
                if kind == "TextUrl" and node.get("url"):
                    urls.append(node["url"])
                if kind == "PageBlockPhoto":
                    media.append(f"photo:{node['photo_id']}")
                if kind == "PageBlockVideo":
                    media.append(f"document:{node['video_id']}")
                # Formatting wrappers concatenate; block boundaries separate.
                values = [walk(v) for k, v in node.items() if k not in {
                    "_", "photos", "documents", "url", "photo_id", "video_id"}]
                return "".join(values)
            if isinstance(node, list):
                return "".join(walk(v) + ("\n" if isinstance(v, dict)
                    and v.get("_", "").startswith("PageBlock") else "") for v in node)
            return ""

        texts.append(walk(value.get("blocks", [])))
    return Content("\n".join(t for t in texts if t).strip(),
                   tuple(dict.fromkeys(urls)), tuple(dict.fromkeys(media)))


def combined_content(messages) -> Content:
    values = [message_content(m) for m in messages]
    return Content("\n".join(v.text for v in values if v.text),
                   tuple(dict.fromkeys(u for v in values for u in v.urls)),
                   tuple(dict.fromkeys(m for v in values for m in v.media)))


def search_text(messages) -> str:
    content = combined_content(messages)
    return content.text + "\n" + "\n".join(content.urls)


def matches_placement(messages, placement):
    import json
    from .parsing import MatchResult, match_ad

    content = combined_content(messages)
    expected = set(json.loads(placement.get("media_keys") or "[]"))
    if expected.intersection(content.media):
        return MatchResult(True, "media:совпадает материал согласованной заявки")
    return match_ad(content.text + "\n" + "\n".join(content.urls),
                    json.loads(placement["keywords"]), json.loads(placement["domains"]))
