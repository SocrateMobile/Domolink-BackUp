"""Constants for the DomoLink-BackUp integration."""
from __future__ import annotations

DOMAIN = "domolink_backup"
NAME = "DomoLink-BackUp"
DEFAULT_NAME = "DomoLink-BackUp"
VERSION = "1.1.1"

# Storage key for persistent state
STORAGE_KEY = "domolink_backup_data"
STORAGE_VERSION = 1

# Configuration Keys - General & Destination
CONF_DESTINATION_TYPE = "destination_type"
CONF_NAS_TYPE = "nas_type"
CONF_PROTOCOL = "protocol"

# Protocol types
PROTO_FTP = "ftp"
PROTO_FTPS = "ftps"
PROTO_WEBDAV = "webdav"
PROTO_GOOGLE_DRIVE = "google_drive"
PROTO_LOCAL_SHARE = "local_share"

PROTOCOLS = [
    PROTO_FTP,
    PROTO_FTPS,
    PROTO_WEBDAV,
    PROTO_GOOGLE_DRIVE,
    PROTO_LOCAL_SHARE,
]

# NAS Types
NAS_SYNOLOGY = "synology"
NAS_QNAP = "qnap"
NAS_ASUSTOR = "asustor"
NAS_TRUENAS = "truenas"
NAS_FREEBOX = "freebox"
NAS_UNRAID = "unraid"
NAS_GENERIC = "generic"

NAS_TYPES = [
    NAS_SYNOLOGY,
    NAS_QNAP,
    NAS_ASUSTOR,
    NAS_TRUENAS,
    NAS_FREEBOX,
    NAS_UNRAID,
    NAS_GENERIC,
]

# NAS Default Configs & Presets
DEFAULT_NAS_CONFIGS = {
    NAS_SYNOLOGY: {
        "name": "Synology (DSM)",
        "default_protocol": PROTO_FTP,
        "ftp_port": 21,
        "ftp_path": "/volume1/backups/domolink",
        "webdav_port": 5006,
        "webdav_path": "backups/domolink",
        "webdav_ssl": True,
    },
    NAS_QNAP: {
        "name": "QNAP (QTS)",
        "default_protocol": PROTO_FTP,
        "ftp_port": 21,
        "ftp_path": "/Public/domolink_backups",
        "webdav_port": 5001,
        "webdav_path": "Public/domolink_backups",
        "webdav_ssl": True,
    },
    NAS_ASUSTOR: {
        "name": "ASUSTOR (ADM)",
        "default_protocol": PROTO_FTP,
        "ftp_port": 21,
        "ftp_path": "/domolink_backups",
        "webdav_port": 8001,
        "webdav_path": "domolink_backups",
        "webdav_ssl": False,
    },
    NAS_TRUENAS: {
        "name": "TrueNAS (SCALE/CORE)",
        "default_protocol": PROTO_WEBDAV,
        "ftp_port": 21,
        "ftp_path": "/mnt/pool/backups/domolink",
        "webdav_port": 443,
        "webdav_path": "backups/domolink",
        "webdav_ssl": True,
    },
    NAS_FREEBOX: {
        "name": "Freebox (Delta / Ultra / Pop)",
        "default_protocol": PROTO_FTP,
        "ftp_host": "mafreebox.freebox.fr",
        "ftp_port": 21,
        "ftp_user": "freebox",
        "ftp_path": "/Disque 1/Sauvegardes/HomeAssistant",
        "webdav_port": 80,
        "webdav_path": "Disque 1/Sauvegardes",
        "webdav_ssl": False,
    },
    NAS_UNRAID: {
        "name": "Unraid",
        "default_protocol": PROTO_FTP,
        "ftp_port": 21,
        "ftp_path": "/mnt/user/backups/homeassistant",
        "webdav_port": 80,
        "webdav_path": "backups/homeassistant",
        "webdav_ssl": False,
    },
    NAS_GENERIC: {
        "name": "Autre NAS / Serveur personnalisé",
        "default_protocol": PROTO_FTP,
        "ftp_port": 21,
        "ftp_path": "/backups/homeassistant",
        "webdav_port": 5005,
        "webdav_path": "backups/homeassistant",
        "webdav_ssl": False,
    },
}

