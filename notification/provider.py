"""Provider: bildirim türlerinin (plugin) uyması gereken interface.

Her bildirim türü (e-posta, WhatsApp ...) bu sınıftan türeyen bir class yazar ve üç metodu uygular:
    validate: adres bu tür için geçerli mi
    send:     mesajı adrese gönder
    close:    açık bağlantı varsa kapat (bağlantı tutmayan türlerde boş kalır)

Türün ayarları (örn. e-posta için SMTP hesabı) class oluşturulurken bir kez verilir (__init__).
Metotlar hata fırlatmaz, sonuç döndürür: Result(ok, error, permanent).
error bir yazı değil koddur (örn. "invalid_address"); kodun kullanıcıya gösterilecek cümlesi,
bildirim sistemini kullanan tarafın kendi dil dosyasındadır.

Parametreler: bütün türlerin kullandıkları zorunlu (address, title, text);
sadece bazı türlerin kullandıkları isteğe bağlı (html). Kullanmayan tür onu yok sayar.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Result:
    """validate ve send'in sonucu.
    ok: başarılı mı. error: başarısızsa hatanın kodu.
    permanent: hata kalıcı mı (kalıcıysa tekrar denenmez; geçiciyse işçi tekrar dener)."""
    ok: bool
    error: Optional[str] = None
    permanent: bool = False


class Provider(ABC):
    """Bildirim türlerinin kalıbı. Doğrudan kullanılamaz; her tür bundan türeyen bir class yazar."""

    def __init__(self, settings: dict):
        # Türün ayarları: bir kez verilir, metotlar self.settings'ten okur
        self.settings = settings

    @abstractmethod
    def validate(self, address: str) -> Result:
        """Adres bu tür için geçerli mi."""

    @abstractmethod
    def send(self, address: str, title: str, text: str, html: Optional[str] = None) -> Result:
        """Mesajı adrese gönderir. html'i kullanmayan türler onu yok sayar."""

    @abstractmethod
    def close(self) -> None:
        """Açık bağlantı varsa kapatır. İşçi elindeki mesajları bitirince çağırır."""