"""Secrets at rest: connection passwords are Fernet-encrypted with a key kept in the
data directory (0600). Losing the key means re-entering hypervisor credentials, nothing else."""
import os

from cryptography.fernet import Fernet

from .. import config

_f = None


def _fernet():
    global _f
    if _f is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        if not os.path.exists(config.SECRET_PATH):
            fd = os.open(config.SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        _f = Fernet(open(config.SECRET_PATH, 'rb').read().strip())
    return _f


def encrypt(s):
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s):
    return _fernet().decrypt(s.encode()).decode()


def signing_key():
    """Bytes used to sign session cookies; derived from the same key file."""
    return open(config.SECRET_PATH, 'rb').read().strip() if os.path.exists(config.SECRET_PATH) else _fernet() and open(config.SECRET_PATH, 'rb').read().strip()
