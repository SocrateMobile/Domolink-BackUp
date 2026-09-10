"""Storage Engine for DomoLink-BackUp.

Supports multi-destination backup operations:
- FTP & FTPS (TLS)
- WebDAV (Nextcloud, ownCloud, Synology, QNAP, Asustor, TrueNAS)
- Google Drive (Webhook via Google Apps Script)
- Local / Mounted Network Share (SMB, NFS, Local folder)
"""
from __future__ import annotations

import asyncio
import base64
import ftplib
import io
import logging
import os
import re
import shutil
import ssl
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Coroutine
import xml.etree.ElementTree as ET

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AUTO_CLEAN_ENABLED,
    CONF_DESTINATION_TYPE,
    CONF_FTP_ENABLED,
    CONF_FTP_HOST,
    CONF_FTP_PASS,
    CONF_FTP_PATH,
    CONF_FTP_PORT,
    CONF_FTP_TLS,
    CONF_FTP_USER,
    CONF_GOOGLE_DRIVE_ENABLED,
    CONF_GOOGLE_DRIVE_FOLDER_ID,
    CONF_GOOGLE_DRIVE_SECRET_KEY,
    CONF_GOOGLE_DRIVE_WEBHOOK_URL,
    CONF_LOCAL_SHARE_PATH,
    CONF_MAX_BACKUPS_COUNT,
    CONF_MAX_STORAGE_MB,
    CONF_NAS_TYPE,
    CONF_PROTOCOL,
    CONF_RETENTION_DAYS,
    CONF_WEBDAV_ENABLED,
    CONF_WEBDAV_PASS,
    CONF_WEBDAV_PATH,
    CONF_WEBDAV_URL,
    CONF_WEBDAV_USER,
    CONF_WEBDAV_VERIFY_SSL,
    DEFAULT_FTP_PATH,
    DEFAULT_FTP_PORT,
    DEFAULT_LOCAL_SHARE_PATH,
    DEFAULT_MAX_BACKUPS_COUNT,
    DEFAULT_MAX_STORAGE_MB,
    DEFAULT_NAS_CONFIGS,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_WEBDAV_PATH,
    NAS_GENERIC,
    PROTO_FTP,
    PROTO_FTPS,
    PROTO_GOOGLE_DRIVE,
    PROTO_LOCAL_SHARE,
    PROTO_SFTP,
    PROTO_WEBDAV,
)

_LOGGER = logging.getLogger(__name__)


