import json
import os
import sqlite3
from typing import Optional

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.db")
WEB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "yedekleme-isleri.html")

app = FastAPI()

# Web arayüzü başka bir adresten (ya da dosyadan) açıldığı için tarayıcının isteğe izin vermesi gerekiyor
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


@app.get("/")
def web():
    # Tarayıcıda API'nin adresi açılınca arayüzü gönder
    return FileResponse(WEB_PATH)


# jobs tablosunda olması gereken sütunlar. Yeni sütun eklenince buraya da yazılır;
# eski veritabanında yoksa connect() onu kendisi ekler (migration).
JOB_COLUMNS = {
    "client_id": "TEXT",
    "source": "TEXT",
    "target_type": "TEXT DEFAULT 'local'",
    "target": "TEXT",
    "schedule": "TEXT"
}


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            source TEXT,
            target_type TEXT DEFAULT 'local',
            target TEXT,
            schedule TEXT
        )
    """)

    # Migration: tablo eski yapıdaysa eksik sütunları ekle (veriler korunur)
    existing = [row[1] for row in connection.execute("PRAGMA table_info(jobs)")]
    for name, column_type in JOB_COLUMNS.items():
        if name not in existing:
            connection.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (name, column_type))
    connection.commit()

    return connection


@app.get("/jobs")
def get_jobs(client_id: Optional[str] = None):
    connection = connect()
    if client_id is None:
        # Arayüz: bütün makinelerin işleri
        rows = connection.execute(
            "SELECT client_id, source, target_type, target, schedule FROM jobs ORDER BY id"
        ).fetchall()
    else:
        # Servis: sadece kendi makinesinin işleri
        rows = connection.execute(
            "SELECT client_id, source, target_type, target, schedule FROM jobs WHERE client_id = ? ORDER BY id",
            (client_id,)
        ).fetchall()
    connection.close()
    return [{"client_id": client_id, "source": source, "target_type": target_type,
             "target": target, "schedule": json.loads(schedule)}
            for client_id, source, target_type, target, schedule in rows]


@app.put("/jobs")
def save_jobs(jobs: list = Body()):
    connection = connect()
    with connection:   # hepsi birlikte kaydedilir ya da hiçbiri
        connection.execute("DELETE FROM jobs")
        for job in jobs:
            connection.execute(
                "INSERT INTO jobs (client_id, source, target_type, target, schedule) VALUES (?, ?, ?, ?, ?)",
                (job["client_id"], job["source"], job.get("target_type", "local"), job["target"],
                 json.dumps(job["schedule"]))
            )
    connection.close()
    return {"saved": len(jobs)}