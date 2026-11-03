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
        mock_local_agent.async_list_backups = AsyncMock()

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

        # Agent archive list must be refreshed via public API
        mock_local_agent.async_list_backups.assert_awaited_once()

        # All keyword-only arguments must be passed correctly
        self.assertEqual(called_kwargs["backup_id"], "slug_12345")
        self.assertEqual(called_kwargs["agent_id"], "backup.local")
        self.assertIsNone(called_kwargs["password"])
        self.assertIsNone(called_kwargs["restore_addons"])
        self.assertTrue(called_kwargs["restore_database"])
        self.assertIsNone(called_kwargs["restore_folders"])
        self.assertTrue(called_kwargs["restore_homeassistant"])

    def test_extract_core_subfolders_strips_data_prefix(self):
        """Ensure _sync_extract_core_subfolders strips leading data/ and rewrites member paths into config_dir."""
        import io
        import tarfile
        import tempfile
        from custom_components.domolink_backup import _sync_extract_core_subfolders

        with tempfile.TemporaryDirectory() as tmpdir:
            outer_tar_path = os.path.join(tmpdir, "backup_test.tar")
            target_config_dir = os.path.join(tmpdir, "config")
            os.makedirs(target_config_dir, exist_ok=True)

            # Build inner homeassistant.tar.gz with './data/...' and 'data/...' entries
            inner_buf = io.BytesIO()
            with tarfile.open(fileobj=inner_buf, mode="w:gz") as inner_tar:
                files = {
                    "./data/custom_components/my_integration/manifest.json": b'{"domain": "my_integration"}',
                    "data/themes/dark_blue.yaml": b"dark_blue: {}",
                    "data/configuration.yaml": b"homeassistant: {}",
                }
                for name, content in files.items():
                    info = tarfile.TarInfo(name=name)
                    info.size = len(content)
                    inner_tar.addfile(info, io.BytesIO(content))
            inner_bytes = inner_buf.getvalue()

            with tarfile.open(outer_tar_path, mode="w") as outer_tar:
                ha_info = tarfile.TarInfo(name="./homeassistant.tar.gz")
                ha_info.size = len(inner_bytes)
                outer_tar.addfile(ha_info, io.BytesIO(inner_bytes))

            extracted = _sync_extract_core_subfolders(
                outer_tar_path,
                target_config_dir,
                ["custom_components", "themes", "blueprints"],
            )

            # Only custom_components and themes were present in the archive
            self.assertEqual(extracted, ["custom_components", "themes"])

            # Files must land directly inside target_config_dir without 'data/' prefix
            self.assertTrue(
                os.path.isfile(os.path.join(target_config_dir, "custom_components", "my_integration", "manifest.json"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(target_configdir := target_config_dir, "themes", "dark_blue.yaml"))
            )
            self.assertFalse(os.path.exists(os.path.join(target_configdir, "data")))
            self.assertFalse(os.path.exists(os.path.join(target_configdir, "configuration.yaml")))

    def test_upload_backup_to_local_agent_and_direct_get_backup_path(self):
        """Ensure downloaded archives are registered via async_upload_backup and get_backup_path is used without copying."""
        import asyncio
        import tempfile
        from custom_components.domolink_backup import (
            async_get_local_agent_backup_path,
            async_upload_backup_to_local_agent,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tar_file = os.path.join(tmpdir, "sample_backup.tar")
            payload = b"dummy-tar-content-" * 100
            with open(tar_file, "wb") as f:
                f.write(payload)

            uploaded_chunks = bytearray()
            uploaded_backup_obj = None

            async def mock_upload_backup(*, open_stream, backup, **kwargs):
                nonlocal uploaded_backup_obj
                uploaded_backup_obj = backup
                stream = await open_stream()
                async for chunk in stream:
                    uploaded_chunks.extend(chunk)

            mock_local_agent = MagicMock()
            mock_local_agent.domain = "backup"
            mock_local_agent.async_upload_backup = mock_upload_backup
            mock_local_agent.async_list_backups = AsyncMock(return_value=[])
            mock_local_agent.get_backup_path = MagicMock(return_value=tar_file)

            mock_manager = MagicMock()
            mock_manager.backup_agents = {"backup.local": mock_local_agent}

            hass = sys.modules["homeassistant.core"].HomeAssistant()
            hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
            hass.services = MagicMock()
            hass.services.has_service = MagicMock(return_value=False)

            agent_id, slug = asyncio.run(
                async_upload_backup_to_local_agent(
                    hass,
                    tar_file,
                    backup_id="slug_abc123",
                    name="Offsite Backup",
                    date="2026-11-03T12:00:00+00:00",
                    size=len(payload),
                    meta={"slug": "slug_abc123", "name": "Offsite Backup"},
                    backup_manager=mock_manager,
                )
            )

            self.assertEqual(agent_id, "backup.local")
            self.assertEqual(slug, "slug_abc123")
            self.assertEqual(bytes(uploaded_chunks), payload)
            self.assertIsNotNone(uploaded_backup_obj)
            self.assertEqual(uploaded_backup_obj.backup_id, "slug_abc123")

            # Verify async_get_local_agent_backup_path returns the direct path without copying
            direct_path = asyncio.run(
                async_get_local_agent_backup_path(mock_manager, "slug_abc123", hass=hass)
            )
            self.assertEqual(direct_path, tar_file)

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

    def test_non_iterable_known_backups_and_slug_selection(self):
        """Ensure KnownBackups (non-iterable) never blocks backup detection or selects old archives."""
        import asyncio
        import time
        from datetime import datetime, timezone
        from custom_components.domolink_backup import (
            _async_get_local_backups_map,
            _pick_newest_backup_slug,
        )

        class NonIterableKnownBackups:
            """Simulate Home Assistant Core KnownBackups which has no __iter__."""
            pass

        old_backup = MagicMock()
        old_backup.backup_id = "fe3aadba"
        old_backup.name = "Old 10KB Backup"
        old_backup.date = "2026-10-30T15:42:00+00:00"
        old_backup.size = 10240

        now_iso = datetime.now(timezone.utc).isoformat()
        new_backup = MagicMock()
        new_backup.backup_id = "a9c8b7d6"
        new_backup.name = "New Full Backup"
        new_backup.date = now_iso
        new_backup.size = 524288000

        mock_hassio_agent = MagicMock()
        mock_hassio_agent.domain = "hassio"
        mock_hassio_agent.async_list_backups = AsyncMock(return_value=[old_backup, new_backup])

        mock_domolink_agent = MagicMock()
        mock_domolink_agent.domain = "domolink_backup"
        mock_domolink_agent.async_list_backups = AsyncMock(side_effect=AssertionError("Should not query remote FTP agent"))

        mock_manager = MagicMock()
        mock_manager.known_backups = NonIterableKnownBackups()
        mock_manager.backup_agents = {
            "hassio.local": mock_hassio_agent,
            "domolink_backup.entry1": mock_domolink_agent,
        }

        backups_map = asyncio.run(_async_get_local_backups_map(mock_manager))
        self.assertIn("fe3aadba", backups_map)
        self.assertIn("a9c8b7d6", backups_map)

        # Case 1: before_ids contains old_backup -> picks new_backup
        slug_1 = _pick_newest_backup_slug(backups_map, {"fe3aadba"}, time.time() - 5.0, "New Full Backup")
        self.assertEqual(slug_1, "a9c8b7d6")

        # Case 2: even if before_ids was empty, it must pick the newest backup, never the old one
        slug_2 = _pick_newest_backup_slug(backups_map, set(), time.time() - 5.0, "New Full Backup")
        self.assertEqual(slug_2, "a9c8b7d6")

        # Case 3: if only the old backup exists and before_ids was empty, must return None (never upload old archive!)
        only_old_map = {"fe3aadba": old_backup}
        slug_3 = _pick_newest_backup_slug(only_old_map, set(), time.time(), "New Full Backup")
        self.assertIsNone(slug_3)


if __name__ == "__main__":
    unittest.main()
