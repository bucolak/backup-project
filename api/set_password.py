"""Panel girişi için kullanıcı adı ve şifreyi belirler. API bilgisayarında çalıştırılır.

Kullanım:
    python set_password.py

Şifre düz metin olarak saklanmaz: rastgele bir "tuz" (salt) ile birlikte PBKDF2 ile
hash'lenir ve login.json'a sadece hash yazılır. Tekrar çalıştırılırsa eski şifrenin yerine geçer.
Değişikliğin geçerli olması için API'yi yeniden başlatmaya gerek yoktur.
"""
import getpass
import hashlib
import json
import os
import secrets

LOGIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "login.json")
ITERATIONS = 600_000   # hash'in kaç tur hesaplanacağı: tahmin denemelerini yavaşlatır


def main():
    username = input("Kullanıcı adı: ").strip()
    password = getpass.getpass("Şifre: ")
    if getpass.getpass("Şifre (tekrar): ") != password:
        print("Şifreler aynı değil, hiçbir şey değiştirilmedi.")
        return
    if not username or not password:
        print("Kullanıcı adı ve şifre boş olamaz, hiçbir şey değiştirilmedi.")
        return

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    with open(LOGIN_PATH, "w", encoding="utf-8") as f:
        json.dump({"username": username, "salt": salt.hex(), "iterations": ITERATIONS,
                   "hash": digest.hex()}, f, indent=4)
    print("Kaydedildi:", LOGIN_PATH)


if __name__ == "__main__":
    main()