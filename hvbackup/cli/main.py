"""hvbackup command line.

Server side (runs on the backup server, needs the data directory):
  hvbackup server                     run the API/UI server
  hvbackup init-admin <user>          create or reset an admin account
  hvbackup agent                      run the proxy agent (same as hvbackup-agent)

Client side (any machine; HVB_URL + HVB_TOKEN, or ~/.config/hvbackup/client.conf):
  hvbackup status [--tsv]             one-line summary, also feeds dashboards
  hvbackup connections list|add|test|refresh|delete
  hvbackup vms list [--connection N]
  hvbackup backup <vm> [--wait]
  hvbackup jobs list|add|run|delete
  hvbackup runs list
  hvbackup restore-points list [--vm <vm>]
  hvbackup restore <rp> --name NAME [--connection C] [--host H] [--datastore DS] [--network SRC=DST]... [--power-on] [--keep-identity] [--wait]
  hvbackup connections placement <id|name>      hosts, datastores and networks to restore onto
  hvbackup tasks list|show|wait|cancel
  hvbackup proxies list|add <name>|deploy <name> --connection <id|name> [...]|image
  hvbackup users list|add|passwd ; hvbackup tokens list|add|delete
  hvbackup gc
Every command accepts --json for raw API output.
"""
import argparse
import getpass
import json
import os
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── client ───────────────────────────────────────────────────────────
class Client:
    def __init__(self):
        conf = {}
        p = os.path.expanduser('~/.config/hvbackup/client.conf')
        if os.path.exists(p):
            for line in open(p):
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    conf[k.strip()] = v.strip().strip('"')
        self.url = (os.environ.get('HVB_URL') or conf.get('HVB_URL') or 'https://127.0.0.1:8460').rstrip('/')
        self.token = os.environ.get('HVB_TOKEN') or conf.get('HVB_TOKEN') or ''
        self.verify = os.environ.get('HVB_CA') or conf.get('HVB_CA') or ((os.environ.get('HVB_INSECURE') or conf.get('HVB_INSECURE', '1')) != '1')
        self.s = requests.Session()
        self.s.headers['Authorization'] = 'Bearer ' + self.token
        self.s.verify = self.verify

    def call(self, method, path, **kw):
        retries = kw.pop('retries', 0)
        timeout = kw.pop('timeout', 120)
        for attempt in range(retries + 1):
            try:
                r = self.s.request(method, self.url + '/api/v1' + path, timeout=timeout, **kw)
            except requests.ConnectionError as e:
                if attempt < retries:
                    time.sleep(2)
                    continue
                sys.exit(f'cannot reach {self.url}: {e.__class__.__name__} (set HVB_URL / HVB_TOKEN in ~/.config/hvbackup/client.conf)')
            if r.status_code >= 500 and attempt < retries:
                time.sleep(2)
                continue
            break
        if r.status_code >= 400:
            try:
                msg = r.json().get('detail')
            except ValueError:
                msg = r.text
            sys.exit(f'error {r.status_code}: {msg}')
        return r.json() if r.content else None


def fmt_bytes(n):
    n = n or 0
    for u in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or u == 'TiB':
            return f'{n:.1f} {u}' if u != 'B' else f'{n} B'
        n /= 1024


def fmt_ts(t):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(t)) if t else '-'


