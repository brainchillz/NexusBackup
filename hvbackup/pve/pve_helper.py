#!/usr/bin/env python3
"""hvbackup Proxmox node helper. Called by the Perl plug-ins; the data path of backups and
restores. Config from the environment (HVB_URL, HVB_PROXY_TOKEN, HVB_INSECURE, HVB_FINGERPRINT)
or --env-file. Every command prints one JSON object on stdout; progress goes to stderr as
'progress ...' / 'info ...' lines. Holds no hypervisor credentials."""
import argparse
import hashlib
import json
import mmap
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import blake3
import requests
import urllib3
import zstandard

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
CHUNK = 4 * 1024 * 1024
RECORD = struct.Struct('<Q32s')
ZERO_HASH = b'\0' * 32
VERSION = '0.1.0'


def info(msg):
    sys.stderr.write(f'info {msg}\n')
    sys.stderr.flush()


def progress(msg):
    sys.stderr.write(f'progress {msg}\n')
    sys.stderr.flush()


def load_env(path):
    if path:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


class Server:
    def __init__(self):
        self.url = os.environ['HVB_URL'].rstrip('/')
        self.token = os.environ['HVB_PROXY_TOKEN']
        self.verify = os.environ.get('HVB_CA') or (os.environ.get('HVB_INSECURE', '1') != '1')
        self.local = threading.local()
        fp = os.environ.get('HVB_FINGERPRINT')
        if fp:
            self.check_fingerprint(fp)

    def check_fingerprint(self, want):
        from urllib.parse import urlparse
        u = urlparse(self.url)
        pem = ssl.get_server_certificate((u.hostname, u.port or 443))
        der = ssl.PEM_cert_to_DER_cert(pem)
        got = ':'.join(f'{b:02X}' for b in hashlib.sha256(der).digest())
        if got.upper() != want.upper():
            raise SystemExit(json.dumps({'error': f'server certificate fingerprint {got} does not match the storage fingerprint {want}'}))

    def s(self):
        s = getattr(self.local, 's', None)
        if s is None:
            s = requests.Session()
            s.headers['Authorization'] = f'Bearer {self.token}'
            s.verify = self.verify
            self.local.s = s
        return s

    def call(self, method, path, timeout=300, **kw):
        r = self.s().request(method, self.url + path, timeout=timeout, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get('detail')
            except Exception:  # noqa: BLE001
                detail = r.text[:300]
            raise RuntimeError(f'{method} {path}: HTTP {r.status_code}: {detail}')
        if r.headers.get('content-type', '').startswith('application/json'):
            return r.json()
        return r.content


# ── chunk pipeline ───────────────────────────────────────────────────
class Uploader:
    """Hash chunks, ask the server which are missing, upload those compressed. Collects records."""

    def __init__(self, srv, workers=4, bwlimit=0):
        self.srv, self.bw = srv, bwlimit
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.records = []
        self.bytes_read = self.bytes_uploaded = self.chunks_new = 0
        self.pending = []  # (idx, hash, data)
        self.lock = threading.Lock()
        self.errors = []
        self.t0 = time.time()
        self.last_prog = 0

    def add(self, idx, data):
        self.bytes_read += len(data)
        if len(data) < CHUNK:
            data = data + bytes(CHUNK - len(data))
        if data == ZERO_CHUNK:
            self.records.append((idx, ZERO_HASH))
        else:
            h = blake3.blake3(data).digest()
            self.records.append((idx, h))
            self.pending.append((idx, h, data))
        if len(self.pending) >= 64:
            self.flush()
        if time.time() - self.last_prog > 10:
            progress(f'{self.bytes_read / 2**20:.0f} MiB read, {self.bytes_uploaded / 2**20:.0f} MiB uploaded')
            self.last_prog = time.time()

    def flush(self):
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        hashes = {h.hex(): data for _, h, data in batch}
        missing = set(self.srv.call('POST', '/api/v1/agent/have', json={'hashes': list(hashes)})['missing'])
        futs = [self.pool.submit(self._upload, hx, hashes[hx]) for hx in missing]
        for f in futs:
            f.result()

    def _upload(self, hx, data):
        z = zstandard.ZstdCompressor(level=3).compress(data)  # compressor contexts are not thread-safe: one per call
        res = self.srv.call('PUT', f'/api/v1/agent/chunks/{hx}', data=z, headers={'Content-Type': 'application/zstd'})
        with self.lock:
            self.bytes_uploaded += len(z)
            if res.get('new'):
                self.chunks_new += 1
        if self.bw:
            time.sleep(len(z) / self.bw)

    def finish(self):
        self.flush()
        self.pool.shutdown()
        return {'bytes_read': self.bytes_read, 'bytes_uploaded': self.bytes_uploaded, 'chunks_new': self.chunks_new,
                'seconds': time.time() - self.t0, 'records': len(self.records)}

    def packed(self):
        return b''.join(RECORD.pack(i, h) for i, h in self.records)


ZERO_CHUNK = bytes(CHUNK)


class SnapshotReader:
    """Reads the snapshot access Proxmox handed us: an inherited O_DIRECT fd (aligned reads
    through a page-aligned buffer) with a fallback to opening the virtual file by path."""

    def __init__(self, fd, path):
        self.fd, self.path = fd, path
        self.buf = mmap.mmap(-1, CHUNK)
        self.alt = None

    def read(self, off, length):
        if self.fd >= 0 and off % 4096 == 0 and length % 4096 == 0:
            try:
                n = os.preadv(self.fd, [memoryview(self.buf)[:length]], off)
                return bytes(self.buf[:n])
            except OSError as e:
                info(f'direct read at {off} failed ({e}); using buffered reads')
                self.fd = -1
        if self.alt is None:
            if not self.path:
                raise RuntimeError('no readable snapshot access (no path for a buffered fallback)')
            self.alt = os.open(self.path, os.O_RDONLY)
        return os.pread(self.alt, length, off)


def cmd_volume(srv, a):
    req = json.load(sys.stdin)
    regions = [(int(o), int(n)) for o, n in req.get('regions', [])]
    size = int(a.size)
    up = Uploader(srv, bwlimit=int(a.bwlimit or 0))
    if regions:
        rd = SnapshotReader(int(a.fd), a.path)
        # chunks touched by the regions, with the dirty sub-ranges inside each
        touched = {}
        for off, n in regions:
            end = min(off + n, size)
            c = off // CHUNK
            while c * CHUNK < end:
                cs, ce = c * CHUNK, min((c + 1) * CHUNK, size)
                lo, hi = max(off, cs), min(end, ce)
                touched.setdefault(c, []).append((lo - cs, hi - cs))
                c += 1
        prev_index = None
        for c in sorted(touched):
            cs, ce = c * CHUNK, min((c + 1) * CHUNK, size)
            parts = touched[c]
            if len(parts) == 1 and parts[0] == (0, ce - cs):
                up.add(c, rd.read(cs, ce - cs))
                continue
            # partially dirty chunk: compose from the previous restore point's chunk + dirty parts
            if prev_index is None:
                try:
                    prev_index = srv.call('GET', f'/api/v1/pve/backup/{a.run}/prev/{a.dev}')['index']
                except RuntimeError:
                    prev_index = []
            ph = prev_index[c] if c < len(prev_index) else ZERO_HASH.hex()
            if ph == ZERO_HASH.hex():
                base_data = bytearray(ce - cs)
            else:
                z = srv.call('GET', f'/api/v1/agent/chunks/{ph}')
                base_data = bytearray(zstandard.ZstdDecompressor().decompress(z, max_output_size=CHUNK)[:ce - cs])
            for lo, hi in parts:
                base_data[lo:hi] = rd.read(cs + lo, hi - lo)
            up.add(c, bytes(base_data))
    st = up.finish()
    srv.call('PUT', f'/api/v1/pve/backup/{a.run}/volume/{a.dev}', params={'size': size, 'bitmap_mode': a.bitmap_mode},
             data=up.packed(), headers={'Content-Type': 'application/octet-stream'}, timeout=900)
    return st


TAR_FLAGS = ['--sparse', '--numeric-owner', '--acls', '--xattrs', '--xattrs-include=user.*', '--xattrs-include=security.capability',
             '--warning=no-file-ignored', '--warning=no-xattr-write', '--one-file-system']


def cmd_tar(srv, a):
    req = json.load(sys.stdin)
    excludes = []
    for pat in req.get('excludes') or []:
        pat = pat.replace('**', '*')
        excludes.append('--exclude=' + ('.' + pat if pat.startswith('/') else './*' + pat))
    cmd = ['tar', 'cpf', '-', f'--directory={req["directory"]}', '--anchored', *TAR_FLAGS, *excludes, *(req.get('sources') or ['.'])]
    info('tar ' + ' '.join(cmd[1:]))
    if req.get('guest_config') is not None:
        srv.call('POST', f'/api/v1/pve/backup/{a.run}/meta', json={'guest_config': req.get('guest_config'), 'firewall_config': req.get('firewall_config')})
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    up = Uploader(srv, bwlimit=int(a.bwlimit or 0))
    idx, total = 0, 0
    buf = bytearray()
    while True:
        data = p.stdout.read(CHUNK - len(buf))
        if not data:
            break
        buf += data
        if len(buf) == CHUNK:
            up.add(idx, bytes(buf))
            idx += 1
            total += len(buf)
            buf = bytearray()
    if buf:
        up.add(idx, bytes(buf))
        total += len(buf)
    err = p.stderr.read().decode('utf-8', 'replace')
    rc = p.wait()
    if rc not in (0, 1):  # 1 = some files changed while reading
        raise RuntimeError(f'tar exited {rc}: {err[-500:]}')
    if err.strip():
        for ln in err.strip().splitlines()[-5:]:
            info('tar: ' + ln)
    st = up.finish()
    srv.call('PUT', f'/api/v1/pve/backup/{a.run}/volume/{a.dev}', params={'size': total, 'bitmap_mode': 'none'},
             data=up.packed(), headers={'Content-Type': 'application/octet-stream'}, timeout=900)
    srv.call('POST', f'/api/v1/pve/backup/{a.run}/meta', json={'bytes_read': st['bytes_read'], 'bytes_uploaded': st['bytes_uploaded'], 'chunks_new': st['chunks_new']})
    return st


# ── restore: chunks on demand ────────────────────────────────────────
class ChunkSource:
    def __init__(self, srv, index, size, prefetch=4, cache=48):
        self.srv, self.index, self.size = srv, index, size
        self.cache, self.order = {}, []
        self.cache_max = cache
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=prefetch)
        self.futs = {}
        self.n = (size + CHUNK - 1) // CHUNK

    def _fetch(self, c):
        hx = self.index[c] if c < len(self.index) else ZERO_HASH.hex()
        if hx == ZERO_HASH.hex():
            return None
        z = self.srv.call('GET', f'/api/v1/agent/chunks/{hx}')
        return zstandard.ZstdDecompressor().decompress(z, max_output_size=CHUNK)

    def chunk(self, c):
        with self.lock:
            if c in self.cache:
                return self.cache[c]
            f = self.futs.pop(c, None)
            if f is None:
                f = self.pool.submit(self._fetch, c)
            for nxt in range(c + 1, min(c + 1 + self.pool._max_workers, self.n)):
                if nxt not in self.cache and nxt not in self.futs:
                    self.futs[nxt] = self.pool.submit(self._fetch, nxt)
        data = f.result()
        with self.lock:
            self.cache[c] = data
            self.order.append(c)
            while len(self.order) > self.cache_max:
                old = self.order.pop(0)
                self.cache.pop(old, None)
        return data

    def read(self, off, length):
        out = bytearray()
        while length > 0:
            c, inner = divmod(off, CHUNK)
            n = min(length, CHUNK - inner, self.size - off)
            if n <= 0:
                break
            data = self.chunk(c)
            out += bytes(n) if data is None else data[inner:inner + n]
            off += n
            length -= n
        return bytes(out)