class DomoLinkStorageEngine:
    """Multi-destination storage manager for DomoLink-BackUp."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        """Initialize the storage engine with integration config."""
        self.hass = hass
        self.config = dict(config)
        self._last_test_result: dict[str, Any] = {}
        self._test_logs: list[dict[str, Any]] = []

    def update_config(self, new_config: dict[str, Any]) -> None:
        """Update active configuration."""
        self.config.update(new_config)

    @property
    def protocol(self) -> str:
        """Return configured protocol."""
        proto = self.config.get(CONF_PROTOCOL)
        if proto:
            return str(proto).lower()

        # Deduce from flags or NAS preset
        nas_type = self.config.get(CONF_NAS_TYPE, NAS_GENERIC)
        nas_preset = DEFAULT_NAS_CONFIGS.get(nas_type, {})
        return nas_preset.get("default_protocol", PROTO_FTP)

    @property
    def destination_label(self) -> str:
        """Return a human-friendly destination label."""
        nas_type = self.config.get(CONF_NAS_TYPE)
        proto = self.protocol
        if nas_type and nas_type in DEFAULT_NAS_CONFIGS:
            nas_name = DEFAULT_NAS_CONFIGS[nas_type]["name"]
            return f"{nas_name} ({proto.upper()})"
        if proto == PROTO_GOOGLE_DRIVE:
            return "Google Drive (Cloud)"
        if proto == PROTO_WEBDAV:
            return "Serveur WebDAV"
        if proto == PROTO_LOCAL_SHARE:
            return "Partage Réseau Local"
        return f"Serveur {proto.upper()}"

    # ═════════════════════════════════════════════════════════════════════
    # DIAGNOSTICS & CONNECTION TESTING
    # ═════════════════════════════════════════════════════════════════════

    async def async_test_connection(self, override_config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Test communication with the configured storage target and log steps."""
        cfg = dict(self.config)
        if override_config:
            cfg.update(override_config)

        proto = cfg.get(CONF_PROTOCOL) or self.protocol
        self._test_logs = []
        start_time = time.monotonic()

        self._log_test(f"Démarrage du test pour : {self.destination_label} (Protocole: {proto.upper()})", "info")

        try:
            if proto in (PROTO_FTP, PROTO_FTPS, PROTO_SFTP):
                res = await self._async_test_ftp(cfg)
            elif proto == PROTO_WEBDAV:
                res = await self._async_test_webdav(cfg)
            elif proto == PROTO_GOOGLE_DRIVE:
                res = await self._async_test_google_drive(cfg)
            elif proto == PROTO_LOCAL_SHARE:
                res = await self._async_test_local_share(cfg)
            else:
                res = {
                    "success": False,
                    "code": 400,
                    "result_label": "Protocole inconnu",
                    "message": f"Protocole {proto} non pris en charge.",
                }
        except Exception as err:
            _LOGGER.exception("DomoLink-BackUp: Erreur inattendue pendant le test de connexion: %s", err)
            res = {
                "success": False,
                "code": 500,
                "result_label": "Erreur 500",
                "message": f"Erreur inattendue : {err}",
            }

        elapsed = round(time.monotonic() - start_time, 2)
        res["elapsed"] = elapsed
        res["logs"] = list(self._test_logs)
        self._last_test_result = res

        status_text = "✓ Connecté" if res.get("success") else f"✗ {res.get('result_label', 'Erreur')}"
        self._log_test(f"Résultat final ({elapsed}s) : {status_text} - {res.get('message', '')}", "success" if res.get("success") else "error")
        return res

    def _log_test(self, message: str, level: str = "info") -> None:
        """Append log message to the test session."""
        now_str = datetime.now().strftime("%H:%M:%S")
        self._test_logs.append({"time": now_str, "message": message, "level": level})
        if len(self._test_logs) > 80:
            self._test_logs = self._test_logs[-80:]

    # ─── FTP / FTPS Test ───
    async def _async_test_ftp(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Test FTP / FTPS connection in executor."""
        host = cfg.get(CONF_FTP_HOST, "")
        port = int(cfg.get(CONF_FTP_PORT, DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT)
        user = cfg.get(CONF_FTP_USER, "")
        passwd = cfg.get(CONF_FTP_PASS, "")
        path = (cfg.get(CONF_FTP_PATH, DEFAULT_FTP_PATH) or DEFAULT_FTP_PATH).strip()
        use_tls = bool(cfg.get(CONF_FTP_TLS, False) or cfg.get(CONF_PROTOCOL) == PROTO_FTPS)

        if not host:
            self._log_test("Hôte FTP manquant.", "error")
            return {"success": False, "code": 400, "result_label": "Erreur 400", "message": "Hôte FTP non configuré."}

        self._log_test(f"1. Connexion réseau vers {host}:{port} (TLS={use_tls})...", "info")

        def _sync_ftp_test():
            ftp = None
            try:
                if use_tls:
                    ftp = ftplib.FTP_TLS(timeout=10)
                    ftp.connect(host, port)
                    ftp.auth()
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP(timeout=10)
                    ftp.connect(host, port)

                ftp.login(user, passwd)
                ftp.set_pasv(True)

                # Ensure directory exists or create it
                cur = ""
                for part in path.strip("/").split("/"):
                    if not part:
                        continue
                    cur += f"/{part}"
                    try:
                        ftp.cwd(cur)
                    except ftplib.error_perm:
                        ftp.mkd(cur)
                        ftp.cwd(cur)

                # Write probe file
                probe_filename = f"domolink_probe_{int(time.time())}.txt"
                probe_data = io.BytesIO(b"DomoLink-BackUp Probe Test OK")
                ftp.storbinary(f"STOR {probe_filename}", probe_data)

                # Clean probe file
                try:
                    ftp.delete(probe_filename)
                except Exception:
                    pass

                ftp.quit()
                return {"success": True, "code": 200, "result_label": "Connecté", "message": "Accès en lecture et écriture vérifié avec succès."}

            except ftplib.error_perm as perm_err:
                err_code = 530
                msg = str(perm_err)
                m = re.search(r"\b([1-5]\d{2})\b", msg)
                if m:
                    err_code = int(m.group(1))
                return {"success": False, "code": err_code, "result_label": f"Erreur {err_code}", "message": f"Erreur FTP : {msg}"}
            except (TimeoutError, asyncio.TimeoutError):
                return {"success": False, "code": 110, "result_label": "Erreur 110", "message": "Délai de connexion dépassé (Timeout)."}
            except OSError as os_err:
                err_code = abs(os_err.errno) if os_err.errno else 111
                return {"success": False, "code": err_code, "result_label": f"Erreur {err_code}", "message": f"Erreur réseau : {os_err}"}
            except Exception as e:
                return {"success": False, "code": 500, "result_label": "Erreur", "message": f"Erreur : {e}"}
            finally:
                if ftp:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        res = await self.hass.async_add_executor_job(_sync_ftp_test)
        if res.get("success"):
            self._log_test("   ✓ Connexion et droits d'écriture validés.", "success")
        else:
            self._log_test(f"   ✗ Échec : {res.get('message')}", "error")
        return res

    # ─── WebDAV Test ───
    async def _async_test_webdav(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Test WebDAV connection."""
        url = cfg.get(CONF_WEBDAV_URL, "").strip()
        user = cfg.get(CONF_WEBDAV_USER, "").strip()
        passwd = cfg.get(CONF_WEBDAV_PASS, "").strip()
        path = (cfg.get(CONF_WEBDAV_PATH, DEFAULT_WEBDAV_PATH) or DEFAULT_WEBDAV_PATH).strip().strip("/")
        verify_ssl = bool(cfg.get(CONF_WEBDAV_VERIFY_SSL, True))

        if not url:
            self._log_test("URL WebDAV manquante.", "error")
            return {"success": False, "code": 400, "result_label": "Erreur 400", "message": "URL WebDAV non configurée."}

        if not (url.startswith("http://") or url.startswith("https://")):
            self._log_test("URL WebDAV invalide (doit commencer par http:// ou https://).", "error")
            return {"success": False, "code": 400, "result_label": "Erreur 400", "message": "URL WebDAV invalide."}

        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        auth = aiohttp.BasicAuth(user, passwd) if user and passwd else None
        base_url = url.rstrip("/")

        self._log_test(f"1. Test d'accès WebDAV (PROPFIND) sur {base_url}...", "info")

        try:
            async with session.request("PROPFIND", base_url, headers={"Depth": "0"}, auth=auth, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (401, 403):
                    self._log_test(f"   ✗ Authentification rejetée (HTTP {resp.status})", "error")
                    return {"success": False, "code": resp.status, "result_label": f"Erreur {resp.status}", "message": "Identifiants WebDAV invalides."}
                elif resp.status == 404:
                    self._log_test(f"   ✗ URL introuvable (HTTP 404)", "error")
                    return {"success": False, "code": 404, "result_label": "Erreur 404", "message": "Chemin WebDAV introuvable."}
                elif resp.status not in (200, 207, 405):
                    self._log_test(f"   ⚠️ Réponse inattendue (HTTP {resp.status}), poursuite...", "warning")

            self._log_test(f"2. Vérification / création de l'arborescence '{path}' (MKCOL)...", "info")
            cur_url = base_url
            for d in path.split("/"):
                if not d:
                    continue
                cur_url = f"{cur_url}/{d}"
                try:
                    async with session.request("MKCOL", cur_url, auth=auth, timeout=aiohttp.ClientTimeout(total=6)) as mkcol_resp:
                        pass
                except Exception:
                    pass

            self._log_test("3. Test des permissions d'écriture (PUT)...", "info")
            probe_filename = f"domolink_probe_{int(time.time())}.txt"
            probe_url = f"{cur_url}/{probe_filename}"
            async with session.put(
                probe_url,
                data=b"DomoLink-BackUp WebDAV Probe OK",
                headers={"Content-Type": "text/plain"},
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as put_resp:
                if put_resp.status not in (200, 201, 204):
                    self._log_test(f"   ✗ Échec de téléversement (HTTP {put_resp.status})", "error")
                    return {"success": False, "code": put_resp.status, "result_label": f"Erreur {put_resp.status}", "message": f"Échec écriture (HTTP {put_resp.status})"}

            # Cleanup probe
            try:
                async with session.delete(probe_url, auth=auth, timeout=aiohttp.ClientTimeout(total=5)):
                    pass
            except Exception:
                pass

            return {"success": True, "code": 200, "result_label": "Connecté", "message": "Connexion WebDAV et droits d'écriture validés."}

        except (TimeoutError, asyncio.TimeoutError):
            self._log_test("   ✗ Délai dépassé (Timeout)", "error")
            return {"success": False, "code": 110, "result_label": "Erreur 110", "message": "Délai de connexion dépassé (Timeout)."}
        except aiohttp.ClientConnectorError as conn_err:
            self._log_test(f"   ✗ Connexion refusée : {conn_err}", "error")
            return {"success": False, "code": 111, "result_label": "Erreur 111", "message": f"Connexion refusée : {conn_err}"}
        except Exception as e:
            self._log_test(f"   ✗ Erreur : {e}", "error")
            return {"success": False, "code": 500, "result_label": "Erreur", "message": str(e)}

    # ─── Google Drive Test ───
    async def _async_test_google_drive(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Test Google Drive Webhook endpoint."""
        webhook_url = cfg.get(CONF_GOOGLE_DRIVE_WEBHOOK_URL, "").strip()
        folder_id = cfg.get(CONF_GOOGLE_DRIVE_FOLDER_ID, "").strip()

        if not webhook_url:
            self._log_test("URL du Webhook Google Apps Script manquante.", "error")
            return {"success": False, "code": 400, "result_label": "Erreur 400", "message": "URL Webhook manquante."}

        if not webhook_url.startswith("https://script.google.com/"):
            self._log_test("URL non valide (doit débuter par https://script.google.com/)", "error")
            return {"success": False, "code": 400, "result_label": "Erreur 400", "message": "URL Webhook non valide."}

        self._log_test("1. Envoi de la sonde diagnostique vers Google Apps Script...", "info")
        session = async_get_clientsession(self.hass)
        payload = {"probe": True, "folder_id": folder_id}

        try:
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    self._log_test(f"   ✗ Erreur HTTP {resp.status} reçue de Google Apps Script", "error")
                    return {"success": False, "code": resp.status, "result_label": f"Erreur {resp.status}", "message": f"Réponse HTTP {resp.status}"}

                data = await resp.json(content_type=None)
                if data.get("success"):
                    self._log_test("   ✓ Sonde validée par Google Drive avec succès.", "success")
                    return {"success": True, "code": 200, "result_label": "Connecté", "message": data.get("message", "Connexion Google Drive établie avec succès.")}
                else:
                    msg = data.get("message", "Erreur retournée par le script.")
                    self._log_test(f"   ✗ Script Google Apps Script : {msg}", "error")
                    return {"success": False, "code": data.get("code", 400), "result_label": "Erreur Script", "message": msg}

        except (TimeoutError, asyncio.TimeoutError):
            self._log_test("   ✗ Délai dépassé (Timeout)", "error")
            return {"success": False, "code": 110, "result_label": "Erreur 110", "message": "Délai de connexion dépassé."}
        except Exception as e:
            self._log_test(f"   ✗ Erreur : {e}", "error")
            return {"success": False, "code": 500, "result_label": "Erreur", "message": str(e)}

    # ─── Local / Share Test ───
    async def _async_test_local_share(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Test local folder or mounted share."""
        path = cfg.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)
        self._log_test(f"Vérification du dossier local/partagé : {path}", "info")

        def _sync_test():
            try:
                os.makedirs(path, exist_ok=True)
                probe_file = os.path.join(path, f"domolink_probe_{int(time.time())}.txt")
                with open(probe_file, "w") as f:
                    f.write("DomoLink-BackUp Probe OK")
                if os.path.exists(probe_file):
                    os.remove(probe_file)
                total, used, free = shutil.disk_usage(path)
                free_gb = round(free / (1024**3), 2)
                return {"success": True, "code": 200, "result_label": "Connecté", "message": f"Dossier accessible en écriture. Espace libre : {free_gb} Go"}
            except Exception as err:
                return {"success": False, "code": 500, "result_label": "Erreur Dossier", "message": str(err)}

        res = await self.hass.async_add_executor_job(_sync_test)
        if res.get("success"):
            self._log_test(f"   ✓ {res.get('message')}", "success")
        else:
            self._log_test(f"   ✗ {res.get('message')}", "error")
        return res

    # ═════════════════════════════════════════════════════════════════════
    # BACKUP UPLOAD OPERATIONS
    # ═════════════════════════════════════════════════════════════════════

    async def async_upload(
        self,
        source: str | Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        filename: str,
        size: int = 0,
        on_progress: Callable[[int], None] | None = None,
    ) -> bool:
        """Upload a backup archive to the configured destination."""
        proto = self.protocol
        _LOGGER.info("DomoLink-BackUp: Début de l'envoi de '%s' via %s (taille: %s octets)", filename, proto.upper(), size)

        # If source is an async stream factory, convert or buffer as needed
        file_path = None
        temp_created = False

        if isinstance(source, str):
            file_path = source
        else:
            # Need a concrete file for FTP executor or large uploads
            temp_path = self.hass.config.path(f"domolink_backup_temp_{filename}")
            temp_created = True
            bytes_written = 0
            stream = await source()
            with open(temp_path, "wb") as f:
                async for chunk in stream:
                    f.write(chunk)
                    bytes_written += len(chunk)
                    if on_progress:
                        try:
                            on_progress(bytes_written)
                        except Exception:
                            pass
            file_path = temp_path
            if size == 0:
                size = bytes_written

        try:
            if proto in (PROTO_FTP, PROTO_FTPS, PROTO_SFTP):
                success = await self._async_upload_ftp(file_path, filename, on_progress)
            elif proto == PROTO_WEBDAV:
                success = await self._async_upload_webdav(file_path, filename, size, on_progress)
            elif proto == PROTO_GOOGLE_DRIVE:
                success = await self._async_upload_google_drive(file_path, filename, on_progress)
            elif proto == PROTO_LOCAL_SHARE:
                success = await self._async_upload_local_share(file_path, filename, on_progress)
            else:
                _LOGGER.error("DomoLink-BackUp: Protocole '%s' non pris en charge pour l'envoi", proto)
                success = False

            if success and self.config.get(CONF_AUTO_CLEAN_ENABLED, True):
                self.hass.async_create_task(self.async_apply_retention())

            return success
        finally:
            if temp_created and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

    # ─── FTP Upload ───
    async def _async_upload_ftp(self, file_path: str, filename: str, on_progress: Callable[[int], None] | None) -> bool:
        """Upload file via FTP / FTPS with progress callback."""
        cfg = self.config
        host = cfg.get(CONF_FTP_HOST, "")
        port = int(cfg.get(CONF_FTP_PORT, DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT)
        user = cfg.get(CONF_FTP_USER, "")
        passwd = cfg.get(CONF_FTP_PASS, "")
        path = (cfg.get(CONF_FTP_PATH, DEFAULT_FTP_PATH) or DEFAULT_FTP_PATH).strip()
        use_tls = bool(cfg.get(CONF_FTP_TLS, False) or self.protocol == PROTO_FTPS)

        def _sync_ftp_upload():
            ftp = None
            try:
                if use_tls:
                    ftp = ftplib.FTP_TLS(timeout=60)
                    ftp.connect(host, port)
                    ftp.auth()
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP(timeout=60)
                    ftp.connect(host, port)

                ftp.login(user, passwd)
                ftp.set_pasv(True)

                cur = ""
                for part in path.strip("/").split("/"):
                    if not part:
                        continue
                    cur += f"/{part}"
                    try:
                        ftp.cwd(cur)
                    except ftplib.error_perm:
                        ftp.mkd(cur)
                        ftp.cwd(cur)

                total_sent = 0

                def _chunk_callback(chunk):
                    nonlocal total_sent
                    total_sent += len(chunk)
                    if on_progress:
                        try:
                            on_progress(total_sent)
                        except Exception:
                            pass

                with open(file_path, "rb") as f:
                    ftp.storbinary(f"STOR {filename}", f, blocksize=65536, callback=_chunk_callback)

                ftp.quit()
                return True
            except Exception as err:
                _LOGGER.error("DomoLink-BackUp: Échec envoi FTP de %s : %s", filename, err)
                return False
            finally:
                if ftp:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        return await self.hass.async_add_executor_job(_sync_ftp_upload)

    # ─── WebDAV Upload ───
    async def _async_upload_webdav(self, file_path: str, filename: str, size: int, on_progress: Callable[[int], None] | None) -> bool:
        """Upload file via WebDAV using streaming chunked PUT."""
        cfg = self.config
        url = cfg.get(CONF_WEBDAV_URL, "").strip().rstrip("/")
        user = cfg.get(CONF_WEBDAV_USER, "").strip()
        passwd = cfg.get(CONF_WEBDAV_PASS, "").strip()
        path = (cfg.get(CONF_WEBDAV_PATH, DEFAULT_WEBDAV_PATH) or DEFAULT_WEBDAV_PATH).strip().strip("/")
        verify_ssl = bool(cfg.get(CONF_WEBDAV_VERIFY_SSL, True))

        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        auth = aiohttp.BasicAuth(user, passwd) if user and passwd else None

        # Ensure directory
        cur_url = url
        for d in path.split("/"):
            if not d:
                continue
            cur_url = f"{cur_url}/{d}"
            try:
                async with session.request("MKCOL", cur_url, auth=auth, timeout=aiohttp.ClientTimeout(total=8)):
                    pass
            except Exception:
                pass

        target_file_url = f"{cur_url}/{filename}"

        async def _file_streamer():
            sent = 0
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if on_progress:
                        try:
                            on_progress(sent)
                        except Exception:
                            pass
                    yield chunk

        try:
            headers = {"Content-Type": "application/x-tar"}
            if size > 0:
                headers["Content-Length"] = str(size)

            async with session.put(
                target_file_url,
                data=_file_streamer(),
                headers=headers,
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=3600),
            ) as resp:
                if resp.status in (200, 201, 204):
                    _LOGGER.info("DomoLink-BackUp: Sauvegarde %s téléversée sur WebDAV avec succès", filename)
                    return True
                else:
                    _LOGGER.error("DomoLink-BackUp: Échec WebDAV (HTTP %s) lors de l'envoi de %s", resp.status, filename)
                    return False
        except Exception as err:
            _LOGGER.error("DomoLink-BackUp: Erreur envoi WebDAV %s : %s", filename, err)
            return False

    # ─── Google Drive Upload ───
    async def _async_upload_google_drive(self, file_path: str, filename: str, on_progress: Callable[[int], None] | None) -> bool:
        """Upload file to Google Drive via Google Apps Script Webhook."""
        cfg = self.config
        webhook_url = cfg.get(CONF_GOOGLE_DRIVE_WEBHOOK_URL, "").strip()
        folder_id = cfg.get(CONF_GOOGLE_DRIVE_FOLDER_ID, "").strip()

        if not webhook_url:
            _LOGGER.error("DomoLink-BackUp: URL Webhook Google Drive non renseignée")
            return False

        def _read_and_encode():
            with open(file_path, "rb") as f:
                content = f.read()
                return base64.b64encode(content).decode("utf-8")

        session = async_get_clientsession(self.hass)
        encoded_content = await self.hass.async_add_executor_job(_read_and_encode)

        payload = {
            "filename": filename,
            "file_content": encoded_content,
            "folder_id": folder_id,
            "mime_type": "application/x-tar",
        }

        try:
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("success"):
                        _LOGGER.info("DomoLink-BackUp: Sauvegarde %s envoyée sur Google Drive avec succès", filename)
                        return True
                    else:
                        _LOGGER.error("DomoLink-BackUp: Réponse négative Google Drive: %s", data.get("message"))
                        return False
                else:
                    _LOGGER.error("DomoLink-BackUp: Erreur HTTP %s lors de l'envoi Google Drive", resp.status)
                    return False
        except Exception as err:
            _LOGGER.error("DomoLink-BackUp: Erreur lors de l'envoi Google Drive : %s", err)
            return False

    # ─── Local / Mounted Share Upload ───
    async def _async_upload_local_share(self, file_path: str, filename: str, on_progress: Callable[[int], None] | None) -> bool:
        """Copy file to local mounted share path."""
        cfg = self.config
        dest_dir = cfg.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)
        target_path = os.path.join(dest_dir, filename)

        def _sync_copy():
            try:
                os.makedirs(dest_dir, exist_ok=True)
                with open(file_path, "rb") as f_in, open(target_path, "wb") as f_out:
                    copied = 0
                    while True:
                        buf = f_in.read(65536)
                        if not buf:
                            break
                        f_out.write(buf)
                        copied += len(buf)
                        if on_progress:
                            try:
                                on_progress(copied)
                            except Exception:
                                pass
                return True
            except Exception as err:
                _LOGGER.error("DomoLink-BackUp: Échec copie locale %s : %s", filename, err)
                return False

        return await self.hass.async_add_executor_job(_sync_copy)

    # ═════════════════════════════════════════════════════════════════════
    # LIST, DOWNLOAD, DELETE OPERATIONS
    # ═════════════════════════════════════════════════════════════════════

    async def async_list_backups(self) -> list[dict[str, Any]]:
        """List all available remote backups across configured storage."""
        proto = self.protocol
        try:
            if proto in (PROTO_FTP, PROTO_FTPS, PROTO_SFTP):
                return await self._async_list_ftp()
            elif proto == PROTO_WEBDAV:
                return await self._async_list_webdav()
            elif proto == PROTO_GOOGLE_DRIVE:
                return await self._async_list_google_drive()
            elif proto == PROTO_LOCAL_SHARE:
                return await self._async_list_local_share()
            return []
        except Exception as err:
            _LOGGER.error("DomoLink-BackUp: Erreur lors du listing des sauvegardes distantes : %s", err)
            return []

    # ─── FTP List ───
    async def _async_list_ftp(self) -> list[dict[str, Any]]:
        cfg = self.config
        host = cfg.get(CONF_FTP_HOST, "")
        port = int(cfg.get(CONF_FTP_PORT, DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT)
        user = cfg.get(CONF_FTP_USER, "")
        passwd = cfg.get(CONF_FTP_PASS, "")
        path = (cfg.get(CONF_FTP_PATH, DEFAULT_FTP_PATH) or DEFAULT_FTP_PATH).strip()
        use_tls = bool(cfg.get(CONF_FTP_TLS, False) or self.protocol == PROTO_FTPS)

        def _sync_list():
            ftp = None
            items = []
            try:
                if use_tls:
                    ftp = ftplib.FTP_TLS(timeout=15)
                    ftp.connect(host, port)
                    ftp.auth()
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP(timeout=15)
                    ftp.connect(host, port)
                ftp.login(user, passwd)
                ftp.set_pasv(True)
                try:
                    ftp.cwd(path)
                except Exception:
                    return []

                # Use MLSD if supported, else NLST
                try:
                    for name, facts in ftp.mlsd():
                        if facts.get("type") == "file" and name.endswith((".tar", ".tar.gz", ".zip")):
                            size = int(facts.get("size", 0))
                            modify_str = facts.get("modify", "")
                            # Parse YYYYMMDDHHMMSS
                            try:
                                dt = datetime.strptime(modify_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                                iso_date = dt.isoformat()
                            except Exception:
                                iso_date = datetime.now(timezone.utc).isoformat()

                            items.append({
                                "backup_id": name.replace(".tar", "").replace(".tar.gz", ""),
                                "name": name,
                                "filename": name,
                                "size": size,
                                "date": iso_date,
                                "protocol": "ftp",
                            })
                except Exception:
                    names = ftp.nlst()
                    for name in names:
                        if name.endswith((".tar", ".tar.gz", ".zip")):
                            try:
                                size = ftp.size(name) or 0
                            except Exception:
                                size = 0
                            items.append({
                                "backup_id": name.replace(".tar", "").replace(".tar.gz", ""),
                                "name": name,
                                "filename": name,
                                "size": size,
                                "date": datetime.now(timezone.utc).isoformat(),
                                "protocol": "ftp",
                            })
                ftp.quit()
                return items
            except Exception as e:
                _LOGGER.error("DomoLink-BackUp: Erreur listing FTP : %s", e)
                return []
            finally:
                if ftp:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        return await self.hass.async_add_executor_job(_sync_list)

    # ─── WebDAV List ───
    async def _async_list_webdav(self) -> list[dict[str, Any]]:
        cfg = self.config
        url = cfg.get(CONF_WEBDAV_URL, "").strip().rstrip("/")
        user = cfg.get(CONF_WEBDAV_USER, "").strip()
        passwd = cfg.get(CONF_WEBDAV_PASS, "").strip()
        path = (cfg.get(CONF_WEBDAV_PATH, DEFAULT_WEBDAV_PATH) or DEFAULT_WEBDAV_PATH).strip().strip("/")
        verify_ssl = bool(cfg.get(CONF_WEBDAV_VERIFY_SSL, True))

        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        auth = aiohttp.BasicAuth(user, passwd) if user and passwd else None
        target_url = f"{url}/{path}" if path else url

        try:
            headers = {"Depth": "1", "Content-Type": "application/xml"}
            body = """<?xml version="1.0" encoding="utf-8" ?>
            <D:propfind xmlns:D="DAV:">
                <D:prop>
                    <D:displayname/>
                    <D:getcontentlength/>
                    <D:getlastmodified/>
                    <D:resourcetype/>
                </D:prop>
            </D:propfind>"""
            async with session.request("PROPFIND", target_url, data=body, headers=headers, auth=auth, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status not in (200, 207):
                    return []
                text = await resp.text()

            # Parse XML
            items = []
            try:
                root = ET.fromstring(text)
                for response_el in root.findall(".//{DAV:}response"):
                    href = response_el.findtext("{DAV:}href") or ""
                    filename = href.rstrip("/").split("/")[-1]
                    if not filename or not filename.endswith((".tar", ".tar.gz", ".zip")):
                        continue

                    # Check resource type (exclude collections)
                    res_type = response_el.find(".//{DAV:}resourcetype")
                    if res_type is not None and res_type.find("{DAV:}collection") is not None:
                        continue

                    size = int(response_el.findtext(".//{DAV:}getcontentlength") or 0)
                    last_mod = response_el.findtext(".//{DAV:}getlastmodified") or ""
                    try:
                        # RFC 1123 format (e.g. Thu, 10 Sep 2026 12:00:00 GMT)
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(last_mod)
                        iso_date = dt.isoformat()
                    except Exception:
                        iso_date = datetime.now(timezone.utc).isoformat()

                    items.append({
                        "backup_id": filename.replace(".tar", "").replace(".tar.gz", ""),
                        "name": filename,
                        "filename": filename,
                        "size": size,
                        "date": iso_date,
                        "protocol": "webdav",
                    })
            except Exception as xml_err:
                _LOGGER.warning("DomoLink-BackUp: Erreur parsing XML WebDAV: %s", xml_err)

            return items
        except Exception as err:
            _LOGGER.error("DomoLink-BackUp: Erreur listing WebDAV : %s", err)
            return []

    # ─── Google Drive List ───
    async def _async_list_google_drive(self) -> list[dict[str, Any]]:
        cfg = self.config
        webhook_url = cfg.get(CONF_GOOGLE_DRIVE_WEBHOOK_URL, "").strip()
        folder_id = cfg.get(CONF_GOOGLE_DRIVE_FOLDER_ID, "").strip()

        if not webhook_url:
            return []

        session = async_get_clientsession(self.hass)
        try:
            payload = {"action": "list", "folder_id": folder_id}
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    backups = data.get("backups", [])
                    result = []
                    for b in backups:
                        fname = b.get("name", "")
                        if fname.endswith((".tar", ".tar.gz", ".zip")):
                            result.append({
                                "backup_id": b.get("id"),
                                "name": fname,
                                "filename": fname,
                                "size": int(b.get("size", 0)),
                                "date": b.get("date", datetime.now(timezone.utc).isoformat()),
                                "protocol": "google_drive",
                            })
                    return result
            return []
        except Exception as err:
            _LOGGER.error("DomoLink-BackUp: Erreur listing Google Drive: %s", err)
            return []

    # ─── Local Share List ───
    async def _async_list_local_share(self) -> list[dict[str, Any]]:
        dest_dir = self.config.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)

        def _sync_list():
            if not os.path.exists(dest_dir):
                return []
            items = []
            for entry in os.scandir(dest_dir):
                if entry.is_file() and entry.name.endswith((".tar", ".tar.gz", ".zip")):
                    stat = entry.stat()
                    dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    items.append({
                        "backup_id": entry.name.replace(".tar", "").replace(".tar.gz", ""),
                        "name": entry.name,
                        "filename": entry.name,
                        "size": stat.st_size,
                        "date": dt.isoformat(),
                        "protocol": "local_share",
                    })
            return items

        return await self.hass.async_add_executor_job(_sync_list)

    # ─── Download Backup ───
    async def async_download_backup(self, backup_id: str) -> AsyncIterator[bytes]:
        """Download or stream a backup from the storage target."""
        proto = self.protocol
        backups = await self.async_list_backups()
        target = next((b for b in backups if b["backup_id"] == backup_id or b["filename"] == backup_id), None)
        filename = target["filename"] if target else f"{backup_id}.tar"

        if proto in (PROTO_FTP, PROTO_FTPS, PROTO_SFTP):
            temp_path = self.hass.config.path(f"domolink_dl_{filename}")
            await self._async_download_ftp(filename, temp_path)
            return self._async_stream_local_file(temp_path)
        elif proto == PROTO_WEBDAV:
            return await self._async_download_webdav_stream(filename)
        elif proto == PROTO_LOCAL_SHARE:
            dest_dir = self.config.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)
            local_path = os.path.join(dest_dir, filename)
            return self._async_stream_local_file(local_path, auto_delete=False)
        else:
            raise NotImplementedError(f"Téléchargement non supporté directement pour le protocole {proto}")

    async def _async_download_ftp(self, filename: str, dest_path: str) -> None:
        cfg = self.config
        host = cfg.get(CONF_FTP_HOST, "")
        port = int(cfg.get(CONF_FTP_PORT, DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT)
        user = cfg.get(CONF_FTP_USER, "")
        passwd = cfg.get(CONF_FTP_PASS, "")
        path = (cfg.get(CONF_FTP_PATH, DEFAULT_FTP_PATH) or DEFAULT_FTP_PATH).strip()
        use_tls = bool(cfg.get(CONF_FTP_TLS, False) or self.protocol == PROTO_FTPS)

        def _sync_dl():
            ftp = None
            try:
                if use_tls:
                    ftp = ftplib.FTP_TLS(timeout=60)
                    ftp.connect(host, port)
                    ftp.auth()
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP(timeout=60)
                    ftp.connect(host, port)
                ftp.login(user, passwd)
                ftp.set_pasv(True)
                ftp.cwd(path)
                with open(dest_path, "wb") as f:
                    ftp.retrbinary(f"RETR {filename}", f.write)
                ftp.quit()
            finally:
                if ftp:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        await self.hass.async_add_executor_job(_sync_dl)

    async def _async_download_webdav_stream(self, filename: str) -> AsyncIterator[bytes]:
        cfg = self.config
        url = cfg.get(CONF_WEBDAV_URL, "").strip().rstrip("/")
        user = cfg.get(CONF_WEBDAV_USER, "").strip()
        passwd = cfg.get(CONF_WEBDAV_PASS, "").strip()
        path = (cfg.get(CONF_WEBDAV_PATH, DEFAULT_WEBDAV_PATH) or DEFAULT_WEBDAV_PATH).strip().strip("/")
        verify_ssl = bool(cfg.get(CONF_WEBDAV_VERIFY_SSL, True))

        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        auth = aiohttp.BasicAuth(user, passwd) if user and passwd else None
        file_url = f"{url}/{path}/{filename}" if path else f"{url}/{filename}"

        resp = await session.get(file_url, auth=auth, timeout=aiohttp.ClientTimeout(total=3600))
        if resp.status != 200:
            resp.close()
            raise FileNotFoundError(f"Fichier non trouvé sur WebDAV (HTTP {resp.status})")

        async def _stream():
            try:
                while True:
                    chunk = await resp.content.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                resp.close()

        return _stream()

    async def _async_stream_local_file(self, file_path: str, auto_delete: bool = True) -> AsyncIterator[bytes]:
        async def _stream():
            try:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                if auto_delete and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        return _stream()

    # ─── Delete Backup ───
    async def async_delete_backup(self, backup_id: str) -> bool:
        """Delete a remote backup archive by ID or filename."""
        proto = self.protocol
        backups = await self.async_list_backups()
        target = next((b for b in backups if b["backup_id"] == backup_id or b["filename"] == backup_id), None)
        filename = target["filename"] if target else f"{backup_id}.tar"

        _LOGGER.info("DomoLink-BackUp: Suppression de la sauvegarde distante %s (ID: %s)", filename, backup_id)

        if proto in (PROTO_FTP, PROTO_FTPS, PROTO_SFTP):
            return await self._async_delete_ftp(filename)
        elif proto == PROTO_WEBDAV:
            return await self._async_delete_webdav(filename)
        elif proto == PROTO_GOOGLE_DRIVE:
            file_id = target.get("backup_id") if target else backup_id
            return await self._async_delete_google_drive(file_id)
        elif proto == PROTO_LOCAL_SHARE:
            return await self._async_delete_local_share(filename)
        return False

    async def _async_delete_ftp(self, filename: str) -> bool:
        cfg = self.config
        host = cfg.get(CONF_FTP_HOST, "")
        port = int(cfg.get(CONF_FTP_PORT, DEFAULT_FTP_PORT) or DEFAULT_FTP_PORT)
        user = cfg.get(CONF_FTP_USER, "")
        passwd = cfg.get(CONF_FTP_PASS, "")
        path = (cfg.get(CONF_FTP_PATH, DEFAULT_FTP_PATH) or DEFAULT_FTP_PATH).strip()
        use_tls = bool(cfg.get(CONF_FTP_TLS, False) or self.protocol == PROTO_FTPS)

        def _sync_del():
            ftp = None
            try:
                if use_tls:
                    ftp = ftplib.FTP_TLS(timeout=15)
                    ftp.connect(host, port)
                    ftp.auth()
                    ftp.prot_p()
                else:
                    ftp = ftplib.FTP(timeout=15)
                    ftp.connect(host, port)
                ftp.login(user, passwd)
                ftp.cwd(path)
                ftp.delete(filename)
                ftp.quit()
                return True
            except Exception as e:
                _LOGGER.error("DomoLink-BackUp: Erreur suppression FTP %s: %s", filename, e)
                return False
            finally:
                if ftp:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        return await self.hass.async_add_executor_job(_sync_del)

    async def _async_delete_webdav(self, filename: str) -> bool:
        cfg = self.config
        url = cfg.get(CONF_WEBDAV_URL, "").strip().rstrip("/")
        user = cfg.get(CONF_WEBDAV_USER, "").strip()
        passwd = cfg.get(CONF_WEBDAV_PASS, "").strip()
        path = (cfg.get(CONF_WEBDAV_PATH, DEFAULT_WEBDAV_PATH) or DEFAULT_WEBDAV_PATH).strip().strip("/")
        verify_ssl = bool(cfg.get(CONF_WEBDAV_VERIFY_SSL, True))

        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        auth = aiohttp.BasicAuth(user, passwd) if user and passwd else None
        file_url = f"{url}/{path}/{filename}" if path else f"{url}/{filename}"

        try:
            async with session.delete(file_url, auth=auth, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            _LOGGER.error("DomoLink-BackUp: Erreur suppression WebDAV %s : %s", filename, e)
            return False

    async def _async_delete_google_drive(self, file_id: str) -> bool:
        webhook_url = self.config.get(CONF_GOOGLE_DRIVE_WEBHOOK_URL, "").strip()
        if not webhook_url:
            return False
        session = async_get_clientsession(self.hass)
        try:
            payload = {"action": "delete", "file_id": file_id}
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return bool(data.get("success"))
            return False
        except Exception as e:
            _LOGGER.error("DomoLink-BackUp: Erreur suppression Google Drive : %s", e)
            return False

    async def _async_delete_local_share(self, filename: str) -> bool:
        dest_dir = self.config.get(CONF_LOCAL_SHARE_PATH, DEFAULT_LOCAL_SHARE_PATH)
        target = os.path.join(dest_dir, filename)

        def _sync_del():
            try:
                if os.path.exists(target):
                    os.remove(target)
                    return True
                return False
            except Exception as e:
                _LOGGER.error("DomoLink-BackUp: Erreur suppression locale %s: %s", filename, e)
                return False

        return await self.hass.async_add_executor_job(_sync_del)

    # ═════════════════════════════════════════════════════════════════════
    # RETENTION & ROTATION (FIFO)
    # ═════════════════════════════════════════════════════════════════════

    async def async_apply_retention(self) -> dict[str, Any]:
        """Apply FIFO retention policy based on max count, age in days, and max total size."""
        cfg = self.config
        max_count = int(cfg.get(CONF_MAX_BACKUPS_COUNT, DEFAULT_MAX_BACKUPS_COUNT) or DEFAULT_MAX_BACKUPS_COUNT)
        retention_days = int(cfg.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS) or DEFAULT_RETENTION_DAYS)
        max_storage_mb = int(cfg.get(CONF_MAX_STORAGE_MB, DEFAULT_MAX_STORAGE_MB) or DEFAULT_MAX_STORAGE_MB)

        backups = await self.async_list_backups()
        if not backups:
            return {"deleted_count": 0, "remaining_count": 0, "total_size_mb": 0}

        # Sort by date, newest first
        def _get_dt(b):
            try:
                return datetime.fromisoformat(b["date"])
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        sorted_backups = sorted(backups, key=_get_dt, reverse=True)
        now = datetime.now(timezone.utc)
        to_delete: list[dict[str, Any]] = []

        # 1. Retention Days Rule
        if retention_days > 0:
            for b in sorted_backups:
                b_dt = _get_dt(b)
                if (now - b_dt).days > retention_days:
                    if b not in to_delete:
                        to_delete.append(b)

        # 2. Max Backups Count Rule (keep the newest `max_count`)
        remaining_candidates = [b for b in sorted_backups if b not in to_delete]
        if max_count > 0 and len(remaining_candidates) > max_count:
            excess = remaining_candidates[max_count:]
            for b in excess:
                if b not in to_delete:
                    to_delete.append(b)

        # 3. Max Storage MB Quota Rule (FIFO delete oldest until under budget)
        active_list = [b for b in sorted_backups if b not in to_delete]
        total_size_bytes = sum(b.get("size", 0) for b in active_list)
        max_bytes = max_storage_mb * 1024 * 1024

        while total_size_bytes > max_bytes and len(active_list) > 1:
            oldest = active_list.pop()  # remove last (oldest)
            to_delete.append(oldest)
            total_size_bytes -= oldest.get("size", 0)

        # Execute deletions
        deleted_count = 0
        for b in to_delete:
            success = await self.async_delete_backup(b.get("backup_id") or b.get("filename"))
            if success:
                deleted_count += 1
                _LOGGER.info("DomoLink-BackUp: Purge automatique (rétention) de l'ancienne sauvegarde : %s", b.get("name"))

        remaining_backups = [b for b in sorted_backups if b not in to_delete]
        remaining_size_mb = round(sum(b.get("size", 0) for b in remaining_backups) / (1024 * 1024), 2)

        return {
            "deleted_count": deleted_count,
            "remaining_count": len(remaining_backups),
            "total_size_mb": remaining_size_mb,
            "purged_files": [b.get("name") for b in to_delete],
        }
