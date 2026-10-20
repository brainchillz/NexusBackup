"""Configuration. One file (`/etc/hvbackup/hvbackup.conf`, KEY=VALUE) for both the
package and the container install; environment variables override the file.

  HVB_DATA_DIR    catalog, secrets, logs            default /var/lib/hvbackup
  HVB_REPO        backup repository directory       default <data>/repo
  HVB_BIND        listen address                    default 0.0.0.0
  HVB_PORT        listen port                       default 8460
  HVB_URL         public base URL (what proxies and browsers reach)
  HVB_TLS_CERT / HVB_TLS_KEY   optional PEM pair; self-signed is generated when unset
  HVB_CONFIG      path of the config file           default /etc/hvbackup/hvbackup.conf
"""
import os

CONFIG_FILE = os.environ.get('HVB_CONFIG', '/etc/hvbackup/hvbackup.conf')


def _load_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


_file = _load_file(CONFIG_FILE)


def get(key, default=None):
    return os.environ.get(key, _file.get(key, default))


DATA_DIR = get('HVB_DATA_DIR', '/var/lib/hvbackup')
REPO_DIR = get('HVB_REPO', os.path.join(DATA_DIR, 'repo'))
BIND = get('HVB_BIND', '0.0.0.0')
PORT = int(get('HVB_PORT', '8460'))
PUBLIC_URL = get('HVB_URL', '')
TLS_CERT = get('HVB_TLS_CERT', '')
TLS_KEY = get('HVB_TLS_KEY', '')
CHUNK_SIZE = 4 * 1024 * 1024
LOG_LEVEL = get('HVB_LOG_LEVEL', 'info')

DB_PATH = os.path.join(DATA_DIR, 'catalog.db')
SECRET_PATH = os.path.join(DATA_DIR, 'secret.key')
CERT_DIR = os.path.join(DATA_DIR, 'certs')
