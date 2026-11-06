"""Turn decoded kernel events into threads, scheduling intervals, messages and mutex waits."""
from collections import defaultdict
from dataclasses import dataclass, field

from . import kev

POLICIES = {1: "FIFO", 2: "RR", 3: "OTHER", 4: "SPORADIC", 8: "INTR"}

ST_MUTEX = kev.STATES.index("MUTEX")
KER_MUTEX_LOCK, KER_MUTEX_UNLOCK, KER_CONDVAR_WAIT = 80, 81, 82

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


def wakeup_transition(previous, state):
    """A captured entry into READY, excluding preemption and READY updates."""
    return state == kev.ST_READY and previous not in (None, kev.ST_RUNNING, kev.ST_READY)


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
    destination_pid: int = None  # rcvid is scoped to this receiving process
    replier: tuple = None        # may differ from the thread that received it
    issues: set = field(default_factory=set)
    reply_started: int = None    # client entered REPLY after the kernel accepted its request

    @property
    def accepted(self):
        known = [at for at in (self.reply_started, self.received) if at is not None]
        return min(known) if known else None

    @property
    def service_complete(self):
        return (self.server is not None and self.received is not None and self.replied is not None
                and self.sent <= self.accepted <= self.received <= self.replied and not self.issues)


# mutex flags kept in the count word of a sync object (sys/target_nto.h)
SYNC_NONRECURSIVE, SYNC_SHARED, SYNC_PRIOCEILING, SYNC_PRIONONE = 0x80000000, 0x40000000, 0x20000000, 0x10000000
SYNC_WAITING, SYNC_OWNER_MASK = 0x80000000, 0x7FFFFFFF


def mutex_protocol(flags):
    """The pthread protocol a mutex was created with, from its sync flags."""
    if flags is None:
        return "unknown"
    if flags & SYNC_PRIONONE:
        return "PTHREAD_PRIO_NONE"
    if flags & SYNC_PRIOCEILING:
        return "PTHREAD_PRIO_PROTECT"
    return "PTHREAD_PRIO_INHERIT"


