"""Provider-neutral pieces: the VM spec every platform maps to, chunk arithmetic,
and the work-record format shared with the proxy agent."""
import struct

RECORD = struct.Struct('<Q32s')  # (chunk index, hash) as the agent reports them


def extents_to_chunk_ranges(extents, chunk_size, capacity):
    """[(start, length)] in bytes -> sorted, merged [[first_chunk, count]] covering them."""
    last = (capacity + chunk_size - 1) // chunk_size - 1
    idx = []
    for start, length in extents:
        if length <= 0:
            continue
        a = start // chunk_size
        b = min((start + length - 1) // chunk_size, last)
        idx.append([a, b])
    idx.sort()
    out = []
    for a, b in idx:
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[a, b - a + 1] for a, b in out]


def ranges_chunk_count(ranges):
    return sum(n for _, n in ranges)


def ranges_bytes(ranges, chunk_size):
    return ranges_chunk_count(ranges) * chunk_size


def pack_records(pairs):
    return b''.join(RECORD.pack(i, h) for i, h in pairs)


def unpack_records(data):
    return [RECORD.unpack_from(data, o) for o in range(0, len(data) - len(data) % RECORD.size, RECORD.size)]


def empty_spec(platform):
    return {'platform': platform, 'name': '', 'cpus': 1, 'cores_per_socket': 1, 'memory_mb': 1024,
            'firmware': 'bios', 'secure_boot': False, 'tpm': False, 'guest_id': '', 'guest_name': '',
            'uuid': '', 'disks': [], 'nics': [], 'placement': {}, 'platform_id': '', 'tools': ''}
