"""Hedef türü "gdrive": Google Drive'daki bir klasör (drive_storage.py ile).

Hedef "backup-project/linux-vm" ise ilk klasör (backup-project) kök, kalanı dosyaların önüne eklenir.
Fonksiyonların açıklaması için targets/local.py'nin başına bakın.
"""
import hashlib
import os
from datetime import datetime

FOLDER_MIME = "application/vnd.google-apps.folder"


def folder_parts(target):
    return [part for part in target.replace("\\", "/").split("/") if part]


def validate(item, svc):
    parts = folder_parts(item["target"])
    if not parts or any(part in (".", "..") for part in parts):
        return "invalid gdrive folder: %r" % item["target"]
    return None


class Target:
    def __init__(self, job, svc):
        self.svc = svc
        self.parts = folder_parts(job["target"])
        self.token_path = os.path.join(svc.BASE_DIR, "token.json")              # python drive_storage.py ile bir kez oluşturulur
        self.credentials_path = os.path.join(svc.BASE_DIR, "credentials.json")  # Google Cloud'dan indirilen OAuth dosyası
        self._storage = None

    def storage(self):
        if self._storage is None:
            from drive_storage import DriveStorage
            self._storage = DriveStorage(self.parts[0], self.token_path, self.credentials_path)
        return self._storage

    def find(self, names):
        """Kök klasörün altındaki yolu (klasörler + son ad) oluşturmadan bulur; yoksa None."""
        storage = self.storage()
        parent = storage.root_id
        for name in names[:-1]:
            parent = storage.find(name, parent, FOLDER_MIME)
            if not parent:
                return None
        return storage.find(names[-1], parent) if names else parent

    def display(self, rel):
        return "gdrive:" + "/".join(self.parts + [rel])

    def check(self):
        # Google Drive için yerel hedef klasörü yok; token ve drive_storage.py hazır mı ona bakılır
        if not os.path.exists(self.token_path):
            return "Google Drive token file not found (run 'python drive_storage.py' once): %s" % self.token_path
        try:
            import drive_storage  # noqa: F401
        except ImportError as error:
            return "Google Drive support is not ready (drive_storage.py or Google packages missing): %s" % error
        return None

    def save(self, src, rel, info):
        svc = self.svc
        info["status"] = "uploading"

        # Yüklemeden önce dosyanın parmak izlerini hesapla (Drive md5 veriyor)
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        with open(svc.lp(src), "rb") as f:
            while True:
                chunk = f.read(svc.CHUNK_SIZE)
                if not chunk:
                    break
                sha256.update(chunk)
                md5.update(chunk)

        result = self.storage().save(src, "/".join(self.parts[1:] + rel.split("/")))
        info["copied"] = info["size"]

        # Doğrulama: Drive'ın aldığı dosya için hesapladığı md5, bizimkiyle aynı mı?
        info["status"] = "verifying"
        if result.get("md5Checksum", "").lower() != md5.hexdigest():
            raise IOError("Google Drive verification failed: " + self.display(rel))
        return sha256.hexdigest(), md5.hexdigest()

    def verify(self, rel, stored_hash):
        file_id = self.find(self.parts[1:] + rel.split("/"))
        if not file_id:
            return False   # Drive'da yok (silinmiş)
        # Drive dosyanın md5'ini kendisi hesaplar, indirmeye gerek yok
        md5 = self.storage().service.files().get(fileId=file_id, fields="md5Checksum").execute().get("md5Checksum", "")
        return md5.lower() == stored_hash

    def exists(self, rel):
        # Her dosya için Drive'a sormak yavaş olur; Taşı + incremental'da verify zaten Drive'a bakar
        return True

    def list(self):
        storage = self.storage()
        folder_id = self.find(self.parts[1:]) if len(self.parts) > 1 else storage.root_id
        files = []
        if not folder_id:
            return files   # klasör henüz yok

        def walk(parent_id, prefix):
            page = None
            while True:
                result = storage.service.files().list(
                    q="'%s' in parents and trashed = false" % parent_id, pageToken=page, pageSize=1000,
                    fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)").execute()
                for item in result.get("files", []):
                    if item["mimeType"] == FOLDER_MIME:
                        walk(item["id"], prefix + item["name"] + "/")
                    else:
                        # Drive tarihi "2026-11-21T19:00:00.000Z" (UTC) biçiminde; diğer hedeflerle aynı olsun diye çevrilir
                        modified = datetime.fromisoformat(item["modifiedTime"].replace("Z", "+00:00"))
                        files.append({"path": prefix + item["name"], "size": int(item.get("size") or 0),
                                      "modified": modified.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")})
                page = result.get("nextPageToken")
                if not page:
                    return

        walk(folder_id, "")
        return files

    def download(self, rel, dest):
        from googleapiclient.http import MediaIoBaseDownload

        file_id = self.find(self.parts[1:] + rel.split("/"))
        if not file_id:
            raise FileNotFoundError("not found on Google Drive: " + rel)
        with open(self.svc.lp(dest), "wb") as f:
            downloader = MediaIoBaseDownload(f, self.storage().service.files().get_media(fileId=file_id),
                                             chunksize=self.svc.CHUNK_SIZE)
            done = False
            while not done:
                _, done = downloader.next_chunk()