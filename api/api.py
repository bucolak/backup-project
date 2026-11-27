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
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

LOG_RETENTION_DAYS = 30   # API'de kaç günlük log tutulacak
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")   # panel: index.html, style.css, app.js, fonts/
RESTORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "restore_tmp")   # servisten gelen dosyalar, indirilene kadar
PASSWORD_ITERATIONS = 600_000   # şifre hash'i kaç tur hesaplanır: tahmin denemelerini yavaşlatır (create_account.py de kullanır)
PASSWORD_MIN_LENGTH = 8         # yeni şifre en az kaç karakter olmalı (create_account.py de kullanır)
LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")   # dil dosyaları: tr.json ...; hangisi kullanılacak: language.json


def load_language():
    # lang/language.json'daki dilin yazılarını okur: {"language": "tr"} -> lang/tr.json (API açılırken bir kez)
    with open(os.path.join(LANG_DIR, "language.json"), encoding="utf-8") as f:
        language = json.load(f)["language"]
    with open(os.path.join(LANG_DIR, language + ".json"), encoding="utf-8") as f:
        return language, json.load(f)


LANGUAGE, TEXTS = load_language()


def text(code, params=None):
    # Kod + değerlerden cümle (panelin t() fonksiyonunun aynısı): "{ad}" yerlerine değerler konur.
    # Değer de bir kod + değer ise ({"code", "params"}) önce o yazıya çevrilir.
    sentence = TEXTS.get(code, code)
    for name, value in (params or {}).items():
        if isinstance(value, dict) and "code" in value:
            value = error_text(value)
        sentence = sentence.replace("{%s}" % name, str(value))
    return sentence


def error_text(error):
    # Servisten gelen hata ({"code", "params"}) -> cümle. İşletim sistemi hatası: dil dosyasında
    # errno'sunun karşılığı varsa o, yoksa hatanın kendi yazısı. Eski biçim (düz yazı) olduğu gibi.
    if not isinstance(error, dict):
        return error
    params = error.get("params") or {}
    if error.get("code") == "error.os":
        reason = TEXTS.get("errno.%s" % params.get("errno"))
        if reason is None:
            return params.get("text", "")
        return text("error.os_path", {"reason": reason, "path": params["path"]}) if params.get("path") else reason
    return text(error.get("code", ""), params)

# Hedef türlerinin yazı olmayan bilgileri (local_path: hedef bu bilgisayardaki bir klasör mü); panel okur.
# Türlerin yazıları (ad, alan yazısı, örnek yol, hataları) dil dosyasında: target.<tür>.label ... (API açılırken bir kez)
TARGETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json")
with open(TARGETS_PATH, encoding="utf-8") as f:
    TARGET_TYPES = json.load(f)

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
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/lang")
def get_language():
    # Panelin yazıları: seçili dilin dosyası. Giriş ekranının yazıları da buradan geldiği için giriş istemez.
    return {"language": LANGUAGE, "texts": TEXTS}


# Panelin stil, kod ve yazı tipi dosyaları: /assets/style.css, /assets/app.js, /assets/fonts/...
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


# ---------------- Giriş (login) ----------------
# Giriş yapılınca rastgele bir oturum anahtarı üretilir ve tarayıcıya çerez olarak verilir.
# Oturumlar hafızada tutulur: API yeniden başlarsa tekrar giriş yapmak gerekir.
SESSIONS = {}   # oturum anahtarı -> giriş yapan hesabın id'si


