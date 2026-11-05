"""Extract partner references and find configured advertising cues.

This module does not resolve Telegram names or join channels. A caller must
verify that a public username belongs to a channel before monitoring it.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class PartnerReference:
    username: str | None
    message_id: int | None = None
    source_url: str = ""
    private: bool = False
    private_channel_id: int | None = None


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    reason: str


_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_URL_TOKEN = re.compile(
    r"https?://[^\s<>\"'«»]+|(?<![\w.@/-])(?:t\.me|telegram\.me)/[^\s<>\"'«»]+",
    re.IGNORECASE,
)
_MENTION = re.compile(r"(?<![\w@./-])@([A-Za-z][A-Za-z0-9_]{4,31})(?![\w])")
_RESERVED = frozenset(
    "s c joinchat share iv proxy socks addstickers addemoji setlanguage login "
    "confirmphone boost contact invoice giftcode m nft addlist bg apps a k z "
    "faq privacy tos instantview resolve msg msg_url passport join".split()
)
_TRAILING = ".,;:!?)]}>\"'»"


def _parse_link(value: str) -> PartnerReference | None:
    value = value.strip().rstrip(_TRAILING)
    if not re.match(r"https?://", value, re.I):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.hostname is None or parsed.hostname.lower() not in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
    except ValueError:
        return None
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if not parts:
        return None
    first = parts[0].lower()
    if first == "joinchat" or parts[0].startswith("+"):
        return PartnerReference(None, source_url=value, private=True)
    if first == "c":
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            return None
        if int(parts[1]) < 1 or int(parts[2]) < 1:
            return None
        return PartnerReference(
            None, int(parts[2]), value, True, int(parts[1])
        )
    if first == "s":
        parts = parts[1:]
    if not parts or parts[0].lower() in _RESERVED or not _USERNAME.fullmatch(parts[0]):
        return None
    username = parts[0].lower()
    message_id = None
    if len(parts) > 1:
        if not parts[1].isdigit() or int(parts[1]) < 1:
            return None
        message_id = int(parts[1])
    canonical = f"https://t.me/{username}"
    if message_id is not None:
        canonical += f"/{message_id}"
    return PartnerReference(username, message_id, canonical)


def parse_partner_references(
    text: str, extra_urls: Iterable[str] = ()
) -> list[PartnerReference]:
    """Find channel/post URLs and mentions, preserving first-seen order.

    ``extra_urls`` can contain hidden URLs from Telegram message entities.
    Public channel links and direct links to its posts remain distinct. Private
    links are returned as such; this never grants access or follows an invite.
    """
    found: list[PartnerReference] = []
    candidates: list[tuple[int, PartnerReference]] = []
    spans: list[tuple[int, int]] = []
    for match in _URL_TOKEN.finditer(text or ""):
        spans.append(match.span())
        reference = _parse_link(match.group())
        if reference is not None:
            candidates.append((match.start(), reference))
    for match in _MENTION.finditer(text or ""):
        if any(start <= match.start() < end for start, end in spans):
            continue
        username = match.group(1).lower()
        if username not in _RESERVED:
            candidates.append((match.start(), PartnerReference(username, source_url=f"https://t.me/{username}")))
    candidates.sort(key=lambda item: item[0])
    found.extend(reference for _, reference in candidates)
    for value in extra_urls:
        reference = _parse_link(value)
        if reference is not None:
            found.append(reference)
    result: list[PartnerReference] = []
    seen: set[tuple] = set()
    for reference in found:
        key = (reference.username, reference.message_id, reference.private, reference.private_channel_id)
        if reference.private and reference.private_channel_id is None:
            key += (reference.source_url,)
        if key not in seen:
            result.append(reference)
            seen.add(key)
    return result


_APOSTROPHES = str.maketrans({c: "'" for c in "’‘ʻʼ`´ʹʽ＇"})
_DOMAIN_TOKEN = re.compile(
    r"(?<![\w@.-])(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?::[0-9]+)?(?:/[^\s<>\"']*)?",
    re.I,
)
_HTTP_TOKEN = re.compile(r"https?://[^\s<>\"']+", re.I)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().translate(_APOSTROPHES).split())


def _configured_host(value: str) -> str | None:
    try:
        host = urlsplit(value if "://" in value else "https://" + value).hostname
        return host.casefold().rstrip(".") if host else None
    except ValueError:
        return None


def match_ad(text: str, keywords: list[str], domains: list[str]) -> MatchResult:
    """Match configured words/phrases or exact host names and subdomains.

    A match is a discovery cue, not proof that the company paid for a post.
    Reasons are suitable for a report or human review.
    """
    normalized = _normalize(text or "")
    for keyword in keywords:
        needle = _normalize(keyword)
        if not needle:
            continue
        # Boundaries also protect short brands such as BRB from matching BRBank.
        if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", normalized):
            return MatchResult(True, f"keyword:{keyword}")
    hosts = {_configured_host(d) for d in domains if d.strip()}
    hosts.discard(None)
    # Parse complete URLs first: matching only the apparent domain prefix of
    # https://brb.uz@evil.example would mistake userinfo for the actual host.
    http_matches = list(_HTTP_TOKEN.finditer(normalized))
    tokens = [m.group() for m in http_matches]
    tokens.extend(
        match.group()
        for match in _DOMAIN_TOKEN.finditer(normalized)
        if not any(start.start() <= match.start() < start.end() for start in http_matches)
    )
    for token in tokens:
        host = _configured_host(token.rstrip(_TRAILING))
        if host is None:
            continue
        for configured in sorted(hosts):
            if host == configured or host.endswith("." + configured):
                return MatchResult(True, f"domain:{configured}")
    return MatchResult(False, "")
