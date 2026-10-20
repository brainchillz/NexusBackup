#!/usr/bin/env python3
"""Phase 0 restore: raw image -> new VM on vSphere, open API only.
  1. create a thin VMDK of the right size with VirtualDiskManager (proper VMFS descriptor)
  2. HTTP PUT the raw image over its -flat.vmdk via the ESXi datastore endpoint (ticket auth)
  3. CreateVM with that disk, firmware/guestId/NIC copied from the source VM, power on
  4. wait for VMware Tools to report an IP
"""
import argparse, os, time, requests, urllib3
from pyVmomi import vim
from cbt_spike import connect, find_vm, wait_task, log, MiB, ds_url, HostReader
from pyVim.connect import Disconnect
urllib3.disable_warnings()

class Progress:
    """File-like with a known length so requests sends Content-Length, not chunked encoding."""
    def __init__(self, f, total):
        self.f, self.total, self.sent, self.t0, self.last = f, total, 0, time.time(), 0
    def __len__(self):
        return self.total
    def read(self, n=-1):
        b = self.f.read(n)
        self.sent += len(b)
        if self.sent - self.last >= 2048 * MiB:
            self.last = self.sent; el = time.time() - self.t0
            log(f'  uploaded {self.sent / MiB:.0f}/{self.total / MiB:.0f} MiB  {self.sent / MiB / el:.0f} MiB/s')
        return b

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True); ap.add_argument('--raw', required=True); ap.add_argument('--name', required=True)
    a = ap.parse_args()
    si = connect()
    try:
        src = find_vm(si, a.src)
        dc = src
        while not isinstance(dc, vim.Datacenter): dc = dc.parent
        ds, host, pool = src.datastore[0], src.runtime.host, src.resourcePool
        size = os.path.getsize(a.raw)
        folder = f'[{ds.name}] {a.name}'
        desc = f'{folder}/{a.name}.vmdk'
        content = si.RetrieveContent()
        try:
            wait_task(content.fileManager.DeleteDatastoreFile_Task(name=folder, datacenter=dc), 'clean old folder')
            log(f'removed leftover {folder}')
        except Exception:
            pass
        content.fileManager.MakeDirectory(name=folder, datacenter=dc, createParentDirectories=True)
        spec = vim.VirtualDiskManager.FileBackedVirtualDiskSpec(diskType='thin', adapterType='lsiLogic', capacityKb=size // 1024)
        wait_task(content.virtualDiskManager.CreateVirtualDisk_Task(name=desc, datacenter=dc, spec=spec), 'create disk')
        log(f'created thin disk {desc} ({size / 1024**3:.0f} GiB)')
        reader = HostReader(si, host.name)
        flat = f'{folder}/{a.name}-flat.vmdk'
        url = ds_url(host.name, flat)
        t0 = time.time()
        with open(a.raw, 'rb') as f:
            spec = vim.SessionManager.HttpServiceRequestSpec(method='httpPut', url=url)
            ticket = si.content.sessionManager.AcquireGenericServiceTicket(spec).id
            r = requests.put(url, data=Progress(f, size), headers={'Content-Type': 'application/octet-stream'},
                             cookies={'vmware_cgi_ticket': ticket}, verify=False, timeout=3600)
        el = time.time() - t0
        log(f'PUT flat -> {r.status_code} in {el:.0f}s = {size / MiB / el:.0f} MiB/s')
        if r.status_code not in (200, 201): raise RuntimeError(r.text[:300])
        ctl = vim.vm.device.ParaVirtualSCSIController(key=-100, busNumber=0, sharedBus='noSharing')
        disk = vim.vm.device.VirtualDisk(key=-1, controllerKey=-100, unitNumber=0,
                 backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=desc, diskMode='persistent'))
        src_nic = [d for d in src.config.hardware.device if isinstance(d, vim.vm.device.VirtualEthernetCard)][0]
        nic = vim.vm.device.VirtualVmxnet3(key=-2, backing=src_nic.backing, addressType='generated',
                 connectable=vim.vm.device.VirtualDevice.ConnectInfo(startConnected=True, allowGuestControl=True))
        extra = [vim.option.OptionValue(key=o.key, value=o.value) for o in src.config.extraConfig if o.key.startswith('guestinfo.')]
        cfg = vim.vm.ConfigSpec(name=a.name, memoryMB=1024, numCPUs=1, guestId=src.config.guestId, firmware=src.config.firmware,
                 files=vim.vm.FileInfo(vmPathName=folder), extraConfig=extra,
                 bootOptions=vim.vm.BootOptions(efiSecureBootEnabled=src.config.bootOptions.efiSecureBootEnabled),
                 deviceChange=[vim.vm.device.VirtualDeviceSpec(operation='add', device=d) for d in (ctl, disk, nic)])
        vm = wait_task(dc.vmFolder.CreateVM_Task(config=cfg, pool=pool, host=host), 'create vm')
        log(f'created VM {vm.name} firmware={src.config.firmware} guest={src.config.guestId}')
        wait_task(vm.PowerOnVM_Task(), 'power on'); t1 = time.time()
        ip = None
        for _ in range(180):
            ip = vm.guest.ipAddress
            if ip and not ip.startswith('169.'): break
            time.sleep(2)
        log(f'guest IP {ip} after {time.time() - t1:.0f}s; tools={vm.guest.toolsRunningStatus}')
        print(f'RESTORED_IP={ip}')
    finally:
        Disconnect(si)
main()