NBD_MAGIC = 0x4e42444d41474943
IHAVEOPT = 0x49484156454F5054
REP_MAGIC = 0x3e889045565a9
REQ_MAGIC = 0x25609513
SIMPLE_REPLY = 0x67446698
OPT_EXPORT_NAME, OPT_ABORT, OPT_LIST, OPT_INFO, OPT_GO = 1, 2, 3, 6, 7
REP_ACK, REP_SERVER, REP_INFO, REP_ERR_UNSUP = 1, 2, 3, 0x80000001
FLAG_FIXED_NEWSTYLE, FLAG_NO_ZEROES = 1, 2
TFLAGS = 1 | 2 | 8  # HAS_FLAGS | READ_ONLY | CAN_MULTI_CONN


def _recvn(c, n):
    buf = b''
    while len(buf) < n:
        d = c.recv(n - len(buf))
        if not d:
            raise ConnectionError('client closed')
        buf += d
    return buf


def _rep(c, opt, rtype, data=b''):
    c.sendall(struct.pack('>QIII', REP_MAGIC, opt, rtype, len(data)) + data)


def nbd_connection(c, src, name):
    c.sendall(struct.pack('>QQH', NBD_MAGIC, IHAVEOPT, FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
    cflags = struct.unpack('>I', _recvn(c, 4))[0]
    no_zeroes = bool(cflags & FLAG_NO_ZEROES)
    while True:
        magic, opt, length = struct.unpack('>QII', _recvn(c, 16))
        data = _recvn(c, length) if length else b''
        if opt == OPT_EXPORT_NAME:
            c.sendall(struct.pack('>QH', src.size, TFLAGS) + (b'' if no_zeroes else bytes(124)))
            break
        if opt in (OPT_INFO, OPT_GO):
            _rep(c, opt, REP_INFO, struct.pack('>HQH', 0, src.size, TFLAGS))
            _rep(c, opt, REP_INFO, struct.pack('>HIII', 3, 1, 4096, 32 * 1024 * 1024))  # block sizes: min, preferred, max
            _rep(c, opt, REP_ACK)
            if opt == OPT_GO:
                break
        elif opt == OPT_LIST:
            _rep(c, opt, REP_SERVER, struct.pack('>I', len(name)) + name.encode())
            _rep(c, opt, REP_ACK)
        elif opt == OPT_ABORT:
            _rep(c, opt, REP_ACK)
            return
        else:
            _rep(c, opt, REP_ERR_UNSUP)
    while True:
        magic, flags, rtype, handle, off, length = struct.unpack('>IHHQQI', _recvn(c, 28))
        if magic != REQ_MAGIC:
            raise ConnectionError('bad request magic')
        if rtype == 0:  # READ
            if off + length > src.size:
                c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 22, handle))
                continue
            data = src.read(off, length)
            c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 0, handle) + data)
        elif rtype == 2:  # DISC
            return
        elif rtype == 3:  # FLUSH
            c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 0, handle))
        else:
            if rtype == 1 and length:
                _recvn(c, length)
            c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 1, handle))  # EPERM


