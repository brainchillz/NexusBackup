#!/usr/bin/env python3
"""Phase 0 spike: back up a running vSphere VM with CBT and datastore HTTPS range reads.

No VDDK, no SDK downloads. Only the vSphere API (pyVmomi) and the datastore
HTTPS endpoint every ESXi host serves.

Flow per run:
  1. connect to vCenter, find the VM
  2. enable Changed Block Tracking if it is off
  3. create a snapshot (quiesced when VMware Tools is running, no memory)
  4. for each disk: QueryChangedDiskAreas since the previous run's changeId
     ('*' on the first run = every allocated area)
  5. read those extents from the frozen base disk over HTTPS Range requests
     against the ESXi host, write them into a raw image at the same offsets
  6. record the new changeId, remove the snapshot, wait for consolidation

Second run with the same --out transfers only blocks changed since the first.

Env: VC_HOST, VC_USER, VC_PASS.
"""
import argparse
import json
import os
import ssl
import sys
import time
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MiB = 1024 * 1024


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def wait_task(task, what):
    while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
        time.sleep(0.5)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f'{what} failed: {task.info.error}')
    return task.info.result


def connect():
    host, user, pw = os.environ['VC_HOST'], os.environ['VC_USER'], os.environ['VC_PASS']
    try:
        si = SmartConnect(host=host, user=user, pwd=pw, disableSslCertValidation=True)
    except TypeError:  # older pyvmomi
        si = SmartConnect(host=host, user=user, pwd=pw, sslContext=ssl._create_unverified_context())
    return si


def all_vms(si):
    content = si.RetrieveContent()
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    try:
        return list(view.view)
    finally:
        view.Destroy()


def find_vm(si, name):
    for vm in all_vms(si):
        if vm.name == name:
            return vm
    raise SystemExit(f'VM {name!r} not found')


def list_vms(si):
    rows = []
    for vm in all_vms(si):
        if vm.config is None or vm.config.template:
            continue
        disks = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        size = sum(d.capacityInBytes for d in disks) / 1024**3
        snaps = 'snap' if vm.snapshot else ''
        rows.append((vm.name, vm.runtime.powerState, vm.guest.toolsRunningStatus,
                     'cbt' if vm.config.changeTrackingEnabled else '',
                     f'{len(disks)}x {size:.0f}G', snaps, vm.runtime.host.name if vm.runtime.host else ''))
    for r in sorted(rows):
        print('  '.join(str(c).ljust(w) for c, w in zip(r, (34, 12, 24, 4, 10, 5, 20))))


# ─── datastore HTTPS access ────────────────────────────────────────────────

def ds_split(path):
    """'[ds] dir/file' -> ('ds', 'dir/file')"""
    ds, rel = path.split('] ', 1)
    return ds.lstrip('['), rel


def ds_url(host, path):
    ds, rel = ds_split(path)
    rel = '/'.join(urllib.parse.quote(p) for p in rel.split('/'))
    return f'https://{host}/folder/{rel}?dcPath=ha-datacenter&dsName={urllib.parse.quote(ds)}'


class HostReader:
    """Range reads from an ESXi host's datastore endpoint, authenticated with
    generic service tickets minted by vCenter — no host credentials needed."""

    def __init__(self, si, host):
        self.si = si
        self.host = host
        self.local = threading.local()
        self.lock = threading.Lock()

    def _session(self):
        s = getattr(self.local, 's', None)
        if s is None:
            s = requests.Session()
            s.verify = False
            self.local.s = s
        return s

    def _ticket(self, url):
        spec = vim.SessionManager.HttpServiceRequestSpec(method='httpGet', url=url)
        with self.lock:  # pyVmomi stubs are not thread safe
            return self.si.content.sessionManager.AcquireGenericServiceTicket(spec).id

    def get(self, path, start=None, length=None):
        url = ds_url(self.host, path)
        headers = {}
        if start is not None:
            headers['Range'] = f'bytes={start}-{start + length - 1}'
        r = self._session().get(url, headers=headers, cookies={'vmware_cgi_ticket': self._ticket(url)},
                                stream=True, timeout=120)
        if start is not None and r.status_code != 206:
            raise RuntimeError(f'expected 206 for range read, got {r.status_code}: {r.text[:200]}')
        r.raise_for_status()
        return r


def flat_extent(reader, descriptor_path):
    """Parse a VMDK descriptor and return the datastore path of its single flat extent."""
    text = reader.get(descriptor_path).text
    ds, rel = ds_split(descriptor_path)
    d = rel.rsplit('/', 1)[0] if '/' in rel else ''
    extents = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in ('RW', 'RDONLY', 'NOACCESS'):
            extents.append(parts[3].strip('"'))
    if len(extents) != 1:
        raise RuntimeError(f'{descriptor_path}: expected one extent, got {extents}')
    return f'[{ds}] {d + "/" if d else ""}{extents[0]}'


# ─── CBT ──────────────────────────────────────────────────────────────────

def changed_areas(vm, snap, disk, change_id):
    """Yield (start, length) for every area changed since change_id ('*' = allocated)."""
    offset = 0
    cap = disk.capacityInBytes
    while offset < cap:
        info = vm.QueryChangedDiskAreas(snapshot=snap, deviceKey=disk.key, startOffset=offset, changeId=change_id)
        for a in info.changedArea:
            yield a.start, a.length
        if info.length == 0:
            break
        offset = info.startOffset + info.length


def split_extents(areas, max_len):
    for start, length in areas:
        while length > 0:
            n = min(length, max_len)
            yield start, n
            start += n
            length -= n


