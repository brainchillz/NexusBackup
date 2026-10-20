"""Tasks: every long operation is a row in `tasks` run by a thread in this process.
Progress, message and log are updated in place; the API and UI poll them. A task
checks `cancelled()` at safe points."""
import json
import logging
import threading
import time
import traceback

from .. import db

log = logging.getLogger('hvbackup.tasks')
_registry = {}
_threads = {}
MAX_LOG = 256 * 1024


class Task:
    def __init__(self, task_id):
        self.id = task_id

    def log(self, msg):
        line = time.strftime('%H:%M:%S ') + str(msg) + '\n'
        log.info('task %s: %s', self.id, msg)
        with db.conn() as c:
            cur = c.execute('SELECT log FROM tasks WHERE id=?', (self.id,)).fetchone()[0] or ''
            cur = (cur + line)[-MAX_LOG:]
            c.execute('UPDATE tasks SET log=?, message=? WHERE id=?', (cur, str(msg)[:200], self.id))

    def progress(self, frac, message=None):
        with db.conn() as c:
            if message is not None:
                c.execute('UPDATE tasks SET progress=?, message=? WHERE id=?', (min(max(frac, 0), 1), message[:200], self.id))
            else:
                c.execute('UPDATE tasks SET progress=? WHERE id=?', (min(max(frac, 0), 1), self.id))

    def cancelled(self):
        with db.conn() as c:
            return bool(c.execute('SELECT cancel FROM tasks WHERE id=?', (self.id,)).fetchone()[0])

    def check_cancel(self):
        if self.cancelled():
            raise Cancelled()


class Cancelled(Exception):
    pass


def register(kind, fn):
    _registry[kind] = fn


def create(kind, subject=None, created_by=None, **params):
    with db.conn() as c:
        c.execute('INSERT INTO tasks(kind,subject,state,created,created_by,result_json) VALUES(?,?,?,?,?,?)',
                  (kind, subject, 'queued', time.time(), created_by, json.dumps({'params': params})))
        task_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    start(task_id, kind, params)
    return task_id


def start(task_id, kind, params):
    fn = _registry[kind]

    def run():
        t = Task(task_id)
        with db.conn() as c:
            c.execute("UPDATE tasks SET state='running', started=? WHERE id=?", (time.time(), task_id))
        try:
            result = fn(t, **params)
            state, err = 'done', None
        except Cancelled:
            state, err, result = 'cancelled', 'cancelled', None
        except Exception as e:  # noqa: BLE001
            state, err, result = 'failed', f'{type(e).__name__}: {e}', None
            t.log('FAILED: ' + err)
            log.error('task %s failed\n%s', task_id, traceback.format_exc())
        with db.conn() as c:
            c.execute('UPDATE tasks SET state=?, finished=?, message=COALESCE(?,message), result_json=?, progress=CASE WHEN ?="done" THEN 1 ELSE progress END WHERE id=?',
                      (state, time.time(), err, json.dumps({'params': params, 'result': result}, default=str), state, task_id))
        _threads.pop(task_id, None)

    th = threading.Thread(target=run, name=f'task-{task_id}', daemon=True)
    _threads[task_id] = th
    th.start()


def get(task_id):
    with db.conn() as c:
        return db.row_to_dict(c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())


def list_tasks(limit=100, state=None):
    with db.conn() as c:
        if state:
            rows = c.execute('SELECT id,kind,subject,state,progress,message,created,started,finished,created_by FROM tasks WHERE state=? ORDER BY id DESC LIMIT ?', (state, limit))
        else:
            rows = c.execute('SELECT id,kind,subject,state,progress,message,created,started,finished,created_by FROM tasks ORDER BY id DESC LIMIT ?', (limit,))
        return [db.row_to_dict(r) for r in rows]


def cancel(task_id):
    with db.conn() as c:
        c.execute('UPDATE tasks SET cancel=1 WHERE id=? AND state IN ("queued","running")', (task_id,))


def recover_orphans():
    """On startup, anything still marked running belonged to a previous process."""
    with db.conn() as c:
        c.execute("UPDATE tasks SET state='failed', message='server restarted while running', finished=? WHERE state IN ('queued','running')", (time.time(),))
        c.execute("UPDATE agent_work SET state='failed', error='server restarted' WHERE state IN ('queued','running')")
