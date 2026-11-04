"""Read QNX tracelogger .kev files (16-byte event encoding) without the QNX SDP.

Layout, as written by tracelogger on QNX 8.0:
    "TRACE_HEADER_BEGIN::" KEY::VALUE ... "TRACE_HEADER_END::"
    syspage blob (TRACE_SYSPAGE_LEN bytes)
    trace buffers, one per CPU at a time, each a run of 16-byte records:
        u32 header  bits 31-30 struct (simple / combine begin / continue / end)
                    bits 29-24 CPU, bits 14-10 class, bits 9-0 event
        u32 data[3] data[0] is the low 32 bits of ClockCycles() for every record

A combined event spans several records; its payload is data[1], data[2] of each record.
The high 32 bits of the clock come from CONTROL/TIME events, one stream per CPU.
"""
import re
import struct
from dataclasses import dataclass, field

# event classes (_TRACE_*_C >> 10)
EMPTY, CONTROL, KER_CALL, INT, PR_TH, SYSTEM, USER, COMM, QUIP, SEC, QVM, PMU = range(12)
CLASS_NAMES = ["EMPTY", "CONTROL", "KER_CALL", "INT", "PR_TH", "SYSTEM", "USER", "COMM",
               "QUIP", "SEC", "QVM", "PMU"]

S, CB, CC, CE = 0, 1, 2, 3

# CONTROL events
CTL_TIME, CTL_BUFFER, CTL_BUFFER_END, CTL_DROPPED = 1, 2, 4, 8

# thread states (sys/states.h); PR_TH events below 64 are thread state changes
STATES = ["DEAD", "RUNNING", "READY", "STOPPED", "SEND", "RECEIVE", "REPLY", "MQ_SEND",
          "MQ_RECEIVE", "WAITPAGE", "SIGSUSPEND", "SIGWAITINFO", "NANOSLEEP", "MUTEX",
          "CONDVAR", "JOIN", "INTR", "SEM", "WAITCTX", "RWLOCK_READ", "RWLOCK_WRITE",
          "BARRIER", "PIPE", "STATE_23", "CREATE", "DESTROY", "MUON_MUTEX", "TRACEBUFFER",
          "INTR_ATTACH_EV", "TIMER_DELEGATE", "SEND_NOTIFY"]
ST_DEAD, ST_RUNNING, ST_READY = 0, 1, 2
ST_CREATE, ST_DESTROY = 24, 25
STATE_MAX = 63

# PR_TH process events
PR_CREATE, PR_DESTROY, PR_CREATE_NAME, PR_DESTROY_NAME, TH_NAME = 64, 128, 192, 256, 320

# COMM events
COMM_NAMES = ["SND_MESSAGE", "SND_PULSE", "REC_MESSAGE", "REC_PULSE", "SND_PULSE_EXE",
              "SND_PULSE_DIS", "SND_PULSE_DEA", "SND_PULSE_UN", "SND_PULSE_QUN", "SIGNAL",
              "REPLY_MESSAGE", "MSG_ERROR"]
COMM_SMSG, COMM_SPULSE, COMM_RMSG, COMM_RPULSE = 0, 1, 2, 3
COMM_REPLY, COMM_ERROR = 10, 11

TRACECOOKIE = 0xE6E6E6E6

_HDR_BEGIN = b"TRACE_HEADER_BEGIN::"
_HDR_END = b"TRACE_HEADER_END::"
_KEY = re.compile(rb"(TRACE_[A-Z0-9_]+)::")


class KevError(ValueError):
    pass


@dataclass
class Event:
    t: int              # 64-bit ClockCycles value
    cpu: int
    cls: int
    code: int
    words: list = field(default_factory=list)   # payload u32 words, timestamp excluded

    def string_at(self, index):
        """NUL-terminated string packed little-endian into words[index:]."""
        raw = struct.pack(f"<{len(self.words) - index}I", *self.words[index:])
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


@dataclass
class Kev:
    header: dict
    cycles_per_sec: int
    cpus: int
    events: list
    dropped: int = 0          # CONTROL/DROPPED records seen
    partial: int = 0          # combined events cut off by a buffer boundary

    def ns(self, t):
        """Cycles since the first event, in nanoseconds."""
        return (t - self.events[0].t) * 1_000_000_000 // self.cycles_per_sec if self.events else 0


def parse_header(data):
    if not data.startswith(_HDR_BEGIN):
        raise KevError("not a tracelogger file (no TRACE_HEADER_BEGIN)")
    end = data.find(_HDR_END)
    if end < 0:
        raise KevError("truncated header (no TRACE_HEADER_END)")
    body = data[len(_HDR_BEGIN):end]
    header = {}
    marks = list(_KEY.finditer(body))
    for i, m in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        header[m.group(1).decode()] = body[m.end():stop].decode("utf-8", "replace").strip("\0 ")
    return header, end + len(_HDR_END)


