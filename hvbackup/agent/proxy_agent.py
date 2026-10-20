#!/usr/bin/env python3
"""hvbackup proxy agent. Runs inside a small VM on the hypervisor cluster, as root.

It holds no hypervisor credentials. It long-polls the backup server for work:
  read   a disk the server hot-added to this VM: hash its chunks, upload the ones
         the server does not have, report (index, hash) records
  write  a disk the server hot-added for a restore: download chunks, write them

Config (/etc/hvbackup/agent.conf or environment):
  HVB_URL          https://backup-server:8460
  HVB_PROXY_TOKEN  hvp_... minted by the server when the proxy was created
  HVB_CA           optional CA bundle; HVB_INSECURE=1 skips TLS verification
"""
import glob
import logging
import os
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import blake3
import requests
import urllib3
import zstandard

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger('hvbackup.agent')
RECORD = struct.Struct('<Q32s')
ZERO_HASH = b'\0' * 32
VERSION = '0.1.0'


def load_conf():
    conf = {}
    for p in ('/etc/hvbackup/agent.conf',):
        try:
            for line in open(p):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    conf[k.strip()] = v.strip().strip('"')
        except FileNotFoundError:
            pass
    conf.update({k: v for k, v in os.environ.items() if k.startswith('HVB_')})
    return conf


class Server:
    def __init__(self, conf):
        self.url = conf['HVB_URL'].rstrip('/')
        self.token = conf['HVB_PROXY_TOKEN']
        self.verify = conf.get('HVB_CA') or (conf.get('HVB_INSECURE', '0') != '1')
        self.local = threading.local()

    def s(self):
        s = getattr(self.local, 's', None)
        if s is None:
            s = requests.Session()
            s.headers['Authorization'] = 'Bearer ' + self.token
            s.verify = self.verify
            self.local.s = s
        return s

    def get(self, path, **kw):
        r = self.s().get(self.url + path, timeout=kw.pop('timeout', 60), **kw)
        r.raise_for_status()
        return r

    def post(self, path, **kw):
        r = self.s().post(self.url + path, timeout=kw.pop('timeout', 120), **kw)
        r.raise_for_status()
        return r

    def put(self, path, **kw):
        r = self.s().put(self.url + path, timeout=kw.pop('timeout', 300), **kw)
        r.raise_for_status()
        return r


def bios_uuid():
    try:
        return open('/sys/class/dmi/id/product_uuid').read().strip()
    except OSError:
        return ''


def rescan_scsi():
    for h in glob.glob('/sys/class/scsi_host/host*/scan'):
        try:
            open(h, 'w').write('- - -')
        except OSError:
            pass


def block_devices():
    out = {}
    for p in glob.glob('/sys/block/sd*') + glob.glob('/sys/block/nvme*n*'):
        try:
            out[os.path.basename(p)] = int(open(p + '/size').read()) * 512
        except OSError:
            pass
    return out


