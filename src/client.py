import requests, hashlib, os, json

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

SUNUCU = config["server"]
KAYNAK = config["source"]

def sha256_hesapla(tam_yol):
    sha256 = hashlib.sha256()
    with open(tam_yol, "rb") as outgoing_file: 
        while True:
            parca = outgoing_file.read(4096)

            if not parca:
                break
            sha256.update(parca)
    return sha256.hexdigest()

def dosya_gonder(tam_yol, goreli):
    try:
        with open(tam_yol, "rb") as dosya:
            response = requests.post(
            SUNUCU + "/upload",
            files={"dosya": dosya},
            data={"goreli_yol": goreli}
        )
    except requests.exceptions.RequestException:
        print("  ERROR: connection problem:", hata)
        return False

    if response.status_code != 200:
        print("  ERROR", response.status_code, response.text)
        return False


    result = response.json()
    if result["sha256"] != sha256_hesapla(tam_yol):
        print("  ERROR: hash mismatch")
        return False
    if result["size"] != os.path.getsize(tam_yol):
        print("  ERROR: size mismatch")
        return False

    return True


try:
    requests.get(SUNUCU + "/", timeout=5)
except requests.exceptions.RequestException:
    print("Cannot reach server:", SUNUCU)
    print("Backup not started.")
    exit()

print("Connected to server, starting backup.")

succeeded = 0
failed = []

for klasor, _, dosyalar in os.walk(KAYNAK):
    for ad in dosyalar:
        tam_yol = os.path.join(klasor, ad)
        goreli = os.path.relpath(tam_yol, KAYNAK).replace("\\", "/")
        print("Uploading:", goreli)

        if dosya_gonder(tam_yol, goreli):
            succeeded += 1
        else:
            failed.append(goreli)

print()
print("Succeeded:", succeeded)
print("Failed:", len(failed))
for goreli in failed:
    print("  -", goreli)