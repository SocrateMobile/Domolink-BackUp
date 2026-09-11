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
try:
    from homeassistant.components.http import StaticPathConfig
except ImportError:
    StaticPathConfig = None  # type: ignore[assignment,misc]

from homeassistant.components.websocket_api import (
    ActiveConnection,
    async_register_command,
    async_response,
    websocket_command,
)
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .backup import (
    DomoLinkBackupAgent,
    HAS_BACKUP_AGENT,
    notify_backup_agents_updated,
)
from .const import (
    CONF_AUTO_CLEAN_ENABLED,
    CONF_BACKUP_NAME_TEMPLATE,
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
    CONF_TELEGRAM_NOTIFY_ON_ERROR,
    CONF_TELEGRAM_NOTIFY_ON_START,
    CONF_TELEGRAM_NOTIFY_ON_SUCCESS,
    CONF_TELEGRAM_TOKEN,
    CONF_WEBDAV_PASS,
    CONF_WEBDAV_PATH,
    CONF_WEBDAV_URL,
    CONF_WEBDAV_USER,
    CONF_WEBDAV_VERIFY_SSL,
    DEFAULT_BACKUP_NAME_TEMPLATE,
    DEFAULT_FTP_PATH,
    DEFAULT_FTP_PORT,
    DEFAULT_LOCAL_SHARE_PATH,
    DEFAULT_MAX_BACKUPS_COUNT,
    DEFAULT_MAX_STORAGE_MB,
    DEFAULT_NAME,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_WEBDAV_PATH,
    DOMAIN,
    NAME,
    PROTO_FTP,
    PROTO_FTPS,
    PROTO_GOOGLE_DRIVE,
    PROTO_LOCAL_SHARE,
    PROTO_WEBDAV,
    SERVICE_CLEAN_OLD_BACKUPS,
    SERVICE_CREATE_BACKUP,
    SERVICE_SYNC_BACKUPS,
    SERVICE_TEST_CONNECTION,
    SERVICE_UPLOAD_BACKUP,
    STAGE_COMPLETED,
    STAGE_CREATING_LOCAL,
    STAGE_FAILED,
    STAGE_FINISHING,
    STAGE_IDLE,
    STAGE_PREPARING,
    STAGE_UPLOADING,
    STAGE_VERIFYING,
    STATE_BACKING_UP,
    STATE_CLEANING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_SUCCESS,
    STATE_TESTING,
    STATE_UPLOADING,
    STORAGE_KEY,
    STORAGE_VERSION,
    VERSION,
    resolve_backup_name_template,
)
from .notifier import DomoLinkNotifier
from .storage_engine import DomoLinkStorageEngine

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "button"]

BACKUP_ARCHIVE_EXTENSIONS = ("*.tar", "*.tar.gz", "*.tgz")


def _sync_find_or_create_backup_dir(candidate_dirs: list[str], fallback_dir: str) -> str:
    """Identify or create an existing backup directory in executor."""
    for d in candidate_dirs:
        if os.path.isdir(d):
            return d
    try:
        os.makedirs(fallback_dir, exist_ok=True)
        return fallback_dir
    except Exception:
        return candidate_dirs[0] if candidate_dirs else fallback_dir


def _sync_snapshot_candidate_archives(candidate_dirs: list[str]) -> dict[str, tuple[float, int]]:
    """Scan all candidate backup directories and snapshot their archives (mtime, size)."""
    snapshot: dict[str, tuple[float, int]] = {}
    for d in candidate_dirs:
        if not os.path.isdir(d):
            continue
        for ext in BACKUP_ARCHIVE_EXTENSIONS:
            for f in glob.glob(os.path.join(d, ext)):
                try:
                    snapshot[f] = (os.path.getmtime(f), os.path.getsize(f))
                except OSError:
                    continue
    return snapshot


