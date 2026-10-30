"""The server certificate. Three sources, in order of precedence:

  HVB_TLS_CERT / HVB_TLS_KEY   a pair managed outside (config file or environment): shown,
                               never changed here
  <data>/certs/server.crt+key  imported through Settings > Certificate (origin "imported")
                               or generated self-signed at first start / on request
                               (origin "generated")

A new pair is written next to the running one; uvicorn keeps the context it started with,
so info() reports restart_needed until the server restarts (Settings offers the restart).
The pair it replaces stays as server.crt.previous / server.key.previous for revert().

Everything that pins the certificate must follow a change: the Proxmox storage
registration carries the SHA-256 fingerprint, proxies verify by HVB_CA or run HVB_INSECURE,
the vSphere plug-in registration records the thumbprint. pins() lists them for the page."""
import datetime
import hashlib
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID, NameOID

from .. import config

ORIGIN_FILE = 'origin'
_running_fp = None  # fingerprint of the certificate the listener started with


def _managed_outside():
    return bool(config.TLS_CERT and config.TLS_KEY)


def paths():
    if _managed_outside():
        return config.TLS_CERT, config.TLS_KEY
    return os.path.join(config.CERT_DIR, 'server.crt'), os.path.join(config.CERT_DIR, 'server.key')


def ensure_cert():
    crt, key = paths()
    if _managed_outside() or (os.path.exists(crt) and os.path.exists(key)):
        return crt, key
    os.makedirs(config.CERT_DIR, exist_ok=True)
    _write_self_signed(default_names())
    return crt, key


def default_names():
    """Names a fresh self-signed certificate carries: the public URL's host when set, the
    FQDN, the short host name, localhost, and the addresses they resolve to."""
    names = []
    try:
        from . import settings
        import urllib.parse
        h = urllib.parse.urlparse(settings.public_url()).hostname
        if h:
            names.append(h)
    except Exception:  # noqa: BLE001
        pass
    names += [socket.getfqdn(), socket.gethostname(), 'localhost']
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    for n in list(out):
        try:
            ip = socket.gethostbyname(n)
            if ip not in out:
                out.append(ip)
        except OSError:
            pass
    if '127.0.0.1' not in out:
        out.append('127.0.0.1')
    return out


def _san(names):
    out = []
    for n in names:
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            out.append(x509.DNSName(n))
    return out


def _keep_previous():
    crt, key = paths()
    for p in (crt, key):
        if os.path.exists(p):
            os.replace(p, p + '.previous')


def _write_pair(cert_pem, key_pem, origin):
    crt, key = paths()
    os.makedirs(os.path.dirname(crt), exist_ok=True)
    _keep_previous()
    fd = os.open(key + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(key_pem)
    with open(crt + '.tmp', 'wb') as f:
        f.write(cert_pem)
    os.replace(key + '.tmp', key)
    os.replace(crt + '.tmp', crt)
    with open(os.path.join(os.path.dirname(crt), ORIGIN_FILE), 'w') as f:
        f.write(origin)


def _write_self_signed(names, days=3650):
    names = [n for n in names if n] or ['localhost']
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64]), x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'hvbackup')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(_san(names)), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(k, hashes.SHA256()))
    _write_pair(cert.public_bytes(serialization.Encoding.PEM),
                k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), 'generated')


def regenerate(names=None, days=3650):
    """A new self-signed pair with the given names (DNS or IP), or the defaults."""
    if _managed_outside():
        raise ValueError('the certificate is managed outside (HVB_TLS_CERT/HVB_TLS_KEY); change the files there')
    names = [str(n).strip() for n in (names or []) if str(n).strip()] or default_names()
    for n in names:
        if len(n) > 253 or any(ch.isspace() for ch in n) or n.startswith('-'):
            raise ValueError(f'bad name {n!r}')
    if not 1 <= int(days) <= 3650:
        raise ValueError('days must be between 1 and 3650')
    _write_self_signed(names, int(days))
    return info()


def _load_chain(pem):
    try:
        return x509.load_pem_x509_certificates(pem) if pem.strip() else []
    except ValueError as e:
        raise ValueError(f'the chain is not PEM certificates: {e}')


def import_pem(cert_pem, key_pem, chain_pem=''):
    """Install a certificate and its private key (PEM text), with an optional chain of
    intermediates appended to the certificate file. The key must match the certificate;
    the chain, when given, must lead up from it."""
    if _managed_outside():
        raise ValueError('the certificate is managed outside (HVB_TLS_CERT/HVB_TLS_KEY); change the files there')
    cert_pem, key_pem, chain_pem = (x.encode() if isinstance(x, str) else (x or b'') for x in (cert_pem, key_pem, chain_pem))
    try:
        certs = x509.load_pem_x509_certificates(cert_pem)
    except ValueError as e:
        raise ValueError(f'the certificate is not PEM: {e}')
    cert, bundled = certs[0], certs[1:]
    try:
        key = serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError) as e:
        raise ValueError(f'the private key is not an unencrypted PEM key: {e}')
    pub = serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(*pub) != cert.public_key().public_bytes(*pub):
        raise ValueError('the private key does not match the certificate')
    chain = bundled + _load_chain(chain_pem)
    prev = cert
    for c in chain:
        if c.subject != prev.issuer:
            raise ValueError(f'chain order: {c.subject.rfc4514_string()} does not issue {prev.subject.rfc4514_string()}')
        prev = c
    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc < now:
        raise ValueError(f'the certificate expired on {cert.not_valid_after_utc:%Y-%m-%d}')
    try:
        cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        raise ValueError('the certificate has no subject alternative names; browsers and the agents will not accept it')
    key_out = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    cert_out = b''.join(c.public_bytes(serialization.Encoding.PEM) for c in [cert] + chain)
    _write_pair(cert_out, key_out, 'imported')
    return info()


