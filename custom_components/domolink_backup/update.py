"""Update platform for DomoLink-BackUp integration."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components import frontend
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, NAME, VERSION

_LOGGER = logging.getLogger(__name__)

GITHUB_REPO = "SocrateMobile/Domolink-BackUp"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
UPDATE_CHECK_INTERVAL = timedelta(hours=4)


def parse_semver(version_str: str) -> tuple[int, ...]:
    """Parse semver string into a comparable tuple of integers."""
    if not version_str:
        return (0, 0, 0)
    clean = re.sub(r"^[vV]", "", version_str.strip())
    parts: list[int] = []
    for segment in clean.split("."):
        digits = re.match(r"^\d+", segment)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def get_installed_version() -> str:
    """Read version directly from manifest.json with fallback to const.VERSION."""
    manifest_path = os.path.join(os.path.dirname(__file__), "manifest.json")
    try:
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get("version", VERSION))
    except Exception as err:
        _LOGGER.warning("Could not read manifest.json version: %s", err)
    return VERSION


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the update platform for DomoLink-BackUp."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data.get("coordinator")
    installed_ver = get_installed_version()

    update_entity = DomolinkBackupUpdateEntity(
        hass=hass,
        entry=entry,
        installed_version=installed_ver,
        coordinator=coordinator,
    )

    data["update_entity"] = update_entity
    async_add_entities([update_entity], True)


class DomolinkBackupUpdateEntity(UpdateEntity):
    """Representation of the DomoLink-BackUp update entity."""

    _attr_has_entity_name = True
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
        | UpdateEntityFeature.PROGRESS
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        installed_version: str,
        coordinator=None,
    ) -> None:
        """Initialize the update entity."""
        self.hass = hass
        self.entry = entry
        self._coordinator = coordinator
        self._attr_name = "Mise à jour"
        self._attr_unique_id = f"domolink_backup_update_{entry.entry_id}"
        self._attr_title = "DomoLink-BackUp"

        self._attr_installed_version = installed_version
        self._attr_latest_version = installed_version
        self._attr_release_summary: str | None = None
        self._attr_release_url: str | None = None
        self._release_body: str | None = None
        self._zip_download_url: str | None = None

        self._attr_in_progress = False
        self._attr_update_percentage: int | None = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"{NAME} ({entry.title})",
            manufacturer="SocrateMobile",
            model="DomoLink Storage Gateway",
            sw_version=installed_version,
        )

        self._unsub_interval = None

    async def async_added_to_hass(self) -> None:
        """Register periodic update checks and initial check."""
        await super().async_added_to_hass()
        self._unsub_interval = async_track_time_interval(
            self.hass, self._async_periodic_check, UPDATE_CHECK_INTERVAL
        )
        # Perform initial check in background
        if hasattr(self.hass, "async_create_background_task"):
            self.hass.async_create_background_task(
                self.async_update(), name=f"{DOMAIN}_initial_update_check"
            )
        else:
            self.hass.async_create_task(self.async_update())

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        await super().async_will_remove_from_hass()

    async def _async_periodic_check(self, _now: Any = None) -> None:
        """Periodic check called by timer."""
        await self.async_update()

    async def async_update(self) -> None:
        """Check GitHub for the latest release."""
        try:
            session = async_get_clientsession(self.hass)
            headers = {
                "User-Agent": "DomoLink-BackUp-HA",
                "Accept": "application/vnd.github.v3+json",
            }
            async with session.get(
                GITHUB_LATEST_RELEASE_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "GitHub release check returned HTTP %s for %s",
                        resp.status,
                        GITHUB_REPO,
                    )
                    return

                data = await resp.json()
                tag = data.get("tag_name", "").strip()
                clean_tag = re.sub(r"^[vV]", "", tag)
                if not clean_tag:
                    return

                self._attr_latest_version = clean_tag
                self._attr_release_summary = data.get("name") or f"Version {clean_tag}"
                self._release_body = data.get("body") or ""
                self._attr_release_url = data.get("html_url")
                self._zip_download_url = (
                    f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag}.zip"
                )

                has_update = parse_semver(clean_tag) > parse_semver(self._attr_installed_version)

                # Update left sidebar panel badge & icon
                self._update_sidebar_panel(has_update)

                # Sync update state to coordinator data for instant frontend propagation
                if self._coordinator:
                    self._coordinator.data.update(
                        {
                            "update_available": has_update,
                            "latest_version": clean_tag,
                            "release_notes": self._release_body,
                            "release_url": self._attr_release_url,
                        }
                    )
                    self._coordinator.async_set_updated_data(self._coordinator.data)

                self.async_write_ha_state()
                _LOGGER.info(
                    "DomoLink-BackUp update check: installed=%s, latest=%s, update_available=%s",
                    self._attr_installed_version,
                    clean_tag,
                    has_update,
                )
        except asyncio.TimeoutError:
            _LOGGER.debug("Timeout checking GitHub releases for DomoLink-BackUp")
        except Exception as err:
            _LOGGER.warning("Error checking GitHub releases for DomoLink-BackUp: %s", err)

    def _update_sidebar_panel(self, update_available: bool) -> None:
        """Update the Home Assistant left sidebar panel badge/title."""
        try:
            title = f"{NAME} 🔴" if update_available else NAME
            icon = "mdi:shield-alert" if update_available else "mdi:archive-arrow-up"
            frontend.async_register_built_in_panel(
                self.hass,
                component_name="custom",
                sidebar_title=title,
                sidebar_icon=icon,
                frontend_url_path="domolink_backup",
                config={
                    "_panel_custom": {
                        "name": "domolink-backup-panel",
                        "module_url": f"/domolink_backup_frontend/domolink-backup-panel.js?v={self._attr_installed_version}",
                    }
                },
                require_admin=False,
                update=True,
            )
        except Exception as err:
            _LOGGER.debug("Could not update sidebar panel registration: %s", err)

    async def async_release_notes(self) -> str | None:
        """Return release notes in markdown."""
        return self._release_body

    async def async_install(
        self, version: str | None = None, backup: bool = True, **kwargs: Any
    ) -> None:
        """Download and install update, then restart Home Assistant."""
        if not self._zip_download_url:
            raise ValueError("No download URL available for DomoLink-BackUp update")

        _LOGGER.info(
            "Starting DomoLink-BackUp update to version %s (download: %s)",
            self._attr_latest_version,
            self._zip_download_url,
        )

        self._attr_in_progress = True
        self._attr_update_percentage = 10
        self.async_write_ha_state()

        def _do_download_and_extract() -> None:
            """Synchronous blocking filesystem work executed in executor."""
            import urllib.request
            import ssl

            ctx = ssl.create_default_context()
            try:
                import certifi
                ctx.load_verify_locations(certifi.where())
            except Exception:
                try:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                except Exception:
                    pass

            temp_dir = tempfile.mkdtemp(prefix="domolink_backup_update_")
            zip_path = os.path.join(temp_dir, "release.zip")
            try:
                # 1. Download zip
                req = urllib.request.Request(
                    self._zip_download_url,
                    headers={"User-Agent": "DomoLink-BackUp-HA"},
                )
                with urllib.request.urlopen(req, context=ctx, timeout=45) as resp, open(
                    zip_path, "wb"
                ) as out_file:
                    shutil.copyfileobj(resp, out_file)

                # 2. Extract zip
                extract_path = os.path.join(temp_dir, "extracted")
                os.makedirs(extract_path, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(extract_path)

                # 3. Locate custom_components/domolink_backup inside extracted content
                source_component_dir = None
                for root, _dirs, _files in os.walk(extract_path):
                    if (
                        os.path.basename(root) == "domolink_backup"
                        and os.path.basename(os.path.dirname(root)) == "custom_components"
                    ):
                        source_component_dir = root
                        break

                if not source_component_dir or not os.path.exists(
                    os.path.join(source_component_dir, "__init__.py")
                ):
                    raise RuntimeError(
                        "Archive does not contain a valid custom_components/domolink_backup structure"
                    )

                # 4. Target component directory
                target_dir = os.path.abspath(os.path.dirname(__file__))

                # 5. Optional safety backup of current directory
                if backup:
                    backup_dir = os.path.join(tempfile.gettempdir(), "domolink_backup_last_backup")
                    if os.path.exists(backup_dir):
                        shutil.rmtree(backup_dir, ignore_errors=True)
                    shutil.copytree(target_dir, backup_dir, dirs_exist_ok=True)
                    _LOGGER.info("DomoLink-BackUp safety backup saved to %s", backup_dir)

                # 6. Overwrite target directory with newly extracted files
                shutil.copytree(source_component_dir, target_dir, dirs_exist_ok=True)
                _LOGGER.info("DomoLink-BackUp files successfully updated in %s", target_dir)

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        try:
            self._attr_update_percentage = 30
            self.async_write_ha_state()

            await self.hass.async_add_executor_job(_do_download_and_extract)

            self._attr_update_percentage = 90
            self.async_write_ha_state()

            # Reset sidebar panel title to default
            self._update_sidebar_panel(False)

            self._attr_update_percentage = 100
            self._attr_installed_version = self._attr_latest_version
            self._attr_in_progress = False
            self.async_write_ha_state()

            _LOGGER.info("Update complete! Requesting Home Assistant restart...")
            await asyncio.sleep(1)

            # Trigger Home Assistant restart
            await self.hass.services.async_call("homeassistant", "restart")

        except Exception as err:
            self._attr_in_progress = False
            self._attr_update_percentage = None
            self.async_write_ha_state()
            _LOGGER.error("DomoLink-BackUp auto-update failed: %s", err, exc_info=True)
            raise