def require_login(session: Optional[str] = Cookie(None)):
    # Panelin kullandığı adresler: geçerli oturum çerezi yoksa 401. Varsa hesabın id'si döner.
    if session not in SESSIONS:
        raise HTTPException(401, "login_required")
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
        raise HTTPException(503, "no_account")
    username = body.get("username", "")
    key = (request.client.host if request.client else "", username)
    now = datetime.now()
    if LOGIN_BLOCKED.get(key, now) > now:
        raise HTTPException(429, "too_many_attempts")
    account_id = check_password(username, body.get("password", ""))
    if account_id is None:
        fails = [t for t in LOGIN_FAILS.get(key, []) if now - t < LOGIN_FAIL_WINDOW] + [now]
        if len(fails) >= LOGIN_MAX_FAILS:
            LOGIN_BLOCKED[key] = now + LOGIN_BLOCK
            fails = []
        LOGIN_FAILS[key] = fails
        raise HTTPException(401, "wrong_credentials")
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
    "target_type": "TEXT",
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
            target_type TEXT,
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

    # Servislerin gönderdiği log satırları. code + params: cümle değil kod ve değerler
    # (panel cümleyi dil dosyasından kurar: log.copied + {"path": ...}); message: kodu olmayan eski biçim satırlar
    connection.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT,
            account_id INTEGER,
            time TEXT,
            level TEXT,
            message TEXT,
            error TEXT,
            code TEXT,
            params TEXT
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
            params TEXT,
            PRIMARY KEY (machine_id, kind, job)
        )
    """)

    # Biten her çalışma (Genel bakış: zaman çizelgesi ve son 14 gün). 30 günden eskiler silinir.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT,
            job TEXT,
            started TEXT,
            finished TEXT,
            files INTEGER,
            failed INTEGER,
            UNIQUE (machine_id, job, finished)
        )
    """)

    # Genel bakıştaki olay akışı: yedekleme başladı/bitti, sorun açıldı/düzeldi, makine eklendi/çıkarıldı,
    # geri yükleme indirildi. 30 günden eskiler silinir.
    # Cümle saklanmaz: olayın türü (kind), işi (job) ve değerleri (params); panel cümleyi dil dosyasından kurar.
    # Eski biçimdeki (cümleli) tablo silinip yeniden oluşturulur: eski olaylar silinir.
    if "text" in [row[1] for row in connection.execute("PRAGMA table_info(events)")]:
        connection.execute("DROP TABLE events")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            machine_id TEXT,
            time TEXT,
            kind TEXT,
            job TEXT,
            params TEXT
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
            last_started TEXT,
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
    if "params" not in [row[1] for row in connection.execute("PRAGMA table_info(problems)")]:
        connection.execute("ALTER TABLE problems ADD COLUMN params TEXT")
    if "code" not in [row[1] for row in connection.execute("PRAGMA table_info(logs)")]:
        # Loglar kod + değer biçimine geçti: eski (cümleli) loglar silinir
        connection.execute("ALTER TABLE logs ADD COLUMN code TEXT")
        connection.execute("ALTER TABLE logs ADD COLUMN params TEXT")
        connection.execute("DELETE FROM logs")
    if "last_started" not in [row[1] for row in connection.execute("PRAGMA table_info(job_health)")]:
        connection.execute("ALTER TABLE job_health ADD COLUMN last_started TEXT")
    connection.commit()

    return connection


# ---------------- Makineler ----------------
@app.post("/machines/register")
def register_machine(body: dict = Body()):
    # Servis ilk açılışta buraya gelir: makineye yeni bir id ve gizli anahtar verilir.
    # Gizli anahtar sadece bu cevapta bir kere gönderilir; servis onu machine.json'a yazar.
    name = body.get("name")
    if not isinstance(name, str):
        raise HTTPException(400, "name_not_text")
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
        raise HTTPException(401, "machine_credentials_required")
    connection = connect()
    row = connection.execute("SELECT secret_hash FROM machines WHERE id = ?", (x_machine_id,)).fetchone()
    connection.close()
    digest = hashlib.sha256(x_machine_secret.encode("utf-8")).hexdigest()
    if row is None or not hmac.compare_digest(digest, row[0]):
        raise HTTPException(401, "wrong_machine_credentials")
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


