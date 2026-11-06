"""Perfetto mutex arrows must not imply ownership or acquisition the trace did not prove."""
from pathlib import Path

import pytest

from kevscope import kev, model, perfetto, report
from test_kevscope import _fields


def mutex_capture(unlock=True, end_state=kev.ST_READY):
    waiter, owner = (101, 1), (101, 2)
    events = [
        kev.Event(0, 0, kev.PR_TH, kev.ST_RUNNING, [*owner, 10, 1]),
        kev.Event(1, 0, kev.PR_TH, kev.ST_RUNNING, [*waiter, 50, 1]),
        kev.Event(2, 0, kev.KER_CALL, model.KER_MUTEX_LOCK,
                  [0x1000, 0, model.SYNC_PRIONONE, owner[0] << 16 | owner[1]]),
        kev.Event(3, 0, kev.PR_TH, model.ST_MUTEX, [*waiter, 50, 1]),
        kev.Event(4, 0, kev.PR_TH, kev.ST_RUNNING, [*owner, 10, 1]),
    ]
    if unlock:
        events.append(kev.Event(7, 0, kev.KER_CALL, model.KER_MUTEX_UNLOCK, [0x1000, 0]))
    events.extend([
        kev.Event(9, 0, kev.PR_TH, end_state, [*waiter, 50, 1]),
        kev.Event(12, 0, kev.PR_TH, kev.ST_RUNNING, [*owner, 10, 1]),
    ])
    mdl = model.build(kev.Kev({}, 1_000_000_000, 1, events))
    mdl.threads[waiter].name = 'control'
    mdl.threads[owner].name = 'sensor'
    return mdl


def track_events(mdl):
    events = []
    for _, packet in _fields(perfetto.convert(mdl)):
        fields = dict(_fields(packet))
        if perfetto.P_TRACK_EVENT in fields:
            event = dict(_fields(fields[perfetto.P_TRACK_EVENT]))
            events.append((fields[perfetto.P_TIMESTAMP], event))
    return events


def named_events(mdl):
    return [(timestamp, event[23].decode()) for timestamp, event in track_events(mdl) if 23 in event]


def test_mutex_unlock_keeps_its_observed_timestamp():
    mdl = mutex_capture()
    wait, = mdl.mutex_waits
    assert wait.released_at == 7 and wait.end == 9
    events = named_events(mdl)
    assert (7, 'mutex unlock call') in events
    assert (9, 'mutex wait ended') in events
    assert all(name != 'got mutex' for _, name in events)


@pytest.mark.parametrize('end_state', [kev.ST_READY, kev.ST_DEAD])
def test_ending_a_wait_does_not_invent_an_unlock_or_acquisition(end_state):
    mdl = mutex_capture(unlock=False, end_state=end_state)
    events = named_events(mdl)
    assert (3, 'mutex wait -> sensor') in events
    assert (9, 'mutex wait ended') in events
    assert not any(name in ('mutex unlock', 'mutex unlock call', 'got mutex') for _, name in events)
    # Only the observed waiter-to-owner dependency has a flow, with no invented return arrow.
    assert sum(47 in event for _, event in track_events(mdl)) == 1
    assert sum(48 in event for _, event in track_events(mdl)) == 1


@pytest.mark.parametrize('condition', ['changed_owner', 'unknown_owner', 'shared'])
def test_uncertain_ownership_keeps_duration_without_causal_arrows(condition):
    mdl = mutex_capture()
    wait, = mdl.mutex_waits
    if condition == 'changed_owner':
        other = model.Thread(101, 3, name='other')
        mdl.threads[other.key] = other
        wait.released_by = other.key
    elif condition == 'unknown_owner':
        wait.owner = wait.released_by = None
    else:
        wait.flags |= model.SYNC_SHARED
    summary = report.summary(mdl)
    assert summary['mutexes'][0]['waits'] == 1
    assert summary['findings'] == []
    assert (3, 'mutex wait (owner uncertain)') in named_events(mdl)
    assert all(47 not in event and 48 not in event for _, event in track_events(mdl))
    assert not any(name in ('mutex unlock', 'mutex unlock call') for _, name in named_events(mdl))


@pytest.mark.parametrize('condition', ['left_censored', 'no_address', 'unfinished'])
def test_unreportable_waits_do_not_become_complete_perfetto_dependencies(condition):
    mdl = mutex_capture()
    wait, = mdl.mutex_waits
    if condition == 'left_censored':
        wait.started_before_trace = True
    elif condition == 'no_address':
        wait.sync = None
    else:
        wait.end = None
    assert report.summary(mdl)['mutexes'] == []
    assert track_events(mdl) == []


@pytest.mark.parametrize('name', ['mutex-inversion', 'mutex-inherit'])
def test_native_mutex_flows_use_captured_unlock_events(name):
    mdl = model.build(kev.read(Path(__file__).parent / 'data' / f'{name}.kev'))
    control = next(th for th in mdl.threads.values() if th.name == 'control')
    waits = [wait for wait in mdl.mutex_waits if wait.waiter == control.key]
    assert waits and all(wait.released_at is not None for wait in waits)
    assert all(wait.start <= wait.released_at <= wait.end for wait in waits)
    assert any(wait.released_at < wait.end for wait in waits)
    unlock_times = {t for t, name in named_events(mdl) if name == 'mutex unlock call'}
    assert {wait.released_at * 1_000_000_000 // mdl.trace.cycles_per_sec for wait in waits} <= unlock_times
