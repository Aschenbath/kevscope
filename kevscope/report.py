"""Find real-time problems in a trace: priority inversion across message passing and mutexes, wake-up latency."""
from collections import defaultdict
from dataclasses import dataclass, field

from . import kev, model as M, quality
from .schedule import Schedule, culprit_times


def state_spans(th, states, a, b):
    """Stretches of [a, b) during which th was in one of `states`."""
    out = []
    ch = th.changes
    for i, (t, st, prio, _) in enumerate(ch):
        stop = ch[i + 1][0] if i + 1 < len(ch) else b
        if st in states and stop > a and t < b:
            out.append((max(t, a), min(stop, b), prio))
    return out


def percentile(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    k = min(len(v) - 1, max(0, int(round(p / 100.0 * (len(v) - 1)))))
    return v[k]


@dataclass
class Inversion:
    """One message whose lower-priority server was kept off the CPU by a thread below the client."""
    message: M.Message
    wait: int                     # cycles the client was blocked
    server_prio: int              # highest READY priority in the attributed intervals
    client_prio: int
    culprits: dict                # thread key -> cycles it ran while the server was ready
    stolen: int                   # sum of culprit time
    evidence: list = field(default_factory=list)


@dataclass
class Channel:
    """All messages between one client thread and one server thread."""
    client: tuple
    server: tuple
    waits: list = field(default_factory=list)
    inversions: list = field(default_factory=list)
    server_prios: set = field(default_factory=set)
    client_prios: set = field(default_factory=set)


def analyze(mdl, schedule=None, waiters=None):
    schedule = schedule or Schedule(mdl)
    channels = {}
    for msg in mdl.messages:
        if waiters is not None and msg.client not in waiters:
            continue
        if msg.server is None or msg.replied is None or msg.server == msg.client:
            continue
        client, server = mdl.threads[msg.client], mdl.threads[msg.server]
        ch = channels.get((msg.client, msg.server))
        if ch is None:
            ch = channels[(msg.client, msg.server)] = Channel(msg.client, msg.server)
        wait = msg.replied - msg.sent
        ch.waits.append(wait)

        _, client_prio = client.state_at(msg.sent)
        client_prio = client_prio or 0
        ch.client_prios.add(client_prio)
        served = list(schedule.runs(server, msg.sent, msg.replied))
        ch.server_prios.update((p or 0) for *_, p in served)
        evidence = schedule.interference(server, client, msg.sent, msg.replied)
        culprits = culprit_times(evidence)
        stolen = sum(culprits.values())
        if stolen:
            ch.inversions.append(Inversion(msg, wait, max(x.owner_prio for x in evidence),
                                           max(x.waiter_prio for x in evidence), culprits, stolen, evidence))
    return channels


@dataclass
class MutexInversion:
    """One mutex wait whose lower-priority owner was kept off the CPU by a thread below the waiter."""
    wait: M.MutexWait
    blocked: int                  # cycles the waiter was MUTEX-blocked
    owner_prio: int               # highest READY priority in the attributed intervals
    waiter_prio: int
    culprits: dict                # thread key -> cycles it ran while the owner was ready
    stolen: int
    evidence: list = field(default_factory=list)


@dataclass
class Lock:
    """All waits of one thread for one mutex."""
    waiter: tuple
    sync: int
    holders: set = field(default_factory=set)
    waits: list = field(default_factory=list)
    inversions: list = field(default_factory=list)
    owner_prios: set = field(default_factory=set)
    waiter_prios: set = field(default_factory=set)
    flags: int = None


def _merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def analyze_mutexes(mdl, schedule=None, waiters=None):
    """Mutex waits grouped by (waiter, mutex), with the waits that were priority inversions."""
    schedule = schedule or Schedule(mdl)
    locks = {}
    for w in mdl.mutex_waits:
        if waiters is not None and w.waiter not in waiters:
            continue
        if w.end is None or w.sync is None or w.started_before_trace:
            continue
        waiter = mdl.threads[w.waiter]
        lk = locks.get((w.waiter, w.sync))
        if lk is None:
            lk = locks[(w.waiter, w.sync)] = Lock(w.waiter, w.sync)
        if w.flags is not None:
            lk.flags = w.flags
        lk.waits.append(w.end - w.start)
        _, waiter_prio = waiter.state_at(w.start)
        waiter_prio = waiter_prio or 0
        lk.waiter_prios.add(waiter_prio)
        holders = [mdl.threads[k] for k in w.holders]
        if not holders:
            continue
        lk.holders.update(w.holders)

        prios = []
        for h in holders:
            prios.extend(p for _, _, _, p in schedule.runs(h, w.start, w.end) if p is not None)
        lk.owner_prios.update(prios)
        if len(holders) != 1:
            schedule.uncertain["ownership_changed"].add((w.waiter, w.start, w.end))
            continue
        evidence = schedule.interference(holders[0], waiter, w.start, w.end)
        culprits = culprit_times(evidence)
        stolen = sum(culprits.values())
        if stolen:
            lk.inversions.append(MutexInversion(w, w.end - w.start, max(x.owner_prio for x in evidence),
                                               max(x.waiter_prio for x in evidence), culprits, stolen, evidence))
    return locks


def mutex_cause(protocol, waiter_prio):
    """Likely cause of a mutex inversion, as report lines."""
    if protocol == "PTHREAD_PRIO_NONE":
        return ["Likely cause: the mutex was created with PTHREAD_PRIO_NONE (the trace shows the",
                "_NTO_SYNC_PRIONONE flag), so the owner keeps its own priority while others wait.",
                "Drop that pthread_mutexattr_setprotocol() call: QNX's default is PTHREAD_PRIO_INHERIT."]
    if protocol == "PTHREAD_PRIO_PROTECT":
        return ["Likely cause: the mutex uses a priority ceiling (PTHREAD_PRIO_PROTECT) below the",
                f"waiter's priority {waiter_prio}. Raise the ceiling or use PTHREAD_PRIO_INHERIT."]
    return ["Likely cause: the owner was not raised to the waiter's priority. Check how the mutex",
            "was created (pthread_mutexattr_setprotocol) and whether the owner sets its own priority."]


def wake_latency(mdl):
    """Per thread: cycles from READY (after being blocked) to RUNNING."""
    out = defaultdict(list)
    for th in mdl.threads.values():
        if th.idle:
            continue
        ch = th.changes
        for i in range(1, len(ch)):
            t, st, prio, _ = ch[i]
            if st != kev.ST_READY or ch[i - 1][1] == kev.ST_RUNNING:
                continue
            for j in range(i + 1, len(ch)):
                if ch[j][1] == kev.ST_RUNNING:
                    out[th.key].append((ch[j][0] - t, t, prio))
                    break
                if ch[j][1] != kev.ST_READY:
                    break
    return out


def summary(mdl, process=None):
    """Everything the text report and --json need, in plain data."""
    ms = mdl.ms
    waiters = None
    if process is not None:
        waiters = {th.key for th in mdl.threads.values()
                   if str(th.pid) == process or th.process.rsplit("/", 1)[-1] == process}
        if not waiters:
            raise kev.KevError(f"no process matches {process!r}; use a process basename or PID")
    schedule = Schedule(mdl)
    chans = analyze(mdl, schedule, waiters)
    locks = analyze_mutexes(mdl, schedule, waiters)
    lat = wake_latency(mdl)
    findings = []

    def culprit_rows(inversions):
        totals, priorities = defaultdict(int), defaultdict(set)
        for inv in inversions:
            for item in inv.evidence:
                totals[item.thread] += item.end - item.start
                priorities[item.thread].add(item.running_prio)
        return [{"thread": mdl.threads[key].label, "prio": max(priorities[key]),
                 "ms": round(ms(value), 3)} for key, value in sorted(totals.items(), key=lambda item: -item[1])]

    def evidence_rows(inv):
        return [{"start_ms": round(ms(x.start - mdl.start), 6),
                 "end_ms": round(ms(x.end - mdl.start), 6),
                 "cpu": x.cpu, "thread": mdl.threads[x.thread].label,
                 "owner": mdl.threads[x.owner].label, "owner_prio": x.owner_prio,
                 "waiter_prio": x.waiter_prio, "running_prio": x.running_prio}
                for x in inv.evidence]
    for ch in sorted(chans.values(), key=lambda c: -sum(i.stolen for i in c.inversions)):
        if not ch.inversions:
            continue
        client, server = mdl.threads[ch.client], mdl.threads[ch.server]
        worst = max(ch.inversions, key=lambda i: i.wait)
        findings.append({
            "kind": "priority_inversion",
            "client": client.label, "client_prio": max(i.client_prio for i in ch.inversions),
            "server": server.label, "server_prios": sorted({i.server_prio for i in ch.inversions}),
            "messages": len(ch.waits), "affected": len(ch.inversions),
            "blocked_ms_total": round(ms(sum(i.wait for i in ch.inversions)), 3),
            "stolen_ms_total": round(ms(sum(i.stolen for i in ch.inversions)), 3),
            "culprits": culprit_rows(ch.inversions),
            "worst": {"at_ms": round(ms(worst.message.sent - mdl.start), 3),
                      "blocked_ms": round(ms(worst.wait), 3),
                      "stolen_ms": round(ms(worst.stolen), 3), "evidence": evidence_rows(worst)},
        })
    for lk in sorted(locks.values(), key=lambda x: -sum(i.stolen for i in x.inversions)):
        if not lk.inversions:
            continue
        waiter = mdl.threads[lk.waiter]
        worst = max(lk.inversions, key=lambda i: i.blocked)
        findings.append({
            "kind": "mutex_priority_inversion",
            "waiter": waiter.label, "waiter_prio": max(i.waiter_prio for i in lk.inversions),
            "owners": [mdl.threads[k].label for k in sorted(lk.holders)],
            "owner_prios": sorted({i.owner_prio for i in lk.inversions}),
            "mutex": f"0x{lk.sync:x}", "protocol": M.mutex_protocol(lk.flags),
            "waits": len(lk.waits), "affected": len(lk.inversions),
            "blocked_ms_total": round(ms(sum(i.blocked for i in lk.inversions)), 3),
            "stolen_ms_total": round(ms(sum(i.stolen for i in lk.inversions)), 3),
            "culprits": culprit_rows(lk.inversions),
            "worst": {"at_ms": round(ms(worst.wait.start - mdl.start), 3),
                      "blocked_ms": round(ms(worst.blocked), 3),
                      "stolen_ms": round(ms(worst.stolen), 3), "evidence": evidence_rows(worst)},
        })
    mutexes = []
    for lk in sorted(locks.values(), key=lambda x: -max(x.waits)):
        w = [ms(x) for x in lk.waits]
        mutexes.append({"waiter": mdl.threads[lk.waiter].label,
                        "owners": [mdl.threads[k].label for k in sorted(lk.holders)],
                        "mutex": f"0x{lk.sync:x}", "protocol": M.mutex_protocol(lk.flags),
                        "waits": len(w), "p50_ms": round(percentile(w, 50), 3),
                        "p99_ms": round(percentile(w, 99), 3), "max_ms": round(max(w), 3),
                        "waiter_prio": max(lk.waiter_prios), "owner_prios": sorted(lk.owner_prios),
                        "inversions": len(lk.inversions)})
    channels = []
    for ch in sorted(chans.values(), key=lambda c: -len(c.waits)):
        w = [ms(x) for x in ch.waits]
        channels.append({"client": mdl.threads[ch.client].label, "server": mdl.threads[ch.server].label,
                         "messages": len(w), "p50_ms": round(percentile(w, 50), 3),
                         "p99_ms": round(percentile(w, 99), 3), "max_ms": round(max(w), 3),
                         "client_prio": max(ch.client_prios), "server_prios": sorted(ch.server_prios),
                         "inversions": len(ch.inversions)})
    latency = []
    for key, vals in lat.items():
        if waiters is not None and key not in waiters:
            continue
        v = [ms(x[0]) for x in vals]
        th = mdl.threads[key]
        latency.append({"thread": th.label, "wakeups": len(v), "p50_ms": round(percentile(v, 50), 3),
                        "p99_ms": round(percentile(v, 99), 3), "max_ms": round(max(v), 3),
                        "prio": max((x[2] or 0) for x in vals)})
    latency.sort(key=lambda r: -r["max_ms"])
    cpu = []
    span = mdl.end - mdl.start
    for th in mdl.threads.values():
        if waiters is not None and th.key not in waiters:
            continue
        busy = sum(e - s for s, e, _, _ in th.runs)
        if busy and not th.idle:
            cpu.append({"thread": th.label, "ms": round(ms(busy), 3), "share": round(busy / (span or 1), 4)})
    cpu.sort(key=lambda r: -r["ms"])
    t = mdl.trace
    return {
        "trace": {"duration_ms": round(ms(span), 3), "cpus": t.cpus, "events": len(t.events),
                  "dropped_buffers": t.dropped, "partial_events": t.partial,
                  "trailing_bytes": t.trailing_bytes,
                  "system": f"{t.header.get('TRACE_SYSNAME', '')} {t.header.get('TRACE_SYS_RELEASE', '')}".strip(),
                  "machine": t.header.get("TRACE_MACHINE", "")},
        "analysis": {**quality.assess(mdl, schedule, waiters), "process": process},
        "findings": findings, "channels": channels, "mutexes": mutexes, "wake_latency": latency, "cpu": cpu,
    }


def render(s, top=8):
    out = []
    tr = s["trace"]
    out.append(f"kevscope report: {tr['system']} {tr['machine']}, {tr['cpus']} CPUs, "
               f"{tr['duration_ms']:.1f} ms, {tr['events']} events")
    analysis = s.get("analysis", {})
    for warning in analysis.get("warnings", []):
        out.append(f"  warning: {warning['message']}")
    if analysis:
        coverage = analysis["coverage"]
        out.append(f"  coverage: {coverage['messages_completed']}/{coverage['messages_observed']} messages, "
                   f"{coverage['mutex_waits_completed']}/{coverage['mutex_waits_observed']} mutex waits completed")
    out.append("")
    if not s["findings"]:
        if analysis.get("status", "usable") == "usable":
            out.append("No priority inversion found in the analyzed message and mutex waits.")
        else:
            out.append("No priority inversion established. Review the evidence warnings before drawing a conclusion.")
        out.append("")
    for f in s["findings"]:
        if f["kind"] == "mutex_priority_inversion":
            out += _render_mutex(f)
            continue
        out.append(f"PRIORITY INVERSION  {f['client']} (prio {f['client_prio']}) waited on {f['server']}")
        out.append(f"  {f['affected']} of {f['messages']} messages affected. While READY, the server had priority "
                   f"{', '.join(map(str, f['server_prios']))}, below its priority {f['client_prio']} client,")
        out.append(f"  so lower-priority work ran first: {f['stolen_ms_total']:.1f} ms in total, "
                   f"while the client was blocked {f['blocked_ms_total']:.1f} ms.")
        for c in f["culprits"][:3]:
            out.append(f"    {c['thread']} (prio {c['prio']}): {c['ms']:.1f} ms")
        w = f["worst"]
        out.append(f"  Worst: at +{w['at_ms']:.3f} ms the client was blocked {w['blocked_ms']:.2f} ms, "
                   f"{w['stolen_ms']:.2f} ms of it behind lower-priority threads.")
        out.append("  Likely cause: the server does not inherit its client's priority. Check for a channel")
        out.append("  created with _NTO_CHF_FIXED_PRIORITY, or a server that sets its own priority.")
        out.append("")
    if s["channels"]:
        out.append("Message passing (client -> server, time blocked per MsgSend):")
    for c in s["channels"][:top]:
        flag = f"  <- {c['inversions']} inversions" if c["inversions"] else ""
        out.append(f"  {c['messages']:5d}  p50 {c['p50_ms']:7.3f}  p99 {c['p99_ms']:7.3f}  max {c['max_ms']:7.3f} ms"
                   f"  {c['client']} -> {c['server']}{flag}")
    if s["mutexes"]:
        if s["channels"]:
            out.append("")
        out.append("Mutex waits (waiter -> owner, time MUTEX-blocked per wait):")
        for x in s["mutexes"][:top]:
            flag = f"  <- {x['inversions']} inversions" if x["inversions"] else ""
            owner = ", ".join(x["owners"]) or "owner unknown"
            out.append(f"  {x['waits']:5d}  p50 {x['p50_ms']:7.3f}  p99 {x['p99_ms']:7.3f}  max {x['max_ms']:7.3f} ms"
                       f"  {x['waiter']} -> {owner}{flag}")
    out.append("")
    out.append("Wake-up latency (ready -> running), worst first:")
    for r in s["wake_latency"][:top]:
        out.append(f"  {r['wakeups']:5d}  p50 {r['p50_ms']:7.3f}  p99 {r['p99_ms']:7.3f}  max {r['max_ms']:7.3f} ms"
                   f"  prio {r['prio']:3d}  {r['thread']}")
    out.append("")
    out.append("CPU time:")
    for r in s["cpu"][:top]:
        out.append(f"  {r['ms']:9.1f} ms  {100 * r['share']:5.1f}%  {r['thread']}")
    return "\n".join(out)


def _render_mutex(f):
    owners = " / ".join(f["owners"])
    out = [f"MUTEX PRIORITY INVERSION  {f['waiter']} (prio {f['waiter_prio']}) waited for a mutex held by {owners}",
           f"  {f['affected']} of {f['waits']} waits affected. While READY, the owner had priority "
           f"{', '.join(map(str, f['owner_prios']))}, below its priority {f['waiter_prio']} waiter,",
           f"  so lower-priority work ran first: {f['stolen_ms_total']:.1f} ms in total, "
           f"while the waiter was blocked {f['blocked_ms_total']:.1f} ms."]
    for c in f["culprits"][:3]:
        out.append(f"    {c['thread']} (prio {c['prio']}): {c['ms']:.1f} ms")
    w = f["worst"]
    out.append(f"  Worst: at +{w['at_ms']:.3f} ms the waiter was blocked {w['blocked_ms']:.2f} ms, "
               f"{w['stolen_ms']:.2f} ms of it behind lower-priority threads.")
    out.append(f"  Mutex {f['mutex']}, protocol {f['protocol']}.")
    out += ["  " + line for line in mutex_cause(f["protocol"], f["waiter_prio"])]
    out.append("")
    return out