def add_event_log(connection, machine_id, account_id, code, params):
    # Panelde yapılan makine işlemleri (eklendi / çıkarıldı) o makinenin loglarına yazılır (kod + değerler)
    connection.execute(
        "INSERT INTO logs (machine_id, account_id, time, level, code, params) VALUES (?, ?, ?, ?, ?, ?)",
        (machine_id, account_id, datetime.now().isoformat(timespec="milliseconds"), "INFO", code, json.dumps(params))
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
        raise HTTPException(400, "name_required")
    return name.strip()


@app.post("/machines/pair")
def pair_machine(body: dict = Body(), account_id: int = Depends(require_login)):
    # Panel: Body {"code", "name"} -> kodun ait olduğu makineyi bu hesaba bağla
    now = now_utc()
    if PAIR_BLOCKED.get(account_id, now) > now:
        raise HTTPException(429, "too_many_attempts")
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
        raise HTTPException(400, "wrong_or_expired_code")

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
            add_event_log(connection, machine_id, account_id, "log.machine_added", {"name": name})
            notify(connection, account_id, machine_id, name, "added")
            add_event(connection, account_id, machine_id, "machine_added")
    except sqlite3.IntegrityError:
        raise HTTPException(409, "duplicate_machine_name")
    finally:
        connection.close()
    return {"id": machine_id, "name": name}


def own_machine(connection, machine_id, account_id):
    # Makine bu hesabın değilse 404
    row = connection.execute("SELECT name FROM machines WHERE id = ? AND account_id = ?",
                             (machine_id, account_id)).fetchone()
    if row is None:
        connection.close()
        raise HTTPException(404, "machine_not_found")
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
        raise HTTPException(409, "duplicate_machine_name")
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
        add_event_log(connection, machine_id, account_id, "log.machine_removed", {"name": name})
        notify(connection, account_id, machine_id, name, "removed")
        add_event(connection, account_id, machine_id, "machine_removed")
        # Makinenin açık sorunları ve iş kayıtları sessizce silinir (hesapta değil artık)
        connection.execute("DELETE FROM problems WHERE machine_id = ?", (machine_id,))
        connection.execute("DELETE FROM job_health WHERE machine_id = ?", (machine_id,))
    connection.close()
    return {"removed": True}


@app.get("/targets")
def get_targets(account_id: int = Depends(require_login)):
    # Panel: iş formundaki hedef türü listesi
    return TARGET_TYPES


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
        raise HTTPException(400, "job_machine_not_in_account")
    if any(not job.get("target_type") for job in jobs):
        connection.close()
        raise HTTPException(400, "job_target_type_missing")
    with connection:   # hepsi birlikte kaydedilir ya da hiçbiri
        connection.execute("DELETE FROM jobs WHERE machine_id IN (SELECT id FROM machines WHERE account_id = ?)",
                           (account_id,))
        for job in jobs:
            connection.execute(
                "INSERT INTO jobs (machine_id, source, target_type, target, schedule, mode, compression, backup_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job["machine_id"], job["source"], job["target_type"], job["target"],
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
        connection.execute("DELETE FROM runs WHERE finished < ?", (cutoff,))
        connection.execute("DELETE FROM events WHERE time < ?", (cutoff,))
        for entry in entries:
            connection.execute(
                "INSERT INTO logs (machine_id, account_id, time, level, message, error, code, params) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (machine_id, account_id, entry.get("time"), entry.get("level"),
                 translate_os_error(entry.get("message")), entry.get("error"),
                 entry.get("code"), json.dumps(entry["params"]) if "params" in entry else None)
            )
        connection.execute("DELETE FROM logs WHERE time < ?", (cutoff,))
    connection.close()
    return {"saved": len(entries)}


@app.get("/logs")
def get_logs(machine_id: Optional[str] = None, level: Optional[str] = None, limit: int = 500,
             account_id: int = Depends(require_login)):
    # Panel: bu hesabın en yeni log satırları; makineye ve seviyeye göre filtrelenebilir.
    # Makinenin adı da gelir: hesaptan çıkarılmış makinenin eski logları da adıyla görünsün.
    query = ("SELECT logs.machine_id, machines.name, logs.time, logs.level, logs.message, logs.error, logs.code, logs.params"
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
             "message": message, "error": error, "code": code, "params": json.loads(params) if params else {}}
            for machine_id, name, time, level, message, error, code, params in rows]


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
        raise HTTPException(404, "job_not_found")
    return row[0]


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
        raise HTTPException(404, "restore_request_not_found")
    keys = ("id", "machine_id", "target", "target_type", "kind", "path", "status", "files", "error")
    result = dict(zip(keys, row))
    result["files"] = json.loads(result["files"]) if result["files"] else None
    try:
        result["error"] = json.loads(result["error"]) if result["error"] else None
    except ValueError:
        pass   # eski biçim: düz yazı
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
        raise HTTPException(404, "restore_request_not_found")
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
        raise HTTPException(409, "file_not_ready")
    name = r["path"].split("/")[-1]

    # Servisin gönderdiği dosyayı ver, gönderim bitince sil
    path = os.path.join(RESTORE_DIR, str(request_id))
    set_restore(request_id, "downloaded")
    connection = connect()
    with connection:
        add_event(connection, account_id, r["machine_id"], "restore", params={"path": r["path"]})
    connection.close()
    return FileResponse(path, filename=name, background=BackgroundTask(os.remove, path))


