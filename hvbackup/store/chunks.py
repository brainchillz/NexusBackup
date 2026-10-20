"""Content-addressed chunk store.

repo/
  repo.json            format version, repository id, chunk size
  chunks/aa/bb/<hash>  zstd frame of one fixed-size chunk, named by BLAKE3 of the raw bytes
  manifests/<vm uuid>/<restore point id>.json + .<disk>.idx
  staging/             scratch, safe to clear

Zero chunks are never stored: an index entry of 32 zero bytes means "all zeros".
"""
import json
import os
import threading
import secrets
import tempfile
import time

import blake3
import zstandard

from .. import config

ZERO_HASH = b'\0' * 32
FORMAT_VERSION = 1


def hash_bytes(b):
    return blake3.blake3(b, max_threads=blake3.blake3.AUTO).digest()


class Repository:
    def __init__(self, path=None):
        self.path = path or config.REPO_DIR
        self.chunks = os.path.join(self.path, 'chunks')
        self.manifests = os.path.join(self.path, 'manifests')
        self.staging = os.path.join(self.path, 'staging')
        self.meta = None
        self._tl = threading.local()  # zstd contexts are per thread

    # ── lifecycle ────────────────────────────────────────────────────
    def open(self, create=True, adopt=False):
        mp = os.path.join(self.path, 'repo.json')
        if os.path.exists(mp):
            self.meta = json.load(open(mp))
            if self.meta.get('format', 0) > FORMAT_VERSION:
                raise RuntimeError(f'repository {self.path} has format {self.meta["format"]}, newer than this server')
        else:
            if not create:
                raise RuntimeError(f'{self.path} is not a repository')
            if os.path.isdir(self.path) and os.listdir(self.path) and not adopt:
                raise RuntimeError(f'{self.path} is not empty and not a repository; pass adopt to use it anyway')
            os.makedirs(self.path, exist_ok=True)
            self.meta = {'format': FORMAT_VERSION, 'id': secrets.token_hex(8), 'chunk_size': config.CHUNK_SIZE,
                         'created': time.time()}
            self._write_json(mp, self.meta)
        for d in (self.chunks, self.manifests, self.staging):
            os.makedirs(d, exist_ok=True)
        return self

    @property
    def id(self):
        return self.meta['id']

    @property
    def chunk_size(self):
        return self.meta['chunk_size']

    @property
    def _cctx(self):
        c = getattr(self._tl, 'c', None)
        if c is None:
            c = self._tl.c = zstandard.ZstdCompressor(level=3)
        return c

    @property
    def _dctx(self):
        d = getattr(self._tl, 'd', None)
        if d is None:
            d = self._tl.d = zstandard.ZstdDecompressor()
        return d

    # ── chunks ───────────────────────────────────────────────────────
    def chunk_path(self, h):
        x = h.hex() if isinstance(h, bytes) else h
        return os.path.join(self.chunks, x[:2], x[2:4], x)

    def has(self, h):
        return h == ZERO_HASH or os.path.exists(self.chunk_path(h))

    def missing(self, hashes):
        return [h for h in hashes if not self.has(h)]

    def put_raw(self, data, expect_hash=None):
        """Store an uncompressed chunk. Returns (hash, stored_bytes, was_new)."""
        h = hash_bytes(data)
        if expect_hash is not None and h != expect_hash:
            raise ValueError('chunk hash mismatch')
        if not any(data):
            return ZERO_HASH, 0, False
        return self._put(h, self._cctx.compress(data))

    def put_compressed(self, zdata, expect_hash):
        """Store a chunk that arrived zstd-compressed; verify its hash after decompression."""
        data = self._dctx.decompress(zdata, max_output_size=self.chunk_size)
        h = hash_bytes(data)
        if h != expect_hash:
            raise ValueError('chunk hash mismatch')
        return self._put(h, zdata)

    def _put(self, h, zdata):
        p = self.chunk_path(h)
        if os.path.exists(p):
            return h, 0, False
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix='.tmp-')
        with os.fdopen(fd, 'wb') as f:
            f.write(zdata)
        os.replace(tmp, p)
        return h, len(zdata), True

    def get(self, h):
        if h == ZERO_HASH:
            return bytes(self.chunk_size)
        with open(self.chunk_path(h), 'rb') as f:
            return self._dctx.decompress(f.read(), max_output_size=self.chunk_size)

    def get_compressed(self, h):
        with open(self.chunk_path(h), 'rb') as f:
            return f.read()

    # ── manifests ────────────────────────────────────────────────────
    def manifest_dir(self, vm_uuid):
        d = os.path.join(self.manifests, vm_uuid)
        os.makedirs(d, exist_ok=True)
        return d

    def write_index(self, vm_uuid, rp_id, disk_key, hashes):
        p = os.path.join(self.manifest_dir(vm_uuid), f'{rp_id}.{disk_key}.idx')
        tmp = p + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(b''.join(hashes))
        os.replace(tmp, p)
        return os.path.relpath(p, self.path)

    def read_index(self, rel):
        with open(os.path.join(self.path, rel), 'rb') as f:
            data = f.read()
        return [data[i:i + 32] for i in range(0, len(data), 32)]

    def write_manifest(self, vm_uuid, rp_id, manifest):
        p = os.path.join(self.manifest_dir(vm_uuid), f'{rp_id}.json')
        self._write_json(p, manifest)
        return os.path.relpath(p, self.path)

    def read_manifest(self, rel):
        return json.load(open(os.path.join(self.path, rel)))

    def delete_manifest(self, manifest_rel):
        m = self.read_manifest(manifest_rel)
        for d in m['disks']:
            try:
                os.remove(os.path.join(self.path, d['index']))
            except FileNotFoundError:
                pass
        os.remove(os.path.join(self.path, manifest_rel))

    def all_manifests(self):
        for vm in os.listdir(self.manifests):
            d = os.path.join(self.manifests, vm)
            for f in os.listdir(d):
                if f.endswith('.json'):
                    yield os.path.relpath(os.path.join(d, f), self.path)

    # ── garbage collection ───────────────────────────────────────────
    def gc(self, grace_seconds=3600, log=lambda m: None):
        """Mark every chunk referenced by any manifest, sweep the rest. Chunks younger
        than grace_seconds are kept so an in-flight run cannot lose data."""
        live = set()
        for rel in self.all_manifests():
            m = self.read_manifest(rel)
            for d in m['disks']:
                live.update(h for h in self.read_index(d['index']) if h != ZERO_HASH)
        log(f'{len(live)} live chunks referenced by manifests')
        removed = freed = kept = 0
        now = time.time()
        for a in os.listdir(self.chunks):
            for b in os.listdir(os.path.join(self.chunks, a)):
                d = os.path.join(self.chunks, a, b)
                for f in os.listdir(d):
                    if f.startswith('.tmp-'):
                        continue
                    p = os.path.join(d, f)
                    if bytes.fromhex(f) in live:
                        kept += 1
                        continue
                    st = os.stat(p)
                    if now - st.st_mtime < grace_seconds:
                        kept += 1
                        continue
                    os.remove(p)
                    removed += 1
                    freed += st.st_size
        log(f'removed {removed} chunks, freed {freed / 2**30:.2f} GiB, kept {kept}')
        return {'removed': removed, 'freed_bytes': freed, 'kept': kept, 'live': len(live)}

    def stats(self):
        n = size = 0
        for a in os.listdir(self.chunks):
            for b in os.listdir(os.path.join(self.chunks, a)):
                d = os.path.join(self.chunks, a, b)
                with os.scandir(d) as it:
                    for e in it:
                        n += 1
                        size += e.stat().st_size
        st = os.statvfs(self.path)
        return {'chunks': n, 'stored_bytes': size, 'free_bytes': st.f_bavail * st.f_frsize,
                'total_bytes': st.f_blocks * st.f_frsize}

    @staticmethod
    def _write_json(p, obj):
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, p)


_repo = None


def repo():
    global _repo
    if _repo is None:
        _repo = Repository().open(create=True, adopt=True)
    return _repo
