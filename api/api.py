import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

LOG_RETENTION_DAYS = 30   # API'de kaç günlük log tutulacak
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.db")
WEB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "yedekleme-isleri.html")
RESTORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restore_tmp")   # servisten gelen dosyalar, indirilene kadar
LOGIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login.json")   # set_password.py üretir

# CORS yok: panel API'nin kendi adresinden açılıyor, başka sitelerin sayfaları API'nin cevaplarını okuyamaz
app = FastAPI()


@app.get("/")
def web():
    # Tarayıcıda API'nin adresi açılınca arayüzü gönder
    return FileResponse(WEB_PATH)


# ---------------- Giriş (login) ----------------
# Giriş yapılınca rastgele bir oturum anahtarı üretilir ve tarayıcıya çerez olarak verilir.
# Oturumlar hafızada tutulur: API yeniden başlarsa tekrar giriş yapmak gerekir.
SESSIONS = set()


def require_login(session: Optional[str] = Cookie(None)):
    # Panelin kullandığı adresler: geçerli oturum çerezi yoksa 401
    if session not in SESSIONS:
        raise HTTPException(401, "login required")


def check_password(username, password):
    # login.json'daki hash ile karşılaştır (şifrenin kendisi hiçbir yerde saklanmaz)
    with open(LOGIN_PATH, "r", encoding="utf-8") as f:
        login = json.load(f)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(login["salt"]),
                                 login["iterations"]).hex()
    same_user = hmac.compare_digest(username.encode("utf-8"), login["username"].encode("utf-8"))
    same_password = hmac.compare_digest(digest, login["hash"])
    return same_user and same_password


@app.post("/login")
def login(response: Response, body: dict = Body()):
    if not os.path.exists(LOGIN_PATH):
        raise HTTPException(503, "login.json not found: run set_password.py on the API computer")
    if not check_password(body.get("username", ""), body.get("password", "")):
        raise HTTPException(401, "wrong username or password")
    token = secrets.token_urlsafe(32)
    SESSIONS.add(token)
    # httponly: sayfadaki JavaScript çerezi okuyamaz; secure: sadece HTTPS'te gönderilir;
    # samesite=strict: başka bir siteden gelen isteklere çerez eklenmez
    response.set_cookie("session", token, httponly=True, secure=True, samesite="strict")
    return {"ok": True}


@app.post("/logout")
def logout(response: Response, session: Optional[str] = Cookie(None)):
    SESSIONS.discard(session)
    response.delete_cookie("session", httponly=True, secure=True, samesite="strict")
    return {"ok": True}


@app.get("/me")
def me(session: Optional[str] = Cookie(None)):
    # Panel açılınca: giriş yapılmış mı?
    return {"logged_in": session in SESSIONS}


