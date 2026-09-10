"""Native Home Assistant BackupAgent platform for DomoLink-BackUp.

Registers DomoLink-BackUp directly into Home Assistant's Backup Manager
(Settings > System > Backups) as an official remote backup destination.
"""
from __future__ import annotations

import inspect
import logging
import time
from typing import Any, AsyncIterator, Callable, Coroutine

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, NAME
from .storage_engine import DomoLinkStorageEngine

_LOGGER = logging.getLogger(__name__)

# Try importing official BackupAgent classes from Home Assistant Core
try:
    from homeassistant.components.backup import (
        AgentBackup,
        BackupAgent,
        BackupAgentError,
        BackupNotFound,
    )
    HAS_BACKUP_AGENT = True
except ImportError:
    HAS_BACKUP_AGENT = False

    class BackupAgent:  # type: ignore[no-redef]
        """Fallback base class when BackupAgent is not present."""
        domain: str = DOMAIN
        name: str = NAME
        unique_id: str = "domolink_backup_default"

    class AgentBackup:  # type: ignore[no-redef]
        """Fallback AgentBackup data model."""
        def __init__(self, backup_id: str, name: str, date: str, size: int, **kwargs: Any) -> None:
            self.backup_id = backup_id
            self.name = name
            self.date = date
            self.size = size

    class BackupAgentError(Exception):  # type: ignore[no-redef]
        """Fallback BackupAgentError."""

    class BackupNotFound(BackupAgentError):  # type: ignore[no-redef]
        """Fallback BackupNotFound error."""


DATA_BACKUP_AGENT_LISTENERS = f"{DOMAIN}_backup_agent_listeners"


def _create_agent_backup(backup_id: str, name: str, date: str, size: int) -> AgentBackup:
    """Safely instantiate AgentBackup with proper parameters for the running HA version."""
    try:
        sig = inspect.signature(AgentBackup)
        params = sig.parameters
        kwargs: dict[str, Any] = {}
        if "backup_id" in params:
            kwargs["backup_id"] = backup_id
        if "name" in params:
            kwargs["name"] = name
        if "date" in params:
            kwargs["date"] = date
        if "size" in params:
            kwargs["size"] = size
        if "addons" in params:
            kwargs["addons"] = []
        if "database_included" in params:
            kwargs["database_included"] = True
        if "extra_metadata" in params:
            kwargs["extra_metadata"] = {}
        return AgentBackup(**kwargs)
    except Exception:
        # Fallback to direct positional or keyword instantiation
        try:
            return AgentBackup(backup_id=backup_id, name=name, date=date, size=size)
        except Exception:
            return AgentBackup(backup_id, name, date, size)  # type: ignore[call-arg]


