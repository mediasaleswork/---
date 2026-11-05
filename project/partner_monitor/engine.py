from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import tempfile
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, errors, types, utils
from telethon.tl.functions.messages import CheckChatInviteRequest
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

from .capture import Screenshotter
from .config import Config
from .parsing import PartnerReference, match_ad, parse_partner_references
from .storage import Store, utcnow
from .telegram_access import TelegramAccess

log = logging.getLogger(__name__)


def due_milestones(post: dict, existing: list[dict], hours: tuple[float, ...], now: datetime) -> list[tuple[str, str]]:
    done = {s["due_key"] for s in existing}
    published = datetime.fromisoformat(post["published_at"])
    first_seen = datetime.fromisoformat(post["first_seen_at"])
    result = []
    if "initial" not in done:
        result.append(("initial", first_seen.isoformat()))
    for h in hours:
        due = published + timedelta(hours=h)
        key = f"{h:g}h"
        # Never pretend that a historical 24-hour measurement can be recovered.
        if key not in done and first_seen < due <= now:
            result.append((key, due.isoformat()))
    return result


def client_for(config: Config) -> TelegramClient:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    api_id, api_hash = config.telegram_credentials()
    return TelegramClient(str(config.data_dir / "telegram"), api_id, api_hash, flood_sleep_threshold=0)


