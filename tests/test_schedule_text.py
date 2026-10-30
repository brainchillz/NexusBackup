"""The schedule picker's cron shapes in plain words, and the preview endpoint."""
import os
import tempfile

os.environ.setdefault('HVB_DATA_DIR', tempfile.mkdtemp(prefix='hvb-test-data-'))  # conftest sets these first
os.environ.setdefault('HVB_REPO', tempfile.mkdtemp(prefix='hvb-test-repo-'))

from hvbackup.core import schedule_text, settings  # noqa: E402


def test_describe_covers_the_picker_shapes():
    d = schedule_text.describe
    assert d('') == 'manual only' and d(None) == 'manual only'
    assert d('0 */6 * * *') == 'Every 6 hours' and d('0 */1 * * *') == 'Every hour' and d('30 */2 * * *') == 'Every 2 hours at minute 30'
    assert d('0 2 * * *') == 'Every day at 02:00' and d('5 14 * * *') == 'Every day at 14:05'
    assert d('0 4 * * 0') == 'Every Sunday at 04:00' and d('0 4 * * 7') == 'Every Sunday at 04:00'
    assert d('0 1 * * 1,5') == 'Every Monday and Friday at 01:00'
    assert d('0 1 * * 1,2,3') == 'Every Monday, Tuesday and Wednesday at 01:00'
    assert d('0 1 * * 1,2,3,4,5') == 'Weekdays at 01:00' and d('0 1 * * 0,6') == 'Weekends at 01:00'
    assert d('0 1 * * 0,1,2,3,4,5,6') == 'Every day at 01:00'
    assert d('0 3 1 * *') == 'Monthly on the 1st at 03:00' and d('0 3 22 * *') == 'Monthly on the 22nd at 03:00' and d('0 3 13 * *') == 'Monthly on the 13th at 03:00'
    assert d('*/15 * * * *') == 'custom schedule (*/15 * * * *)'
    assert d('bogus') == 'invalid schedule (bogus)'


def test_preview_gives_next_runs_in_zone():
    settings.update({'timezone': 'UTC'})
    p = schedule_text.preview('0 4 * * 0')
    assert p['valid'] and p['description'] == 'Every Sunday at 04:00' and len(p['next']) == 3 and p['next'][0] < p['next'][1] < p['next'][2]
    assert all(t.startswith('Sun') and t.endswith('04:00') for t in p['next_text'])
    assert schedule_text.preview('')['next'] == [] and not schedule_text.preview('x y')['valid']
    settings.update({'timezone': None})