# ─── main ─────────────────────────────────────────────────────────────────

def backup(si, vm, out_dir, state, workers, chunk_mb, quiesce):
    os.makedirs(out_dir, exist_ok=True)
    vm_state = state.setdefault(vm.name, {})

    if not vm.config.changeTrackingEnabled:
        log('enabling CBT')
        wait_task(vm.ReconfigVM_Task(vim.vm.ConfigSpec(changeTrackingEnabled=True)), 'enable CBT')

    tools_ok = vm.guest.toolsRunningStatus == 'guestToolsRunning'
    q = quiesce and tools_ok
    log(f'creating snapshot (quiesce={q}, tools={vm.guest.toolsRunningStatus})')
    t0 = time.time()
    snap = wait_task(vm.CreateSnapshot_Task(name='cbt-spike', description='phase 0 spike', memory=False, quiesce=q),
                     'snapshot')
    log(f'snapshot ready in {time.time() - t0:.1f}s')

    host = vm.runtime.host.name
    reader = HostReader(si, host)
    total = 0
    t_read = time.time()
    try:
        disks = [d for d in snap.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        for disk in disks:
            b = disk.backing
            new_change_id = getattr(b, 'changeId', None)
            if not new_change_id:
                raise RuntimeError('snapshot has no changeId: CBT not active yet, remove snapshot and retry')
            # the snapshot's backing is the delta; its parent is the frozen base descriptor
            base = b.parent.fileName if b.parent else b.fileName
            flat = flat_extent(reader, base)
            key = str(disk.key)
            prev = vm_state.get(key, {})
            since = prev.get('changeId', '*')
            raw = os.path.join(out_dir, f'{vm.name}-disk{disk.key}.raw')
            log(f'disk {disk.key}: {disk.capacityInBytes / 1024**3:.1f} GiB, base {flat}, since {since}')

            with open(raw, 'ab'):
                pass
            os.truncate(raw, disk.capacityInBytes)
            fd = os.open(raw, os.O_WRONLY)

            areas = list(changed_areas(vm, snap, disk, since))
            to_read = sum(l for _, l in areas)
            log(f'disk {disk.key}: {len(areas)} changed areas, {to_read / MiB:.0f} MiB to transfer')

            def fetch(ext):
                start, length = ext
                r = reader.get(flat, start, length)
                pos = start
                for buf in r.iter_content(4 * MiB):
                    os.pwrite(fd, buf, pos)
                    pos += len(buf)
                if pos - start != length:
                    raise RuntimeError(f'short read at {start}: {pos - start} of {length}')
                return length

            done = 0
            t1 = time.time()
            with ThreadPoolExecutor(workers) as ex:
                futs = [ex.submit(fetch, e) for e in split_extents(areas, chunk_mb * MiB)]
                for f in as_completed(futs):
                    done += f.result()
                    if done and (done // (512 * MiB)) != ((done - chunk_mb * MiB) // (512 * MiB)):
                        el = time.time() - t1
                        log(f'  {done / MiB:.0f}/{to_read / MiB:.0f} MiB  {done / MiB / max(el, 1e-6):.0f} MiB/s')
            os.close(fd)
            el = time.time() - t1
            log(f'disk {disk.key}: {done / MiB:.0f} MiB in {el:.1f}s = {done / MiB / max(el, 1e-6):.0f} MiB/s')
            total += done
            vm_state[key] = {'changeId': new_change_id, 'capacity': disk.capacityInBytes, 'file': raw, 'base': flat}
    finally:
        log('removing snapshot')
        t2 = time.time()
        wait_task(snap.RemoveSnapshot_Task(removeChildren=False, consolidate=True), 'remove snapshot')
        for _ in range(60):
            if not vm.runtime.consolidationNeeded:
                break
            time.sleep(1)
        log(f'snapshot removed in {time.time() - t2:.1f}s, consolidationNeeded={vm.runtime.consolidationNeeded}')
    el = time.time() - t_read
    log(f'TOTAL {total / MiB:.0f} MiB transferred in {el:.1f}s')
    return total


def check_image(path):
    with open(path, 'rb') as f:
        s0 = f.read(512)
        s1 = f.read(512)
    mbr = s0[510:512] == b'\x55\xaa'
    gpt = s1[:8] == b'EFI PART'
    return f'MBR signature={mbr} GPT header={gpt}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--vm')
    ap.add_argument('--out', default='./out')
    ap.add_argument('--state', default=None, help='default <out>/state.json')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--chunk-mb', type=int, default=32)
    ap.add_argument('--no-quiesce', action='store_true')
    a = ap.parse_args()

    si = connect()
    try:
        if a.list:
            list_vms(si)
            return
        if not a.vm:
            ap.error('--vm or --list')
        state_path = a.state or os.path.join(a.out, 'state.json')
        state = json.load(open(state_path)) if os.path.exists(state_path) else {}
        vm = find_vm(si, a.vm)
        log(f'VM {vm.name}: {vm.runtime.powerState}, host {vm.runtime.host.name}, snapshots={"yes" if vm.snapshot else "no"}')
        backup(si, vm, a.out, state, a.workers, a.chunk_mb, not a.no_quiesce)
        os.makedirs(a.out, exist_ok=True)
        with open(state_path, 'w') as f:
            json.dump(state, f, indent=2)
        for d in state[vm.name].values():
            log(f'{d["file"]}: {check_image(d["file"])}')
    finally:
        Disconnect(si)


if __name__ == '__main__':
    main()
