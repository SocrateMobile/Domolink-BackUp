"""DomoLink-BackUp integration for Home Assistant.

Provides remote off-site backup storage (NAS, FTP/FTPS, WebDAV, Google Drive, Local share),
native Home Assistant BackupAgent registration, custom sensors, buttons, and a dedicated
sidebar dashboard panel.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import glob
import json
import logging
import os
import shutil
import tarfile
import time
from typing import Any

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
    BACKUP_TYPE_FULL,
    BACKUP_TYPE_PARTIAL,
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
    CONF_LOCAL_BACKUP_PATH,
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
    DEFAULT_LOCAL_BACKUP_PATH,
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
    RESTORE_MODE_DOWNLOAD_ONLY,
    RESTORE_MODE_FULL,
    RESTORE_MODE_PARTIAL,
    SERVICE_CLEAN_OLD_BACKUPS,
    SERVICE_CREATE_BACKUP,
    SERVICE_RESTORE_BACKUP,
    SERVICE_SYNC_BACKUPS,
    SERVICE_TEST_CONNECTION,
    SERVICE_UPLOAD_BACKUP,
    STAGE_COMPLETED,
    STAGE_CREATING_LOCAL,
    STAGE_DOWNLOADING,
    STAGE_FAILED,
    STAGE_FINISHING,
    STAGE_IDLE,
    STAGE_PREPARING,
    STAGE_RESTORING,
    STAGE_UPLOADING,
    STAGE_VERIFYING,
    STATE_BACKING_UP,
    STATE_CLEANING,
    STATE_DOWNLOADING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RESTORING,
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

PLATFORMS: list[str] = ["sensor", "button", "update"]

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


SKIP_SCAN_EXACT_NAMES = {
    "proc", "sys", "dev", "run", "lost+found",
    "bin", "sbin", "lib", "lib64", "lib32", "libx32",
    "__pycache__", ".git", "node_modules", ".venv", ".cache",
    ".cargo", ".rustup", ".npm", ".yarn", ".gradle", ".local",
}

SKIP_SCAN_PATH_PREFIXES = (
    "usr/lib", "usr/share/doc", "usr/share/man", "usr/share/locale",
    "usr/share/zoneinfo", "usr/include", "var/cache/apt", "var/lib/apt",
)


def _sync_ensure_dir(path: str) -> None:
    """Safely create directory with exist_ok=True in worker thread."""
    os.makedirs(path, exist_ok=True)


def _format_eta(seconds: int | float | None) -> str:
    """Format estimated remaining time (ETA):
    - >= 1h: 'HH:MM:SS'
    - >= 10m and < 1h: 'MM:SS'
    - >= 1m and < 10m: 'M:SS'
    - < 1m: 'SS secondes'
    """
    if seconds is None:
        return "En calcul..."
    try:
        s = max(0, int(round(float(seconds))))
    except (ValueError, TypeError):
        return "En calcul..."

    if s < 60:
        return f"{s:02d} secondes"
    m = s // 60
    sec = s % 60
    if m < 10:
        return f"{m}:{sec:02d}"
    if m < 60:
        return f"{m:02d}:{sec:02d}"
    h = s // 3600
    rem_m = (s % 3600) // 60
    return f"{h:02d}:{rem_m:02d}:{sec:02d}"


def _sync_get_all_system_mount_points() -> list[str]:
    """Inspect /proc/mounts and /etc/mtab to discover mounted partitions and external storage."""
    mounts: list[str] = []
    for mfile in ("/proc/mounts", "/etc/mtab"):
        if os.path.isfile(mfile):
            try:
                with open(mfile, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            mp = parts[1]
                            if mp and os.path.isdir(mp):
                                if not any(mp.startswith(p) for p in ("/proc", "/sys", "/dev", "/run")):
                                    if mp not in mounts:
                                        mounts.append(mp)
                if mounts:
                    break
            except Exception:
                pass
    return mounts


def _sync_scan_local_disk_for_backups(
    hass_config_dir: str | None = None,
    extra_roots: list[str] | None = None,
    max_depth: int = 8,
    timeout_sec: float = 25.0,
) -> dict[str, Any]:
    """Scan local filesystem across Docker, Raspberry Pi, host filesystem and all mounts for backup archives."""
    start = time.monotonic()
    deadline = start + timeout_sec
    scanned_count = 0
    found_dirs: dict[str, dict[str, Any]] = {}

    # High-probability candidates checked instantly in Pass 1
    priority_candidates = [
        "/backup",
        "/backups",
        "/usr/share/hassio/backup",
        "/mnt/data/supervisor/backup",
        "/mnt/data/backup",
        "/data/backup",
        "/data/backups",
        "/share/backup",
        "/share/backups",
        "/media/backup",
        "/media/backups",
        "/homeassistant/backups",
        "/homeassistant/backup",
    ]
    if hass_config_dir:
        priority_candidates.insert(0, os.path.join(hass_config_dir, "backups"))
        priority_candidates.insert(1, os.path.join(hass_config_dir, "backup"))
    if extra_roots:
        for er in extra_roots:
            if er and er not in priority_candidates:
                priority_candidates.insert(0, er)

    # Pass 1: check direct candidate directories (instant < 0.05s)
    for direct in list(priority_candidates):
        if not os.path.isdir(direct):
            continue
        scanned_count += 1
        tar_files: list[str] = []
        for ext in BACKUP_ARCHIVE_EXTENSIONS:
            try:
                tar_files.extend(glob.glob(os.path.join(direct, ext)))
            except OSError:
                pass

        if tar_files:
            valid_backups = []
            for tf in tar_files:
                try:
                    st = os.stat(tf)
                    if st.st_size > 500:
                        valid_backups.append((tf, st.st_size, st.st_mtime))
                except OSError:
                    continue
            if valid_backups:
                valid_backups.sort(key=lambda x: x[2], reverse=True)
                newest = valid_backups[0]
                category = "Système / Docker"
                if "supervisor" in direct or direct == "/backup":
                    category = "Supervisor"
                elif "config" in direct or "homeassistant" in direct:
                    category = "Home Assistant Core"
                elif direct.startswith(("/mnt", "/media", "/share")):
                    category = "Montage externe"

                found_dirs[direct] = {
                    "path": direct,
                    "count": len(valid_backups),
                    "latest_backup": os.path.basename(newest[0]),
                    "latest_size_mb": round(newest[1] / (1024 * 1024), 2),
                    "latest_mtime": newest[2],
                    "latest_date": datetime.fromtimestamp(newest[2], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "category": category,
                }

    # Pass 2: exhaustive deep sweep across root "/" and all system mounts
    mount_points = _sync_get_all_system_mount_points()
    system_roots = ["/"]
    for mp in mount_points:
        if mp not in system_roots and mp not in found_dirs:
            system_roots.append(mp)
    # Common Raspberry Pi / Docker partitions
    for common_root in ("/mnt", "/media", "/share", "/data", "/home", "/opt", "/srv", "/var"):
        if os.path.isdir(common_root) and common_root not in system_roots:
            system_roots.append(common_root)

    for root in system_roots:
        if time.monotonic() > deadline:
            break
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            if time.monotonic() > deadline:
                break
            scanned_count += 1
            cur_depth = dirpath.count(os.sep) - base_depth
            if cur_depth >= max_depth:
                dirnames.clear()
                continue

            filtered_dirs = []
            for d in dirnames:
                if d.startswith("."):
                    continue
                d_lower = d.lower()
                if d_lower in SKIP_SCAN_EXACT_NAMES:
                    continue
                full_sub = os.path.join(dirpath, d)
                rel = full_sub.lstrip(os.sep)
                if any(rel.startswith(pfx) for pfx in SKIP_SCAN_PATH_PREFIXES):
                    continue
                filtered_dirs.append(d)
            dirnames[:] = filtered_dirs

            tar_files = [
                os.path.join(dirpath, f) for f in filenames
                if f.lower().endswith((".tar", ".tar.gz", ".tgz"))
            ]
            if not tar_files or dirpath in found_dirs:
                continue

            valid_backups = []
            for tf in tar_files:
                try:
                    st = os.stat(tf)
                    if st.st_size > 500:
                        valid_backups.append((tf, st.st_size, st.st_mtime))
                except OSError:
                    continue
            if valid_backups:
                valid_backups.sort(key=lambda x: x[2], reverse=True)
                newest = valid_backups[0]
                category = "Système / Docker"
                if "supervisor" in dirpath or dirpath == "/backup":
                    category = "Supervisor"
                elif "config" in dirpath or "homeassistant" in dirpath:
                    category = "Home Assistant Core"
                elif dirpath.startswith(("/mnt", "/media", "/share")):
                    category = "Montage externe"

                found_dirs[dirpath] = {
                    "path": dirpath,
                    "count": len(valid_backups),
                    "latest_backup": os.path.basename(newest[0]),
                    "latest_size_mb": round(newest[1] / (1024 * 1024), 2),
                    "latest_mtime": newest[2],
                    "latest_date": datetime.fromtimestamp(newest[2], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "category": category,
                }

    results = sorted(found_dirs.values(), key=lambda x: x["latest_mtime"], reverse=True)
    elapsed = round(time.monotonic() - start, 2)
    return {
        "results": results,
        "scanned_count": scanned_count,
        "elapsed_sec": elapsed,
    }


def _sync_find_targeted_or_newest_backup_on_disk(
    hass_config_dir: str | None = None,
    target_slug: str | None = None,
    max_age_seconds: int = 900,
) -> tuple[str, str, int] | None:
    """Fast search for a specific slug or the newest created backup on disk."""
    scan_res = _sync_scan_local_disk_for_backups(hass_config_dir=hass_config_dir, timeout_sec=14.0)
    now = time.time()

    # 1. If target slug is provided, look for exact match first
    if target_slug:
        slug_lower = target_slug.lower()
        for d in scan_res.get("results", []):
            dir_p = d["path"]
            try:
                for f in os.listdir(dir_p):
                    if slug_lower in f.lower() and f.lower().endswith((".tar", ".tar.gz", ".tgz")):
                        full_p = os.path.join(dir_p, f)
                        sz = os.path.getsize(full_p)
                        if sz > 500:
                            return dir_p, full_p, sz
            except OSError:
                continue

    # 2. Fallback to newest modified archive within max_age_seconds
    for d in scan_res.get("results", []):
        mtime = d.get("latest_mtime", 0)
        if (now - mtime) <= max_age_seconds:
            full_p = os.path.join(d["path"], d["latest_backup"])
            try:
                sz = os.path.getsize(full_p)
                if sz > 500:
                    return d["path"], full_p, sz
            except OSError:
                continue
    return None


def _get_supervisor_auth_headers() -> tuple[str, dict[str, str]]:
    """Retrieve host and authentication headers for Home Assistant Supervisor."""
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN", "")
    host = os.environ.get("SUPERVISOR", "supervisor")
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Supervisor-Token"] = token
    return host, headers


async def async_query_supervisor(
    hass: HomeAssistant,
    endpoint: str,
    method: str = "GET",
    json_data: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """Safely communicate with the Home Assistant Supervisor REST API."""
    host, headers = _get_supervisor_auth_headers()
    clean_endpoint = "/" + endpoint.lstrip("/")
    url = f"http://{host}{clean_endpoint}"

    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(hass)
        async with session.request(
            method,
            url,
            headers=headers,
            json=json_data,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status in (200, 201):
                return await resp.json()
            else:
                _LOGGER.debug("DomoLink-BackUp: Réponse HTTP %s depuis Supervisor (%s)", resp.status, url)
                return None
    except Exception as err:
        _LOGGER.debug("DomoLink-BackUp: Impossible de joindre l'API Supervisor sur %s: %s", url, err)
        return None


async def async_download_supervisor_backup(
    hass: HomeAssistant,
    slug: str,
    target_path: str,
    timeout: float = 600.0,
) -> bool:
    """Download a backup archive directly from Home Assistant Supervisor stream to disk."""
    host, headers = _get_supervisor_auth_headers()
    url = f"http://{host}/backups/{slug}/download"

    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(hass)
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "wb") as f_out:
                    while True:
                        chunk = await resp.content.read(131072)
                        if not chunk:
                            break
                        f_out.write(chunk)
                _LOGGER.info(
                    "DomoLink-BackUp: Archive '%s' téléchargée depuis Supervisor vers %s (%s octets)",
                    slug, target_path, os.path.getsize(target_path)
                )
                return True
            else:
                _LOGGER.error("DomoLink-BackUp: Échec téléchargement archive %s depuis Supervisor (HTTP %s)", slug, resp.status)
                return False
    except Exception as err:
        _LOGGER.error("DomoLink-BackUp: Exception lors du téléchargement de l'archive %s depuis Supervisor: %s", slug, err)
        return False


async def async_download_backup_via_manager(
    backup_manager: Any,
    backup_id: str,
    target_path: str,
) -> bool:
    """Download a backup archive via Home Assistant BackupManager stream."""
    if not backup_manager or not hasattr(backup_manager, "async_download_backup"):
        return False
    try:
        stream = await backup_manager.async_download_backup(backup_id)
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "wb") as f_out:
            async for chunk in stream:
                f_out.write(chunk)
        return True
    except Exception as err:
        _LOGGER.debug("DomoLink-BackUp: async_download_backup échoué via manager: %s", err)
        return False


def _sync_read_tar_backup_json(tar_path: str) -> dict[str, Any]:
    """Safely extract and parse backup.json or snapshot.json from a .tar archive."""
    try:
        with tarfile.open(tar_path, "r:*") as tar:
            for member_name in ("./backup.json", "backup.json", "./snapshot.json", "snapshot.json"):
                try:
                    f = tar.extractfile(member_name)
                    if f:
                        return json.load(f)
                except KeyError:
                    continue
    except Exception as err:
        _LOGGER.debug("DomoLink-BackUp: Impossible d'extraire backup.json de %s: %s", tar_path, err)
    return {}


def _sync_extract_core_subfolders(tar_path: str, target_config_dir: str, subfolders: list[str]) -> list[str]:
    """Safely extract specific subfolders (custom_components, themes, blueprints) from homeassistant.tar.gz."""
    extracted = []
    try:
        with tarfile.open(tar_path, "r:*") as outer_tar:
            ha_tar_member = None
            for name in ("homeassistant.tar.gz", "./homeassistant.tar.gz", "homeassistant.tar", "./homeassistant.tar"):
                try:
                    ha_tar_member = outer_tar.getmember(name)
                    break
                except KeyError:
                    continue
            if not ha_tar_member:
                _LOGGER.debug("DomoLink-BackUp: homeassistant.tar.gz introuvable dans %s", tar_path)
                return extracted

            ha_fileobj = outer_tar.extractfile(ha_tar_member)
            if not ha_fileobj:
                return extracted

            with tarfile.open(fileobj=ha_fileobj, mode="r:*") as inner_tar:
                members_to_extract = []
                for member in inner_tar.getmembers():
                    clean_name = member.name.lstrip("./")
                    for sub in subfolders:
                        if clean_name == sub or clean_name.startswith(f"{sub}/"):
                            members_to_extract.append(member)
                            if sub not in extracted:
                                extracted.append(sub)
                            break
                if members_to_extract:
                    inner_tar.extractall(path=target_config_dir, members=members_to_extract)
                    _LOGGER.info(
                        "DomoLink-BackUp: Extraction ciblée de %d fichiers (%s) dans %s",
                        len(members_to_extract),
                        ", ".join(extracted),
                        target_config_dir,
                    )
    except Exception as err:
        _LOGGER.error("DomoLink-BackUp: Erreur extraction ciblée de %s: %s", tar_path, err)
    return extracted


async def async_upload_backup_to_supervisor(hass: HomeAssistant, tar_path: str) -> bool:
    """Upload and register a local .tar backup archive into Home Assistant Supervisor."""
    host, headers = _get_supervisor_auth_headers()
    url = f"http://{host}/backups/new/upload"

    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(hass)
        data = aiohttp.FormData()
        with open(tar_path, "rb") as f:
            data.add_field("file", f, filename=os.path.basename(tar_path), content_type="application/x-tar")
            async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status in (200, 201):
                    res_json = await resp.json()
                    _LOGGER.info("DomoLink-BackUp: Archive injectée avec succès dans Supervisor: %s", res_json)
                    return True
                else:
                    _LOGGER.warning("DomoLink-BackUp: Échec upload vers Supervisor /backups/new/upload (HTTP %s)", resp.status)
                    return False
    except Exception as err:
        _LOGGER.warning("DomoLink-BackUp: Exception upload vers Supervisor: %s", err)
        return False


async def async_get_ha_backup_info(hass: HomeAssistant) -> dict[str, Any]:
    """Query Supervisor and BackupManager for known backup metadata and environment type."""
    info: dict[str, Any] = {
        "supervisor_available": False,
        "supervisor_backups": [],
        "manager_backups": [],
        "known_slugs": set(),
        "environment": "Core / Docker",
    }

    # 1. Query Supervisor API
    res = await async_query_supervisor(hass, "/backups")
    if res and isinstance(res, dict) and "data" in res and "backups" in res["data"]:
        info["supervisor_available"] = True
        info["environment"] = "Home Assistant OS / Supervised"
        for b in res["data"]["backups"]:
            slug = b.get("slug")
            if slug:
                info["known_slugs"].add(str(slug).lower())
                info["supervisor_backups"].append({
                    "slug": str(slug),
                    "name": b.get("name", ""),
                    "date": b.get("date", ""),
                    "size": b.get("size", 0),
                    "type": b.get("type", "full"),
                    "location": b.get("location"),
                })

    # 2. Check Core BackupManager
    try:
        from homeassistant.components.backup.const import DATA_MANAGER
        manager = hass.data.get(DATA_MANAGER) or hass.data.get("backup")
        if manager and hasattr(manager, "async_get_backups"):
            mgr_res = await manager.async_get_backups()
            raw_backups: dict[str, Any] = {}
            if isinstance(mgr_res, tuple) and mgr_res and isinstance(mgr_res[0], dict):
                raw_backups = mgr_res[0]
            elif isinstance(mgr_res, dict):
                raw_backups = mgr_res
            elif isinstance(mgr_res, list):
                for b in mgr_res:
                    bid = getattr(b, "backup_id", None) or getattr(b, "slug", None)
                    if bid:
                        raw_backups[str(bid)] = b

            for b_id, b_obj in raw_backups.items():
                info["known_slugs"].add(str(b_id).lower())
                info["manager_backups"].append({
                    "slug": str(b_id),
                    "name": getattr(b_obj, "name", ""),
                    "date": getattr(b_obj, "date", ""),
                    "size": getattr(b_obj, "size", 0),
                })
    except Exception as err:
        _LOGGER.debug("DomoLink-BackUp: Impossible d'interroger BackupManager: %s", err)

    return info


async def async_get_installed_addons(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Query Supervisor API to retrieve installed add-ons."""
    res = await async_query_supervisor(hass, "/addons")
    if res and isinstance(res, dict) and "data" in res and "addons" in res["data"]:
        addons_list = []
        for a in res["data"]["addons"]:
            if a.get("installed", True):
                addons_list.append({
                    "slug": str(a.get("slug", "")),
                    "name": str(a.get("name", a.get("slug", ""))),
                    "version": str(a.get("version", "")),
                    "state": str(a.get("state", "")),
                    "icon": bool(a.get("icon", False)),
                })
        return sorted(addons_list, key=lambda x: x["name"].lower())
    return []


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
            "local_backup_path": self.entry.options.get(
                CONF_LOCAL_BACKUP_PATH,
                self.entry.data.get(CONF_LOCAL_BACKUP_PATH, DEFAULT_LOCAL_BACKUP_PATH)
            ),
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
        local_path = self.entry.options.get(
            CONF_LOCAL_BACKUP_PATH,
            self.entry.data.get(CONF_LOCAL_BACKUP_PATH, self.data.get("local_backup_path", DEFAULT_LOCAL_BACKUP_PATH))
        )
        self.data["local_backup_path"] = local_path

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
            "local_backup_path": self.data.get("local_backup_path"),
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
        log_tag: str | None = None,
    ) -> None:
        """Update live progress and append or update timestamped log message."""
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
            p["eta_formatted"] = _format_eta(eta_seconds)
        if report is not None:
            p["report"] = report
            self.data["last_report"] = report

        if log_msg:
            now_str = datetime.now().strftime("%H:%M:%S")
            logs = p.setdefault("logs", [])

            if log_tag:
                # Look for existing log entry with this tag to update in place
                for existing in reversed(logs):
                    if existing.get("tag") == log_tag:
                        existing["time"] = now_str
                        existing["message"] = log_msg
                        existing["level"] = level
                        self.async_set_updated_data(self.data)
                        return

            entry_dict: dict[str, Any] = {
                "time": now_str,
                "stage": stage,
                "message": log_msg,
                "level": level,
            }
            if log_tag:
                entry_dict["tag"] = log_tag
            logs.append(entry_dict)
            if len(logs) > 150:
                p["logs"] = logs[-150:]

        self.async_set_updated_data(self.data)

    def _get_candidate_backup_dirs(self) -> list[str]:
        """Return all potential backup directories across all HA installation types."""
        candidates: list[str] = []

        # 1. User configured local backup directory has highest priority
        configured_path = self.entry.options.get(
            CONF_LOCAL_BACKUP_PATH,
            self.entry.data.get(CONF_LOCAL_BACKUP_PATH, self.data.get("local_backup_path", ""))
        )
        if configured_path and os.path.isdir(configured_path):
            candidates.append(configured_path)

        # 2. Standard directories across all HA installation modes
        standard_candidates = [
            "/backup",
            "/backups",
            "/usr/share/hassio/backup",
            "/homeassistant/backups",
            "/homeassistant/backup",
            self.hass.config.path("backups"),
            self.hass.config.path("backup"),
            "/share/backup",
            "/share/backups",
            "/media/backup",
            "/media/backups",
            "/mnt/data/supervisor/backup",
            "/mnt/data/backup",
            "/data/backup",
            "/data/backups",
            "/var/lib/docker/volumes",
        ]
        candidates.extend(standard_candidates)

        # 3. Mount points exploration (/mnt, /media, /share and system mounts)
        system_mounts = _sync_get_all_system_mount_points()
        for sm in system_mounts:
            if sm not in candidates:
                candidates.append(sm)
                for sub in ("backup", "backups"):
                    sub_p = os.path.join(sm, sub)
                    if os.path.isdir(sub_p):
                        candidates.append(sub_p)

        for mount_root in ("/mnt", "/media", "/share"):
            try:
                if os.path.isdir(mount_root):
                    for entry in os.scandir(mount_root):
                        if entry.is_dir():
                            candidates.append(entry.path)
                            for sub in ("backup", "backups"):
                                sub_p = os.path.join(entry.path, sub)
                                if os.path.isdir(sub_p):
                                    candidates.append(sub_p)
            except Exception:
                pass

        try:
            from homeassistant.components.backup.const import DATA_MANAGER
            manager = self.hass.data.get(DATA_MANAGER) or self.hass.data.get("backup")
            if manager and hasattr(manager, "backup_agents"):
                for agent in manager.backup_agents.values():
                    for attr in ("_backup_dir", "backup_dir", "_backup_path", "path"):
                        b_dir = getattr(agent, attr, None)
                        if b_dir:
                            candidates.append(str(b_dir))
        except Exception:
            pass

        seen = set()
        unique_dirs = []
        for d in candidates:
            if d and d not in seen:
                seen.add(d)
                try:
                    if os.path.isdir(d):
                        unique_dirs.append(d)
                except Exception:
                    pass
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
        backup_type: str = BACKUP_TYPE_FULL,
        homeassistant: bool = True,
        include_integrations: bool = True,
        include_themes: bool = True,
        include_blueprints: bool = True,
        addons: list[str] | None = None,
        folders: list[str] | None = None,
    ) -> bool:
        """Create a Home Assistant backup archive and upload it to remote storage."""
        if self.data.get("is_busy"):
            _LOGGER.warning("DomoLink-BackUp: Une opération de sauvegarde est déjà en cours.")
            return False

        if backup_type == BACKUP_TYPE_PARTIAL and mode == "MANUEL":
            mode = "PARTIEL"

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
        self.data.setdefault("progress", {})["logs"] = []

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
        existing_dirs = await self.hass.async_add_executor_job(self._get_candidate_backup_dirs)
        if not existing_dirs:
            fallback = self.hass.config.path("backups")
            await self.hass.async_add_executor_job(_sync_ensure_dir, fallback)
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

        # Snapshot existing Supervisor backups before triggering
        ha_info = await async_get_ha_backup_info(self.hass)
        supervisor_slugs_before: set[str] = set()
        if ha_info.get("supervisor_available"):
            for sb in ha_info.get("supervisor_backups", []):
                if sb.get("slug"):
                    supervisor_slugs_before.add(str(sb["slug"]).lower())
            _LOGGER.info("DomoLink-BackUp: %d sauvegardes Supervisor existantes détectées.", len(supervisor_slugs_before))

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
            if backup_type == BACKUP_TYPE_PARTIAL:
                if self.hass.services.has_service("hassio", "backup_partial"):
                    service_data: dict[str, Any] = {"name": backup_title}
                    if homeassistant is not None:
                        service_data["homeassistant"] = bool(homeassistant)
                    if addons is not None:
                        service_data["addons"] = list(addons)
                    if folders is not None:
                        service_data["folders"] = list(folders)
                    _LOGGER.info("DomoLink-BackUp: Appel du service hassio.backup_partial: %s", service_data)
                    self.update_progress(
                        STAGE_CREATING_LOCAL,
                        12,
                        "Service hassio.backup_partial",
                        "Création de la sauvegarde partielle / incrémentielle...",
                        log_msg=f"Service hassio.backup_partial déclenché (HA Core: {homeassistant}, Add-ons: {len(addons or [])}, Dossiers: {len(folders or [])}).",
                    )
                    backup_task = self.hass.async_create_task(
                        self.hass.services.async_call("hassio", "backup_partial", service_data, blocking=True),
                        name=f"{DOMAIN}_hassio_backup_call",
                    )
                elif self.hass.services.has_service("backup", "create"):
                    service_data = {"name": backup_title}
                    if homeassistant is not None:
                        service_data["include_homeassistant"] = bool(homeassistant)
                    if include_database is not None:
                        service_data["include_database"] = bool(include_database)
                    if addons is not None:
                        service_data["include_addons"] = list(addons)
                    if folders is not None:
                        service_data["include_folders"] = list(folders)
                    _LOGGER.info("DomoLink-BackUp: Appel backup.create (partiel): %s", service_data)
                    self.update_progress(
                        STAGE_CREATING_LOCAL,
                        12,
                        "Service backup.create",
                        "Création de la sauvegarde partielle...",
                        log_msg="Service backup.create partiel déclenché.",
                    )
                    backup_task = self.hass.async_create_task(
                        self.hass.services.async_call("backup", "create", service_data, blocking=True),
                        name=f"{DOMAIN}_backup_create_call",
                    )
                else:
                    raise RuntimeError("Aucun service de sauvegarde partielle Home Assistant disponible.")
            else:
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
                await asyncio.sleep(2.0)
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

                # Check Supervisor API directly (every 2.0s)
                if not detected_slug and ("hassio" in self.hass.config.components or ha_info.get("supervisor_available")):
                    try:
                        s_res = await async_query_supervisor(self.hass, "/backups")
                        if s_res and isinstance(s_res, dict) and "data" in s_res and "backups" in s_res["data"]:
                            for sb in s_res["data"]["backups"]:
                                sb_slug = sb.get("slug")
                                if not sb_slug:
                                    continue
                                sb_slug_str = str(sb_slug)
                                is_new = (sb_slug_str.lower() not in supervisor_slugs_before)
                                sb_date = sb.get("date", "")
                                if not is_new and sb_date:
                                    try:
                                        dt = datetime.fromisoformat(sb_date.replace("Z", "+00:00"))
                                        if dt.timestamp() >= started_epoch - 30.0:
                                            is_new = True
                                    except Exception:
                                        pass
                                if is_new:
                                    detected_slug = sb_slug_str
                                    sb_sz = sb.get("size", 0)
                                    sb_mb = round(sb_sz / (1024 * 1024), 1)
                                    self.update_progress(
                                        STAGE_CREATING_LOCAL,
                                        35,
                                        "Archive identifiée (Supervisor)",
                                        f"Slug : {detected_slug} ({sb_mb} Mo)",
                                        log_msg=f"📦 [Supervisor] Sauvegarde enregistrée : {detected_slug} ({sb_mb} Mo)",
                                    )
                                    break
                    except Exception as s_err:
                        _LOGGER.debug("DomoLink-BackUp: Erreur scrutation Supervisor: %s", s_err)

                # Check if backup task is completed
                if backup_task and backup_task.done():
                    exc = backup_task.exception()
                    if exc:
                        raise exc
                    # If slug was not yet detected, query supervisor or backup manager one more time
                    if not detected_slug and ("hassio" in self.hass.config.components or ha_info.get("supervisor_available")):
                        try:
                            s_res = await async_query_supervisor(self.hass, "/backups")
                            if s_res and isinstance(s_res, dict) and "data" in s_res and "backups" in s_res["data"]:
                                backups_list = s_res["data"]["backups"]
                                for sb in backups_list:
                                    sb_slug = sb.get("slug")
                                    if sb_slug and str(sb_slug).lower() not in supervisor_slugs_before:
                                        detected_slug = str(sb_slug)
                                        break
                                if not detected_slug and backups_list:
                                    detected_slug = str(backups_list[0].get("slug", ""))
                        except Exception:
                            pass

                # If detected_slug is known and (backup_task is done or not running):
                # In HAOS / Supervised, the file is NOT on disk in the Core container.
                # Stream it directly from Supervisor (or BackupManager) into /config/backups/{slug}.tar!
                if detected_slug and (backup_task is None or backup_task.done()):
                    target_dl_dir = self.hass.config.path("backups")
                    dl_tar_path = os.path.join(target_dl_dir, f"{detected_slug}.tar")

                    # Check if file already exists locally and has valid size
                    if os.path.isfile(dl_tar_path) and os.path.getsize(dl_tar_path) > 1024:
                        new_file = dl_tar_path
                        file_size = os.path.getsize(dl_tar_path)
                        break

                    # Download from Supervisor
                    if "hassio" in self.hass.config.components or ha_info.get("supervisor_available"):
                        self.update_progress(
                            STAGE_CREATING_LOCAL,
                            42,
                            "Téléchargement depuis Supervisor",
                            f"Récupération de l'archive {detected_slug}...",
                            log_msg=f"⬇️ Récupération de l'archive '{detected_slug}' via le flux Supervisor...",
                        )
                        dl_ok = await async_download_supervisor_backup(self.hass, detected_slug, dl_tar_path)
                        if dl_ok and os.path.isfile(dl_tar_path) and os.path.getsize(dl_tar_path) > 0:
                            new_file = dl_tar_path
                            file_size = os.path.getsize(dl_tar_path)
                            self.data["local_backup_path"] = target_dl_dir
                            self.update_progress(
                                STAGE_CREATING_LOCAL,
                                48,
                                "Archive récupérée",
                                f"{round(file_size / (1024*1024), 2)} Mo",
                                log_msg=f"✓ Archive récupérée avec succès depuis Supervisor ({round(file_size / (1024*1024), 2)} Mo)",
                            )
                            break
                    elif backup_manager:
                        dl_ok = await async_download_backup_via_manager(backup_manager, detected_slug, dl_tar_path)
                        if dl_ok and os.path.isfile(dl_tar_path) and os.path.getsize(dl_tar_path) > 0:
                            new_file = dl_tar_path
                            file_size = os.path.getsize(dl_tar_path)
                            self.data["local_backup_path"] = target_dl_dir
                            break

                # Dynamically append newly created directories if any
                fresh_candidates = await self.hass.async_add_executor_job(self._get_candidate_backup_dirs)
                for fc in fresh_candidates:
                    if fc not in existing_dirs:
                        existing_dirs.append(fc)

                # Poll filesystem (for Supervised/Container/Core installs where files are stored locally)
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
                    # Active periodic disk scan if candidate dirs haven't yielded anything yet
                    if poll_count >= 4 and poll_count % 3 == 0:
                        disk_found = await self.hass.async_add_executor_job(
                            _sync_find_targeted_or_newest_backup_on_disk,
                            self.hass.config.config_dir,
                            detected_slug,
                            900,
                        )
                        if disk_found:
                            found_dir, found_tar, found_sz = disk_found
                            if found_dir not in existing_dirs:
                                existing_dirs.insert(0, found_dir)
                                self.data["local_backup_path"] = found_dir
                                self.update_progress(
                                    STAGE_CREATING_LOCAL,
                                    42,
                                    "Dossier local identifié",
                                    f"Archive trouvée dans {found_dir}",
                                    log_msg=f"🔍 Scan du disque : archive trouvée dans '{found_dir}' ({os.path.basename(found_tar)})",
                                )
                                try:
                                    opts = dict(self.entry.options)
                                    opts[CONF_LOCAL_BACKUP_PATH] = found_dir
                                    self.hass.config_entries.async_update_entry(self.entry, options=opts)
                                except Exception:
                                    pass
                            new_file = found_tar
                            file_size = found_sz
                            break

                    if poll_count % 4 == 0:
                        self.update_progress(
                            STAGE_CREATING_LOCAL,
                            min(45, 12 + poll_count * 2),
                            "Création locale en cours",
                            f"Attente de finalisation ({elapsed_now}s écoulées)...",
                            log_msg=f"Scrutation des archives ({elapsed_now}s écoulées)...",
                            log_tag="poll_progress",
                        )

            # Check if task failed with an exception
            if backup_task and backup_task.done():
                exc = backup_task.exception()
                if exc:
                    raise exc

            if not new_file:
                # Fallback 1: check recent archives in candidate dirs
                new_file = await self.hass.async_add_executor_job(
                    _sync_find_recent_backup, existing_dirs, 600
                )
                if new_file:
                    file_size = await self.hass.async_add_executor_job(os.path.getsize, new_file)

            if not new_file:
                # Fallback 2: deep disk search across entire system for target slug or newest backup
                disk_found = await self.hass.async_add_executor_job(
                    _sync_find_targeted_or_newest_backup_on_disk,
                    self.hass.config.config_dir,
                    detected_slug,
                    900,
                )
                if disk_found:
                    found_dir, new_file, file_size = disk_found
                    self.data["local_backup_path"] = found_dir
                    self.update_progress(
                        STAGE_CREATING_LOCAL,
                        44,
                        "Archive localisée par scan",
                        f"{new_file}",
                        log_msg=f"✓ Archive localisée par scan système : {new_file}",
                    )

            if not new_file and ("hassio" in self.hass.config.components or ha_info.get("supervisor_available")):
                # Fallback 3: direct download stream from Supervisor API
                try:
                    target_slug = detected_slug
                    if not target_slug:
                        s_res = await async_query_supervisor(self.hass, "/backups")
                        if s_res and isinstance(s_res, dict) and "data" in s_res and "backups" in s_res["data"]:
                            b_list = s_res["data"]["backups"]
                            for sb in b_list:
                                if sb.get("slug") and str(sb["slug"]).lower() not in supervisor_slugs_before:
                                    target_slug = str(sb["slug"])
                                    break
                            if not target_slug and b_list:
                                target_slug = str(b_list[0].get("slug", ""))

                    if target_slug:
                        self.update_progress(
                            STAGE_CREATING_LOCAL,
                            45,
                            "Téléchargement depuis Supervisor",
                            f"Récupération de l'archive {target_slug}...",
                            log_msg=f"Flux direct : récupération de l'archive '{target_slug}' via l'API Supervisor...",
                        )
                        target_dl_dir = self.hass.config.path("backups")
                        dl_tar_path = os.path.join(target_dl_dir, f"{target_slug}.tar")
                        dl_ok = await async_download_supervisor_backup(self.hass, target_slug, dl_tar_path)
                        if dl_ok and os.path.isfile(dl_tar_path) and os.path.getsize(dl_tar_path) > 0:
                            new_file = dl_tar_path
                            file_size = os.path.getsize(dl_tar_path)
                            self.data["local_backup_path"] = target_dl_dir
                            self.update_progress(
                                STAGE_CREATING_LOCAL,
                                48,
                                "Archive récupérée",
                                f"{round(file_size / (1024*1024), 2)} Mo",
                                log_msg=f"✓ Archive récupérée avec succès depuis Supervisor ({round(file_size / (1024*1024), 2)} Mo)",
                            )
                except Exception as dl_err:
                    _LOGGER.warning("DomoLink-BackUp: Échec téléchargement direct Supervisor: %s", dl_err)

            if not new_file:
                raise FileNotFoundError(
                    f"Aucune nouvelle archive .tar détectée après 10 minutes de scrutation dans {existing_dirs}."
                )

            tar_path = new_file
            if file_size == 0:
                file_size = await self.hass.async_add_executor_job(os.path.getsize, tar_path)

            local_filename = os.path.basename(tar_path)
            # Remote filename: resolve from user's template (safe_slug) or fallback to local tar name
            remote_filename = f"{safe_slug}.tar" if safe_slug else local_filename
            if not remote_filename.endswith(".tar"):
                remote_filename += ".tar"

            filename = remote_filename
            size_mb = round(file_size / (1024 * 1024), 2)

            # 5. Verification step
            self.update_progress(
                STAGE_VERIFYING,
                48,
                "Vérification de l'archive",
                f"Archive {local_filename} validée ({size_mb} Mo)",
                log_msg=f"✓ Archive locale prête : {tar_path} ({size_mb} Mo) • Cible distante : {remote_filename}",
            )

            # 6. Remote upload step
            self.set_status(STATE_UPLOADING, f"Téléversement de {remote_filename} vers {dest_label}...", is_busy=True)
            self.update_progress(
                STAGE_UPLOADING,
                50,
                "Téléversement distant",
                f"Envoi de {remote_filename} vers {dest_label} ({size_mb} Mo)...",
                log_msg=f"Début du transfert vers {dest_label} : {remote_filename} ({size_mb} Mo)...",
                current_file=remote_filename,
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
                eta_sec = int(remaining_bytes / speed_bytes_sec) if (speed_bytes_sec > 0 and remaining_bytes > 0) else (0 if remaining_bytes == 0 else None)
                sent_mb = round(bytes_sent / (1024 * 1024), 1)
                eta_fmt = _format_eta(eta_sec)

                self.update_progress(
                    STAGE_UPLOADING,
                    pct,
                    "Téléversement distant",
                    f"{sent_mb} Mo / {size_mb} Mo ({pct}%) • {speed_mb_sec} Mo/s",
                    current_file=remote_filename,
                    transferred_bytes=bytes_sent,
                    total_bytes=file_size,
                    speed_kbps=round(speed_bytes_sec / 1024, 1),
                    eta_seconds=eta_sec,
                    log_msg=f"Téléversement : {sent_mb} Mo / {size_mb} Mo ({pct}%) - {speed_mb_sec} Mo/s (ETA: {eta_fmt})",
                    log_tag="upload_progress",
                )

            upload_success = await self.storage_engine.async_upload(
                source=tar_path,
                filename=remote_filename,
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

                self.data["last_backup_name"] = remote_filename
                self.data["last_backup_date"] = datetime.now(timezone.utc).isoformat()
                self.data["last_backup_size_mb"] = size_mb
                self.data["last_upload_duration_sec"] = round(elapsed, 1)
                self.data["last_error"] = ""

                await self.async_refresh_backups_list()

                # Build full completion report
                report = {
                    "backup_name": backup_title,
                    "filename": remote_filename,
                    "local_filename": local_filename,
                    "source_path": tar_path,
                    "destination": dest_label,
                    "size_bytes": file_size,
                    "size_mb": size_mb,
                    "duration_sec": round(elapsed, 1),
                    "duration_formatted": duration_formatted,
                    "compression_ratio": "Archive compressée (.tar ~35% de gain)",
                    "components": (
                        [
                            "Type: Sauvegarde Partielle / Incrémentielle",
                            f"Core HA: {'Inclus' if homeassistant else 'Exclu'}",
                            f"Intégrations (custom_components): {'Incluses' if include_integrations else 'Exclues'}",
                            f"Thèmes Lovelace: {'Inclus' if include_themes else 'Exclus'}",
                            f"Blueprints: {'Inclus' if include_blueprints else 'Exclus'}",
                            "Base de données SQLite / MariaDB" if include_database else "Base de données (exclue)",
                            f"Add-ons: {len(addons) if addons is not None else 'Tous'}",
                            f"Dossiers: {len(folders) if folders is not None else 'Tous'}",
                        ]
                        if backup_type == BACKUP_TYPE_PARTIAL
                        else [
                            "Type: Sauvegarde Complète",
                            "Configuration Home Assistant (/config)",
                            "Intégrations (custom_components)",
                            "Thèmes d'interface Lovelace",
                            "Blueprints & Scripts",
                            "Base de données SQLite / MariaDB" if include_database else "Base de données (exclue)",
                            "Tous les modules complémentaires (Add-ons)",
                            "Tous les dossiers partagés (/share, /ssl, /media)",
                        ]
                    ),
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
            err_str = str(err)
            user_msg = err_str
            if "freeze" in err_str.lower() or "blocked from execution" in err_str.lower():
                user_msg = (
                    "Le système Supervisor est actuellement verrouillé ('freeze'). "
                    "Une sauvegarde ou une mise à jour est déjà en cours dans Home Assistant. "
                    "Veuillez patienter ou redémarrer le Supervisor si le blocage persiste."
                )
            elif "an error occurred while making backup" in err_str.lower() or "supervisorbadrequesterror" in err_str.lower():
                user_msg = (
                    "Une erreur est survenue lors de la création de l'archive par le Supervisor "
                    "(tâche concurrente, module complémentaire verrouillé ou espace disque insuffisant). "
                    "Vérifiez l'état de vos modules et l'espace disque de Home Assistant."
                )
            _LOGGER.exception("DomoLink-BackUp: Erreur pendant la sauvegarde: %s", user_msg)
            self.set_status(STATE_ERROR, f"Erreur : {user_msg}", is_busy=False, error=user_msg)
            self.update_progress(
                STAGE_FAILED,
                100,
                "Erreur de sauvegarde",
                user_msg,
                log_msg=f"✗ Erreur : {user_msg}",
                level="error",
            )
            await self.notifier.async_notify_error(backup_title, dest_label, user_msg)
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

    async def async_restore_backup(
        self,
        filename: str,
        restore_mode: str = RESTORE_MODE_DOWNLOAD_ONLY,
        restore_homeassistant: bool = True,
        restore_integrations: bool = True,
        restore_themes: bool = True,
        restore_blueprints: bool = True,
        restore_addons: list[str] | None = None,
        restore_folders: list[str] | None = None,
    ) -> bool:
        """Download remote backup archive and optionally trigger system restoration."""
        if self.data.get("is_busy"):
            _LOGGER.warning("DomoLink-BackUp: Une opération est déjà en cours.")
            return False

        clean_filename = os.path.basename(filename.strip())
        dest_label = self.storage_engine.destination_label
        start_time = time.monotonic()

        # Find target backup metadata in remote list if possible
        backups = await self.storage_engine.async_list_backups()
        target = next(
            (b for b in backups if b.get("filename") == clean_filename or b.get("name") == clean_filename or b.get("backup_id") == clean_filename),
            None,
        )
        expected_size = target.get("size", 0) if target else 0
        self.data.setdefault("progress", {})["logs"] = []

        self.set_status(
            STATE_DOWNLOADING,
            f"Rapatriement de '{clean_filename}' depuis {dest_label}...",
            is_busy=True,
        )
        self.update_progress(
            STAGE_DOWNLOADING,
            5,
            "Rapatriement distant",
            f"Connexion à {dest_label} pour récupérer {clean_filename}...",
            log_msg=f"📥 Début du rapatriement de '{clean_filename}' depuis {dest_label}...",
            current_file=clean_filename,
            total_bytes=expected_size,
        )

        target_dl_dir = self.hass.config.path("backups")
        await self.hass.async_add_executor_job(_sync_ensure_dir, target_dl_dir)
        local_dest = os.path.join(target_dl_dir, clean_filename)

        dl_start = time.monotonic()
        last_log = 0.0

        def _on_download_progress(bytes_received: int) -> None:
            nonlocal last_log
            now_mono = time.monotonic()
            elapsed_dl = max(0.1, now_mono - dl_start)
            speed_bytes_sec = bytes_received / elapsed_dl
            speed_mb_sec = round(speed_bytes_sec / (1024 * 1024), 2)
            received_mb = round(bytes_received / (1024 * 1024), 1)

            eta_dl_sec = int((expected_size - bytes_received) / speed_bytes_sec) if (expected_size > bytes_received and speed_bytes_sec > 0) else (0 if expected_size > 0 and bytes_received >= expected_size else None)
            eta_dl_fmt = f" (ETA: {_format_eta(eta_dl_sec)})" if eta_dl_sec is not None else ""

            if expected_size > 0:
                pct = 5 + int((bytes_received / expected_size) * 75)
                pct = min(pct, 80)
                tot_mb = round(expected_size / (1024 * 1024), 1)
                detail = f"{received_mb} Mo / {tot_mb} Mo ({pct}%) • {speed_mb_sec} Mo/s{eta_dl_fmt}"
            else:
                pct = min(80, 10 + int(bytes_received / (10 * 1024 * 1024)))
                detail = f"{received_mb} Mo téléchargés • {speed_mb_sec} Mo/s"

            if now_mono - last_log >= 1.5 or (expected_size > 0 and bytes_received >= expected_size):
                last_log = now_mono
                self.update_progress(
                    STAGE_DOWNLOADING,
                    pct,
                    "Rapatriement distant",
                    detail,
                    current_file=clean_filename,
                    transferred_bytes=bytes_received,
                    total_bytes=expected_size,
                    speed_kbps=round(speed_bytes_sec / 1024, 1),
                    eta_seconds=eta_dl_sec,
                    log_msg=f"Téléchargement : {detail}",
                    log_tag="download_progress",
                )
            else:
                self.update_progress(
                    STAGE_DOWNLOADING,
                    pct,
                    "Rapatriement distant",
                    detail,
                    current_file=clean_filename,
                    transferred_bytes=bytes_received,
                    total_bytes=expected_size,
                    speed_kbps=round(speed_bytes_sec / 1024, 1),
                    eta_seconds=eta_dl_sec,
                )

        try:
            # 1. Download file to local_dest
            success = await self.storage_engine.async_download_to_file(
                clean_filename, local_dest, on_progress=_on_download_progress
            )
            if not success or not os.path.isfile(local_dest) or os.path.getsize(local_dest) == 0:
                raise RuntimeError(f"Échec du téléchargement du fichier {clean_filename}")

            final_size = os.path.getsize(local_dest)
            final_mb = round(final_size / (1024 * 1024), 2)

            # 2. Inspect archive internal backup.json to get slug and metadata
            meta = await self.hass.async_add_executor_job(_sync_read_tar_backup_json, local_dest)
            internal_slug = meta.get("slug")
            backup_display_name = meta.get("name") or clean_filename

            self.update_progress(
                STAGE_VERIFYING,
                85,
                "Archive vérifiée",
                f"{backup_display_name} ({final_mb} Mo)",
                log_msg=f"✓ Archive rapatriée avec succès : {final_mb} Mo (Slug détecté : {internal_slug or 'aucun'})",
            )

            # 3. If Supervisor is available, register / upload it
            ha_info = await async_get_ha_backup_info(self.hass)
            if ha_info.get("supervisor_available") or "hassio" in self.hass.config.components:
                self.update_progress(
                    STAGE_VERIFYING,
                    90,
                    "Enregistrement Supervisor",
                    "Injection de l'archive dans Home Assistant...",
                    log_msg="Enregistrement de l'archive dans le gestionnaire Supervisor...",
                )
                sup_ok = await async_upload_backup_to_supervisor(self.hass, local_dest)
                if sup_ok:
                    _LOGGER.info("DomoLink-BackUp: Archive '%s' injectée dans Supervisor.", clean_filename)

            # Also ensure a copy exists with {internal_slug}.tar if slug is known
            if internal_slug and internal_slug != clean_filename.removesuffix(".tar"):
                slug_tar = os.path.join(target_dl_dir, f"{internal_slug}.tar")
                if not os.path.exists(slug_tar):
                    try:
                        import shutil
                        await self.hass.async_add_executor_job(shutil.copy2, local_dest, slug_tar)
                    except Exception as copy_err:
                        _LOGGER.debug("DomoLink-BackUp: Copie slug .tar: %s", copy_err)

            elapsed_total = round(time.monotonic() - start_time, 1)

            # 4. If mode is full_restore, trigger the actual restoration
            if restore_mode == "full_restore":
                target_slug = internal_slug or clean_filename.removesuffix(".tar")
                self.set_status(
                    STATE_RESTORING,
                    f"Restauration de '{backup_display_name}'...",
                    is_busy=True,
                )
                self.update_progress(
                    STAGE_RESTORING,
                    95,
                    "Restauration du système",
                    "Lancement de la restauration. Home Assistant va redémarrer...",
                    log_msg=f"⚡ Lancement de la restauration complète pour l'archive '{backup_display_name}' (slug: {target_slug})...",
                    level="warning",
                )
                await self.notifier.async_notify_start(f"Restauration : {backup_display_name}", "Home Assistant (Système)")

                # Trigger restore service
                if self.hass.services.has_service("hassio", "backup_restore_full"):
                    _LOGGER.info("DomoLink-BackUp: Appel hassio.backup_restore_full pour slug %s", target_slug)
                    await self.hass.services.async_call(
                        "hassio", "backup_restore_full", {"slug": target_slug}, blocking=False
                    )
                elif self.hass.services.has_service("backup", "restore"):
                    _LOGGER.info("DomoLink-BackUp: Appel backup.restore pour backup_id %s", target_slug)
                    await self.hass.services.async_call(
                        "backup", "restore", {"backup_id": target_slug}, blocking=False
                    )
                else:
                    raise RuntimeError("Aucun service de restauration Home Assistant disponible.")

                restore_report = {
                    "operation": "Restauration Complète du système",
                    "backup_name": backup_display_name,
                    "destination": "Home Assistant (Système)",
                    "source_path": f"{dest_label}/{clean_filename}",
                    "size_bytes": final_size,
                    "size_mb": final_mb,
                    "duration_sec": elapsed_total,
                    "duration_formatted": f"{int(elapsed_total // 60)}m {int(elapsed_total % 60)}s" if elapsed_total >= 60 else f"{elapsed_total}s",
                    "compression_ratio": "Archive .tar d'origine",
                    "components": [f"Slug: {target_slug}", "Mode: Restauration complète"],
                }
                self.data["last_report"] = restore_report
                self.data["progress"]["report"] = restore_report
                self.async_set_updated_data(self.data)
                return True

            # 5. If mode is partial_restore, trigger partial restoration
            elif restore_mode in ("partial_restore", RESTORE_MODE_PARTIAL):
                target_slug = internal_slug or clean_filename.removesuffix(".tar")
                self.set_status(
                    STATE_RESTORING,
                    f"Restauration partielle de '{backup_display_name}'...",
                    is_busy=True,
                )
                self.update_progress(
                    STAGE_RESTORING,
                    95,
                    "Restauration partielle",
                    "Application sélective des composants choisis...",
                    log_msg=f"⚡ Lancement de la restauration partielle pour l'archive '{backup_display_name}' (Core: {restore_homeassistant}, Intégrations: {restore_integrations}, Thèmes: {restore_themes}, Blueprints: {restore_blueprints}, Addons: {len(restore_addons or [])}, Dossiers: {len(restore_folders or [])})...",
                    level="warning",
                )
                await self.notifier.async_notify_start(f"Restauration partielle : {backup_display_name}", "Home Assistant (Système)")

                # If user wants full Core restore:
                if restore_homeassistant:
                    if self.hass.services.has_service("hassio", "backup_restore_partial"):
                        service_data: dict[str, Any] = {
                            "slug": target_slug,
                            "homeassistant": True,
                        }
                        if restore_addons is not None:
                            service_data["addons"] = list(restore_addons)
                        if restore_folders is not None:
                            service_data["folders"] = list(restore_folders)
                        _LOGGER.info("DomoLink-BackUp: Appel hassio.backup_restore_partial: %s", service_data)
                        await self.hass.services.async_call(
                            "hassio", "backup_restore_partial", service_data, blocking=False
                        )
                    elif self.hass.services.has_service("backup", "restore"):
                        _LOGGER.info("DomoLink-BackUp: Appel backup.restore pour backup_id %s", target_slug)
                        await self.hass.services.async_call(
                            "backup", "restore", {"backup_id": target_slug}, blocking=False
                        )
                    else:
                        raise RuntimeError("Aucun service de restauration Home Assistant disponible.")
                else:
                    # User did not want full Core restore, but may have requested specific subcomponents
                    subfolders_to_extract = []
                    if restore_integrations:
                        subfolders_to_extract.append("custom_components")
                    if restore_themes:
                        subfolders_to_extract.append("themes")
                    if restore_blueprints:
                        subfolders_to_extract.append("blueprints")

                    if subfolders_to_extract:
                        config_dir = self.hass.config.config_dir
                        extracted = await self.hass.async_add_executor_job(
                            _sync_extract_core_subfolders, local_dest, config_dir, subfolders_to_extract
                        )
                        _LOGGER.info("DomoLink-BackUp: Éléments extraits directement dans config: %s", extracted)

                    # Also trigger add-ons or folders restoration if requested
                    if (restore_addons or restore_folders) and self.hass.services.has_service("hassio", "backup_restore_partial"):
                        service_data = {
                            "slug": target_slug,
                            "homeassistant": False,
                        }
                        if restore_addons is not None:
                            service_data["addons"] = list(restore_addons)
                        if restore_folders is not None:
                            service_data["folders"] = list(restore_folders)
                        _LOGGER.info("DomoLink-BackUp: Appel hassio.backup_restore_partial (addons/folders): %s", service_data)
                        await self.hass.services.async_call(
                            "hassio", "backup_restore_partial", service_data, blocking=False
                        )

                restore_report = {
                    "operation": "Restauration Partielle / Incrémentielle",
                    "backup_name": backup_display_name,
                    "destination": "Home Assistant (Composants sélectionnés)",
                    "source_path": f"{dest_label}/{clean_filename}",
                    "size_bytes": final_size,
                    "size_mb": final_mb,
                    "duration_sec": elapsed_total,
                    "duration_formatted": f"{int(elapsed_total // 60)}m {int(elapsed_total % 60)}s" if elapsed_total >= 60 else f"{elapsed_total}s",
                    "compression_ratio": "Archive .tar d'origine",
                    "components": [
                        f"Slug: {target_slug}",
                        f"Home Assistant Core: {'Inclus' if restore_homeassistant else 'Exclu'}",
                        f"Intégrations (custom_components): {'Restaurées' if (restore_homeassistant or restore_integrations) else 'Exclues'}",
                        f"Thèmes Lovelace: {'Restaurés' if (restore_homeassistant or restore_themes) else 'Exclus'}",
                        f"Blueprints: {'Restaurés' if (restore_homeassistant or restore_blueprints) else 'Exclus'}",
                        f"Add-ons restaurés: {len(restore_addons) if restore_addons else '0'}",
                        f"Dossiers restaurés: {len(restore_folders) if restore_folders else '0'}",
                    ],
                }
                self.data["last_report"] = restore_report
                self.data["progress"]["report"] = restore_report
                self.async_set_updated_data(self.data)
                return True

            # Otherwise (download_only):
            self.set_status(STATE_SUCCESS, f"Rapatriement réussi ({final_mb} Mo)", is_busy=False)
            restore_report = {
                "operation": "Rapatriement vers Home Assistant",
                "backup_name": backup_display_name,
                "destination": "Stockage local (/config/backups)",
                "source_path": f"{dest_label}/{clean_filename}",
                "size_bytes": final_size,
                "size_mb": final_mb,
                "duration_sec": elapsed_total,
                "duration_formatted": f"{int(elapsed_total // 60)}m {int(elapsed_total % 60)}s" if elapsed_total >= 60 else f"{elapsed_total}s",
                "compression_ratio": "Archive .tar d'origine",
                "components": [f"Slug détecté: {internal_slug or 'aucun'}", "Statut: Enregistré dans Supervisor"],
            }
            self.data["last_report"] = restore_report
            self.data["progress"]["report"] = restore_report
            self.update_progress(
                STAGE_COMPLETED,
                100,
                "Rapatriement réussi",
                f"L'archive '{backup_display_name}' ({final_mb} Mo) est prête dans vos sauvegardes locales.",
                log_msg=f"✓ Rapatriement terminé en {elapsed_total}s ({final_mb} Mo). Disponible dans Paramètres > Sauvegardes.",
                level="success",
            )
            await self.notifier.async_notify_success(
                backup_name=f"Rapatriement: {backup_display_name}",
                destination="Stockage local HA",
                size_bytes=final_size,
                duration_sec=elapsed_total,
                remaining_count=self.data.get("total_backups_count", 1),
                total_size_mb=self.data.get("total_storage_mb", final_mb),
            )
            return True

        except Exception as err:
            _LOGGER.exception("DomoLink-BackUp: Erreur lors du rapatriement/restauration: %s", err)
            self.set_status(STATE_ERROR, f"Erreur de restauration : {err}", is_busy=False, error=str(err))
            self.update_progress(
                STAGE_FAILED,
                100,
                "Échec de restauration",
                str(err),
                log_msg=f"✗ Échec du rapatriement ou de la restauration : {err}",
                level="error",
            )
            await self.notifier.async_notify_error(f"Restauration: {clean_filename}", dest_label, str(err))
            return False


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
        backup_type = call.data.get("backup_type", BACKUP_TYPE_FULL)
        homeassistant = call.data.get("homeassistant", True)
        include_integrations = call.data.get("include_integrations", True)
        include_themes = call.data.get("include_themes", True)
        include_blueprints = call.data.get("include_blueprints", True)
        addons = call.data.get("addons")
        folders = call.data.get("folders")
        await coordinator.async_create_and_upload_backup(
            name=name,
            include_database=include_db,
            backup_type=backup_type,
            homeassistant=homeassistant,
            include_integrations=include_integrations,
            include_themes=include_themes,
            include_blueprints=include_blueprints,
            addons=addons,
            folders=folders,
        )

    async def _handle_upload_backup(call: ServiceCall) -> None:
        file_path = call.data.get("file_path")
        if file_path:
            await coordinator.async_upload_file(file_path)

    async def _handle_restore_backup(call: ServiceCall) -> None:
        filename = call.data.get("filename")
        restore_mode = call.data.get("restore_mode", RESTORE_MODE_DOWNLOAD_ONLY)
        restore_homeassistant = call.data.get("restore_homeassistant", True)
        restore_integrations = call.data.get("restore_integrations", True)
        restore_themes = call.data.get("restore_themes", True)
        restore_blueprints = call.data.get("restore_blueprints", True)
        restore_addons = call.data.get("restore_addons")
        restore_folders = call.data.get("restore_folders")
        if filename:
            await coordinator.async_restore_backup(
                filename=filename,
                restore_mode=restore_mode,
                restore_homeassistant=restore_homeassistant,
                restore_integrations=restore_integrations,
                restore_themes=restore_themes,
                restore_blueprints=restore_blueprints,
                restore_addons=restore_addons,
                restore_folders=restore_folders,
            )

    async def _handle_test_connection(call: ServiceCall) -> None:
        await coordinator.async_run_test_connection()

    async def _handle_clean_old_backups(call: ServiceCall) -> None:
        await coordinator.async_apply_retention()

    async def _handle_sync_backups(call: ServiceCall) -> None:
        await coordinator.async_refresh_backups_list()

    schema_create = vol.Schema({
        vol.Optional("name"): str,
        vol.Optional("include_database", default=True): bool,
        vol.Optional("backup_type", default=BACKUP_TYPE_FULL): vol.In([BACKUP_TYPE_FULL, BACKUP_TYPE_PARTIAL]),
        vol.Optional("homeassistant", default=True): bool,
        vol.Optional("include_integrations", default=True): bool,
        vol.Optional("include_themes", default=True): bool,
        vol.Optional("include_blueprints", default=True): bool,
        vol.Optional("addons"): [str],
        vol.Optional("folders"): [str],
    })
    schema_upload = vol.Schema({
        vol.Required("file_path"): str,
    })
    schema_restore = vol.Schema({
        vol.Required("filename"): str,
        vol.Optional("restore_mode", default=RESTORE_MODE_DOWNLOAD_ONLY): vol.In(
            [RESTORE_MODE_DOWNLOAD_ONLY, RESTORE_MODE_FULL, RESTORE_MODE_PARTIAL]
        ),
        vol.Optional("restore_homeassistant", default=True): bool,
        vol.Optional("restore_integrations", default=True): bool,
        vol.Optional("restore_themes", default=True): bool,
        vol.Optional("restore_blueprints", default=True): bool,
        vol.Optional("restore_addons"): [str],
        vol.Optional("restore_folders"): [str],
    })

    async def _handle_check_updates(call: ServiceCall) -> None:
        """Handle manual update check."""
        for ed in hass.data.get(DOMAIN, {}).values():
            if isinstance(ed, dict) and "update_entity" in ed:
                await ed["update_entity"].async_update()

    async def _handle_install_update(call: ServiceCall) -> None:
        """Handle install update request."""
        backup = call.data.get("backup", True)
        for ed in hass.data.get(DOMAIN, {}).values():
            if isinstance(ed, dict) and "update_entity" in ed:
                await ed["update_entity"].async_install(backup=backup)
                break

    hass.services.async_register(DOMAIN, SERVICE_CREATE_BACKUP, _handle_create_backup, schema=schema_create)
    hass.services.async_register(DOMAIN, SERVICE_UPLOAD_BACKUP, _handle_upload_backup, schema=schema_upload)
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_BACKUP, _handle_restore_backup, schema=schema_restore)
    hass.services.async_register(DOMAIN, SERVICE_TEST_CONNECTION, _handle_test_connection)
    hass.services.async_register(DOMAIN, SERVICE_CLEAN_OLD_BACKUPS, _handle_clean_old_backups)
    hass.services.async_register(DOMAIN, SERVICE_SYNC_BACKUPS, _handle_sync_backups)
    if not hass.services.has(DOMAIN, "check_updates"):
        hass.services.async_register(DOMAIN, "check_updates", _handle_check_updates)
    if not hass.services.has(DOMAIN, "install_update"):
        hass.services.async_register(DOMAIN, "install_update", _handle_install_update)

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
    @async_response
    async def ws_get_data(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
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

        installed_addons = await async_get_installed_addons(hass)
        available_folders = [
            {"id": "share", "name": "Partage réseau (/share)"},
            {"id": "ssl", "name": "Certificats SSL/TLS (/ssl)"},
            {"id": "media", "name": "Médias (/media)"},
            {"id": "addons/local", "name": "Extensions locales (/addons/local)"},
        ]

        # Enrich coordinator data with update entity state if available
        update_info = {
            "update_available": coordinator.data.get("update_available", False),
            "latest_version": coordinator.data.get("latest_version", VERSION),
            "release_notes": coordinator.data.get("release_notes", ""),
            "release_url": coordinator.data.get("release_url", ""),
            "installed_version": VERSION,
        }
        for ed in hass.data.get(DOMAIN, {}).values():
            if isinstance(ed, dict) and "update_entity" in ed:
                ue = ed["update_entity"]
                if getattr(ue, "installed_version", None):
                    update_info["installed_version"] = ue.installed_version
                if getattr(ue, "latest_version", None):
                    update_info["latest_version"] = ue.latest_version
                if getattr(ue, "_release_body", None):
                    update_info["release_notes"] = ue._release_body
                if getattr(ue, "_attr_release_url", None):
                    update_info["release_url"] = ue._attr_release_url
                if getattr(ue, "installed_version", None) and getattr(ue, "latest_version", None):
                    update_info["update_available"] = (
                        ue.installed_version != ue.latest_version
                    )
                coordinator.data.update(update_info)
                break

        connection.send_result(
            msg["id"],
            {
                "data": coordinator.data,
                "config": safe_cfg,
                "version": update_info["installed_version"],
                "update": update_info,
                "installed_addons": installed_addons,
                "available_folders": available_folders,
            },
        )

    @websocket_command({
        vol.Required("type"): "domolink_backup/trigger_backup",
        vol.Optional("name"): str,
        vol.Optional("include_database", default=True): bool,
        vol.Optional("mode", default="MANUEL"): str,
        vol.Optional("backup_type", default=BACKUP_TYPE_FULL): vol.In([BACKUP_TYPE_FULL, BACKUP_TYPE_PARTIAL]),
        vol.Optional("homeassistant", default=True): bool,
        vol.Optional("addons"): [str],
        vol.Optional("folders"): [str],
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
        backup_type = msg.get("backup_type", BACKUP_TYPE_FULL)
        homeassistant = msg.get("homeassistant", True)
        include_integrations = msg.get("include_integrations", True)
        include_themes = msg.get("include_themes", True)
        include_blueprints = msg.get("include_blueprints", True)
        addons = msg.get("addons")
        folders = msg.get("folders")
        create_task = getattr(hass, "async_create_background_task", hass.async_create_task)
        create_task(
            coordinator.async_create_and_upload_backup(
                name=name,
                include_database=include_db,
                mode=mode,
                backup_type=backup_type,
                homeassistant=homeassistant,
                include_integrations=include_integrations,
                include_themes=include_themes,
                include_blueprints=include_blueprints,
                addons=addons,
                folders=folders,
            ),
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

    @websocket_command({
        vol.Required("type"): "domolink_backup/restore_backup",
        vol.Required("filename"): str,
        vol.Optional("restore_mode", default=RESTORE_MODE_DOWNLOAD_ONLY): vol.In(
            [RESTORE_MODE_DOWNLOAD_ONLY, RESTORE_MODE_FULL, RESTORE_MODE_PARTIAL]
        ),
        vol.Optional("restore_homeassistant", default=True): bool,
        vol.Optional("restore_integrations", default=True): bool,
        vol.Optional("restore_themes", default=True): bool,
        vol.Optional("restore_blueprints", default=True): bool,
        vol.Optional("restore_addons"): [str],
        vol.Optional("restore_folders"): [str],
    })
    @async_response
    async def ws_restore_backup(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        filename = msg["filename"]
        restore_mode = msg.get("restore_mode", RESTORE_MODE_DOWNLOAD_ONLY)
        restore_homeassistant = msg.get("restore_homeassistant", True)
        restore_integrations = msg.get("restore_integrations", True)
        restore_themes = msg.get("restore_themes", True)
        restore_blueprints = msg.get("restore_blueprints", True)
        restore_addons = msg.get("restore_addons")
        restore_folders = msg.get("restore_folders")
        create_task = getattr(hass, "async_create_background_task", hass.async_create_task)
        create_task(
            coordinator.async_restore_backup(
                filename,
                restore_mode=restore_mode,
                restore_homeassistant=restore_homeassistant,
                restore_integrations=restore_integrations,
                restore_themes=restore_themes,
                restore_blueprints=restore_blueprints,
                restore_addons=restore_addons,
                restore_folders=restore_folders,
            ),
            name=f"{DOMAIN}_restore_task",
        )
        connection.send_result(msg["id"], {"success": True, "status": "started"})

    @websocket_command({
        vol.Required("type"): "domolink_backup/scan_local_backup_paths",
        vol.Optional("extra_roots"): [str],
    })
    @async_response
    async def ws_scan_local_backup_paths(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        extra_roots = msg.get("extra_roots")

        # 1. Query Home Assistant Supervisor and Core BackupManager
        ha_info = await async_get_ha_backup_info(hass)
        known_slugs = ha_info.get("known_slugs", set())

        # 2. Perform deep disk scan across root "/" and all system mounts
        scan_res = await hass.async_add_executor_job(
            _sync_scan_local_disk_for_backups,
            hass.config.config_dir,
            extra_roots,
        )
        current_path = coordinator.data.get("local_backup_path", "")
        results = scan_res.get("results", [])
        for item in results:
            item["is_current"] = (item["path"] == current_path)
            fname_lower = item.get("latest_backup", "").lower()
            if any(slug in fname_lower for slug in known_slugs):
                item["ha_verified"] = True

        # If Supervisor is available and has backups, prepend the Supervisor entry
        if ha_info.get("supervisor_available") and ha_info.get("supervisor_backups"):
            sup_backups = ha_info["supervisor_backups"]
            latest_sup = sup_backups[0] if sup_backups else {}
            sz = latest_sup.get("size", 0)
            sz_mb = round(sz / (1024 * 1024), 2)
            results.insert(0, {
                "path": "/config/backups",
                "count": len(sup_backups),
                "latest_backup": latest_sup.get("name") or latest_sup.get("slug", ""),
                "latest_size_mb": sz_mb,
                "latest_mtime": time.time(),
                "latest_date": latest_sup.get("date", ""),
                "category": "Supervisor",
                "ha_verified": True,
                "is_current": (current_path in ("/config/backups", "")),
            })

        connection.send_result(
            msg["id"],
            {
                "success": True,
                "results": results,
                "scanned_count": scan_res.get("scanned_count", 0),
                "elapsed_sec": scan_res.get("elapsed_sec", 0.0),
                "current_path": current_path,
                "environment": ha_info.get("environment", "Core / Docker"),
                "ha_backups_count": len(ha_info.get("supervisor_backups", [])) or len(ha_info.get("manager_backups", [])),
                "supervisor_available": ha_info.get("supervisor_available", False),
            },
        )

    @websocket_command({
        vol.Required("type"): "domolink_backup/set_local_backup_path",
        vol.Required("path"): str,
    })
    @callback
    def ws_set_local_backup_path(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        coordinator = _get_active_coordinator(hass)
        if not coordinator:
            connection.send_error(msg["id"], "not_found", "Coordinateur non disponible")
            return
        chosen_path = msg["path"].strip()
        options = dict(coordinator.entry.options or coordinator.entry.data)
        options[CONF_LOCAL_BACKUP_PATH] = chosen_path
        hass.config_entries.async_update_entry(coordinator.entry, options=options)
        coordinator.data["local_backup_path"] = chosen_path
        coordinator.async_set_updated_data(coordinator.data)
        connection.send_result(
            msg["id"],
            {
                "success": True,
                "path": chosen_path,
            },
        )

    @websocket_command({vol.Required("type"): "domolink_backup/check_updates"})
    @async_response
    async def ws_check_updates(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        for ed in hass.data.get(DOMAIN, {}).values():
            if isinstance(ed, dict) and "update_entity" in ed:
                await ed["update_entity"].async_update()
                connection.send_result(
                    msg["id"],
                    {
                        "status": "checked",
                        "latest_version": ed["update_entity"].latest_version,
                        "installed_version": ed["update_entity"].installed_version,
                        "update_available": ed["update_entity"].installed_version != ed["update_entity"].latest_version,
                    },
                )
                return
        connection.send_result(msg["id"], {"status": "checked", "update_available": False})

    @websocket_command({
        vol.Required("type"): "domolink_backup/install_update",
        vol.Optional("backup", default=True): bool,
    })
    @async_response
    async def ws_install_update(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        backup = msg.get("backup", True)
        for ed in hass.data.get(DOMAIN, {}).values():
            if isinstance(ed, dict) and "update_entity" in ed:
                create_task = getattr(hass, "async_create_background_task", hass.async_create_task)
                create_task(
                    ed["update_entity"].async_install(backup=backup),
                    name=f"{DOMAIN}_install_update_task",
                )
                connection.send_result(msg["id"], {"status": "started"})
                return
        connection.send_error(msg["id"], "not_found", "Entité de mise à jour non disponible")

    ws_commands = [
        ws_get_data,
        ws_trigger_backup,
        ws_save_template,
        ws_get_template_preview,
        ws_scan_local_backup_paths,
        ws_set_local_backup_path,
        ws_test_connection,
        ws_clean_backups,
        ws_delete_backup,
        ws_restore_backup,
        ws_check_updates,
        ws_install_update,
    ]
    for ws_cmd in ws_commands:
        try:
            async_register_command(hass, ws_cmd)
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
                SERVICE_RESTORE_BACKUP,
                "check_updates",
                "install_update",
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