class Monitor:
    def __init__(self, config: Config, store: Store, client, capture):
        self.config, self.store, self.capture = config, store, capture
        self.client = TelegramAccess(client)
        self.wake = asyncio.Event()
        self.changed = asyncio.Event()
        self.source_peer_id = None
        self.on_channel = None
        self._post_locks = WeakValueDictionary()
        self._attempted = set()
        self._capturing = set()

    async def record_post(self, partner, entity, members, reason, placements=()):
        """Persist and capture at discovery, before scanning the next message."""
        members = sorted(members, key=lambda m: m.id)
        anchor = members[0]
        key = (partner["id"], getattr(anchor, "grouped_id", None) or f"m:{anchor.id}")
        lock = self._post_locks.setdefault(key, asyncio.Lock())
        async with lock:
            target = partner["username"] or f"c/{members[0].peer_id.channel_id}"
            post_id = self.store.save_post(partner["id"], members, reason, target)
            self.store.execute("UPDATE posts SET last_checked_at=?,unavailable_since=NULL,status='active',last_error=NULL WHERE id=?", (utcnow(), post_id))
            for placement in placements:
                self.store.execute("INSERT OR IGNORE INTO placement_posts VALUES (?,?)", (placement["id"], post_id))
                self.store.execute("UPDATE placements SET status='found' WHERE id=?", (placement["id"],))
            if not self.store.rows("SELECT id FROM snapshots WHERE post_id=? AND due_key='initial'", (post_id,)):
                post = self.store.rows("SELECT * FROM posts WHERE id=?", (post_id,))[0]
                self.store.execute("""INSERT INTO snapshots(post_id,due_key,due_at,checked_at,views,screenshot_status)
                  VALUES (?,'initial',?,?,?,'pending')""", (post_id, post["first_seen_at"], utcnow(), getattr(anchor, "views", None)))
                snapshot = self.store.rows("SELECT * FROM snapshots WHERE post_id=? AND due_key='initial'", (post_id,))[0]
                await self.capture_snapshot(partner, post, snapshot)
            self.changed.set()
            return post_id

    async def process_live(self, messages):
        """Handle live messages without moving the history/backfill cursor."""
        messages = sorted([m for m in messages if isinstance(m, types.Message)], key=lambda m: m.id)
        if not messages or not isinstance(messages[0].peer_id, types.PeerChannel):
            return
        channel_id = messages[0].peer_id.channel_id
        partners = self.store.rows("SELECT * FROM partners WHERE channel_id=? AND status='active'", (channel_id,))
        if not partners:
            return
        messages = [m for m in messages if isinstance(m.peer_id, types.PeerChannel) and m.peer_id.channel_id == channel_id]
        text = "\n".join((m.message or "") + " " + " ".join(e.url for e in m.entities or [] if isinstance(e, types.MessageEntityTextUrl)) for m in messages)
        for partner in partners:
            placements = self.store.rows("SELECT * FROM placements WHERE partner_id=?", (partner["id"],))
            matched = []
            reason = ""
            for placement in placements:
                if placement["status"] in {"needs_review", "dismissed", "not_found"}:
                    continue
                direct = json.loads(placement["direct_ids"])
                if direct:
                    if set(direct).intersection(m.id for m in messages):
                        matched.append(placement)
                        reason = "direct_link:ссылка из заявки"
                    continue
                if datetime.fromisoformat(placement["starts_at"]) <= messages[0].date < datetime.fromisoformat(placement["ends_at"]):
                    match = match_ad(text, json.loads(placement["keywords"]), json.loads(placement["domains"]))
                    if match.matched:
                        matched.append(placement)
                        reason = reason or match.reason
            if not placements:
                reason = match_ad(text, self.config.keywords, self.config.domains).reason
            if matched or not placements and reason:
                await self.record_post(partner, types.PeerChannel(channel_id), messages, reason, matched)
                for placement in matched:
                    for mid in json.loads(placement["direct_ids"]):
                        if any(m.id == mid for m in messages):
                            self.store.execute("UPDATE requested_posts SET done=1 WHERE partner_id=? AND message_id=?", (partner["id"], mid))

    async def ingest_group(self):
        if not self.config.source_group:
            raise ValueError("Заполните source_group в config.yaml; команда dialogs покажет ID доступных групп")
        entity = await self.client.get_entity(self.config.source_group)
        if not (isinstance(entity, types.Chat) or isinstance(entity, types.Channel) and entity.megagroup):
            raise ValueError("source_group должен указывать на рабочую группу, а не на пользователя или канал")
        self.source_peer_id = utils.get_peer_id(entity)
        key = f"source_cursor:{entity.id}"
        cursor = int(self.store.state(key))
        options = {"min_id": cursor, "reverse": True, "limit": self.config.batch_size}
        if not cursor:
            options["offset_date"] = datetime.now(timezone.utc) - timedelta(days=self.config.lookback_days)
        async for message in self.client.iter_messages(entity, **options):
            text = message.message or ""
            urls = [e.url for e in message.entities or [] if isinstance(e, types.MessageEntityTextUrl)]
            refs = parse_partner_references(text, urls)
            document = getattr(message, "document", None)
            if document:
                name = next((a.file_name for a in document.attributes if isinstance(a, types.DocumentAttributeFilename)), "")
                extension = Path(name).suffix.lower()
                if extension in {".pdf", ".docx", ".xlsx", ".txt"}:
                    try:
                        from .documents import extract_document
                        if document.size > 20 * 1024 * 1024:
                            raise ValueError("Размер документа превышает 20 МБ")
                        with tempfile.TemporaryDirectory(prefix="partner-document-") as scratch:
                            local = Path(scratch) / ("attachment" + extension)
                            await self.client.download_media(message, file=str(local))
                            extracted = await asyncio.to_thread(extract_document, local)
                        refs.extend(parse_partner_references(extracted))
                        text += "\n" + extracted
                        if not refs:
                            self.store.add_review(entity.id, message.id, name, "В документе не найдена Telegram-ссылка; пришлите ссылку на канал отдельным сообщением")
                    except errors.FloodWaitError:
                        raise
                    except (OSError, ValueError) as exc:
                        self.store.add_review(entity.id, message.id, name, f"Документ не удалось прочитать ({type(exc).__name__}); требуется явная Telegram-ссылка")
                elif not refs:
                    self.store.add_review(entity.id, message.id, name or "вложение", "Формат вложения не поддерживается; пришлите текст условий и ссылку на канал")
            forwarded = getattr(message, "fwd_from", None)
            from_id = getattr(forwarded, "from_id", None)
            if isinstance(from_id, types.PeerChannel):
                try:
                    origin = await self.client.get_entity(from_id)
                    if getattr(origin, "broadcast", False):
                        username = getattr(origin, "username", None)
                        original_id = getattr(forwarded, "channel_post", None)
                        if username:
                            refs.append(PartnerReference(username.lower(), original_id, f"https://t.me/{username}"))
                        else:
                            refs.append(PartnerReference(None, original_id, f"https://t.me/c/{origin.id}/{original_id or 0}", True, origin.id))
                except errors.FloodWaitError:
                    raise
                except (ValueError, errors.RPCError):
                    log.warning("Не удалось определить источник пересланного сообщения %s; явные ссылки всё равно обработаны", message.id)
            if refs:
                self.store.register_brief(refs, text, utils.get_peer_id(entity), message.id, message.date, self.config)
            elif getattr(message, "photo", None):
                self.store.add_review(entity.id, message.id, "изображение", "Нужен текст с названием рекламы, ссылкой на канал и длительностью; OCR не подключён")
            if not refs and not document and re.search(r"партн[её]р|hamkor", text, re.IGNORECASE):
                self.store.add_review(entity.id, message.id, f"сообщение №{message.id}", "Партнёр упомянут без ссылки на Telegram-канал; требуется уточнение")
            self.store.set_state(key, message.id)

    async def album(self, entity, message) -> list:
        if not getattr(message, "grouped_id", None):
            return [message]
        neighbours = await self.client.get_messages(entity, ids=list(range(max(1, message.id - 9), message.id + 10)))
        return sorted([m for m in neighbours if m and getattr(m, "grouped_id", None) == message.grouped_id], key=lambda m: m.id) or [message]

    async def discover(self, partner: dict, entity):
        placements = self.store.rows("SELECT * FROM placements WHERE partner_id=?", (partner["id"],))
        if placements:
            await self.discover_placements(partner, entity, placements)
            return
        pending = self.store.rows("SELECT message_id FROM requested_posts WHERE partner_id=? AND done=0", (partner["id"],))
        for item in pending:
            message = await self.client.get_messages(entity, ids=item["message_id"])
            if message and isinstance(message, types.Message):
                members = await self.album(entity, message)
                await self.record_post(partner, entity, members, "direct_link:ссылка из рабочей группы")
                self.store.execute("UPDATE requested_posts SET done=1 WHERE partner_id=? AND message_id=?", (partner["id"], item["message_id"]))
            else:
                self.store.execute("UPDATE partners SET last_error=? WHERE id=?", (f"Пост {item['message_id']} по прямой ссылке недоступен; повторим проверку", partner["id"]))
        cursor = partner["last_message_id"]
        options = {"min_id": cursor, "reverse": True, "limit": self.config.batch_size}
        if not cursor:
            options["offset_date"] = datetime.now(timezone.utc) - timedelta(days=self.config.lookback_days)
        seen = set()
        async for message in self.client.iter_messages(entity, **options):
            if isinstance(message, types.Message):
                key = getattr(message, "grouped_id", None) or f"m:{message.id}"
                if key not in seen:
                    seen.add(key)
                    members = await self.album(entity, message)
                    text = "\n".join((m.message or "") + " " + " ".join(e.url for e in m.entities or [] if isinstance(e, types.MessageEntityTextUrl)) for m in members)
                    match = match_ad(text, self.config.keywords, self.config.domains)
                    if match.matched:
                        await self.record_post(partner, entity, members, match.reason)
            # Advance only after successful processing, so a failure is retried.
            self.store.execute("UPDATE partners SET last_message_id=? WHERE id=?", (message.id, partner["id"]))

    async def save_placement_post(self, partner, entity, message, placement, reason):
        members = await self.album(entity, message)
        await self.record_post(partner, entity, members, reason, [placement])

    async def discover_placements(self, partner, entity, placements):
        now = datetime.now(timezone.utc)
        allowed = [p for p in placements if p["status"] not in {"needs_review", "dismissed", "not_found"}]
        for placement in allowed:
            for mid in json.loads(placement["direct_ids"]):
                linked = self.store.rows("""SELECT p.message_ids FROM posts p JOIN placement_posts x
                  ON x.post_id=p.id WHERE x.placement_id=?""", (placement["id"],))
                if any(mid in json.loads(p["message_ids"]) for p in linked):
                    continue
                message = await self.client.get_messages(entity, ids=mid)
                if isinstance(message, types.Message):
                    await self.save_placement_post(partner, entity, message, placement, "direct_link:ссылка из заявки")
                    self.store.execute("UPDATE requested_posts SET done=1 WHERE partner_id=? AND message_id=?", (partner["id"], mid))
                else:
                    self.store.execute("UPDATE partners SET last_error=? WHERE id=?", (f"Пост {mid} недоступен; повторим проверку", partner["id"]))
        searches = [p for p in allowed if not p["scan_complete"] and not json.loads(p["direct_ids"]) and datetime.fromisoformat(p["starts_at"]) <= now]
        if not searches:
            return
        cursor = partner["last_message_id"]
        options = {"min_id": cursor, "reverse": True, "limit": self.config.batch_size}
        if not cursor:
            # A second's overlap includes a post exactly at the window start.
            options["offset_date"] = min(datetime.fromisoformat(p["starts_at"]) for p in searches) - timedelta(seconds=1)
        seen = set()
        count = 0
        latest = None
        for_scan = max(datetime.fromisoformat(p["ends_at"]) for p in searches)
        async for message in self.client.iter_messages(entity, **options):
            count += 1
            latest = message.date
            if isinstance(message, types.Message):
                key = getattr(message, "grouped_id", None) or f"m:{message.id}"
                if key not in seen:
                    seen.add(key)
                    members = await self.album(entity, message)
                    published = members[0].date
                    text = "\n".join((m.message or "") + " " + " ".join(e.url for e in m.entities or [] if isinstance(e, types.MessageEntityTextUrl)) for m in members)
                    for placement in searches:
                        if not datetime.fromisoformat(placement["starts_at"]) <= published < datetime.fromisoformat(placement["ends_at"]):
                            continue
                        match = match_ad(text, json.loads(placement["keywords"]), json.loads(placement["domains"]))
                        if match.matched:
                            # Reuse the already fetched album.
                            await self.record_post(partner, entity, members, match.reason, [placement])
            self.store.execute("UPDATE partners SET last_message_id=? WHERE id=?", (message.id, partner["id"]))
            if latest >= for_scan:
                break
        for placement in searches:
            end = datetime.fromisoformat(placement["ends_at"])
            # Do not report a missing placement until every relevant history
            # batch has been processed, even if the publication window expired.
            complete = end <= now and (count < self.config.batch_size or latest is not None and latest >= end)
            if complete:
                self.store.execute("""UPDATE placements SET scan_complete=1,status=CASE WHEN EXISTS(
                  SELECT 1 FROM placement_posts WHERE placement_id=placements.id) THEN 'found' ELSE 'not_found' END
                  WHERE id=?""", (placement["id"],))

    async def capture_snapshot(self, partner: dict, post: dict, snapshot: dict):
        self._attempted.add(snapshot["id"])
        if not partner["username"]:
            self.store.execute("""UPDATE snapshots SET screenshot_status='unavailable',screenshot_error=?,
              capture_attempts=3 WHERE id=?""", ("Закрытый канал: публичный виджет Telegram недоступен; нужен отдельный снимок из авторизованного клиента", snapshot["id"]))
            return
        self._capturing.add(snapshot["id"])
        try:
            result = await self.capture.capture(partner["username"], json.loads(post["message_ids"]), f"post-{post['id']}-snapshot-{snapshot['id']}")
            self.store.execute("""UPDATE snapshots SET screenshot_path=?,screenshot_status=?,screenshot_error=?,
              screenshot_at=?,capture_attempts=capture_attempts+1 WHERE id=?""",
              (result.path, result.status, result.error, utcnow() if result.path else None, snapshot["id"]))
        except Exception as exc:
            log.warning("Ошибка скриншота поста %s: %s", post["id"], type(exc).__name__)
            self.store.execute("UPDATE snapshots SET screenshot_status='error',screenshot_error=?,capture_attempts=capture_attempts+1 WHERE id=?", (type(exc).__name__, snapshot["id"]))
        finally:
            self._capturing.discard(snapshot["id"])

    async def measure(self, partner: dict, entity):
        for post in self.store.rows("SELECT * FROM posts WHERE partner_id=?", (partner["id"],)):
            existing = self.store.rows("SELECT * FROM snapshots WHERE post_id=?", (post["id"],))
            due = due_milestones(post, existing, self.config.milestones_hours, datetime.now(timezone.utc))
            now = datetime.now(timezone.utc)
            placements = self.store.rows("""SELECT p.* FROM placements p JOIN placement_posts x ON x.placement_id=p.id
              WHERE x.post_id=? AND p.retention_hours IS NOT NULL AND p.status!='dismissed'""", (post["id"],))
            retention_active = False
            for placement in placements:
                until = datetime.fromisoformat(post["published_at"]) + timedelta(hours=placement["retention_hours"])
                key = f"placement-{placement['id']}-{placement['retention_hours']:g}h"
                if datetime.fromisoformat(post["first_seen_at"]) < until and not any(s["due_key"] == key for s in existing):
                    retention_active = True
                    if until <= now and datetime.fromisoformat(post["first_seen_at"]) < until:
                        due.append((key, until.isoformat()))
            retries = [s for s in existing if not s["screenshot_path"] and s["capture_attempts"] < 3 and s["id"] not in self._attempted and s["id"] not in self._capturing]
            if not due and not retries and not retention_active:
                continue
            messages = await self.client.get_messages(entity, ids=json.loads(post["message_ids"]))
            available = [m for m in messages if isinstance(m, types.Message)]
            if len(available) != len(json.loads(post["message_ids"])):
                checked = utcnow()
                self.store.execute("UPDATE posts SET status='unavailable',last_checked_at=?,unavailable_since=COALESCE(unavailable_since,?),last_error=? WHERE id=?", (checked, checked, "Не удалось получить все сообщения публикации; удаление не подтверждено", post["id"]))
                continue
            available.sort(key=lambda m: m.id)
            self.store.execute("UPDATE posts SET status='active',last_checked_at=?,unavailable_since=NULL,last_error=NULL,text=? WHERE id=?", (utcnow(), "\n".join(m.message or "" for m in available).strip(), post["id"]))
            # get_messages reads stored counters, without incrementing them via GetMessagesViews.
            views = getattr(available[0], "views", None)
            for key, due_at in due:
                self.store.execute("""INSERT OR IGNORE INTO snapshots(post_id,due_key,due_at,checked_at,views,screenshot_status)
                  VALUES (?,?,?,?,?,'pending')""", (post["id"], key, due_at, utcnow(), views))
                snapshot = self.store.rows("SELECT * FROM snapshots WHERE post_id=? AND due_key=?", (post["id"], key))[0]
                await self.capture_snapshot(partner, post, snapshot)
            for snapshot in retries:
                await self.capture_snapshot(partner, post, snapshot)

    async def tick(self):
        self._attempted.clear()
        await self.ingest_group()
        for partner in self.store.rows("SELECT * FROM partners WHERE status NOT IN ('ignored','needs_review')"):
            try:
                # Never resolve or read an ambiguous brief until the operator
                # has supplied the missing details locally.
                placements = self.store.rows("SELECT status FROM placements WHERE partner_id=?", (partner["id"],))
                if placements and all(p["status"] in {"needs_review", "dismissed", "not_found"} for p in placements) and not self.store.rows("SELECT id FROM posts WHERE partner_id=?", (partner["id"],)):
                    continue
                await asyncio.sleep(1)
                if partner["username"]:
                    entity = await self.client.get_entity(partner["username"])
                elif partner.get("private_channel_id"):
                    entity = await self.client.get_entity(types.PeerChannel(partner["private_channel_id"]))
                else:
                    path = urlsplit(partner["source_url"] or "").path
                    invite = path.rsplit("/", 1)[-1].lstrip("+")
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", invite):
                        continue
                    checked = await self.client(CheckChatInviteRequest(invite))
                    if not isinstance(checked, types.ChatInviteAlready):
                        self.store.execute("UPDATE partners SET status='private',last_error=? WHERE id=?", ("У аккаунта нет подтверждённого доступа; вступление не выполняется", partner["id"]))
                        continue
                    entity = checked.chat
                if not isinstance(entity, types.Channel) or not entity.broadcast:
                    self.store.execute("UPDATE partners SET status='ignored',last_error=? WHERE id=?", ("Ссылка ведёт на пользователя, бота или группу, а не на канал", partner["id"]))
                    continue
                if partner["username"] and not getattr(entity, "username", None):
                    raise ValueError("Нет публичного username")
                if partner.get("channel_id") and partner["channel_id"] != entity.id:
                    self.store.execute("UPDATE partners SET status='identity_changed',last_error=? WHERE id=?", ("Username теперь принадлежит другому каналу. Нужна проверка партнёра", partner["id"]))
                    continue
                self.store.execute("UPDATE partners SET channel_id=?,private_channel_id=?,title=?,status='active',last_error=NULL WHERE id=?", (entity.id, entity.id if not partner["username"] else None, entity.title, partner["id"]))
                if self.on_channel:
                    self.on_channel(partner, entity)
                await self.discover(partner, entity)
                await self.measure(partner, entity)
            except errors.FloodWaitError:
                raise
            except (ValueError, errors.RPCError) as exc:
                self.store.execute("UPDATE partners SET status=?,last_error=? WHERE id=?", ("unavailable" if partner["username"] else "private", f"Не удалось проверить канал: {type(exc).__name__}; доступ не подтверждён" if not partner["username"] else f"Не удалось проверить канал: {type(exc).__name__}", partner["id"]))
                log.warning("Канал @%s недоступен: %s", partner["username"], type(exc).__name__)


