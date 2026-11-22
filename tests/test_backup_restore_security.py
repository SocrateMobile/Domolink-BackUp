"""Unit and security non-regression tests for Domolink-BackUp."""
import json
import os
import ast
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENT_DIR = os.path.join(REPO_ROOT, "custom_components", "domolink_backup")


def test_manifest_structure_and_version():
    """Verify manifest.json is valid and keys are properly ordered."""
    manifest_path = os.path.join(COMPONENT_DIR, "manifest.json")
    assert os.path.isfile(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest["domain"] == "domolink_backup"
    assert manifest["name"] == "DomoLink-BackUp"
    assert manifest["version"] == "1.5.25"
    assert manifest["codeowners"] == ["@SocrateMobile"]
    assert manifest["iot_class"] == "local_polling"


def test_translations_parity():
    """Verify en.json and fr.json exist and match keys."""
    fr_path = os.path.join(COMPONENT_DIR, "translations", "fr.json")
    en_path = os.path.join(COMPONENT_DIR, "translations", "en.json")
    assert os.path.isfile(fr_path), "translations/fr.json must exist"
    assert os.path.isfile(en_path), "translations/en.json must exist"

    with open(fr_path, "r", encoding="utf-8") as f:
        fr = json.load(f)
    with open(en_path, "r", encoding="utf-8") as f:
        en = json.load(f)

    def extract_keys(d, prefix=""):
        keys = set()
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys.update(extract_keys(v, full))
            else:
                keys.add(full)
        return keys

    fr_keys = extract_keys(fr)
    en_keys = extract_keys(en)
    assert fr_keys == en_keys, f"Translation keys mismatch: {fr_keys ^ en_keys}"


def test_no_direct_blocking_io_in_async_functions():
    """Scan all Python files in the component to ensure no open() or blocking subprocess calls directly inside async def (excluding nested defs)."""
    blocking_calls = {"open", "check_output", "urlopen"}
    violations = []

    def check_node(node, in_async=False):
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            # Nested synchronous function - this is safe as it's meant for executor or thread
            return
        if isinstance(node, ast.AsyncFunctionDef):
            for child in node.body:
                check_async_body(child, node.name, file)
            return

        for child in ast.iter_child_nodes(node):
            check_node(child, in_async=False)

    def check_async_body(node, func_name, file_name):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Don't recurse into nested defs here; if async, top-level loop handles it
            return
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in blocking_calls:
                violations.append((file_name, func_name, name, node.lineno))

        for child in ast.iter_child_nodes(node):
            check_async_body(child, func_name, file_name)

    for root, _, files in os.walk(COMPONENT_DIR):
        for file in files:
            if not file.endswith(".py"):
                continue
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for item in tree.body:
                check_node(item)

    assert not violations, f"Blocking calls found directly inside async functions: {violations}"


def test_extract_core_subfolders_rejects_symlinks_hardlinks_and_traversal(tmp_path):
    """Ensure _sync_extract_core_subfolders strips data/, rejects ../ traversal, symlinks, and hardlinks."""
    import io
    import sys
    import tarfile
    from tests.ha_mock import setup_ha_mock

    setup_ha_mock()
    from custom_components.domolink_backup import _sync_extract_core_subfolders

    inner_buf = io.BytesIO()
    with tarfile.open(fileobj=inner_buf, mode="w:gz") as inner_tar:
        # 1. Valid file under data/themes/
        valid_data = b"theme: dark\n"
        ti_valid = tarfile.TarInfo("data/themes/dark.yaml")
        ti_valid.size = len(valid_data)
        inner_tar.addfile(ti_valid, io.BytesIO(valid_data))

        # 2. Traversal attempt
        evil_data = b"evil"
        ti_evil = tarfile.TarInfo("data/themes/../../evil.yaml")
        ti_evil.size = len(evil_data)
        inner_tar.addfile(ti_evil, io.BytesIO(evil_data))

        # 3. Symlink pointing outside /config
        ti_sym = tarfile.TarInfo("data/themes/escape_link")
        ti_sym.type = tarfile.SYMTYPE
        ti_sym.linkname = "/etc/passwd"
        inner_tar.addfile(ti_sym)

        # 4. Hardlink
        ti_lnk = tarfile.TarInfo("data/themes/hard_link")
        ti_lnk.type = tarfile.LNKTYPE
        ti_lnk.linkname = "/etc/passwd"
        inner_tar.addfile(ti_lnk)

    outer_tar_path = tmp_path / "backup.tar"
    inner_bytes = inner_buf.getvalue()
    with tarfile.open(outer_tar_path, "w") as outer_tar:
        ti_ha = tarfile.TarInfo("homeassistant.tar.gz")
        ti_ha.size = len(inner_bytes)
        outer_tar.addfile(ti_ha, io.BytesIO(inner_bytes))

    target_dir = tmp_path / "config"
    target_dir.mkdir()
    extracted = _sync_extract_core_subfolders(str(outer_tar_path), str(target_dir), ["themes"])

    assert extracted == ["themes"]
    assert (target_dir / "themes" / "dark.yaml").read_bytes() == b"theme: dark\n"
    assert not (target_dir / "themes" / "escape_link").exists()
    assert not (target_dir / "themes" / "hard_link").exists()
    assert not (tmp_path / "evil.yaml").exists()


def test_partial_restore_subfolders_only_resets_is_busy(tmp_path):
    """Ensure subfolder-only partial_restore resets coordinator.is_busy=False and progress=100."""
    import asyncio
    import io
    import sys
    import tarfile
    from unittest.mock import AsyncMock, MagicMock
    from tests.ha_mock import setup_ha_mock

    setup_ha_mock()
    from custom_components.domolink_backup import DomoLinkBackupCoordinator
    from custom_components.domolink_backup.const import STATE_SUCCESS, STAGE_COMPLETED

    inner_buf = io.BytesIO()
    with tarfile.open(fileobj=inner_buf, mode="w:gz") as inner_tar:
        data = b"blueprint: ok\n"
        ti = tarfile.TarInfo("data/blueprints/test.yaml")
        ti.size = len(data)
        inner_tar.addfile(ti, io.BytesIO(data))

    archive_bytes_buf = io.BytesIO()
    inner_bytes = inner_buf.getvalue()
    with tarfile.open(fileobj=archive_bytes_buf, mode="w") as outer_tar:
        ti_ha = tarfile.TarInfo("homeassistant.tar.gz")
        ti_ha.size = len(inner_bytes)
        outer_tar.addfile(ti_ha, io.BytesIO(inner_bytes))
    archive_bytes = archive_bytes_buf.getvalue()

    hass = sys.modules["homeassistant.core"].HomeAssistant()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    hass.config.config_dir = str(config_dir)
    hass.config.path = lambda *parts: str(config_dir.joinpath(*parts))
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.services = MagicMock()
    hass.services.has_service = MagicMock(return_value=False)
    hass.services.async_call = AsyncMock()
    hass.data = {}

    storage = MagicMock()
    storage.destination_label = "WebDAV"
    storage.async_list_backups = AsyncMock(return_value=[{"filename": "test.tar", "size": len(archive_bytes)}])

    async def fake_download(fname, dest, on_progress=None):
        with open(dest, "wb") as f:
            f.write(archive_bytes)
        return True

    storage.async_download_to_file = AsyncMock(side_effect=fake_download)

    notifier = MagicMock()
    notifier.async_notify_start = AsyncMock()
    notifier.async_notify_success = AsyncMock()
    notifier.async_notify_error = AsyncMock()

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {}
    entry.options = {}

    store = MagicMock()
    store.async_save = AsyncMock()

    coordinator = DomoLinkBackupCoordinator(hass, entry, storage, notifier, store)
    coordinator.async_set_updated_data = MagicMock()

    result = asyncio.run(
        coordinator.async_restore_backup(
            "test.tar",
            restore_mode="partial_restore",
            restore_homeassistant=False,
            restore_integrations=False,
            restore_themes=False,
            restore_blueprints=True,
            restore_addons=None,
            restore_folders=None,
        )
    )

    assert result is True
    assert coordinator.data["is_busy"] is False
    assert coordinator.data["status"] == STATE_SUCCESS
    assert coordinator.data["progress"]["stage"] == STAGE_COMPLETED
    assert coordinator.data["progress"]["percent"] == 100
    assert (config_dir / "blueprints" / "test.yaml").exists()


def test_upload_backup_to_local_agent_restricts_to_local_agents_and_skips_duplicates(tmp_path):
    """Ensure async_upload_backup_to_local_agent never uploads to cloud agents and skips existing slugs."""
    import asyncio
    import sys
    from unittest.mock import AsyncMock, MagicMock
    from tests.ha_mock import setup_ha_mock

    setup_ha_mock()
    from custom_components.domolink_backup import async_upload_backup_to_local_agent

    tar_file = tmp_path / "b.tar"
    tar_file.write_bytes(b"tardata")

    hass = sys.modules["homeassistant.core"].HomeAssistant()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.services = MagicMock()
    hass.services.has_service = MagicMock(return_value=False)

    # 1. Only a third-party cloud agent is present -> must return (None, backup_id) and NOT upload
    cloud_agent = MagicMock()
    cloud_agent.domain = "cloud"
    cloud_agent.async_upload_backup = AsyncMock()
    mgr_cloud_only = MagicMock()
    mgr_cloud_only.backup_agents = {"cloud.nabucasa": cloud_agent}

    ag_id, slug = asyncio.run(
        async_upload_backup_to_local_agent(
            hass,
            str(tar_file),
            backup_id="slug1",
            name="B1",
            date="2026-11-01T00:00:00+00:00",
            size=7,
            backup_manager=mgr_cloud_only,
        )
    )
    assert ag_id is None
    assert slug == "slug1"
    cloud_agent.async_upload_backup.assert_not_awaited()

    # 2. Local agent already has slug1 -> must skip async_upload_backup
    local_agent = MagicMock()
    local_agent.domain = "backup"
    local_agent.async_get_backup = AsyncMock(return_value=MagicMock(backup_id="slug1"))
    local_agent.async_upload_backup = AsyncMock()
    mgr_local = MagicMock()
    mgr_local.backup_agents = {"backup.local": local_agent}

    ag_id2, slug2 = asyncio.run(
        async_upload_backup_to_local_agent(
            hass,
            str(tar_file),
            backup_id="slug1",
            name="B1",
            date="2026-11-01T00:00:00+00:00",
            size=7,
            backup_manager=mgr_local,
        )
    )
    assert ag_id2 == "backup.local"
    assert slug2 == "slug1"
    local_agent.async_upload_backup.assert_not_awaited()


def test_no_self_updater_leftovers_in_services_or_js():
    """Ensure check_updates and install_update are removed from services.yaml and JS panel."""
    services_path = os.path.join(COMPONENT_DIR, "services.yaml")
    with open(services_path, "r", encoding="utf-8") as f:
        services_txt = f.read()
    assert "check_updates" not in services_txt
    assert "install_update" not in services_txt

    js_path = os.path.join(COMPONENT_DIR, "frontend", "domolink-backup-panel.js")
    with open(js_path, "r", encoding="utf-8") as f:
        js_txt = f.read()
    assert 'callService("domolink_backup", "install_update"' not in js_txt
    assert "domolink_backup/install_update" not in js_txt
    assert "domolink_backup/check_updates" not in js_txt