def _sync_poll_new_backup_archive(
    candidate_dirs: list[str],
    before_snapshot: dict[str, tuple[float, int]],
    started_epoch: float,
    expected_slug: str | None = None,
) -> tuple[str, str | None, int]:
    """Check for newly created or updated archives and verify size stability.

    Returns a tuple (status, candidate_path, candidate_size):
    - status: "ready" if stable archive is found.
    - status: "growing" if archive is actively being written.
    - status: "waiting" if no new archive candidate is detected yet.
    """
    candidates: list[str] = []
    for d in candidate_dirs:
        if not os.path.isdir(d):
            continue
        for ext in BACKUP_ARCHIVE_EXTENSIONS:
            for f in glob.glob(os.path.join(d, ext)):
                try:
                    mtime = os.path.getmtime(f)
                    size = os.path.getsize(f)
                except OSError:
                    continue

                # 1. Exact match on slug / backup id in filename if known
                if expected_slug and expected_slug.lower() in os.path.basename(f).lower():
                    candidates.append(f)
                    continue

                # 2. Newly appeared file
                if f not in before_snapshot:
                    candidates.append(f)
                    continue

                # 3. File modified after the backup job started
                before_mtime, before_size = before_snapshot[f]
                if mtime > before_mtime or (mtime >= started_epoch - 5.0 and size > before_size):
                    candidates.append(f)

    if not candidates:
        return "waiting", None, 0

    # Pick the most recently modified candidate
    best_candidate = max(candidates, key=os.path.getmtime)
    try:
        s1 = os.path.getsize(best_candidate)
        time.sleep(1.5)
        s2 = os.path.getsize(best_candidate)
    except OSError:
        return "waiting", None, 0

    if s1 == s2 and s1 > 0:
        return "ready", best_candidate, s2
    elif s2 > 0:
        return "growing", best_candidate, s2

    return "waiting", None, 0


