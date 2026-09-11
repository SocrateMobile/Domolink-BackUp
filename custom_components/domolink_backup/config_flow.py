"""Config Flow and Options Flow for DomoLink-BackUp."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

try:
    from homeassistant.config_entries import ConfigFlowResult
except ImportError:
    ConfigFlowResult = Any  # type: ignore[misc,assignment]

from .const import (
    CONF_AUTO_CLEAN_ENABLED,
    CONF_BACKUP_NAME_TEMPLATE,
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
    DEFAULT_AUTO_CLEAN_ENABLED,
    DEFAULT_BACKUP_NAME_TEMPLATE,
    DEFAULT_FTP_PATH,
    DEFAULT_FTP_PORT,
    DEFAULT_LOCAL_SHARE_PATH,
    DEFAULT_MAX_BACKUPS_COUNT,
    DEFAULT_MAX_STORAGE_MB,
    DEFAULT_NAS_CONFIGS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_WEBDAV_PATH,
    DOMAIN,
    NAME,
    NAS_GENERIC,
    PROTO_FTP,
    PROTO_FTPS,
    PROTO_GOOGLE_DRIVE,
    PROTO_LOCAL_SHARE,
    PROTO_WEBDAV,
)
from .storage_engine import DomoLinkStorageEngine

_LOGGER = logging.getLogger(__name__)


class DomoLinkBackupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for DomoLink-BackUp."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        self.data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: Choose destination type and protocol."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_credentials()

        nas_options = [
            selector.SelectOptionDict(value="synology", label="Synology (DSM)"),
            selector.SelectOptionDict(value="qnap", label="QNAP (QTS)"),
            selector.SelectOptionDict(value="asustor", label="ASUSTOR (ADM)"),
            selector.SelectOptionDict(value="truenas", label="TrueNAS (SCALE/CORE)"),
            selector.SelectOptionDict(value="freebox", label="Freebox (Delta / Ultra / Pop)"),
            selector.SelectOptionDict(value="unraid", label="Unraid"),
            selector.SelectOptionDict(value="generic", label="Autre NAS / Serveur personnalisé"),
        ]

        proto_options = [
            selector.SelectOptionDict(value=PROTO_FTP, label="FTP (Standard port 21)"),
            selector.SelectOptionDict(value=PROTO_FTPS, label="FTPS (Sécurisé avec TLS/SSL)"),
            selector.SelectOptionDict(value=PROTO_WEBDAV, label="WebDAV (HTTP / HTTPS)"),
            selector.SelectOptionDict(value=PROTO_GOOGLE_DRIVE, label="Google Drive (Webhook)"),
            selector.SelectOptionDict(value=PROTO_LOCAL_SHARE, label="Partage Réseau Local (Dossier partagé)"),
        ]

        schema = vol.Schema(
            {
                vol.Required("name", default=NAME): str,
                vol.Required(CONF_NAS_TYPE, default="synology"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=nas_options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_PROTOCOL, default=PROTO_FTP): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=proto_options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: Enter credentials and connection parameters with NAS presets."""
        errors: dict[str, str] = {}

        nas_type = self.data.get(CONF_NAS_TYPE, NAS_GENERIC)
        proto = self.data.get(CONF_PROTOCOL, PROTO_FTP)
        preset = DEFAULT_NAS_CONFIGS.get(nas_type, {})

        if user_input is not None:
            self.data.update(user_input)

            # Perform a test connection check
            test_engine = DomoLinkStorageEngine(self.hass, self.data)
            test_res = await test_engine.async_test_connection()
            if not test_res.get("success"):
                _LOGGER.warning("DomoLink-BackUp: Échec connexion lors du setup: %s", test_res.get("message"))
                err_code = test_res.get("code", 500)
                if err_code in (401, 403, 530):
                    errors["base"] = "invalid_auth"
                elif err_code in (110, 111, 113):
                    errors["base"] = "cannot_connect"
                else:
                    errors["base"] = "cannot_connect"
            else:
                return await self.async_step_retention()

        # Build dynamic schema depending on protocol
        schema_dict: dict[Any, Any] = {}

        if proto in (PROTO_FTP, PROTO_FTPS):
            default_port = preset.get("ftp_port", DEFAULT_FTP_PORT)
            default_path = preset.get("ftp_path", DEFAULT_FTP_PATH)
            default_host = preset.get("ftp_host", "")
            default_user = preset.get("ftp_user", "")

            schema_dict = {
                vol.Required(CONF_FTP_HOST, default=self.data.get(CONF_FTP_HOST, default_host)): str,
                vol.Required(CONF_FTP_PORT, default=int(self.data.get(CONF_FTP_PORT, default_port))): int,
                vol.Required(CONF_FTP_USER, default=self.data.get(CONF_FTP_USER, default_user)): str,
                vol.Required(CONF_FTP_PASS, default=self.data.get(CONF_FTP_PASS, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_FTP_PATH, default=self.data.get(CONF_FTP_PATH, default_path)): str,
                vol.Optional(CONF_FTP_TLS, default=bool(self.data.get(CONF_FTP_TLS, proto == PROTO_FTPS))): bool,
            }

        elif proto == PROTO_WEBDAV:
            default_port = preset.get("webdav_port", 5006 if nas_type == "synology" else 80)
            default_path = preset.get("webdav_path", DEFAULT_WEBDAV_PATH)
            ssl_scheme = "https" if preset.get("webdav_ssl", True) else "http"
            default_url = f"{ssl_scheme}://192.168.1.100:{default_port}" if nas_type != "freebox" else "http://mafreebox.freebox.fr"

            schema_dict = {
                vol.Required(CONF_WEBDAV_URL, default=self.data.get(CONF_WEBDAV_URL, default_url)): str,
                vol.Required(CONF_WEBDAV_USER, default=self.data.get(CONF_WEBDAV_USER, "")): str,
                vol.Required(CONF_WEBDAV_PASS, default=self.data.get(CONF_WEBDAV_PASS, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_WEBDAV_PATH, default=self.data.get(CONF_WEBDAV_PATH, default_path)): str,
                vol.Optional(CONF_WEBDAV_VERIFY_SSL, default=bool(self.data.get(CONF_WEBDAV_VERIFY_SSL, False))): bool,
            }

        elif proto == PROTO_GOOGLE_DRIVE:
            schema_dict = {
                vol.Required(CONF_GOOGLE_DRIVE_WEBHOOK_URL, default=self.data.get(CONF_GOOGLE_DRIVE_WEBHOOK_URL, "")): str,
                vol.Optional(CONF_GOOGLE_DRIVE_FOLDER_ID, default=self.data.get(CONF_GOOGLE_DRIVE_FOLDER_ID, "")): str,
            }

        elif proto == PROTO_LOCAL_SHARE:
            schema_dict = {
                vol.Required(CONF_LOCAL_SHARE_PATH, default=self.data.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)): str,
            }

        return self.async_show_form(step_id="credentials", data_schema=vol.Schema(schema_dict), errors=errors)

    async def async_step_retention(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 3: Configure backup retention rules (FIFO)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            return await self.async_step_telegram()

        schema = vol.Schema(
            {
                vol.Required(CONF_MAX_BACKUPS_COUNT, default=DEFAULT_MAX_BACKUPS_COUNT): int,
                vol.Required(CONF_RETENTION_DAYS, default=DEFAULT_RETENTION_DAYS): int,
                vol.Required(CONF_MAX_STORAGE_MB, default=DEFAULT_MAX_STORAGE_MB): int,
                vol.Optional(CONF_AUTO_CLEAN_ENABLED, default=True): bool,
                vol.Optional(CONF_BACKUP_NAME_TEMPLATE, default=DEFAULT_BACKUP_NAME_TEMPLATE): str,
            }
        )

        return self.async_show_form(step_id="retention", data_schema=schema, errors=errors)

    async def async_step_telegram(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 4: Optional Telegram alerts configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self.data.update(user_input)
            title = self.data.get("name", NAME)
            return self.async_create_entry(title=title, data=self.data)

        schema = vol.Schema(
            {
                vol.Optional(CONF_TELEGRAM_ENABLED, default=False): bool,
                vol.Optional(CONF_TELEGRAM_TOKEN, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_TELEGRAM_CHAT_ID, default=""): str,
                vol.Optional(CONF_TELEGRAM_NOTIFY_ON_START, default=False): bool,
                vol.Optional(CONF_TELEGRAM_NOTIFY_ON_SUCCESS, default=True): bool,
                vol.Optional(CONF_TELEGRAM_NOTIFY_ON_ERROR, default=True): bool,
            }
        )

        return self.async_show_form(step_id="telegram", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> DomoLinkBackupOptionsFlow:
        """Create the options flow."""
        return DomoLinkBackupOptionsFlow(config_entry)


class DomoLinkBackupOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for DomoLink-BackUp."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry
        self.options: dict[str, Any] = dict(config_entry.options or config_entry.data)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Entry point for options flow."""
        return await self.async_step_destination()

    async def async_step_destination(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Modify destination & protocol."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_credentials()

        nas_options = [
            selector.SelectOptionDict(value="synology", label="Synology (DSM)"),
            selector.SelectOptionDict(value="qnap", label="QNAP (QTS)"),
            selector.SelectOptionDict(value="asustor", label="ASUSTOR (ADM)"),
            selector.SelectOptionDict(value="truenas", label="TrueNAS (SCALE/CORE)"),
            selector.SelectOptionDict(value="freebox", label="Freebox (Delta / Ultra / Pop)"),
            selector.SelectOptionDict(value="unraid", label="Unraid"),
            selector.SelectOptionDict(value="generic", label="Autre NAS / Serveur personnalisé"),
        ]

        proto_options = [
            selector.SelectOptionDict(value=PROTO_FTP, label="FTP (Standard port 21)"),
            selector.SelectOptionDict(value=PROTO_FTPS, label="FTPS (Sécurisé avec TLS/SSL)"),
            selector.SelectOptionDict(value=PROTO_WEBDAV, label="WebDAV (HTTP / HTTPS)"),
            selector.SelectOptionDict(value=PROTO_GOOGLE_DRIVE, label="Google Drive (Webhook)"),
            selector.SelectOptionDict(value=PROTO_LOCAL_SHARE, label="Partage Réseau Local (Dossier partagé)"),
        ]

        current_nas = self.options.get(CONF_NAS_TYPE, "synology")
        current_proto = self.options.get(CONF_PROTOCOL, PROTO_FTP)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAS_TYPE, default=current_nas): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=nas_options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_PROTOCOL, default=current_proto): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=proto_options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
            }
        )

        return self.async_show_form(step_id="destination", data_schema=schema)

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Modify credentials."""
        proto = self.options.get(CONF_PROTOCOL, PROTO_FTP)
        nas_type = self.options.get(CONF_NAS_TYPE, NAS_GENERIC)
        preset = DEFAULT_NAS_CONFIGS.get(nas_type, {})

        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_retention()

        schema_dict: dict[Any, Any] = {}

        if proto in (PROTO_FTP, PROTO_FTPS):
            default_port = preset.get("ftp_port", DEFAULT_FTP_PORT)
            default_path = preset.get("ftp_path", DEFAULT_FTP_PATH)
            default_host = preset.get("ftp_host", "")
            default_user = preset.get("ftp_user", "")

            schema_dict = {
                vol.Required(CONF_FTP_HOST, default=self.options.get(CONF_FTP_HOST) or default_host): str,
                vol.Required(CONF_FTP_PORT, default=int(self.options.get(CONF_FTP_PORT) or default_port)): int,
                vol.Required(CONF_FTP_USER, default=self.options.get(CONF_FTP_USER) or default_user): str,
                vol.Required(CONF_FTP_PASS, default=self.options.get(CONF_FTP_PASS, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_FTP_PATH, default=self.options.get(CONF_FTP_PATH) or default_path): str,
                vol.Optional(CONF_FTP_TLS, default=bool(self.options.get(CONF_FTP_TLS, proto == PROTO_FTPS))): bool,
            }

        elif proto == PROTO_WEBDAV:
            default_port = preset.get("webdav_port", 5006 if nas_type == "synology" else 80)
            default_path = preset.get("webdav_path", DEFAULT_WEBDAV_PATH)
            ssl_scheme = "https" if preset.get("webdav_ssl", True) else "http"
            default_url = f"{ssl_scheme}://192.168.1.100:{default_port}" if nas_type != "freebox" else "http://mafreebox.freebox.fr"

            schema_dict = {
                vol.Required(CONF_WEBDAV_URL, default=self.options.get(CONF_WEBDAV_URL) or default_url): str,
                vol.Required(CONF_WEBDAV_USER, default=self.options.get(CONF_WEBDAV_USER, "")): str,
                vol.Required(CONF_WEBDAV_PASS, default=self.options.get(CONF_WEBDAV_PASS, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_WEBDAV_PATH, default=self.options.get(CONF_WEBDAV_PATH) or default_path): str,
                vol.Optional(CONF_WEBDAV_VERIFY_SSL, default=bool(self.options.get(CONF_WEBDAV_VERIFY_SSL, False))): bool,
            }

        elif proto == PROTO_GOOGLE_DRIVE:
            schema_dict = {
                vol.Required(CONF_GOOGLE_DRIVE_WEBHOOK_URL, default=self.options.get(CONF_GOOGLE_DRIVE_WEBHOOK_URL, "")): str,
                vol.Optional(CONF_GOOGLE_DRIVE_FOLDER_ID, default=self.options.get(CONF_GOOGLE_DRIVE_FOLDER_ID, "")): str,
            }

        elif proto == PROTO_LOCAL_SHARE:
            schema_dict = {
                vol.Required(CONF_LOCAL_SHARE_PATH, default=self.options.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)): str,
            }

        return self.async_show_form(step_id="credentials", data_schema=vol.Schema(schema_dict))

    async def async_step_retention(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Modify retention rules."""
        if user_input is not None:
            self.options.update(user_input)
            return await self.async_step_telegram()

        schema = vol.Schema(
            {
                vol.Required(CONF_MAX_BACKUPS_COUNT, default=int(self.options.get(CONF_MAX_BACKUPS_COUNT, DEFAULT_MAX_BACKUPS_COUNT))): int,
                vol.Required(CONF_RETENTION_DAYS, default=int(self.options.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS))): int,
                vol.Required(CONF_MAX_STORAGE_MB, default=int(self.options.get(CONF_MAX_STORAGE_MB, DEFAULT_MAX_STORAGE_MB))): int,
                vol.Optional(CONF_AUTO_CLEAN_ENABLED, default=bool(self.options.get(CONF_AUTO_CLEAN_ENABLED, True))): bool,
                vol.Optional(CONF_BACKUP_NAME_TEMPLATE, default=str(self.options.get(CONF_BACKUP_NAME_TEMPLATE, DEFAULT_BACKUP_NAME_TEMPLATE))): str,
            }
        )

        return self.async_show_form(step_id="retention", data_schema=schema)

    async def async_step_telegram(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Modify Telegram settings and finalize."""
        if user_input is not None:
            self.options.update(user_input)
            return self.async_create_entry(title="", data=self.options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_TELEGRAM_ENABLED, default=bool(self.options.get(CONF_TELEGRAM_ENABLED, False))): bool,
                vol.Optional(CONF_TELEGRAM_TOKEN, default=self.options.get(CONF_TELEGRAM_TOKEN, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_TELEGRAM_CHAT_ID, default=self.options.get(CONF_TELEGRAM_CHAT_ID, "")): str,
                vol.Optional(CONF_TELEGRAM_NOTIFY_ON_START, default=bool(self.options.get(CONF_TELEGRAM_NOTIFY_ON_START, False))): bool,
                vol.Optional(CONF_TELEGRAM_NOTIFY_ON_SUCCESS, default=bool(self.options.get(CONF_TELEGRAM_NOTIFY_ON_SUCCESS, True))): bool,
                vol.Optional(CONF_TELEGRAM_NOTIFY_ON_ERROR, default=bool(self.options.get(CONF_TELEGRAM_NOTIFY_ON_ERROR, True))): bool,
            }
        )

        return self.async_show_form(step_id="telegram", data_schema=schema)


CONFIG_FLOW = DomoLinkBackupConfigFlow