# Servis
def own_request(request_id, machine_id):
    # Servis sadece kendi makinesine ait isteğe cevap verebilir
    if get_restore(request_id)["machine_id"] != machine_id:
        raise HTTPException(403, "restore_request_other_machine")


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
    set_restore(request_id, "error", error=json.dumps(body.get("error"), ensure_ascii=False))   # kod + değerler
    return {"saved": True}


# ---------------- Bildirimler (e-posta) ----------------
# Mailler hesabın Ayarlar'da girdiği adrese gider; adres boşsa mail gönderilmez.
# Gönderim bilgileri (SMTP) api klasöründeki smtp.json'dadır; dosya yoksa mail gönderilmez.
# Her sorun bir kere açılır (problems tablosu): açılınca bir mail, düzelince bir mail.
SMTP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smtp.json")
OFFLINE_AFTER = timedelta(minutes=10)    # işin zamanından bu kadar sonra hâlâ haber yoksa "ulaşılamıyor"
OFFLINE_CHECK_SECONDS = 60               # zamanı geçen işler kaç saniyede bir kontrol edilir
DAYS = ["pzt", "sal", "car", "per", "cum", "cmt", "paz"]   # servisteki gün adlarıyla aynı
NO_SUCCESS_DAYS = 3                      # bir iş bu kadar gündür hiç başarılı olmadıysa (seyrek işlerde aralığın 2 katı)
log = logging.getLogger("uvicorn.error")         # API'nin kendi log'u (api-service.err.log)

# Mail türleri: renk (sorun: gri, olumlu: mavi), başlık, açıklama cümlesi, "Ne yapmalı?" önerisi.
# "_ok" ile bitenler sorunun düzeldiğini bildirir.
MAIL_GRAY = "#4b5563"
MAIL_BLUE = "#1d5fa8"
MAIL_TYPES = {
    "offline": (MAIL_GRAY, "Makine ulaşılamıyor",
                "{m} makinesinde bir yedeklemenin zamanı geldi ama makineden haber alınamıyor.",
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

# İşletim sisteminden gelen sık hatalar İngilizce olabilir (Linux, İngilizce Windows): [Errno numara] -> Türkçesi.
# Servisin kendi mesajları zaten Türkçe; Türkçe Windows'un hataları da Türkçe gelir.
ERRNO_TEXTS = {2: "Dosya ya da klasör bulunamadı", 5: "Giriş/çıkış hatası", 13: "Erişim izni yok",
               28: "Diskte yer kalmadı", 30: "Disk salt okunur"}
ERRNO_PATTERN = re.compile(r"\[Errno (\d+)\] [^:]*(: .*)?$")


def translate_os_error(text):
    # İşletim sisteminin sık İngilizce hatalarını Türkçeye çevirir (log satırları ve mailler için)
    if not text:
        return text
    match = ERRNO_PATTERN.search(text)
    if match and int(match.group(1)) in ERRNO_TEXTS:
        text = text[:match.start()] + ERRNO_TEXTS[int(match.group(1))] + (match.group(2) or "")
    return text


def turkish_error(text):
    # Maildeki "Sebep" satırı: işletim sistemi hataları çevrilir, ilk harf büyütülür
    if not text:
        return text
    text = translate_os_error(text)
    first = "İ" if text[0] == "i" else text[0].upper()
    return first + text[1:]


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
            log.warning("Mail gönderilmedi (smtp.json okunamadı: %s): %s", error, MAIL_TYPES[kind][1])
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
            log.warning("Mail gönderilemedi: %s: %s", subject, error)
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
                 translate_os_error("Mail gönderilemedi (%s): %s" % (title, error)), None)
            )
        error_connection.close()

    send_mail(row[0], kind, machine_name, list(details), mail_error)


