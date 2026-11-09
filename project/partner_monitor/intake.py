"""Conservative, durable conversation intake. Never sends Telegram messages.

Replies and unique pending previews can carry context. Ambiguous short
approvals are recorded for review, never applied across unrelated adverts.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import re

from .briefs import Brief, _fields, parse_brief
from .parsing import PartnerReference, _normalize
from .storage import utcnow
from .scope import target_key, target_reference
log = logging.getLogger(__name__)


def packed(value):
    return json.dumps(value, ensure_ascii=False)


def creative_cues(text, brief, fields):
    explicit = fields.get("искать") or fields.get("ключевые слова")
    if explicit:
        return [s.strip() for s in re.split(r"[;\n]", explicit) if s.strip()]
    if brief.keywords:
        return list(brief.keywords)
    # Channel signatures, disclosure labels and emoji often differ between
    # publications. Match substantive paragraphs of the approved creative,
    # rather than requiring its channel footer to occur verbatim everywhere.
    paragraphs = [re.sub(r"^[^\w]+", "", p.strip()) for p in re.split(r"\n\s*\n", text)]
    usable = [p for p in paragraphs if 60 <= len(p) <= 1500 and not re.search(r"https?://|t\.me/|@[A-Za-z]", p)]
    return sorted(usable, key=len, reverse=True)[:2] or ([text] if text else [])


class ConversationIntake:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def _draft(self, ident):
        rows = self.store.rows("SELECT * FROM intake_drafts WHERE id=?", (ident,))
        return rows[0] if rows else None

    def _log(self, chat, message, content, draft, targets, kind, reason=None):
        self.store.execute("""INSERT OR IGNORE INTO source_messages(source_chat_id,message_id,draft_id,
          text,media_keys,targets,kind,review_reason,received_at) VALUES (?,?,?,?,?,?,?,?,?)""",
          (str(chat), message.id, draft["id"] if draft else None, content.text,
           packed(content.media), packed(targets), kind, reason, message.date.isoformat()))
        if reason and not self.store.rows("SELECT id FROM partners LIMIT 1"):
            self.store.add_review(chat, message.id, "переписка", reason)

    def _candidates(self, chat, message):
        cutoff = (message.date - timedelta(hours=24)).isoformat()
        return self.store.rows("SELECT * FROM intake_drafts WHERE source_chat_id=? AND updated_at>=? ORDER BY id DESC",
                               (str(chat), cutoff))

    def _sync(self, draft, received, targets=None, *, approved=False, direct=None, invalidate=False):
        direct = direct or {}
        names = targets if targets is not None else json.loads(draft["target_refs"])
        fields = {} if self.config.forwarded_intake else _fields(draft["creative_text"])
        # Use explicitly supplied search cues, otherwise the actual creative.
        text = draft["creative_text"]
        created = datetime.fromisoformat(draft["created_at"])
        if self.config.forwarded_intake:
            # Dates, usernames and words like "Клиент:" inside an advert are
            # content, not operational instructions or an implicit direct link.
            brief = Brief(draft["name"], "рекламный материал", "",
                          created.isoformat(),
                          (created + timedelta(days=self.config.default_window_days)).isoformat(),
                          None, (), (), "", True)
        else:
            brief = parse_brief(text, [], created, self.config.timezone, self.config.default_window_days)
        keywords = creative_cues(text, brief, fields)
        issues = []
        for name in names:
            ref = target_reference(name)
            if not self.config.permits(ref):
                continue
            pid = self.store.add_partner(ref, draft["source_chat_id"], draft["anchor_id"], enqueue=False)
            plans = [p for p in self.config.channel_plans if p["channel"] == name and p["client"] == draft["client_name"]]
            previous = self.store.rows("SELECT * FROM placements WHERE partner_id=? AND source_chat_id=? AND source_message_id=?",
                                       (pid, draft["source_chat_id"], draft["anchor_id"]))
            reason = None if approved or name in direct else "Материалы получены; требуется согласование превью"
            if not invalidate and previous and previous[0]["status"] in {"watching", "found"}:
                reason = None
            if not keywords and not json.loads(draft["media_keys"]) and name not in direct:
                reason = "Нет текста, признака поиска или материала рекламы"
            if len(plans) > 1:
                reason = "Для клиента и канала несколько договоров; уточните договор"
            if brief.review_reason and name not in direct:
                reason = brief.review_reason
            if reason:
                issues.append(reason)
            direct_ids = sorted(set(direct.get(name, []) + (json.loads(previous[0]["direct_ids"]) if previous else [])))
            self.store.execute("""INSERT INTO placements(partner_id,source_chat_id,source_message_id,
              source_text,name,ad_type,terms,starts_at,ends_at,retention_hours,keywords,domains,
              direct_ids,status,review_reason,default_window,created_at,client_name,language,
              media_keys,draft_id,contract_key)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(partner_id,source_chat_id,source_message_id) DO UPDATE SET
              source_text=excluded.source_text,name=excluded.name,ad_type=excluded.ad_type,
              terms=excluded.terms,starts_at=excluded.starts_at,ends_at=excluded.ends_at,
              retention_hours=excluded.retention_hours,default_window=excluded.default_window,
              keywords=excluded.keywords,domains=excluded.domains,
              media_keys=excluded.media_keys,client_name=excluded.client_name,language=excluded.language,
              status=CASE WHEN placements.status='found' AND excluded.status='watching' THEN 'found' ELSE excluded.status END,
              review_reason=excluded.review_reason,
              direct_ids=excluded.direct_ids,draft_id=excluded.draft_id,contract_key=excluded.contract_key""",
              (pid, draft["source_chat_id"], draft["anchor_id"], text, draft["name"],
               brief.ad_type or ("медиа" if json.loads(draft["media_keys"]) else "текст"), brief.terms,
               brief.start, brief.end, brief.retention_hours, packed(keywords), packed(brief.domains),
               packed(direct_ids), "needs_review" if reason else "watching", reason,
               int(brief.default_window), utcnow(), draft["client_name"], draft["language"] or (plans[0].get("language", "") if plans else ""),
               draft["media_keys"], draft["id"], plans[0]["contract"] if len(plans) == 1 else ""))
            self.store.execute("UPDATE partners SET last_message_id=0 WHERE id=?", (pid,))
        return list(dict.fromkeys(issues))

    def consume(self, chat, message, content, refs):
        if self.store.rows("SELECT message_id FROM source_messages WHERE source_chat_id=? AND message_id=?", (str(chat), message.id)):
            return
        if self.config.forwarded_intake:
            self.consume_forwarded(chat, message, content)
            return
        text = content.text.strip()
        normalized = _normalize(text)
        client_key = f"intake_client:{chat}"
        client = self.store.state(client_key, self.config.default_client)
        new_client = re.fullmatch(r"\s*новый\s+клиент\s*:\s*(\S[^\n]{0,199})\s*", text, re.I)
        if normalized.rstrip(":") == "новый клиент":
            self._log(chat, message, content, None, [], "needs_review", "Укажите название: Новый клиент: Название компании")
            return
        if new_client:
            self.store.set_state(client_key, new_client[1].strip())
            self._log(chat, message, content, None, [], "client")
            return
        candidates = self._candidates(chat, message)
        reply = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
        referenced = self.store.rows("SELECT * FROM source_messages WHERE source_chat_id=? AND message_id=?", (str(chat), reply)) if reply else []
        draft = self._draft(referenced[0]["draft_id"]) if referenced and referenced[0]["draft_id"] else None
        fields = _fields(text)
        title = fields.get("название рекламы") or fields.get("название") or fields.get("продукт") or fields.get("бренд")
        explicit_client = fields.get("клиент") or fields.get("заказчик")
        client = explicit_client or (draft["client_name"] if draft else client)
        if not draft and title:
            matching = [d for d in candidates if _normalize(d["name"]) == _normalize(title) and d["client_name"] == client]
            draft = matching[0] if len(matching) == 1 else None
        if not draft and content.media:
            matching = [d for d in candidates if d["client_name"] == client and set(json.loads(d["media_keys"])).intersection(content.media)]
            draft = matching[0] if len(matching) == 1 else None
        if not draft and len(text) >= 100:
            matching = [d for d in candidates if d["client_name"] == client and _normalize(d["creative_text"]) == normalized]
            draft = matching[0] if len(matching) == 1 else None
        targets = list(dict.fromkeys(target_key(r) for r in refs if target_key(r) and self.config.permits(r)))
        for p in self.config.channel_plans:
            if p["client"] != client:
                continue
            aliases = [p.get("title", ""), *p.get("aliases", [])]
            if any(a.strip() and re.search(r"(?<!\w)" + re.escape(_normalize(a)) + r"(?!\w)", normalized) for a in aliases):
                if p["channel"] not in targets:
                    targets.append(p["channel"])
        preview = bool(re.search(r"\b(?:превью|preview|образец|пример)\b", normalized))
        approval = bool(re.fullmatch(r"(?:ок|ok|хоп|согласовано|согласован|утверждено|можно выпускать)[.!\s🤝✅]*", normalized))
        direct = {target_key(r): [r.message_id] for r in refs if target_key(r) in targets and r.message_id} if not preview else {}
        if approval:
            if not draft:
                pending = [d for d in candidates if d["status"] == "preview" and d["client_name"] == client]
                # Adjacency is accepted only for a unique, recent preview.
                recent = self.store.rows("SELECT * FROM source_messages WHERE source_chat_id=? ORDER BY message_id DESC LIMIT 1", (str(chat),))
                if len(pending) == 1 and recent and recent[0]["draft_id"] == pending[0]["id"] and recent[0]["kind"] == "preview":
                    draft = pending[0]
                    referenced = recent
            if not draft:
                self._log(chat, message, content, None, targets, "needs_review", "Короткое согласование без однозначного превью; ответьте на нужное сообщение")
                return
            targets = targets or (json.loads(referenced[0]["targets"]) if referenced else []) or json.loads(draft["target_refs"])
            issues = self._sync(draft, message.date, targets, approved=True)
            reason = "; ".join(issues) or None
            self.store.execute("UPDATE intake_drafts SET status=?,updated_at=?,review_reason=? WHERE id=?",
                               ("needs_review" if issues else "approved", message.date.isoformat(), reason, draft["id"]))
            self._log(chat, message, content, draft, targets, "needs_review" if issues else "approved", reason)
            return
        creative = bool(content.media or title or len(text) >= 100)
        if creative and not targets and self.config.channel_folder_watch and not self.config.test_mode:
            # A creative without explicit platforms is searched throughout the
            # selected folder after approval, never in unrelated subscriptions.
            targets = list(self.config.allowed_channels)
        correction = bool(re.search(r"имидж|изображени|исправ|обнов|согласован", normalized))
        if not draft and content.media and correction:
            possible = [d for d in candidates if d["client_name"] == client]
            if len(possible) == 1:
                draft = possible[0]
            elif len(possible) > 1:
                self._log(chat, message, content, None, targets, "needs_review", "Несколько реклам; ответьте на материал, который нужно заменить")
                return
        if not draft and not creative:
            pending = [d for d in candidates if d["status"] in {"draft", "preview"} and d["client_name"] == client]
            if len(pending) == 1 and (targets or preview or re.search(r"имидж|изображени|ссылк|текст", normalized)):
                draft = pending[0]
        if not draft and (creative or direct):
            name = title or next((line.strip()[:100] for line in text.splitlines() if line.strip()), "Материал рекламы")
            self.store.execute("""INSERT INTO intake_drafts(source_chat_id,anchor_id,name,client_name,
              language,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
              (str(chat), message.id, name, client, fields.get("язык", "").upper(), message.date.isoformat(), message.date.isoformat()))
            draft = self.store.rows("SELECT * FROM intake_drafts WHERE source_chat_id=? AND anchor_id=?", (str(chat), message.id))[0]
        if not draft:
            reason = "Неясно, к какой рекламе относятся каналы или правка; ответьте на материал" if targets or preview else None
            self._log(chat, message, content, None, targets, "needs_review" if reason else "ignored", reason)
            return
        all_targets = list(dict.fromkeys(json.loads(draft["target_refs"]) + targets))
        # A captionless image correction preserves the approved text, while
        # recording the new media version. Every source message remains logged.
        creative_text = text if creative and text and not (content.media and correction and len(text) < 100) else draft["creative_text"]
        media = list(content.media) if content.media else json.loads(draft["media_keys"])
        changed = creative_text != draft["creative_text"] or set(media) != set(json.loads(draft["media_keys"]))
        stage = "preview" if preview else "draft" if creative else draft["status"]
        self.store.execute("""UPDATE intake_drafts SET name=?,client_name=?,creative_text=?,media_keys=?,
          target_refs=?,status=?,language=?,updated_at=? WHERE id=?""",
          (title or draft["name"], client, creative_text, packed(media), packed(all_targets), stage,
           fields.get("язык", draft["language"]).upper(), message.date.isoformat(), draft["id"]))
        draft = self._draft(draft["id"])
        if direct:
            self._sync(draft, message.date, list(direct), direct=direct)
        elif all_targets:
            scope = all_targets if changed else targets or all_targets
            self._sync(draft, message.date, scope, invalidate=changed or preview)
        self._log(chat, message, content, draft, targets or all_targets,
                  "publication" if direct else "preview" if preview else "material" if creative else "context")

    def consume_forwarded(self, chat, message, content):
        text = content.text.strip()
        document = getattr(message, "document", None)
        if document and not getattr(document, "mime_type", "").startswith(("image/", "video/")):
            self._log(chat, message, content, None, [], "ignored")
            return
        if not (getattr(message, "fwd_from", None) or content.media) or not (text or content.media):
            self._log(chat, message, content, None, [], "ignored")
            return
        targets = list(self.config.allowed_channels)
        if not targets:
            # The source cursor must not discard materials when the folder is
            # temporarily unavailable. Retry the same source batch next tick.
            raise OSError("Папка каналов пока не прочитана; материал не пропущен")
        media = packed(content.media)
        cutoff = (message.date - timedelta(days=self.config.default_window_days)).isoformat()
        identical = self.store.rows("""SELECT * FROM intake_drafts WHERE source_chat_id=? AND creative_text=?
          AND media_keys=? AND status='approved' AND created_at>=? ORDER BY id DESC LIMIT 1""", (str(chat), text, media, cutoff))
        if identical:
            draft = identical[0]
            new = [name for name in targets if name not in json.loads(draft["target_refs"])]
            if new:
                self._sync(draft, message.date, new, approved=True)
                self.store.execute("UPDATE intake_drafts SET target_refs=? WHERE id=?", (packed(targets), draft["id"]))
            self._log(chat, message, content, draft, targets, "duplicate")
            return
        name = next((line.strip()[:100] for line in text.splitlines() if line.strip()), f"Рекламный материал №{message.id}")
        self.store.execute("""INSERT INTO intake_drafts(source_chat_id,anchor_id,name,client_name,creative_text,
          media_keys,target_refs,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'approved',?,?)""",
          (str(chat), message.id, name, self.config.default_client, text, media, packed(targets), message.date.isoformat(), message.date.isoformat()))
        draft = self.store.rows("SELECT * FROM intake_drafts WHERE source_chat_id=? AND anchor_id=?", (str(chat), message.id))[0]
        self._sync(draft, message.date, targets, approved=True)
        self._log(chat, message, content, draft, targets, "material")
        log.info("Рекламный материал #%s принят из группы; площадок %s", message.id, len(targets))

    def attach_folder_channels(self, received):
        if not self.config.forwarded_intake:
            return
        cutoff = (received - timedelta(days=self.config.default_window_days)).isoformat()
        for draft in self.store.rows("SELECT * FROM intake_drafts WHERE status='approved' AND created_at>=?", (cutoff,)):
            previous = json.loads(draft["target_refs"])
            new = [name for name in self.config.allowed_channels if name not in previous]
            if new:
                self._sync(draft, received, new, approved=True)
                self.store.execute("UPDATE intake_drafts SET target_refs=? WHERE id=?", (packed(list(dict.fromkeys(previous + new))), draft["id"]))
