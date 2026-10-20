"""Two open-API transports for running VMs:
 A. ExportSnapshot -> HttpNfcLease -> stream disk (full export, measure rate, test Range)
 B. hot-add the snapshot's base disk to a throwaway powered-on VM (HotAdd transport, no VDDK)
"""
import os, sys, time, requests, urllib3
from pyVmomi import vim
from cbt_spike import connect, find_vm, wait_task, log, MiB
urllib3.disable_warnings()

def lease_ready(lease, what):
    for _ in range(120):
        if lease.state == vim.HttpNfcLease.State.ready:
            return
        if lease.state == vim.HttpNfcLease.State.error:
            raise RuntimeError(f'{what}: {lease.error}')
        time.sleep(0.5)
    raise RuntimeError(f'{what}: lease not ready')

def probe_export(vm, snap):
    lease = snap.ExportSnapshot()
    lease_ready(lease, 'ExportSnapshot')
    try:
        for du in lease.info.deviceUrl:
            url = du.url.replace('*', os.environ['VC_HOST'])
            log(f'A: device {du.key} disk={du.disk} url={url} size={du.fileSize}')
            r = requests.get(url, headers={'Range': 'bytes=1048576-2097151'}, verify=False, stream=True, timeout=60)
            log(f'A: Range request -> {r.status_code} content-range={r.headers.get("Content-Range")} len={r.headers.get("Content-Length")}')
            r.close()
            r = requests.get(url, verify=False, stream=True, timeout=120)
            log(f'A: full stream -> {r.status_code} len={r.headers.get("Content-Length")} type={r.headers.get("Content-Type")}')
            t0 = time.time(); n = 0
            for buf in r.iter_content(4 * MiB):
                n += len(buf)
                if n >= 512 * MiB:
                    break
            el = time.time() - t0
            log(f'A: read {n / MiB:.0f} MiB of export stream in {el:.1f}s = {n / MiB / el:.0f} MiB/s (streamOptimized, compressed)')
            r.close()
            break
    finally:
        lease.HttpNfcLeaseAbort()
        log('A: lease aborted')

def probe_hotadd(si, vm, snap):
    dc = vm
    while not isinstance(dc, vim.Datacenter):
        dc = dc.parent
    ds = vm.datastore[0]
    name = 'vmbackup-hotadd-probe'
    for v in dc.vmFolder.childEntity:
        if getattr(v, 'name', None) == name:
            raise RuntimeError('probe VM already exists, clean up first')
    ctl = vim.vm.device.ParaVirtualSCSIController(key=-100, busNumber=0, sharedBus='noSharing')
    spec = vim.vm.ConfigSpec(name=name, memoryMB=256, numCPUs=1, guestId='otherLinux64Guest',
                             files=vim.vm.FileInfo(vmPathName=f'[{ds.name}]'),
                             deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=ctl)])
    probe = wait_task(dc.vmFolder.CreateVM_Task(config=spec, pool=vm.resourcePool, host=vm.runtime.host), 'create probe')
    log(f'B: created {name} on {vm.runtime.host.name}')
    added = None
    try:
        wait_task(probe.PowerOnVM_Task(), 'power on probe')
        log('B: probe powered on (diskless)')
        src = [d for d in snap.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)][0]
        base = src.backing.parent.fileName if src.backing.parent else src.backing.fileName
        ctlkey = [d for d in probe.config.hardware.device if isinstance(d, vim.vm.device.ParaVirtualSCSIController)][0].key
        for mode in ('independent_nonpersistent', 'nonpersistent'):
            disk = vim.vm.device.VirtualDisk(key=-1, controllerKey=ctlkey, unitNumber=0,
                                             backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=base, diskMode=mode))
            try:
                t0 = time.time()
                wait_task(probe.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=disk)])), 'hot-add')
                added = [d for d in probe.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)][0]
                log(f'B: HOT-ADD OK mode={mode} in {time.time() - t0:.1f}s: {added.backing.fileName} {added.capacityInBytes / 1024**3:.0f} GiB, probe sees it as controller {ctlkey} unit 0')
                break
            except Exception as e:
                log(f'B: hot-add mode={mode} failed: {str(e)[:300]}')
    finally:
        if added is not None:
            # remove WITHOUT fileOperation: never destroy the source VM's disk
            wait_task(probe.ReconfigVM_Task(vim.vm.ConfigSpec(deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='remove', device=added)])), 'hot-remove')
            log('B: hot-removed')
        still = [d for d in probe.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]
        if still:
            log(f'B: probe still has disks {[d.backing.fileName for d in still]}; NOT destroying, clean up by hand')
        else:
            if probe.runtime.powerState == 'poweredOn':
                wait_task(probe.PowerOffVM_Task(), 'power off probe')
            wait_task(probe.Destroy_Task(), 'destroy probe')
            log('B: probe VM destroyed')

def main():
    si = connect()
    try:
        vm = find_vm(si, sys.argv[1])
        snap = wait_task(vm.CreateSnapshot_Task(name='probe3', description='', memory=False, quiesce=False), 'snapshot')
        log('snapshot created')
        try:
            try:
                probe_export(vm, snap)
            except Exception as e:
                log(f'A failed: {str(e)[:400]}')
            try:
                probe_hotadd(si, vm, snap)
            except Exception as e:
                log(f'B failed: {str(e)[:400]}')
        finally:
            wait_task(snap.RemoveSnapshot_Task(removeChildren=False, consolidate=True), 'remove snapshot')
            log(f'snapshot removed; source VM disks: {[d.backing.fileName for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)]}')
    finally:
        from pyVim.connect import Disconnect; Disconnect(si)

main()
