import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import asynccontextmanager
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

LOG_RETENTION_DAYS = 30   # API'de kaç günlük log tutulacak
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.db")
WEB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "yedekleme-isleri.html")
RESTORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restore_tmp")   # servisten gelen dosyalar, indirilene kadar
PASSWORD_ITERATIONS = 600_000   # şifre hash'i kaç tur hesaplanır: tahmin denemelerini yavaşlatır (create_account.py de kullanır)
PASSWORD_MIN_LENGTH = 8         # yeni şifre en az kaç karakter olmalı (create_account.py de kullanır)

@asynccontextmanager
async def lifespan(app):
    # API başlayınca: ulaşılamayan makineleri dakikada bir kontrol eden arka plan görevi
    threading.Thread(target=offline_loop, daemon=True).start()
    yield


# CORS yok: panel API'nin kendi adresinden açılıyor, başka sitelerin sayfaları API'nin cevaplarını okuyamaz
app = FastAPI(lifespan=lifespan)


@app.get("/")
def web():
    # Tarayıcıda API'nin adresi açılınca arayüzü gönder
    return FileResponse(WEB_PATH)


# ---------------- Giriş (login) ----------------
# Giriş yapılınca rastgele bir oturum anahtarı üretilir ve tarayıcıya çerez olarak verilir.
# Oturumlar hafızada tutulur: API yeniden başlarsa tekrar giriş yapmak gerekir.
SESSIONS = {}   # oturum anahtarı -> giriş yapan hesabın id'si


def require_login(session: Optional[str] = Cookie(None)):
    # Panelin kullandığı adresler: geçerli oturum çerezi yoksa 401. Varsa hesabın id'si döner.
    if session not in SESSIONS:
        raise HTTPException(401, "login required")
    return SESSIONS[session]


def check_password(username, password):
    # accounts tablosundaki hash ile karşılaştır (şifrenin kendisi hiçbir yerde saklanmaz).
    # Doğruysa hesabın id'si, değilse None döner.
    connection = connect()
    row = connection.execute(
        "SELECT id, salt, iterations, hash FROM accounts WHERE username = ?", (username,)
    ).fetchone()
    connection.close()
    if row is None:
        # Kullanıcı adı yoksa da aynı hesaplama yapılır: cevap süresinden kullanıcı adının var olup olmadığı anlaşılmasın
        hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes(16), PASSWORD_ITERATIONS)
        return None
    account_id, salt, iterations, stored_hash = row
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
    return account_id if hmac.compare_digest(digest, stored_hash) else None


# Giriş deneme sınırı: aynı IP'den aynı kullanıcı adına 10 dakikada 5 yanlış deneme -> o IP o hesap için 10 dakika bekler
LOGIN_MAX_FAILS = 5
LOGIN_FAIL_WINDOW = timedelta(minutes=10)
LOGIN_BLOCK = timedelta(minutes=10)
LOGIN_FAILS = {}     # (IP, kullanıcı adı) -> yanlış denemelerin zamanları (hafızada)
LOGIN_BLOCKED = {}   # (IP, kullanıcı adı) -> engelin biteceği zaman (hafızada)
# Şifre değiştirmede mevcut şifre için de aynı sınır, hesaba göre sayılır
PASSWORD_FAILS = {}    # hesap id'si -> yanlış mevcut şifre denemelerinin zamanları (hafızada)
PASSWORD_BLOCKED = {}  # hesap id'si -> engelin biteceği zaman (hafızada)


@app.post("/login")
def login(request: Request, response: Response, body: dict = Body()):
    connection = connect()
    has_account = connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
    connection.close()
    if not has_account:
        raise HTTPException(503, "no account yet: run create_account.py on the API computer")
    username = body.get("username", "")
    key = (request.client.host if request.client else "", username)
    now = datetime.now()
    if LOGIN_BLOCKED.get(key, now) > now:
        raise HTTPException(429, "too many wrong attempts, try again later")
    account_id = check_password(username, body.get("password", ""))
    if account_id is None:
        fails = [t for t in LOGIN_FAILS.get(key, []) if now - t < LOGIN_FAIL_WINDOW] + [now]
        if len(fails) >= LOGIN_MAX_FAILS:
            LOGIN_BLOCKED[key] = now + LOGIN_BLOCK
            fails = []
        LOGIN_FAILS[key] = fails
        raise HTTPException(401, "wrong username or password")
    LOGIN_FAILS.pop(key, None)   # doğru giriş: o IP'nin o hesap için yanlış deneme sayacı sıfırlanır
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = account_id
    # httponly: sayfadaki JavaScript çerezi okuyamaz; secure: sadece HTTPS'te gönderilir;
    # samesite=strict: başka bir siteden gelen isteklere çerez eklenmez
    response.set_cookie("session", token, httponly=True, secure=True, samesite="strict")
    return {"ok": True}


@app.post("/logout")
def logout(response: Response, session: Optional[str] = Cookie(None)):
    SESSIONS.pop(session, None)
    response.delete_cookie("session", httponly=True, secure=True, samesite="strict")
    return {"ok": True}


@app.get("/me")
def me(session: Optional[str] = Cookie(None)):
    # Panel açılınca: giriş yapılmış mı?
    return {"logged_in": session in SESSIONS}


