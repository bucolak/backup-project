"""Hedef türü "local": yerel ya da ağdaki bir klasör.

Her hedef dosyası aynı şeyleri sağlar (servis hangi hedef olduğunu bilmeden bunları çağırır):
    validate(item, svc)          config'teki iş bu hedef için geçerli mi? (geçerliyse None, değilse açıklama)
    Target(job, svc)
        .check()                 hedef hazır mı? (hazırsa None, değilse açıklama)
        .display(rel)            loglarda görünecek tam hedef adı
        .save(src, rel, info)    dosyayı hedefe koyar ve doğrular -> (src'nin SHA-256'sı, hedefteki dosyanın hash'i)
        .verify(rel, hash)       hedefte rel'de duran dosyanın hash'i bu mu?
        .exists(rel)             hedefte rel'de dosya var mı?
        .list()                  geri yükleme için hedefteki dosyalar: [{"path", "size", "modified"}]
        .download(rel, dest)     geri yükleme için hedefteki dosyayı dest'e alır
rel: hedefin içindeki yol, "/" ile ayrılmış: "2026-11-23_02-00-00/alt/a.txt"
svc: servisin ortak yardımcıları (lp, calculate_hash, CHUNK_SIZE, TEMP_SUFFIX, stopping, ...)
"""
import hashlib
import os
import secrets
import shutil
from datetime import datetime


def validate(item, svc):
    source, target = svc.norm_path(item["source"]), svc.norm_path(item["target"])
    if source == target:
        return svc.err("target.local.same_folder")
    if (target + os.sep).startswith(source + os.sep):
        return svc.err("target.local.target_inside")
    return None


def probe_target(folder, svc):
    """Hedefte gerçekten deneme dosyası oluşturur, adını değiştirir ve siler.
    Servisin çalıştığı hesabın gerçek yazma iznini ölçer; hata varsa OSError fırlatır."""
    lp = svc.lp
    token = secrets.token_hex(4)
    first = os.path.join(folder, ".bksvc-probe-%s%s" % (token, svc.TEMP_SUFFIX))
    second = os.path.join(folder, ".bksvc-probe-%s-b%s" % (token, svc.TEMP_SUFFIX))
    current = None
    try:
        with open(lp(first), "wb") as f:
            f.write(b"x")
        current = first
        os.replace(lp(first), lp(second))
        current = second
        os.remove(lp(second))
        current = None
    finally:
        if current:
            try:
                os.remove(lp(current))
            except OSError:
                pass


class Target:
    def __init__(self, job, svc):
        self.root = job["target"]
        self.svc = svc

    def full(self, rel):
        return os.path.join(self.root, rel.replace("/", os.sep))

    def display(self, rel):
        return self.full(rel)

    def check(self):
        # Hedef: klasör oluşturulabiliyor mu, içine yazılabiliyor mu
        try:
            os.makedirs(self.svc.lp(self.root), exist_ok=True)
        except OSError as error:
            return self.svc.err("target.local.mkdir_failed", error=self.svc.error_info(error))
        try:
            probe_target(self.root, self.svc)
        except OSError as error:
            return self.svc.err("target.local.not_writable", error=self.svc.error_info(error))
        return None

    def save(self, src, rel, info):
        svc, lp = self.svc, self.svc.lp
        target_path = self.full(rel)
        src_info = os.stat(lp(src))
        os.makedirs(lp(os.path.dirname(target_path)), exist_ok=True)

        # Önce geçici dosyaya yaz; doğrulanmadan hedefe dokunma
        # Rastgele kod: aynı dosyaya aynı anda yazan iki kopyalama birbirine karışmasın
        temp_path = target_path + "." + secrets.token_hex(4) + svc.TEMP_SUFFIX

        reservation = None
        try:
            # Yer yoksa hiç başlama; varsa bu dosyanın boyutu kadar yeri ayır
            reservation = svc.reserve_space(os.path.dirname(target_path), src_info.st_size)

            # Dosyayı parça parça kopyala, hash'ini kopyalarken hesapla
            sha256 = hashlib.sha256()
            done = 0
            info["status"] = "copying"
            with open(lp(src), "rb") as f_src, open(lp(temp_path), "wb") as f_dst:
                while True:
                    if svc.stopping.is_set():
                        raise InterruptedError()   # servis duruyor
                    chunk = f_src.read(svc.CHUNK_SIZE)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    f_dst.write(chunk)
                    done += len(chunk)
                    info["copied"] = info["size"] * done // src_info.st_size if src_info.st_size else 0
                # Bellekte bekleyen veriyi şimdi diske yazdır (elektrik kesintisine karşı)
                f_dst.flush()
                os.fsync(f_dst.fileno())

            # Sadece tarihleri kaynakla aynı yap (salt okunur gibi özellikler kopyalanmaz)
            os.utime(lp(temp_path), ns=(src_info.st_atime_ns, src_info.st_mtime_ns))

            info["status"] = "verifying"
            # Doğrulama: geçici dosyayı diskten tekrar oku, hash'ini karşılaştır
            if svc.calculate_hash(temp_path) != sha256.hexdigest():
                raise svc.ServiceError("target.local.verify_failed", path=target_path)

            # Doğrulandı: hedefteki eski kopya salt okunursa önce bu özelliği kaldır,
            # sonra geçici dosyayı tek adımda hedefin yerine koy
            svc.clear_readonly(target_path)
            os.replace(lp(temp_path), lp(target_path))
        finally:
            svc.release_space(reservation)
            # Bir hata olduysa yarım ya da bozuk geçici dosyayı sil
            if os.path.exists(lp(temp_path)):
                os.remove(lp(temp_path))

        return sha256.hexdigest(), sha256.hexdigest()

    def verify(self, rel, stored_hash):
        try:
            return self.svc.calculate_hash(self.full(rel)) == stored_hash
        except OSError:
            return False   # hedefteki kopya yok ya da okunamadı

    def exists(self, rel):
        return os.path.exists(self.svc.lp(self.full(rel)))

    def list(self):
        files = []
        for path in self.svc.list_files(self.root):
            info = os.stat(self.svc.lp(path))
            files.append({
                "path": os.path.relpath(path, self.root).replace(os.sep, "/"),
                "size": info.st_size,
                "modified": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds")
            })
        return files

    def download(self, rel, dest):
        shutil.copyfile(self.svc.lp(self.full(rel)), self.svc.lp(dest))