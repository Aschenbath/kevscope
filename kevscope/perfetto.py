"""Write a Perfetto trace (protobuf) from a kevscope model, using only the standard library.

What you get in ui.perfetto.dev:
  - CPU lanes with every context switch (ftrace sched_switch / sched_waking records)
  - one track per QNX thread, with its scheduling state and slices for each MsgSend
    (client side) and each message being served (server side), linked by flow arrows
  - a slice for each mutex wait on the waiting thread, with arrows to the owner and back
  - a "kevscope findings" track marking each priority inversion found by the report

QNX ids don't fit Linux's model (pids are large, tids restart at 1 in every process), so each
thread gets a synthetic Linux-style tid; the real pid/tid stay in the thread name.
Priorities are QNX priorities (higher = more urgent), unlike Linux's kernel prio.
"""
import struct

from . import kev, model as M, report

# ---- minimal protobuf encoding -------------------------------------------------------------


def _varint(v):
    if v < 0:
        v += 1 << 64
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field, wire):
    return _varint((field << 3) | wire)


def u(field, v):
    return _key(field, 0) + _varint(int(v))


def s(field, text):
    raw = text.encode("utf-8")
    return _key(field, 2) + _varint(len(raw)) + raw


def m(field, *parts):
    body = b"".join(parts)
    return _key(field, 2) + _varint(len(body)) + body


def f64(field, v):
    return _key(field, 1) + struct.pack("<Q", v)


# ---- field numbers (perfetto/protos/perfetto/trace/...) -----------------------------------
TRACE_PACKET = 1
P_FTRACE_EVENTS, P_PROCESS_TREE, P_TIMESTAMP, P_SEQ_ID = 1, 2, 8, 10
P_TRACK_EVENT, P_SEQ_FLAGS, P_TRACK_DESCRIPTOR = 11, 13, 60
SEQ_INCREMENTAL_STATE_CLEARED = 1

TYPE_SLICE_BEGIN, TYPE_SLICE_END, TYPE_INSTANT = 1, 2, 3

# Linux task states as Perfetto decodes sched_switch.prev_state
LINUX_RUNNABLE, LINUX_SLEEPING, LINUX_DEAD = 0, 1, 0x80

SEQ = 7  # trusted_packet_sequence_id for our track events


def _packet(*parts):
    return m(TRACE_PACKET, *parts)


class _Ids:
    """Synthetic Linux tids: tid 1 of a process uses the pid (like Linux), others count up."""

    def __init__(self, model):
        self.map = {}
        top = max((th.pid for th in model.threads.values()), default=1)
        nxt = top + 1
        for key in sorted(model.threads):
            th = model.threads[key]
            if th.idle:
                self.map[key] = 0
            elif th.tid == 1:
                self.map[key] = th.pid
            else:
                self.map[key] = nxt
                nxt += 1

    def __getitem__(self, key):
        return self.map.get(key, 0)


def _base(th):
    if th.name:
        return th.name
    proc = th.process.rsplit("/", 1)[-1] if th.process else "thread"
    return proc if th.tid == 1 else f"{proc}:{th.tid}"


def _comm(th):
    return _base(th)[:15]  # Linux comm is 16 bytes with NUL


def _thread_name(th):
    return f"{_base(th)} [{th.pid}/{th.tid}]"


def write(model, path, findings=None):
    data = convert(model, findings)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


