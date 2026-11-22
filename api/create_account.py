"""Panele giriş için yeni bir hesap açar. API bilgisayarında, api klasöründe çalıştırılır.

Kullanım:
    python create_account.py

Şifre düz metin olarak saklanmaz: rastgele bir "tuz" (salt) ile birlikte PBKDF2 ile hash'lenir
ve backup.db'deki accounts tablosuna sadece hash yazılır.
Yeni hesapla giriş yapmak için API'yi yeniden başlatmaya gerek yoktur.
"""
import getpass
import hashlib
import secrets
import sqlite3
from datetime import datetime

from api import PASSWORD_ITERATIONS, PASSWORD_MIN_LENGTH, connect   # tablo yapısı ve şifre ayarları api.py ile aynı olsun


def main():
    username = input("Kullanıcı adı: ").strip()
    password = getpass.getpass("Şifre: ")
    if getpass.getpass("Şifre (tekrar): ") != password:
        print("Şifreler aynı değil, hesap açılmadı.")
        return
    if not username or not password:
        print("Kullanıcı adı ve şifre boş olamaz, hesap açılmadı.")
        return
    if len(password) < PASSWORD_MIN_LENGTH:
        print("Şifre en az %d karakter olmalı, hesap açılmadı." % PASSWORD_MIN_LENGTH)
        return

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    connection = connect()
    try:
        with connection:
            connection.execute(
                "INSERT INTO accounts (username, salt, iterations, hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, salt.hex(), PASSWORD_ITERATIONS, digest.hex(), datetime.now().isoformat(timespec="seconds"))
            )
    except sqlite3.IntegrityError:
        print("Bu kullanıcı adıyla bir hesap zaten var, hesap açılmadı.")
        return
    finally:
        connection.close()
    print("Hesap açıldı:", username)


if __name__ == "__main__":
    main()