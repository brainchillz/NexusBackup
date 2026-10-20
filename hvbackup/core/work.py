"""Agent work queue: rows in agent_work that a proxy agent long-polls for.
The server enqueues a read or write job, hot-adds the disk, and waits here."""
import json
import os
import time

from .. import config, db
from .tasks import Cancelled


def enqueue(proxy_id, kind, spec, task_id=None):
    with db.conn() as c:
        c.execute('INSERT INTO agent_work(proxy_id,task_id,kind,spec_json,created) VALUES(?,?,?,?,?)',
                  (proxy_id, task_id, kind, json.dumps(spec), time.time()))
        return c.execute('SELECT last_insert_rowid()').fetchone()[0]


def take(proxy_id):
    with db.conn() as c:
        r = c.execute("SELECT * FROM agent_work WHERE proxy_id=? AND state='queued' ORDER BY id LIMIT 1", (proxy_id,)).fetchone()
        if not r:
            return None
        c.execute("UPDATE agent_work SET state='running', taken=? WHERE id=?", (time.time(), r['id']))
        return db.row_to_dict(r)


def get(work_id):
    with db.conn() as c:
        return db.row_to_dict(c.execute('SELECT * FROM agent_work WHERE id=?', (work_id,)).fetchone())


def progress(work_id, done_bytes):
    with db.conn() as c:
        c.execute('UPDATE agent_work SET progress_bytes=? WHERE id=?', (done_bytes, work_id))


def finish(work_id, result=None, error=None):
    with db.conn() as c:
        c.execute('UPDATE agent_work SET state=?, result_json=?, error=?, finished=? WHERE id=?',
                  ('failed' if error else 'done', json.dumps(result or {}), error, time.time(), work_id))


def records_path(work_id):
    d = os.path.join(config.REPO_DIR, 'staging')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'work-{work_id}.rec')


def wait(task, work_id, total_bytes, label, timeout=24 * 3600, on_progress=None):
    """Block until the agent finishes; mirrors its progress into the task."""
    t0 = time.time()
    last = -1
    while True:
        w = get(work_id)
        if w['state'] == 'done':
            return w
        if w['state'] == 'failed':
            raise RuntimeError(f'{label}: agent failed: {w.get("error")}')
        if task.cancelled():
            with db.conn() as c:
                c.execute("UPDATE agent_work SET state='failed', error='cancelled' WHERE id=? AND state!='done'", (work_id,))
            raise Cancelled()
        if time.time() - t0 > timeout:
            raise RuntimeError(f'{label}: agent timed out')
        if total_bytes and w['progress_bytes'] != last:
            last = w['progress_bytes']
            if on_progress:
                on_progress(last)
        time.sleep(1)