@dataclass
class MutexWait:
    """One stretch of a thread being MUTEX-blocked, from the MUTEX state change to leaving it."""
    waiter: tuple
    start: int
    end: int = None
    sync: int = None          # address of the mutex in the waiter's process
    flags: int = None         # _NTO_SYNC_* flags from the mutex's count word
    owner_raw: int = None     # owner word as the kernel saw it at SyncMutexLock
    owner: tuple = None       # thread that held the mutex when the wait began
    released_by: tuple = None # thread with a matching SyncMutexUnlock observed during the wait
    via_condvar: bool = False # re-acquiring the mutex after a condvar wake-up
    flow: int = 0
    started_before_trace: bool = False
    released_at: int = None   # timestamp of that observed unlock call, not the wait's end

    @property
    def holders(self):
        return [k for k in dict.fromkeys((self.owner, self.released_by)) if k and k != self.waiter]

    @property
    def reportable(self):
        """A complete captured wait with a known lock address."""
        return self.end is not None and self.sync is not None and not self.started_before_trace

    @property
    def attribution_owner(self):
        """The single supported private-lock owner, shared by analysis and causal export."""
        holders = self.holders
        if self.reportable and not (self.flags or 0) & SYNC_SHARED and len(holders) == 1:
            return holders[0]
        return None


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
    mutex_waits: list = field(default_factory=list)

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
    messages, open_by_client = [], {}
    open_by_destination = defaultdict(list)

    def forget_message(message):
        if open_by_client.get(message.client) is message:
            del open_by_client[message.client]
        key = (message.destination_pid, message.rcvid)
        remaining = [m for m in open_by_destination[key] if m is not message]
        if remaining:
            open_by_destination[key] = remaining
        else:
            del open_by_destination[key]
    flow_seq = 0
    last_sync_call = {}    # thread key -> (call, words) of its last mutex/condvar kernel call
    last_unlock = {}
    mutex_waits, open_wait = [], {}
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
                pending_message = open_by_client.get(th.key)
                if code == kev.STATES.index("REPLY") and pending_message is not None and pending_message.reply_started is None:
                    pending_message.reply_started = ev.t
                w_open = open_wait.get(th.key)
                if w_open is not None and code != ST_MUTEX:
                    w_open.end = ev.t
                    private = not ((w_open.flags or 0) & SYNC_SHARED)
                    hit = last_unlock.get((w_open.waiter[0], w_open.sync)) if w_open.sync is not None and private else None
                    if hit and w_open.start <= hit[0] <= ev.t:
                        w_open.released_by = hit[1]
                        w_open.released_at = hit[0]
                    del open_wait[th.key]
                elif w_open is None and code == ST_MUTEX:
                    flow_seq += 1
                    initial_state = all(change[1] == kev.ST_CREATE for change in th.changes[:-1])
                    w_new = MutexWait(th.key, ev.t, flow=flow_seq, started_before_trace=initial_state)
                    w_new.via_condvar = len(th.changes) > 1 and th.changes[-2][1] == kev.STATES.index("CONDVAR")
                    call, args = last_sync_call.pop(th.key, (None, None))
                    if call == KER_MUTEX_LOCK and len(args) >= 4:
                        w_new.sync = args[1] << 32 | args[0]
                        w_new.flags, w_new.owner_raw = args[2], args[3]
                    elif call == KER_CONDVAR_WAIT and len(args) >= 8:
                        w_new.sync = args[3] << 32 | args[2]
                        w_new.flags = args[6]
                        w_new.via_condvar = True
                    mutex_waits.append(w_new)
                    open_wait[th.key] = w_new
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
                    if prev is None or prev[0] != th.key:
                        running[ev.cpu] = (th.key, ev.t, prio)
                    elif prio != prev[2]:
                        if ev.t > prev[1]:
                            th.runs.append((prev[1], ev.t, ev.cpu, prev[2]))
                        running[ev.cpu] = (th.key, ev.t, prio)
                elif code == kev.ST_READY:
                    # Priority updates while already READY are not new wakeups.
                    # The logging CPU identifies where the decision was made.
                    last = th.changes[-2][1] if len(th.changes) >= 2 else None
                    if wakeup_transition(last, code):
                        wakeups.append((ev.t, th.key, prio, ev.cpu))
        elif ev.cls == kev.KER_CALL and not ev.code & kev.KER_EXIT_BIT:
            call = ev.code & 0x7F
            actor = running.get(ev.cpu)
            if actor and call in (KER_MUTEX_LOCK, KER_CONDVAR_WAIT):
                last_sync_call[actor[0]] = (call, ev.words)
            elif actor and call == KER_MUTEX_UNLOCK and len(ev.words) >= 2:
                lock_key = (actor[0][0], ev.words[1] << 32 | ev.words[0])
                last_unlock[lock_key] = (ev.t, actor[0])
        elif ev.cls == kev.COMM:
            actor = running.get(ev.cpu)
            actor_key = actor[0] if actor else None
            code, w = ev.code, ev.words
            if len(w) < 2:
                continue
            if code == kev.COMM_SMSG and actor_key:
                previous = open_by_client.get(actor_key)
                if previous is not None:
                    previous.issues.add("message_restarted")
                    forget_message(previous)
                flow_seq += 1
                m = Message(client=actor_key, rcvid=w[0], destination_pid=w[1], sent=ev.t, flow=flow_seq)
                messages.append(m)
                key = (m.destination_pid, m.rcvid)
                for previous in open_by_destination[key]:
                    previous.issues.add("message_id_collision")
                    m.issues.add("message_id_collision")
                open_by_destination[key].append(m)
                open_by_client[actor_key] = m
            elif code == kev.COMM_RMSG:
                pending = open_by_destination.get((w[1], w[0]), [])
                if len(pending) == 1:
                    m = pending[0]
                    if actor_key is None or actor_key[0] != m.destination_pid:
                        m.issues.add("message_receive_context")
                    elif m.received is not None:
                        m.issues.add("message_repeated_receive")
                    else:
                        m.received, m.server = ev.t, actor_key
            elif code in (kev.COMM_REPLY, kev.COMM_ERROR):
                # Unlike SND_MESSAGE/REC_MESSAGE, these payloads identify the client tid and pid.
                m = open_by_client.get((w[1], w[0]))
                if m is not None:
                    if actor_key is None or actor_key[0] != m.destination_pid:
                        m.issues.add("message_reply_mismatch")
                        continue
                    m.replied = ev.t
                    m.error = code == kev.COMM_ERROR
                    m.replier = actor_key
                    if m.server is None:
                        m.server = actor_key
                        if not m.error:
                            m.issues.add("message_missing_receive")
                    elif m.server != actor_key:
                        m.issues.add("message_handoff")
                    forget_message(m)

    for cpu, (key, since, prio) in running.items():
        threads[key].runs.append((since, end, cpu, prio))
    for th in threads.values():
        th.runs.sort()
    for w in mutex_waits:
        private_pid = None if (w.flags or 0) & SYNC_SHARED else w.waiter[0]
        w.owner = owner_thread(threads, w.owner_raw, private_pid)
    return Model(trace, threads, processes, messages, switches, wakeups, start, end, mutex_waits)


def owner_thread(threads, raw, near_pid=None):
    """Thread named by a mutex owner word: bits 30-16 are the low 15 bits of the pid, 15-0 the tid."""
    if raw is None:
        return None
    v = raw & SYNC_OWNER_MASK
    if v == 0 or raw >= 0xFFFFFF00:
        return None
    pid15, tid = v >> 16, v & 0xFFFF
    if near_pid is not None:
        return (near_pid, tid) if (near_pid & 0x7FFF) == pid15 and (near_pid, tid) in threads else None
    found = [k for k in threads if k[1] == tid and (k[0] & 0x7FFF) == pid15]
    return found[0] if len(found) == 1 else None


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
