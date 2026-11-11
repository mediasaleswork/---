"""Read folder membership with the existing MTProto client; never joins chats."""
from datetime import datetime, timezone
from telethon import types, utils
from telethon.tl.functions.messages import GetDialogFiltersRequest


def public_username(entity):
    # Channels with multiple/collectible usernames may omit the legacy field.
    return (getattr(entity, "username", None) or next(
        (u.username for u in getattr(entity, "usernames", None) or [] if u.active), None))


async def folder_channels(client, name, *, resolved=None):
    result = await client(GetDialogFiltersRequest())
    folders = [f for f in result.filters
               if getattr(getattr(f, "title", None), "text", "").strip().casefold() == name.strip().casefold()]
    if len(folders) != 1:
        raise ValueError("Папка не найдена или её название неоднозначно")
    folder = folders[0]
    excluded = {utils.get_peer_id(p) for p in getattr(folder, "exclude_peers", [])}
    entities = {}
    peers = {utils.get_peer_id(p): p for p in [*folder.pinned_peers, *folder.include_peers]
             if utils.get_peer_id(p) not in excluded}
    if peers and callable(getattr(type(client), 'get_entities', None)):
        explicit = await client.get_entities(list(peers.values()))
    else:
        explicit = [await client.get_entity(p) for p in peers.values()]
    for entity in explicit:
        if isinstance(entity, types.Channel) and entity.broadcast:
            entities[entity.id] = entity
    # A custom folder ID is NOT iter_dialogs(folder=ID), which only selects
    # Telegram's main/archive folders. Evaluate automatic channel rules here.
    if getattr(folder, "broadcasts", False):
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, types.Channel) or not entity.broadcast or utils.get_peer_id(entity) in excluded:
                continue
            if entity.id in entities:
                continue  # Explicitly included peers override automatic rules.
            if getattr(folder, "exclude_archived", False) and dialog.archived:
                continue
            if getattr(folder, "exclude_read", False) and not (dialog.unread_count or getattr(dialog.dialog, "unread_mark", False)):
                continue
            muted_until = getattr(dialog.dialog.notify_settings, "mute_until", None)
            if getattr(folder, "exclude_muted", False) and muted_until and muted_until > datetime.now(timezone.utc):
                continue
            entities[entity.id] = entity
    if resolved is not None:
        resolved.clear()
        resolved.update(entities)
    return [{"id": e.id, "title": e.title, "username": public_username(e).lower() if public_username(e) else None,
             "screenshot_supported": bool(public_username(e))} for e in sorted(entities.values(), key=lambda e: e.id)]
