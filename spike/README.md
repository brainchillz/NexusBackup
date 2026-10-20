# Phase 0 spike

Throwaway code that proved the VMware transport decisions recorded in
`../PLAN.md` (section "Phase 0 findings"). Nothing here is product code.

| File | Runs on | What it does |
|---|---|---|
| `cbt_spike.py` | anywhere | shared helpers (connect, CBT query, datastore URLs) plus the original HTTPS range-read attempt, which only works for powered-off VMs |
| `probe_http.py`, `probe2.py`, `probe3.py` | anywhere | the experiments that ruled transports in or out |
| `hotadd_spike.py` | inside the proxy VM, as root | snapshot a VM, hot-add its base disk to the proxy, stream CBT extents to the receiver |
| `recv.py` | backup server | accepts the extent stream into a raw sparse image |
| `restore_spike.py` | backup server | raw image back to a new VM: thin disk, HTTPS PUT, CreateVM, power on |

Credentials come from `VC_HOST`, `VC_USER`, `VC_PASS` in the environment.
On Ubuntu 26.04 `sudo` (sudo-rs) drops the environment, so pass them on the
sudo command line.

```
# backup server
python3 recv.py 9400 /path/image.raw 4
# proxy VM
sudo VC_HOST=.. VC_USER=.. VC_PASS=.. venv/bin/python hotadd_spike.py \
    --vm <target> --self <proxy vm name> --to <server>:9400 --workers 4
```

Verifying an image on the server without touching the host's own LVM:
`losetup -fP --read-only` the raw file, mount plain partitions directly, and
for an LVM root compute the logical volume's byte offset from
`pvck --dump metadata` and `losetup -o <offset> --sizelimit <size>`.