def read(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    header, off = parse_header(data)
    if header.get("TRACE_LITTLE_ENDIAN", "TRUE") != "TRUE":
        raise KevError("big-endian traces are not supported")
    if "16 byte" not in header.get("TRACE_ENCODING", "16 byte"):
        raise KevError(f"unsupported encoding: {header.get('TRACE_ENCODING')}")
    off += int(header.get("TRACE_SYSPAGE_LEN", "0"))
    cps = int(header.get("TRACE_CYCLES_PER_SEC", "0"))
    if cps <= 0:
        raise KevError("missing TRACE_CYCLES_PER_SEC")
    ncpu = int(header.get("TRACE_CPU_NUM", "1"))

    msb = {}        # cpu -> high 32 bits of the clock
    last_lsb = {}   # cpu -> previous low 32 bits, to catch a wrap between TIME events
    pending = {}    # cpu -> Event being assembled from combine records
    events = []
    dropped = partial = 0

    n = (len(data) - off) // 16
    for (h, d0, d1, d2) in struct.iter_unpack("<4I", data[off:off + n * 16]):
        if h == 0:
            continue                        # empty record (padding)
        kind = h >> 30
        cpu = (h >> 24) & 0x3F
        cls = (h >> 10) & 0x1F
        code = h & 0x3FF

        if cls == CONTROL and code == CTL_TIME:
            msb[cpu] = d1
            last_lsb[cpu] = d2
        hi = msb.get(cpu, 0)
        prev = last_lsb.get(cpu)
        if prev is not None and d0 < prev and prev - d0 > 0x80000000:
            hi += 1                         # low word wrapped without a TIME event
            msb[cpu] = hi
        last_lsb[cpu] = d0
        t = (hi << 32) | d0

        if kind == S:
            if cpu in pending:
                pending.pop(cpu)
                partial += 1
            ev = Event(t, cpu, cls, code, [d1, d2])
        elif kind == CB:
            if cpu in pending:
                partial += 1
            pending[cpu] = Event(t, cpu, cls, code, [d1, d2])
            continue
        else:
            ev = pending.get(cpu)
            if ev is None or ev.cls != cls or ev.code != code:
                partial += 1                # continuation without its beginning
                pending.pop(cpu, None)
                continue
            ev.words += (d1, d2)
            if kind == CC:
                continue
            del pending[cpu]

        if ev.cls == CONTROL and ev.code == CTL_DROPPED:
            dropped += 1
        events.append(ev)

    partial += len(pending)
    events.sort(key=lambda e: e.t)          # stable: keeps per-CPU order for equal stamps
    return Kev(header, cps, ncpu, events, dropped, partial)


# kernel call numbers (sys/kercalls.h, QNX 8.0); KER_CALL event code = call | 0x80 on exit | 0x200 wide
KERCALLS = {
    0: "NOP", 1: "TRACE_EVENT", 2: "RING0", 3: "CACHE_CONTROL", 4: "SYS_SRANDOM",
    5: "MSG_REGISTER_EVENT", 6: "MSG_SEND_PULSEPTR", 7: "SYS_CPUPAGE_GET", 8: "SYS_CPUPAGE_SET",
    9: "MSG_PAUSE", 10: "MSG_CURRENT", 11: "MSG_SENDV", 12: "MSG_SENDVNC", 13: "MSG_ERROR",
    14: "MSG_RECEIVEV", 15: "MSG_REPLYV", 16: "MSG_READV", 17: "MSG_WRITEV", 19: "MSG_INFO",
    20: "MSG_SEND_PULSE", 21: "MSG_DELIVER_EVENT", 24: "MSG_RECEIVEPULSEV", 25: "MSG_VERIFY_EVENT",
    26: "SIGNAL_KILL", 27: "SIGNAL_RETURN", 29: "SIGNAL_ACTION", 30: "SIGNAL_PROCMASK",
    31: "SIGNAL_SUSPEND", 32: "SIGNAL_WAITINFO", 33: "SIGNAL_KILL_SIGVAL", 35: "CHANNEL_CREATE",
    36: "CHANNEL_DESTROY", 37: "CHANNEL_CTL", 39: "CONNECT_ATTACH", 40: "CONNECT_DETACH",
    41: "CONNECT_SERVER_INFO", 42: "CONNECT_CLIENT_INFO", 43: "CONNECT_FLAGS", 44: "CONNECT_DUPFD",
    46: "THREAD_CREATE", 47: "THREAD_DESTROY", 49: "THREAD_DETACH", 50: "THREAD_JOIN",
    51: "THREAD_CANCEL", 52: "THREAD_CTL", 53: "THREAD_CTLEXT", 55: "INTERRUPT_ATTACH",
    56: "INTERRUPT_QUERY", 57: "INTERRUPT_DETACH", 58: "INTERRUPT_WAIT", 59: "INTERRUPT_MASK",
    60: "INTERRUPT_UNMASK", 61: "INTERRUPT_UNBLOCK", 62: "INTERRUPT_CTL", 65: "CLOCK_TIME",
    66: "CLOCK_ADJUST", 67: "CLOCK_PERIOD", 68: "CLOCK_ID", 70: "TIMER_CREATE", 71: "TIMER_DESTROY",
    72: "TIMER_SETTIME", 73: "TIMER_INFO", 74: "TIMER_ALARM", 75: "TIMER_TIMEOUT",
    76: "TIMER_DELEGATE", 77: "SYNC_RWLOCK", 78: "SYNC_CREATE", 79: "SYNC_DESTROY",
    80: "SYNC_MUTEX_LOCK", 81: "SYNC_MUTEX_UNLOCK", 82: "SYNC_CONDVAR_WAIT",
    83: "SYNC_CONDVAR_SIGNAL", 84: "SYNC_SEM_POST", 85: "SYNC_SEM_WAIT", 86: "SYNC_CTL",
    87: "SYNC_BARRIER_WAIT", 88: "SCHED_GET", 89: "SCHED_SET", 90: "SCHED_YIELD", 91: "SCHED_INFO",
    92: "SCHED_CTL", 93: "MSG_QUEUE_OPEN", 94: "MSG_QUEUE_SEND", 95: "MSG_QUEUE_RECEIVE",
    96: "MSG_QUEUE_CLOSE", 97: "MSG_QUEUE_CTL", 98: "PIPE_OPEN", 99: "PIPE_CTL", 100: "PIPE_CLOSE",
    104: "SYNC_TYPE_DESTROY", 105: "TEST", 106: "SYS_CUSTOM",
}
KER_EXIT_BIT, KER_WIDE_BIT = 0x80, 0x200

SYSTEM_NAMES = {1: "RESERVED", 2: "PATHMGR_OPEN", 3: "APS_NAME", 4: "APS_BUDGETS", 5: "APS_BNKR",
                6: "MMAP", 7: "MUNMAP", 8: "MAPNAME", 9: "ADDRESS", 10: "FUNC_ENTER",
                11: "FUNC_EXIT", 12: "SLOG", 13: "DEFRAG_START", 14: "RUNSTATE", 15: "POWER",
                16: "IPI", 17: "PAGEWAIT", 18: "TIMER", 19: "DEFRAG_END", 20: "PROFILE",
                22: "APS_PSTATS", 23: "APS_OSTATS", 24: "APS_INFO", 25: "APS_JOIN",
                26: "APS_THREAD", 27: "APS_PROCESS", 28: "SCHED_CONF", 29: "IST_ATTACH",
                30: "IST_DETACH", 31: "PCTRACE", 32: "SEND_IPI"}
INT_NAMES = {7: "INT_DELIVER"}
CONTROL_NAMES = {CTL_TIME: "TIME", CTL_BUFFER: "BUFFER", CTL_BUFFER_END: "BUFFER_END",
                 CTL_DROPPED: "DROPPED"}
PROCESS_NAMES = {PR_CREATE: "PROCCREATE", PR_DESTROY: "PROCDESTROY", PR_CREATE_NAME: "PROCCREATE_NAME",
                 PR_DESTROY_NAME: "PROCDESTROY_NAME", TH_NAME: "PROCTHREAD_NAME"}


def label(ev):
    """(class, event) names as QNX traceprinter prints them, e.g. ("THREAD", "THRUNNING")."""
    c, code = ev.cls, ev.code
    if c == PR_TH:
        if code < STATE_MAX:
            name = STATES[code] if code < len(STATES) else f"STATE_{code}"
            return "THREAD", "TH" + name
        return "PROCESS", PROCESS_NAMES.get(code, f"PR_{code}")
    if c == KER_CALL:
        call = code & 0x7F
        kind = "KER_EXIT" if code & KER_EXIT_BIT else "KER_CALL"
        return kind, f"{KERCALLS.get(call, 'KER_' + str(call))}/{call:02d}"
    if c == COMM:
        return "COMM", COMM_NAMES[code] if code < len(COMM_NAMES) else f"COMM_{code}"
    if c == CONTROL:
        return "CONTROL", CONTROL_NAMES.get(code, f"CONTROL_{code}")
    if c == SYSTEM:
        base = SYSTEM_NAMES.get(code & 0x1FF, f"SYS_{code & 0x1FF}")
        return "SYSTEM", base + ("_64" if code & 0x200 else "")
    if c == INT:
        return "INTERRUPT", INT_NAMES.get(code, f"INT_{code}")
    return CLASS_NAMES[c] if c < len(CLASS_NAMES) else f"CLASS_{c}", f"EVENT_{code}"
