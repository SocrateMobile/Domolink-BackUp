/**
 * DomoLink-BackUp Frontend Dashboard Panel
 * Sidebar Title: DomoLink-BackUp
 * Version: 1.2.0
 * Style: Modern DomoLink Glassmorphism Dark UI
 */

const GOOGLE_SCRIPT_CODE = `/**
 * Script Google Apps Script pour DomoLink-BackUp
 * Déployer en tant qu'Application Web :
 * - Exécuter en tant que : Moi (votre adresse Gmail)
 * - Qui a accès : Tout le monde (Anyone)
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return ContentService.createTextOutput(JSON.stringify({
        success: false, code: 400, message: "Corps vide."
      })).setMimeType(ContentService.MimeType.JSON);
    }
    var data = JSON.parse(e.postData.contents);
    if (data.probe === true) {
      return ContentService.createTextOutput(JSON.stringify({
        success: true, code: 200, message: "Connexion Google Drive validée avec succès."
      })).setMimeType(ContentService.MimeType.JSON);
    }
    var folder = DriveApp.getFoldersByName("DomoLink-BackUp").hasNext() ? 
                 DriveApp.getFoldersByName("DomoLink-BackUp").next() : 
                 DriveApp.createFolder("DomoLink-BackUp");
    if (data.action === "list") {
      var files = folder.getFiles();
      var list = [];
      while (files.hasNext()) {
        var f = files.next();
        list.push({ id: f.getId(), name: f.getName(), size: f.getSize(), date: f.getDateCreated().toISOString() });
      }
      return ContentService.createTextOutput(JSON.stringify({ success: true, code: 200, backups: list })).setMimeType(ContentService.MimeType.JSON);
    }
    if (data.action === "delete") {
      DriveApp.getFileById(data.file_id).setTrashed(true);
      return ContentService.createTextOutput(JSON.stringify({ success: true, code: 200, message: "Supprimé." })).setMimeType(ContentService.MimeType.JSON);
    }
    if (data.file_content) {
      var decoded = Utilities.base64Decode(data.file_content);
      var blob = Utilities.newBlob(decoded, "application/x-tar", data.filename || "backup.tar");
      var fCreated = folder.createFile(blob);
      return ContentService.createTextOutput(JSON.stringify({ success: true, code: 200, file_id: fCreated.getId() })).setMimeType(ContentService.MimeType.JSON);
    }
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ success: false, code: 500, message: err.toString() })).setMimeType(ContentService.MimeType.JSON);
  }
}`;

const DEFAULT_TEMPLATE = "$Date $Heure BackUp Home Assistant $Mode";

function evaluateTemplate(tpl, mode = "MANUEL", dObj = new Date()) {
  if (!tpl) tpl = DEFAULT_TEMPLATE;
  const day = String(dObj.getDate()).padStart(2, "0");
  const month = String(dObj.getMonth() + 1).padStart(2, "0");
  const year = dObj.getFullYear();
  const dateStr = `${day}/${month}/${year}`;

  const hr = String(dObj.getHours()).padStart(2, "0");
  const mn = String(dObj.getMinutes()).padStart(2, "0");
  const hourStr = `${hr}H${mn}`;

  return tpl
    .replace(/\$Date/g, dateStr)
    .replace(/\$Heure/g, hourStr)
    .replace(/\$Mode/g, mode);
}

function roundSize(bytes) {
  if (!bytes) return "0 Mo";
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) {
    return (mb / 1024).toFixed(2) + " Go";
  }
  return mb.toFixed(1) + " Mo";
}

