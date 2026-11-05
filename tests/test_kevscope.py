"""Tests against six real traces captured on QNX 8.0 (QEMU x86_64) with demo/rtdemo.c.

inversion.kev        rtdemo bad:        sensor channel uses _NTO_CHF_FIXED_PRIORITY (no priority inheritance)
inherit.kev          rtdemo good:       default channel, sensor inherits control's priority 50
mutex-inversion.kev  rtdemo mutex-bad:  process-private mutex created with PTHREAD_PRIO_NONE
mutex-inherit.kev    rtdemo mutex-good: default mutex (PTHREAD_PRIO_INHERIT)
ipc-inversion.kev    rtdemo ipc-bad:    separate client/server processes on CPUs 1/0, fixed priority
ipc-inherit.kev      rtdemo ipc-good:   same topology, default channel priority inheritance
"""
import gzip
import struct
from pathlib import Path

import pytest

from kevscope import kev, model, perfetto, report
from traceprinter_ref import compare

DATA = Path(__file__).parent / "data"


TRACES = ("inversion", "inherit", "mutex-inversion", "mutex-inherit", "ipc-inversion", "ipc-inherit")


@pytest.fixture(scope="module", params=TRACES)
def name(request):
    return request.param


@pytest.fixture(scope="module")
def models():
    return {n: model.build(kev.read(DATA / f"{n}.kev")) for n in TRACES}


def _thread(m, name):
    return next(th for th in m.threads.values() if th.name == name and th.process.endswith("rtdemo"))


def test_decoder_matches_qnx_traceprinter(name):
    r = compare(DATA / f"{name}.kev", DATA / f"{name}.traceprinter.txt.gz", verbose=False)
    assert r["reference"] == r["decoded"] == r["matched"] > 12000
    assert r["checked"] > 3500
    assert not r["problems"]


def test_header(models):
    t = models["inversion"].trace
    assert t.header["TRACE_SYS_RELEASE"] == "8.0.0"
    assert t.cpus == 2 and t.cycles_per_sec > 10**9
    assert t.dropped == 0 and t.partial == 0


def test_fixture_wall_clock_metadata_is_redacted(name):
    data = (DATA / f"{name}.kev").read_bytes()
    header, offset = kev.parse_header(data)
    assert header['TRACE_DATE'] == header['TRACE_BOOT_DATE'] == 'redacted'
    # QNX 8.0 x86_64 syspage qtime: time-of-day adjustment and boot UTC seconds.
    qtime_offset, qtime_size = struct.unpack_from('<HH', data, offset + 20)
    assert qtime_size >= 24
    qtime = data[offset + qtime_offset:offset + qtime_offset + qtime_size]
    assert qtime[8:16] == bytes(8) and qtime[20:24] == bytes(4)
    reference = (DATA / f"{name}.traceprinter.txt.gz").read_bytes()
    assert reference[4:8] == bytes(4)  # gzip mtime
    text = gzip.decompress(reference).decode('utf-8')
    assert 'TRACE_DATE:: redacted' in text and 'TRACE_BOOT_DATE:: redacted' in text


def test_names_and_threads(models):
    m = models["inversion"]
    demo = {th.name: th for th in m.threads.values() if th.process.endswith("rtdemo")}
    assert set(demo) == {"control", "sensor", "logger"}
    assert {p for *_, p in demo["control"].runs} == {50}
    assert {p for *_, p in demo["logger"].runs} == {30}
    assert sum(1 for th in m.threads.values() if th.idle) == 2


def test_server_priority_shows_inheritance(models):
    sensor = lambda m: next(th for th in m.threads.values() if th.name == "sensor")  # noqa: E731
    assert {p for *_, p in sensor(models["inversion"]).runs} == {10}
    assert {p for *_, p in sensor(models["inherit"]).runs} == {50}


def test_report_finds_the_inversion_and_blames_logger(models):
    s = report.summary(models["inversion"])
    assert len(s["findings"]) == 1
    f = s["findings"][0]
    assert f["client"].startswith("control") and f["server"].startswith("sensor")
    assert f["server_prios"] == [10] and f["client_prio"] == 50
    assert f["culprits"][0]["thread"].startswith("logger")
    assert f["culprits"][0]["ms"] > 0.9 * f["stolen_ms_total"]
    # rtdemo itself counted 55 of 300 cycles over budget in 3 s; the trace holds 1 s of it
    assert 15 <= f["affected"] <= 40
    ch = s["channels"][0]
    assert ch["max_ms"] > 5.0      # blocked past the 5 ms budget


def test_report_is_clean_with_inheritance(models):
    s = report.summary(models["inherit"])
    assert s["findings"] == []
    ch = s["channels"][0]
    assert ch["client"].startswith("control") and ch["server"].startswith("sensor")
    assert ch["max_ms"] < 3.0      # rtdemo good: worst cycle 2.96 ms


