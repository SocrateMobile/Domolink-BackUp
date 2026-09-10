"""Sensors for the DomoLink-BackUp integration."""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    NAME,
    STATE_IDLE,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DomoLink-BackUp sensor platform from config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    entities: list[SensorEntity] = [
        DomoLinkBackupStatusSensor(coordinator, entry),
        DomoLinkBackupLastBackupSensor(coordinator, entry),
        DomoLinkBackupCountSensor(coordinator, entry),
        DomoLinkBackupStorageUsedSensor(coordinator, entry),
        DomoLinkBackupDestinationSensor(coordinator, entry),
        DomoLinkBackupConnectionSensor(coordinator, entry),
    ]

    async_add_entities(entities)


class DomoLinkBaseSensor(CoordinatorEntity, SensorEntity):
    """Base sensor for DomoLink-BackUp."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize base sensor."""
        super().__init__(coordinator)
        self.entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{NAME} ({entry.title})",
            manufacturer="SocrateMobile",
            model="DomoLink Storage Gateway",
            sw_version=VERSION,
        )


class DomoLinkBackupStatusSensor(DomoLinkBaseSensor):
    """Sensor showing backup activity status."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Statut"
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_icon = "mdi:archive-check"

    @property
    def native_value(self) -> str:
        """Return the current activity status."""
        return self.coordinator.data.get("status", STATE_IDLE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "destination": self.coordinator.data.get("destination_label", ""),
            "last_action": self.coordinator.data.get("last_action", ""),
            "last_error": self.coordinator.data.get("last_error", ""),
            "is_busy": self.coordinator.data.get("is_busy", False),
        }


class DomoLinkBackupLastBackupSensor(DomoLinkBaseSensor):
    """Sensor displaying timestamp of the last backup."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Dernière sauvegarde"
        self._attr_unique_id = f"{entry.entry_id}_last_backup"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:backup-restore"

    @property
    def native_value(self) -> datetime | None:
        """Return timestamp of the last successful backup."""
        last_dt_str = self.coordinator.data.get("last_backup_date")
        if not last_dt_str:
            return None
        try:
            return datetime.fromisoformat(last_dt_str)
        except Exception:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "backup_name": self.coordinator.data.get("last_backup_name", ""),
            "backup_size_mb": self.coordinator.data.get("last_backup_size_mb", 0.0),
            "upload_duration_sec": self.coordinator.data.get("last_upload_duration_sec", 0.0),
        }


class DomoLinkBackupCountSensor(DomoLinkBaseSensor):
    """Sensor for number of remote backups."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Nombre de sauvegardes"
        self._attr_unique_id = f"{entry.entry_id}_total_backups"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_icon = "mdi:folder-zip-outline"

    @property
    def native_value(self) -> int:
        """Return total count of remote backups."""
        return int(self.coordinator.data.get("total_backups_count", 0))


class DomoLinkBackupStorageUsedSensor(DomoLinkBaseSensor):
    """Sensor for storage space used on remote target."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Espace utilisé distant"
        self._attr_unique_id = f"{entry.entry_id}_storage_used"
        self._attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
        self._attr_device_class = SensorDeviceClass.DATA_SIZE
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:database"

    @property
    def native_value(self) -> float:
        """Return total MB used on remote storage."""
        return float(self.coordinator.data.get("total_storage_mb", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        total_mb = float(self.coordinator.data.get("total_storage_mb", 0.0))
        max_mb = float(self.coordinator.data.get("max_storage_mb", 10240))
        pct = round((total_mb / max_mb) * 100, 1) if max_mb > 0 else 0.0
        return {
            "quota_max_mb": max_mb,
            "quota_percent": pct,
        }


class DomoLinkBackupDestinationSensor(DomoLinkBaseSensor):
    """Sensor showing active destination profile."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Destination de stockage"
        self._attr_unique_id = f"{entry.entry_id}_destination"
        self._attr_icon = "mdi:server-network"

    @property
    def native_value(self) -> str:
        """Return label of active destination."""
        return str(self.coordinator.data.get("destination_label", "Non configuré"))


class DomoLinkBackupConnectionSensor(DomoLinkBaseSensor):
    """Sensor showing connection test health."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "État de la connexion"
        self._attr_unique_id = f"{entry.entry_id}_connection_state"
        self._attr_icon = "mdi:check-network"

    @property
    def native_value(self) -> str:
        """Return connection state badge."""
        return str(self.coordinator.data.get("connection_status", "Prêt"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "latency_sec": self.coordinator.data.get("connection_latency", 0.0),
            "last_checked": self.coordinator.data.get("connection_last_checked", ""),
            "diagnostic_message": self.coordinator.data.get("connection_message", ""),
        }
