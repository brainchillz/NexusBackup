"""Restart of the server process from the UI. The process exits once no task runs (new
tasks keep being accepted; the wait is bounded) and the supervisor starts it again:
Docker Compose with restart: unless-stopped, systemd with Restart=always (the shipped
unit). The exit code is 3, so a unit with Restart=on-failure restarts it too."""
import logging
import os
import sys
import threading
import time

from .. import db

log = logging.getLogger('hvbackup.server')
_pending = {}
EXIT_CODE = 3


def restart_pending():
    return dict(_pending) if _pending else None


def running_tasks():
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM tasks WHERE state='running'").fetchone()[0]


def request_restart(user, reason='', wait_seconds=600):
    """Exit as soon as no task runs, or after wait_seconds. Returns the state."""
    if _pending:
        return restart_pending()
    _pending.update({'requested': time.time(), 'by': user, 'reason': reason, 'deadline': time.time() + wait_seconds})
    db.audit(user, 'restart', reason)

    def waiter():
        while time.time() < _pending['deadline']:
            try:
                if running_tasks() == 0:
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(2)
        log.warning('restarting on request of %s%s', user, f' ({reason})' if reason else '')
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(EXIT_CODE)
    threading.Thread(target=waiter, name='restart', daemon=True).start()
    return restart_pending()
