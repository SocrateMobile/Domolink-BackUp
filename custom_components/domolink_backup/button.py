"""Buttons for the DomoLink-BackUp integration."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME, VERSION

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DomoLink-BackUp button platform from config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    entities: list[ButtonEntity] = [
        DomoLinkBackupNowButton(coordinator, entry),
        DomoLinkTestConnectionButton(coordinator, entry),
        DomoLinkCleanBackupsButton(coordinator, entry),
        DomoLinkSyncBackupsButton(coordinator, entry),
    ]

    async_add_entities(entities)


class DomoLinkBaseButton(ButtonEntity):
    """Base button for DomoLink-BackUp."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        """Initialize base button."""
        self.coordinator = coordinator
        self.entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{NAME} ({entry.title})",
            manufacturer="SocrateMobile",
            model="DomoLink Storage Gateway",
            sw_version=VERSION,
        )


class DomoLinkBackupNowButton(DomoLinkBaseButton):
    """Button to trigger an immediate backup and remote upload."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Sauvegarder maintenant"
        self._attr_unique_id = f"{entry.entry_id}_backup_now"
        self._attr_icon = "mdi:cloud-upload"

    async def async_press(self) -> None:
        """Press the button to create and upload backup."""
        _LOGGER.info("DomoLink-BackUp: Bouton 'Sauvegarder maintenant' pressé")
        self.hass.async_create_task(self.coordinator.async_create_and_upload_backup())


class DomoLinkTestConnectionButton(DomoLinkBaseButton):
    """Button to test connection to remote target."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Tester la connexion"
        self._attr_unique_id = f"{entry.entry_id}_test_connection"
        self._attr_icon = "mdi:lan-connect"

    async def async_press(self) -> None:
        """Press the button to run connection diagnostic."""
        _LOGGER.info("DomoLink-BackUp: Bouton 'Tester la connexion' pressé")
        self.hass.async_create_task(self.coordinator.async_run_test_connection())


class DomoLinkCleanBackupsButton(DomoLinkBaseButton):
    """Button to enforce retention policy immediately."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Nettoyer selon la rétention"
        self._attr_unique_id = f"{entry.entry_id}_clean_backups"
        self._attr_icon = "mdi:broom"

    async def async_press(self) -> None:
        """Press the button to purge old backups."""
        _LOGGER.info("DomoLink-BackUp: Bouton 'Nettoyer selon la rétention' pressé")
        self.hass.async_create_task(self.coordinator.async_apply_retention())


class DomoLinkSyncBackupsButton(DomoLinkBaseButton):
    """Button to refresh and synchronize list of remote backups."""

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_name = "Synchroniser les sauvegardes"
        self._attr_unique_id = f"{entry.entry_id}_sync_backups"
        self._attr_icon = "mdi:sync"

    async def async_press(self) -> None:
        """Press the button to refresh remote backups."""
        _LOGGER.info("DomoLink-BackUp: Bouton 'Synchroniser les sauvegardes' pressé")
        self.hass.async_create_task(self.coordinator.async_refresh_backups_list())
