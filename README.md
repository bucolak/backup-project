# Automated Backup System

A cross-platform, scheduled backup system that runs as a background service on Windows and Linux machines, verifies every file it copies, and can be managed remotely from a web panel through a REST API.

> 🚧 **Status: actively developed.** This is my senior design project (SE491, Üsküdar University) and I keep adding features every week. The idea came from problems I found while testing UrBackup during my internship, and version 1 is aimed at real on-premise use at that company.
>
> 🌐 **Coming next:** the API is going live on the internet so that machines on any network can connect. The public link will be added here once it is deployed.

## Why

Small companies often back up their servers by hand or with tools that are hard to configure and give little feedback when something fails. This project aims to make backups:

- **Automatic** — scheduled per folder, running without anyone logged in
- **Verifiable** — every copied file is checked with a SHA-256 hash
- **Observable** — progress, history and logs are visible from one web panel
- **Remote-friendly** — machines on different networks are configured from a central API instead of logging into each one

## Architecture

```
┌──────────────┐      REST (jobs, status, logs, restore)      ┌──────────────────┐
│  Web panel   │ ───────────────────────────────────────────▶ │   FastAPI API    │
│ (HTML / JS)  │ ◀─────────────────────────────────────────── │   + SQLite DB    │
└──────────────┘                                              └────────┬─────────┘
                                                                       │ polling
                               ┌───────────────────────────────────────┼───────────────────┐
                               ▼                                       ▼                   ▼
                     ┌───────────────────┐                   ┌───────────────────┐
                     │  Windows service  │                   │   Linux service   │   (one per machine,
                     │  (pywin32)        │                   │   (systemd)       │    identified by client_id)
                     └─────────┬─────────┘                   └─────────┬─────────┘
                               ▼                                       ▼
                       Local disk / pCloud / Google Drive targets
```

| Folder | What it contains |
|---|---|
| `windows-service/` | Windows service (pywin32) that reads its config, runs due jobs and reports status |
| `linux-service/` | Same service for Linux, installed with a `systemd` unit |
| `api/` | FastAPI app: stores jobs, status, logs and restore requests in SQLite; serves the web panel; can be installed as a Windows service (WinSW) |
| `web/` | Single-page web panel for managing jobs, watching progress, reading logs and restoring files |
| `server/` | Earlier server-side prototype: upload endpoint, job queue with claim/complete, APScheduler scheduling and pluggable storage providers (local, Google Drive, pCloud) |

## Features

- **Schedules:** daily, weekly (chosen days) or fixed interval, defined per job; a missed run creates a single catch-up job, not one per missed slot
- **Copy or move:** in *move* mode a source file is deleted only after it has been verified at the target
- **Integrity checks:** SHA-256 hashing, temporary `.part` files and atomic rename so a half-written file never looks like a finished backup
- **Optional compression:** per-file ZIP
- **Parallel transfers:** thread pool with 1 MB chunked copies and live progress reporting
- **Reliability:** configurable retries, permission/target re-checks, free-space reservation on the target disk
- **Cloud targets:** pCloud via its OAuth2 API (own client code, no rclone); Google Drive provider in the server prototype
- **Logs:** daily rotating log files with a retention period, also pushed to the API for the web panel
- **Restore:** browse backed-up files from the web panel and download them through the API

## Tech Stack

Python · FastAPI · Uvicorn · SQLite · APScheduler · pywin32 · systemd · WinSW · pCloud API · Google Drive API · HTML/CSS/JavaScript

## Getting Started

```bash
# API + web panel
cd api
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8001
# open http://localhost:8001
```

**Windows service** (run as administrator):

```bash
cd windows-service/service
pip install pywin32
python service.py install
python service.py start
```

**Linux service:** edit paths and user in `linux-service/backup-service.service`, copy it to `/etc/systemd/system/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now backup-service
```

Each machine has a `config.json` with its `client_id`, general settings (log folder, retention, minimum free space) and its backup jobs. Example job:

```json
{
  "client_id": "windows-pc",
  "source": "C:\\Data\\Reports",
  "target_type": "pcloud",
  "target": "backups/windows-pc/reports",
  "schedule": { "type": "weekly", "time": "02:00", "days": ["pzt", "per"] },
  "mode": "copy",
  "compression": "zip"
}
```

Day codes are Turkish abbreviations (`pzt`, `sal`, `car`, `per`, `cum`, `cmt`, `paz` = Mon–Sun). An optional `"retry": {"count": 3, "delay": 60}` overrides the default retry policy.

## Roadmap

- [ ] Authentication and per-client secret keys for the API
- [ ] Deploy the API publicly so machines on any network can connect *(in progress)*
- [ ] Incremental / full backup options
- [ ] Continuous (on-change) backups with file system watching
- [ ] Multi-user panel with roles (version 2)

## Author

**Buket Çolak** — [github.com/bucolak](https://github.com/bucolak)
