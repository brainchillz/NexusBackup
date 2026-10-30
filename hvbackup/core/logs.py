"""The server log in memory: a ring of the last lines on the root logger, served by
GET /api/v1/logs so a container user never has to find the console. The level switch
applies to the root logger, so uvicorn and every hvbackup logger follow."""
import collections
import logging
import threading
import time

RING = 4000
_lines = collections.deque(maxlen=RING)
_lock = threading.Lock()
_seq = 0


class RingHandler(logging.Handler):
    def emit(self, record):
        global _seq
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            msg = record.getMessage()
        with _lock:
            _seq += 1
            _lines.append({'id': _seq, 'ts': record.created, 'level': record.levelname.lower(), 'logger': record.name, 'message': msg})


_handler = None


def install():
    global _handler
    if _handler is None:
        _handler = RingHandler()
        _handler.setFormatter(logging.Formatter('%(message)s'))
        logging.getLogger().addHandler(_handler)
    return _handler


def set_level(level):
    lv = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    if root.level != lv:
        root.setLevel(lv)
        for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
            logging.getLogger(name).setLevel(lv)


def tail(limit=200, level=None, after=None, grep=None):
    order = {'debug': 10, 'info': 20, 'warning': 30, 'error': 40, 'critical': 50}
    floor = order.get((level or 'debug').lower(), 10)
    needle = (grep or '').lower()
    with _lock:
        rows = [l for l in _lines if order.get(l['level'], 20) >= floor and (after is None or l['id'] > after)
                and (not needle or needle in l['message'].lower() or needle in l['logger'].lower())]
    return rows[-limit:]


def line(level, logger, message):
    """For tests and for lines that do not come through logging."""
    rec = logging.LogRecord(logger, getattr(logging, level.upper()), '', 0, message, (), None)
    rec.created = time.time()
    install().emit(rec)