def revert():
    """Swap the previous pair back in."""
    if _managed_outside():
        raise ValueError('the certificate is managed outside')
    crt, key = paths()
    if not (os.path.exists(crt + '.previous') and os.path.exists(key + '.previous')):
        raise ValueError('there is no previous certificate to revert to')
    cur = open(crt, 'rb').read(), open(key, 'rb').read()
    os.replace(crt + '.previous', crt + '.tmp')
    os.replace(key + '.previous', key + '.tmp')
    os.replace(crt + '.tmp', crt)
    os.replace(key + '.tmp', key)
    with open(crt + '.previous', 'wb') as f:
        f.write(cur[0])
    fd = os.open(key + '.previous', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(cur[1])
    origin_p = os.path.join(os.path.dirname(crt), ORIGIN_FILE)
    with open(origin_p, 'w') as f:
        f.write('generated' if _is_self_signed(_load_cert()) else 'imported')
    return info()


def _load_cert(path=None):
    return x509.load_pem_x509_certificates(open(path or paths()[0], 'rb').read())[0]


def _is_self_signed(cert):
    return cert.issuer == cert.subject


def fingerprint(path=None):
    """SHA-256 of the DER certificate, upper-case colon-separated (what Proxmox pins)."""
    der = _load_cert(path).public_bytes(serialization.Encoding.DER)
    return ':'.join(f'{b:02X}' for b in hashlib.sha256(der).digest())


def mark_running():
    """Called once the listener is up: the fingerprint it serves, for restart_needed."""
    global _running_fp
    try:
        _running_fp = fingerprint()
    except (OSError, ValueError):
        _running_fp = None
    return _running_fp


def is_self_signed():
    try:
        return _is_self_signed(_load_cert())
    except (OSError, ValueError):
        return True


def info():
    crt, key = paths()
    cert = _load_cert(crt)
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    chain = x509.load_pem_x509_certificates(open(crt, 'rb').read())[1:]
    origin = 'environment' if _managed_outside() else None
    if not origin:
        try:
            origin = open(os.path.join(os.path.dirname(crt), ORIGIN_FILE)).read().strip() or 'generated'
        except OSError:
            origin = 'generated' if _is_self_signed(cert) else 'imported'
    fp = fingerprint(crt)
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    now = datetime.datetime.now(datetime.timezone.utc)
    return {'subject': cert.subject.rfc4514_string(), 'common_name': cn[0].value if cn else None, 'names': names,
            'issuer': cert.issuer.rfc4514_string(), 'self_signed': _is_self_signed(cert),
            'not_before': cert.not_valid_before_utc.timestamp(), 'not_after': cert.not_valid_after_utc.timestamp(),
            'days_left': (cert.not_valid_after_utc - now).days, 'fingerprint': fp, 'serial': format(cert.serial_number, 'x'),
            'chain': [c.subject.rfc4514_string() for c in chain], 'origin': origin, 'managed_outside': _managed_outside(),
            'path': crt, 'running_fingerprint': _running_fp, 'restart_needed': bool(_running_fp) and _running_fp != fp,
            'previous_available': not _managed_outside() and os.path.exists(crt + '.previous') and os.path.exists(key + '.previous')}


def pins():
    """What holds on to the server certificate and how to update it."""
    import json
    from .. import db
    out = []
    with db.conn() as c:
        for r in c.execute("SELECT id, name, type, info_json FROM connections ORDER BY name"):
            info_ = {}
            try:
                info_ = json.loads(r['info_json'] or '{}')
            except ValueError:
                pass
            if r['type'] == 'proxmox' and info_.get('pve_storage'):
                out.append({'kind': 'pve_storage', 'connection_id': r['id'], 'connection': r['name'], 'storage': info_['pve_storage'].get('storage') if isinstance(info_['pve_storage'], dict) else info_['pve_storage'],
                            'what': 'The hvbackup storage on this cluster pins the fingerprint; the nodes refuse the new certificate until it is re-registered.',
                            'action': 'pve-storage'})
            elif r['type'] == 'vcenter':
                out.append({'kind': 'vsphere_plugin', 'connection_id': r['id'], 'connection': r['name'],
                            'what': 'The vSphere Client plug-in registration records the thumbprint; vCenter stops loading the plug-in until it is updated.',
                            'action': 'vsphere-plugin'})
        proxies = c.execute('SELECT COUNT(*) FROM proxies WHERE enabled=1').fetchone()[0]
    if proxies:
        out.append({'kind': 'proxies', 'count': proxies,
                    'what': f'{proxies} proxy agent(s): those running HVB_INSECURE=1 (deployed while the certificate was self-signed) keep working; '
                            'those verifying by HVB_CA need the new certificate from "Download" in their agent.conf.', 'action': None})
    out.append({'kind': 'dashboard', 'what': 'The Nexus Dashboard plug-in and the CLI verify only when their client.conf says so (HVB_INSECURE=0 or HVB_CA); update the CA file there.', 'action': None})
    return out