def test_report_kinds_stay_apart(models):
    assert [f["kind"] for f in report.summary(models["inversion"])["findings"]] == ["priority_inversion"]
    assert [f["kind"] for f in report.summary(models["mutex-inversion"])["findings"]] == ["mutex_priority_inversion"]


# ---- mutexes ---------------------------------------------------------------------------------------

def test_mutex_waits_know_the_mutex_and_its_owner(models):
    for n, proto in (("mutex-inversion", "PTHREAD_PRIO_NONE"), ("mutex-inherit", "PTHREAD_PRIO_INHERIT")):
        m = models[n]
        control, sensor = _thread(m, "control"), _thread(m, "sensor")
        waits = [w for w in m.mutex_waits if w.waiter == control.key]
        assert len(waits) >= 25
        assert len({w.sync for w in waits}) == 1
        assert {model.mutex_protocol(w.flags) for w in waits} == {proto}
        # the owner word in SyncMutexLock and the thread whose SyncMutexUnlock ended the wait agree
        assert all(w.owner == w.released_by == sensor.key for w in waits)
        assert all(w.start < w.end for w in waits)


def test_owner_word_decoding():
    threads = {(663571, 1): None, (663571, 2): None, (1, 2): None}
    assert model.owner_thread(threads, 0x20130002) == (663571, 2)
    assert model.owner_thread(threads, 0xA0130002) == (663571, 2)   # _NTO_SYNC_WAITING set
    assert model.owner_thread(threads, 0x00010002, near_pid=1) == (1, 2)
    assert model.owner_thread(threads, 0) is None
    assert model.owner_thread(threads, 0xFFFFFFFB) is None          # condvar marker, not an owner
    assert model.owner_thread(threads, 0x20130009) is None          # no such thread


@pytest.mark.parametrize("owner_unlock_observed", [False, True])
def test_mutex_unlocks_do_not_cross_process_address_spaces(owner_unlock_observed):
    waiter_key, owner_key, unrelated_key = (101, 1), (101, 2), (202, 2)
    mutex_address = 0x1000
    events = [
        kev.Event(0, 1, kev.PR_TH, kev.ST_RUNNING, [*unrelated_key, 30, 1]),
        kev.Event(0, 0, kev.PR_TH, kev.ST_RUNNING, [*owner_key, 10, 1]),
        kev.Event(1, 0, kev.PR_TH, kev.ST_RUNNING, [*waiter_key, 50, 1]),
        kev.Event(2, 0, kev.KER_CALL, model.KER_MUTEX_LOCK,
                  [mutex_address, 0, model.SYNC_NONRECURSIVE | model.SYNC_PRIONONE,
                   owner_key[0] << 16 | owner_key[1]]),
        kev.Event(3, 0, kev.PR_TH, model.ST_MUTEX, [*waiter_key, 50, 1]),
        kev.Event(4, 0, kev.PR_TH, kev.ST_RUNNING, [*owner_key, 10, 1]),
    ]
    if owner_unlock_observed:
        events.append(kev.Event(5, 0, kev.KER_CALL, model.KER_MUTEX_UNLOCK, [mutex_address, 0]))
    events.extend([
        kev.Event(6, 1, kev.KER_CALL, model.KER_MUTEX_UNLOCK, [mutex_address, 0]),
        kev.Event(7, 0, kev.PR_TH, kev.ST_READY, [*waiter_key, 50, 1]),
        kev.Event(8, 0, kev.PR_TH, kev.ST_RUNNING, [*waiter_key, 50, 1]),
    ])
    trace_model = model.build(kev.Kev({}, 1_000_000_000, 2, events))
    wait = trace_model.mutex_waits[0]
    assert wait.owner == owner_key
    assert wait.released_by == (owner_key if owner_unlock_observed else None)
    assert unrelated_key not in wait.holders


@pytest.mark.parametrize("changed_at", [0, 4])
def test_running_priority_change_preserves_the_new_priority(changed_at):
    owner_key = (101, 2)
    events = [
        kev.Event(0, 0, kev.PR_TH, kev.ST_RUNNING, [*owner_key, 10, 1]),
        kev.Event(changed_at, 0, kev.PR_TH, kev.ST_RUNNING, [*owner_key, 50, 1]),
        kev.Event(10, 0, kev.PR_TH, kev.ST_RUNNING, [101, 1, 50, 1]),
    ]
    trace_model = model.build(kev.Kev({}, 1_000_000_000, 1, events))
    expected_runs = [(0, changed_at, 0, 10)] if changed_at else []
    expected_runs.append((changed_at, 10, 0, 50))
    assert trace_model.threads[owner_key].runs == expected_runs