def add_event(connection, account_id, machine_id, kind, job=None, params=None, time=None):
    # Genel bakıştaki olay akışına bir satır: cümle değil olayın türü ve değerleri (panel dil dosyasından yazar)
    connection.execute(
        "INSERT INTO events (account_id, machine_id, time, kind, job, params) VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, machine_id, time or datetime.now().isoformat(timespec="seconds"), kind, job, json.dumps(params or {}))
    )


def open_problem(connection, account_id, machine_id, machine_name, kind, job, details, params):
    # Sorun zaten açıksa hiçbir şey yapma (sorun başına tek mail).
    # details: mail için satırlar; params: panel için değerler (panel cümleyi dil dosyasından kurar)
    cursor = connection.execute(
        "INSERT OR IGNORE INTO problems (machine_id, kind, job, opened_at, params) VALUES (?, ?, ?, ?, ?)",
        (machine_id, kind, job, datetime.now().isoformat(timespec="seconds"), json.dumps(params))
    )
    if cursor.rowcount:
        notify(connection, account_id, machine_id, machine_name, kind, details)
        add_event(connection, account_id, machine_id, "problem", job, dict(params, problem=kind))


def close_problem(connection, account_id, machine_id, machine_name, kind, job, details):
    # Sorun açıksa kapat ve "düzeldi" maili gönder (sorunun ne kadar sürdüğüyle)
    row = connection.execute("SELECT opened_at FROM problems WHERE machine_id = ? AND kind = ? AND job = ?",
                             (machine_id, kind, job)).fetchone()
    if row is None:
        return
    connection.execute("DELETE FROM problems WHERE machine_id = ? AND kind = ? AND job = ?", (machine_id, kind, job))
    seconds = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
    notify(connection, account_id, machine_id, machine_name, kind + "_ok",
           list(details) + [("Sorun süresi", format_duration(seconds), False)])
    add_event(connection, account_id, machine_id, "resolved", job, {"problem": kind, "seconds": int(seconds)})


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
        # Hedef türünün adı: dil dosyasından (target.<tür>.label)
        target_type = target_types.get((job["source"], job["target"]))
        target_label = TEXTS.get("target.%s.label" % target_type, "")
        job_rows = [("Kaynak", job["source"], True),
                    ("Hedef", job["target"] + (" (%s)" % target_label if target_label else ""), True)]
        # Hata servisten kod + değer olarak gelir: mail için cümleye çevrilir, panele kod olarak gider
        reason = [("Sebep", turkish_error(error_text(job["last_error"])), False)] if job.get("last_error") else []
        error = {"error": job["last_error"]} if job.get("last_error") else {}
        result = job.get("last_result")
        state = job.get("state")

        # Çalışma geçmişi: başlayan ve biten her çalışma kaydedilir (Genel bakış)
        connection.execute(
            "INSERT OR IGNORE INTO job_health (machine_id, job, first_seen) VALUES (?, ?, ?)",
            (machine_id, key, now.isoformat(timespec="seconds"))
        )
        last_started = connection.execute("SELECT last_started FROM job_health WHERE machine_id = ? AND job = ?",
                                          (machine_id, key)).fetchone()[0]
        if state == "running" and job.get("last_run") and job["last_run"] != last_started:
            connection.execute("UPDATE job_health SET last_started = ? WHERE machine_id = ? AND job = ?",
                               (job["last_run"], machine_id, key))
            add_event(connection, account_id, machine_id, "run_started", key, time=job["last_run"])
        if result and result.get("finished"):
            cursor = connection.execute(
                "INSERT OR IGNORE INTO runs (machine_id, job, started, finished, files, failed) VALUES (?, ?, ?, ?, ?, ?)",
                (machine_id, key, job.get("last_run") or result["finished"], result["finished"], result["files"], result["failed"])
            )
            if cursor.rowcount and result["failed"] == 0:
                add_event(connection, account_id, machine_id, "run_ok", key, {"files": result["files"]}, result["finished"])
            elif cursor.rowcount and state != "waiting_retry":
                add_event(connection, account_id, machine_id, "run_failed", key,
                          {"files": result["files"], "failed": result["failed"]}, result["finished"])

        # A: tüm denemelerden sonra hatalı dosya kaldı
        if result and result["failed"] > 0 and state not in ("running", "waiting_retry"):
            open_problem(connection, account_id, machine_id, name, "job_failed", key,
                         job_rows + [("Sonuç", "%d dosyadan %d tanesi kopyalanamadı" % (result["files"], result["failed"]), False)]
                         + reason, dict(error, files=result["files"], failed=result["failed"]))
        elif result and result["failed"] == 0:
            close_problem(connection, account_id, machine_id, name, "job_failed", key, job_rows)

        # C: iş başlayamıyor (kaynak/hedef erişilemiyor, token geçersiz)
        if state == "waiting_access":
            open_problem(connection, account_id, machine_id, name, "no_access", key, job_rows + reason, error)
        else:
            close_problem(connection, account_id, machine_id, name, "no_access", key, job_rows)

        # D: hedef diskte yer kalmadı
        if isinstance(job.get("last_error"), dict) and job["last_error"].get("code") == "error.disk_full":
            open_problem(connection, account_id, machine_id, name, "disk_full", key, job_rows + reason, error)
        else:
            close_problem(connection, account_id, machine_id, name, "disk_full", key, job_rows)

        # E: iş uzun süredir hiç başarılı olmadı
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
                         [("Gün", limit.days, False)] + job_rows + reason, {"days": limit.days})

    # Silinen işlerin sorunları ve kayıtları sessizce kaldırılır
    marks = ",".join("?" * len(current))
    connection.execute("DELETE FROM problems WHERE machine_id = ? AND job != '' AND job NOT IN (%s)" % marks,
                       [machine_id] + current)
    connection.execute("DELETE FROM job_health WHERE machine_id = ? AND job NOT IN (%s)" % marks,
                       [machine_id] + current)


