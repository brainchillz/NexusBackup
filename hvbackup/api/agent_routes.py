"""Endpoints the proxy agent talks to. Authenticated with the proxy token only."""
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from .. import db
from ..core import work
from ..store.chunks import repo
from .deps import current_proxy

r = APIRouter(prefix='/api/v1/agent')


@r.get('/files/proxy_agent.py', include_in_schema=False)
def agent_source():
    """The agent program itself (no secrets): a proxy VM deployed from this server fetches it at first boot."""
    from ..agent import proxy_agent
    return FileResponse(proxy_agent.__file__, media_type='text/x-python', filename='proxy_agent.py')


@r.post('/hello')
async def hello(request: Request, proxy=Depends(current_proxy)):
    body = await request.json()
    with db.conn() as c:
        c.execute('UPDATE proxies SET last_seen=?, version=?, bios_uuid=COALESCE(?,bios_uuid), address=? WHERE id=?',
                  (time.time(), body.get('version'), body.get('bios_uuid') or None, request.client.host if request.client else None, proxy['id']))
        # an agent only says hello between jobs, so a running job at this point was lost in an agent restart: hand it out again
        c.execute("UPDATE agent_work SET state='queued', taken=NULL, progress_bytes=0 WHERE proxy_id=? AND state='running'", (proxy['id'],))
    return {'ok': True, 'name': proxy['name']}


@r.get('/work')
def get_work(wait: int = 25, proxy=Depends(current_proxy)):
    deadline = time.time() + min(max(wait, 0), 55)
    while True:
        w = work.take(proxy['id'])
        if w:
            return {'id': w['id'], 'kind': w['kind'], 'spec': w['spec']}
        if time.time() >= deadline:
            return Response(status_code=204)
        time.sleep(1)


def _own(work_id, proxy):
    w = work.get(work_id)
    if not w or w['proxy_id'] != proxy['id']:
        raise HTTPException(404, 'no such work')
    return w


@r.post('/work/{work_id}/progress')
async def progress(work_id: int, request: Request, proxy=Depends(current_proxy)):
    _own(work_id, proxy)
    body = await request.json()
    work.progress(work_id, int(body.get('bytes', 0)))
    return {'ok': True}


@r.post('/work/{work_id}/have')
async def have(work_id: int, request: Request, proxy=Depends(current_proxy)):
    _own(work_id, proxy)
    body = await request.json()
    rep = repo()
    missing = [h for h in body.get('hashes', []) if len(h) == 64 and not rep.has(bytes.fromhex(h))]
    return {'missing': missing}


@r.post('/have')
async def have_any(request: Request, proxy=Depends(current_proxy)):
    """Which of these chunk hashes are missing (no work item needed; used by the Proxmox node plug-in)."""
    body = await request.json()
    rep = repo()
    missing = [h for h in body.get('hashes', []) if len(h) == 64 and not rep.has(bytes.fromhex(h))]
    return {'missing': missing}


@r.put('/chunks/{hex_hash}')
async def put_chunk(hex_hash: str, request: Request, proxy=Depends(current_proxy)):
    if len(hex_hash) != 64:
        raise HTTPException(400)
    data = await request.body()
    try:
        _, stored, new = repo().put_compressed(data, bytes.fromhex(hex_hash))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'stored': stored, 'new': new}


@r.get('/chunks/{hex_hash}')
def get_chunk(hex_hash: str, proxy=Depends(current_proxy)):
    rep = repo()
    h = bytes.fromhex(hex_hash)
    if not rep.has(h):
        raise HTTPException(404)
    return FileResponse(rep.chunk_path(h), media_type='application/zstd')


@r.get('/work/{work_id}/index')
def get_index(work_id: int, proxy=Depends(current_proxy)):
    _own(work_id, proxy)
    return FileResponse(work.records_path(work_id), media_type='application/octet-stream')


@r.post('/work/{work_id}/complete')
async def complete(work_id: int, request: Request, seconds: float = 0, uploaded: int = 0, proxy=Depends(current_proxy)):
    w = _own(work_id, proxy)
    body = await request.body()
    if w['kind'] == 'read':
        with open(work.records_path(work_id), 'wb') as f:
            f.write(body)
    work.finish(work_id, result={'seconds': seconds or time.time() - (w['taken'] or w['created']), 'records': len(body) // 40, 'uploaded': uploaded})
    return {'ok': True}


@r.post('/work/{work_id}/fail')
async def fail(work_id: int, request: Request, proxy=Depends(current_proxy)):
    _own(work_id, proxy)
    body = await request.json()
    work.finish(work_id, error=str(body.get('error', 'unknown'))[:1000])
    return {'ok': True}