function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return "0s";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}m ${rem}s`;
}

class DomoLinkBackupPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._data = {};
    this._config = {};
    this._version = "1.2.0";
    this._activeTab = "dashboard";
    this._refreshTimer = null;
    this._pollingInterval = 8000;
    this._showBackupModal = false;
    this._showReport = true;
    this._modalBackupName = "";
    this._modalIncludeDb = true;
    this._templateInputVal = "";
    this._templateSavedNotice = "";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialFetchDone) {
      this._initialFetchDone = true;
      this._fetchData();
      this._scheduleRefresh();
    }
  }

  connectedCallback() {
    this._render();
  }

  disconnectedCallback() {
    if (this._refreshTimer) {
      clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  _scheduleRefresh() {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    const isBusy = (this._data && this._data.is_busy) || (this._data && this._data.progress && this._data.progress.active);
    const interval = isBusy ? 1500 : 8000;
    this._refreshTimer = setTimeout(() => {
      if (this._hass) {
        this._fetchData(true);
      }
      this._scheduleRefresh();
    }, interval);
  }

  async _fetchData(silent = false) {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callWS({ type: "domolink_backup/get_data" });
      if (resp && resp.data) {
        this._data = resp.data;
        this._config = resp.config || {};
        if (resp.version) this._version = resp.version;

        if (!this._templateInputVal) {
          this._templateInputVal = this._data.backup_name_template || this._config.backup_name_template || DEFAULT_TEMPLATE;
        }

        this._render();

        // Auto-scroll live log console to bottom if present
        const logConsole = this.shadowRoot.querySelector("#live-log-console");
        if (logConsole) {
          logConsole.scrollTop = logConsole.scrollHeight;
        }
      }
    } catch (err) {
      if (!silent) console.error("DomoLink-BackUp: Erreur récupération données:", err);
    }
  }

  _openBackupModal() {
    const currentTpl = this._data.backup_name_template || this._config.backup_name_template || DEFAULT_TEMPLATE;
    this._modalBackupName = evaluateTemplate(currentTpl, "MANUEL");
    this._modalIncludeDb = true;
    this._showBackupModal = true;
    this._render();
  }

  _closeBackupModal() {
    this._showBackupModal = false;
    this._render();
  }

  async _executeBackup() {
    if (!this._hass) return;
    const name = this._modalBackupName.trim();
    const includeDb = this._modalIncludeDb;
    this._closeBackupModal();
    this._showReport = true;

    try {
      await this._hass.callWS({
        type: "domolink_backup/trigger_backup",
        name: name || undefined,
        include_database: includeDb,
        mode: "MANUEL",
      });
      this._fetchData();
      this._scheduleRefresh();
    } catch (err) {
      alert("Erreur lors du déclenchement de la sauvegarde : " + (err.message || err));
    }
  }

  async _saveTemplate(newTpl) {
    if (!this._hass) return;
    const tpl = (newTpl !== undefined ? newTpl : this._templateInputVal).trim() || DEFAULT_TEMPLATE;
    try {
      await this._hass.callWS({
        type: "domolink_backup/save_template",
        template: tpl,
      });
      this._data.backup_name_template = tpl;
      this._templateInputVal = tpl;
      this._templateSavedNotice = "✓ Modèle enregistré avec succès !";
      this._render();
      setTimeout(() => {
        this._templateSavedNotice = "";
        this._render();
      }, 3500);
    } catch (err) {
      alert("Erreur lors de l'enregistrement du modèle : " + (err.message || err));
    }
  }

  async _testConnection() {
    if (!this._hass) return;
    this._activeTab = "diagnostics";
    this._render();
    try {
      await this._hass.callWS({ type: "domolink_backup/test_connection" });
      this._fetchData();
    } catch (err) {
      alert("Erreur lors du test : " + (err.message || err));
    }
  }

  async _cleanBackups() {
    if (!this._hass) return;
    if (!confirm("Voulez-vous appliquer la politique de rétention (suppression des plus anciennes sauvegardes dépassant vos quotas) ?")) return;
    try {
      const res = await this._hass.callWS({ type: "domolink_backup/clean_backups" });
      alert(`Nettoyage terminé : ${res.deleted_count || 0} sauvegarde(s) supprimée(s).`);
      this._fetchData();
    } catch (err) {
      alert("Erreur nettoyage : " + (err.message || err));
    }
  }

  async _deleteBackup(backupId, name) {
    if (!this._hass) return;
    if (!confirm(`Supprimer définitivement la sauvegarde "${name}" de votre stockage distant ?`)) return;
    try {
      await this._hass.callWS({
        type: "domolink_backup/delete_backup",
        backup_id: backupId,
      });
      this._fetchData();
    } catch (err) {
      alert("Erreur lors de la suppression : " + (err.message || err));
    }
  }

  _copyScript() {
    navigator.clipboard.writeText(GOOGLE_SCRIPT_CODE).then(() => {
      alert("✓ Code Google Apps Script copié dans le presse-papiers !");
    }).catch(() => {
      alert("Impossible de copier automatiquement. Veuillez sélectionner le code manuellement.");
    });
  }

  _setTab(tab) {
    this._activeTab = tab;
    this._render();
  }

  _render() {
    const d = this._data || {};
    const cfg = this._config || {};
    const isBusy = d.is_busy || false;
    const status = d.status || "Prêt";
    const dest = d.destination_label || "Non configuré";
    const totalBackups = d.total_backups_count || 0;
    const totalMb = d.total_storage_mb || 0;
    const maxMb = d.max_storage_mb || cfg.max_storage_mb || 10240;
    const pctUsed = Math.min(100, Math.round((totalMb / maxMb) * 100));
    const connState = d.connection_status || "Prêt";
    const connSuccess = connState === "Connecté" || connState.includes("✓");
    const lastBackupDate = d.last_backup_date ? new Date(d.last_backup_date).toLocaleString("fr-FR") : "Aucune";
    const lastBackupName = d.last_backup_name || "N/A";
    const lastBackupSize = d.last_backup_size_mb ? `${d.last_backup_size_mb} Mo` : "";

    const backupsList = d.backups_list || [];
    const testLogs = d.test_logs || [];

    const progress = d.progress || { active: false, percent: 0, step_title: "Prêt", logs: [] };
    const progressActive = progress.active || isBusy;
    const progressPercent = Math.max(0, Math.min(100, progress.percent || 0));
    const progressLogs = progress.logs || [];
    const report = (this._showReport && (progress.report || d.last_report)) ? (progress.report || d.last_report) : null;

    const currentTpl = this._templateInputVal || d.backup_name_template || cfg.backup_name_template || DEFAULT_TEMPLATE;
    const tplPreview = evaluateTemplate(currentTpl, "MANUEL");

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          color: #e0e6ed;
          background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
          min-height: 100vh;
          padding: 24px;
          box-sizing: border-box;
        }

        .container {
          max-width: 1200px;
          margin: 0 auto;
        }

        /* Top Header */
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          background: rgba(30, 41, 59, 0.7);
          backdrop-filter: blur(12px);
          border: 1px solid rgba(255, 255, 255, 0.08);
          border-radius: 16px;
          padding: 20px 24px;
          margin-bottom: 24px;
          box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }

        .header-title-group {
          display: flex;
          align-items: center;
          gap: 16px;
        }

        .app-icon {
          width: 54px;
          height: 54px;
          background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
          border-radius: 14px;
          display: flex;
          align-items: center;
          justify-content: center;
          font-size: 28px;
          box-shadow: 0 4px 20px rgba(37, 99, 235, 0.45);
          border: 1px solid rgba(255, 255, 255, 0.15);
        }

        .title-sub {
          display: flex;
          flex-direction: column;
        }

        .app-title {
          font-size: 24px;
          font-weight: 700;
          letter-spacing: -0.5px;
          color: #ffffff;
          margin: 0;
        }

        .app-subtitle {
          font-size: 13px;
          color: #94a3b8;
          margin-top: 4px;
        }

        .badge-status {
          display: inline-flex;
          align-items: center;
          gap: 8px;
          padding: 6px 14px;
          border-radius: 999px;
          font-size: 13px;
          font-weight: 600;
          background: ${isBusy ? "rgba(59, 130, 246, 0.2)" : (connSuccess ? "rgba(16, 185, 129, 0.15)" : "rgba(239, 68, 68, 0.15)")};
          color: ${isBusy ? "#60a5fa" : (connSuccess ? "#34d399" : "#f87171")};
          border: 1px solid ${isBusy ? "rgba(59, 130, 246, 0.4)" : (connSuccess ? "rgba(16, 185, 129, 0.3)" : "rgba(239, 68, 68, 0.3)")};
        }

        .badge-dot {
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background: ${isBusy ? "#3b82f6" : (connSuccess ? "#10b981" : "#ef4444")};
          box-shadow: 0 0 8px ${isBusy ? "#3b82f6" : (connSuccess ? "#10b981" : "#ef4444")};
        }

        /* Navigation Tabs */
        .tabs {
          display: flex;
          gap: 8px;
          margin-bottom: 24px;
          background: rgba(15, 23, 42, 0.6);
          padding: 6px;
          border-radius: 12px;
          border: 1px solid rgba(255, 255, 255, 0.05);
        }

        .tab-button {
          flex: 1;
          padding: 12px 16px;
          border-radius: 8px;
          border: none;
          background: transparent;
          color: #94a3b8;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.2s ease;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
        }

        .tab-button:hover {
          color: #ffffff;
          background: rgba(255, 255, 255, 0.05);
        }

        .tab-button.active {
          color: #ffffff;
          background: #2563eb;
          box-shadow: 0 4px 12px rgba(37, 99, 235, 0.35);
        }

        /* Stats Grid */
        .stats-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
          gap: 16px;
          margin-bottom: 24px;
        }

        .stat-card {
          background: rgba(30, 41, 59, 0.6);
          backdrop-filter: blur(12px);
          border: 1px solid rgba(255, 255, 255, 0.06);
          border-radius: 14px;
          padding: 20px;
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }

        .stat-label {
          font-size: 12px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.5px;
          color: #94a3b8;
          margin-bottom: 8px;
        }

        .stat-value {
          font-size: 26px;
          font-weight: 700;
          color: #f8fafc;
        }

        .stat-sub {
          font-size: 13px;
          color: #64748b;
          margin-top: 6px;
        }

        /* Quota Bar */
        .progress-bar-bg {
          width: 100%;
          height: 8px;
          background: rgba(255, 255, 255, 0.1);
          border-radius: 4px;
          overflow: hidden;
          margin-top: 10px;
        }

        .progress-bar-fill {
          height: 100%;
          background: linear-gradient(90deg, #3b82f6, #10b981);
          width: ${pctUsed}%;
          transition: width 0.3s ease;
        }

        /* Action Buttons */
        .action-row {
          display: flex;
          flex-wrap: wrap;
          gap: 12px;
          margin-bottom: 24px;
        }

        .btn {
          padding: 12px 20px;
          border-radius: 10px;
          font-size: 14px;
          font-weight: 600;
          border: none;
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          gap: 8px;
          transition: all 0.2s ease;
        }

        .btn-primary {
          background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
          color: white;
          box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4);
        }

        .btn-primary:hover {
          background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
          transform: translateY(-1px);
        }

        .btn-secondary {
          background: rgba(51, 65, 85, 0.8);
          color: #e2e8f0;
          border: 1px solid rgba(255, 255, 255, 0.1);
        }

        .btn-secondary:hover {
          background: rgba(71, 85, 105, 0.9);
          color: white;
        }

        .btn-success {
          background: linear-gradient(135deg, #10b981 0%, #059669 100%);
          color: white;
        }

        /* Cards */
        .card {
          background: rgba(30, 41, 59, 0.6);
          backdrop-filter: blur(12px);
          border: 1px solid rgba(255, 255, 255, 0.06);
          border-radius: 14px;
          padding: 24px;
          margin-bottom: 24px;
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }

        .card-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 18px;
        }

        .card-title {
          font-size: 18px;
          font-weight: 700;
          color: #ffffff;
          margin: 0;
          display: flex;
          align-items: center;
          gap: 8px;
        }

        /* Progress Card & Realtime Tracking */
        .live-progress-card {
          background: rgba(15, 23, 42, 0.85);
          border: 1px solid rgba(59, 130, 246, 0.35);
          box-shadow: 0 8px 32px rgba(37, 99, 235, 0.2);
          border-radius: 16px;
          padding: 24px;
          margin-bottom: 24px;
          animation: pulseBorder 3s infinite ease-in-out;
        }

        @keyframes pulseBorder {
          0%, 100% { border-color: rgba(59, 130, 246, 0.35); box-shadow: 0 8px 32px rgba(37, 99, 235, 0.15); }
          50% { border-color: rgba(59, 130, 246, 0.75); box-shadow: 0 8px 32px rgba(37, 99, 235, 0.35); }
        }

        .live-track-bar-bg {
          width: 100%;
          height: 14px;
          background: rgba(255, 255, 255, 0.08);
          border-radius: 8px;
          overflow: hidden;
          position: relative;
          margin: 16px 0;
          border: 1px solid rgba(255, 255, 255, 0.1);
        }

        .live-track-bar-fill {
          height: 100%;
          background: linear-gradient(90deg, #3b82f6, #06b6d4, #10b981);
          border-radius: 8px;
          transition: width 0.4s ease;
          background-size: 30px 30px;
          background-image: linear-gradient(
            135deg,
            rgba(255, 255, 255, 0.15) 25%,
            transparent 25%,
            transparent 50%,
            rgba(255, 255, 255, 0.15) 50%,
            rgba(255, 255, 255, 0.15) 75%,
            transparent 75%,
            transparent
          );
          animation: progressStripes 1.5s linear infinite;
        }

        @keyframes progressStripes {
          from { background-position: 0 0; }
          to { background-position: 60px 0; }
        }

        .stage-badge {
          display: inline-block;
          padding: 4px 10px;
          border-radius: 6px;
          font-size: 11px;
          font-weight: 700;
          text-transform: uppercase;
          background: rgba(59, 130, 246, 0.2);
          color: #60a5fa;
          border: 1px solid rgba(59, 130, 246, 0.4);
        }

        .live-metrics-row {
          display: flex;
          flex-wrap: wrap;
          gap: 20px;
          background: rgba(0, 0, 0, 0.25);
          padding: 12px 16px;
          border-radius: 10px;
          margin-top: 12px;
          font-size: 13px;
        }

        .metric-item {
          display: flex;
          flex-direction: column;
        }

        .metric-lbl {
          font-size: 11px;
          color: #94a3b8;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }

        .metric-val {
          font-size: 14px;
          font-weight: 600;
          color: #f1f5f9;
        }

        /* Console Log Window */
        .console {
          background: #090d16;
          border-radius: 10px;
          border: 1px solid #1e293b;
          padding: 16px;
          font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
          font-size: 12px;
          max-height: 240px;
          overflow-y: auto;
          color: #cbd5e1;
          margin-top: 16px;
          box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.5);
        }

        .log-entry {
          margin-bottom: 5px;
          line-height: 1.5;
          word-break: break-word;
        }

        .log-time {
          color: #64748b;
          margin-right: 8px;
        }

        .log-success { color: #34d399; }
        .log-error { color: #f87171; }
        .log-warning { color: #fbbf24; }
        .log-info { color: #93c5fd; }

        /* Report Summary Card */
        .report-card {
          background: rgba(16, 185, 129, 0.08);
          border: 1px solid rgba(16, 185, 129, 0.3);
          border-radius: 14px;
          padding: 24px;
          margin-bottom: 24px;
          position: relative;
        }

        .report-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 16px;
          margin-top: 16px;
        }

        .report-box {
          background: rgba(0, 0, 0, 0.25);
          padding: 12px 14px;
          border-radius: 8px;
          border: 1px solid rgba(255, 255, 255, 0.04);
        }

        .report-box-title {
          font-size: 11px;
          color: #94a3b8;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }

        .report-box-value {
          font-size: 15px;
          font-weight: 600;
          color: #f8fafc;
          margin-top: 4px;
        }

        /* Modal Overlay */
        .modal-overlay {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(15, 23, 42, 0.75);
          backdrop-filter: blur(8px);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 9999;
          padding: 16px;
        }

        .modal-card {
          background: #1e293b;
          border: 1px solid rgba(255, 255, 255, 0.12);
          border-radius: 16px;
          width: 100%;
          max-width: 580px;
          box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
          overflow: hidden;
          animation: modalSlideUp 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }

        @keyframes modalSlideUp {
          from { opacity: 0; transform: translateY(20px) scale(0.97); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }

        .modal-header {
          padding: 20px 24px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          display: flex;
          align-items: center;
          justify-content: space-between;
        }

        .modal-title {
          font-size: 18px;
          font-weight: 700;
          color: #ffffff;
          margin: 0;
        }

        .modal-body {
          padding: 24px;
        }

        .modal-footer {
          padding: 16px 24px;
          background: rgba(15, 23, 42, 0.4);
          border-top: 1px solid rgba(255, 255, 255, 0.08);
          display: flex;
          justify-content: flex-end;
          gap: 12px;
        }

        /* Inputs & Form controls */
        .form-group {
          margin-bottom: 20px;
        }

        .form-label {
          display: block;
          font-size: 13px;
          font-weight: 600;
          color: #cbd5e1;
          margin-bottom: 8px;
        }

        .text-input {
          width: 100%;
          padding: 12px 14px;
          background: #0f172a;
          border: 1px solid rgba(255, 255, 255, 0.15);
          border-radius: 8px;
          color: #f8fafc;
          font-size: 14px;
          font-family: inherit;
          box-sizing: border-box;
          outline: none;
          transition: border-color 0.2s;
        }

        .text-input:focus {
          border-color: #3b82f6;
          box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.25);
        }

        .var-chips {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          margin-top: 10px;
        }

        .var-chip {
          display: inline-flex;
          align-items: center;
          background: rgba(59, 130, 246, 0.15);
          color: #93c5fd;
          border: 1px solid rgba(59, 130, 246, 0.3);
          padding: 4px 10px;
          border-radius: 6px;
          font-size: 12px;
          font-family: monospace;
          cursor: pointer;
          transition: all 0.15s;
        }

        .var-chip:hover {
          background: rgba(59, 130, 246, 0.3);
          color: #ffffff;
        }

        .preview-box {
          background: rgba(0, 0, 0, 0.3);
          padding: 10px 14px;
          border-radius: 8px;
          border: 1px dashed rgba(255, 255, 255, 0.15);
          margin-top: 10px;
          font-size: 13px;
          color: #94a3b8;
        }

        .preview-val {
          color: #38bdf8;
          font-weight: 600;
          font-family: monospace;
        }

        .checkbox-label {
          display: flex;
          align-items: center;
          gap: 10px;
          font-size: 14px;
          color: #e2e8f0;
          cursor: pointer;
          user-select: none;
        }

        /* Table */
        table {
          width: 100%;
          border-collapse: collapse;
          text-align: left;
          font-size: 14px;
        }

        th {
          padding: 12px 16px;
          color: #94a3b8;
          font-weight: 600;
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }

        td {
          padding: 14px 16px;
          border-bottom: 1px solid rgba(255, 255, 255, 0.04);
          color: #e2e8f0;
        }

        tr:hover td {
          background: rgba(255, 255, 255, 0.02);
        }

        .tag-proto {
          display: inline-block;
          padding: 4px 8px;
          border-radius: 6px;
          font-size: 11px;
          font-weight: 700;
          text-transform: uppercase;
          background: rgba(59, 130, 246, 0.2);
          color: #60a5fa;
          border: 1px solid rgba(59, 130, 246, 0.3);
        }

        .btn-delete {
          background: rgba(239, 68, 68, 0.15);
          color: #f87171;
          border: 1px solid rgba(239, 68, 68, 0.3);
          padding: 6px 12px;
          border-radius: 6px;
          cursor: pointer;
          font-size: 12px;
          font-weight: 600;
        }

        .btn-delete:hover {
          background: rgba(239, 68, 68, 0.3);
        }

        pre {
          background: #090d16;
          padding: 16px;
          border-radius: 8px;
          border: 1px solid #1e293b;
          overflow-x: auto;
          font-size: 12px;
          color: #38bdf8;
        }
      </style>

      <div class="container">
        <!-- Top App Bar -->
        <div class="header">
          <div class="header-title-group">
            <div class="app-icon">💾</div>
            <div class="title-sub">
              <h1 class="app-title">DomoLink-BackUp</h1>
              <div class="app-subtitle">Stockage distant & haute sécurité pour Home Assistant • v${this._version}</div>
            </div>
          </div>
          <div class="badge-status">
            <div class="badge-dot"></div>
            <span>${isBusy ? "Sauvegarde en cours..." : connState}</span>
          </div>
        </div>

        <!-- Navigation Tabs -->
        <div class="tabs">
          <button class="tab-button ${this._activeTab === 'dashboard' ? 'active' : ''}" id="tab-dash">
            📊 Tableau de bord
          </button>
          <button class="tab-button ${this._activeTab === 'backups' ? 'active' : ''}" id="tab-backups">
            🗄️ Sauvegardes distantes (${totalBackups})
          </button>
          <button class="tab-button ${this._activeTab === 'config' ? 'active' : ''}" id="tab-cfg">
            ⚙️ Profil & Modèle
          </button>
          <button class="tab-button ${this._activeTab === 'diagnostics' ? 'active' : ''}" id="tab-diag">
            🧪 Diagnostics & Guide
          </button>
        </div>

        <!-- TAB 1: DASHBOARD -->
        ${this._activeTab === 'dashboard' ? `
          <!-- LIVE PROGRESS & VERBOSE TRACKING (Visible when active or busy) -->
          ${progressActive ? `
            <div class="live-progress-card">
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="display: flex; align-items: center; gap: 10px;">
                  <span class="stage-badge">Étape active : ${progress.stage || 'Traitement'}</span>
                  <span style="font-size: 15px; font-weight: 700; color: #60a5fa;">${progress.step_title || 'En cours...'}</span>
                </div>
                <div style="font-size: 22px; font-weight: 800; color: #38bdf8;">
                  ${progressPercent}%
                </div>
              </div>

              <div class="live-track-bar-bg">
                <div class="live-track-bar-fill" style="width: ${progressPercent}%;"></div>
              </div>

              ${progress.step_detail ? `
                <div style="font-size: 13px; color: #cbd5e1; margin-top: 4px;">
                  <b>Action :</b> ${progress.step_detail}
                </div>
              ` : ''}

              <!-- Live Transfer Metrics (if uploading) -->
              ${progress.transferred_bytes ? `
                <div class="live-metrics-row">
                  <div class="metric-item">
                    <span class="metric-lbl">Volume transféré</span>
                    <span class="metric-val">${roundSize(progress.transferred_bytes)} / ${roundSize(progress.total_bytes)}</span>
                  </div>
                  <div class="metric-item">
                    <span class="metric-lbl">Vitesse moyenne</span>
                    <span class="metric-val">${progress.speed_kbps ? (progress.speed_kbps / 1024).toFixed(1) + ' Mo/s' : 'En calcul...'}</span>
                  </div>
                  <div class="metric-item">
                    <span class="metric-lbl">Temps restant estimé</span>
                    <span class="metric-val">${progress.eta_seconds ? progress.eta_seconds + ' s' : '—'}</span>
                  </div>
                </div>
              ` : ''}

              <!-- Realtime Diagnostic Logs Console -->
              <div style="margin-top: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                  <span style="font-size: 12px; font-weight: 600; color: #94a3b8; text-transform: uppercase;">
                    📡 Journal d'exécution en direct
                  </span>
                  <span style="font-size: 11px; color: #64748b;">${progressLogs.length} entrée(s)</span>
                </div>
                <div class="console" id="live-log-console">
                  ${progressLogs.length === 0 ? `
                    <div class="log-entry log-info">Initialisation du processus de sauvegarde...</div>
                  ` : progressLogs.map(l => `
                    <div class="log-entry log-${l.level || 'info'}">
                      <span class="log-time">[${l.time}]</span>
                      <span>${l.message}</span>
                    </div>
                  `).join('')}
                </div>
              </div>
            </div>
          ` : ''}

          <!-- COMPLETION REPORT CARD (Shown after backup completion) -->
          ${report ? `
            <div class="report-card">
              <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="display: flex; align-items: center; gap: 10px;">
                  <span style="font-size: 20px;">✅</span>
                  <h3 style="margin: 0; font-size: 17px; font-weight: 700; color: #34d399;">
                    Rapport de la dernière sauvegarde
                  </h3>
                </div>
                <button class="btn btn-secondary" id="btn-close-report" style="padding: 4px 10px; font-size: 11px;">
                  ✕ Masquer
                </button>
              </div>

              <div class="report-grid">
                <div class="report-box">
                  <div class="report-box-title">Nom de l'archive</div>
                  <div class="report-box-value">${report.backup_name || 'N/A'}</div>
                </div>
                <div class="report-box">
                  <div class="report-box-title">Cible de stockage</div>
                  <div class="report-box-value">${report.destination || dest}</div>
                </div>
                <div class="report-box">
                  <div class="report-box-title">Taille générée</div>
                  <div class="report-box-value">${report.size_mb ? report.size_mb + ' Mo' : roundSize(report.size_bytes)}</div>
                </div>
                <div class="report-box">
                  <div class="report-box-title">Durée de l'opération</div>
                  <div class="report-box-value">${report.duration_formatted || formatDuration(report.duration_sec)}</div>
                </div>
                <div class="report-box">
                  <div class="report-box-title">Taux de compression</div>
                  <div class="report-box-value" style="color: #60a5fa;">${report.compression_ratio || 'Format .tar standard'}</div>
                </div>
                <div class="report-box">
                  <div class="report-box-title">Emplacement source</div>
                  <div class="report-box-value" style="font-size: 12px; word-break: break-all; color: #94a3b8;">${report.source_path || 'Dossier /backup Home Assistant'}</div>
                </div>
              </div>

              ${report.components && report.components.length ? `
                <div style="margin-top: 14px; font-size: 12px; color: #94a3b8;">
                  <b>Éléments inclus :</b> ${report.components.join(' • ')}
                </div>
              ` : ''}
            </div>
          ` : ''}

          <div class="stats-grid">
            <div class="stat-card">
              <div class="stat-label">Cible active</div>
              <div class="stat-value" style="font-size: 20px;">${dest}</div>
              <div class="stat-sub">Protocole : ${d.protocol || cfg.protocol || "FTP"}</div>
            </div>

            <div class="stat-card">
              <div class="stat-label">Archives distantes</div>
              <div class="stat-value">${totalBackups}</div>
              <div class="stat-sub">Rétention max : ${cfg.max_backups_count || 7} fichiers</div>
            </div>

            <div class="stat-card">
              <div class="stat-label">Espace occupé</div>
              <div class="stat-value">${totalMb} Mo</div>
              <div class="progress-bar-bg">
                <div class="progress-bar-fill"></div>
              </div>
              <div class="stat-sub">${pctUsed}% sur ${maxMb} Mo alloués</div>
            </div>

            <div class="stat-card">
              <div class="stat-label">Dernière sauvegarde</div>
              <div class="stat-value" style="font-size: 16px;">${lastBackupDate}</div>
              <div class="stat-sub">${lastBackupName} ${lastBackupSize ? '(' + lastBackupSize + ')' : ''}</div>
            </div>
          </div>

          <div class="action-row">
            <button class="btn btn-primary" id="btn-backup-now">
              🚀 Sauvegarder maintenant
            </button>
            <button class="btn btn-secondary" id="btn-test-conn">
              ⚡ Tester la connexion
            </button>
            <button class="btn btn-secondary" id="btn-clean">
              🧹 Nettoyer selon rétention
            </button>
            <button class="btn btn-secondary" id="btn-refresh">
              🔄 Rafraîchir
            </button>
          </div>

          <div class="card">
            <div class="card-header">
              <h2 class="card-title">État du système</h2>
              <span style="font-size: 13px; color: ${isBusy ? '#60a5fa' : '#34d399'}; font-weight: 600;">
                ${isBusy ? '⏳ Opération en cours...' : '✓ En attente d\'instructions'}
              </span>
            </div>
            <p style="margin: 0; color: #cbd5e1; font-size: 14px;">
              Activité : <b>${status}</b>
            </p>
            ${d.last_action ? `<p style="margin-top: 8px; color: #94a3b8; font-size: 13px;">Détail : ${d.last_action}</p>` : ''}
            ${d.last_error ? `<p style="margin-top: 8px; color: #f87171; font-size: 13px;">⚠️ Erreur : ${d.last_error}</p>` : ''}
          </div>
        ` : ''}

        <!-- TAB 2: REMOTE BACKUPS LIST -->
        ${this._activeTab === 'backups' ? `
          <div class="card">
            <div class="card-header">
              <h2 class="card-title">Sauvegardes stockées sur ${dest}</h2>
              <button class="btn btn-secondary" id="btn-refresh-list" style="padding: 6px 12px; font-size: 12px;">
                🔄 Actualiser
              </button>
            </div>
            ${backupsList.length === 0 ? `
              <p style="color: #94a3b8; font-size: 14px; text-align: center; padding: 32px 0;">
                Aucune sauvegarde distante trouvée sur cette cible.<br>
                Cliquez sur <b>Sauvegarder maintenant</b> pour réaliser votre premier envoi sécurisé.
              </p>
            ` : `
              <table>
                <thead>
                  <tr>
                    <th>Fichier</th>
                    <th>Date de création</th>
                    <th>Taille</th>
                    <th>Protocole</th>
                    <th style="text-align: right;">Action</th>
                  </tr>
                </thead>
                <tbody>
                  ${backupsList.map(b => `
                    <tr>
                      <td style="font-weight: 600;">${b.name || b.filename}</td>
                      <td>${b.date ? new Date(b.date).toLocaleString('fr-FR') : 'Inconnue'}</td>
                      <td>${b.size ? (roundSize(b.size)) : '0 Ko'}</td>
                      <td><span class="tag-proto">${b.protocol || 'ftp'}</span></td>
                      <td style="text-align: right;">
                        <button class="btn-delete" data-id="${b.backup_id}" data-name="${b.name}">
                          🗑️ Supprimer
                        </button>
                      </td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            `}
          </div>
        ` : ''}

        <!-- TAB 3: CONFIGURATION & PROFILES & TEMPLATE -->
        ${this._activeTab === 'config' ? `
          <!-- TEMPLATE CONFIGURATION CARD -->
          <div class="card">
            <div class="card-header">
              <h2 class="card-title">⚙️ Format du nom de sauvegarde</h2>
              ${this._templateSavedNotice ? `
                <span style="color: #34d399; font-weight: 600; font-size: 13px;">${this._templateSavedNotice}</span>
              ` : ''}
            </div>
            <p style="font-size: 13px; color: #94a3b8; margin-bottom: 16px;">
              Définissez le modèle appliqué automatiquement à vos sauvegardes manuelles et programmées.<br>
              Vous pouvez insérer les variables <code>$Date</code>, <code>$Heure</code> et <code>$Mode</code>.
            </p>

            <div class="form-group">
              <label class="form-label" for="input-template">Modèle de nom de sauvegarde :</label>
              <input type="text" id="input-template" class="text-input" value="${currentTpl}" placeholder="${DEFAULT_TEMPLATE}">
              
              <div class="var-chips">
                <span style="font-size: 12px; color: #94a3b8; margin-right: 4px; align-self: center;">Variables :</span>
                <span class="var-chip" id="chip-date" title="Ajoute la date sous la forme 01/02/2026">+ $Date</span>
                <span class="var-chip" id="chip-heure" title="Ajoute l'heure sous la forme 20H28">+ $Heure</span>
                <span class="var-chip" id="chip-mode" title="Ajoute le mode (MANUEL ou AUTO)">+ $Mode</span>
              </div>

              <div class="preview-box">
                Aperçu en temps réel : <span class="preview-val" id="tpl-preview">${tplPreview}</span>
              </div>
            </div>

            <div style="display: flex; gap: 10px; margin-top: 16px;">
              <button class="btn btn-primary" id="btn-save-tpl">
                💾 Enregistrer le modèle
              </button>
              <button class="btn btn-secondary" id="btn-reset-tpl">
                ↺ Rétablir le modèle par défaut
              </button>
            </div>
          </div>

          <div class="card">
            <div class="card-header">
              <h2 class="card-title">Paramètres actifs de DomoLink-BackUp</h2>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; font-size: 14px;">
              <div>
                <p><b>Profil NAS / Cible :</b> ${cfg.nas_type || 'Synology'}</p>
                <p><b>Protocole :</b> ${cfg.protocol || 'FTP'}</p>
                <p><b>Hôte / URL :</b> ${cfg.ftp_host || cfg.webdav_url || cfg.google_drive_webhook_url || cfg.local_share_path || 'Non configuré'}</p>
                <p><b>Port :</b> ${cfg.ftp_port || 21}</p>
                <p><b>Dossier distant :</b> ${cfg.ftp_path || cfg.webdav_path || cfg.google_drive_folder_id || cfg.local_share_path || '/'}</p>
              </div>
              <div>
                <p><b>Rétention max :</b> ${cfg.max_backups_count || 7} sauvegardes</p>
                <p><b>Âge max :</b> ${cfg.retention_days || 30} jours</p>
                <p><b>Quota d'espace :</b> ${cfg.max_storage_mb || 10240} Mo</p>
                <p><b>Nettoyage auto :</b> ${cfg.auto_clean_enabled !== false ? 'Activé (FIFO)' : 'Désactivé'}</p>
                <p><b>Alertes Telegram :</b> ${cfg.telegram_enabled ? '✓ Activées' : 'Désactivées'}</p>
              </div>
            </div>
            <div style="margin-top: 20px; border-top: 1px solid rgba(255,255,255,0.08); padding-top: 16px;">
              <p style="font-size: 13px; color: #94a3b8; margin-bottom: 12px;">
                Pour modifier la cible (changer de serveur, adapter les quotas ou ajuster Telegram), rendez-vous dans <b>Paramètres > Appareils et services > DomoLink-BackUp > Configurer</b>.
              </p>
              <button class="btn btn-secondary" id="btn-retest-cfg">
                ⚡ Tester les identifiants actuels
              </button>
            </div>
          </div>
        ` : ''}

        <!-- TAB 4: DIAGNOSTICS & GOOGLE SCRIPT GUIDE -->
        ${this._activeTab === 'diagnostics' ? `
          <div class="card">
            <div class="card-header">
              <h2 class="card-title">Console de diagnostic réseau & protocole</h2>
              <button class="btn btn-secondary" id="btn-run-diag" style="padding: 6px 12px; font-size: 12px;">
                ⚡ Relancer le test
              </button>
            </div>
            <div class="console">
              ${testLogs.length === 0 ? `
                <div class="log-entry log-info">Aucun log récent. Cliquez sur "Relancer le test" pour interroger la cible.</div>
              ` : testLogs.map(l => `
                <div class="log-entry log-${l.level || 'info'}">
                  <span class="log-time">[${l.time}]</span>
                  <span>${l.message}</span>
                </div>
              `).join('')}
            </div>
          </div>

          <div class="card">
            <div class="card-header">
              <h2 class="card-title">Google Drive via Webhook (Google Apps Script)</h2>
              <button class="btn btn-primary" id="btn-copy-script" style="padding: 6px 14px; font-size: 12px;">
                📋 Copier le Google Script
              </button>
            </div>
            <p style="font-size: 13px; color: #94a3b8; margin-bottom: 12px;">
              Collez ce code dans <a href="https://script.google.com" target="_blank" style="color: #60a5fa;">script.google.com</a>, 
              déployez-le en <b>Application Web</b> (accès : <i>Tout le monde</i>) et reportez l'URL du Webhook dans la configuration de DomoLink-BackUp !
            </p>
            <pre><code>${GOOGLE_SCRIPT_CODE}</code></pre>
          </div>
        ` : ''}
      </div>

      <!-- MODAL POPIN: SAUVEGARDER MAINTENANT -->
      ${this._showBackupModal ? `
        <div class="modal-overlay" id="modal-overlay">
          <div class="modal-card">
            <div class="modal-header">
              <h3 class="modal-title">🚀 Nouvelle sauvegarde Home Assistant</h3>
              <button class="btn btn-secondary" id="btn-close-modal" style="padding: 4px 10px; font-size: 12px;">✕</button>
            </div>
            <div class="modal-body">
              <p style="font-size: 13px; color: #94a3b8; margin-top: 0; margin-bottom: 16px;">
                Vérifiez ou personnalisez le nom avant de lancer la création locale et le téléversement distant vers votre cible sécurisée.
              </p>

              <div class="form-group">
                <label class="form-label" for="modal-backup-input">Nom de la sauvegarde :</label>
                <input type="text" id="modal-backup-input" class="text-input" value="${this._modalBackupName}">

                <div class="var-chips">
                  <span style="font-size: 12px; color: #94a3b8; margin-right: 4px; align-self: center;">Insérer :</span>
                  <span class="var-chip" id="modal-chip-date">+ $Date</span>
                  <span class="var-chip" id="modal-chip-heure">+ $Heure</span>
                  <span class="var-chip" id="modal-chip-mode">+ $Mode</span>
                </div>
              </div>

              <div class="form-group" style="margin-bottom: 8px;">
                <label class="checkbox-label">
                  <input type="checkbox" id="modal-check-db" ${this._modalIncludeDb ? 'checked' : ''}>
                  <span>Inclure la base de données (enregistreur & historique)</span>
                </label>
              </div>
            </div>
            <div class="modal-footer">
              <button class="btn btn-secondary" id="btn-cancel-backup">Annuler</button>
              <button class="btn btn-primary" id="btn-confirm-backup">🚀 Lancer la sauvegarde</button>
            </div>
          </div>
        </div>
      ` : ''}
    `;

    this._bindEvents();
  }

  _bindEvents() {
    const root = this.shadowRoot;

    // Tabs
    const tabDash = root.querySelector("#tab-dash");
    if (tabDash) tabDash.onclick = () => this._setTab("dashboard");

    const tabBackups = root.querySelector("#tab-backups");
    if (tabBackups) tabBackups.onclick = () => this._setTab("backups");

    const tabCfg = root.querySelector("#tab-cfg");
    if (tabCfg) tabCfg.onclick = () => this._setTab("config");

    const tabDiag = root.querySelector("#tab-diag");
    if (tabDiag) tabDiag.onclick = () => this._setTab("diagnostics");

    // Action buttons
    const btnBackup = root.querySelector("#btn-backup-now");
    if (btnBackup) btnBackup.onclick = () => this._openBackupModal();

    const btnTest = root.querySelector("#btn-test-conn");
    if (btnTest) btnTest.onclick = () => this._testConnection();

    const btnClean = root.querySelector("#btn-clean");
    if (btnClean) btnClean.onclick = () => this._cleanBackups();

    const btnRefresh = root.querySelector("#btn-refresh");
    if (btnRefresh) btnRefresh.onclick = () => this._fetchData();

    const btnRefreshList = root.querySelector("#btn-refresh-list");
    if (btnRefreshList) btnRefreshList.onclick = () => this._fetchData();

    const btnRunDiag = root.querySelector("#btn-run-diag");
    if (btnRunDiag) btnRunDiag.onclick = () => this._testConnection();

    const btnRetest = root.querySelector("#btn-retest-cfg");
    if (btnRetest) btnRetest.onclick = () => this._testConnection();

    const btnCopyScript = root.querySelector("#btn-copy-script");
    if (btnCopyScript) btnCopyScript.onclick = () => this._copyScript();

    const btnCloseReport = root.querySelector("#btn-close-report");
    if (btnCloseReport) {
      btnCloseReport.onclick = () => {
        this._showReport = false;
        this._render();
      };
    }

    // Template input and chips (Config tab)
    const tplInput = root.querySelector("#input-template");
    const tplPreview = root.querySelector("#tpl-preview");
    if (tplInput) {
      tplInput.oninput = (e) => {
        this._templateInputVal = e.target.value;
        if (tplPreview) {
          tplPreview.textContent = evaluateTemplate(e.target.value, "MANUEL");
        }
      };
    }

    const insertVarInInput = (inputEl, varStr) => {
      if (!inputEl) return;
      const start = inputEl.selectionStart || inputEl.value.length;
      const end = inputEl.selectionEnd || inputEl.value.length;
      const current = inputEl.value;
      inputEl.value = current.substring(0, start) + varStr + current.substring(end);
      inputEl.dispatchEvent(new Event("input"));
      inputEl.focus();
    };

    const chipDate = root.querySelector("#chip-date");
    if (chipDate) chipDate.onclick = () => insertVarInInput(tplInput, "$Date");

    const chipHeure = root.querySelector("#chip-heure");
    if (chipHeure) chipHeure.onclick = () => insertVarInInput(tplInput, "$Heure");

    const chipMode = root.querySelector("#chip-mode");
    if (chipMode) chipMode.onclick = () => insertVarInInput(tplInput, "$Mode");

    const btnSaveTpl = root.querySelector("#btn-save-tpl");
    if (btnSaveTpl) {
      btnSaveTpl.onclick = () => {
        const val = tplInput ? tplInput.value : this._templateInputVal;
        this._saveTemplate(val);
      };
    }

    const btnResetTpl = root.querySelector("#btn-reset-tpl");
    if (btnResetTpl) {
      btnResetTpl.onclick = () => {
        this._templateInputVal = DEFAULT_TEMPLATE;
        this._saveTemplate(DEFAULT_TEMPLATE);
      };
    }

    // Modal Events
    const btnCloseModal = root.querySelector("#btn-close-modal");
    if (btnCloseModal) btnCloseModal.onclick = () => this._closeBackupModal();

    const btnCancelBackup = root.querySelector("#btn-cancel-backup");
    if (btnCancelBackup) btnCancelBackup.onclick = () => this._closeBackupModal();

    const modalInput = root.querySelector("#modal-backup-input");
    if (modalInput) {
      modalInput.oninput = (e) => {
        this._modalBackupName = e.target.value;
      };
    }

    const modalCheckDb = root.querySelector("#modal-check-db");
    if (modalCheckDb) {
      modalCheckDb.onchange = (e) => {
        this._modalIncludeDb = e.target.checked;
      };
    }

    const modalChipDate = root.querySelector("#modal-chip-date");
    if (modalChipDate) modalChipDate.onclick = () => insertVarInInput(modalInput, "$Date");

    const modalChipHeure = root.querySelector("#modal-chip-heure");
    if (modalChipHeure) modalChipHeure.onclick = () => insertVarInInput(modalInput, "$Heure");

    const modalChipMode = root.querySelector("#modal-chip-mode");
    if (modalChipMode) modalChipMode.onclick = () => insertVarInInput(modalInput, "$Mode");

    const btnConfirmBackup = root.querySelector("#btn-confirm-backup");
    if (btnConfirmBackup) btnConfirmBackup.onclick = () => this._executeBackup();

    // Delete buttons
    root.querySelectorAll(".btn-delete").forEach(btn => {
      btn.onclick = (e) => {
        const id = e.target.getAttribute("data-id");
        const name = e.target.getAttribute("data-name");
        this._deleteBackup(id, name);
      };
    });
  }
}

customElements.define("domolink-backup-panel", DomoLinkBackupPanel);
