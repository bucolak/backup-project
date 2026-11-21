"""API için HTTPS sertifikalarını üretir. API bilgisayarında bir kez çalıştırılır.

Kullanım (API'ye hangi adreslerle bağlanılacaksa hepsi yazılır):
    python make_cert.py 127.0.0.1 localhost 192.168.89.1

Üretilenler (cert/ klasörüne):
    ca.crt      bizim onay makamımız (CA). Servislere ve tarayıcıya "buna güven" diye verilir.
    ca.key      onay makamının gizli anahtarı. API bilgisayarından çıkmamalı.
    server.crt  API'nin kimlik kartı; ca.key ile imzalanır.
    server.key  API'nin gizli anahtarı. API bilgisayarından çıkmamalı.
"""
import ipaddress
import os
import sys
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert")
VALID_DAYS = 5 * 365   # sertifikalar 5 yıl geçerli


def save_key(key, name):
    with open(os.path.join(CERT_DIR, name), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))


def save_cert(cert, name):
    with open(os.path.join(CERT_DIR, name), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def main():
    addresses = sys.argv[1:]
    if not addresses:
        print("Kullanım: python make_cert.py 127.0.0.1 localhost 192.168.89.1")
        sys.exit(1)

    os.makedirs(CERT_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)

    # 1) Onay makamı (CA): kendi kendini imzalar
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Backup Project CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                     crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    # 2) API'nin kimlik kartı: hangi adreslerde geçerli olduğu yazılır, CA ile imzalanır
    names = []
    for address in addresses:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError:
            names.append(x509.DNSName(address))

    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, addresses[0])]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    save_key(ca_key, "ca.key")
    save_cert(ca_cert, "ca.crt")
    save_key(server_key, "server.key")
    save_cert(server_cert, "server.crt")
    print("Sertifikalar üretildi:", CERT_DIR)
    print("Geçerli adresler:", ", ".join(addresses))


if __name__ == "__main__":
    main()