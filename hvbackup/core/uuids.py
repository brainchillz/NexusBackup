"""VMware reports a VM's BIOS UUID to the guest with the first three fields byte-swapped
relative to vm.config.uuid (SMBIOS 2.6 little-endian encoding). Normalise both to one form."""
import uuid


def normalise(u):
    try:
        x = uuid.UUID(str(u).strip())
    except ValueError:
        return None
    return str(x)


def swapped(u):
    x = uuid.UUID(str(u).strip())
    return str(uuid.UUID(bytes_le=x.bytes))


def same_vm(a, b):
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return False
    return a == b or swapped(a) == b
