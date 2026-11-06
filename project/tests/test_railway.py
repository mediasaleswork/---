from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml

from deploy.railway_entrypoint import main, prepare_config, validate_volume, verify_account


class RailwayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config.example.yaml').write_text("source_group: ''\n", encoding='utf-8')

    def test_setup_never_authenticates_or_starts_monitoring(self):
        with patch('deploy.railway_entrypoint.wait_for_setup') as wait, \
                patch('deploy.railway_entrypoint.verify_account') as verify, \
                patch('deploy.railway_entrypoint.os.execv') as execute:
            self.assertEqual(main(self.root, {}), 0)
        wait.assert_called_once()
        verify.assert_not_called()
        execute.assert_not_called()

    def test_railway_refuses_ephemeral_or_wrong_data_directory(self):
        config = prepare_config(self.root, {})
        for mount in ('', str(self.root / 'wrong')):
            with self.assertRaisesRegex(ValueError, 'persistent Railway volume'):
                validate_volume(config, {'RAILWAY_PROJECT_ID': 'project', 'RAILWAY_VOLUME_MOUNT_PATH': mount})
        validate_volume(config, {'RAILWAY_PROJECT_ID': 'project', 'RAILWAY_VOLUME_MOUNT_PATH': str(self.root / 'data')})

    def test_environment_group_overrides_config_without_copying_secrets(self):
        (self.root / 'config.yaml').write_text('source_group: old\ninterval_seconds: 600\n', encoding='utf-8')
        config = prepare_config(self.root, {'SOURCE_GROUP': '-1001234567890', 'TELEGRAM_API_HASH': 'secret-test-value'})
        self.assertEqual(config.source_group, -1001234567890)
        self.assertEqual(config.interval_seconds, 600)
        text = (self.root / 'config.yaml').read_text(encoding='utf-8')
        self.assertNotIn('secret-test-value', text)
        self.assertNotIn('TELEGRAM_API_HASH', yaml.safe_load(text))

    def test_run_requires_group_before_opening_telegram(self):
        with patch('deploy.railway_entrypoint.verify_account') as verify:
            with self.assertRaisesRegex(ValueError, 'SOURCE_GROUP'):
                main(self.root, {'MONITOR_MODE': 'run'})
        verify.assert_not_called()

    def test_google_settings_survive_a_fresh_container_configuration(self):
        path = self.root / 'data' / 'google.json'
        config = prepare_config(self.root, {'GOOGLE_SPREADSHEET_ID': 'test-sheet',
            'GOOGLE_DRIVE_FOLDER_ID': 'test-folder', 'GOOGLE_CREDENTIALS_FILE': str(path)})
        self.assertEqual(config.spreadsheet_id, 'test-sheet')
        self.assertEqual(config.drive_folder_id, 'test-folder')
        self.assertEqual(config.google_credentials, path.resolve())

    def test_invalid_mode_does_not_create_configuration(self):
        with self.assertRaisesRegex(ValueError, 'MONITOR_MODE'):
            main(self.root, {'MONITOR_MODE': 'wrong'})
        self.assertFalse((self.root / 'config.yaml').exists())

    def test_persistent_test_configuration_is_reloaded_after_deployment(self):
        path = self.root / 'data' / 'test-config.yaml'
        path.parent.mkdir()
        path.write_text("source_group: '-1001234567890'\ntest_mode: true\nconversation_mode: true\ndata_namespace: test\nallowed_channels: [partner_one]\nmilestones_hours: [24, 48]\n", encoding='utf-8')
        config = prepare_config(self.root, {'MONITOR_CONFIG_FILE': str(path)})
        self.assertTrue(config.test_mode)
        self.assertEqual(config.db_path, self.root / 'data' / 'test' / 'monitor.sqlite3')
        self.assertEqual(config.milestones_hours, (24, 48))
        config.validate_run()


class RailwayAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_account_and_bot_are_rejected_and_disconnected(self):
        for account in (None, Mock(bot=True, id=100), Mock(bot=False, id=200)):
            client = Mock(connect=AsyncMock(), disconnect=AsyncMock(),
                          is_user_authorized=AsyncMock(return_value=True), get_me=AsyncMock(return_value=account))
            with patch('partner_monitor.engine.client_for', return_value=client):
                with self.assertRaises(ValueError):
                    await verify_account(Mock(), expected_user_id=100)
            client.disconnect.assert_awaited_once()

    async def test_authorized_work_account_is_checked_and_disconnected_before_start(self):
        client = Mock(connect=AsyncMock(), disconnect=AsyncMock(),
                      is_user_authorized=AsyncMock(return_value=True), get_me=AsyncMock(return_value=Mock(bot=False, id=100)))
        with patch('partner_monitor.engine.client_for', return_value=client):
            await verify_account(Mock(), expected_user_id=100)
        client.get_me.assert_awaited_once()
        client.disconnect.assert_awaited_once()

    async def test_missing_authorization_does_not_start_an_interactive_login(self):
        client = Mock(connect=AsyncMock(), disconnect=AsyncMock(), start=AsyncMock(),
                      is_user_authorized=AsyncMock(return_value=False), get_me=AsyncMock())
        with patch('partner_monitor.engine.client_for', return_value=client):
            with self.assertRaisesRegex(ValueError, 'login is required'):
                await verify_account(Mock())
        client.start.assert_not_called()
        client.get_me.assert_not_called()
        client.disconnect.assert_awaited_once()

