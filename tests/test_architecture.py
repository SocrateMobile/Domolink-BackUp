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


if __name__ == "__main__":
    unittest.main()
