import json
import os
import sqlite3

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
            source TEXT,
            target TEXT,
            schedule TEXT
        )
    """)
    return connection


@app.get("/jobs")
def get_jobs():
    connection = connect()
    rows = connection.execute("SELECT source, target, schedule FROM jobs ORDER BY id").fetchall()
    connection.close()
    return [{"source": source, "target": target, "schedule": json.loads(schedule)}
            for source, target, schedule in rows]


@app.put("/jobs")
def save_jobs(jobs: list = Body()):
    connection = connect()
    with connection:   # hepsi birlikte kaydedilir ya da hiçbiri
        connection.execute("DELETE FROM jobs")
        for job in jobs:
            connection.execute(
                "INSERT INTO jobs (source, target, schedule) VALUES (?, ?, ?)",
                (job["source"], job["target"], json.dumps(job["schedule"]))
            )
    connection.close()
    return {"saved": len(jobs)}