def convert(model, findings=None):
    cps = model.trace.cycles_per_sec

    def ns(t):
        return t * 1_000_000_000 // cps

    ids = _Ids(model)
    out = []

    # process tree: processes and threads with their names
    procs = {}
    for th in model.threads.values():
        if not th.idle:
            procs.setdefault(th.pid, th.process)
    tree = []
    for pid, name in sorted(procs.items()):
        ppid = model.processes.get(pid, (0, ""))[0]
        tree.append(m(1, u(1, pid), u(2, ppid), s(3, name or f"pid {pid}")))
    for key, th in sorted(model.threads.items()):
        if th.idle:
            continue
        tree.append(m(2, u(1, ids[key]), u(3, th.pid), s(2, _thread_name(th))))
    out.append(_packet(u(P_TIMESTAMP, ns(model.start)), m(P_PROCESS_TREE, *tree)))

    # scheduling: one ftrace bundle per CPU
    per_cpu = {}
    for (t, cpu, prev_key, prev_state, next_key, next_prio) in model.switches:
        prev = model.threads.get(prev_key) if prev_key else None
        nxt = model.threads[next_key]
        if prev is None or prev.idle:
            p_comm, p_pid, p_prio, p_state = "swapper", 0, 0, LINUX_RUNNABLE
        else:
            p_comm, p_pid = _comm(prev), ids[prev_key]
            st, pr = prev.state_at(t)
            p_prio = pr or 0
            if prev_state in (kev.ST_READY, kev.ST_RUNNING, None):
                p_state = LINUX_RUNNABLE
            elif prev_state in (kev.ST_DEAD, kev.ST_DESTROY):
                p_state = LINUX_DEAD
            else:
                p_state = LINUX_SLEEPING
        if nxt.idle:
            n_comm, n_pid, n_prio = "swapper", 0, 0
        else:
            n_comm, n_pid, n_prio = _comm(nxt), ids[next_key], next_prio or 0
        sw = m(4, s(1, p_comm), u(2, p_pid), u(3, p_prio), u(4, p_state),
               s(5, n_comm), u(6, n_pid), u(7, n_prio))
        per_cpu.setdefault(cpu, []).append(m(2, u(1, ns(t)), u(2, p_pid), sw))
    for (t, key, prio, cpu) in model.wakeups:
        th = model.threads[key]
        if th.idle:
            continue
        wk = m(20, s(1, _comm(th)), u(2, ids[key]), u(3, prio or 0), u(4, 1), u(5, cpu))
        per_cpu.setdefault(cpu, []).append(m(2, u(1, ns(t)), u(2, 0), wk))
    for cpu in sorted(per_cpu):
        evs = sorted(per_cpu[cpu], key=_event_ts)
        out.append(_packet(m(P_FTRACE_EVENTS, u(1, cpu), *evs)))

    # track events: message passing on thread tracks, findings on their own track
    tracks = {}

    def thread_track(key):
        if key not in tracks:
            th = model.threads[key]
            uuid = 0x1000_0000 + len(tracks) + 1
            tracks[key] = uuid
            out.append(_packet(u(P_SEQ_ID, SEQ), m(P_TRACK_DESCRIPTOR, u(1, uuid),
                       m(4, u(1, th.pid), u(2, ids[key]), s(5, _thread_name(th))))))
        return tracks[key]

    first = True

    def event(t, track, kind, name=None, flows=(), ends=(), args=()):
        nonlocal first
        parts = [u(9, kind), u(11, track)]
        if name:
            parts.append(s(23, name))
        parts += [f64(47, f) for f in flows]
        parts += [f64(48, f) for f in ends]
        for k, v in args:
            parts.append(m(4, s(10, k), u(4, v) if isinstance(v, int) else s(6, str(v))))
        hdr = [u(P_TIMESTAMP, ns(t)), u(P_SEQ_ID, SEQ)]
        if first:
            hdr.append(u(P_SEQ_FLAGS, SEQ_INCREMENTAL_STATE_CLEARED))
            first = False
        out.append(_packet(*hdr, m(P_TRACK_EVENT, *parts)))

    for msg in model.messages:
        if not msg.service_complete or msg.server == msg.client:
            continue
        client, server = model.threads[msg.client], model.threads[msg.server]
        ct, st = thread_track(msg.client), thread_track(msg.server)
        flow = 0x2000_0000 + msg.flow
        event(msg.sent, ct, TYPE_SLICE_BEGIN, f"MsgSend -> {server.name or server.tid}", flows=[flow],
              args=[("server", server.label), ("rcvid", msg.rcvid)])
        event(msg.replied, ct, TYPE_SLICE_END)
        begin = msg.accepted
        event(begin, st, TYPE_SLICE_BEGIN, f"serve {client.name or client.tid}", ends=[flow],
              args=[("client", client.label), ("rcvid", msg.rcvid),
                    ("receive_event_delay_us", int(model.ms(msg.received - begin) * 1000))])
        event(msg.replied, st, TYPE_SLICE_END, flows=[flow + 0x1000_0000])
        event(msg.replied, ct, TYPE_INSTANT, "error reply" if msg.error else "reply", ends=[flow + 0x1000_0000])

    for w in model.mutex_waits:
        if not w.reportable:
            continue
        waiter = model.threads[w.waiter]
        wt = thread_track(w.waiter)
        owner = w.attribution_owner
        info = [("mutex", f"0x{w.sync:x}"), ("protocol", M.mutex_protocol(w.flags))]
        if owner is None:
            reason = ("shared object identity unknown" if (w.flags or 0) & M.SYNC_SHARED
                      else "owner changed; handoff time unknown" if len(w.holders) > 1
                      else "owner unknown")
            event(w.start, wt, TYPE_SLICE_BEGIN, "mutex wait (owner uncertain)",
                  args=info + [("ownership", reason)])
            event(w.end, wt, TYPE_SLICE_END)
            continue
        holder = model.threads[owner]
        ht = thread_track(owner)
        flow = 0x4000_0000 + w.flow
        info.append(("owner", holder.label))
        event(w.start, wt, TYPE_SLICE_BEGIN, f"mutex wait -> {holder.name or holder.tid}", flows=[flow], args=info)
        event(w.end, wt, TYPE_SLICE_END)
        event(w.start, ht, TYPE_INSTANT, f"{waiter.name or waiter.tid} waits for my mutex", ends=[flow],
              args=[("waiter", waiter.label)])
        returned = []
        if w.released_by == owner and w.released_at is not None and w.start <= w.released_at <= w.end:
            returned = [flow + 0x1000_0000]
            event(w.released_at, ht, TYPE_INSTANT, "mutex unlock call", flows=returned)
        # Leaving MUTEX state does not establish successful acquisition (for example, cancellation).
        event(w.end, wt, TYPE_INSTANT, "mutex wait ended", ends=returned)

    if findings:
        lanes = []          # (uuid, end of the last slice on it)

        def lane(a, b):
            for i, (uuid, busy_until) in enumerate(lanes):
                if busy_until <= a:
                    lanes[i] = (uuid, b)
                    return uuid
            uuid = 0x3000_0000 + len(lanes)
            name = "kevscope findings" if not lanes else f"kevscope findings ({len(lanes) + 1})"
            out.append(_packet(u(P_SEQ_ID, SEQ), m(P_TRACK_DESCRIPTOR, u(1, uuid), s(2, name))))
            lanes.append((uuid, b))
            return uuid

        def ran_instead(inv):
            culprits = sorted(inv.culprits.items(), key=lambda kv: -kv[1])
            return ", ".join(f"{model.threads[k].name or k[1]} {model.ms(v):.2f} ms" for k, v in culprits[:3])

        spans = []
        for inv in findings:
            if isinstance(inv, report.MutexInversion):
                spans.append((inv.wait.start, inv.wait.end, inv))
            else:
                spans.append((inv.message.sent, inv.message.replied, inv))
        for a, b, inv in sorted(spans, key=lambda x: x[:2]):
            track = lane(a, b)
            if isinstance(inv, report.MutexInversion):
                w = inv.wait
                event(a, track, TYPE_SLICE_BEGIN, "mutex priority inversion",
                      args=[("waiter", model.threads[w.waiter].label), ("waiter_prio", inv.waiter_prio),
                            ("owner", ", ".join(model.threads[k].label for k in w.holders)),
                            ("owner_prio", inv.owner_prio),
                            ("mutex", f"0x{w.sync:x}"), ("protocol", M.mutex_protocol(w.flags)),
                            ("blocked_us", int(model.ms(inv.blocked) * 1000)),
                            ("stolen_us", int(model.ms(inv.stolen) * 1000)),
                            ("ran_instead", ran_instead(inv))])
            else:
                msg = inv.message
                event(a, track, TYPE_SLICE_BEGIN, "priority inversion",
                      args=[("client", model.threads[msg.client].label), ("client_prio", inv.client_prio),
                            ("server", model.threads[msg.server].label), ("server_prio", inv.server_prio),
                            ("blocked_us", int(model.ms(inv.wait) * 1000)),
                            ("stolen_us", int(model.ms(inv.stolen) * 1000)),
                            ("ran_instead", ran_instead(inv))])
            event(b, track, TYPE_SLICE_END)
    return b"".join(out)


def _event_ts(encoded):
    """Timestamp of an encoded FtraceEvent (first field) for sorting."""
    # layout: key(2) len(varint) key(1) ts(varint)
    i = 1
    while encoded[i] & 0x80:
        i += 1
    i += 2  # end of length varint, then field-1 key byte
    v, shift = 0, 0
    while True:
        b = encoded[i]
        v |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return v
        shift += 7


def all_inversions(model):
    """Message-passing and mutex priority inversions, for the findings track."""
    schedule = report.Schedule(model)
    msgs = [inv for ch in report.analyze(model, schedule).values() for inv in ch.inversions]
    mutexes = [inv for lk in report.analyze_mutexes(model, schedule).values() for inv in lk.inversions]
    return msgs + mutexes
