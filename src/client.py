
import requests
import hashlib
import os
import json
import sys
import time


# 1. Ayarları config.json dosyasından oku

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

SUNUCU = config["server"]
KAYNAK = config["source"]
CLIENT_ID = config["client_id"]


# 2. Dosyanın SHA256 değerini hesapla

def sha256_hesapla(tam_yol):
    sha256 = hashlib.sha256()

    with open(tam_yol, "rb") as dosya:
        while True:
            parca = dosya.read(4096)

            if not parca:
                break

            sha256.update(parca)

    return sha256.hexdigest()


# 3. Dosyayı sunucuya gönder

def dosya_gonder(tam_yol, goreli):

    try:
        with open(tam_yol, "rb") as dosya:
            response = requests.post(
                SUNUCU + "/upload",
                files={"dosya": dosya},
                data={"goreli_yol": goreli},
                timeout=30
            )

    except (requests.exceptions.RequestException, OSError) as hata:
        print("ERROR: Dosya gönderilemedi:", hata)
        return False

    if response.status_code != 200:
        print("ERROR:", response.status_code, response.text)
        return False

    result = response.json()

    if result["sha256"] != sha256_hesapla(tam_yol):
        print("ERROR: Hash mismatch:", goreli)
        return False

    if result["size"] != os.path.getsize(tam_yol):
        print("ERROR: Size mismatch:", goreli)
        return False

    return True


# 4. Yedekleme görevini çalıştır


def main():
    if not os.path.isdir(KAYNAK):
        print("Source folder not found:", KAYNAK)
        sys.exit(1)

    while True:
        print("\nChecking for pending backup jobs...")

        try:
            response = requests.post(
                SUNUCU + "/jobs/claim",
                json={"client_id": CLIENT_ID},
                timeout=10
            )
            response.raise_for_status()

        except requests.exceptions.RequestException as hata:
            print("Cannot reach server:", hata)
            print("Retrying in 30 seconds...")
            time.sleep(30)
            continue

        result = response.json()
        job = result["job"]

        if job is None:
            print(result["message"])
            print("Next check in 30 seconds...")
            time.sleep(30)
            continue

        job_id = job["id"]

        print("Backup job received. Job ID:", job_id)
        print("Starting backup...")

        succeeded = 0
        failed = []

        for klasor, _, dosyalar in os.walk(KAYNAK):
            for ad in dosyalar:
                tam_yol = os.path.join(klasor, ad)
                goreli = os.path.relpath(
                    tam_yol, KAYNAK
                ).replace("\\", "/")

                print("Uploading:", goreli)

                if dosya_gonder(tam_yol, goreli):
                    succeeded += 1
                else:
                    failed.append(goreli)

        backup_success = len(failed) == 0

        print()
        print("Succeeded:", succeeded)
        print("Failed:", len(failed))

        for goreli in failed:
            print("  -", goreli)

        try:
            response = requests.post(
                SUNUCU + f"/jobs/{job_id}/complete",
                json={
                    "client_id": CLIENT_ID,
                    "success": backup_success
                },
                timeout=10
            )
            response.raise_for_status()

            print()
            print("Backup result reported to server.")
            print("Job status:", response.json()["status"])

        except requests.exceptions.RequestException as hata:
            print("Could not report backup result:", hata)


if __name__ == "__main__":
    main()
