#!/usr/bin/env python3
"""Probe datastore HTTPS access modes: ESXi host + ticket vs vCenter proxy + session,
full GET vs Range, locked (running VM) vs unlocked (powered off) vs snapshot parent."""
import os, sys, time, urllib.parse
import requests, urllib3
from pyVmomi import vim
from cbt_spike import connect, find_vm, HostReader, ds_url, ds_split, flat_extent, wait_task
urllib3.disable_warnings()

def vc_url(vc, path, dc):
    ds, rel = ds_split(path)
    rel = '/'.join(urllib.parse.quote(p) for p in rel.split('/'))
    return f'https://{vc}/folder/{rel}?dcPath={urllib.parse.quote(dc)}&dsName={urllib.parse.quote(ds)}'

def try_host(reader, path, rng):
    url = ds_url(reader.host, path)
    h = {'Range': f'bytes={rng[0]}-{rng[1]}'} if rng else {}
    r = requests.get(url, headers=h, cookies={'vmware_cgi_ticket': reader._ticket(url)}, verify=False, stream=True, timeout=60)
    n = len(next(r.iter_content(1 << 20), b''))
    return f'{r.status_code} len={r.headers.get("Content-Length")} first={n} {r.headers.get("Content-Range","")} {r.text[:120] if r.status_code>=400 else ""}'

def try_vc(si, path, dc, rng):
    url = vc_url(os.environ['VC_HOST'], path, dc)
    h = {'Range': f'bytes={rng[0]}-{rng[1]}'} if rng else {}
    cookie = si._stub.cookie.split(';')[0].split('=',1)[1].strip('"')
    r = requests.get(url, headers=h, cookies={'vmware_soap_session': cookie}, verify=False, stream=True, timeout=60)
    n = len(next(r.iter_content(1 << 20), b''))
    return f'{r.status_code} len={r.headers.get("Content-Length")} first={n} {r.headers.get("Content-Range","")} {r.text[:120] if r.status_code>=400 else ""}'

def disk_paths(vm):
    for d in vm.config.hardware.device:
        if isinstance(d, vim.vm.device.VirtualDisk):
            return d.backing.fileName
    raise SystemExit('no disk')

def main():
    si = connect()
    dc = 'Datacenter'
    RNG = (1 << 30, (1 << 30) + (1 << 20) - 1)  # 1 MiB at 1 GiB
    try:
        for name in sys.argv[1:]:
            vm = find_vm(si, name)
            host = vm.runtime.host.name
            reader = HostReader(si, host)
            desc = disk_paths(vm)
            flat = flat_extent(reader, desc)
            print(f'== {name} {vm.runtime.powerState} host={host}')
            print(f'   flat   : {flat}')
            for label, fn in (('host  full ', lambda: try_host(reader, flat, None)),
                              ('host  range', lambda: try_host(reader, flat, RNG)),
                              ('vc    full ', lambda: try_vc(si, flat, dc, None)),
                              ('vc    range', lambda: try_vc(si, flat, dc, RNG))):
                try:
                    print(f'   {label}: {fn()}')
                except Exception as e:
                    print(f'   {label}: EXC {e}')
            if vm.runtime.powerState == 'poweredOn' and not vm.snapshot:
                print('   -- with snapshot --')
                snap = wait_task(vm.CreateSnapshot_Task(name='probe', description='', memory=False, quiesce=False), 'snap')
                try:
                    for label, fn in (('host  range', lambda: try_host(reader, flat, RNG)),
                                      ('vc    range', lambda: try_vc(si, flat, dc, RNG))):
                        try:
                            print(f'   {label}: {fn()}')
                        except Exception as e:
                            print(f'   {label}: EXC {e}')
                finally:
                    wait_task(snap.RemoveSnapshot_Task(removeChildren=False, consolidate=True), 'rm')
                    print('   snapshot removed')
    finally:
        from pyVim.connect import Disconnect; Disconnect(si)

main()