def last_due_time(schedule, last_run, now):
    # İşin en son ne zaman çalışması gerekiyordu (servisin zamanlama kuralıyla aynı). Bilinemiyorsa None.
    if schedule.get("type") == "interval":
        if last_run is None:
            return None   # iş hiç çalışmamış: ne zaman çalışacağı bilinmiyor
        due = last_run + timedelta(minutes=schedule.get("minutes", 0))
        return due if due <= now else None
    if schedule.get("type") in ("daily", "weekly"):
        hour, minute = (int(x) for x in schedule.get("time", "00:00").split(":"))
        for back in range(8):
            day = now - timedelta(days=back)
            if schedule["type"] == "weekly" and DAYS[day.weekday()] not in schedule.get("days", []):
                continue
            due = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                return due
    return None


def check_offline():
    # B: bir işin zamanı geldikten 10 dakika sonra makineden hâlâ hiç haber yoksa
    # (işin zamanından beri tek bir durum bile gelmediyse) "ulaşılamıyor".
    # Makine tekrar durum gönderince sorun kapanır (check_status) ve "tekrar çevrimiçi" maili gider.
    now = datetime.now()
    connection = connect()
    machines = connection.execute(
        "SELECT machines.id, machines.account_id, machines.name, machine_status.updated_at, machine_status.status"
        " FROM machines JOIN machine_status ON machine_status.machine_id = machines.id"
        " WHERE machines.account_id IS NOT NULL"
    ).fetchall()
    with connection:
        for machine_id, account_id, name, updated_at, status in machines:
            last_seen = datetime.fromisoformat(updated_at)
            last_runs = {(job["source"], job["target"]): job.get("last_run")
                         for job in json.loads(status).get("jobs", [])}
            missed = []
            for source, target, schedule in connection.execute(
                    "SELECT source, target, schedule FROM jobs WHERE machine_id = ?", (machine_id,)).fetchall():
                last_run = last_runs.get((source, target))
                due = last_due_time(json.loads(schedule), datetime.fromisoformat(last_run) if last_run else None, now)
                if due is not None and last_seen < due and now - due >= OFFLINE_AFTER:
                    missed.append((due, source, target))
            if missed:
                due, source, target = min(missed)   # zamanı ilk geçen iş
                open_problem(connection, account_id, machine_id, name, "offline", "", [
                    ("Kaynak", source, True), ("Hedef", target, True),
                    ("Yedekleme zamanı", due.strftime("%d.%m.%Y %H:%M"), False),
                    ("Son haber", last_seen.strftime("%d.%m.%Y %H:%M"), False)],
                    {"due": due.isoformat(timespec="minutes"), "last_seen": last_seen.isoformat(timespec="minutes")})
    connection.close()