async def publish_reports(config, store):
    from .reports import export_xlsx
    await asyncio.to_thread(export_xlsx, *store.dump(), config.data_dir / "report.xlsx", timezone=config.timezone)
    if config.google_credentials and config.spreadsheet_id:
        try:
            from .reports import GoogleReporter
            reporter = GoogleReporter(config.google_credentials, config.spreadsheet_id, config.drive_folder_id, config.timezone)
            uploaded = await asyncio.to_thread(reporter.sync, *store.dump())
            for sid, url in uploaded.items():
                store.execute("UPDATE snapshots SET screenshot_url=? WHERE id=?", (url, int(sid)))
        except Exception as exc:
            log.warning("Google Sheets не синхронизирован (%s); локальный отчёт сохранён, повторим позже", type(exc).__name__)


async def report_loop(monitor):
    while True:
        await monitor.changed.wait()
        monitor.changed.clear()
        try:
            await publish_reports(monitor.config, monitor.store)
        except Exception as exc:
            log.warning("Отчёт временно недоступен: %s; повторим", type(exc).__name__)
            await asyncio.sleep(5)
            monitor.changed.set()


async def run(config: Config, *, once: bool = False):

    client = client_for(config)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("Сначала выполните partner-monitor login на своём компьютере или сервере")
        with Store(config.db_path) as store:
            async with Screenshotter(config.data_dir / "screenshots") as capture:
                monitor = Monitor(config, store, client, capture)
                from .realtime import Realtime
                realtime = Realtime(monitor)
                publisher = None
                if not once:
                    realtime.start()
                    publisher = asyncio.create_task(report_loop(monitor))
                try:
                    while True:
                        monitor.wake.clear()
                        delay = config.interval_seconds
                        try:
                            await monitor.tick()
                            monitor.changed.set()
                            if once:
                                await publish_reports(config, store)
                            log.info("Цикл завершён. Отчёт: %s", config.data_dir / "report.xlsx")
                        except errors.FloodWaitError as exc:
                            delay = max(delay, exc.seconds + 1)
                            log.warning("Telegram запросил паузу %s секунд; продолжим после неё", exc.seconds)
                            if once:
                                raise ValueError(f"Telegram запросил паузу {exc.seconds} секунд; повторите позже") from exc
                        except (OSError, asyncio.TimeoutError, errors.ServerError, errors.RpcCallFailError, errors.TimedOutError) as exc:
                            log.warning("Временная ошибка соединения: %s; повторим цикл", type(exc).__name__)
                            if once:
                                raise
                        if once:
                            break
                        try:
                            await asyncio.wait_for(monitor.wake.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    if not once:
                        await realtime.close()
                    if publisher:
                        publisher.cancel()
                        await asyncio.gather(publisher, return_exceptions=True)
    finally:
        await client.disconnect()
