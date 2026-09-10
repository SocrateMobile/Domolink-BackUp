"""DomoLink-BackUp integration for Home Assistant.

Provides remote off-site backup storage (NAS, FTP/FTPS, WebDAV, Google Drive, Local share),
native Home Assistant BackupAgent registration, custom sensors, buttons, and a dedicated
sidebar dashboard panel.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import glob
import logging
import os
import time
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.websocket_api import (
    async_register_command,
    websocket_command,
    ActiveConnection,
)
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backup import (
    DomoLinkBackupAgent,
    HAS_BACKUP_AGENT,
    notify_backup_agents_updated,
)
from .const import (
    CONF_AUTO_CLEAN_ENABLED,
    CONF_DESTINATION_TYPE,
    CONF_FTP_HOST,
    CONF_FTP_PASS,
    CONF_FTP_PATH,
    CONF_FTP_PORT,
    CONF_FTP_TLS,
    CONF_FTP_USER,
    CONF_GOOGLE_DRIVE_FOLDER_ID,
    CONF_GOOGLE_DRIVE_WEBHOOK_URL,
    CONF_LOCAL_SHARE_PATH,
    CONF_MAX_BACKUPS_COUNT,
    CONF_MAX_STORAGE_MB,
    CONF_NAS_TYPE,
    CONF_PROTOCOL,
    CONF_RETENTION_DAYS,
    CONF_TELEGRAM_CHAT_ID,
    CONF_TELEGRAM_ENABLED,
    CONF_TELEGRAM_TOKEN,
    CONF_WEBDAV_PASS,
    CONF_WEBDAV_PATH,
    CONF_WEBDAV_URL,
    CONF_WEBDAV_USER,
    CONF_WEBDAV_VERIFY_SSL,
    DEFAULT_MAX_STORAGE_MB,
    DEFAULT_NAME,
    DOMAIN,
    NAME,
    SERVICE_CLEAN_OLD_BACKUPS,
    SERVICE_CREATE_BACKUP,
    SERVICE_SYNC_BACKUPS,
    SERVICE_TEST_CONNECTION,
    SERVICE_UPLOAD_BACKUP,
    STATE_BACKING_UP,
    STATE_CLEANING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_SUCCESS,
    STATE_UPLOADING,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERSION,
)
from .notifier import DomoLinkNotifier
from .storage_engine import DomoLinkStorageEngine

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "button"]
if HAS_BACKUP_AGENT:
    PLATFORMS.append("backup")


class DomoLinkBackupCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator managing DomoLink-BackUp state, tasks and synchronization."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        storage_engine: DomoLinkStorageEngine,
        notifier: DomoLinkNotifier,
        store: Store,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=NAME,
            update_interval=None,  # Updated on events
        )
        self.entry = entry
        self.storage_engine = storage_engine
        self.notifier = notifier
        self.store = store

        # Default state
        self.data: dict[str, Any] = {
            "status": STATE_IDLE,
            "is_busy": False,
            "last_action": "Prêt",
            "last_error": "",
            "destination_label": self.storage_engine.destination_label,
            "last_backup_name": "",
            "last_backup_date": None,
            "last_backup_size_mb": 0.0,
            "last_upload_duration_sec": 0.0,
            "total_backups_count": 0,
            "total_storage_mb": 0.0,
            "max_storage_mb": self.entry.options.get(CONF_MAX_STORAGE_MB, self.entry.data.get(CONF_MAX_STORAGE_MB, DEFAULT_MAX_STORAGE_MB)),
            "connection_status": "Prêt",
            "connection_latency": 0.0,
            "connection_message": "",
            "connection_last_checked": "",
            "backups_list": [],
            "test_logs": [],
        }

    async def async_init_load(self) -> None:
        """Load stored metadata from persistent storage."""
        stored = await self.store.async_load()
        if stored and isinstance(stored, dict):
            for k, v in stored.items():
                if k in self.data and v is not None:
                    self.data[k] = v
        self.data["destination_label"] = self.storage_engine.destination_label
        self.async_set_updated_data(self.data)

        # Initial background sync
        self.hass.async_create_task(self.async_refresh_backups_list())

    async def _async_save(self) -> None:
        """Persist state to storage."""
        to_save = {
            "last_backup_name": self.data.get("last_backup_name"),
            "last_backup_date": self.data.get("last_backup_date"),
            "last_backup_size_mb": self.data.get("last_backup_size_mb"),
            "last_upload_duration_sec": self.data.get("last_upload_duration_sec"),
            "connection_status": self.data.get("connection_status"),
            "connection_message": self.data.get("connection_message"),
            "connection_last_checked": self.data.get("connection_last_checked"),
        }
        await self.store.async_save(to_save)

    def set_status(self, status: str, action: str = "", is_busy: bool = False, error: str = "") -> None:
        """Update internal status and notify state listeners."""
        self.data["status"] = status
        self.data["is_busy"] = is_busy
        if action:
            self.data["last_action"] = action
        if error:
            self.data["last_error"] = error
        self.async_set_updated_data(self.data)

    async def async_refresh_backups_list(self) -> list[dict[str, Any]]:
        """Fetch remote backups list and refresh statistics."""
        try:
            backups = await self.storage_engine.async_list_backups()
            total_bytes = sum(b.get("size", 0) for b in backups)
            total_mb = round(total_bytes / (1024 * 1024), 2)

            self.data["backups_list"] = backups
            self.data["total_backups_count"] = len(backups)
            self.data["total_storage_mb"] = total_mb
            self.data["destination_label"] = self.storage_engine.destination_label

            if backups and not self.data.get("last_backup_date"):
                # Sort to find most recent
                def _dt(x):
                    try:
                        return datetime.fromisoformat(x.get("date", ""))
                    except Exception:
                        return datetime.min.replace(tzinfo=timezone.utc)

                latest = max(backups, key=_dt)
                self.data["last_backup_name"] = latest.get("name", "")
                self.data["last_backup_date"] = latest.get("date")
                self.data["last_backup_size_mb"] = round(latest.get("size", 0) / (1024 * 1024), 2)

            self.async_set_updated_data(self.data)
            return backups
        except Exception as err:
            _LOGGER.warning("DomoLink-BackUp: Échec rafraîchissement des sauvegardes distantes: %s", err)
            return []

    async def async_run_test_connection(self) -> dict[str, Any]:
        """Execute a full connection test and update sensors."""
        self.set_status("Test de connexion en cours...", "Vérification réseau", is_busy=True)
        res = await self.storage_engine.async_test_connection()

        self.data["connection_status"] = res.get("result_label", "Inconnu")
        self.data["connection_latency"] = res.get("elapsed", 0.0)
        self.data["connection_message"] = res.get("message", "")
        self.data["connection_last_checked"] = datetime.now(timezone.utc).isoformat()
        self.data["test_logs"] = res.get("logs", [])

        self.set_status(STATE_IDLE, "Test de connexion terminé", is_busy=False)
        await self._async_save()
        return res

    async def async_create_and_upload_backup(
        self,
        name: str | None = None,
        include_database: bool = True,
    ) -> bool:
        """Create a Home Assistant backup archive and upload it to remote storage."""
        if self.data.get("is_busy"):
            _LOGGER.warning("DomoLink-BackUp: Une opération de sauvegarde est déjà en cours.")
            return False

        backup_title = name or f"DomoLink-BackUp_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
        dest_label = self.storage_engine.destination_label
        start_time = time.monotonic()

        self.set_status(STATE_BACKING_UP, f"Création locale de '{backup_title}'...", is_busy=True)
        await self.notifier.async_notify_start(backup_title, dest_label)

        # 1. Trigger Home Assistant native backup creation
        tar_path = None
        try:
            # Check for existing backup directory
            backup_dir = self.hass.config.path("backup")
            if not os.path.exists(backup_dir):
                os.makedirs(backup_dir, exist_ok=True)

            before_files = set(glob.glob(os.path.join(backup_dir, "*.tar")))

            # Call HA backup service
            service_data: dict[str, Any] = {"name": backup_title}
            if not include_database:
                service_data["include_database"] = False

            # Try modern backup service first
            try:
                if self.hass.services.has_service("backup", "create"):
                    await self.hass.services.async_call("backup", "create", service_data, blocking=True)
                elif self.hass.services.has_service("backup", "create_automatic"):
                    await self.hass.services.async_call("backup", "create_automatic", {}, blocking=True)
                elif self.hass.services.has_service("hassio", "backup_full"):
                    await self.hass.services.async_call("hassio", "backup_full", {"name": backup_title}, blocking=True)
                else:
                    _LOGGER.warning("DomoLink-BackUp: Aucun service de sauvegarde standard trouvé dans Home Assistant.")
            except Exception as srv_err:
                _LOGGER.warning("DomoLink-BackUp: Tentative d'appel du service de sauvegarde HA: %s", srv_err)

            # Wait a moment for file to finalize
            await asyncio.sleep(2)
            after_files = set(glob.glob(os.path.join(backup_dir, "*.tar")))
            new_files = list(after_files - before_files)

            if new_files:
                tar_path = max(new_files, key=os.path.getmtime)
            elif after_files:
                # Find most recently modified .tar in backup directory
                tar_path = max(after_files, key=os.path.getmtime)

            if not tar_path or not os.path.exists(tar_path):
                raise FileNotFoundError("Impossible d'identifier l'archive de sauvegarde créée par Home Assistant dans /backup")

            filename = os.path.basename(tar_path)
            file_size = os.path.getsize(tar_path)

            # 2. Upload to remote destination
            self.set_status(STATE_UPLOADING, f"Téléversement de {filename} vers {dest_label}...", is_busy=True)

            upload_success = await self.storage_engine.async_upload(
                source=tar_path,
                filename=filename,
                size=file_size,
            )

            elapsed = time.monotonic() - start_time
            if upload_success:
                size_mb = round(file_size / (1024 * 1024), 2)
                self.data["last_backup_name"] = filename
                self.data["last_backup_date"] = datetime.now(timezone.utc).isoformat()
                self.data["last_backup_size_mb"] = size_mb
                self.data["last_upload_duration_sec"] = round(elapsed, 1)

                await self.async_refresh_backups_list()
                self.set_status(STATE_SUCCESS, f"Sauvegarde réussie ({round(elapsed, 1)}s)", is_busy=False)

                await self.notifier.async_notify_success(
                    backup_name=filename,
                    destination=dest_label,
                    size_bytes=file_size,
                    duration_sec=elapsed,
                    remaining_count=self.data.get("total_backups_count", 1),
                    total_size_mb=self.data.get("total_storage_mb", size_mb),
                )
                await self._async_save()
                return True
            else:
                self.set_status(STATE_ERROR, "Échec du transfert distant", is_busy=False, error="Échec du transfert")
                await self.notifier.async_notify_error(backup_title, dest_label, "Échec du transfert vers le serveur distant")
                return False

        except Exception as err:
            _LOGGER.exception("DomoLink-BackUp: Erreur pendant la sauvegarde: %s", err)
            self.set_status(STATE_ERROR, f"Erreur : {err}", is_busy=False, error=str(err))
            await self.notifier.async_notify_error(backup_title, dest_label, str(err))
            return False

    async def async_upload_file(self, file_path: str) -> bool:
        """Upload an existing tar archive file to remote destination."""
        if not os.path.exists(file_path):
            _LOGGER.error("DomoLink-BackUp: Le fichier spécifié n'existe pas : %s", file_path)
            return False

        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        dest_label = self.storage_engine.destination_label
        start_time = time.monotonic()

        self.set_status(STATE_UPLOADING, f"Téléversement de {filename}...", is_busy=True)
        await self.notifier.async_notify_start(filename, dest_label)

        success = await self.storage_engine.async_upload(
            source=file_path,
            filename=filename,
            size=file_size,
        )

        elapsed = time.monotonic() - start_time
        if success:
            size_mb = round(file_size / (1024 * 1024), 2)
            self.data["last_backup_name"] = filename
            self.data["last_backup_date"] = datetime.now(timezone.utc).isoformat()
            self.data["last_backup_size_mb"] = size_mb
            self.data["last_upload_duration_sec"] = round(elapsed, 1)

            await self.async_refresh_backups_list()
            self.set_status(STATE_SUCCESS, "Téléversement réussi", is_busy=False)

            await self.notifier.async_notify_success(
                backup_name=filename,
                destination=dest_label,
                size_bytes=file_size,
                duration_sec=elapsed,
                remaining_count=self.data.get("total_backups_count", 1),
                total_size_mb=self.data.get("total_storage_mb", size_mb),
            )
            await self._async_save()
            return True
        else:
            self.set_status(STATE_ERROR, "Échec du téléversement", is_busy=False, error="Échec du transfert")
            await self.notifier.async_notify_error(filename, dest_label, "Échec de téléversement")
            return False

    async def async_apply_retention(self) -> dict[str, Any]:
        """Apply FIFO retention policy."""
        self.set_status(STATE_CLEANING, "Application des quotas de rétention...", is_busy=True)
        res = await self.storage_engine.async_apply_retention()
        await self.async_refresh_backups_list()
        self.set_status(STATE_IDLE, f"Nettoyage terminé ({res.get('deleted_count', 0)} purgées)", is_busy=False)
        return res

    async def async_delete_backup(self, backup_id: str) -> bool:
        """Delete specific remote backup archive."""
        success = await self.storage_engine.async_delete_backup(backup_id)
        if success:
            await self.async_refresh_backups_list()
        return success


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DomoLink-BackUp from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    config = {**entry.data, **entry.options}
    storage_engine = DomoLinkStorageEngine(hass, config)
    notifier = DomoLinkNotifier(hass, config)
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")

    coordinator = DomoLinkBackupCoordinator(hass, entry, storage_engine, notifier, store)
    await coordinator.async_init_load()

    entry_data: dict[str, Any] = {
        "storage_engine": storage_engine,
        "notifier": notifier,
        "coordinator": coordinator,
        "entry": entry,
    }

    # Setup BackupAgent if supported
    if HAS_BACKUP_AGENT:
        agent = DomoLinkBackupAgent(hass, entry.entry_id, entry.title)
        entry_data["backup_agent"] = agent
        notify_backup_agents_updated(hass)

    hass.data[DOMAIN][entry.entry_id] = entry_data

    # ─── Enregistrement du Panneau Frontend dans la Barre Latérale ───
    frontend_dir = hass.config.path("custom_components/domolink_backup/frontend")
    if os.path.exists(frontend_dir):
        if hasattr(hass.http, "async_register_static_paths"):
            await hass.http.async_register_static_paths(
                [StaticPathConfig("/domolink_backup_frontend", frontend_dir, cache_headers=False)]
            )
        else:
            hass.http.register_static_path(
                "/domolink_backup_frontend",
                frontend_dir,
                cache_headers=False,
            )

        try:
            frontend.async_register_built_in_panel(
                hass,
                component_name="custom",
                sidebar_title=NAME,  # "DomoLink-BackUp" as requested!
                sidebar_icon="mdi:archive-arrow-up",
                frontend_url_path="domolink_backup",
                config={
                    "_panel_custom": {
                        "name": "domolink-backup-panel",
                        "module_url": f"/domolink_backup_frontend/domolink-backup-panel.js?v={VERSION}",
                    }
                },
                require_admin=False,
            )
            _LOGGER.info("DomoLink-BackUp: Panneau latéral enregistré avec le titre '%s'", NAME)
        except ValueError:
            # Panel already registered
            pass

    # ─── Enregistrement des Services Home Assistant ───
    async def _handle_create_backup(call: ServiceCall) -> None:
        name = call.data.get("name")
        include_db = call.data.get("include_database", True)
        await coordinator.async_create_and_upload_backup(name, include_db)

    async def _handle_upload_backup(call: ServiceCall) -> None:
        file_path = call.data.get("file_path")
        if file_path:
            await coordinator.async_upload_file(file_path)

    async def _handle_test_connection(call: ServiceCall) -> None:
        await coordinator.async_run_test_connection()

    async def _handle_clean_old_backups(call: ServiceCall) -> None:
        await coordinator.async_apply_retention()

    async def _handle_sync_backups(call: ServiceCall) -> None:
        await coordinator.async_refresh_backups_list()

    hass.services.async_register(DOMAIN, SERVICE_CREATE_BACKUP, _handle_create_backup)
    hass.services.async_register(DOMAIN, SERVICE_UPLOAD_BACKUP, _handle_upload_backup)
    hass.services.async_register(DOMAIN, SERVICE_TEST_CONNECTION, _handle_test_connection)
    hass.services.async_register(DOMAIN, SERVICE_CLEAN_OLD_BACKUPS, _handle_clean_old_backups)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_BACKUPS, _handle_sync_backups)

    # ─── Enregistrement des commandes WebSocket pour le Dashboard UI ───
    _register_websocket_commands(hass, coordinator)

    # Forward setup to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


def _register_websocket_commands(hass: HomeAssistant, coordinator: DomoLinkBackupCoordinator) -> None:
    """Register WebSocket API handlers for the frontend dashboard panel."""

    @websocket_command({vol.Required("type"): "domolink_backup/get_data"})
    @callback
    def ws_get_data(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        cfg = {**coordinator.entry.data, **coordinator.entry.options}
        # Mask sensitive passwords before sending to frontend
        safe_cfg = dict(cfg)
        for secret_key in (CONF_FTP_PASS, CONF_WEBDAV_PASS, CONF_TELEGRAM_TOKEN):
            if safe_cfg.get(secret_key):
                safe_cfg[secret_key] = "********"

        connection.send_result(
            msg["id"],
            {
                "data": coordinator.data,
                "config": safe_cfg,
                "version": VERSION,
            },
        )

    @websocket_command({
        vol.Required("type"): "domolink_backup/trigger_backup",
        vol.Optional("name"): str,
        vol.Optional("include_database", default=True): bool,
    })
    async def ws_trigger_backup(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        name = msg.get("name")
        include_db = msg.get("include_database", True)
        hass.async_create_task(coordinator.async_create_and_upload_backup(name, include_db))
        connection.send_result(msg["id"], {"status": "started"})

    @websocket_command({vol.Required("type"): "domolink_backup/test_connection"})
    async def ws_test_connection(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        res = await coordinator.async_run_test_connection()
        connection.send_result(msg["id"], res)

    @websocket_command({vol.Required("type"): "domolink_backup/clean_backups"})
    async def ws_clean_backups(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        res = await coordinator.async_apply_retention()
        connection.send_result(msg["id"], res)

    @websocket_command({
        vol.Required("type"): "domolink_backup/delete_backup",
        vol.Required("backup_id"): str,
    })
    async def ws_delete_backup(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        backup_id = msg["backup_id"]
        success = await coordinator.async_delete_backup(backup_id)
        connection.send_result(msg["id"], {"success": success})

    try:
        async_register_command(hass, ws_get_data)
        async_register_command(hass, ws_trigger_backup)
        async_register_command(hass, ws_test_connection)
        async_register_command(hass, ws_clean_backups)
        async_register_command(hass, ws_delete_backup)
    except Exception:
        pass


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if HAS_BACKUP_AGENT:
            notify_backup_agents_updated(hass)

        if not hass.data[DOMAIN]:
            try:
                frontend.async_remove_panel(hass, "domolink_backup")
            except Exception:
                pass

    return unload_ok
