"""Scheduling counterexamples: each priority comparison must apply at the same instant."""
import pytest

from kevscope import kev, model, report


@pytest.fixture(params=["message", "mutex"])
def scenario(request):
    control = model.Thread(100, 1, "control", "rtdemo")
    sensor = model.Thread(100, 2, "sensor", "rtdemo")
    logger = model.Thread(100, 3, "logger", "rtdemo")
    control.changes = [(0, kev.ST_RUNNING, 50, 1), (5, model.ST_MUTEX, 50, 1),
                       (40, kev.ST_RUNNING, 50, 1)]
    control.runs = [(0, 5, 1, 50), (40, 50, 1, 50)]
    sensor.changes = [(0, kev.ST_RUNNING, 10, 1), (10, kev.ST_READY, 10, 1),
                      (30, kev.ST_RUNNING, 10, 1)]
    sensor.runs = [(0, 10, 0, 10), (30, 40, 0, 10)]
    logger.changes = [(10, kev.ST_RUNNING, 30, 1)]
    logger.runs = [(10, 30, 0, 30)]
    trace = kev.Kev({}, 1000, 2, [])
    mdl = model.Model(trace, {t.key: t for t in (control, sensor, logger)}, {}, [], [], [], 0, 50)
    if request.param == "message":
        control.changes[1] = (5, kev.STATES.index("REPLY"), 50, 1)
        mdl.messages = [model.Message(control.key, sensor.key, sent=5, received=6, replied=40)]
    else:
        mdl.mutex_waits = [model.MutexWait(control.key, 5, 40, sync=0x1000,
                                         flags=model.SYNC_PRIONONE, owner=sensor.key,
                                         released_by=sensor.key)]
    return mdl, control, sensor, logger


def inversions(mdl):
    groups = report.analyze(mdl) if mdl.messages else report.analyze_mutexes(mdl)
    return [inv for group in groups.values() for inv in group.inversions]


def test_one_cpu_interference(scenario):
    mdl, _, _, logger = scenario
    inv, = inversions(mdl)
    assert inv.stolen == 20 and inv.culprits == {logger.key: 20}


def test_later_priority_boost_does_not_erase_earlier_inversion(scenario):
    mdl, _, sensor, _ = scenario
    sensor.changes[-1] = (30, kev.ST_RUNNING, 50, 1)
    sensor.runs[-1] = (30, 40, 0, 50)
    inv, = inversions(mdl)
    assert inv.stolen == 20


def test_ready_priority_boost_stops_attribution_at_that_instant(scenario):
    mdl, _, sensor, _ = scenario
    sensor.changes.insert(2, (20, kev.ST_READY, 50, 1))
    sensor.changes[-1] = (30, kev.ST_RUNNING, 50, 1)
    sensor.runs[-1] = (30, 40, 0, 50)
    inv, = inversions(mdl)
    assert inv.stolen == 10


def test_waiter_priority_drop_stops_attribution_at_that_instant(scenario):
    mdl, control, _, _ = scenario
    control.changes.insert(2, (20, control.changes[1][1], 25, 1))
    inv, = inversions(mdl)
    assert inv.stolen == 10


def test_inherited_ready_priority_prevents_false_positive(scenario):
    mdl, _, sensor, _ = scenario
    sensor.changes[1] = (10, kev.ST_READY, 50, 1)
    assert inversions(mdl) == []


def test_unknown_culprit_priority_is_not_priority_zero(scenario):
    mdl, _, _, logger = scenario
    logger.runs[0] = (10, 30, 0, None)
    assert inversions(mdl) == []


def test_migration_across_a_ready_interval_is_not_assumed_to_be_one_cpu(scenario):
    mdl, _, sensor, _ = scenario
    sensor.runs[-1] = (30, 40, 1, 10)
    assert inversions(mdl) == []


def test_unrelated_cpu_is_not_added_because_owner_migrates_later(scenario):
    mdl, control, sensor, logger = scenario
    sensor.changes = [(0, kev.ST_RUNNING, 10, 1), (10, kev.ST_READY, 10, 1),
                      (20, kev.ST_RUNNING, 10, 1), (25, kev.ST_READY, 10, 1),
                      (35, kev.ST_RUNNING, 10, 1)]
    sensor.runs = [(0, 10, 0, 10), (20, 25, 0, 10), (35, 45, 1, 10)]
    logger.runs = [(10, 20, 0, 30)]
    bystander = model.Thread(200, 1, "unrelated", "other-app", runs=[(10, 20, 1, 30)])
    mdl.threads[bystander.key] = bystander
    control.changes[-1] = (45, kev.ST_RUNNING, 50, 1)
    control.runs[-1] = (45, 50, 1, 50)
    if mdl.messages:
        mdl.messages[0].replied = 45
    else:
        mdl.mutex_waits[0].end = 45
    inv, = inversions(mdl)
    assert inv.stolen == 10 and inv.culprits == {logger.key: 10}


def test_culprit_priority_comes_from_the_evidence_interval(scenario):
    mdl, _, _, logger = scenario
    logger.runs.append((40, 50, 0, 80))
    summary = report.summary(mdl)
    assert summary['findings'][0]['culprits'][0]['prio'] == 30


def test_process_scope_keeps_external_interference(scenario):
    mdl, _, _, logger = scenario
    del mdl.threads[logger.key]
    logger.pid = 200
    logger.process = 'another-process'
    mdl.threads[logger.key] = logger
    summary = report.summary(mdl, process='rtdemo')
    assert summary['findings'][0]['culprits'][0]['thread'] == logger.label
    assert all('another-process' not in row['thread'] for row in summary['cpu'])