# Configuration Keys - FTP / FTPS / SFTP
CONF_FTP_ENABLED = "ftp_enabled"
CONF_FTP_HOST = "ftp_host"
CONF_FTP_PORT = "ftp_port"
CONF_FTP_USER = "ftp_user"
CONF_FTP_PASS = "ftp_pass"
CONF_FTP_PATH = "ftp_path"
CONF_FTP_TLS = "ftp_tls"

DEFAULT_FTP_PORT = 21
DEFAULT_FTP_PATH = "/domolink_backups"

# Configuration Keys - WebDAV
CONF_WEBDAV_ENABLED = "webdav_enabled"
CONF_WEBDAV_URL = "webdav_url"
CONF_WEBDAV_USER = "webdav_user"
CONF_WEBDAV_PASS = "webdav_pass"
CONF_WEBDAV_PATH = "webdav_path"
CONF_WEBDAV_VERIFY_SSL = "webdav_verify_ssl"

DEFAULT_WEBDAV_PATH = "domolink_backups"

# Configuration Keys - Google Drive
CONF_GOOGLE_DRIVE_ENABLED = "google_drive_enabled"
CONF_GOOGLE_DRIVE_WEBHOOK_URL = "google_drive_webhook_url"
CONF_GOOGLE_DRIVE_FOLDER_ID = "google_drive_folder_id"
CONF_GOOGLE_DRIVE_SECRET_KEY = "google_drive_secret_key"

# Configuration Keys - Local / Mount Share
CONF_LOCAL_SHARE_PATH = "local_share_path"
DEFAULT_LOCAL_SHARE_PATH = "/share/domolink_backups"

# Configuration Keys - Retention Policy
CONF_RETENTION_DAYS = "retention_days"
CONF_MAX_BACKUPS_COUNT = "max_backups_count"
CONF_MAX_STORAGE_MB = "max_storage_mb"
CONF_AUTO_CLEAN_ENABLED = "auto_clean_enabled"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_BACKUPS_COUNT = 7
DEFAULT_MAX_STORAGE_MB = 10240  # 10 GB
DEFAULT_AUTO_CLEAN_ENABLED = True

# Configuration Keys - Telegram Notifications
CONF_TELEGRAM_ENABLED = "telegram_enabled"
CONF_TELEGRAM_TOKEN = "telegram_token"
CONF_TELEGRAM_CHAT_ID = "telegram_chat_id"
CONF_TELEGRAM_NOTIFY_ON_START = "telegram_notify_on_start"
CONF_TELEGRAM_NOTIFY_ON_SUCCESS = "telegram_notify_on_success"
CONF_TELEGRAM_NOTIFY_ON_ERROR = "telegram_notify_on_error"

DEFAULT_TELEGRAM_ENABLED = False
DEFAULT_TELEGRAM_NOTIFY_ON_START = False
DEFAULT_TELEGRAM_NOTIFY_ON_SUCCESS = True
DEFAULT_TELEGRAM_NOTIFY_ON_ERROR = True

# Configuration Keys - Auto Backup Schedule
CONF_SCHEDULE_ENABLED = "schedule_enabled"
CONF_SCHEDULE_FREQUENCY = "schedule_frequency"  # daily, weekly, custom
CONF_SCHEDULE_TIME = "schedule_time"  # HH:MM
CONF_SCHEDULE_DAYS = "schedule_days"  # List of weekdays (0=Monday, 6=Sunday)

DEFAULT_SCHEDULE_ENABLED = False
DEFAULT_SCHEDULE_FREQUENCY = "daily"
DEFAULT_SCHEDULE_TIME = "03:00"

