"""Notification Engine for DomoLink-BachUp.

Handles Telegram alerts and Home Assistant persistent notifications.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_TELEGRAM_CHAT_ID,
    CONF_TELEGRAM_ENABLED,
    CONF_TELEGRAM_NOTIFY_ON_ERROR,
    CONF_TELEGRAM_NOTIFY_ON_START,
    CONF_TELEGRAM_NOTIFY_ON_SUCCESS,
    CONF_TELEGRAM_TOKEN,
    DOMAIN,
    NAME,
)

_LOGGER = logging.getLogger(__name__)


class DomoLinkNotifier:
    """Manages notifications for backup lifecycle events."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        """Initialize notifier with integration configuration."""
        self.hass = hass
        self.config = dict(config)

    def update_config(self, new_config: dict[str, Any]) -> None:
        """Update active configuration."""
        self.config.update(new_config)

    @property
    def is_telegram_enabled(self) -> bool:
        """Return True if Telegram is configured and enabled."""
        return (
            bool(self.config.get(CONF_TELEGRAM_ENABLED, False))
            and bool(self.config.get(CONF_TELEGRAM_TOKEN))
            and bool(self.config.get(CONF_TELEGRAM_CHAT_ID))
        )

    async def async_notify_start(self, backup_name: str, destination: str) -> None:
        """Notify that a backup process has begun."""
        if not self.config.get(CONF_TELEGRAM_NOTIFY_ON_START, False):
            return

        msg = (
            f"💾 <b>{NAME}</b>\n\n"
            f"🚀 <b>Démarrage de la sauvegarde</b>\n"
            f"• <b>Nom</b> : <code>{backup_name}</code>\n"
            f"• <b>Destination</b> : {destination}\n"
            f"• <b>Statut</b> : Envoi vers le stockage distant..."
        )
        await self._send_telegram(msg)

    async def async_notify_success(
        self,
        backup_name: str,
        destination: str,
        size_bytes: int,
        duration_sec: float,
        remaining_count: int = 0,
        total_size_mb: float = 0.0,
    ) -> None:
        """Notify on successful backup upload."""
        size_mb = round(size_bytes / (1024 * 1024), 2)
        size_str = f"{size_mb} Mo" if size_mb < 1024 else f"{round(size_mb / 1024, 2)} Go"

        self.hass.bus.async_fire(
            f"{DOMAIN}_event",
            {
                "event": "backup_success",
                "backup_name": backup_name,
                "destination": destination,
                "size_bytes": size_bytes,
                "duration_sec": duration_sec,
            },
        )

        if not self.config.get(CONF_TELEGRAM_NOTIFY_ON_SUCCESS, True):
            return

        msg = (
            f"💾 <b>{NAME}</b>\n\n"
            f"✅ <b>Sauvegarde réussie et sécurisée !</b>\n"
            f"• <b>Fichier</b> : <code>{backup_name}</code>\n"
            f"• <b>Taille</b> : {size_str}\n"
            f"• <b>Durée de transfert</b> : {round(duration_sec, 1)} s\n"
            f"• <b>Cible</b> : {destination}\n"
            f"• <b>Archives distantes</b> : {remaining_count} sauvegarde(s) ({total_size_mb} Mo occupés)\n\n"
            f"🛡️ <i>Vos données Home Assistant sont protégées hors site.</i>"
        )
        await self._send_telegram(msg)

    async def async_notify_error(
        self,
        backup_name: str,
        destination: str,
        error_message: str,
        error_code: int | str | None = None,
    ) -> None:
        """Notify on backup failure."""
        code_str = f" [Code {error_code}]" if error_code else ""

        # Create persistent notification in Home Assistant UI
        persistent_notification.async_create(
            self.hass,
            f"Échec de l'envoi de la sauvegarde vers {destination}{code_str} : {error_message}",
            title=f"⚠️ {NAME} — Échec de sauvegarde",
            notification_id="domolink_backup_error",
        )

        self.hass.bus.async_fire(
            f"{DOMAIN}_event",
            {
                "event": "backup_error",
                "backup_name": backup_name,
                "destination": destination,
                "error": error_message,
                "code": error_code,
            },
        )

        if not self.config.get(CONF_TELEGRAM_NOTIFY_ON_ERROR, True):
            return

        msg = (
            f"💾 <b>{NAME}</b>\n\n"
            f"⚠️ <b>Échec du transfert de la sauvegarde !</b>\n"
            f"• <b>Fichier</b> : <code>{backup_name}</code>\n"
            f"• <b>Destination</b> : {destination}\n"
            f"• <b>Erreur</b> : {error_message}{code_str}\n\n"
            f"🔧 <i>Vérifiez vos paramètres réseau ou vos identifiants dans le panneau DomoLink-BachUp.</i>"
        )
        await self._send_telegram(msg)

    async def _send_telegram(self, html_message: str) -> bool:
        """Send HTML message via Telegram Bot API."""
        if not self.is_telegram_enabled:
            return False

        token = self.config.get(CONF_TELEGRAM_TOKEN, "").strip()
        chat_id = self.config.get(CONF_TELEGRAM_CHAT_ID, "").strip()
        session = async_get_clientsession(self.hass)

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": html_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                else:
                    _LOGGER.warning("DomoLink-BachUp: Échec envoi Telegram (HTTP %s)", resp.status)
                    return False
        except Exception as err:
            _LOGGER.warning("DomoLink-BachUp: Erreur lors de l'envoi Telegram : %s", err)
            return False
