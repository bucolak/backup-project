"""Bildirim türü: e-posta (SMTP).

Ayarlar (Email oluşturulurken verilir):
    host:     mail sunucusu, örn. "smtp.gmail.com"
    port:     örn. 587 (starttls) ya da 465 (ssl)
    security: "starttls" (bağlantı şifresiz açılır, sonra şifrelenir) ya da "ssl" (en baştan şifreli)
    username, password: mail hesabı (Gmail'de uygulama şifresi)
    from:     mailin kimden geldiği görünen adres

Bağlantı birden çok mail için tekrar kullanılır: ilk send'de açılır, açık kalır;
arada koptuysa bir kez yeniden açılıp tekrar denenir. İşçi işini bitirince close() ile kapatılır.

Hata kodları:
    validate: invalid_address (biçim yanlış), address_undeliverable (alan adı mail kabul etmiyor),
              address_unverifiable (alan adı o an sorgulanamadı)
    send:     settings_invalid, auth_failed, recipient_refused, sender_refused, message_refused (kalıcı)
              connection_failed, server_busy (geçici)
"""
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from email_validator import EmailNotValidError, EmailUndeliverableError, validate_email

from provider import Provider, Result

TIMEOUT = 30   # bir denemede sunucu en fazla bu kadar saniye beklenir


class Email(Provider):

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.server = None   # açık SMTP bağlantısı (yoksa None)

    def validate(self, address: str) -> Result:
        # @, biçim ve alan adının MX kaydı (mail kabul ediyor mu) tek seferde kontrol edilir
        try:
            answer = validate_email(address, check_deliverability=True, timeout=TIMEOUT)
        except EmailUndeliverableError:
            return Result(False, "address_undeliverable", permanent=True)
        except EmailNotValidError:
            return Result(False, "invalid_address", permanent=True)
        # Alan adı o an sorgulanamadıysa (DNS cevap vermedi) kütüphane adresi geçirir; biz geçirmiyoruz
        if getattr(answer, "unknown_deliverability", None):
            return Result(False, "address_unverifiable")
        return Result(True)

    def connect(self):
        # Ayardaki şifreleme yoluyla sunucuya bağlanır ve giriş yapar
        host, port, security = self.settings["host"], int(self.settings["port"]), self.settings["security"]
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
        elif security == "starttls":
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.starttls(context=ssl.create_default_context())
        else:
            raise ValueError("security")
        server.login(self.settings["username"], self.settings["password"])
        self.server = server

    def send(self, address: str, title: str, text: str, html: Optional[str] = None) -> Result:
        try:
            message = EmailMessage()
            message["From"] = self.settings["from"]
            message["To"] = address
            message["Subject"] = title
            message.set_content(text)
            if html:
                message.add_alternative(html, subtype="html")   # mail programı HTML'i gösteremezse düz metin görünür
        except (KeyError, ValueError):
            return Result(False, "settings_invalid", permanent=True)

        for attempt in (1, 2):   # açık bağlantı koptuysa bir kez yeniden bağlanıp tekrar dene
            try:
                if self.server is None:
                    self.connect()
                self.server.send_message(message)
                return Result(True)
            except smtplib.SMTPServerDisconnected:
                self.server = None
                if attempt == 2:
                    return Result(False, "connection_failed")
            except smtplib.SMTPAuthenticationError:
                self.close()
                return Result(False, "auth_failed", permanent=True)
            except smtplib.SMTPRecipientsRefused as error:
                code = next(iter(error.recipients.values()))[0]
                return Result(False, "recipient_refused", permanent=code >= 500)
            except smtplib.SMTPSenderRefused as error:
                return Result(False, "sender_refused", permanent=error.smtp_code >= 500)
            except smtplib.SMTPResponseException as error:
                # SMTP kodu: 4xx geçici, 5xx kalıcı
                if error.smtp_code >= 500:
                    return Result(False, "message_refused", permanent=True)
                return Result(False, "server_busy")
            except (KeyError, ValueError):
                return Result(False, "settings_invalid", permanent=True)
            except OSError:
                # Sunucuya ulaşılamadı, zaman aşımı, bağlantı kesildi
                self.close()
                return Result(False, "connection_failed")

    def close(self) -> None:
        if self.server is not None:
            try:
                self.server.quit()
            except (smtplib.SMTPException, OSError):
                pass   # bağlantı zaten kopmuş olabilir
            self.server = None


PROVIDER = Email   # registry bu türün class'ını bu değişkenden bulur