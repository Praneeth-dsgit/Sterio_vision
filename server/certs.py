from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import ROOT

CERT_DIR = ROOT / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"


def local_ip_addresses() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        ips.update(
            info[4][0]
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET)
            if not info[4][0].startswith("127.")
        )
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    return sorted(ips)


def public_urls(https_port: int, http_port: int) -> list[dict[str, str]]:
    urls = []
    for ip in local_ip_addresses():
        urls.append({"ip": ip, "https": f"https://{ip}:{https_port}", "http": f"http://{ip}:{http_port}"})
    return urls


def ensure_certs() -> tuple[Path, Path]:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostnames = {socket.gethostname(), "localhost", "stereo-vision.local"}
    names = [x509.DNSName(name) for name in sorted(hostnames)]
    names.extend(x509.IPAddress(ipaddress.ip_address(ip)) for ip in local_ip_addresses())

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Stereo Vision"),
            x509.NameAttribute(NameOID.COMMON_NAME, "stereo-vision.local"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return CERT_FILE, KEY_FILE
