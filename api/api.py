import json
import os
import sqlite3
from typing import Optional

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import FileResponse

WEB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "yedekleme-isleri.html")

@app.get("/")
def web():
    return FileResponse(WEB_PATH)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.db")

app = FastAPI()

# Web arayüzü başka bir adresten (ya da dosyadan) açıldığı için tarayıcının isteğe izin vermesi gerekiyor
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            source TEXT,
            target TEXT,
            schedule TEXT
        )
    """)
    return connection


@app.get("/jobs")
def get_jobs(client_id: Optional[str] = None):
    connection = connect()
    if client_id is None:
        # Arayüz: bütün makinelerin işleri
        rows = connection.execute(
            "SELECT client_id, source, target, schedule FROM jobs ORDER BY id"
        ).fetchall()
    else:
        # Servis: sadece kendi makinesinin işleri
        rows = connection.execute(
            "SELECT client_id, source, target, schedule FROM jobs WHERE client_id = ? ORDER BY id",
            (client_id,)
        ).fetchall()
    connection.close()
    return [{"client_id": client_id, "source": source, "target": target, "schedule": json.loads(schedule)}
            for client_id, source, target, schedule in rows]


@app.put("/jobs")
def save_jobs(jobs: list = Body()):
    connection = connect()
    with connection:   # hepsi birlikte kaydedilir ya da hiçbiri
        connection.execute("DELETE FROM jobs")
        for job in jobs:
            connection.execute(
                "INSERT INTO jobs (client_id, source, target, schedule) VALUES (?, ?, ?, ?)",
                (job["client_id"], job["source"], job["target"], json.dumps(job["schedule"]))
            )
    connection.close()
    return {"saved": len(jobs)}