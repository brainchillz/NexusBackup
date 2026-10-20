"""Job scheduler: a thread wakes every 30 s, runs any enabled job whose next_run has
passed, and computes the following next_run from its cron expression."""
import json
import logging
import threading
import time

from croniter import croniter

from .. import db

log = logging.getLogger('hvbackup.scheduler')
_stop = threading.Event()


def next_run(cron, base=None):
    if not cron:
        return None
    return croniter(cron, base or time.time()).get_next(float)


def validate(cron):
    if not cron:
        return True
    return croniter.is_valid(cron)


def refresh_next_runs():
    with db.conn() as c:
        for j in c.execute('SELECT id,schedule,next_run FROM jobs WHERE enabled=1').fetchall():
            if j['schedule'] and not j['next_run']:
                c.execute('UPDATE jobs SET next_run=? WHERE id=?', (next_run(j['schedule']), j['id']))


def _tick(run_job):
    now = time.time()
    with db.conn() as c:
        due = [db.row_to_dict(r) for r in c.execute(
            'SELECT * FROM jobs WHERE enabled=1 AND schedule IS NOT NULL AND next_run IS NOT NULL AND next_run<=?', (now,))]
        for j in due:
            c.execute('UPDATE jobs SET next_run=? WHERE id=?', (next_run(j['schedule'], now), j['id']))
    for j in due:
        try:
            log.info('scheduler: starting job %s', j['name'])
            run_job(j['id'], created_by='scheduler')
        except Exception as e:  # noqa: BLE001
            log.error('scheduler: job %s failed to start: %s', j['name'], e)


def start(run_job, every_tick=None):
    refresh_next_runs()

    def loop():
        while not _stop.wait(30):
            try:
                _tick(run_job)
                if every_tick:
                    every_tick()
            except Exception:  # noqa: BLE001
                log.exception('scheduler tick failed')
    threading.Thread(target=loop, name='scheduler', daemon=True).start()


def stop():
    _stop.set()