def offline_loop():
    while True:
        time.sleep(OFFLINE_CHECK_SECONDS)
        try:
            check_offline()
        except Exception:
            log.exception("Ulaşılamıyor kontrolü başarısız")


# ---------------- Genel bakış ----------------
@app.get("/overview")
def overview(day: Optional[str] = None, account_id: int = Depends(require_login)):
    # Panel: açık sorunlar, son 14 günün çalışmaları ve seçilen günün olayları (gün: YYYY-AA-GG, boşsa bugün).
    # "now": API'nin saati; panel "son haber" ve zaman çizelgesini buna göre hesaplar.
    now = datetime.now()
    day = day or now.strftime("%Y-%m-%d")
    own = "SELECT id FROM machines WHERE account_id = ?"
    connection = connect()
    problems = connection.execute(
        "SELECT problems.machine_id, machines.name, problems.kind, problems.job, problems.opened_at, problems.params"
        " FROM problems JOIN machines ON machines.id = problems.machine_id"
        " WHERE machines.account_id = ? ORDER BY problems.opened_at", (account_id,)
    ).fetchall()
    since = min((now - timedelta(days=14)).strftime("%Y-%m-%dT00:00:00"), day + "T00:00:00")   # 14 gün ya da seçilen gün
    runs = connection.execute(
        "SELECT machine_id, job, started, finished, files, failed FROM runs"
        " WHERE machine_id IN (%s) AND finished >= ? ORDER BY finished" % own, (account_id, since)
    ).fetchall()
    events = connection.execute(
        "SELECT events.machine_id, machines.name, events.time, events.kind, events.job, events.params"
        " FROM events LEFT JOIN machines ON machines.id = events.machine_id"
        " WHERE events.account_id = ? AND events.time >= ? AND events.time < ? ORDER BY events.time DESC, events.id DESC",
        (account_id, day + "T00:00:00", day + "T99")
    ).fetchall()
    connection.close()
    return {
        "now": now.isoformat(timespec="seconds"),
        "problems": [{"machine_id": m, "machine_name": n, "kind": k, "job": j, "opened_at": o,
                      "params": json.loads(p) if p else {}} for m, n, k, j, o, p in problems],
        "runs": [{"machine_id": m, "job": j, "started": s, "finished": f, "files": fi, "failed": fa}
                 for m, j, s, f, fi, fa in runs],
        "events": [{"machine_id": m, "machine_name": n, "time": t, "kind": k, "job": j,
                    "params": json.loads(p) if p else {}} for m, n, t, k, j, p in events],
    }


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
        raise HTTPException(429, "too_many_attempts")
    current, new = body.get("current"), body.get("new")
    if not isinstance(current, str) or not isinstance(new, str):
        raise HTTPException(400, "password_not_text")
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
        raise HTTPException(400, "wrong_current_password")
    PASSWORD_FAILS.pop(account_id, None)   # mevcut şifre doğru: sayaç sıfırlanır
    if len(new) < PASSWORD_MIN_LENGTH:
        connection.close()
        raise HTTPException(400, "new_password_too_short")
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
        raise HTTPException(400, "email_not_text")
    email = email.strip()
    if email and "@" not in email:
        raise HTTPException(400, "invalid_email")
    connection = connect()
    with connection:
        connection.execute("UPDATE accounts SET email = ? WHERE id = ?", (email, account_id))
    connection.close()
    return {"email": email}