"""Tests against two real traces captured on QNX 8.0 (QEMU x86_64) with demo/rtdemo.c.

inversion.kev  rtdemo bad:  sensor channel uses _NTO_CHF_FIXED_PRIORITY (no priority inheritance)
inherit.kev    rtdemo good: default channel, sensor inherits control's priority 50
"""
import struct
from pathlib import Path

import pytest

from kevscope import kev, model, perfetto, report
from traceprinter_ref import compare

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module", params=["inversion", "inherit"])
def name(request):
    return request.param


@pytest.fixture(scope="module")
def models():
    return {n: model.build(kev.read(DATA / f"{n}.kev")) for n in ("inversion", "inherit")}


def test_decoder_matches_qnx_traceprinter(name):
    r = compare(DATA / f"{name}.kev", DATA / f"{name}.traceprinter.txt.gz", verbose=False)
    assert r["reference"] == r["decoded"] == r["matched"] > 15000
    assert r["checked"] > 4000
    assert not r["problems"]


def test_header(models):
    t = models["inversion"].trace
    assert t.header["TRACE_SYS_RELEASE"] == "8.0.0"
    assert t.cpus == 2 and t.cycles_per_sec > 10**9
    assert t.dropped == 0 and t.partial == 0


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
    assert slice_names.count("priority inversion") == len(inv) == 27
    assert slice_names.count("MsgSend -> sensor") >= 95


def test_rejects_non_trace(tmp_path):
    p = tmp_path / "x.kev"
    p.write_bytes(b"hello")
    with pytest.raises(kev.KevError):
        kev.read(p)


def test_truncated_trace_still_parses():
    data = (DATA / "inversion.kev").read_bytes()
    t = kev.read(data[: len(data) // 2 + 7])
    assert 5000 < len(t.events) < 12000