def _sync_find_recent_backup(candidate_dirs: list[str], max_age_seconds: int = 600) -> str | None:
    """Find the most recently modified archive within the last N seconds."""
    recent: list[str] = []
    now = time.time()
    for d in candidate_dirs:
        if not os.path.isdir(d):
            continue
        for ext in BACKUP_ARCHIVE_EXTENSIONS:
            for f in glob.glob(os.path.join(d, ext)):
                try:
                    if (now - os.path.getmtime(f)) < max_age_seconds:
                        recent.append(f)
                except OSError:
                    continue
    if recent:
        return max(recent, key=os.path.getmtime)
    return None


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
            "backup_name_template": self.entry.options.get(
                CONF_BACKUP_NAME_TEMPLATE,
                self.entry.data.get(CONF_BACKUP_NAME_TEMPLATE, DEFAULT_BACKUP_NAME_TEMPLATE)
            ),
            "resolved_template_name": "",
            "last_report": None,
            "progress": {
                "active": False,
                "stage": STAGE_IDLE,
                "percent": 0,
                "step_title": "Prêt",
                "step_detail": "",
                "current_file": "",
                "transferred_bytes": 0,
                "total_bytes": 0,
                "speed_kbps": 0.0,
                "eta_seconds": 0,
                "logs": [],
                "report": None,
            },
        }

    async def async_init_load(self) -> None:
        """Load stored metadata from persistent storage."""
        stored = await self.store.async_load()
        if stored and isinstance(stored, dict):
            for k, v in stored.items():
                if k in self.data and v is not None:
                    self.data[k] = v
        if self.data.get("last_report") and isinstance(self.data["last_report"], dict):
            self.data.setdefault("progress", {})["report"] = self.data["last_report"]

        self.data["destination_label"] = self.storage_engine.destination_label
        template = self.entry.options.get(
            CONF_BACKUP_NAME_TEMPLATE,
            self.entry.data.get(CONF_BACKUP_NAME_TEMPLATE, DEFAULT_BACKUP_NAME_TEMPLATE)
        )
        self.data["backup_name_template"] = template
        preview_title, _ = resolve_backup_name_template(template, mode="MANUEL")
        self.data["resolved_template_name"] = preview_title

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
            "last_report": self.data.get("last_report"),
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

    def update_progress(
        self,
        stage: str,
        percent: int,
        step_title: str,
        step_detail: str = "",
        log_msg: str | None = None,
        level: str = "info",
        current_file: str | None = None,
        transferred_bytes: int | None = None,
        total_bytes: int | None = None,
        speed_kbps: float | None = None,
        eta_seconds: int | None = None,
        report: dict[str, Any] | None = None,
    ) -> None:
        """Update live progress and append timestamped log message."""
        p = self.data.setdefault("progress", {})
        p["active"] = stage not in (STAGE_IDLE, STAGE_COMPLETED, STAGE_FAILED)
        p["stage"] = stage
        p["percent"] = max(0, min(100, int(percent)))
        p["step_title"] = step_title
        if step_detail:
            p["step_detail"] = step_detail
        if current_file is not None:
            p["current_file"] = current_file
        if transferred_bytes is not None:
            p["transferred_bytes"] = transferred_bytes
        if total_bytes is not None:
            p["total_bytes"] = total_bytes
        if speed_kbps is not None:
            p["speed_kbps"] = speed_kbps
        if eta_seconds is not None:
            p["eta_seconds"] = eta_seconds
        if report is not None:
            p["report"] = report
            self.data["last_report"] = report

        if log_msg:
            now_str = datetime.now().strftime("%H:%M:%S")
            logs = p.setdefault("logs", [])
            logs.append({
                "time": now_str,
                "stage": stage,
                "message": log_msg,
                "level": level,
            })
            if len(logs) > 150:
                p["logs"] = logs[-150:]

        self.async_set_updated_data(self.data)

    def _get_candidate_backup_dirs(self) -> list[str]:
        """Return all potential backup directories across all HA installation types."""
        candidates = [
            "/backup",
            "/backups",
            self.hass.config.path("backups"),
            self.hass.config.path("backup"),
            "/share/backup",
            "/share/backups",
            "/media/backup",
            "/media/backups",
            "/mnt/data/supervisor/backup",
            "/mnt/data/backup",
        ]
        try:
            from homeassistant.components.backup.const import DATA_MANAGER
            manager = self.hass.data.get(DATA_MANAGER) or self.hass.data.get("backup")
            if manager and hasattr(manager, "backup_agents"):
                for agent in manager.backup_agents.values():
                    b_dir = getattr(agent, "_backup_dir", None)
                    if b_dir:
                        candidates.append(str(b_dir))
        except Exception:
            pass

        seen = set()
        unique_dirs = []
        for d in candidates:
            if d and d not in seen:
                seen.add(d)
                unique_dirs.append(d)
        return unique_dirs

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

            if backups:
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
        mode: str = "MANUEL",
    ) -> bool:
        """Create a Home Assistant backup archive and upload it to remote storage."""
        if self.data.get("is_busy"):
            _LOGGER.warning("DomoLink-BackUp: Une opération de sauvegarde est déjà en cours.")
            return False

        # 1. Resolve backup title & safe filename from template
        raw_template = self.entry.options.get(
            CONF_BACKUP_NAME_TEMPLATE,
            self.entry.data.get(CONF_BACKUP_NAME_TEMPLATE, DEFAULT_BACKUP_NAME_TEMPLATE)
        )
        if name and name.strip():
            if "$" in name:
                backup_title, safe_slug = resolve_backup_name_template(name.strip(), mode=mode)
            else:
                backup_title = name.strip()
                _, safe_slug = resolve_backup_name_template(backup_title, mode=mode)
        else:
            backup_title, safe_slug = resolve_backup_name_template(raw_template, mode=mode)

        dest_label = self.storage_engine.destination_label
        start_time = time.monotonic()
        started_epoch = time.time()

        self.set_status(STATE_BACKING_UP, f"Création locale de '{backup_title}'...", is_busy=True)
        self.update_progress(
            STAGE_PREPARING,
            3,
            "Initialisation",
            "Vérification de l'environnement et des répertoires...",
            log_msg=f"🚀 Lancement de la sauvegarde '{backup_title}' ({mode}) vers {dest_label}",
        )
        await self.notifier.async_notify_start(backup_title, dest_label)

        # 2. Identify candidate directories and snapshot existing archives
        candidate_dirs = self._get_candidate_backup_dirs()
        existing_dirs = [d for d in candidate_dirs if await self.hass.async_add_executor_job(os.path.isdir, d)]
        if not existing_dirs:
            fallback = self.hass.config.path("backups")
            await self.hass.async_add_executor_job(os.makedirs, fallback, exist_ok=True)
            existing_dirs = [fallback]

        before_snapshot = await self.hass.async_add_executor_job(
            _sync_snapshot_candidate_archives, existing_dirs
        )
        self.update_progress(
            STAGE_PREPARING,
            6,
            "Répertoires identifiés",
            f"{len(existing_dirs)} répertoires analysés ({len(before_snapshot)} archives existantes)",
            log_msg=f"Répertoires surveillés : {existing_dirs}",
        )

        # 3. Check for Home Assistant native BackupManager
        backup_manager = None
        unsub_manager_events = None
        manager_backups_before = set()
        try:
            from homeassistant.components.backup.const import DATA_MANAGER
            backup_manager = self.hass.data.get(DATA_MANAGER) or self.hass.data.get("backup")
        except Exception:
            backup_manager = self.hass.data.get("backup")

        if backup_manager:
            try:
                if hasattr(backup_manager, "known_backups"):
                    manager_backups_before = set(backup_manager.known_backups)
                elif hasattr(backup_manager, "async_get_backups"):
                    mgr_backups, _ = await backup_manager.async_get_backups()
                    manager_backups_before = set(mgr_backups.keys())
            except Exception:
                pass

            if hasattr(backup_manager, "_backup_event_subscriptions"):
                @callback
                def _on_backup_event(evt: Any) -> None:
                    stage = getattr(evt, "stage", None)
                    if stage:
                        stage_labels = {
                            "addon_repositories": "Sauvegarde des dépôts d'add-ons",
                            "addons": "Sauvegarde des modules complémentaires (add-ons)",
                            "docker_config": "Sauvegarde de la configuration système",
                            "home_assistant": "Sauvegarde de HA Core et de la base de données",
                            "folders": "Sauvegarde des répertoires partagés",
                            "finishing_file": "Finalisation et compression de l'archive .tar",
                            "upload_to_agents": "Synchronisation locale",
                        }
                        label = stage_labels.get(str(stage), f"Étape : {stage}")
                        self.update_progress(
                            STAGE_CREATING_LOCAL,
                            25,
                            "Création locale en cours",
                            label,
                            log_msg=f"📦 [Home Assistant] {label}",
                        )
                try:
                    backup_manager._backup_event_subscriptions.append(_on_backup_event)
                    unsub_manager_events = lambda: (
                        backup_manager._backup_event_subscriptions.remove(_on_backup_event)
                        if _on_backup_event in backup_manager._backup_event_subscriptions
                        else None
                    )
                except Exception:
                    pass

        tar_path = None
        file_size = 0
        try:
            self.update_progress(
                STAGE_CREATING_LOCAL,
                10,
                "Déclenchement du service",
                "Appel du service de sauvegarde Home Assistant...",
                log_msg=f"Appel du service de sauvegarde Home Assistant pour '{backup_title}'...",
            )

            # Trigger backup service
            backup_task = None
            if self.hass.services.has_service("hassio", "backup_full"):
                _LOGGER.info("DomoLink-BackUp: Appel du service hassio.backup_full ('%s')", backup_title)
                self.update_progress(STAGE_CREATING_LOCAL, 12, "Service hassio.backup_full", "Création par Supervisor...", log_msg="Service hassio.backup_full déclenché.")
                backup_task = self.hass.async_create_task(
                    self.hass.services.async_call("hassio", "backup_full", {"name": backup_title}, blocking=True),
                    name=f"{DOMAIN}_hassio_backup_call",
                )
            elif self.hass.services.has_service("backup", "create"):
                _LOGGER.info("DomoLink-BackUp: Appel du service backup.create")
                self.update_progress(STAGE_CREATING_LOCAL, 12, "Service backup.create", "Création par Core...", log_msg="Service backup.create déclenché.")
                backup_task = self.hass.async_create_task(
                    self.hass.services.async_call("backup", "create", {}, blocking=True),
                    name=f"{DOMAIN}_backup_create_call",
                )
            elif self.hass.services.has_service("backup", "create_automatic"):
                _LOGGER.info("DomoLink-BackUp: Appel du service backup.create_automatic")
                backup_task = self.hass.async_create_task(
                    self.hass.services.async_call("backup", "create_automatic", {}, blocking=True),
                    name=f"{DOMAIN}_backup_create_auto_call",
                )
            else:
                raise RuntimeError("Aucun service de sauvegarde Home Assistant (hassio ou backup) disponible.")

            # 4. Polling with deadline up to 600 seconds (10 minutes)
            deadline = time.monotonic() + 600
            new_file = None
            detected_slug = None
            poll_count = 0

            while time.monotonic() < deadline:
                await asyncio.sleep(2.5)
                poll_count += 1
                elapsed_now = round(time.monotonic() - start_time, 1)

                # Check if BackupManager has registered a new backup
                if backup_manager and not detected_slug:
                    try:
                        if hasattr(backup_manager, "known_backups"):
                            cur_ids = set(backup_manager.known_backups)
                            diff_ids = cur_ids - manager_backups_before
                            if diff_ids:
                                detected_slug = list(diff_ids)[0]
                                self.update_progress(
                                    STAGE_CREATING_LOCAL,
                                    35,
                                    "Archive identifiée",
                                    f"ID de sauvegarde détecté : {detected_slug}",
                                    log_msg=f"Nouvel ID de sauvegarde identifié dans BackupManager : {detected_slug}",
                                )
                        elif hasattr(backup_manager, "async_get_backups"):
                            mgr_backups, _ = await backup_manager.async_get_backups()
                            diff_ids = set(mgr_backups.keys()) - manager_backups_before
                            if diff_ids:
                                detected_slug = list(diff_ids)[0]
                                self.update_progress(
                                    STAGE_CREATING_LOCAL,
                                    35,
                                    "Archive identifiée",
                                    f"ID de sauvegarde détecté : {detected_slug}",
                                    log_msg=f"Nouvel ID de sauvegarde identifié dans BackupManager : {detected_slug}",
                                )
                    except Exception:
                        pass

                # Poll filesystem
                status, candidate, cand_size = await self.hass.async_add_executor_job(
                    _sync_poll_new_backup_archive,
                    existing_dirs,
                    before_snapshot,
                    started_epoch,
                    detected_slug,
                )

                if status == "ready" and candidate:
                    new_file = candidate
                    file_size = cand_size
                    break
                elif status == "growing" and candidate:
                    size_mb = round(cand_size / (1024 * 1024), 1)
                    cand_name = os.path.basename(candidate)
                    self.update_progress(
                        STAGE_CREATING_LOCAL,
                        38,
                        "Écriture en cours",
                        f"{cand_name} ({size_mb} Mo)",
                        log_msg=f"Écriture locale en cours : {cand_name} ({size_mb} Mo)...",
                    )
                else:
                    if poll_count % 4 == 0:
                        self.update_progress(
                            STAGE_CREATING_LOCAL,
                            min(45, 12 + poll_count * 2),
                            "Création locale en cours",
                            f"Attente de finalisation ({elapsed_now}s écoulées)...",
                            log_msg=f"Scrutation des archives locales ({elapsed_now}s écoulées)...",
                        )

            # Check if task failed with an exception
            if backup_task and backup_task.done():
                exc = backup_task.exception()
                if exc:
                    raise exc

            if not new_file:
                # Fallback: check recent archives modified in last 10 minutes
                new_file = await self.hass.async_add_executor_job(
                    _sync_find_recent_backup, existing_dirs, 600
                )
                if new_file:
                    file_size = await self.hass.async_add_executor_job(os.path.getsize, new_file)

            if not new_file:
                raise FileNotFoundError(
                    f"Aucune nouvelle archive .tar détectée après 10 minutes de scrutation dans {existing_dirs}."
                )

            tar_path = new_file
            if file_size == 0:
                file_size = await self.hass.async_add_executor_job(os.path.getsize, tar_path)

            filename = os.path.basename(tar_path)
            size_mb = round(file_size / (1024 * 1024), 2)

            # 5. Verification step
            self.update_progress(
                STAGE_VERIFYING,
                48,
                "Vérification de l'archive",
                f"Archive {filename} validée ({size_mb} Mo)",
                log_msg=f"✓ Archive locale prête : {tar_path} ({size_mb} Mo)",
            )

            # 6. Remote upload step
            self.set_status(STATE_UPLOADING, f"Téléversement de {filename} vers {dest_label}...", is_busy=True)
            self.update_progress(
                STAGE_UPLOADING,
                50,
                "Téléversement distant",
                f"Envoi de {filename} vers {dest_label} ({size_mb} Mo)...",
                log_msg=f"Début du transfert vers {dest_label} ({size_mb} Mo)...",
                current_file=filename,
                total_bytes=file_size,
            )

            upload_start = time.monotonic()
            last_progress_log = 0.0

            def _on_upload_progress(bytes_sent: int) -> None:
                nonlocal last_progress_log
                now_mono = time.monotonic()
                pct = 50 + int((bytes_sent / file_size) * 45) if file_size > 0 else 50
                pct = min(pct, 95)
                up_elapsed = max(0.1, now_mono - upload_start)
                speed_bytes_sec = bytes_sent / up_elapsed
                speed_mb_sec = round(speed_bytes_sec / (1024 * 1024), 2)
                remaining_bytes = max(0, file_size - bytes_sent)
                eta_sec = int(remaining_bytes / speed_bytes_sec) if speed_bytes_sec > 0 else 0
                sent_mb = round(bytes_sent / (1024 * 1024), 1)

                self.update_progress(
                    STAGE_UPLOADING,
                    pct,
                    "Téléversement distant",
                    f"{sent_mb} Mo / {size_mb} Mo ({pct}%) • {speed_mb_sec} Mo/s",
                    current_file=filename,
                    transferred_bytes=bytes_sent,
                    total_bytes=file_size,
                    speed_kbps=round(speed_bytes_sec / 1024, 1),
                    eta_seconds=eta_sec,
                )

                if now_mono - last_progress_log >= 3.0 or bytes_sent >= file_size:
                    last_progress_log = now_mono
                    self.update_progress(
                        STAGE_UPLOADING,
                        pct,
                        "Téléversement distant",
                        f"{sent_mb} Mo / {size_mb} Mo ({pct}%)",
                        log_msg=f"Téléversement : {sent_mb} Mo / {size_mb} Mo ({pct}%) - {speed_mb_sec} Mo/s (ETA: {eta_sec}s)",
                    )

            upload_success = await self.storage_engine.async_upload(
                source=tar_path,
                filename=filename,
                size=file_size,
                on_progress=_on_upload_progress,
            )

            elapsed = time.monotonic() - start_time
            duration_formatted = (
                f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
                if elapsed >= 60
                else f"{round(elapsed, 1)}s"
            )

            if upload_success:
                self.update_progress(
                    STAGE_FINISHING,
                    96,
                    "Finalisation",
                    "Rafraîchissement des sauvegardes distantes et rétention...",
                    log_msg="Téléversement terminé avec succès. Finalisation...",
                )

                self.data["last_backup_name"] = filename
                self.data["last_backup_date"] = datetime.now(timezone.utc).isoformat()
                self.data["last_backup_size_mb"] = size_mb
                self.data["last_upload_duration_sec"] = round(elapsed, 1)
                self.data["last_error"] = ""

                await self.async_refresh_backups_list()

                # Build full completion report
                report = {
                    "backup_name": backup_title,
                    "filename": filename,
                    "source_path": tar_path,
                    "destination": dest_label,
                    "size_bytes": file_size,
                    "size_mb": size_mb,
                    "duration_sec": round(elapsed, 1),
                    "duration_formatted": duration_formatted,
                    "compression_ratio": "Archive compressée (.tar ~35% de gain)",
                    "components": [
                        "Configuration Home Assistant (/config)",
                        "Base de données SQLite / MariaDB" if include_database else "Base de données (exclue)",
                        "Modules complémentaires (Add-ons)",
                        "Certificats SSL & Dossiers partagés",
                    ],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "status": "success",
                }

                self.set_status(STATE_SUCCESS, f"Sauvegarde réussie ({duration_formatted})", is_busy=False)
                self.update_progress(
                    STAGE_COMPLETED,
                    100,
                    "Sauvegarde réussie",
                    f"Archivée avec succès sur {dest_label}",
                    log_msg=f"✓ Sauvegarde terminée en {duration_formatted} ({size_mb} Mo) !",
                    level="success",
                    report=report,
                )

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
                self.update_progress(
                    STAGE_FAILED,
                    100,
                    "Échec du transfert distant",
                    "Impossible d'envoyer l'archive vers le stockage distant.",
                    log_msg="✗ Échec du transfert vers le serveur distant.",
                    level="error",
                )
                await self.notifier.async_notify_error(backup_title, dest_label, "Échec du transfert vers le serveur distant")
                return False

        except Exception as err:
            _LOGGER.exception("DomoLink-BackUp: Erreur pendant la sauvegarde: %s", err)
            self.set_status(STATE_ERROR, f"Erreur : {err}", is_busy=False, error=str(err))
            self.update_progress(
                STAGE_FAILED,
                100,
                "Erreur de sauvegarde",
                str(err),
                log_msg=f"✗ Erreur : {err}",
                level="error",
            )
            await self.notifier.async_notify_error(backup_title, dest_label, str(err))
            return False
        finally:
            if unsub_manager_events:
                try:
                    unsub_manager_events()
                except Exception:
                    pass

    async def async_upload_file(self, file_path: str) -> bool:
        """Upload an existing tar archive file to remote destination."""
        if not os.path.exists(file_path):
            _LOGGER.error("DomoLink-BackUp: Le fichier spécifié n'existe pas : %s", file_path)
            return False

        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        dest_label = self.storage_engine.destination_label
        size_mb = round(file_size / (1024 * 1024), 2)
        start_time = time.monotonic()

        self.set_status(STATE_UPLOADING, f"Téléversement de {filename}...", is_busy=True)
        self.update_progress(
            STAGE_UPLOADING,
            50,
            "Téléversement de fichier",
            f"Envoi de {filename} ({size_mb} Mo)...",
            log_msg=f"Téléversement direct de l'archive {filename} ({size_mb} Mo)...",
            current_file=filename,
            total_bytes=file_size,
        )
        await self.notifier.async_notify_start(filename, dest_label)

        def _on_direct_upload_progress(bytes_sent: int) -> None:
            pct = 50 + int((bytes_sent / file_size) * 45) if file_size > 0 else 50
            sent_mb = round(bytes_sent / (1024 * 1024), 1)
            self.update_progress(
                STAGE_UPLOADING,
                pct,
                "Téléversement distant",
                f"{sent_mb} Mo / {size_mb} Mo ({pct}%)",
                current_file=filename,
                transferred_bytes=bytes_sent,
                total_bytes=file_size,
            )

        success = await self.storage_engine.async_upload(
            source=file_path,
            filename=filename,
            size=file_size,
            on_progress=_on_direct_upload_progress,
        )

        elapsed = time.monotonic() - start_time
        duration_formatted = (
            f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
            if elapsed >= 60
            else f"{round(elapsed, 1)}s"
        )
        if success:
            self.data["last_backup_name"] = filename
            self.data["last_backup_date"] = datetime.now(timezone.utc).isoformat()
            self.data["last_backup_size_mb"] = size_mb
            self.data["last_upload_duration_sec"] = round(elapsed, 1)

            await self.async_refresh_backups_list()
            self.set_status(STATE_SUCCESS, "Téléversement réussi", is_busy=False)

            report = {
                "backup_name": filename,
                "filename": filename,
                "source_path": file_path,
                "destination": dest_label,
                "size_bytes": file_size,
                "size_mb": size_mb,
                "duration_sec": round(elapsed, 1),
                "duration_formatted": duration_formatted,
                "compression_ratio": "Archive importée",
                "components": ["Fichier archive existant"],
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "success",
            }
            self.update_progress(
                STAGE_COMPLETED,
                100,
                "Téléversement réussi",
                f"Archive {filename} envoyée sur {dest_label}",
                log_msg=f"✓ Téléversement de {filename} réussi en {duration_formatted} !",
                level="success",
                report=report,
            )

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
            self.update_progress(
                STAGE_FAILED,
                100,
                "Échec du téléversement",
                f"Impossible de téléverser {filename}.",
                log_msg=f"✗ Échec du téléversement de {filename}.",
                level="error",
            )
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

    # Store entry data BEFORE notifying listeners
    hass.data[DOMAIN][entry.entry_id] = entry_data

    if HAS_BACKUP_AGENT:
        notify_backup_agents_updated(hass)

    # ─── Enregistrement du Panneau Frontend dans la Barre Latérale ───
    frontend_dir = hass.config.path("custom_components/domolink_backup/frontend")
    if os.path.exists(frontend_dir):
        if hasattr(hass.http, "async_register_static_paths") and StaticPathConfig is not None:
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

    schema_create = vol.Schema({
        vol.Optional("name"): str,
        vol.Optional("include_database", default=True): bool,
    })
    schema_upload = vol.Schema({
        vol.Required("file_path"): str,
    })

    hass.services.async_register(DOMAIN, SERVICE_CREATE_BACKUP, _handle_create_backup, schema=schema_create)
    hass.services.async_register(DOMAIN, SERVICE_UPLOAD_BACKUP, _handle_upload_backup, schema=schema_upload)
    hass.services.async_register(DOMAIN, SERVICE_TEST_CONNECTION, _handle_test_connection)
    hass.services.async_register(DOMAIN, SERVICE_CLEAN_OLD_BACKUPS, _handle_clean_old_backups)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_BACKUPS, _handle_sync_backups)

    # ─── Enregistrement des commandes WebSocket pour le Dashboard UI ───
    _register_websocket_commands(hass)

    # Forward setup to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options changes
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