class DomoLinkBackupAgent(BackupAgent):
    """Home Assistant Backup Agent implementation for DomoLink-BackUp."""

    domain: str = DOMAIN

    def __init__(self, hass: HomeAssistant, entry_id: str, title: str) -> None:
        """Initialize the DomoLink backup agent."""
        self.hass = hass
        self.unique_id = entry_id
        self.name = f"{NAME} ({title})"

    @property
    def _storage_engine(self) -> DomoLinkStorageEngine:
        """Get the storage engine instance from hass data."""
        data = self.hass.data.get(DOMAIN, {}).get(self.unique_id, {})
        return data.get("storage_engine")

    @property
    def _coordinator(self):
        """Get the coordinator instance from hass data."""
        data = self.hass.data.get(DOMAIN, {}).get(self.unique_id, {})
        return data.get("coordinator")

    @property
    def _notifier(self):
        """Get the notifier instance from hass data."""
        data = self.hass.data.get(DOMAIN, {}).get(self.unique_id, {})
        return data.get("notifier")

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        """Return metadata for a specific backup."""
        engine = self._storage_engine
        if not engine:
            raise BackupAgentError("Moteur de stockage DomoLink-BackUp non initialisé")

        backups = await engine.async_list_backups()
        target = next((b for b in backups if b["backup_id"] == backup_id or b["filename"] == backup_id), None)
        if not target:
            raise BackupNotFound(f"Sauvegarde introuvable sur le stockage distant : {backup_id}")

        return _create_agent_backup(
            backup_id=target["backup_id"],
            name=target["name"],
            date=target["date"],
            size=target["size"],
        )

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """Return list of backups stored in the remote destination."""
        engine = self._storage_engine
        if not engine:
            return []

        raw_backups = await engine.async_list_backups()
        result: list[AgentBackup] = []
        for b in raw_backups:
            try:
                result.append(
                    _create_agent_backup(
                        backup_id=b["backup_id"],
                        name=b["name"],
                        date=b["date"],
                        size=b["size"],
                    )
                )
            except Exception as err:
                _LOGGER.debug("DomoLink-BackUp: Impossible de convertir la sauvegarde %s : %s", b, err)
        return result

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: Callable[[int], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Upload a backup stream to the remote storage target."""
        engine = self._storage_engine
        notifier = self._notifier

        if not engine:
            raise BackupAgentError("Moteur de stockage DomoLink-BackUp non disponible")

        filename = f"{backup.backup_id}.tar"
        size = getattr(backup, "size", 0)
        start_time = time.monotonic()

        _LOGGER.info("DomoLink-BackUp: Début de l'envoi de la sauvegarde officielle '%s' (ID: %s)", backup.name, backup.backup_id)

        # Notify start if enabled
        if notifier:
            self.hass.async_create_task(
                notifier.async_notify_start(backup.name, engine.destination_label)
            )

        try:
            success = await engine.async_upload(
                source=open_stream,
                filename=filename,
                size=size,
                on_progress=on_progress,
            )

            elapsed = time.monotonic() - start_time
            if not success:
                if notifier:
                    self.hass.async_create_task(
                        notifier.async_notify_error(
                            backup_name=backup.name,
                            destination=engine.destination_label,
                            error_message="Échec de transfert vers la cible configurée",
                        )
                    )
                raise BackupAgentError(f"Échec du téléversement de la sauvegarde vers {engine.destination_label}")

            # Notify success
            stats = await engine.async_list_backups()
            total_mb = round(sum(b.get("size", 0) for b in stats) / (1024 * 1024), 2)
            if notifier:
                self.hass.async_create_task(
                    notifier.async_notify_success(
                        backup_name=backup.name,
                        destination=engine.destination_label,
                        size_bytes=size,
                        duration_sec=elapsed,
                        remaining_count=len(stats),
                        total_size_mb=total_mb,
                    )
                )

        except BackupAgentError:
            # Already notified
            raise
        except Exception as err:
            _LOGGER.exception("DomoLink-BackUp: Exception pendant async_upload_backup: %s", err)
            if notifier:
                self.hass.async_create_task(
                    notifier.async_notify_error(
                        backup_name=backup.name,
                        destination=engine.destination_label,
                        error_message=str(err),
                    )
                )
            raise BackupAgentError(f"Erreur d'envoi DomoLink-BackUp : {err}") from err
        finally:
            if self._coordinator:
                self.hass.async_create_task(self._coordinator.async_refresh_backups_list())

    async def async_download_backup(self, backup_id: str, **kwargs: Any) -> AsyncIterator[bytes]:
        """Download a backup file as an asynchronous stream."""
        engine = self._storage_engine
        if not engine:
            raise BackupAgentError("Moteur de stockage non initialisé")

        try:
            return await engine.async_download_backup(backup_id)
        except FileNotFoundError as fnf:
            raise BackupNotFound(str(fnf)) from fnf
        except Exception as err:
            raise BackupAgentError(f"Erreur de téléchargement : {err}") from err

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Delete a backup archive from remote storage."""
        engine = self._storage_engine
        if not engine:
            raise BackupAgentError("Moteur de stockage non initialisé")

        try:
            success = await engine.async_delete_backup(backup_id)
        except FileNotFoundError as fnf:
            raise BackupNotFound(str(fnf)) from fnf
        except Exception as err:
            raise BackupAgentError(f"Erreur suppression : {err}") from err

        if not success:
            raise BackupAgentError(f"Impossible de supprimer la sauvegarde distante : {backup_id}")

        if self._coordinator:
            self.hass.async_create_task(self._coordinator.async_refresh_backups_list())


async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    """Return all active DomoLink-BackUp backup agents."""
    agents: list[BackupAgent] = []
    domain_data = hass.data.get(DOMAIN, {})
    for entry_id, entry_data in domain_data.items():
        if isinstance(entry_data, dict) and "backup_agent" in entry_data:
            agents.append(entry_data["backup_agent"])
    return agents


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener called when backup agents are added or removed."""
    listeners = hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, [])
    listeners.append(listener)

    @callback
    def remove_listener() -> None:
        if listener in listeners:
            listeners.remove(listener)

    return remove_listener


@callback
def notify_backup_agents_updated(hass: HomeAssistant) -> None:
    """Notify listeners that backup agents have been updated."""
    for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
        try:
            listener()
        except Exception as err:
            _LOGGER.warning("DomoLink-BackUp: Erreur lors de l'appel du listener de backup agent : %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Setup backup platform."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Unload backup platform."""
    return True
