"""Use Telegram's original channel and message ID on an approved forward as evidence."""
from copy import copy
from datetime import datetime

from telethon import types

from .measurements import ensure_jobs
from .parsing import PartnerReference
from .storage import utcnow


async def verify_forward_origins(monitor, request):
    store = monitor.store
    group = monitor.source_peer_id
    run = request['id']
    if not group or not monitor.scope_ready:
        return False
    if not store.rows('SELECT 1 FROM history_pages WHERE run=? AND peer=? AND done=1', (run, group)):
        return False
    key = 'history_forward_cursor:' + run
    cursor = int(store.state(key))
    if cursor == -1:
        return False
    ids = [r['mid'] for r in store.rows(
        'SELECT mid FROM history_seen WHERE run=? AND peer=? AND mid>? ORDER BY mid LIMIT 100',
        (run, group, cursor))]
    if not ids:
        store.set_state(key, -1)
        return True
    start, end = (datetime.fromisoformat(request[k]) for k in ('start', 'end'))
    partners = {p['channel_id']: p for p in store.rows('SELECT * FROM partners')}
    allowed = {item['channel_id'] for item in request['channels']}
    source = await monitor.client.get_messages(group, ids=ids)
    batch = {m.id: m for m in source if isinstance(m, types.Message)}
    for mid in ids:
        message = batch.get(mid)
        header = getattr(message, 'fwd_from', None)
        origin = getattr(header, 'from_id', None)
        post_id = getattr(header, 'channel_post', None)
        published = getattr(header, 'date', None)
        if (isinstance(origin, types.PeerChannel) and origin.channel_id in allowed and post_id
                and published and start <= published <= end):
            partner = partners.get(origin.channel_id)
            if not partner or not monitor.config.permits(PartnerReference(
                    partner['username'], private_channel_id=partner.get('private_channel_id'))):
                return False
            anchor = mid
            if message.grouped_id:
                anchor = min(m.id for m in await monitor.album(group, message))
            placements = store.rows("""SELECT p.* FROM source_messages s JOIN placements p
                ON p.draft_id=s.draft_id AND p.source_chat_id=s.source_chat_id
                WHERE s.source_chat_id=? AND s.message_id=? AND p.partner_id=?
                AND p.status NOT IN ('dismissed','needs_review')""", (str(group), anchor, partner['id']))
            if placements:
                await record_forward(monitor, partner, origin, post_id, published, message, placements)
                store.execute('INSERT OR IGNORE INTO history_forward_matches VALUES(?,?,?,?)',
                              (run, mid, origin.channel_id, post_id))
        store.set_state(key, mid)
    return True


async def record_forward(monitor, partner, peer, mid, published, forwarded, placements):
    store = monitor.store
    reason = f'forward:Telegram подтвердил исходный канал и ID поста в пересылке №{forwarded.id}'
    original = await monitor.client.get_messages(peer, ids=mid)
    if isinstance(original, types.Message):
        members = await monitor.album(peer, original)
        await monitor.record_post(partner, peer, members, reason, placements)
        return
    # The forward proves publication and its Telegram date. Its counter belongs to
    # a different chat, and its media is not a current screenshot of the original.
    if store.rows('SELECT id FROM posts WHERE message_id=? AND (telegram_channel_id=? OR partner_id=?)',
                  (mid, peer.channel_id, partner['id'])):
        return
    observed = copy(forwarded)
    observed.peer_id, observed.id, observed.date = peer, mid, published
    observed.views = None
    target = partner['username'] or f'c/{peer.channel_id}'
    with store.transaction():
        pid = store.save_post(partner['id'], [observed], reason, target, separate=True)
        store.execute("""UPDATE posts SET status='unavailable',unavailable_since=?,last_checked_at=?,
            last_error='Оригинал недоступен, выход подтверждён пересылкой Telegram' WHERE id=?""",
                      (utcnow(), utcnow(), pid))
        for item in placements:
            store.execute('INSERT OR IGNORE INTO placement_posts VALUES(?,?)', (item['id'], pid))
            store.execute("UPDATE placements SET status='found' WHERE id=?", (item['id'],))
    ensure_jobs(store, store.rows('SELECT * FROM posts WHERE id=?', (pid,))[0], monitor.config.milestones_hours)
    monitor.changed.set()
