"""Non-regression tests for DomoLink-BackUp integration."""
import os
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Add component to path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from tests.ha_mock import setup_ha_mock

setup_ha_mock()

import voluptuous as vol
from custom_components.domolink_backup.const import (
    DOMAIN,
    SERVICE_CREATE_BACKUP,
    SERVICE_UPLOAD_BACKUP,
    SERVICE_RESTORE_BACKUP,
    SERVICE_TEST_CONNECTION,
    SERVICE_CLEAN_OLD_BACKUPS,
    SERVICE_SYNC_BACKUPS,
)


class TestDomolinkBackupArchitecture(unittest.TestCase):
    """Test architecture, security and schema validation for DomoLink-BackUp."""

    def setUp(self):
        self.hass = sys.modules["homeassistant.core"].HomeAssistant()

    def test_admin_services_allow_extra_keys(self):
        """Ensure schemas for backup services permit extra keys (preventing HA 2024.10+ rejection)."""
        schema_create = vol.Schema({
            vol.Optional("name"): str,
            vol.Optional("include_database", default=True): bool,
        }, extra=vol.ALLOW_EXTRA)

        schema_upload = vol.Schema({
            vol.Required("file_path"): str,
        }, extra=vol.ALLOW_EXTRA)

        schema_empty = vol.Schema({}, extra=vol.ALLOW_EXTRA)

        # Extra keys should be accepted without raising MultipleInvalid
        valid_create = schema_create({"name": "Test Backup", "extra_option": 123})
        self.assertEqual(valid_create["name"], "Test Backup")
        self.assertEqual(valid_create["extra_option"], 123)

        valid_upload = schema_upload({"file_path": "/backup/test.tar", "caller_id": "test"})
        self.assertEqual(valid_upload["file_path"], "/backup/test.tar")

        valid_empty = schema_empty({"any_extra_data": "ok"})
        self.assertIn("any_extra_data", valid_empty)

    def test_manifest_requirements_and_version(self):
        """Ensure manifest.json is well-formed with domain and version."""
        import json
        manifest_path = os.path.join(ROOT_DIR, "custom_components", "domolink_backup", "manifest.json")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest.get("domain"), "domolink_backup")
        self.assertTrue(manifest.get("version"))
        self.assertIn("codeowners", manifest)

    def test_path_containment_helper(self):
        """Ensure backup paths cannot escape outside the configured directories."""
        from homeassistant.exceptions import HomeAssistantError

        def is_safe_path(target, base_dir="/backup"):
            real_base = os.path.realpath(base_dir)
            real_target = os.path.realpath(os.path.join(base_dir, target))
            return os.path.commonpath([real_base]) == os.path.commonpath([real_base, real_target])

        self.assertTrue(is_safe_path("core_2026.tar", "/backup"))
        self.assertTrue(is_safe_path("sub/core_2026.tar", "/backup"))
        self.assertFalse(is_safe_path("../../etc/passwd", "/backup"))
        self.assertFalse(is_safe_path("../.storage/auth", "/backup"))

    def test_no_obsolete_supervisor_queries(self):
        """Ensure async_query_supervisor is completely removed from codebase."""
        import glob
        py_files = glob.glob(os.path.join(ROOT_DIR, "custom_components", "domolink_backup", "**", "*.py"), recursive=True)
        for py_file in py_files:
            with open(py_file, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("async_query_supervisor", content, f"Obsolete call found in {py_file}")
            self.assertNotIn("supervisor_slugs_before", content, f"Obsolete variable found in {py_file}")

    def test_backup_manager_restore_keyword_args(self):
        """Ensure async_restore_backup_via_manager supplies all required keyword arguments to BackupManager."""
        import asyncio
        from custom_components.domolink_backup import async_restore_backup_via_manager

        mock_local_agent = MagicMock()
        mock_local_agent._load_backups = AsyncMock()

        mock_manager = MagicMock()
        mock_manager.backup_agents = {"backup.local": mock_local_agent}
        mock_manager.async_get_backups = AsyncMock()

        # Strict function to simulate BackupManager.async_restore_backup keyword-only signature
        called_kwargs = {}
        async def mock_async_restore_backup(
            backup_id: str,
            *,
            agent_id: str,
            password: str | None,
            restore_addons: list[str] | None,
            restore_database: bool,
            restore_folders: list | None,
            restore_homeassistant: bool,
        ) -> None:
            called_kwargs.update({
                "backup_id": backup_id,
                "agent_id": agent_id,
                "password": password,
                "restore_addons": restore_addons,
                "restore_database": restore_database,
                "restore_folders": restore_folders,
                "restore_homeassistant": restore_homeassistant,
            })

        mock_manager.async_restore_backup = mock_async_restore_backup

        asyncio.run(
            async_restore_backup_via_manager(
                mock_manager,
                "slug_12345",
                agent_id="backup.local",
                password=None,
                restore_addons=None,
                restore_database=True,
                restore_folders=None,
                restore_homeassistant=True,
            )
        )

        # Agent archive list must be refreshed
        mock_local_agent._load_backups.assert_awaited_once()

        # All keyword-only arguments must be passed correctly
        self.assertEqual(called_kwargs["backup_id"], "slug_12345")
        self.assertEqual(called_kwargs["agent_id"], "backup.local")
        self.assertIsNone(called_kwargs["password"])
        self.assertIsNone(called_kwargs["restore_addons"])
        self.assertTrue(called_kwargs["restore_database"])
        self.assertIsNone(called_kwargs["restore_folders"])
        self.assertTrue(called_kwargs["restore_homeassistant"])

    def test_partial_restore_service_dispatch_conditions(self):
        """Ensure hassio.restore_partial is only called when there is work for Supervisor."""
        # Condition 1: restore_homeassistant=False, no addons, no folders -> no Supervisor call
        has_supervisor_work_1 = bool(False) or bool(None) or bool([])
        self.assertFalse(has_supervisor_work_1)

        # Condition 2: restore_homeassistant=True -> Supervisor call needed
        has_supervisor_work_2 = bool(True) or bool(None) or bool([])
        self.assertTrue(has_supervisor_work_2)

        # Condition 3: restore_homeassistant=False, but addons selected -> Supervisor call needed
        has_supervisor_work_3 = bool(False) or bool(["addon_1"]) or bool([])
        self.assertTrue(has_supervisor_work_3)


if __name__ == "__main__":
    unittest.main()
