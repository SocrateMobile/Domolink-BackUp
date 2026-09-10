# 💾 DomoLink-BackUp pour Home Assistant

[![HACS Compatible](https://img.shields.io/badge/HACS-Custom-blue.svg)](https://github.com/SocrateMobile/Domolink-BackUp)
[![Version](https://img.shields.io/badge/version-1.1.2-green.svg)](https://github.com/SocrateMobile/Domolink-BackUp/releases)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](LICENSE)

Dans la lignée directe de **DomoLink-Alarm** et **DomoLink-Mistral IA**, **DomoLink-BackUp** est une intégration Home Assistant puissante et clé en main dédiée au **stockage distant, automatique et sécurisé des sauvegardes de votre domotique**.

Elle reprend l'ergonomie, la mécanique et le paramétrage éprouvés de la suite DomoLink (profils NAS Asustor, Synology, QNAP, TrueNAS, Freebox, Unraid, transferts FTP/FTPS, WebDAV/Nextcloud, Google Drive sans clé API complexe, alertes Telegram et purge FIFO automatique).

---

## ✨ Points Forts & Fonctionnalités

### 🛡️ 1. Agent de Sauvegarde Officiel Home Assistant (`BackupAgent`)
- S'enregistre directement dans le gestionnaire de sauvegarde natif de Home Assistant (*Paramètres > Système > Sauvegardes*).
- Vos sauvegardes automatiques ou manuelles peuvent être dirigées directement vers **DomoLink-BackUp** comme emplacement de stockage distant, avec barre de progression en temps réel.
- Permet la consultation, le téléchargement et la suppression des archives distantes directement depuis l'interface native de Home Assistant.

### 🖧 2. Profils NAS & Multi-Destinations
- **ASUSTOR (ADM)** : Profil pré-configuré (FTP port 21 / WebDAV port 8001).
- **Synology (DSM)** : Profil pré-configuré (FTP port 21 / WebDAV port 5006 HTTPS sécurisé).
- **QNAP (QTS)** : Profil pré-configuré (FTP port 21 / WebDAV port 5001 HTTPS).
- **TrueNAS (SCALE / CORE)** : Profil WebDAV pré-optimisé.
- **Freebox (Delta / Ultra / Pop)** : Pré-rempli avec `mafreebox.freebox.fr`, port 21, utilisateur `freebox`, chemin `/Disque 1/Sauvegardes`.
- **Unraid & Autre Serveur** : Configuration libre FTP/FTPS et WebDAV.
- **Google Drive** : Téléversement direct via **Webhook Google Apps Script** (aucun compte Google Cloud payant ni token OAuth expirable requis).
- **Partage Réseau Local (Samba / NFS)** : Sauvegarde directe vers un répertoire monté sur l'hôte (`/share/...`, `/media/...`).

### 🧹 3. Politique de Rétention & Quotas FIFO
- **Nombre maximum de sauvegardes** : Conservez par exemple les 7 dernières sauvegardes.
- **Âge maximal en jours** : Suppression automatique des archives de plus de 30 jours.
- **Quota d'espace disque (Mo / Go)** : Définissez un plafond (ex: 10 Go) avec suppression FIFO automatique des plus anciennes archives si dépassement.

### 📱 4. Notifications Telegram en Temps Réel
- Notification optionnelle au lancement du transfert.
- Rapport complet de succès avec :
  - Nom du fichier d'archive
  - Taille en Mo / Go
  - Temps de transfert en secondes
  - Espace total occupé et nombre de sauvegardes sur le serveur distant
- Alerte immédiate en cas d'échec avec description et code diagnostic d'erreur.

### 🎛️ 5. Panneau Interactif dans la Barre Latérale (`DomoLink-BackUp`)
Un tableau de bord complet reprenant l'élégance de l'univers DomoLink (thème sombre, Glassmorphism, animations de statut) :
- **📊 Tableau de bord** : Jauge de quota d'espace, carte de la dernière sauvegarde, statistiques en direct et boutons d'action rapide.
- **🗄️ Sauvegardes Distantes** : Explorateur des archives hébergées sur le NAS/Cloud avec date, taille et suppression individuelle.
- **⚙️ Profil & Cible** : Visualisation et ajustement instantané des paramètres de connexion.
- **🧪 Diagnostics & Guide** : Console de test réseau en temps réel affichant les codes protocolaires (RFC FTP `220`, `230`, `530`, `550`, `111` ou HTTP WebDAV `200`, `401`, `403`, `404`) et script Google Apps Script copiable en 1 clic.

---

## 📥 Installation

### Méthode 1 : Via HACS (Recommandé)
1. Ouvrez **HACS** dans Home Assistant.
2. Rendez-vous dans **Intégrations** > Menu ⋮ (en haut à droite) > **Dépôts personnalisés**.
3. Ajoutez l'URL de votre dépôt : `https://github.com/SocrateMobile/Domolink-BackUp` avec la catégorie **Intégration**.
4. Cliquez sur **Installer**, puis redémarrez Home Assistant.
5. Allez dans **Paramètres > Appareils et services > Ajouter une intégration**, puis sélectionnez **DomoLink-BackUp**.

### Méthode 2 : Installation Manuelle
1. Copiez le dossier `custom_components/domolink_backup` dans votre répertoire `config/custom_components/` de Home Assistant.
2. Redémarrez Home Assistant.
3. Allez dans **Paramètres > Appareils et services > Ajouter une intégration** et recherchez **DomoLink-BackUp**.

---

## ⚙️ Configuration Pas-à-Pas

Lors de l'ajout de l'intégration, un assistant interactif en 4 étapes vous guide :

1. **Étape 1 : Profil NAS & Protocole**  
   Sélectionnez votre fabricant de NAS (Synology, QNAP, Asustor, TrueNAS, Freebox, Unraid) ou votre cible Cloud (WebDAV, Google Drive, Partage Local) ainsi que le protocole (FTP, FTPS, WebDAV).
2. **Étape 2 : Identifiants & Chemins**  
   Les ports et chemins par défaut sont automatiquement pré-remplis en fonction de votre modèle de NAS.
3. **Étape 3 : Rétention & Quotas**  
   Ajustez le nombre d'archives à conserver, l'âge maximum et le quota en Mo.
4. **Étape 4 : Alertes Telegram (Optionnel)**  
   Indiquez votre token de bot et votre chat ID pour être notifié à chaque sauvegarde.

---

## 📂 Configuration Google Drive (En 1 Minute)

La méthode Webhook Google Apps Script évite la complexité des clés OAuth2 :

1. Ouvrez le panneau **DomoLink-BackUp** dans votre barre latérale Home Assistant.
2. Rendez-vous sur l'onglet **🧪 Diagnostics & Guide** et cliquez sur **« 📋 Copier le Google Script »**.
3. Rendez-vous sur [script.google.com](https://script.google.com) et créez un **Nouveau projet**.
4. Remplacez le code existant par le script copié.
5. Cliquez sur **Déployer > Nouveau déploiement > Application Web** :
   - **Exécuter en tant que** : *Moi (votre adresse Gmail)*
   - **Qui a accès** : *Tout le monde (Anyone)*
6. Cliquez sur **Déployer**, autorisez l'accès à Google Drive, puis copiez l'URL de l'application Web (se terminant par `/exec`).
7. Collez cette URL dans la configuration de DomoLink-BackUp.

---

## 🧩 Entités & Services

### Entités Capteurs (`sensor`)
| Entité | Nom | Description |
|---|---|---|
| `sensor.domolink_backup_statut` | Statut | État en direct (`Prêt`, `Sauvegarde en cours`, `Téléversement en cours`, `Succès`, `Erreur`) |
| `sensor.domolink_backup_derniere_sauvegarde` | Dernière sauvegarde | Horodatage de la dernière sauvegarde réussie (avec taille et durée) |
| `sensor.domolink_backup_nombre_de_sauvegardes` | Nombre de sauvegardes | Nombre total d'archives présentes sur la cible distante |
| `sensor.domolink_backup_espace_utilise_distant` | Espace utilisé distant | Taille cumulée des sauvegardes sur le stockage distant (en Mo) |
| `sensor.domolink_backup_destination_de_stockage` | Destination de stockage | Nom et protocole du profil actif (ex: *Synology (DSM) (FTP)*) |
| `sensor.domolink_backup_etat_de_la_connexion` | État de la connexion | Résultat du dernier test réseau (`Connecté` ou code d'erreur) |

### Entités Boutons (`button`)
| Entité | Nom | Action |
|---|---|---|
| `button.domolink_backup_sauvegarder_maintenant` | Sauvegarder maintenant | Crée une sauvegarde Home Assistant et l'envoie immédiatement à distance |
| `button.domolink_backup_tester_la_connexion` | Tester la connexion | Exécute un test complet des droits d'accès et d'écriture |
| `button.domolink_backup_nettoyer_selon_la_retention` | Nettoyer selon la rétention | Applique la purge FIFO selon vos quotas |
| `button.domolink_backup_synchroniser_les_sauvegardes` | Synchroniser les sauvegardes | Rafraîchit la liste et les compteurs depuis le stockage distant |

### Services Disponibles
- `domolink_backup.create_backup` : Déclenche la création et l'envoi d'une sauvegarde (paramètres optionnels : `name`, `include_database`).
- `domolink_backup.upload_backup` : Envoie un fichier `.tar` existant (`file_path`).
- `domolink_backup.test_connection` : Lance un diagnostic de connexion avec logs détaillés.
- `domolink_backup.clean_old_backups` : Déclenche manuellement la purge des quotas.
- `domolink_backup.sync_backups` : Actualise les compteurs distants.

---

## 🤖 Exemple d'Automatisation

### Sauvegarde automatique hebdomadaire avec envoi vers le NAS
```yaml
alias: "DomoLink-BackUp - Sauvegarde hebdomadaire du Dimanche"
description: "Déclenche la sauvegarde automatique et l'envoi hors-site sur le NAS"
trigger:
  - platform: time
    at: "03:30:00"
condition:
  - condition: time
    weekday:
      - sun
action:
  - service: domolink_backup.create_backup
    data:
      name: "Sauvegarde_Dimanche"
      include_database: true
```

---

## 📄 Licence

Distribué sous licence **MIT**. Développé avec passion pour l'écosystème Home Assistant par **[SocrateMobile](https://github.com/SocrateMobile)**.
