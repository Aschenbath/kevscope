"""Turn decoded kernel events into threads, scheduling intervals and message-passing transactions."""
from collections import defaultdict
from dataclasses import dataclass, field

from . import kev

POLICIES = {1: "FIFO", 2: "RR", 3: "OTHER", 4: "SPORADIC", 8: "INTR"}

# QNX blocked states, named the way QNX documentation does
BLOCKED_LABELS = {
    kev.STATES.index("SEND"): "SEND-blocked", kev.STATES.index("RECEIVE"): "RECEIVE-blocked",
    kev.STATES.index("REPLY"): "REPLY-blocked", kev.STATES.index("NANOSLEEP"): "NANOSLEEP",
    kev.STATES.index("MUTEX"): "MUTEX-blocked", kev.STATES.index("CONDVAR"): "CONDVAR-blocked",
    kev.STATES.index("SEM"): "SEM-blocked", kev.STATES.index("INTR"): "INTR-blocked",
    kev.STATES.index("JOIN"): "JOIN-blocked", kev.STATES.index("SIGWAITINFO"): "SIGWAITINFO",
    kev.STATES.index("SIGSUSPEND"): "SIGSUSPEND", kev.STATES.index("WAITPAGE"): "WAITPAGE",
    kev.STATES.index("STOPPED"): "STOPPED",
}