def test_mutex_owner_priority_shows_inheritance(models):
    def owner_prios(m):
        sensor = _thread(m, "sensor")
        out = set()
        for w in m.mutex_waits:
            if w.owner == sensor.key:
                out |= {p for s, e, _, p in sensor.runs if e > w.start and s < w.end}
        return out
    assert owner_prios(models["mutex-inversion"]) == {10}
    assert owner_prios(models["mutex-inherit"]) == {50}


def test_report_finds_the_mutex_inversion_and_blames_logger(models):
    s = report.summary(models["mutex-inversion"])
    assert len(s["findings"]) == 1
    f = s["findings"][0]
    assert f["waiter"].startswith("control") and f["owners"][0].startswith("sensor")
    assert f["waiter_prio"] == 50 and f["owner_prios"] == [10]
    assert f["protocol"] == "PTHREAD_PRIO_NONE"
    assert f["culprits"][0]["thread"].startswith("logger")
    assert f["culprits"][0]["ms"] > 0.9 * f["stolen_ms_total"]
    # rtdemo counted 12 of 300 cycles over budget in 3 s; the trace holds 1 s of it
    assert 3 <= f["affected"] <= 15
    assert f["worst"]["blocked_ms"] > 4.4      # with 0.6 ms of work, the 5 ms budget is gone
    text = report.render(s)
    assert "MUTEX PRIORITY INVERSION" in text and "_NTO_SYNC_PRIONONE" in text


def test_report_is_clean_with_mutex_inheritance(models):
    s = report.summary(models["mutex-inherit"])
    assert s["findings"] == []
    mx = next(x for x in s["mutexes"] if x["waiter"].startswith("control"))
    assert mx["owners"][0].startswith("sensor") and mx["protocol"] == "PTHREAD_PRIO_INHERIT"
    assert mx["max_ms"] < 2.0      # sensor runs at 50 and finishes its 2 ms critical section


def test_messages_pair_up(models):
    m = models["inversion"]
    demo = [x for x in m.messages if m.threads[x.client].name == "control" and x.server
            and m.threads[x.server].name == "sensor"]
    assert len(demo) >= 95
    assert all(x.sent <= x.received <= x.replied for x in demo)


# ---- Perfetto output: decode it back with a tiny protobuf reader --------------------------------

def _fields(buf):
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        f, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(buf, i)
        elif wire == 1:
            v = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        elif wire == 2:
            n, i = _varint(buf, i)
            v = buf[i:i + n]
            i += n
        else:
            raise AssertionError(f"unexpected wire type {wire}")
        yield f, v


def _varint(buf, i):
    v = shift = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7


def test_perfetto_output_structure(models):
    m = models["inversion"]
    inv = perfetto.all_inversions(m)
    data = perfetto.convert(m, inv)
    switches = names = 0
    slice_names = []
    for f, packet in _fields(data):
        assert f == 1
        for pf, pv in _fields(packet):
            if pf == perfetto.P_FTRACE_EVENTS:
                for bf, bv in _fields(pv):
                    if bf == 2:
                        switches += any(ef == 4 for ef, _ in _fields(bv))
            elif pf == perfetto.P_PROCESS_TREE:
                names += sum(1 for tf, _ in _fields(pv) if tf == 2)
            elif pf == perfetto.P_TRACK_EVENT:
                slice_names += [v.decode() for ef, v in _fields(pv) if ef == 23]
    assert switches == len(m.switches)
    assert names == sum(1 for th in m.threads.values() if not th.idle)
    # Two replies from procnto used to terminate unrelated control -> sensor calls.
    # Matching the actual client pid/tid retains all 100 calls and all 29 findings.
    assert slice_names.count("priority inversion") == len(inv) == 29
    assert slice_names.count("MsgSend -> sensor") == 100


def test_perfetto_output_marks_mutex_waits(models):
    m = models["mutex-inversion"]
    inv = perfetto.all_inversions(m)
    assert len(inv) == 6 and all(isinstance(i, report.MutexInversion) for i in inv)
    slice_names = []
    for _, packet in _fields(perfetto.convert(m, inv)):
        for pf, pv in _fields(packet):
            if pf == perfetto.P_TRACK_EVENT:
                slice_names += [v.decode() for ef, v in _fields(pv) if ef == 23]
    assert slice_names.count("mutex priority inversion") == 6
    control = _thread(m, "control")
    assert slice_names.count("mutex wait -> sensor") == sum(1 for w in m.mutex_waits if w.waiter == control.key)


def test_rejects_non_trace(tmp_path):
    p = tmp_path / "x.kev"
    p.write_bytes(b"hello")
    with pytest.raises(kev.KevError):
        kev.read(p)


def test_truncated_trace_still_parses():
    data = (DATA / "inversion.kev").read_bytes()
    t = kev.read(data[: len(data) // 2 + 7])
    assert 5000 < len(t.events) < 12000
