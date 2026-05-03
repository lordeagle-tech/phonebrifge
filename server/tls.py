"""
Self-signed TLS certificate management.

Generates and caches a self-signed cert + key under ~/.phone-bridge/ on first
run. Returns paths and a SHA-256 fingerprint so the user can verify the cert
on first connect from their phone.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import socket
from pathlib import Path
from typing import List, Tuple

CERT_DIR = Path.home() / ".phone-bridge"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"


def _fingerprint(cert_path: Path) -> str:
    """Compute SHA-256 fingerprint of the DER form of the cert (X.509)."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        data = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(data)
        der = cert.public_bytes(serialization.Encoding.DER)
        digest = hashlib.sha256(der).hexdigest().upper()
    except Exception:
        digest = hashlib.sha256(cert_path.read_bytes()).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _generate(cert_path: Path, key_path: Path, hostnames: List[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "phone-bridge"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Laptop-Phone Bridge"),
    ])
    san_list: list = []
    for h in hostnames:
        try:
            san_list.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            san_list.append(x509.DNSName(h))

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass


def ensure_cert(extra_hosts: List[str] | None = None) -> Tuple[Path, Path, str]:
    """Generate cert+key on first run, return (cert_path, key_path, fingerprint)."""
    CERT_DIR.mkdir(mode=0o700, exist_ok=True)
    hosts = ["127.0.0.1", "localhost"]
    try:
        hosts.append(socket.gethostname())
    except Exception:
        pass
    if extra_hosts:
        hosts.extend(extra_hosts)
    # de-dupe preserving order
    seen = set()
    hosts = [h for h in hosts if not (h in seen or seen.add(h))]

    if not (CERT_FILE.exists() and KEY_FILE.exists()):
        try:
            _generate(CERT_FILE, KEY_FILE, hosts)
        except ImportError as e:
            raise RuntimeError(
                "TLS requires the 'cryptography' package. Install with:\n"
                "  pip install cryptography"
            ) from e

    return CERT_FILE, KEY_FILE, _fingerprint(CERT_FILE)
