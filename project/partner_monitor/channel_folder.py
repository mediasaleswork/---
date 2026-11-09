"""Read folder membership with the existing MTProto client; never joins chats."""
from datetime import datetime, timezone
from telethon import types, utils
from telethon.tl.functions.messages import GetDialogFiltersRequest


async def folder_channels(client, name):
    result = await client(GetDialogFiltersRequest())
    folders = [f for f in result.filters
               if getattr(getattr(f, "title", None), "text", "").strip().casefold() == name.strip().casefold()]
    if len(folders) != 1:
        raise ValueError("Папка не найдена или её название неоднозначно")
    folder = folders[0]
    excluded = {utils.get_peer_id(p) for p in getattr(folder, "exclude_peers", [])}
    entities = {}
    for peer in [*folder.pinned_peers, *folder.include_peers]:
        if utils.get_peer_id(peer) in excluded:
            continue
        entity = await client.get_entity(peer)
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
    return [{"id": e.id, "title": e.title, "username": e.username.lower() if e.username else None,
             "screenshot_supported": bool(e.username)} for e in sorted(entities.values(), key=lambda e: e.id)]
