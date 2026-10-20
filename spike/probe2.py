import os, sys, requests, urllib3
from pyVmomi import vim
from cbt_spike import connect, find_vm, HostReader, ds_url, flat_extent, wait_task
urllib3.disable_warnings()
si = connect()
vm = find_vm(si, sys.argv[1])
reader = HostReader(si, vm.runtime.host.name)
desc = [d for d in vm.config.hardware.device if isinstance(d, vim.vm.device.VirtualDisk)][0].backing.fileName
flat = flat_extent(reader, desc)
url = ds_url(reader.host, flat)
r = requests.get(url, headers={'Range': 'bytes=0-1048575'}, cookies={'vmware_cgi_ticket': reader._ticket(url)}, verify=False, timeout=60)
print('host range on running base disk:', r.status_code, repr(r.text[:300]), dict(r.headers))
print('ExportSnapshot available on vim.vm.Snapshot:', hasattr(vim.vm.Snapshot, 'ExportSnapshot'))
print('HttpNfcLease ok:', hasattr(vim, 'HttpNfcLease'))
# does the VM's host see any other hosts (for hot-add proxy placement)?
print('hosts in cluster:', [h.name for h in vm.runtime.host.parent.host])
print('datastores:', [(d.name, d.summary.type) for d in vm.datastore])
from pyVim.connect import Disconnect; Disconnect(si)
