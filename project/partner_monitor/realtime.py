"""Immediate discovery via Telegram updates, without joining any channel.

Public non-member channels use the documented getChannelDifference lease.
At most ten are short-polled, following each response's server-side timeout.
History polling remains independent so live delivery never skips a backlog.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import logging
from datetime import datetime, timedelta, timezone

from telethon import errors, events, types, utils
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.updates import GetChannelDifferenceRequest

log = logging.getLogger(__name__)


def difference_batch(result):
    """Return messages, next PTS, lease delay and whether history must catch up."""
    if isinstance(result, types.updates.ChannelDifferenceTooLong):
        return result.messages, result.dialog.pts, max(1, result.timeout or 1), True
    if isinstance(result, types.updates.ChannelDifference):
        return result.new_messages, result.pts, max(1, result.timeout or 1) if result.final else 0, False
    if isinstance(result, types.updates.ChannelDifferenceEmpty):
        return [], result.pts, max(1, result.timeout or 1), False
    raise ValueError("Неизвестный ответ Telegram для обновлений канала")


class Realtime:
    def __init__(self, monitor):
        self.monitor = monitor
        self.client = monitor.client
        self.raw = self.client.raw
        self.tasks = {}
        self.handlers = set()
        self.closed = False
        self.warned = set()

    def start(self):
        self.raw.add_event_handler(self.new_message, events.NewMessage())
        self.raw.add_event_handler(self.album, events.Album())
        self.raw.add_event_handler(self.deleted, events.MessageDeleted())
        self.raw.add_event_handler(self.edited, events.MessageEdited())
        self.monitor.on_channel = self.register_channel

    def watching(self, partner_id):
        from .parsing import PartnerReference
        partner = self.monitor.store.rows("SELECT * FROM partners WHERE id=?", (partner_id,))
        if not partner or partner[0]["status"] != "active":
            return False
        if not self.monitor.config.permits(PartnerReference(partner[0]["username"], private_channel_id=partner[0].get("private_channel_id"))):
            return False
        rows = self.monitor.store.rows("SELECT * FROM placements WHERE partner_id=?", (partner_id,))
        if not rows:
            return True  # Legacy channel-level monitoring.
        now = datetime.now(timezone.utc)
        for row in rows:
            if row["status"] in {"needs_review", "dismissed", "not_found"}:
                continue
            # Arm before a scheduled window so its first post need not wait
            # for the next history cycle to enable the live lease.
            if datetime.fromisoformat(row["starts_at"]) <= now + timedelta(seconds=self.monitor.config.interval_seconds) and now < datetime.fromisoformat(row["ends_at"]):
                return True
        return False

    def register_channel(self, partner, entity):
        from .channel_folder import public_username
        # Joined channels already have passive updates; don't spend a lease.
        if self.closed or not public_username(entity) or not entity.left or not self.watching(partner["id"]):
            return
        if entity.id in self.tasks and not self.tasks[entity.id].done():
            return
        try:
            utils.get_input_channel(entity)
        except (TypeError, ValueError):
            log.warning("@%s: для быстрых обновлений пока недостаточно данных канала; используется история", entity.username)
            return
        active = sum(not task.done() for task in self.tasks.values())
        if active >= self.monitor.config.realtime_channel_limit:
            if entity.id not in self.warned:
                log.warning("@%s: лимит быстрых каналов занят; используется проверка истории", entity.username)
                self.warned.add(entity.id)
            return
        # Clean up finished jobs; a resolved identity is always checked by tick.
        self.tasks = {cid: task for cid, task in self.tasks.items() if not task.done()}
        self.tasks[entity.id] = asyncio.create_task(self.poll_channel(partner["id"], entity))
        self.tasks[entity.id].add_done_callback(self.finished)

    def finished(self, task):
        if not task.cancelled() and (error := task.exception()) is not None:
            log.warning("Обработчик быстрых обновлений остановился: %s; проверка истории продолжится", type(error).__name__)
            self.monitor.wake.set()

    async def deliver(self, messages, expand_albums=False):
        groups = defaultdict(list)
        for message in messages:
            if isinstance(message, types.Message):
                channel = getattr(message.peer_id, "channel_id", None)
                key = (channel, message.grouped_id or f"m:{message.id}")
                groups[key].append(message)
        for members in groups.values():
            if expand_albums and members[0].grouped_id:
                # A difference page may end halfway through an album. Fetch
                # neighbours before recording its canonical first message.
                members = await self.monitor.album(members[0].peer_id, members[0])
            await self.monitor.process_live(members)

    async def _event(self, messages):
        if self.closed:
            return
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            if messages and utils.get_peer_id(messages[0].peer_id) == self.monitor.source_peer_id:
                self.monitor.wake.set()
                return
            await self.deliver(messages)
        except errors.FloodWaitError as exc:
            log.warning("Пауза Telegram для событий: %s секунд", exc.seconds)
            self.monitor.wake.set()
        except (ValueError, OSError, errors.RPCError) as exc:
            log.warning("Событие будет восстановлено из истории: %s", type(exc).__name__)
            self.monitor.wake.set()
        finally:
            self.handlers.discard(task)

    async def new_message(self, event):
        if event.message.grouped_id:
            return  # Album event waits for the members, avoiding a partial shot.
        await self._event([event.message])

    async def album(self, event):
        await self._event(event.messages)

    async def edited(self, event):
        message=event.message
        if self.monitor.config.forwarded_intake and utils.get_peer_id(message.peer_id)==self.monitor.source_peer_id:
            import json
            if getattr(message,'grouped_id',None):
                message=(await self.monitor.album(message.peer_id,message))[0]
            pending=json.loads(self.monitor.store.state('source_edited_ids','[]'))
            self.monitor.store.set_state('source_edited_ids',json.dumps(list(dict.fromkeys(pending+[message.id]))))
            self.monitor.wake.set()
            return
        if getattr(message,'grouped_id',None):
            await self._event(await self.monitor.album(message.peer_id,message))
        else:
            await self._event([message])

    async def deleted(self, event):
        from .parsing import PartnerReference
        from .storage import utcnow
        import json
        if self.closed or not getattr(event, "chat_id", None):
            return
        # Telegram does not always identify the chat of deleted messages. Only
        # channel-scoped updates can confirm deletion of our exact publication.
        peer = event.chat_id
        for partner in self.monitor.store.rows("SELECT * FROM partners WHERE channel_id IS NOT NULL"):
            if utils.get_peer_id(types.PeerChannel(partner["channel_id"])) != peer:
                continue
            if not self.monitor.config.permits(PartnerReference(partner["username"], private_channel_id=partner.get("private_channel_id"))):
                continue
            for post in self.monitor.store.rows("SELECT * FROM posts WHERE partner_id=?", (partner["id"],)):
                if set(json.loads(post["message_ids"])).intersection(event.deleted_ids):
                    checked = utcnow()
                    self.monitor.store.execute("""UPDATE posts SET status='deleted',last_checked_at=?,
                      unavailable_since=COALESCE(unavailable_since,?),last_error=? WHERE id=?""",
                      (checked, checked, "Удаление сообщения подтверждено обновлением Telegram; выход остаётся засчитанным", post["id"]))
                    self.monitor.changed.set()

    async def deliver_deletions(self, updates):
        for update in updates or []:
            if isinstance(update,types.UpdateEditChannelMessage):
                await self.deliver([update.message],expand_albums=True)
            if isinstance(update, types.UpdateDeleteChannelMessages):
                event = events.MessageDeleted.build(update, None, None)
                if event is not None:
                    await self.deleted(event)

    async def poll_channel(self, partner_id, entity):
        key = f"realtime_pts:{entity.id}"
        store = self.monitor.store
        channel = utils.get_input_channel(entity)
        while not self.closed and self.watching(partner_id):
            try:
                pts = int(store.state(key))
                if not pts:
                    info = await self.client(GetFullChannelRequest(channel))
                    pts = info.full_chat.pts
                    if not pts:
                        raise ValueError("Telegram не предоставил PTS канала")
                    store.set_state(key, pts)
                result = await self.client(GetChannelDifferenceRequest(
                    channel=channel, filter=types.ChannelMessagesFilterEmpty(), pts=pts, limit=100, force=False))
                messages, next_pts, delay, gap = difference_batch(result)
                if not isinstance(next_pts, int) or next_pts <= 0:
                    raise ValueError("Telegram не предоставил следующий PTS")
                await self.deliver(messages, expand_albums=True)
                await self.deliver_deletions(getattr(result, "other_updates", []))
                # Commit only after every message has been processed. A live
                # event never advances this PTS or the separate history cursor.
                store.set_state(key, next_pts)
                if gap:
                    self.monitor.wake.set()
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as exc:
                log.warning("Telegram запросил паузу быстрых обновлений: %s секунд", exc.seconds)
                await self.client.wait()
            except (OSError, asyncio.TimeoutError, errors.ServerError) as exc:
                log.warning("Обновления @%s временно недоступны: %s", entity.username, type(exc).__name__)
                await asyncio.sleep(5)
            except errors.RPCError as exc:
                if "PersistentTimestamp" in type(exc).__name__:
                    store.set_state(key, 0)
                log.warning("Быстрые обновления @%s недоступны (%s); история остаётся включённой", entity.username, type(exc).__name__)
                self.monitor.wake.set()
                return
            except ValueError as exc:
                log.warning("Быстрые обновления @%s остановлены: %s", entity.username, type(exc).__name__)
                self.monitor.wake.set()
                return

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.monitor.on_channel = None
        self.raw.remove_event_handler(self.new_message)
        self.raw.remove_event_handler(self.album)
        self.raw.remove_event_handler(self.deleted)
        self.raw.remove_event_handler(self.edited)
        jobs = list(self.tasks.values()) + list(self.handlers)
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