def table(rows, cols):
    if not rows:
        print('(none)')
        return
    widths = [max(len(str(c)), *(len(str(r.get(c, '') if isinstance(r, dict) else r[i])) for r in rows)) for i, c in enumerate(cols)]
    print('  '.join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print('  '.join(str(r.get(c, '') if isinstance(r, dict) else r[i]).ljust(w) for i, (c, w) in enumerate(zip(cols, widths))))


def out(args, data, rows=None, cols=None):
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    elif rows is not None:
        table(rows, cols)
    else:
        print(json.dumps(data, indent=2, default=str))


def wait_task(c, tid, quiet=False):
    last = ''
    errors = 0
    while True:
        try:
            t = c.call('GET', f'/tasks/{tid}', retries=5)
        except SystemExit:
            errors += 1
            if errors > 10:
                raise
            time.sleep(3)
            continue
        line = f'{t["state"]} {t["progress"] * 100:5.1f}% {t.get("message") or ""}'
        if line != last and not quiet:
            print(line, file=sys.stderr)
            last = line
        if t['state'] in ('done', 'failed', 'cancelled'):
            if t['state'] != 'done':
                print(t.get('log', ''), file=sys.stderr)
                sys.exit(f'task {tid} {t["state"]}: {t.get("message")}')
            return t
        time.sleep(2)


def resolve_vm(c, ref):
    if str(ref).isdigit():
        return int(ref)
    vms = [v for v in c.call('GET', '/vms') if v['name'] == ref]
    if len(vms) != 1:
        sys.exit(f'vm {ref!r}: {"not found" if not vms else "ambiguous, use the id"}')
    return vms[0]['id']


# ── server-side commands ─────────────────────────────────────────────
def cmd_server(args):
    import logging

    import uvicorn

    from .. import config
    from ..api.app import create_app
    from ..core import tls
    logging.basicConfig(level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    crt, key = tls.ensure_cert()
    uvicorn.run(create_app(), host=config.BIND, port=config.PORT, ssl_certfile=crt, ssl_keyfile=key, log_level=config.LOG_LEVEL,
                proxy_headers=True, forwarded_allow_ips='*')


def cmd_init_admin(args):
    from ..core import auth
    pw = args.password or getpass.getpass(f'password for {args.username}: ')
    u = auth.get_user(args.username)
    if u:
        auth.set_password(args.username, pw)
        auth.set_role(args.username, 'admin')
        print(f'updated {args.username} (admin)')
    else:
        auth.create_user(args.username, pw, 'admin')
        print(f'created {args.username} (admin)')


def cmd_agent(args):
    from ..agent.proxy_agent import main as agent_main
    agent_main()


# ── client commands ──────────────────────────────────────────────────
def cmd_status(args):
    c = Client()
    s = c.call('GET', '/status')
    rp = s['repository']
    if args.tsv:
        print('\t'.join(['vms', 'protected', 'restore_points', 'jobs', 'running', 'failed_24h', 'proxies_online', 'repo_used', 'repo_free']))
        print('\t'.join(str(x) for x in [s['vms'], s['protected_vms'], s['restore_points'], s['jobs'], s['running_tasks'], s['failed_runs_24h'],
                                         s['proxies_online'], fmt_bytes(rp['stored_bytes']), fmt_bytes(rp['free_bytes'])]))
        return
    if args.json:
        print(json.dumps(s, indent=2)); return
    print(f"hvbackup {s['version']}  repository {rp['path']}  {rp['chunks']} chunks, {fmt_bytes(rp['stored_bytes'])} used, {fmt_bytes(rp['free_bytes'])} free")
    print(f"{s['vms']} VMs, {s['protected_vms']} protected, {s['restore_points']} restore points, {s['jobs']} jobs enabled, "
          f"{s['running_tasks']} running tasks, {s['failed_runs_24h']} failed runs in 24h, {s['proxies_online']} proxies online, last GC {fmt_ts(s['last_gc'])}")


def cmd_connections(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/connections')
        out(args, rows, [{**r, 'last_inventory': fmt_ts(r['last_inventory'])} for r in rows], ['id', 'name', 'type', 'host', 'vm_count', 'proxies_online', 'last_inventory', 'last_error'])
    elif args.action == 'add':
        pw = args.password or getpass.getpass('password: ')
        res = c.call('POST', '/connections', json={'type': args.type, 'name': args.name, 'host': args.host, 'username': args.username, 'password': pw, 'verify_ssl': args.verify_ssl})
        out(args, res)
        if not args.json:
            wait_task(c, res['task_id'])
    elif args.action == 'test':
        out(args, c.call('POST', f'/connections/{args.id}/test'))
    elif args.action == 'refresh':
        res = c.call('POST', f'/connections/{args.id}/refresh')
        out(args, res) if args.json else wait_task(c, res['task_id'])
    elif args.action == 'delete':
        out(args, c.call('DELETE', f'/connections/{args.id}'))
    elif args.action == 'placement':
        p = c.call('GET', f'/connections/{resolve_connection(c, args.id)}/placement')
        if args.json:
            out(args, p)
            return
        ds = p.get('datastores') or {}
        for h in p['hosts']:
            state = '' if h.get('connection_state', 'connected') == 'connected' and not h.get('maintenance') else f' [{h.get("connection_state")}{", maintenance" if h.get("maintenance") else ""}]'
            print(f'host {h["name"]}{" (" + h["cluster"] + ")" if h.get("cluster") else ""}{state}')
            for d in h['datastores']:
                i = ds.get(d) or {}
                print(f'  datastore {d}' + (f'  {fmt_bytes(i["free"])} free of {fmt_bytes(i["capacity"])}' if i else '') + ('' if i.get('accessible', True) else '  (inaccessible)'))
            for n in h['networks']:
                print(f'  network   {n}')
        for f in p['folders']:
            print(f'folder {f["name"]} ({f["moref"]})')


def resolve_connection(c, ref):
    if ref is None:
        return None
    if str(ref).isdigit():
        return int(ref)
    for r in c.call('GET', '/connections'):
        if r['name'] == ref:
            return r['id']
    sys.exit(f'no connection named {ref!r}')


def cmd_vms(args):
    c = Client()
    rows = c.call('GET', '/vms', params={'connection_id': args.connection} if args.connection else None)
    view = [{'id': v['id'], 'name': v['name'], 'connection': v['connection_name'], 'power': v['power_state'].replace('powered', ''),
             'size': fmt_bytes(v['size_bytes']), 'cbt': 'yes' if v['cbt_enabled'] else '', 'points': v['restore_points'],
             'last_backup': fmt_ts(v['last_restore_point']['created']) if v['last_restore_point'] else '-',
             'jobs': ','.join(j['name'] for j in v['jobs']), 'last_status': (v['last_run'] or {}).get('status', '')} for v in rows]
    out(args, rows, view, ['id', 'name', 'connection', 'power', 'size', 'cbt', 'points', 'last_backup', 'jobs', 'last_status'])


def cmd_backup(args):
    c = Client()
    res = c.call('POST', f'/vms/{resolve_vm(c, args.vm)}/backup', json={'quiesce': not args.no_quiesce})
    out(args, res)
    if args.wait and not args.json:
        t = wait_task(c, res['task_id'])
        print(json.dumps(t.get('result', {}).get('result'), indent=2, default=str))


def cmd_jobs(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/jobs')
        out(args, rows, [{**j, 'vms': ','.join(j['vm_names']), 'next_run': fmt_ts(j['next_run']), 'last_run': fmt_ts(j['last_run']),
                          'retention': json.dumps(j['retention'])} for j in rows], ['id', 'name', 'connection_name', 'vms', 'schedule', 'retention', 'enabled', 'next_run', 'last_run', 'last_status'])
    elif args.action == 'add':
        ids = [resolve_vm(c, v) for v in args.vms]
        conns = {v['id']: v['connection_id'] for v in c.call('GET', '/vms')}
        cid = args.connection or conns[ids[0]]
        out(args, c.call('POST', '/jobs', json={'name': args.name, 'connection_id': cid, 'vm_ids': ids, 'schedule': args.schedule,
                                                 'retention': {'keep_last': args.keep_last, 'keep_days': args.keep_days}, 'quiesce': not args.no_quiesce}))
    elif args.action == 'run':
        res = c.call('POST', f'/jobs/{args.id}/run')
        out(args, res)
        if args.wait and not args.json:
            wait_task(c, res['task_id'])
    elif args.action == 'delete':
        out(args, c.call('DELETE', f'/jobs/{args.id}'))


def cmd_runs(args):
    c = Client()
    rows = c.call('GET', '/runs', params={'limit': args.limit})
    out(args, rows, [{**r, 'started': fmt_ts(r['started']), 'read': fmt_bytes(r['bytes_read']),
                      'secs': f'{(r["finished"] or time.time()) - r["started"]:.0f}'} for r in rows],
        ['id', 'vm_name', 'job_name', 'started', 'secs', 'status', 'kind', 'transport', 'read', 'restore_point_id', 'error'])


def cmd_restore_points(args):
    c = Client()
    rows = c.call('GET', '/restore-points', params={'vm_id': resolve_vm(c, args.vm)} if args.vm else None)
    out(args, rows, [{**r, 'created': fmt_ts(r['created']), 'size': fmt_bytes(r['size_bytes']), 'allocated': fmt_bytes(r['allocated_bytes'])} for r in rows],
        ['id', 'vm_name', 'created', 'kind', 'size', 'allocated'])


def cmd_restore(args):
    c = Client()
    nets = {}
    for m in args.network or []:
        if '=' not in m:
            sys.exit(f'--network wants SOURCE=TARGET, got {m!r}')
        a, b = m.split('=', 1)
        nets[a] = b
    body = {'name': args.name, 'power_on': args.power_on, 'new_identity': not args.keep_identity, 'host': args.host, 'datastore': args.datastore,
            'connection_id': resolve_connection(c, args.connection), 'folder': args.folder, 'networks': nets or None}
    res = c.call('POST', f'/restore-points/{args.rp}/restore', json=body)
    out(args, res)
    if args.wait and not args.json:
        t = wait_task(c, res['task_id'])
        print(json.dumps(t.get('result', {}).get('result'), indent=2, default=str))


def cmd_tasks(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/tasks', params={'limit': args.limit})
        out(args, rows, [{**t, 'progress': f'{t["progress"] * 100:.0f}%', 'created': fmt_ts(t['created'])} for t in rows],
            ['id', 'kind', 'subject', 'state', 'progress', 'message', 'created', 'created_by'])
    elif args.action == 'show':
        t = c.call('GET', f'/tasks/{args.id}')
        out(args, t) if args.json else print(json.dumps({k: v for k, v in t.items() if k != 'log'}, indent=2, default=str) + '\n' + (t.get('log') or ''))
    elif args.action == 'wait':
        wait_task(c, args.id)
    elif args.action == 'cancel':
        out(args, c.call('POST', f'/tasks/{args.id}/cancel'))


def cmd_proxies(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/proxies')
        out(args, rows, [{**p, 'last_seen': fmt_ts(p['last_seen'])} for p in rows], ['id', 'name', 'connection_name', 'online', 'address', 'version', 'last_seen'])
    elif args.action == 'add':
        res = c.call('POST', '/proxies', json={'name': args.name, 'connection_id': resolve_connection(c, args.connection)})
        out(args, res)
        if not args.json:
            print(f'\n/etc/hvbackup/agent.conf on the proxy VM:\nHVB_URL={c.url}\nHVB_PROXY_TOKEN={res["token"]}\nHVB_INSECURE=1')
    elif args.action == 'deploy':
        if not args.name or not args.connection:
            raise SystemExit('usage: hvbackup proxies deploy <name> --connection <id|name> [--host H --datastore DS --network NET --folder MOREF --cpus N --memory MB --ssh-key-file FILE --url URL --wait]')
        body = {'name': args.name, 'connection_id': resolve_connection(c, args.connection), 'host': args.host, 'datastore': args.datastore,
                'folder': args.folder, 'network': args.network, 'cpus': args.cpus, 'memory_mb': args.memory, 'timeout_minutes': args.timeout}
        if args.ssh_key_file:
            body['ssh_key'] = open(args.ssh_key_file).read().strip()
        if args.url:
            body['server_url'] = args.url
        elif not c.url.startswith(('https://127.0.0.1', 'https://localhost', 'http://127.0.0.1', 'http://localhost')):
            body['server_url'] = c.url
        res = c.call('POST', '/proxies/deploy', json=body)
        out(args, res)
        if args.wait and not args.json:
            t = wait_task(c, res['task_id'])
            print(json.dumps(t.get('result', {}).get('result'), indent=2, default=str))
    elif args.action == 'image':
        out(args, c.call('GET', '/proxies/image'))
    elif args.action == 'delete':
        out(args, c.call('DELETE', f'/proxies/{args.id}'))


def cmd_users(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/users')
        out(args, rows, rows, ['id', 'username', 'role', 'disabled'])
    elif args.action == 'add':
        pw = args.password or getpass.getpass('password: ')
        out(args, c.call('POST', '/users', json={'username': args.username, 'password': pw, 'role': args.role}))
    elif args.action == 'passwd':
        pw = args.password or getpass.getpass('new password: ')
        out(args, c.call('PATCH', f'/users/{args.username}', json={'password': pw}))
    elif args.action == 'delete':
        out(args, c.call('DELETE', f'/users/{args.username}'))


def cmd_tokens(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/tokens')
        out(args, rows, [{**t, 'created': fmt_ts(t['created']), 'last_used': fmt_ts(t['last_used'])} for t in rows], ['id', 'username', 'name', 'prefix', 'created', 'last_used'])
    elif args.action == 'add':
        out(args, c.call('POST', '/tokens', json={'name': args.name, 'username': args.username}))
    elif args.action == 'delete':
        out(args, c.call('DELETE', f'/tokens/{args.id}'))


def cmd_export(args):
    c = Client()
    res = c.call('POST', f'/restore-points/{args.rp}/export', json={'format': args.format})
    out(args, res)
    if args.wait and not args.json:
        t = wait_task(c, res['task_id'])
        print(json.dumps(t.get('result', {}).get('result'), indent=2, default=str))


def cmd_exports(args):
    c = Client()
    if args.action == 'list':
        rows = c.call('GET', '/exports')
        out(args, rows, [{**e, 'created': fmt_ts(e['created']), 'size': fmt_bytes(e['size_bytes'])} for e in rows], ['id', 'vm_name', 'restore_point_id', 'format', 'status', 'size', 'created', 'path', 'error'])
    elif args.action == 'delete':
        out(args, c.call('DELETE', f'/exports/{args.id}'))
    elif args.action == 'download':
        r = c.s.get(c.url + f'/api/v1/exports/{args.id}/download', stream=True, timeout=3600)
        if r.status_code >= 400:
            sys.exit(f'error {r.status_code}: {r.text[:200]}')
        name = args.out or r.headers.get('content-disposition', 'export.ova').split('filename=')[-1].strip('"')
        with open(name, 'wb') as f:
            for blk in r.iter_content(8 * 2**20):
                f.write(blk)
        print(name)


def cmd_vsphere_plugin(args):
    c = Client()
    out(args, c.call('POST', f'/connections/{args.connection}/vsphere-plugin', json={'action': args.action}, timeout=120))


def cmd_gc(args):
    c = Client()
    res = c.call('POST', '/maintenance/gc')
    out(args, res)
    if not args.json:
        wait_task(c, res['task_id'])


def main(argv=None):
    from .. import APP_VERSION
    ap = argparse.ArgumentParser(prog='hvbackup', description='hypervisor backup server')
    ap.add_argument('--json', action='store_true', help='raw JSON output')
    ap.add_argument('--version', action='version', version=APP_VERSION)
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('server', help='run the server').set_defaults(fn=cmd_server)
    p = sub.add_parser('init-admin', help='create or reset an admin account'); p.add_argument('username'); p.add_argument('--password'); p.set_defaults(fn=cmd_init_admin)
    sub.add_parser('agent', help='run the proxy agent').set_defaults(fn=cmd_agent)

    p = sub.add_parser('status'); p.add_argument('--tsv', action='store_true'); p.set_defaults(fn=cmd_status)
    p = sub.add_parser('connections'); p.add_argument('action', choices=['list', 'add', 'test', 'refresh', 'delete', 'placement']); p.add_argument('id', nargs='?')
    p.add_argument('--type', choices=['vcenter', 'esxi', 'proxmox'], default='vcenter'); p.add_argument('--name'); p.add_argument('--host'); p.add_argument('--username'); p.add_argument('--password'); p.add_argument('--verify-ssl', action='store_true'); p.set_defaults(fn=cmd_connections)
    p = sub.add_parser('vms'); p.add_argument('action', choices=['list'], nargs='?', default='list'); p.add_argument('--connection', type=int); p.set_defaults(fn=cmd_vms)
    p = sub.add_parser('backup'); p.add_argument('vm'); p.add_argument('--wait', action='store_true'); p.add_argument('--no-quiesce', action='store_true'); p.set_defaults(fn=cmd_backup)
    p = sub.add_parser('jobs'); p.add_argument('action', choices=['list', 'add', 'run', 'delete']); p.add_argument('id', nargs='?')
    p.add_argument('--name'); p.add_argument('--vms', nargs='*', default=[]); p.add_argument('--connection', type=int); p.add_argument('--schedule', help='cron, e.g. "0 2 * * *"')
    p.add_argument('--keep-last', type=int, default=14); p.add_argument('--keep-days', type=int, default=0); p.add_argument('--no-quiesce', action='store_true'); p.add_argument('--wait', action='store_true'); p.set_defaults(fn=cmd_jobs)
    p = sub.add_parser('runs'); p.add_argument('action', choices=['list'], nargs='?', default='list'); p.add_argument('--limit', type=int, default=50); p.set_defaults(fn=cmd_runs)
    p = sub.add_parser('restore-points'); p.add_argument('action', choices=['list'], nargs='?', default='list'); p.add_argument('--vm'); p.set_defaults(fn=cmd_restore_points)
    p = sub.add_parser('restore'); p.add_argument('rp', type=int); p.add_argument('--name', required=True); p.add_argument('--power-on', action='store_true'); p.add_argument('--keep-identity', action='store_true')
    p.add_argument('--host', help='target host name as the target connection shows it'); p.add_argument('--datastore'); p.add_argument('--folder', help='VM folder moref on the target (see connections placement)')
    p.add_argument('--connection', help='target connection id or name; any connection, not only the one the backup came from')
    p.add_argument('--network', action='append', metavar='SOURCE=TARGET', help='map a source network to one on the target (repeatable)')
    p.add_argument('--wait', action='store_true'); p.set_defaults(fn=cmd_restore)
    p = sub.add_parser('tasks'); p.add_argument('action', choices=['list', 'show', 'wait', 'cancel']); p.add_argument('id', nargs='?', type=int); p.add_argument('--limit', type=int, default=30); p.set_defaults(fn=cmd_tasks)
    p = sub.add_parser('proxies', help='proxy VMs: list, add (register one you install yourself), deploy (the server builds one), image, delete')
    p.add_argument('action', choices=['list', 'add', 'deploy', 'image', 'delete']); p.add_argument('name', nargs='?'); p.add_argument('--connection', help='id or name')
    p.add_argument('--host'); p.add_argument('--datastore'); p.add_argument('--folder', help='VM folder moref'); p.add_argument('--network')
    p.add_argument('--cpus', type=int, default=2); p.add_argument('--memory', type=int, default=2048, help='MiB'); p.add_argument('--ssh-key-file', help='public key for the ubuntu user')
    p.add_argument('--url', help='URL the proxy calls the server on (default: HVB_URL of the server, else this client\'s URL)'); p.add_argument('--timeout', type=int, default=20, help='minutes to wait for the agent')
    p.add_argument('--wait', action='store_true'); p.set_defaults(fn=cmd_proxies)
    p = sub.add_parser('users'); p.add_argument('action', choices=['list', 'add', 'passwd', 'delete']); p.add_argument('username', nargs='?'); p.add_argument('--password'); p.add_argument('--role', default='viewer'); p.set_defaults(fn=cmd_users)
    p = sub.add_parser('tokens'); p.add_argument('action', choices=['list', 'add', 'delete']); p.add_argument('name', nargs='?'); p.add_argument('--username'); p.add_argument('--id', type=int); p.set_defaults(fn=cmd_tokens)
    sub.add_parser('gc', help='run garbage collection now').set_defaults(fn=cmd_gc)
    p = sub.add_parser('export', help='export a copy of a restore point as OVA/OVF (the backup is untouched)'); p.add_argument('rp', type=int); p.add_argument('--format', choices=['ova', 'ovf'], default='ova'); p.add_argument('--wait', action='store_true'); p.set_defaults(fn=cmd_export)
    p = sub.add_parser('exports', help='list, download or delete exports'); p.add_argument('action', choices=['list', 'download', 'delete']); p.add_argument('id', nargs='?', type=int); p.add_argument('--out'); p.set_defaults(fn=cmd_exports)
    p = sub.add_parser('vsphere-plugin', help='register the vSphere Client plug-in on a vCenter connection'); p.add_argument('action', choices=['register', 'update', 'unregister', 'status']); p.add_argument('--connection', type=int, required=True); p.set_defaults(fn=cmd_vsphere_plugin)

    args = ap.parse_args(argv)
    if args.cmd == 'proxies' and args.action == 'delete':
        args.id = args.name
    if args.cmd == 'tokens' and args.action == 'delete' and args.id is None and args.name and args.name.isdigit():
        args.id = int(args.name)
    args.fn(args)


if __name__ == '__main__':
    main()