# jobs tablosunda olması gereken sütunlar. Yeni sütun eklenince buraya da yazılır;
# eski veritabanında yoksa connect() onu kendisi ekler (migration).
JOB_COLUMNS = {
    "client_id": "TEXT",
    "source": "TEXT",
    "target_type": "TEXT DEFAULT 'local'",
    "target": "TEXT",
    "schedule": "TEXT",
    "mode": "TEXT DEFAULT 'copy'",
    "compression": "TEXT DEFAULT 'none'",
    "backup_type": "TEXT DEFAULT 'full'"
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
            schedule TEXT,
            mode TEXT DEFAULT 'copy',
            compression TEXT DEFAULT 'none',
            backup_type TEXT DEFAULT 'full'
        )
    """)

    # Her makinenin son gönderdiği durum (sonuçlar ve ilerleme); makine başına tek satır
    connection.execute("""
        CREATE TABLE IF NOT EXISTS client_status (
            client_id TEXT PRIMARY KEY,
            updated_at TEXT,
            status TEXT
        )
    """)

    # Servislerin gönderdiği log satırları
    connection.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            time TEXT,
            level TEXT,
            message TEXT,
            error TEXT
        )
    """)

    # Geri yükleme istekleri: "list" (işin hedefindeki dosyaları listele) ya da "file" (bir dosyayı getir)
    # status: pending (servis bekleniyor) -> ready (hazır) / error; indirilen dosya: downloaded
    connection.execute("""
        CREATE TABLE IF NOT EXISTS restore_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            source TEXT,
            target TEXT,
            target_type TEXT,
            kind TEXT,
            path TEXT,
            status TEXT,
            files TEXT,
            error TEXT,
            created_at TEXT
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
def get_jobs(client_id: Optional[str] = None, session: Optional[str] = Cookie(None)):
    if client_id is None:
        require_login(session)   # bütün işler: sadece panel (giriş yapılmış)
    connection = connect()
    if client_id is None:
        # Arayüz: bütün makinelerin işleri
        rows = connection.execute(
            "SELECT client_id, source, target_type, target, schedule, mode, compression, backup_type FROM jobs ORDER BY id"
        ).fetchall()
    else:
        # Servis: sadece kendi makinesinin işleri
        rows = connection.execute(
            "SELECT client_id, source, target_type, target, schedule, mode, compression, backup_type FROM jobs WHERE client_id = ? ORDER BY id",
            (client_id,)
        ).fetchall()
    connection.close()
    return [{"client_id": client_id, "source": source, "target_type": target_type,
             "target": target, "schedule": json.loads(schedule), "mode": mode, "compression": compression,
             "backup_type": backup_type}
            for client_id, source, target_type, target, schedule, mode, compression, backup_type in rows]


@app.put("/jobs", dependencies=[Depends(require_login)])
def save_jobs(jobs: list = Body()):
    connection = connect()
    with connection:   # hepsi birlikte kaydedilir ya da hiçbiri
        connection.execute("DELETE FROM jobs")
        for job in jobs:
            connection.execute(
                "INSERT INTO jobs (client_id, source, target_type, target, schedule, mode, compression, backup_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job["client_id"], job["source"], job.get("target_type", "local"), job["target"],
                 json.dumps(job["schedule"]), job.get("mode", "copy"), job.get("compression", "none"),
                 job.get("backup_type", "full"))
            )
    connection.close()
    return {"saved": len(jobs)}


@app.post("/status")
def save_status(client_id: str, status: dict = Body()):
    # Servis durumunu gönderir; makinenin eski durumunun yerine yazılır
    connection = connect()
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO client_status (client_id, updated_at, status) VALUES (?, ?, ?)",
            (client_id, datetime.now().isoformat(timespec="seconds"), json.dumps(status))
        )
    connection.close()
    return {"saved": True}


@app.get("/status", dependencies=[Depends(require_login)])
def get_status():
    # Panel: bütün makinelerin son durumu
    connection = connect()
    rows = connection.execute(
        "SELECT client_id, updated_at, status FROM client_status ORDER BY client_id"
    ).fetchall()
    connection.close()
    return [{"client_id": client_id, "updated_at": updated_at, "status": json.loads(status)}
            for client_id, updated_at, status in rows]


@app.post("/logs")
def save_logs(client_id: str, entries: list = Body()):
    # Servisin yeni log satırlarını kaydet, 30 günden eskileri sil
    cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).isoformat(timespec="milliseconds")
    connection = connect()
    with connection:
        for entry in entries:
            connection.execute(
                "INSERT INTO logs (client_id, time, level, message, error) VALUES (?, ?, ?, ?, ?)",
                (client_id, entry.get("time"), entry.get("level"), entry.get("message"), entry.get("error"))
            )
        connection.execute("DELETE FROM logs WHERE time < ?", (cutoff,))
    connection.close()
    return {"saved": len(entries)}


@app.get("/logs", dependencies=[Depends(require_login)])
def get_logs(client_id: Optional[str] = None, level: Optional[str] = None, limit: int = 500):
    # Panel: en yeni log satırları; makineye ve seviyeye göre filtrelenebilir
    query = "SELECT client_id, time, level, message, error FROM logs WHERE 1 = 1"
    params = []
    if client_id:
        query += " AND client_id = ?"
        params.append(client_id)
    if level:
        query += " AND level = ?"
        params.append(level)
    query += " ORDER BY time DESC, id DESC LIMIT ?"
    params.append(limit)

    connection = connect()
    rows = connection.execute(query, params).fetchall()
    connection.close()
    return [{"client_id": client_id, "time": time, "level": level, "message": message, "error": error}
            for client_id, time, level, message, error in rows]


# ---------------- Geri yükleme ----------------
# Hedef türü ne olursa olsun aynı akış: istek tabloya yazılır, servis 5 saniyede bir sorar,
# listeyi/dosyayı hedeften alıp API'ye gönderir. API hedef türlerini bilmez, bulut token'ı tutmaz.

def find_job(client_id, source, target):
    connection = connect()
    row = connection.execute(
        "SELECT target_type FROM jobs WHERE client_id = ? AND source = ? AND target = ?",
        (client_id, source, target)
    ).fetchone()
    connection.close()
    if row is None:
        raise HTTPException(404, "job not found")
    return row[0] or "local"


def create_restore(job, kind, path=None):
    # İsteği kaydeder; servis alıp yerine getirene kadar "pending" kalır
    target_type = find_job(job["client_id"], job["source"], job["target"])
    status, files, error = "pending", None, None

    connection = connect()
    with connection:
        cursor = connection.execute(
            "INSERT INTO restore_requests (client_id, source, target, target_type, kind, path, status, files, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job["client_id"], job["source"], job["target"], target_type, kind, path, status, files, error,
             datetime.now().isoformat(timespec="seconds"))
        )
    connection.close()
    return {"id": cursor.lastrowid}


def get_restore(request_id):
    connection = connect()
    row = connection.execute(
        "SELECT id, client_id, target, target_type, kind, path, status, files, error FROM restore_requests WHERE id = ?",
        (request_id,)
    ).fetchone()
    connection.close()
    if row is None:
        raise HTTPException(404, "restore request not found")
    keys = ("id", "client_id", "target", "target_type", "kind", "path", "status", "files", "error")
    result = dict(zip(keys, row))
    result["files"] = json.loads(result["files"]) if result["files"] else None
    return result


def set_restore(request_id, status, files=None, error=None):
    connection = connect()
    with connection:
        connection.execute(
            "UPDATE restore_requests SET status = ?, files = ?, error = ? WHERE id = ?",
            (status, json.dumps(files) if files is not None else None, error, request_id)
        )
    connection.close()


# Panel
@app.post("/restore/list", dependencies=[Depends(require_login)])
def restore_list(job: dict = Body()):
    # Body: {"client_id", "source", "target"} -> işin hedefindeki dosyaları listeleme isteği
    return create_restore(job, "list")


@app.post("/restore/file", dependencies=[Depends(require_login)])
def restore_file(job: dict = Body()):
    # Body: {"client_id", "source", "target", "path"} -> tek bir dosyayı getirme isteği
    return create_restore(job, "file", job["path"])


@app.get("/restore/requests/{request_id}", dependencies=[Depends(require_login)])
def restore_status(request_id: int):
    # Panel isteğin durumunu buradan takip eder
    return get_restore(request_id)


@app.get("/restore/download/{request_id}", dependencies=[Depends(require_login)])
def restore_download(request_id: int):
    r = get_restore(request_id)
    if r["kind"] != "file" or r["status"] != "ready":
        raise HTTPException(409, "file is not ready")
    name = r["path"].split("/")[-1]

    # Servisin gönderdiği dosyayı ver, gönderim bitince sil
    path = os.path.join(RESTORE_DIR, str(request_id))
    set_restore(request_id, "downloaded")
    return FileResponse(path, filename=name, background=BackgroundTask(os.remove, path))


# Servis
@app.get("/restore/pending")
def restore_pending(client_id: str):
    # Servis: bu makinenin bekleyen istekleri
    connection = connect()
    rows = connection.execute(
        "SELECT id, kind, target, target_type, path FROM restore_requests WHERE client_id = ? AND status = 'pending' ORDER BY id",
        (client_id,)
    ).fetchall()
    connection.close()
    return [{"id": id, "kind": kind, "target": target, "target_type": target_type, "path": path}
            for id, kind, target, target_type, path in rows]


@app.post("/restore/requests/{request_id}/list")
def restore_list_done(request_id: int, files: list = Body()):
    # Servis: istenen dosya listesini gönderdi
    set_restore(request_id, "ready", files=files)
    return {"saved": len(files)}


@app.post("/restore/requests/{request_id}/file")
async def restore_file_done(request_id: int, request: Request):
    # Servis: istenen dosyanın içeriğini gönderdi; indirilene kadar restore_tmp'de durur
    os.makedirs(RESTORE_DIR, exist_ok=True)
    with open(os.path.join(RESTORE_DIR, str(request_id)), "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    set_restore(request_id, "ready")
    return {"saved": True}


@app.post("/restore/requests/{request_id}/error")
def restore_error(request_id: int, body: dict = Body()):
    # Servis: isteği yerine getiremedi
    set_restore(request_id, "error", error=body.get("error"))
    return {"saved": True}