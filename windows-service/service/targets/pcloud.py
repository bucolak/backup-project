"""Hedef türü "pcloud": pCloud'daki bir klasör (pcloud_storage.py ile).

Hedef "backup-project/linux-vm" ise ilk klasör (backup-project) kök, kalanı dosyaların önüne eklenir.
Fonksiyonların açıklaması için targets/local.py'nin başına bakın.
"""
import hashlib
import os
from email.utils import parsedate_to_datetime


def folder_parts(target):
    return [part for part in target.replace("\\", "/").split("/") if part]


def validate(item, svc):
    parts = folder_parts(item["target"])
    if not parts or any(part in (".", "..") for part in parts):
        return "invalid pcloud folder: %r" % item["target"]
    return None


class Target:
    def __init__(self, job, svc):
        self.svc = svc
        self.parts = folder_parts(job["target"])
        self.token_path = os.path.join(svc.BASE_DIR, "pcloud_token.json")
        self._storage = None

    def storage(self):
        if self._storage is None:
            from pcloud_storage import PCloudStorage
            self._storage = PCloudStorage(self.parts[0], token_file=self.token_path)
        return self._storage

    def remote(self, rel):
        # pCloud API'sindeki tam yol: "/backup-project/linux-vm/2026-11-23_02-00-00/a.txt"
        return "/" + "/".join(self.parts + rel.split("/"))

    def display(self, rel):
        return "pcloud:" + "/".join(self.parts + [rel])

    def check(self):
        # pCloud için yerel hedef klasörü yok; token ve pcloud_storage.py hazır mı ona bakılır
        if not os.path.exists(self.token_path):
            return "pcloud token file not found: %s" % self.token_path
        try:
            import pcloud_storage  # noqa: F401
        except ImportError as error:
            return "pcloud support is not ready (pcloud_storage.py or requests package missing): %s" % error
        return None

    def save(self, src, rel, info):
        svc = self.svc
        info["status"] = "uploading"

        # Yüklemeden önce dosyanın parmak izlerini hesapla (pCloud bölgesine göre SHA-256 ya da SHA-1 veriyor)
        sha256 = hashlib.sha256()
        sha1 = hashlib.sha1()
        with open(svc.lp(src), "rb") as f:
            while True:
                chunk = f.read(svc.CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                sha1.update(chunk)

        def on_progress(done, total):
            # pcloud_storage yüklemenin ilerlemesini bildirir; dosya boyutuna oranlanır
            info["copied"] = info["size"] * done // total if total else 0

        result = self.storage().save(src, "/".join(self.parts[1:] + rel.split("/")), progress=on_progress)
        info["copied"] = info["size"]

        # Doğrulama: pCloud'un aldığı dosya için hesapladığı hash, bizimkiyle aynı mı?
        info["status"] = "verifying"
        checksums = result["checksums"][0]
        if "sha256" in checksums:
            same = checksums["sha256"].lower() == sha256.hexdigest()
        else:
            same = checksums["sha1"].lower() == sha1.hexdigest()
        if not same:
            raise IOError("pCloud verification failed: " + self.display(rel))
        return sha256.hexdigest(), sha1.hexdigest()

    def verify(self, rel, stored_hash):
        try:
            # pCloud dosyanın hash'ini kendisi hesaplar, indirmeye gerek yok
            sha1 = self.storage().call("checksumfile", {"path": self.remote(rel)})["sha1"]
        except RuntimeError:
            return False   # pCloud'da yok (silinmiş)
        return sha1.lower() == stored_hash

    def exists(self, rel):
        # Her dosya için pCloud'a sormak yavaş olur; Taşı + incremental'da verify zaten pCloud'a bakar
        return True

    def list(self):
        try:
            data = self.storage().call("listfolder", {"path": "/" + "/".join(self.parts), "recursive": 1})
        except RuntimeError:
            return []   # klasör henüz yok
        files = []

        def walk(folder, prefix):
            for item in folder.get("contents", []):
                if item["isfolder"]:
                    walk(item, prefix + item["name"] + "/")
                else:
                    # pCloud tarihi "Tue, 17 Nov 2026 08:00:00 +0000" biçiminde; diğer hedeflerle aynı olsun diye çevrilir
                    modified = parsedate_to_datetime(item["modified"]).astimezone().replace(tzinfo=None)
                    files.append({"path": prefix + item["name"], "size": item["size"],
                                  "modified": modified.isoformat(timespec="seconds")})

        walk(data["metadata"], "")
        return files

    def download(self, rel, dest):
        import requests

        link = self.storage().call("getfilelink", {"path": self.remote(rel)})
        with requests.get("https://" + link["hosts"][0] + link["path"], stream=True, timeout=60) as response:
            response.raise_for_status()
            with open(self.svc.lp(dest), "wb") as f:
                for chunk in response.iter_content(self.svc.CHUNK_SIZE):
                    f.write(chunk)