def find_device(size, bus, unit, before, timeout=60):
    """Locate the hot-added disk: by SCSI address first, by size among new devices second."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rescan_scsi()
        time.sleep(1)
        for p in glob.glob('/dev/disk/by-path/*-scsi-0:%d:%d:0' % (bus, unit)) + glob.glob('/dev/disk/by-path/*-scsi-%d:0:%d:0' % (bus, unit)):
            if 'part' in p:
                continue
            dev = os.path.realpath(p)
            try:
                if int(open(f'/sys/block/{os.path.basename(dev)}/size').read()) * 512 == size:
                    return dev
            except OSError:
                pass
        new = [d for d, s in block_devices().items() if d not in before and s == size]
        if len(new) == 1:
            return '/dev/' + new[0]
    raise RuntimeError(f'hot-added disk of {size} bytes at scsi {bus}:{unit} not visible; devices: {block_devices()}')


def settle_removed(dev, timeout=30):
    """After hot-remove, drop the stale node so the next add is unambiguous."""
    name = os.path.basename(dev)
    try:
        open(f'/sys/block/{name}/device/delete', 'w').write('1')
    except OSError:
        pass


# ── read job ──────────────────────────────────────────────────────────
def do_read(srv, work):
    spec = work['spec']
    size, cs = spec['size'], spec['chunk_size']
    dev = find_device(size, spec['bus'], spec['unit'], set(spec.get('known_devices', [])))
    log.info('work %s: reading %s (%d MiB in %d ranges)', work['id'], dev, sum(n for _, n in spec['ranges']) * cs >> 20, len(spec['ranges']))
    zero = bytes(cs)
    fd = os.open(dev, os.O_RDONLY)
    tl = threading.local()  # zstd contexts are not thread safe: one per worker thread
    records = []
    lock = threading.Lock()
    done = [0]
    last_report = [time.time()]

    t_start = time.time()
    uploaded = [0]

    def report():
        if time.time() - last_report[0] > 2:
            last_report[0] = time.time()
            el = time.time() - t_start
            log.info('work %s: %d MiB read, %d MiB uploaded, %.0f MiB/s', work['id'], done[0] >> 20, uploaded[0] >> 20, (done[0] >> 20) / max(el, 0.1))
            try:
                srv.post(f'/api/v1/agent/work/{work["id"]}/progress', json={'bytes': done[0]})
            except requests.RequestException:
                pass

    def process(batch):
        local = []
        hashed = []
        for idx in batch:
            off = idx * cs
            buf = os.pread(fd, cs, off)
            if len(buf) < cs:
                buf = buf + bytes(cs - len(buf))
            if buf == zero:
                local.append((idx, ZERO_HASH))
                continue
            h = blake3.blake3(buf, max_threads=1).digest()
            local.append((idx, h))
            hashed.append((h, buf))
        if hashed:
            want = {h.hex(): buf for h, buf in hashed}
            missing = srv.post(f'/api/v1/agent/work/{work["id"]}/have', json={'hashes': list(want)}).json()['missing']
            for hx in missing:
                cctx = getattr(tl, 'c', None) or setattr(tl, 'c', zstandard.ZstdCompressor(level=3)) or tl.c
                z = cctx.compress(want[hx])
                srv.put(f'/api/v1/agent/chunks/{hx}', data=z, headers={'Content-Type': 'application/zstd'})
                with lock:
                    uploaded[0] += len(z)
        with lock:
            records.extend(local)
            done[0] += len(batch) * cs
            report()

    batches = []
    cur = []
    for first, count in spec['ranges']:
        for idx in range(first, first + count):
            cur.append(idx)
            if len(cur) >= 8:
                batches.append(cur)
                cur = []
    if cur:
        batches.append(cur)
    with ThreadPoolExecutor(spec.get('workers', 4)) as ex:
        list(ex.map(process, batches))
    os.close(fd)
    records.sort()
    body = b''.join(RECORD.pack(i, h) for i, h in records)
    el = time.time() - t_start
    srv.post(f'/api/v1/agent/work/{work["id"]}/complete', data=body, headers={'Content-Type': 'application/octet-stream'}, timeout=600,
             params={'seconds': f'{el:.1f}', 'uploaded': uploaded[0]})
    log.info('work %s: done, %d chunks in %.0fs (%.0f MiB/s), %d MiB uploaded', work['id'], len(records), el, (done[0] >> 20) / max(el, 0.1), uploaded[0] >> 20)
    return dev


# ── write job ─────────────────────────────────────────────────────────
def do_write(srv, work):
    spec = work['spec']
    size, cs = spec['size'], spec['chunk_size']
    dev = find_device(size, spec['bus'], spec['unit'], set(spec.get('known_devices', [])))
    data = srv.get(f'/api/v1/agent/work/{work["id"]}/index', timeout=600).content
    recs = [RECORD.unpack_from(data, o) for o in range(0, len(data) - len(data) % RECORD.size, RECORD.size)]
    recs = [(i, h) for i, h in recs if h != ZERO_HASH]
    log.info('work %s: writing %d chunks to %s', work['id'], len(recs), dev)
    fd = os.open(dev, os.O_WRONLY)
    tl = threading.local()
    lock = threading.Lock()
    done = [0]
    last_report = [time.time()]

    def one(rec):
        idx, h = rec
        z = srv.get(f'/api/v1/agent/chunks/{h.hex()}', timeout=300).content
        dctx = getattr(tl, 'd', None) or setattr(tl, 'd', zstandard.ZstdDecompressor()) or tl.d
        buf = dctx.decompress(z, max_output_size=cs)
        if blake3.blake3(buf).digest() != h:
            raise RuntimeError(f'chunk {h.hex()} corrupt in transit')
        off = idx * cs
        n = min(cs, size - off)
        os.pwrite(fd, buf[:n], off)
        with lock:
            done[0] += n
            if time.time() - last_report[0] > 2:
                last_report[0] = time.time()
                try:
                    srv.post(f'/api/v1/agent/work/{work["id"]}/progress', json={'bytes': done[0]})
                except requests.RequestException:
                    pass

    with ThreadPoolExecutor(spec.get('workers', 4)) as ex:
        list(ex.map(one, recs))
    os.fsync(fd)
    os.close(fd)
    srv.post(f'/api/v1/agent/work/{work["id"]}/complete', data=b'', headers={'Content-Type': 'application/octet-stream'})
    log.info('work %s: done', work['id'])
    return dev


def hello(srv):
    srv.post('/api/v1/agent/hello', json={'bios_uuid': bios_uuid(), 'hostname': socket.gethostname(),
                                          'version': VERSION, 'devices': list(block_devices())})


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    conf = load_conf()
    if 'HVB_URL' not in conf or 'HVB_PROXY_TOKEN' not in conf:
        sys.exit('HVB_URL and HVB_PROXY_TOKEN are required (/etc/hvbackup/agent.conf)')
    if os.geteuid() != 0:
        sys.exit('the agent must run as root (SCSI rescan and raw disk access)')
    srv = Server(conf)
    backoff = 5
    while True:
        try:
            hello(srv)
            backoff = 5
            r = srv.get('/api/v1/agent/work', params={'wait': 25}, timeout=60)
            if r.status_code == 204 or not r.content:
                continue
            work = r.json()
            dev = None
            try:
                if work['kind'] == 'read':
                    dev = do_read(srv, work)
                elif work['kind'] == 'write':
                    dev = do_write(srv, work)
                else:
                    srv.post(f'/api/v1/agent/work/{work["id"]}/complete', data=b'')
            except Exception as e:  # noqa: BLE001
                log.exception('work %s failed', work['id'])
                try:
                    srv.post(f'/api/v1/agent/work/{work["id"]}/fail', json={'error': f'{type(e).__name__}: {e}'})
                except requests.RequestException:
                    pass
            finally:
                if dev:
                    settle_removed(dev)
        except requests.RequestException as e:
            log.warning('server unreachable: %s; retrying in %ds', e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except KeyboardInterrupt:
            return


if __name__ == '__main__':
    main()
