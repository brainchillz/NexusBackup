"""NBD on the server: export a restore point's disk read-only on a unix socket straight from
the chunk repository, and a small client to read an export (qemu-nbd's, for example).

The export is what qemu-img, libguestfs and virt-v2v open when a restore point is converted:
a qcow2 overlay with the export as its backing file gives the tools a writable disk while
the backup stays immutable and nothing is copied.
"""
import os
import socket
import struct
import threading

NBD_MAGIC = 0x4e42444d41474943
IHAVEOPT = 0x49484156454F5054
REP_MAGIC = 0x3e889045565a9
REQ_MAGIC = 0x25609513
SIMPLE_REPLY = 0x67446698
OPT_EXPORT_NAME, OPT_ABORT, OPT_LIST, OPT_INFO, OPT_GO = 1, 2, 3, 6, 7
REP_ACK, REP_SERVER, REP_INFO, REP_ERR_UNSUP = 1, 2, 3, 0x80000001
FLAG_FIXED_NEWSTYLE, FLAG_NO_ZEROES = 1, 2
TFLAGS = 1 | 2 | 8  # HAS_FLAGS | READ_ONLY | CAN_MULTI_CONN
CMD_READ, CMD_DISC, CMD_FLUSH = 0, 2, 3


def _recvn(c, n):
    buf = bytearray()
    while len(buf) < n:
        part = c.recv(n - len(buf))
        if not part:
            raise ConnectionError('peer closed')
        buf += part
    return bytes(buf)


def _rep(c, opt, rtype, data=b''):
    c.sendall(struct.pack('>QIII', REP_MAGIC, opt, rtype, len(data)) + data)


class IndexSource:
    """Read-only view of one disk: a list of chunk hashes over a repository."""

    def __init__(self, rep, hashes, size, cache_chunks=64):
        self.rep, self.hashes, self.size = rep, hashes, size
        self.cs = rep.chunk_size
        self.cache, self.order, self.cache_max = {}, [], cache_chunks
        self.lock = threading.Lock()

    def chunk(self, i):
        with self.lock:
            if i in self.cache:
                return self.cache[i]
        data = self.rep.get(self.hashes[i]) if i < len(self.hashes) else bytes(self.cs)
        with self.lock:
            self.cache[i] = data
            self.order.append(i)
            while len(self.order) > self.cache_max:
                self.cache.pop(self.order.pop(0), None)
        return data

    def read(self, off, length):
        out = bytearray()
        while length > 0:
            i, inner = divmod(off, self.cs)
            n = min(length, self.cs - inner, self.size - off)
            if n <= 0:
                break
            out += self.chunk(i)[inner:inner + n]
            off += n
            length -= n
        return bytes(out)


def serve_connection(c, src, name):
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
            _rep(c, opt, REP_INFO, struct.pack('>HIII', 3, 1, 4096, 32 * 1024 * 1024))
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
        if rtype == CMD_READ:
            if off + length > src.size:
                c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 22, handle))
                continue
            c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 0, handle) + src.read(off, length))
        elif rtype == CMD_DISC:
            return
        elif rtype == CMD_FLUSH:
            c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 0, handle))
        else:
            if rtype == 1 and length:
                _recvn(c, length)
            c.sendall(struct.pack('>IIQ', SIMPLE_REPLY, 1, handle))  # EPERM: read-only


class Export:
    """A read-only NBD export of a source on a unix socket, served by a background thread."""

    def __init__(self, src, path, name='disk'):
        self.src, self.path, self.name = src, path, name
        self.ls = None
        self.threads = []
        self.stopping = False

    def uri(self):
        return f'nbd+unix:///{self.name}?socket={self.path}'

    def start(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.ls.bind(self.path)
        os.chmod(self.path, 0o600)
        self.ls.listen(16)
        t = threading.Thread(target=self._accept, name=f'nbd-{self.name}', daemon=True)
        t.start()
        self.threads.append(t)
        return self

    def _accept(self):
        while not self.stopping:
            try:
                c, _ = self.ls.accept()
            except OSError:
                return
            t = threading.Thread(target=self._one, args=(c,), daemon=True)
            t.start()
            self.threads.append(t)

    def _one(self, c):
        try:
            serve_connection(c, self.src, self.name)
        except (ConnectionError, OSError):
            pass
        finally:
            c.close()

    def stop(self):
        self.stopping = True
        if self.ls:
            try:
                self.ls.close()
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


class Client:
    """Minimal NBD client (fixed newstyle, one connection): size and reads."""

    def __init__(self, path, name=''):
        self.c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.c.connect(path)
        magic, ihaveopt, flags = struct.unpack('>QQH', _recvn(self.c, 18))
        if magic != NBD_MAGIC or ihaveopt != IHAVEOPT:
            raise ConnectionError('not an NBD server')
        self.c.sendall(struct.pack('>I', FLAG_FIXED_NEWSTYLE | FLAG_NO_ZEROES))
        nm = name.encode()
        self.c.sendall(struct.pack('>QII', IHAVEOPT, OPT_GO, 4 + len(nm) + 2) + struct.pack('>I', len(nm)) + nm + struct.pack('>H', 0))
        self.size = None
        while True:
            rmagic, opt, rtype, length = struct.unpack('>QIII', _recvn(self.c, 20))
            data = _recvn(self.c, length) if length else b''
            if rtype == REP_INFO and len(data) >= 12 and struct.unpack('>H', data[:2])[0] == 0:
                self.size = struct.unpack('>Q', data[2:10])[0]
            elif rtype == REP_ACK:
                break
            elif rtype & 0x80000000:
                raise ConnectionError(f'NBD_OPT_GO refused: {rtype:#x} {data[:200]!r}')
        if self.size is None:
            raise ConnectionError('server sent no export size')
        self.handle = 0

    def read(self, off, length):
        out = bytearray()
        while length > 0:
            n = min(length, 32 * 1024 * 1024)
            self.handle += 1
            self.c.sendall(struct.pack('>IHHQQI', REQ_MAGIC, 0, CMD_READ, self.handle, off, n))
            magic, err, handle = struct.unpack('>IIQ', _recvn(self.c, 16))
            if magic != SIMPLE_REPLY or handle != self.handle:
                raise ConnectionError('bad reply')
            if err:
                raise OSError(err, f'NBD read error {err} at {off}')
            out += _recvn(self.c, n)
            off += n
            length -= n
        return bytes(out)

    def close(self):
        try:
            self.c.sendall(struct.pack('>IHHQQI', REQ_MAGIC, 0, CMD_DISC, 0, 0, 0))
        except OSError:
            pass
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
