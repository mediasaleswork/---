"""Folder scoping never reads histories, joins chats or expands test scope."""
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import types
from partner_monitor.channel_folder import folder_channels
from partner_monitor.engine import Monitor
from partner_monitor.parsing import PartnerReference
from test_engine import NOW, channel, FakeCapture, FakeClient, message
import test_conversation as conversation
from test_conversation import image_message
from partner_monitor.intake import ConversationIntake
from partner_monitor.message_content import message_content
from partner_monitor.capture import CaptureResult


def folder(peers, **flags):
    return types.DialogFilter(2, types.TextWithEntities("каналы", []), [], peers, [], **flags)


class FolderClient:
    def __init__(self, selected, entities, dialogs=()):
        self.selected, self.entities, self.dialogs = selected, entities, dialogs

    async def __call__(self, request):
        self.request_name = type(request).__name__
        return types.messages.DialogFilters(self.selected)

    async def get_entity(self, peer):
        return self.entities[peer.channel_id]

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d


class FolderTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_additional_username_supports_public_screenshot(self):
        entity = channel()
        entity.username = None
        entity.usernames = [types.Username("OldInactive", active=False), types.Username("ActiveAlias", active=True)]
        client = FolderClient([folder([types.InputPeerChannel(200, 1)])], {200: entity})
        self.assertEqual((await folder_channels(client, "каналы"))[0]["username"], "activealias")

    async def test_explicit_folder_deduplicates_excludes_groups_and_never_joins(self):
        one, two = types.InputPeerChannel(200, 1), types.InputPeerChannel(201, 2)
        group = channel()
        group.id, group.broadcast, group.megagroup = 201, False, True
        selected = folder([one, one, two])
        selected.pinned_peers = [one]
        client = FolderClient([selected], {200: channel(), 201: group})
        result = await folder_channels(client, "Каналы")
        self.assertEqual([c["id"] for c in result], [200])
        self.assertEqual(client.request_name, "GetDialogFiltersRequest")
        self.assertNotIn("access_hash", json.dumps(result))

    async def test_missing_or_ambiguous_folder_fails_closed(self):
        for folders in ([], [folder([]), folder([])]):
            with self.assertRaises(ValueError):
                await folder_channels(FolderClient(folders, {}), "каналы")

    async def test_automatic_broadcast_rules_respect_exclusions_and_explicit_inclusions(self):
        one = channel()
        two = channel()
        two.id = 201
        three = channel()
        three.id = 202
        dialogs = [SimpleNamespace(entity=e, archived=True, unread_count=0,
                    dialog=SimpleNamespace(unread_mark=False, notify_settings=SimpleNamespace(mute_until=NOW + timedelta(days=3650))))
                   for e in (one, two, three)]
        selected = folder([types.InputPeerChannel(200, 1)], broadcasts=True, exclude_read=True, exclude_archived=True, exclude_muted=True)
        selected.exclude_peers = [types.InputPeerChannel(202, 1)]
        client = FolderClient([selected], {200: one}, dialogs)
        self.assertEqual([c["id"] for c in await folder_channels(client, "каналы")], [200])


class FolderScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await conversation.ConversationTests.asyncSetUp(self)
        self.client = FakeClient()

    async def asyncTearDown(self):
        await conversation.ConversationTests.asyncTearDown(self)

    async def test_inventory_does_not_expand_test_whitelist(self):
        config = replace(self.config, channel_folder="каналы", channel_folder_watch=True)
        monitor = Monitor(config, self.store, self.client, FakeCapture())
        with patch("partner_monitor.channel_folder.folder_channels", new=AsyncMock(return_value=[{"id": 999, "title": "Work", "username": "work_channel", "screenshot_supported": True}])):
            await monitor.refresh_channel_folder()
        self.assertEqual(monitor.config.allowed_channels, ("partner_one",))
        self.assertFalse(monitor.config.permits(PartnerReference("work_channel")))
        self.assertTrue((config.runtime_dir / "channel-folder.json").exists())

    async def test_failed_folder_removes_old_production_whitelist(self):
        config = replace(self.config, channel_folder="каналы", channel_folder_watch=True, test_mode=False, campaign_matching=True)
        monitor = Monitor(config, self.store, self.client, FakeCapture())
        with patch("partner_monitor.channel_folder.folder_channels", new=AsyncMock(side_effect=ValueError("missing"))):
            await monitor.refresh_channel_folder()
        self.assertFalse(monitor.config.permits(PartnerReference("partner_one")))
        self.assertEqual(monitor.campaign_worker.config.allowed_channels, ())

    async def test_material_without_target_uses_folder_only_after_approval(self):
        config = replace(self.config, channel_folder="каналы", channel_folder_watch=True, test_mode=False)
        intake = ConversationIntake(self.store, config)
        m = image_message(1, "Название: Test offer")
        intake.consume(-1000000000100, m, message_content(m), [])
        self.assertEqual(self.store.rows("SELECT status FROM placements")[0]["status"], "needs_review")
        self.assertEqual(self.store.rows("SELECT username FROM partners")[0]["username"], "partner_one")

    async def test_closed_folder_channel_is_monitored_without_a_public_widget(self):
        config = replace(self.config, channel_folder="каналы", channel_folder_watch=True,
                         allowed_channels=("c:200",), test_mode=False, campaign_matching=True)
        intake = ConversationIntake(self.store, config)
        for m in (image_message(1, "Название: Closed test offer\nПревью"), message(2, "ок", peer=100)):
            intake.consume(-1000000000100, m, message_content(m), [])
        self.client.partner.username = None
        self.client.histories[200] = [image_message(7, "Approved image", peer=200)]
        capture = FakeCapture()
        monitor = Monitor(config, self.store, self.client, capture)
        inventory = [{"id": 200, "title": "Closed", "username": None, "screenshot_supported": False}]
        with patch("partner_monitor.channel_folder.folder_channels", new=AsyncMock(return_value=inventory)), patch("partner_monitor.engine.asyncio.sleep", new=AsyncMock()), patch("partner_monitor.private_card.capture_private_card", new=AsyncMock(return_value=CaptureResult("card.png", "ok", "Карточка закрытого поста"))) as renderer:
            await monitor.tick()
        self.assertEqual(monitor.config.allowed_channels, ("c:200",))
        self.assertEqual(len(self.store.rows("SELECT * FROM posts")), 1)
        self.assertEqual(self.store.rows("SELECT views,screenshot_status FROM snapshots")[0], {"views": 10, "screenshot_status": "ok"})
        self.assertEqual(capture.calls, [])
        self.assertEqual(renderer.await_count, 1)
        self.assertEqual(renderer.call_args.args[3][0].peer_id.channel_id, 200)
