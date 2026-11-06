"""Wake-up latency and Perfetto wake events must describe state transitions."""
from pathlib import Path

import pytest

from kevscope import kev, model, report

WORKER = (100, 1)
ASLEEP = kev.STATES.index("NANOSLEEP")


def trace_model(changes):
    events = [kev.Event(at, cpu, kev.PR_TH, state, [*WORKER, priority, 1])
              for at, state, priority, cpu in changes]
    return model.build(kev.Kev({}, 1000, 2, events))


def test_ready_priority_updates_keep_one_wakeup_and_its_original_start():
    mdl = trace_model([(0, ASLEEP, 10, 0), (10, kev.ST_READY, 10, 1),
                       (15, kev.ST_READY, 30, 0), (20, kev.ST_READY, 50, 1),
                       (30, kev.ST_RUNNING, 50, 1)])
    assert mdl.wakeups == [(10, WORKER, 10, 1)]
    assert report.wake_latency(mdl)[WORKER] == [(20, 10, 10)]


def test_priority_updates_after_preemption_are_not_wakeups():
    mdl = trace_model([(0, kev.ST_RUNNING, 10, 0), (10, kev.ST_READY, 10, 0),
                       (20, kev.ST_READY, 50, 0), (30, kev.ST_RUNNING, 50, 0)])
    assert mdl.wakeups == []
    assert report.wake_latency(mdl) == {}


def test_capture_starting_ready_does_not_invent_a_wakeup_boundary():
    mdl = trace_model([(10, kev.ST_READY, 10, 0), (20, kev.ST_READY, 50, 0),
                       (30, kev.ST_RUNNING, 50, 0)])
    assert mdl.wakeups == []
    assert report.wake_latency(mdl) == {}


@pytest.mark.parametrize("ending", [[], [(30, ASLEEP, 50, 0)],
                                   [(30, ASLEEP, 50, 0), (40, kev.ST_RUNNING, 50, 0)]])
def test_unfinished_or_cancelled_ready_interval_has_no_completed_latency(ending):
    mdl = trace_model([(0, ASLEEP, 10, 0), (10, kev.ST_READY, 10, 0),
                       (20, kev.ST_READY, 50, 0)] + ending)
    assert mdl.wakeups == [(10, WORKER, 10, 0)]
    assert report.wake_latency(mdl) == {}


def test_cancelled_wakeup_does_not_bleed_into_a_later_wakeup():
    mdl = trace_model([(0, ASLEEP, 10, 0), (10, kev.ST_READY, 10, 0),
                       (20, ASLEEP, 10, 0), (30, kev.ST_READY, 50, 1),
                       (35, kev.ST_READY, 50, 1), (40, kev.ST_RUNNING, 50, 1)])
    assert mdl.wakeups == [(10, WORKER, 10, 0), (30, WORKER, 50, 1)]
    assert report.wake_latency(mdl)[WORKER] == [(10, 30, 50)]


def test_equal_timestamp_priority_update_keeps_one_zero_latency_wakeup():
    mdl = trace_model([(0, ASLEEP, 10, 0), (5, kev.ST_READY, 10, 0),
                       (5, kev.ST_READY, 50, 0), (5, kev.ST_RUNNING, 50, 0)])
    assert mdl.wakeups == [(5, WORKER, 10, 0)]
    assert report.wake_latency(mdl)[WORKER] == [(0, 5, 10)]


class CountedChanges(list):
    """Bound sequence visits, so complexity checks do not depend on machine speed."""
    def __init__(self, values):
        super().__init__(values)
        self.visits = 0

    def __getitem__(self, index):
        self.visits += 1
        return super().__getitem__(index)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]


def test_long_cancelled_ready_interval_is_scanned_in_linear_work():
    th = model.Thread(*WORKER)
    th.changes = CountedChanges([(0, ASLEEP, 10, 1)] +
                               [(i, kev.ST_READY, i % 40 + 10, 1) for i in range(1, 2001)] +
                               [(2001, ASLEEP, 10, 1)])
    mdl = model.Model(kev.Kev({}, 1000, 1, []), {WORKER: th}, {}, [], [], [], 0, 2001)
    assert report.wake_latency(mdl) == {}
    assert th.changes.visits <= 4 * len(th.changes)


def test_native_mutex_inheritance_updates_do_not_inflate_sensor_wakeups():
    path = Path(__file__).parent / "data" / "mutex-inherit.kev"
    mdl = model.build(kev.read(path))
    sensor = next(th for th in mdl.threads.values()
                  if th.name == "sensor" and th.process.endswith("rtdemo"))
    samples = report.wake_latency(mdl)[sensor.key]
    # QNX's capture has 153 wake transitions and 38 updates while already READY.
    assert len(samples) == 153
    assert sum(key == sensor.key for _, key, _, _ in mdl.wakeups) == 153
    stats = next(row for row in report.summary(mdl)["wake_latency"] if row["thread"] == sensor.label)
    assert stats["wakeups"] == 153

