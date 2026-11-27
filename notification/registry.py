"""Registry: bildirim türlerinin nesnelerini hazırlar ve tutar.

settings.json'daki açık (enabled: true) her tür için providers/<tür>.py dosyasını yükler,
dosyadaki PROVIDER değişkeninden class'ı bulur ve türün ayarlarıyla bir nesne oluşturur.
Sonuç bir sözlüktür: {"email": Email nesnesi, ...}. İşçi ve API türü buradan bulur; türü bilmezler.

Her tür için tek nesne vardır (bağlantı tekrar kullanılabilsin diye).
settings.json'un değişme zamanına (mtime) 30 saniyede bir bakılır; değiştiyse sadece ayarı değişen
türler yeniden oluşturulur, diğerlerinin nesneleri (ve açık bağlantıları) korunur.
Yüklenemeyen tür (dosyası yok, dosyada hata var, PROVIDER yok) atlanır ve loga yazılır.
"""
import importlib
import json
import logging
import os
import threading
import time

from provider import Provider

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
CHECK_SECONDS = 30   # settings.json kaç saniyede bir kontrol edilir

log = logging.getLogger("notification")


class Registry:

    def __init__(self):
        self.providers = {}   # tür -> nesne (sadece açık ve yüklenebilen türler)
        self.settings = {}    # her türün, nesnesi oluşturulurken kullanılan ayarı
        self.mtime = None     # settings.json'un son okunduğundaki değişme zamanı

    def get(self, type_name):
        """Türün nesnesi; tür yoksa, kapalıysa ya da yüklenemediyse None."""
        return self.providers.get(type_name)

    def refresh(self):
        """settings.json değiştiyse okur ve sadece ayarı değişen türleri yeniden oluşturur."""
        try:
            mtime = os.path.getmtime(SETTINGS_PATH)
        except OSError as error:
            log.error("settings.json bulunamadı: %s", error)
            return
        if mtime == self.mtime:
            return   # değişmemiş: bir şey yapma
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError) as error:
            # Dosya o an yarım kaydedilmiş olabilir: mevcut türlerle devam et, sonraki kontrolde tekrar dene
            log.error("settings.json okunamadı, mevcut ayarlarla devam ediliyor: %s", error)
            return
        self.mtime = mtime

        enabled = {name: value for name, value in settings.items() if value.get("enabled")}
        providers = dict(self.providers)
        old = []   # yeni sözlük yerine konduktan sonra kapatılacak eski nesneler
        for name in list(providers):
            if enabled.get(name) != self.settings.get(name):   # kapatıldı, silindi ya da ayarı değişti
                old.append(providers.pop(name))
                self.settings.pop(name, None)
        for name, value in enabled.items():
            if name in providers:
                continue   # ayarı değişmemiş: nesnesi korunur
            provider = self.create(name, value)
            if provider is not None:
                providers[name] = provider
                self.settings[name] = value

        self.providers = providers   # tek adımda yerine konur (atomic swap)
        for provider in old:
            provider.close()
        log.info("Bildirim türleri yüklendi: %s", ", ".join(sorted(providers)) or "yok")

    def create(self, name, settings):
        """providers/<name>.py dosyasından türün nesnesini oluşturur; olmazsa None (loga yazılır)."""
        try:
            module = importlib.import_module("providers." + name)
            cls = getattr(module, "PROVIDER")
            if not (isinstance(cls, type) and issubclass(cls, Provider)):
                raise TypeError("PROVIDER bir Provider class'ı değil")
            return cls(settings)
        except Exception as error:
            log.error("Bildirim türü yüklenemedi, atlanıyor (%s): %s", name, error)
            return None

    def start(self):
        """İlk yüklemeyi yapar, sonra arka planda 30 saniyede bir kontrol eder."""
        self.refresh()
        thread = threading.Thread(target=self.loop, daemon=True)
        thread.start()

    def loop(self):
        while True:
            time.sleep(CHECK_SECONDS)
            try:
                self.refresh()
            except Exception:
                log.exception("settings.json kontrolü başarısız")