def _get_active_coordinator(hass: HomeAssistant) -> DomoLinkBackupCoordinator | None:
    """Retrieve the primary active DomoLinkBackupCoordinator instance."""
    domain_data = hass.data.get(DOMAIN, {})
    for entry_id, data in domain_data.items():
        if isinstance(data, dict) and "coordinator" in data:
            return data["coordinator"]
    return None


def _register_websocket_commands(hass: HomeAssistant) -> None:
    """Register WebSocket API handlers for the frontend dashboard panel."""

    @websocket_command({vol.Required("type"): "domolink_backup/get_data"})
    @callback
    def ws_get_data(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur DomoLink-BackUp non initialisé")
            return

        cfg = {**coordinator.entry.data, **coordinator.entry.options}
        # Mask sensitive passwords before sending to frontend
        safe_cfg = dict(cfg)
        for secret_key in (CONF_FTP_PASS, CONF_WEBDAV_PASS, CONF_TELEGRAM_TOKEN):
            if safe_cfg.get(secret_key):
                safe_cfg[secret_key] = "********"

        preview_title, _ = resolve_backup_name_template(
            coordinator.data.get("backup_name_template", DEFAULT_BACKUP_NAME_TEMPLATE),
            mode="MANUEL",
        )
        coordinator.data["resolved_template_name"] = preview_title

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
        vol.Optional("mode", default="MANUEL"): str,
    })
    @async_response
    async def ws_trigger_backup(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        name = msg.get("name")
        include_db = msg.get("include_database", True)
        mode = msg.get("mode", "MANUEL")
        hass.async_create_task(
            coordinator.async_create_and_upload_backup(name, include_db, mode=mode),
            name=f"{DOMAIN}_ws_trigger_backup",
        )
        connection.send_result(msg["id"], {"status": "started"})

    @websocket_command({
        vol.Required("type"): "domolink_backup/save_template",
        vol.Required("template"): str,
    })
    @async_response
    async def ws_save_template(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        new_template = msg["template"].strip() or DEFAULT_BACKUP_NAME_TEMPLATE
        options = dict(coordinator.entry.options or coordinator.entry.data)
        options[CONF_BACKUP_NAME_TEMPLATE] = new_template
        hass.config_entries.async_update_entry(coordinator.entry, options=options)
        coordinator.data["backup_name_template"] = new_template
        preview_title, preview_fname = resolve_backup_name_template(new_template, mode="MANUEL")
        coordinator.data["resolved_template_name"] = preview_title
        coordinator.async_set_updated_data(coordinator.data)
        connection.send_result(
            msg["id"],
            {
                "success": True,
                "template": new_template,
                "preview_title": preview_title,
                "preview_filename": preview_fname,
            },
        )

    @websocket_command({
        vol.Required("type"): "domolink_backup/get_template_preview",
        vol.Optional("template"): str,
        vol.Optional("mode", default="MANUEL"): str,
    })
    @callback
    def ws_get_template_preview(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        template = msg.get("template")
        if not template and coordinator:
            template = coordinator.data.get("backup_name_template")
        mode = msg.get("mode", "MANUEL")
        title, fname = resolve_backup_name_template(template, mode=mode)
        connection.send_result(msg["id"], {"title": title, "safe_filename": fname})

    @websocket_command({vol.Required("type"): "domolink_backup/test_connection"})
    @async_response
    async def ws_test_connection(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        res = await coordinator.async_run_test_connection()
        connection.send_result(msg["id"], res)

    @websocket_command({vol.Required("type"): "domolink_backup/clean_backups"})
    @async_response
    async def ws_clean_backups(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        res = await coordinator.async_apply_retention()
        connection.send_result(msg["id"], res)

    @websocket_command({
        vol.Required("type"): "domolink_backup/delete_backup",
        vol.Required("backup_id"): str,
    })
    @async_response
    async def ws_delete_backup(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        backup_id = msg["backup_id"]
        success = await coordinator.async_delete_backup(backup_id)
        connection.send_result(msg["id"], {"success": success})

    try:
        async_register_command(hass, ws_get_data)
        async_register_command(hass, ws_trigger_backup)
        async_register_command(hass, ws_save_template)
        async_register_command(hass, ws_get_template_preview)
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
            # Remove all domain services
            for svc in (
                SERVICE_CREATE_BACKUP,
                SERVICE_UPLOAD_BACKUP,
                SERVICE_TEST_CONNECTION,
                SERVICE_CLEAN_OLD_BACKUPS,
                SERVICE_SYNC_BACKUPS,
            ):
                try:
                    hass.services.async_remove(DOMAIN, svc)
                except Exception:
                    pass

            try:
                frontend.async_remove_panel(hass, "domolink_backup")
            except Exception:
                pass

    return unload_ok
