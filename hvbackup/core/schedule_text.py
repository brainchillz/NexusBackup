"""Plain words for the cron expressions the UI's schedule picker writes, and the next
firings, so nobody has to read cron to know when a job runs. The picker covers:

  0 */N * * *      every N hours
  M H * * *        daily at H:M
  M H * * d,d      weekly on those days at H:M
  M H D * *        monthly on day D at H:M

Anything else is described as the raw expression ("custom schedule")."""
import datetime
import re

from croniter import croniter

DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
_DOW = {'0': 6, '7': 6, '1': 0, '2': 1, '3': 2, '4': 3, '5': 4, '6': 5,
        'sun': 6, 'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5}


def _hm(m, h):
    return f'{int(h):02d}:{int(m):02d}'


def _ordinal(n):
    n = int(n)
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suffix}'


def describe(cron):
    """'Every day at 02:00', 'Every Monday and Friday at 04:30', ... or the expression."""
    if not cron or not cron.strip():
        return 'manual only'
    parts = cron.split()
    if len(parts) != 5 or not croniter.is_valid(cron):
        return f'invalid schedule ({cron})'
    m, h, dom, mon, dow = parts
    if re.fullmatch(r'\d{1,2}', m) and re.fullmatch(r'\*/\d{1,2}', h) and dom == mon == dow == '*':
        n = int(h[2:])
        return 'Every hour' if n == 1 else f'Every {n} hours' + (f' at minute {int(m)}' if int(m) else '')
    if re.fullmatch(r'\d{1,2}', m) and re.fullmatch(r'\d{1,2}', h):
        if dom == mon == dow == '*':
            return f'Every day at {_hm(m, h)}'
        if dom == mon == '*':
            try:
                days = sorted({_DOW[d.lower()] for d in dow.split(',')})
            except KeyError:
                days = None
            if days:
                names = [DAYS[d] for d in days]
                if len(names) == 7:
                    return f'Every day at {_hm(m, h)}'
                if days == [0, 1, 2, 3, 4]:
                    return f'Weekdays at {_hm(m, h)}'
                if days == [5, 6]:
                    return f'Weekends at {_hm(m, h)}'
                return f'Every {", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]} at {_hm(m, h)}'
        if re.fullmatch(r'\d{1,2}', dom) and mon == '*' and dow == '*':
            return f'Monthly on the {_ordinal(dom)} at {_hm(m, h)}'
    return f'custom schedule ({cron})'


def preview(cron, count=3):
    """Description plus the next `count` firings as timestamps, in the configured zone."""
    from . import scheduler, settings
    out = {'cron': cron or '', 'valid': True, 'description': describe(cron), 'next': [], 'timezone': settings.get('timezone')}
    if not cron or not cron.strip():
        return out
    if not croniter.is_valid(cron):
        out['valid'] = False
        return out
    t = None
    for _ in range(count):
        t = scheduler.next_run(cron, t)
        out['next'].append(t)
    out['next_text'] = [datetime.datetime.fromtimestamp(t, settings.zone()).strftime('%a %d %b %H:%M') for t in out['next']]
    return out