@dataclass
class Thread:
    pid: int
    tid: int
    name: str = ""
    process: str = ""
    idle: bool = False
    # (t, state, prio, policy) for every state change, in time order
    changes: list = field(default_factory=list)
    # (start, end, cpu, prio) for every stretch on a CPU
    runs: list = field(default_factory=list)

    @property
    def key(self):
        return (self.pid, self.tid)

    @property
    def label(self):
        base = self.name or f"tid {self.tid}"
        proc = self.process.rsplit("/", 1)[-1] if self.process else f"pid {self.pid}"
        return f"{base} ({proc} {self.pid}/{self.tid})"

    def state_at(self, t):
        """(state, prio) in effect at time t, or (None, None) before the first change."""
        lo, hi = 0, len(self.changes)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.changes[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return None, None
        _, st, prio, _ = self.changes[lo - 1]
        return st, prio


@dataclass
class Message:
    """One MsgSend: client blocked from send until the server's MsgReply (or MsgError)."""
    client: tuple
    server: tuple = None
    rcvid: int = 0
    sent: int = 0          # SND_MESSAGE time
    received: int = None   # REC_MESSAGE time
    replied: int = None    # REPLY_MESSAGE / MSG_ERROR time
    error: bool = False
    flow: int = 0


@dataclass
class Model:
    trace: kev.Kev
    threads: dict
    processes: dict        # pid -> (ppid, name)
    messages: list
    switches: list         # (t, cpu, prev_key, prev_state, next_key, next_prio)
    wakeups: list          # (t, key, prio, cpu)
    start: int
    end: int

    def thread(self, key):
        return self.threads.get(key)

    def ns(self, t):
        return self.trace.ns(t)

    def ms(self, cycles):
        return cycles * 1000.0 / self.trace.cycles_per_sec


def build(trace):
    threads = {}
    processes = {}

    def get(pid, tid):
        th = threads.get((pid, tid))
        if th is None:
            th = threads[(pid, tid)] = Thread(pid, tid)
            if pid in processes:
                th.process = processes[pid][1]
        return th

    running = {}           # cpu -> (key, since, prio)
    switches, wakeups = [], []
    messages, open_by_rcvid = [], {}
    flow_seq = 0
    start = trace.events[0].t if trace.events else 0
    end = trace.events[-1].t if trace.events else 0

    for ev in trace.events:
        if ev.cls == kev.PR_TH:
            code, w = ev.code, ev.words
            if code == kev.PR_CREATE_NAME and len(w) >= 2:
                processes[w[1]] = (w[0], ev.string_at(2))
                for th in threads.values():
                    if th.pid == w[1]:
                        th.process = processes[w[1]][1]
            elif code == kev.TH_NAME and len(w) >= 2:
                th = get(w[0], w[1])
                th.name = ev.string_at(2)
                th.idle = th.pid == 1 and th.name.startswith("idle")
            elif code < kev.STATE_MAX and len(w) >= 2:
                th = get(w[0], w[1])
                prio = w[2] if len(w) >= 4 else None
                policy = w[3] if len(w) >= 4 else None
                if prio is None and th.changes:
                    prio, policy = th.changes[-1][2], th.changes[-1][3]
                th.changes.append((ev.t, code, prio, policy))
                if code == kev.ST_RUNNING:
                    prev = running.get(ev.cpu)
                    prev_state = None
                    if prev is not None:
                        pkey, since, pprio = prev
                        pth = threads[pkey]
                        if pkey != th.key:
                            pth.runs.append((since, ev.t, ev.cpu, pprio))
                            prev_state = pth.changes[-1][1] if pth.changes else kev.ST_READY
                    if prev is None or prev[0] != th.key:
                        switches.append((ev.t, ev.cpu, prev[0] if prev else None, prev_state, th.key, prio))
                    running[ev.cpu] = (th.key, ev.t if prev is None or prev[0] != th.key else prev[1], prio)
                elif code == kev.ST_READY:
                    # woken (or preempted): the CPU that logged it is where the decision was made
                    last = th.changes[-2][1] if len(th.changes) >= 2 else None
                    if last != kev.ST_RUNNING:
                        wakeups.append((ev.t, th.key, prio, ev.cpu))
        elif ev.cls == kev.COMM:
            actor = running.get(ev.cpu)
            actor_key = actor[0] if actor else None
            code, w = ev.code, ev.words
            rcvid = w[0] if w else 0
            if code == kev.COMM_SMSG and actor_key:
                flow_seq += 1
                m = Message(client=actor_key, rcvid=rcvid, sent=ev.t, flow=flow_seq)
                messages.append(m)
                open_by_rcvid[rcvid] = m
            elif code == kev.COMM_RMSG:
                m = open_by_rcvid.get(rcvid)
                if m is not None and m.received is None:
                    m.received = ev.t
            elif code in (kev.COMM_REPLY, kev.COMM_ERROR):
                m = open_by_rcvid.pop(rcvid, None)
                if m is not None:
                    m.replied = ev.t
                    m.error = code == kev.COMM_ERROR
                    m.server = actor_key

    for cpu, (key, since, prio) in running.items():
        threads[key].runs.append((since, end, cpu, prio))
    for th in threads.values():
        th.runs.sort()
    return Model(trace, threads, processes, messages, switches, wakeups, start, end)


def blocked_spans(th, end):
    """(start, end, state) for each stretch where th was blocked (not running, not ready)."""
    spans = []
    for i, (t, st, _, _) in enumerate(th.changes):
        if st in BLOCKED_LABELS:
            stop = th.changes[i + 1][0] if i + 1 < len(th.changes) else end
            if stop > t:
                spans.append((t, stop, st))
    return spans


def cpu_time(th, a, b):
    """Cycles th spent on a CPU within [a, b)."""
    total = 0
    for s, e, _, _ in th.runs:
        if e <= a:
            continue
        if s >= b:
            break
        total += min(e, b) - max(s, a)
    return total


def runs_between(model, a, b, exclude=()):
    """Per-thread cycles on any CPU within [a, b): {key: (cycles, max prio seen)}."""
    out = defaultdict(lambda: [0, 0])
    for th in model.threads.values():
        if th.key in exclude or th.idle:
            continue
        for s, e, _, prio in th.runs:
            if e <= a:
                continue
            if s >= b:
                break
            out[th.key][0] += min(e, b) - max(s, a)
            out[th.key][1] = max(out[th.key][1], prio or 0)
    return {k: tuple(v) for k, v in out.items() if v[0] > 0}
