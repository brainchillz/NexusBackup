#!/usr/bin/env python3
"""Spike receiver: accept N connections, each streaming [offset u64][length u64][data] frames
into a raw sparse image. First frame of each connection is [0xFFFFFFFFFFFFFFFF][capacity]."""
import os, socket, struct, sys, time
HDR = struct.Struct('<QQ')
MAGIC = 0xFFFFFFFFFFFFFFFF
def recvn(c, n):
    b = bytearray()
    while len(b) < n:
        p = c.recv(min(n - len(b), 1 << 20))
        if not p: raise EOFError
        b += p
    return bytes(b)
def main():
    port, path, n = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', port)); s.listen(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    import threading
    total = [0]; lock = threading.Lock(); t0 = time.time()
    def handle(c):
        got = 0
        try:
            while True:
                off, ln = HDR.unpack(recvn(c, 16))
                if off == MAGIC:
                    if os.fstat(fd).st_size != ln: os.ftruncate(fd, ln)
                    continue
                os.pwrite(fd, recvn(c, ln), off); got += ln
        except EOFError:
            pass
        with lock: total[0] += got
        c.close()
    ths = []
    for _ in range(n):
        c, _ = s.accept(); t = threading.Thread(target=handle, args=(c,)); t.start(); ths.append(t)
    for t in ths: t.join()
    el = time.time() - t0
    print(f'received {total[0] / 2**20:.0f} MiB in {el:.1f}s = {total[0] / 2**20 / max(el, 1e-6):.0f} MiB/s -> {path}', flush=True)
main()