# jobs tablosunda olması gereken sütunlar. Yeni sütun eklenince buraya da yazılır;
# eski veritabanında yoksa connect() onu kendisi ekler (migration).
JOB_COLUMNS = {
    "machine_id": "TEXT",
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
            machine_id TEXT,
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
        CREATE TABLE IF NOT EXISTS machine_status (
            machine_id TEXT PRIMARY KEY,
            updated_at TEXT,
            status TEXT
        )
    """)

    # Servislerin gönderdiği log satırları
    connection.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT,
            account_id INTEGER,
            time TEXT,
            level TEXT,
            message TEXT,
            error TEXT
        )
    """)

    # Panel hesapları: şifrenin kendisi saklanmaz, rastgele salt ile PBKDF2 hash'i saklanır (create_account.py ekler)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            salt TEXT NOT NULL,
            iterations INTEGER NOT NULL,
            hash TEXT NOT NULL,
            created_at TEXT,
            email TEXT
        )
    """)

    # Açık sorunlar: her sorun bir kere açılır (bir mail), düzelince silinir (bir mail).
    # job: "kaynak -> hedef" (makinenin kendisiyle ilgili sorunda boş)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS problems (
            machine_id TEXT,
            kind TEXT,
            job TEXT,
            opened_at TEXT,
            PRIMARY KEY (machine_id, kind, job)
        )
    """)

    # İşlerin en son ne zaman başarılı olduğu ("uzun süredir başarılı olmadı" bildirimi için)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS job_health (
            machine_id TEXT,
            job TEXT,
            first_seen TEXT,
            last_success TEXT,
            last_finished TEXT,
            PRIMARY KEY (machine_id, job)
        )
    """)

    # Makineler: her servis ilk açılışta kaydolur, bir kimlik (id) ve gizli anahtar alır.
    # Gizli anahtarın kendisi saklanmaz, sadece SHA-256 hash'i saklanır.
    # account_id: makinenin bağlı olduğu hesap (eşleştirme kodu panele girilene kadar boş).
    # Aynı hesapta iki makine aynı adı alamaz; farklı hesaplar aynı adı kullanabilir.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS machines (
            id TEXT PRIMARY KEY,
            secret_hash TEXT NOT NULL,
            name TEXT,
            created_at TEXT,
            account_id INTEGER,
            pair_code TEXT UNIQUE,
            pair_expires TEXT,
            UNIQUE (account_id, name)
        )
    """)

    # Geri yükleme istekleri: "list" (işin hedefindeki dosyaları listele) ya da "file" (bir dosyayı getir)
    # status: pending (servis bekleniyor) -> ready (hazır) / error; indirilen dosya: downloaded
    connection.execute("""
        CREATE TABLE IF NOT EXISTS restore_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT,
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
    if "email" not in [row[1] for row in connection.execute("PRAGMA table_info(accounts)")]:
        connection.execute("ALTER TABLE accounts ADD COLUMN email TEXT")
    connection.commit()

    return connection


# ---------------- Makineler ----------------
@app.post("/machines/register")
def register_machine(body: dict = Body()):
    # Servis ilk açılışta buraya gelir: makineye yeni bir id ve gizli anahtar verilir.
    # Gizli anahtar sadece bu cevapta bir kere gönderilir; servis onu machine.json'a yazar.
    name = body.get("name")
    if not isinstance(name, str):
        raise HTTPException(400, "name must be text")
    machine_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(32)
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    connection = connect()
    with connection:
        connection.execute(
            "INSERT INTO machines (id, secret_hash, name, created_at) VALUES (?, ?, ?, ?)",
            (machine_id, secret_hash, name.strip(), datetime.now().isoformat(timespec="seconds"))
        )
    connection.close()
    return {"id": machine_id, "secret": secret}


def require_machine(x_machine_id: Optional[str] = Header(None), x_machine_secret: Optional[str] = Header(None)):
    # Servisin kullandığı adresler: makine id'si ve gizli anahtarı başlıklarda gelir.
    # Gizli anahtarın hash'i DB'dekiyle aynı değilse 401. Doğruysa makinenin id'si döner.
    if not x_machine_id or not x_machine_secret:
        raise HTTPException(401, "machine id and secret required")
    connection = connect()
    row = connection.execute("SELECT secret_hash FROM machines WHERE id = ?", (x_machine_id,)).fetchone()
    connection.close()
    digest = hashlib.sha256(x_machine_secret.encode("utf-8")).hexdigest()
    if row is None or not hmac.compare_digest(digest, row[0]):
        raise HTTPException(401, "wrong machine id or secret")
    return x_machine_id


# Eşleştirme kodu: 8 karakter, karışan karakterler (0/O, 1/I) yok; ekranda XXXX-XXXX gösterilir
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_LIFETIME = timedelta(hours=24)          # kod 24 saat içinde girilmezse geçersiz olur
PAIR_MAX_FAILS = 5                           # bir hesap 10 dakikada 5 kez yanlış kod girerse
PAIR_FAIL_WINDOW = timedelta(minutes=10)
PAIR_BLOCK = timedelta(minutes=10)           # 10 dakika boyunca kod giremez
PAIR_FAILS = {}                              # hesap id'si -> yanlış denemelerin zamanları (hafızada)
PAIR_BLOCKED = {}                            # hesap id'si -> engelin biteceği zaman (hafızada)


def now_utc():
    return datetime.now(timezone.utc)


def add_event_log(connection, machine_id, account_id, message):
    # Panelde yapılan makine işlemleri (eklendi / çıkarıldı) o makinenin loglarına yazılır
    connection.execute(
        "INSERT INTO logs (machine_id, account_id, time, level, message, error) VALUES (?, ?, ?, ?, ?, ?)",
        (machine_id, account_id, datetime.now().isoformat(timespec="milliseconds"), "INFO", message, None)
    )


@app.get("/machines/me")
def machine_me(machine_id: str = Depends(require_machine)):
    # Servis: bir hesaba bağlı mıyım? Bağlı değilse geçerli eşleştirme kodu (süresi dolmuşsa yenisi üretilir)
    connection = connect()
    account_id, code, expires = connection.execute(
        "SELECT account_id, pair_code, pair_expires FROM machines WHERE id = ?", (machine_id,)
    ).fetchone()
    if account_id is not None:
        connection.close()
        return {"paired": True}
    if code is None or datetime.fromisoformat(expires) <= now_utc():
        expires = (now_utc() + PAIR_LIFETIME).isoformat(timespec="seconds")
        while True:
            code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
            try:
                with connection:
                    connection.execute("UPDATE machines SET pair_code = ?, pair_expires = ? WHERE id = ?",
                                       (code, expires, machine_id))
                break
            except sqlite3.IntegrityError:
                continue   # aynı kod başka makinede varsa yenisini üret
    connection.close()
    return {"paired": False, "pair_code": code[:4] + "-" + code[4:], "pair_expires": expires}


@app.get("/machines")
def get_machines(account_id: int = Depends(require_login)):
    # Panel: bu hesabın makineleri (id ve adı)
    connection = connect()
    rows = connection.execute("SELECT id, name FROM machines WHERE account_id = ? ORDER BY name, id",
                              (account_id,)).fetchall()
    connection.close()
    return [{"id": id, "name": name} for id, name in rows]


def machine_name_or_400(body):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(400, "name is required")
    return name.strip()


@app.post("/machines/pair")
def pair_machine(body: dict = Body(), account_id: int = Depends(require_login)):
    # Panel: Body {"code", "name"} -> kodun ait olduğu makineyi bu hesaba bağla
    now = now_utc()
    if PAIR_BLOCKED.get(account_id, now) > now:
        raise HTTPException(429, "too many wrong codes, try again later")
    name = machine_name_or_400(body)
    code = str(body.get("code", "")).upper().replace("-", "").replace(" ", "")

    connection = connect()
    row = connection.execute(
        "SELECT id, pair_expires FROM machines WHERE pair_code = ? AND account_id IS NULL", (code,)
    ).fetchone()
    if row is None or datetime.fromisoformat(row[1]) <= now:
        connection.close()
        fails = [t for t in PAIR_FAILS.get(account_id, []) if now - t < PAIR_FAIL_WINDOW] + [now]
        if len(fails) >= PAIR_MAX_FAILS:
            PAIR_BLOCKED[account_id] = now + PAIR_BLOCK
            fails = []
        PAIR_FAILS[account_id] = fails
        raise HTTPException(400, "wrong or expired code")

    machine_id = row[0]
    try:
        with connection:
            connection.execute(
                "UPDATE machines SET account_id = ?, name = ?, pair_code = NULL, pair_expires = NULL WHERE id = ?",
                (account_id, name, machine_id)
            )
            # Eşleşmeden önce gelen loglar da artık bu hesapta görünsün
            connection.execute("UPDATE logs SET account_id = ? WHERE machine_id = ? AND account_id IS NULL",
                               (account_id, machine_id))
            add_event_log(connection, machine_id, account_id, "Makine hesaba eklendi: " + name)
            notify(connection, account_id, machine_id, name, "added")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "a machine with this name already exists in your account")
    finally:
        connection.close()
    return {"id": machine_id, "name": name}


def own_machine(connection, machine_id, account_id):
    # Makine bu hesabın değilse 404
    row = connection.execute("SELECT name FROM machines WHERE id = ? AND account_id = ?",
                             (machine_id, account_id)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(404, "machine not found")
    return row[0]


@app.put("/machines/{machine_id}/name")
def rename_machine(machine_id: str, body: dict = Body(), account_id: int = Depends(require_login)):
    # Panel: makinenin adını değiştir (aynı hesapta aynı ad olamaz)
    name = machine_name_or_400(body)
    connection = connect()
    own_machine(connection, machine_id, account_id)
    try:
        with connection:
            connection.execute("UPDATE machines SET name = ? WHERE id = ?", (name, machine_id))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "a machine with this name already exists in your account")
    finally:
        connection.close()
    return {"id": machine_id, "name": name}


@app.delete("/machines/{machine_id}")
def remove_machine(machine_id: str, account_id: int = Depends(require_login)):
    # Panel: makineyi hesaptan çıkar. Makinenin işleri silinir; logları hesapta kalır.
    # Makine kaydı silinmez: servis yeni bir eşleştirme kodu alır, istenirse tekrar eklenebilir.
    connection = connect()
    name = own_machine(connection, machine_id, account_id)
    with connection:
        connection.execute("DELETE FROM jobs WHERE machine_id = ?", (machine_id,))
        connection.execute(
            "UPDATE machines SET account_id = NULL, pair_code = NULL, pair_expires = NULL WHERE id = ?",
            (machine_id,)
        )
        add_event_log(connection, machine_id, account_id, "Makine hesaptan çıkarıldı: " + name)
        notify(connection, account_id, machine_id, name, "removed")
        # Makinenin açık sorunları ve iş kayıtları sessizce silinir (hesapta değil artık)
        connection.execute("DELETE FROM problems WHERE machine_id = ?", (machine_id,))
        connection.execute("DELETE FROM job_health WHERE machine_id = ?", (machine_id,))
    connection.close()
    return {"removed": True}


@app.get("/jobs")
def get_jobs(session: Optional[str] = Cookie(None), x_machine_id: Optional[str] = Header(None),
             x_machine_secret: Optional[str] = Header(None)):
    # Kimlik başlıklarıyla gelen servis: sadece kendi işleri. Başlıksız gelen panel: giriş gerekli, hesabın makinelerinin işleri.
    if x_machine_id is not None:
        machine_id = require_machine(x_machine_id, x_machine_secret)
    else:
        account_id = require_login(session)
        machine_id = None
    connection = connect()
    if machine_id is None:
        rows = connection.execute(
            "SELECT machine_id, source, target_type, target, schedule, mode, compression, backup_type FROM jobs"
            " WHERE machine_id IN (SELECT id FROM machines WHERE account_id = ?) ORDER BY id",
            (account_id,)
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT machine_id, source, target_type, target, schedule, mode, compression, backup_type FROM jobs WHERE machine_id = ? ORDER BY id",
            (machine_id,)
        ).fetchall()
    connection.close()
    return [{"machine_id": machine_id, "source": source, "target_type": target_type,
             "target": target, "schedule": json.loads(schedule), "mode": mode, "compression": compression,
             "backup_type": backup_type}
            for machine_id, source, target_type, target, schedule, mode, compression, backup_type in rows]


@app.put("/jobs")
def save_jobs(jobs: list = Body(), account_id: int = Depends(require_login)):
    # Panel: bu hesabın bütün işleri gönderilir; sadece bu hesabın makinelerinin işleri değişir
    connection = connect()
    own = {row[0] for row in connection.execute("SELECT id FROM machines WHERE account_id = ?", (account_id,))}
    if any(job.get("machine_id") not in own for job in jobs):
        connection.close()
        raise HTTPException(400, "job machine is not in your account")
    with connection:   # hepsi birlikte kaydedilir ya da hiçbiri
        connection.execute("DELETE FROM jobs WHERE machine_id IN (SELECT id FROM machines WHERE account_id = ?)",
                           (account_id,))
        for job in jobs:
            connection.execute(
                "INSERT INTO jobs (machine_id, source, target_type, target, schedule, mode, compression, backup_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job["machine_id"], job["source"], job.get("target_type", "local"), job["target"],
                 json.dumps(job["schedule"]), job.get("mode", "copy"), job.get("compression", "none"),
                 job.get("backup_type", "full"))
            )
    connection.close()
    return {"saved": len(jobs)}


@app.post("/status")
def save_status(status: dict = Body(), machine_id: str = Depends(require_machine)):
    # Servis durumunu gönderir; makinenin eski durumunun yerine yazılır
    connection = connect()
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO machine_status (machine_id, updated_at, status) VALUES (?, ?, ?)",
            (machine_id, datetime.now().isoformat(timespec="seconds"), json.dumps(status))
        )
        check_status(connection, machine_id, status)   # sorun açıldı / düzeldi mi? (bildirim)
    connection.close()
    return {"saved": True}


@app.get("/status")
def get_status(account_id: int = Depends(require_login)):
    # Panel: bu hesabın makinelerinin son durumu
    connection = connect()
    rows = connection.execute(
        "SELECT machine_id, updated_at, status FROM machine_status"
        " WHERE machine_id IN (SELECT id FROM machines WHERE account_id = ?) ORDER BY machine_id",
        (account_id,)
    ).fetchall()
    connection.close()
    return [{"machine_id": machine_id, "updated_at": updated_at, "status": json.loads(status)}
            for machine_id, updated_at, status in rows]


@app.post("/logs")
def save_logs(entries: list = Body(), machine_id: str = Depends(require_machine)):
    # Servisin yeni log satırlarını kaydet, 30 günden eskileri sil
    cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).isoformat(timespec="milliseconds")
    # Log, makinenin o an bağlı olduğu hesaba yazılır (bağlı değilse boş; eşleşince hesaba geçer)
    connection = connect()
    account_id = connection.execute("SELECT account_id FROM machines WHERE id = ?", (machine_id,)).fetchone()[0]
    with connection:
        for entry in entries:
            connection.execute(
                "INSERT INTO logs (machine_id, account_id, time, level, message, error) VALUES (?, ?, ?, ?, ?, ?)",
                (machine_id, account_id, entry.get("time"), entry.get("level"), entry.get("message"), entry.get("error"))
            )
        connection.execute("DELETE FROM logs WHERE time < ?", (cutoff,))
    connection.close()
    return {"saved": len(entries)}


@app.get("/logs")
def get_logs(machine_id: Optional[str] = None, level: Optional[str] = None, limit: int = 500,
             account_id: int = Depends(require_login)):
    # Panel: bu hesabın en yeni log satırları; makineye ve seviyeye göre filtrelenebilir.
    # Makinenin adı da gelir: hesaptan çıkarılmış makinenin eski logları da adıyla görünsün.
    query = ("SELECT logs.machine_id, machines.name, logs.time, logs.level, logs.message, logs.error"
             " FROM logs LEFT JOIN machines ON machines.id = logs.machine_id WHERE logs.account_id = ?")
    params = [account_id]
    if machine_id:
        query += " AND logs.machine_id = ?"
        params.append(machine_id)
    if level:
        query += " AND logs.level = ?"
        params.append(level)
    query += " ORDER BY logs.time DESC, logs.id DESC LIMIT ?"
    params.append(limit)

    connection = connect()
    rows = connection.execute(query, params).fetchall()
    connection.close()
    return [{"machine_id": machine_id, "machine_name": name, "time": time, "level": level,
             "message": message, "error": error}
            for machine_id, name, time, level, message, error in rows]


# ---------------- Geri yükleme ----------------
# Hedef türü ne olursa olsun aynı akış: istek tabloya yazılır, servis 5 saniyede bir sorar,
# listeyi/dosyayı hedeften alıp API'ye gönderir. API hedef türlerini bilmez, bulut token'ı tutmaz.

def find_job(machine_id, source, target, account_id):
    # İş bu hesabın bir makinesine ait olmalı
    connection = connect()
    row = connection.execute(
        "SELECT target_type FROM jobs WHERE machine_id = ? AND source = ? AND target = ?"
        " AND machine_id IN (SELECT id FROM machines WHERE account_id = ?)",
        (machine_id, source, target, account_id)
    ).fetchone()
    connection.close()
    if row is None:
        raise HTTPException(404, "job not found")
    return row[0] or "local"


def create_restore(job, kind, account_id, path=None):
    # İsteği kaydeder; servis alıp yerine getirene kadar "pending" kalır
    target_type = find_job(job["machine_id"], job["source"], job["target"], account_id)
    status, files, error = "pending", None, None

    connection = connect()
    with connection:
        cursor = connection.execute(
            "INSERT INTO restore_requests (machine_id, source, target, target_type, kind, path, status, files, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job["machine_id"], job["source"], job["target"], target_type, kind, path, status, files, error,
             datetime.now().isoformat(timespec="seconds"))
        )
    connection.close()
    return {"id": cursor.lastrowid}


def get_restore(request_id):
    connection = connect()
    row = connection.execute(
        "SELECT id, machine_id, target, target_type, kind, path, status, files, error FROM restore_requests WHERE id = ?",
        (request_id,)
    ).fetchone()
    connection.close()
    if row is None:
        raise HTTPException(404, "restore request not found")
    keys = ("id", "machine_id", "target", "target_type", "kind", "path", "status", "files", "error")
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
def account_restore(request_id, account_id):
    # Panel sadece kendi hesabının makinelerine ait isteği görebilir
    r = get_restore(request_id)
    connection = connect()
    row = connection.execute("SELECT 1 FROM machines WHERE id = ? AND account_id = ?",
                             (r["machine_id"], account_id)).fetchone()
    connection.close()
    if row is None:
        raise HTTPException(404, "restore request not found")
    return r


@app.post("/restore/list")
def restore_list(job: dict = Body(), account_id: int = Depends(require_login)):
    # Body: {"machine_id", "source", "target"} -> işin hedefindeki dosyaları listeleme isteği
    return create_restore(job, "list", account_id)


@app.post("/restore/file")
def restore_file(job: dict = Body(), account_id: int = Depends(require_login)):
    # Body: {"machine_id", "source", "target", "path"} -> tek bir dosyayı getirme isteği
    return create_restore(job, "file", account_id, job["path"])


@app.get("/restore/requests/{request_id}")
def restore_status(request_id: int, account_id: int = Depends(require_login)):
    # Panel isteğin durumunu buradan takip eder
    return account_restore(request_id, account_id)


@app.get("/restore/download/{request_id}")
def restore_download(request_id: int, account_id: int = Depends(require_login)):
    r = account_restore(request_id, account_id)
    if r["kind"] != "file" or r["status"] != "ready":
        raise HTTPException(409, "file is not ready")
    name = r["path"].split("/")[-1]

    # Servisin gönderdiği dosyayı ver, gönderim bitince sil
    path = os.path.join(RESTORE_DIR, str(request_id))
    set_restore(request_id, "downloaded")
    return FileResponse(path, filename=name, background=BackgroundTask(os.remove, path))


# Servis
def own_request(request_id, machine_id):
    # Servis sadece kendi makinesine ait isteğe cevap verebilir
    if get_restore(request_id)["machine_id"] != machine_id:
        raise HTTPException(403, "restore request belongs to another machine")


@app.get("/restore/pending")
def restore_pending(machine_id: str = Depends(require_machine)):
    # Servis: bu makinenin bekleyen istekleri
    connection = connect()
    rows = connection.execute(
        "SELECT id, kind, target, target_type, path FROM restore_requests WHERE machine_id = ? AND status = 'pending' ORDER BY id",
        (machine_id,)
    ).fetchall()
    connection.close()
    return [{"id": id, "kind": kind, "target": target, "target_type": target_type, "path": path}
            for id, kind, target, target_type, path in rows]


@app.post("/restore/requests/{request_id}/list")
def restore_list_done(request_id: int, files: list = Body(), machine_id: str = Depends(require_machine)):
    # Servis: istenen dosya listesini gönderdi
    own_request(request_id, machine_id)
    set_restore(request_id, "ready", files=files)
    return {"saved": len(files)}


@app.post("/restore/requests/{request_id}/file")
async def restore_file_done(request_id: int, request: Request, machine_id: str = Depends(require_machine)):
    # Servis: istenen dosyanın içeriğini gönderdi; indirilene kadar restore_tmp'de durur
    own_request(request_id, machine_id)
    os.makedirs(RESTORE_DIR, exist_ok=True)
    with open(os.path.join(RESTORE_DIR, str(request_id)), "wb") as f:
        async for chunk in request.stream():
            f.write(chunk)
    set_restore(request_id, "ready")
    return {"saved": True}


@app.post("/restore/requests/{request_id}/error")
def restore_error(request_id: int, body: dict = Body(), machine_id: str = Depends(require_machine)):
    # Servis: isteği yerine getiremedi
    own_request(request_id, machine_id)
    set_restore(request_id, "error", error=body.get("error"))
    return {"saved": True}


# ---------------- Bildirimler (e-posta) ----------------
# Mailler hesabın Ayarlar'da girdiği adrese gider; adres boşsa mail gönderilmez.
# Gönderim bilgileri (SMTP) api klasöründeki smtp.json'dadır; dosya yoksa mail gönderilmez.
# Her sorun bir kere açılır (problems tablosu): açılınca bir mail, düzelince bir mail.
SMTP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smtp.json")
OFFLINE_AFTER = timedelta(minutes=10)    # bu kadar süre haber gelmeyen makine "ulaşılamıyor"
OFFLINE_CHECK_SECONDS = 60               # ulaşılamayan makineler kaç saniyede bir kontrol edilir
NO_SUCCESS_DAYS = 3                      # bir iş bu kadar gündür hiç başarılı olmadıysa (seyrek işlerde aralığın 2 katı)
DISK_ERROR = "not enough free space on target"   # servisin "en az X MB boş kalsın" kuralına takılınca yazdığı hata
log = logging.getLogger("uvicorn.error")         # API'nin kendi log'u (api-service.err.log)

# Mail türleri: renk (sorun: gri, olumlu: mavi), başlık, açıklama cümlesi, "Ne yapmalı?" önerisi.
# "_ok" ile bitenler sorunun düzeldiğini bildirir.
MAIL_GRAY = "#4b5563"
MAIL_BLUE = "#1d5fa8"
MAIL_TYPES = {
    "offline": (MAIL_GRAY, "Makine ulaşılamıyor",
                "{m} makinesinden 10 dakikadır haber alınamıyor.",
                "Makinenin açık ve internete bağlı olduğunu, yedekleme servisinin çalıştığını kontrol edin."),
    "offline_ok": (MAIL_BLUE, "Makine tekrar çevrimiçi",
                   "{m} makinesi tekrar bağlandı.", None),
    "job_failed": (MAIL_GRAY, "Yedekleme başarısız",
                   "{m} makinesindeki yedekleme işi, tüm yeniden denemelerden sonra da tamamlanamadı.",
                   "Sebebi kontrol edin. İş bir sonraki zamanında tekrar çalışacak."),
    "job_failed_ok": (MAIL_BLUE, "Yedekleme tekrar başarılı",
                      "{m} makinesindeki yedekleme işi tekrar başarıyla tamamlandı.", None),
    "no_access": (MAIL_GRAY, "Yedekleme başlayamadı",
                  "{m} makinesindeki yedekleme işi başlayamadı: kaynağa ya da hedefe erişilemiyor.",
                  "Kaynak klasörün var olduğunu ve hedefe erişilebildiğini kontrol edin. Hedef pCloud ya da "
                  "Google Drive ise hesabın bağlı olduğunu kontrol edin. Servis erişimi düzenli olarak tekrar dener."),
    "no_access_ok": (MAIL_BLUE, "Yedekleme tekrar başlayabiliyor",
                     "{m} makinesindeki yedekleme işi için kaynağa ve hedefe tekrar erişilebiliyor.", None),
    "disk_full": (MAIL_GRAY, "Hedef disk dolu",
                  "{m} makinesindeki yedekleme işinin hedef diskinde yeterli boş alan kalmadı.",
                  "Hedef diskte yer açın ya da işe başka bir hedef seçin."),
    "disk_full_ok": (MAIL_BLUE, "Hedef diskte yer açıldı",
                     "{m} makinesindeki yedekleme işi hedef diske tekrar yazabiliyor.", None),
    "no_success": (MAIL_GRAY, "Yedekleme uzun süredir başarılı olmadı",
                   "{m} makinesindeki yedekleme işi {days} gündür başarıyla tamamlanmadı.",
                   "Panelde bu makinenin loglarını kontrol edin."),
    "no_success_ok": (MAIL_BLUE, "Yedekleme tekrar başarılı",
                      "{m} makinesindeki yedekleme işi tekrar başarıyla tamamlandı.", None),
    "added": (MAIL_BLUE, "Makine hesaba eklendi",
              "{m} makinesi hesabınıza eklendi.", None),
    "removed": (MAIL_BLUE, "Makine hesaptan çıkarıldı",
                "{m} makinesi hesabınızdan çıkarıldı. Bu makinenin işleri silindi.", None),
}
TARGET_LABELS = {"local": "Yerel", "pcloud": "pCloud", "gdrive": "Google Drive"}

# Servisin İngilizce hata mesajlarının Türkçesi: (İngilizce başlangıç, Türkçe karşılığı).
# Mesajın geri kalanı (dosya yolu, ayrıntı) olduğu gibi eklenir.
ERROR_TEXTS = [
    ("source folder not found", "Kaynak klasör bulunamadı"),
    ("source is not a folder", "Kaynak bir klasör değil"),
    ("source folder cannot be listed", "Kaynak klasörün içi okunamadı"),
    ("source files cannot be read", "Kaynak dosyalar okunamadı"),
    ("target folder cannot be created", "Hedef klasör oluşturulamadı"),
    ("no write permission on target", "Hedefe yazma izni yok"),
    ("Source changed during backup, will be retried", "Dosya yedekleme sırasında değişti, tekrar denenecek"),
    ("Zip verification failed", "Zip doğrulaması başarısız"),
    ("pCloud verification failed", "pCloud doğrulaması başarısız"),
    ("Google Drive verification failed", "Google Drive doğrulaması başarısız"),
    ("Verification failed", "Doğrulama başarısız"),
    ("pcloud token file not found", "pCloud token dosyası bulunamadı"),
    ("pcloud support is not ready (pcloud_storage.py or requests package missing)",
     "pCloud desteği hazır değil (pcloud_storage.py ya da requests paketi eksik)"),
    ("Google Drive token file not found (run 'python drive_storage.py' once)",
     "Google Drive token dosyası bulunamadı (servis klasöründe bir kere 'python drive_storage.py' çalıştırılmalı)"),
    ("Google Drive support is not ready (drive_storage.py or Google packages missing)",
     "Google Drive desteği hazır değil (drive_storage.py ya da Google paketleri eksik)"),
    ("Google Drive token is missing or invalid: run 'python drive_storage.py' once in the service folder",
     "Google Drive token'ı eksik ya da geçersiz: servis klasöründe bir kere 'python drive_storage.py' çalıştırılmalı"),
]
# İşletim sisteminden gelen sık hatalar: [Errno numara] -> Türkçesi
ERRNO_TEXTS = {2: "Dosya ya da klasör bulunamadı", 5: "Giriş/çıkış hatası", 13: "Erişim izni yok",
               28: "Diskte yer kalmadı", 30: "Disk salt okunur"}
DISK_PATTERN = re.compile(r"not enough free space on target: need (\d+) MB \+ (\d+) MB reserve, free (\d+) MB")
ERRNO_PATTERN = re.compile(r"\[Errno (\d+)\] [^:]*(: .*)?$")


def turkish_error(text):
    # Servisin hata mesajını Türkçeye çevirir; listede olmayan kısım olduğu gibi kalır
    if not text:
        return text
    match = DISK_PATTERN.search(text)
    if match:
        return ("Hedef diskte yeterli boş alan yok: %s MB gerekli, %s MB her zaman boş bırakılıyor, boş alan %s MB"
                % match.groups())
    for english, turkish in ERROR_TEXTS:
        if text.startswith(english):
            text = turkish + text[len(english):]
            break
    match = ERRNO_PATTERN.search(text)
    if match and int(match.group(1)) in ERRNO_TEXTS:
        text = text[:match.start()] + ERRNO_TEXTS[int(match.group(1))] + (match.group(2) or "")
    return text


def format_duration(seconds):
    # 13860 -> "3 saat 51 dakika"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [("%d gün" % days) if days else "", ("%d saat" % hours) if hours else "",
             ("%d dakika" % minutes) if minutes and not days else ""]
    return " ".join(p for p in parts if p) or "1 dakikadan az"


def build_mail(kind, machine_name, details, panel_url):
    # Konu, düz metin ve HTML hâli. details: [(satır adı, değer, yazı tipi sabit genişlikte mi)]
    color, title, sentence, tip = MAIL_TYPES[kind]
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    rows = [("Makine", machine_name, False)] + details + [("Zaman", now, False)]
    sentence = sentence.format(m=machine_name, days=dict((r[0], r[1]) for r in rows).get("Gün", ""))
    rows = [r for r in rows if r[0] != "Gün"]

    text = [title, "", sentence, ""] + ["%s: %s" % (label, value) for label, value, _ in rows]
    if tip:
        text += ["", "Ne yapmalı? " + tip]
    if panel_url:
        text += ["", "Panel: " + panel_url]
    text += ["", "Bu mail yedekleme sistemi tarafından otomatik gönderildi. "
                 "Bildirim adresini panelde Ayarlar'dan değiştirebilirsiniz."]

    e = html.escape
    bold_sentence = e(sentence).replace(e(machine_name), "<b>%s</b>" % e(machine_name), 1)
    table = "".join(
        '<tr><td style="padding:8px 10px;border-top:1px solid #e5e9ed;color:#5a6875;width:120px;'
        'white-space:nowrap;vertical-align:top">%s</td><td style="padding:8px 10px;border-top:1px solid #e5e9ed;'
        'vertical-align:top;%s">%s</td></tr>'
        % (e(label), "font-family:Consolas,monospace;font-size:13px;" if mono else "", e(str(value)))
        for label, value, mono in rows)
    tip_html = ('<div style="background:#f4f6f8;border-radius:8px;padding:12px 14px;font-size:14px;margin-top:4px">'
                '<b>Ne yapmalı?</b> %s</div>' % e(tip)) if tip else ""
    button = ('<a href="%s" style="display:inline-block;background:#0f6e78;color:#ffffff;padding:10px 18px;'
              'border-radius:6px;font-weight:600;text-decoration:none;margin-top:16px">Panele git</a>'
              % e(panel_url)) if panel_url else ""
    body = (
        '<div style="background:#eef1f4;padding:24px 12px;font-family:Segoe UI,Arial,sans-serif">'
        '<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #d6dce2;border-radius:10px;overflow:hidden">'
        '<div style="background:%s;color:#ffffff;padding:18px 24px">'
        '<div style="font-size:20px;font-weight:700">%s</div>'
        '<div style="font-size:14px;opacity:.9;margin-top:4px">%s · %s</div></div>'
        '<div style="padding:22px 24px;color:#16202a;font-size:15px;line-height:1.55">%s'
        '<table style="border-collapse:collapse;width:100%%;margin:14px 0;font-size:14px">%s</table>%s%s</div>'
        '<div style="padding:14px 24px;font-size:12px;color:#5a6875;border-top:1px solid #e5e9ed;background:#fafbfc">'
        'Bu mail yedekleme sistemi tarafından otomatik gönderildi. Bildirim adresini panelde Ayarlar\'dan değiştirebilirsiniz.'
        '</div></div></div>'
        % (color, e(title), e(machine_name), now, bold_sentence, table, tip_html, button))
    return "%s: %s" % (machine_name, title), "\n".join(text), body


def send_mail(to, kind, machine_name, details, on_error):
    # Maili arka planda gönderir: API'nin cevabı mail gidene kadar beklemesin.
    # Gönderilemezse on_error(hata metni) çağrılır.
    def worker():
        try:
            with open(SMTP_PATH, encoding="utf-8") as f:
                smtp = json.load(f)
        except (OSError, ValueError) as error:
            log.warning("Mail not sent (smtp.json could not be read: %s): %s", error, kind)
            on_error("smtp.json okunamadı: %s" % error)
            return
        subject, text, body = build_mail(kind, machine_name, details, smtp.get("panel_url"))
        message = EmailMessage()
        message["From"] = smtp["from"]
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        message.add_alternative(body, subtype="html")
        try:
            with smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(smtp["username"], smtp["password"])
                server.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            log.warning("Mail could not be sent: %s: %s", subject, error)
            on_error(str(error))
    threading.Thread(target=worker, daemon=True).start()


def notify(connection, account_id, machine_id, machine_name, kind, details=()):
    # Hesabın e-posta adresine mail. Gönderilemezse o makinenin loglarına ERROR satırı yazılır.
    row = connection.execute("SELECT email FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None or not row[0]:
        return
    title = MAIL_TYPES[kind][1]

    def mail_error(error):
        error_connection = connect()
        with error_connection:
            error_connection.execute(
                "INSERT INTO logs (machine_id, account_id, time, level, message, error) VALUES (?, ?, ?, ?, ?, ?)",
                (machine_id, account_id, datetime.now().isoformat(timespec="milliseconds"), "ERROR",
                 "Mail gönderilemedi (%s): %s" % (title, error), None)
            )
        error_connection.close()

    send_mail(row[0], kind, machine_name, list(details), mail_error)


def open_problem(connection, account_id, machine_id, machine_name, kind, job, details):
    # Sorun zaten açıksa hiçbir şey yapma (sorun başına tek mail)
    cursor = connection.execute(
        "INSERT OR IGNORE INTO problems (machine_id, kind, job, opened_at) VALUES (?, ?, ?, ?)",
        (machine_id, kind, job, datetime.now().isoformat(timespec="seconds"))
    )
    if cursor.rowcount:
        notify(connection, account_id, machine_id, machine_name, kind, details)


def close_problem(connection, account_id, machine_id, machine_name, kind, job, details):
    # Sorun açıksa kapat ve "düzeldi" maili gönder (sorunun ne kadar sürdüğüyle)
    row = connection.execute("SELECT opened_at FROM problems WHERE machine_id = ? AND kind = ? AND job = ?",
                             (machine_id, kind, job)).fetchone()
    if row is None:
        return
    connection.execute("DELETE FROM problems WHERE machine_id = ? AND kind = ? AND job = ?", (machine_id, kind, job))
    duration = format_duration((datetime.now() - datetime.fromisoformat(row[0])).total_seconds())
    notify(connection, account_id, machine_id, machine_name, kind + "_ok", list(details) + [("Sorun süresi", duration, False)])


def schedule_gap_days(schedule):
    # İşin normalde iki çalışması arasındaki en uzun süre (gün)
    if schedule.get("type") == "interval":
        return schedule.get("minutes", 0) / 1440
    if schedule.get("type") == "weekly":
        order = ["pzt", "sal", "car", "per", "cum", "cmt", "paz"]
        days = sorted(order.index(d) for d in schedule.get("days", []) if d in order)
        if len(days) <= 1:
            return 7
        return max((days[(i + 1) % len(days)] - day) % 7 or 7 for i, day in enumerate(days))
    return 1   # daily


def check_status(connection, machine_id, status):
    # Servisin gönderdiği durumdan sorunları bul: açılan sorun için mail, düzelen sorun için mail
    row = connection.execute("SELECT account_id, name FROM machines WHERE id = ?", (machine_id,)).fetchone()
    account_id, name = row
    if account_id is None:
        return   # hesaba bağlı olmayan makine için bildirim yok

    close_problem(connection, account_id, machine_id, name, "offline", "", [])

    target_types = {(source, target): target_type for source, target, target_type in connection.execute(
        "SELECT source, target, target_type FROM jobs WHERE machine_id = ?", (machine_id,))}
    now = datetime.now()
    current = []
    for job in status.get("jobs", []):
        key = job["source"] + " -> " + job["target"]
        current.append(key)
        target_label = TARGET_LABELS.get(target_types.get((job["source"], job["target"])) or "local", "")
        job_rows = [("Kaynak", job["source"], True),
                    ("Hedef", job["target"] + (" (%s)" % target_label if target_label else ""), True)]
        reason = [("Sebep", turkish_error(job["last_error"]), False)] if job.get("last_error") else []
        result = job.get("last_result")
        state = job.get("state")

        # A: tüm denemelerden sonra hatalı dosya kaldı
        if result and result["failed"] > 0 and state not in ("running", "waiting_retry"):
            open_problem(connection, account_id, machine_id, name, "job_failed", key,
                         job_rows + [("Sonuç", "%d dosyadan %d tanesi kopyalanamadı" % (result["files"], result["failed"]), False)]
                         + reason)
        elif result and result["failed"] == 0:
            close_problem(connection, account_id, machine_id, name, "job_failed", key, job_rows)

        # C: iş başlayamıyor (kaynak/hedef erişilemiyor, token geçersiz)
        if state == "waiting_access":
            open_problem(connection, account_id, machine_id, name, "no_access", key, job_rows + reason)
        else:
            close_problem(connection, account_id, machine_id, name, "no_access", key, job_rows)

        # D: hedef diskte yer kalmadı
        if DISK_ERROR in (job.get("last_error") or ""):
            open_problem(connection, account_id, machine_id, name, "disk_full", key, job_rows + reason)
        else:
            close_problem(connection, account_id, machine_id, name, "disk_full", key, job_rows)

        # E: iş uzun süredir hiç başarılı olmadı
        connection.execute(
            "INSERT OR IGNORE INTO job_health (machine_id, job, first_seen) VALUES (?, ?, ?)",
            (machine_id, key, now.isoformat(timespec="seconds"))
        )
        first_seen, last_success, last_finished = connection.execute(
            "SELECT first_seen, last_success, last_finished FROM job_health WHERE machine_id = ? AND job = ?",
            (machine_id, key)
        ).fetchone()
        if result and result["failed"] == 0 and result["finished"] != last_finished:
            last_success = now.isoformat(timespec="seconds")
            connection.execute("UPDATE job_health SET last_success = ?, last_finished = ? WHERE machine_id = ? AND job = ?",
                               (last_success, result["finished"], machine_id, key))
            close_problem(connection, account_id, machine_id, name, "no_success", key, job_rows)
        gap = schedule_gap_days(job.get("schedule", {}))
        limit = timedelta(days=NO_SUCCESS_DAYS if gap <= NO_SUCCESS_DAYS else 2 * gap)
        if now - datetime.fromisoformat(last_success or first_seen) > limit:
            open_problem(connection, account_id, machine_id, name, "no_success", key,
                         [("Gün", limit.days, False)] + job_rows + reason)

    # Silinen işlerin sorunları ve kayıtları sessizce kaldırılır
    marks = ",".join("?" * len(current))
    connection.execute("DELETE FROM problems WHERE machine_id = ? AND job != '' AND job NOT IN (%s)" % marks,
                       [machine_id] + current)
    connection.execute("DELETE FROM job_health WHERE machine_id = ? AND job NOT IN (%s)" % marks,
                       [machine_id] + current)


def check_offline():
    # B: hesaba bağlı olup 10 dakikadır durum göndermeyen makineler
    limit = (datetime.now() - OFFLINE_AFTER).isoformat(timespec="seconds")
    connection = connect()
    rows = connection.execute(
        "SELECT machines.id, machines.account_id, machines.name, machine_status.updated_at FROM machines"
        " JOIN machine_status ON machine_status.machine_id = machines.id"
        " WHERE machines.account_id IS NOT NULL AND machine_status.updated_at < ?",
        (limit,)
    ).fetchall()
    with connection:
        for machine_id, account_id, name, updated_at in rows:
            last_seen = datetime.fromisoformat(updated_at).strftime("%d.%m.%Y %H:%M")
            open_problem(connection, account_id, machine_id, name, "offline", "", [("Son haber", last_seen, False)])
    connection.close()


def offline_loop():
    while True:
        time.sleep(OFFLINE_CHECK_SECONDS)
        try:
            check_offline()
        except Exception:
            log.exception("Offline check failed")


# ---------------- Ayarlar ----------------
@app.get("/account")
def get_account(account_id: int = Depends(require_login)):
    # Panel: hesabın kullanıcı adı ve bildirim e-postası
    connection = connect()
    username, email = connection.execute("SELECT username, email FROM accounts WHERE id = ?",
                                         (account_id,)).fetchone()
    connection.close()
    return {"username": username, "email": email or ""}


@app.put("/account/password")
def change_password(body: dict = Body(), session: Optional[str] = Cookie(None)):
    # Panel: Body {"current", "new"}. Mevcut şifre doğru olmalı; yeni şifre en az 8 karakter.
    # Başarılı olursa bu hesabın diğer açık oturumları kapatılır (bu oturum açık kalır).
    account_id = require_login(session)
    now = datetime.now()
    if PASSWORD_BLOCKED.get(account_id, now) > now:
        raise HTTPException(429, "too many wrong attempts, try again later")
    current, new = body.get("current"), body.get("new")
    if not isinstance(current, str) or not isinstance(new, str):
        raise HTTPException(400, "current and new password must be text")
    connection = connect()
    salt, iterations, stored_hash = connection.execute(
        "SELECT salt, iterations, hash FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    digest = hashlib.pbkdf2_hmac("sha256", current.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
    if not hmac.compare_digest(digest, stored_hash):
        connection.close()
        fails = [t for t in PASSWORD_FAILS.get(account_id, []) if now - t < LOGIN_FAIL_WINDOW] + [now]
        if len(fails) >= LOGIN_MAX_FAILS:
            PASSWORD_BLOCKED[account_id] = now + LOGIN_BLOCK
            fails = []
        PASSWORD_FAILS[account_id] = fails
        raise HTTPException(400, "current password is wrong")
    PASSWORD_FAILS.pop(account_id, None)   # mevcut şifre doğru: sayaç sıfırlanır
    if len(new) < PASSWORD_MIN_LENGTH:
        connection.close()
        raise HTTPException(400, "new password is too short")
    new_salt = secrets.token_bytes(16)
    new_hash = hashlib.pbkdf2_hmac("sha256", new.encode("utf-8"), new_salt, PASSWORD_ITERATIONS).hex()
    with connection:
        connection.execute("UPDATE accounts SET salt = ?, iterations = ?, hash = ? WHERE id = ?",
                           (new_salt.hex(), PASSWORD_ITERATIONS, new_hash, account_id))
    connection.close()
    for token in [t for t, owner in SESSIONS.items() if owner == account_id and t != session]:
        del SESSIONS[token]
    return {"changed": True}


@app.put("/account/email")
def set_email(body: dict = Body(), account_id: int = Depends(require_login)):
    # Panel: bildirim e-postası (boş bırakılırsa mail gönderilmez)
    email = body.get("email")
    if not isinstance(email, str):
        raise HTTPException(400, "email must be text")
    email = email.strip()
    if email and "@" not in email:
        raise HTTPException(400, "invalid email")
    connection = connect()
    with connection:
        connection.execute("UPDATE accounts SET email = ? WHERE id = ?", (email, account_id))
    connection.close()
    return {"email": email}