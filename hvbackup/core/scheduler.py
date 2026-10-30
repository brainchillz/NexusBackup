"""Job scheduler: a thread wakes every 30 s, runs any enabled job (backup or replication)
whose next_run has passed, and computes the following next_run from its cron expression."""
import datetime
import logging
import threading
import time

from croniter import croniter

from .. import db

log = logging.getLogger('hvbackup.scheduler')
_stop = threading.Event()


def next_run(cron, base=None):
    """Timestamp of the next firing after `base`, with the expression read in the configured
    zone (croniter takes a bare timestamp as UTC)."""
    if not cron:
        return None
    from . import settings
    start = datetime.datetime.fromtimestamp(base or time.time(), settings.zone())
    return croniter(cron, start).get_next(float)


def validate(cron):
    if not cron:
        return True
    return croniter.is_valid(cron)


TABLES = ('jobs', 'replications')


def refresh_next_runs():
    with db.conn() as c:
        for table in TABLES:
            for j in c.execute(f'SELECT id,schedule,next_run FROM {table} WHERE enabled=1').fetchall():
                if j['schedule'] and not j['next_run']:
                    c.execute(f'UPDATE {table} SET next_run=? WHERE id=?', (next_run(j['schedule']), j['id']))


def due_rows(table, now):
    """Rows of `table` whose schedule has come, with their next_run already advanced."""
    with db.conn() as c:
        due = [db.row_to_dict(r) for r in c.execute(
            f'SELECT * FROM {table} WHERE enabled=1 AND schedule IS NOT NULL AND next_run IS NOT NULL AND next_run<=?', (now,))]
        for j in due:
            c.execute(f'UPDATE {table} SET next_run=? WHERE id=?', (next_run(j['schedule'], now), j['id']))
    return due


def _tick(run_job, run_replication=None):
    now = time.time()
    for table, fn in (('jobs', run_job), ('replications', run_replication)):
        if fn is None:
            continue
        for j in due_rows(table, now):
            try:
                log.info('scheduler: starting %s %s', table[:-1], j['name'])
                fn(j['id'], created_by='scheduler')
            except Exception as e:  # noqa: BLE001
                log.error('scheduler: %s %s failed to start: %s', table[:-1], j['name'], e)


def start(run_job, every_tick=None, run_replication=None):
    refresh_next_runs()

    def loop():
        while not _stop.wait(30):
            try:
                _tick(run_job, run_replication)
                if every_tick:
                    every_tick()
            except Exception:  # noqa: BLE001
                log.exception('scheduler tick failed')
    threading.Thread(target=loop, name='scheduler', daemon=True).start()


def stop():
    _stop.set()
