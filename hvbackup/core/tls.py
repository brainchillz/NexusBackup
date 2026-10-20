"""Self-signed certificate for the server when no PEM pair is configured. Replace it
with a real one by setting HVB_TLS_CERT/HVB_TLS_KEY; proxies pin the CA or use HVB_INSECURE."""
import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .. import config


def ensure_cert():
    if config.TLS_CERT and config.TLS_KEY:
        return config.TLS_CERT, config.TLS_KEY
    os.makedirs(config.CERT_DIR, exist_ok=True)
    crt, key = os.path.join(config.CERT_DIR, 'server.crt'), os.path.join(config.CERT_DIR, 'server.key')
    if os.path.exists(crt) and os.path.exists(key):
        return crt, key
    host = socket.getfqdn()
    names = [x509.DNSName(host), x509.DNSName(socket.gethostname()), x509.DNSName('localhost')]
    try:
        for ip in {socket.gethostbyname(host), '127.0.0.1'}:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except OSError:
        pass
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host), x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'hvbackup')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(k, hashes.SHA256()))
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(crt, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key
