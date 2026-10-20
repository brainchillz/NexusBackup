#!/usr/bin/env python3
"""Phase 0 spike, HotAdd transport without VDDK. Runs INSIDE a proxy VM on the vSphere cluster.

  1. snapshot the target VM (quiesced if Tools runs); CBT enabled if needed
  2. hot-add the snapshot's frozen base disk to this proxy VM (independent_nonpersistent)
  3. rescan SCSI, find the new block device
  4. QueryChangedDiskAreas since last changeId ('*' first time), pread those extents,
     stream them to the receiver on the backup server over N TCP connections
  5. hot-remove the disk (never with a file operation), remove the snapshot, save changeId

Env: VC_HOST VC_USER VC_PASS.  Must run as root (SCSI rescan, raw device reads).
"""
import argparse, glob, json, os, socket, struct, time
from concurrent.futures import ThreadPoolExecutor
from pyVmomi import vim
from cbt_spike import connect, find_vm, wait_task, log, MiB, changed_areas, split_extents
from pyVim.connect import Disconnect

HDR = struct.Struct('<QQ'); MAGIC = 0xFFFFFFFFFFFFFFFF

def block_devs():
    return {os.path.basename(p): int(open(p + '/size').read()) * 512 for p in glob.glob('/sys/block/sd*')}

def rescan():
    for h in glob.glob('/sys/class/scsi_host/host*/scan'):
        open(h, 'w').write('- - -')

def scsi_ctl_and_unit(proxy):
    ctl = [d for d in proxy.config.hardware.device if isinstance(d, vim.vm.device.VirtualSCSIController)][0]
    used = {d.unitNumber for d in proxy.config.hardware.device if getattr(d, 'controllerKey', None) == ctl.key}
    unit = next(u for u in range(0, 16) if u != 7 and u not in used)
    return ctl, unit

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--vm', required=True); ap.add_argument('--self', required=True)
    ap.add_argument('--to', required=True, help='receiver host:port')
    ap.add_argument('--workers', type=int, default=4); ap.add_argument('--chunk-mb', type=int, default=16)
    ap.add_argument('--state', default='state.json')
    a = ap.parse_args()
    rhost, rport = a.to.split(':'); rport = int(rport)
    state = json.load(open(a.state)) if os.path.exists(a.state) else {}
    si = connect()
    try:
        vm = find_vm(si, a.vm); proxy = find_vm(si, a.self)
        log(f'target {vm.name} on {vm.runtime.host.name}; proxy {proxy.name} on {proxy.runtime.host.name}')
        if not vm.config.changeTrackingEnabled:
            wait_task(vm.ReconfigVM_Task(vim.vm.ConfigSpec(changeTrackingEnabled=True)), 'enable CBT'); log('CBT enabled')
        q = vm.guest.toolsRunningStatus == 'guestToolsRunning'
        t0 = time.time()
        snap = wait_task(vm.CreateSnapshot_Task(name='hotadd-spike', description='', memory=False, quiesce=q), 'snapshot')
        log(f'snapshot (quiesce={q}) in {time.time() - t0:.1f}s')
        try:
            for src in [d for d in snap.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]:
                change_id = src.backing.changeId
                if not change_id: raise RuntimeError('no changeId on snapshot; CBT not active yet')
                base = src.backing.parent.fileName if src.backing.parent else src.backing.fileName
                key = str(src.key); since = state.get(vm.name, {}).get(key, {}).get('changeId', '*')
                areas = list(changed_areas(vm, snap, src, since)); to_read = sum(l for _, l in areas)
                log(f'disk {key}: {src.capacityInBytes / 1024**3:.0f} GiB, since={since[:12]}.. -> {len(areas)} areas, {to_read / MiB:.0f} MiB')
                before = block_devs()
                ctl, unit = scsi_ctl_and_unit(proxy)
                disk = vim.vm.device.VirtualDisk(key=-1, controllerKey=ctl.key, unitNumber=unit,
                        backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=base, diskMode='independent_nonpersistent'))
                t1 = time.time()
                wait_task(proxy.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=disk)])), 'hot-add')
                added = [d for d in proxy.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk) and d.backing.fileName == base][0]
                try:
                    dev = None
                    for _ in range(30):
                        rescan(); time.sleep(1)
                        new = {k: v for k, v in block_devs().items() if k not in before and v == src.capacityInBytes}
                        if new: dev = '/dev/' + sorted(new)[0]; break
                    if not dev: raise RuntimeError(f'hot-added disk not visible; devs now {block_devs()}')
                    log(f'hot-added as {dev} in {time.time() - t1:.1f}s (unit {unit})')
                    fd = os.open(dev, os.O_RDONLY)
                    exts = list(split_extents(areas, a.chunk_mb * MiB))
                    def worker(i):
                        s = socket.create_connection((rhost, rport)); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        s.sendall(HDR.pack(MAGIC, src.capacityInBytes)); n = 0
                        for off, ln in exts[i::a.workers]:
                            buf = os.pread(fd, ln, off)
                            if len(buf) != ln: raise RuntimeError(f'short pread at {off}')
                            s.sendall(HDR.pack(off, ln)); s.sendall(buf); n += ln
                        s.close(); return n
                    t2 = time.time()
                    with ThreadPoolExecutor(a.workers) as ex: sent = sum(ex.map(worker, range(a.workers)))
                    el = time.time() - t2; os.close(fd)
                    log(f'disk {key}: sent {sent / MiB:.0f} MiB in {el:.1f}s = {sent / MiB / max(el, 1e-6):.0f} MiB/s')
                    state.setdefault(vm.name, {})[key] = {'changeId': change_id, 'capacity': src.capacityInBytes, 'base': base}
                finally:
                    wait_task(proxy.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='remove', device=added)])), 'hot-remove')
                    rescan(); log('hot-removed')
        finally:
            t3 = time.time()
            wait_task(snap.RemoveSnapshot_Task(removeChildren=False, consolidate=True), 'remove snapshot')
            log(f'snapshot removed in {time.time() - t3:.1f}s; consolidationNeeded={vm.runtime.consolidationNeeded}')
        json.dump(state, open(a.state, 'w'), indent=2)
        log(f'total elapsed {time.time() - t0:.1f}s')
    finally:
        Disconnect(si)
main()