def cmd_nbd_serve(srv, a):
    vols = srv.call('GET', f'/api/v1/pve/archives/{a.run}/volumes')
    v = vols['volumes'].get(a.dev)
    if not v:
        raise RuntimeError(f'no volume {a.dev} in archive of run {a.run}')
    src = ChunkSource(srv, v['index'], int(v['size']))
    try:
        os.unlink(a.socket)
    except FileNotFoundError:
        pass
    ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ls.bind(a.socket)
    os.chmod(a.socket, 0o600)
    ls.listen(8)
    if a.pidfile:
        open(a.pidfile, 'w').write(str(os.getpid()))
    import signal
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    while True:
        c, _ = ls.accept()
        threading.Thread(target=_serve_one, args=(c, src, a.dev), daemon=True).start()


def _serve_one(c, src, name):
    try:
        nbd_connection(c, src, name)
    except (ConnectionError, OSError):
        pass
    finally:
        c.close()


def cmd_fetch_tar(srv, a):
    vols = srv.call('GET', f'/api/v1/pve/archives/{a.run}/volumes')
    v = vols['volumes'].get(a.dev)
    if not v:
        raise RuntimeError(f'no volume {a.dev} in archive of run {a.run}')
    src = ChunkSource(srv, v['index'], int(v['size']), prefetch=6)
    size = int(v['size'])
    t0 = time.time()
    with open(a.out, 'wb') as f:
        off = 0
        while off < size:
            n = min(CHUNK, size - off)
            data = src.chunk(off // CHUNK)
            if data is None:
                f.seek(n, 1)
            else:
                f.write(data[:n])
            off += n
            if off % (256 * CHUNK) == 0:
                progress(f'{off / 2**20:.0f} of {size / 2**20:.0f} MiB fetched')
        f.truncate(size)
    os.chmod(a.out, 0o600)
    return {'bytes': size, 'seconds': time.time() - t0, 'path': a.out}


# ── thin control calls ───────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd')
    ap.add_argument('--env-file')
    ap.add_argument('--run'); ap.add_argument('--dev'); ap.add_argument('--vmid'); ap.add_argument('--vmtype'); ap.add_argument('--node')
    ap.add_argument('--start-time'); ap.add_argument('--size'); ap.add_argument('--bitmap-mode', default='none'); ap.add_argument('--fd', default='-1')
    ap.add_argument('--path', default=''); ap.add_argument('--bwlimit', default='0'); ap.add_argument('--socket'); ap.add_argument('--pidfile')
    ap.add_argument('--out')
    a = ap.parse_args(argv)
    load_env(a.env_file)
    try:
        srv = Server()
        if a.cmd == 'status':
            res = srv.call('GET', '/api/v1/pve/status', timeout=30)
        elif a.cmd == 'init':
            res = srv.call('POST', '/api/v1/pve/backup/init', json={'vmid': int(a.vmid), 'vmtype': a.vmtype, 'node': a.node, 'start_time': a.start_time})
        elif a.cmd == 'incremental':
            res = srv.call('POST', f'/api/v1/pve/backup/{a.run}/incremental', json=json.load(sys.stdin))
        elif a.cmd == 'finish':
            res = srv.call('POST', f'/api/v1/pve/backup/{a.run}/finish', json=json.load(sys.stdin), timeout=600)
        elif a.cmd == 'log':
            res = srv.call('POST', f'/api/v1/pve/backup/{a.run}/log', data=sys.stdin.buffer.read(), headers={'Content-Type': 'text/plain'})
        elif a.cmd == 'archives':
            res = {'archives': srv.call('GET', '/api/v1/pve/archives', params={'vmid': a.vmid} if a.vmid else None, timeout=60)}
        elif a.cmd == 'archive-config':
            res = srv.call('GET', f'/api/v1/pve/archives/{a.run}/config', timeout=60)
        elif a.cmd == 'archive-volumes':
            res = srv.call('GET', f'/api/v1/pve/archives/{a.run}/volumes', timeout=300)
            res['volumes'] = {k: {'size': v['size'], 'allocated': v.get('allocated')} for k, v in res['volumes'].items()}
        elif a.cmd == 'delete':
            res = srv.call('DELETE', f'/api/v1/pve/archives/{a.run}', timeout=120)
        elif a.cmd == 'volume':
            res = cmd_volume(srv, a)
        elif a.cmd == 'tar':
            res = cmd_tar(srv, a)
        elif a.cmd == 'nbd-serve':
            cmd_nbd_serve(srv, a)
            return
        elif a.cmd == 'fetch-tar':
            res = cmd_fetch_tar(srv, a)
        else:
            raise RuntimeError(f'unknown command {a.cmd}')
    except Exception as e:  # noqa: BLE001
        print(json.dumps({'error': f'{type(e).__name__}: {e}'}))
        sys.exit(1)
    print(json.dumps(res if isinstance(res, dict) else {'result': res}, default=str))


if __name__ == '__main__':
    main()