# Status States
STATE_IDLE = "Prêt"
STATE_BACKING_UP = "Sauvegarde en cours"
STATE_UPLOADING = "Téléversement en cours"
STATE_CLEANING = "Nettoyage en cours"
STATE_TESTING = "Test en cours"
STATE_SUCCESS = "Succès"
STATE_ERROR = "Erreur"

# Service Names
SERVICE_CREATE_BACKUP = "create_backup"
SERVICE_UPLOAD_BACKUP = "upload_backup"
SERVICE_TEST_CONNECTION = "test_connection"
SERVICE_CLEAN_OLD_BACKUPS = "clean_old_backups"
SERVICE_SYNC_BACKUPS = "sync_backups"

# Google Apps Script Source Template
GOOGLE_APPS_SCRIPT_TEMPLATE = """/**
 * Script Google Apps Script pour DomoLink-BackUp
 * Déployer en tant qu'Application Web :
 * - Exécuter en tant que : Moi (votre adresse Gmail)
 * - Qui a accès : Tout le monde (Anyone)
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput(JSON.stringify({
        success: false,
        code: 400,
        message: "Corps de requête vide."
      })).setMimeType(ContentService.MimeType.JSON);
    }

    var data = JSON.parse(e.postData.contents);

    // 1. Sonde de test diagnostic DomoLink-BackUp
    if (data.probe === true) {
      return ContentService.createTextOutput(JSON.stringify({
        success: true,
        code: 200,
        message: "Connexion Google Drive validée avec succès.",
        folder_name: "DomoLink-BackUp"
      })).setMimeType(ContentService.MimeType.JSON);
    }

    // 2. Dossier cible
    var folderId = data.folder_id || "";
    var folder;
    if (folderId && folderId.trim() !== "") {
      try {
        folder = DriveApp.getFolderById(folderId.trim());
      } catch (fErr) {
        folder = null;
      }
    }
    if (!folder) {
      var folders = DriveApp.getFoldersByName("DomoLink-BackUp");
      if (folders.hasNext()) {
        folder = folders.next();
      } else {
        folder = DriveApp.createFolder("DomoLink-BackUp");
      }
    }

    // 3. Action : Lister les sauvegardes
    if (data.action === "list") {
      var files = folder.getFiles();
      var list = [];
      while (files.hasNext()) {
        var f = files.next();
        list.push({
          id: f.getId(),
          name: f.getName(),
          size: f.getSize(),
          date: f.getDateCreated().toISOString()
        });
      }
      return ContentService.createTextOutput(JSON.stringify({
        success: true,
        code: 200,
        backups: list
      })).setMimeType(ContentService.MimeType.JSON);
    }

    // 4. Action : Supprimer une sauvegarde
    if (data.action === "delete") {
      var fileToDelete = DriveApp.getFileById(data.file_id);
      fileToDelete.setTrashed(true);
      return ContentService.createTextOutput(JSON.stringify({
        success: true,
        code: 200,
        message: "Fichier supprimé."
      })).setMimeType(ContentService.MimeType.JSON);
    }

    // 5. Téléversement de fichier (Base64)
    if (!data.filename || !data.file_content) {
      return ContentService.createTextOutput(JSON.stringify({
        success: false,
        code: 400,
        message: "Nom ou contenu de fichier manquant."
      })).setMimeType(ContentService.MimeType.JSON);
    }

    var decoded = Utilities.base64Decode(data.file_content);
    var blob = Utilities.newBlob(decoded, data.mime_type || "application/x-tar", data.filename);
    var createdFile = folder.createFile(blob);

    return ContentService.createTextOutput(JSON.stringify({
      success: true,
      code: 200,
      file_id: createdFile.getId(),
      file_name: createdFile.getName(),
      file_size: createdFile.getSize(),
      url: createdFile.getUrl()
    })).setMimeType(ContentService.MimeType.JSON);

  } catch (error) {
    return ContentService.createTextOutput(JSON.stringify({
      success: false,
      code: 500,
      message: error.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}
"""
