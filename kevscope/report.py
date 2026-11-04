"""Find real-time problems in a trace: priority inversion across message passing, wake-up latency."""
from collections import defaultdict
from dataclasses import dataclass, field

from . import kev, model as M


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
    server_prio: int              # priority the server actually ran at
    client_prio: int
    culprits: dict                # thread key -> cycles it ran while the server was ready
    stolen: int                   # sum of culprit time


@dataclass
class Channel:
    """All messages between one client thread and one server thread."""
    client: tuple
    server: tuple
    waits: list = field(default_factory=list)
    inversions: list = field(default_factory=list)
    server_prios: set = field(default_factory=set)
    client_prios: set = field(default_factory=set)


def analyze(mdl):
    channels = {}
    for msg in mdl.messages:
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
        served = [(s, e, cpu, p) for s, e, cpu, p in server.runs if e > msg.sent and s < msg.replied]
        server_prio = max((p or 0) for *_, p in served) if served else 0
        ch.server_prios.update((p or 0) for *_, p in served)
        server_cpus = {cpu for _, _, cpu, _ in served}

        culprits = defaultdict(int)
        for a, b, _ in state_spans(server, {kev.ST_READY}, msg.sent, msg.replied):
            for th in mdl.threads.values():
                if th.idle or th.key in (msg.client, msg.server):
                    continue
                for s, e, cpu, p in th.runs:
                    if e <= a:
                        continue
                    if s >= b:
                        break
                    if (not server_cpus or cpu in server_cpus) and (p or 0) < client_prio:
                        culprits[th.key] += min(e, b) - max(s, a)
        stolen = sum(culprits.values())
        if stolen > 0 and server_prio < client_prio:
            ch.inversions.append(Inversion(msg, wait, server_prio, client_prio, dict(culprits), stolen))
    return channels


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


def summary(mdl):
    """Everything the text report and --json need, in plain data."""
    ms = mdl.ms
    chans = analyze(mdl)
    lat = wake_latency(mdl)
    findings = []
    for ch in sorted(chans.values(), key=lambda c: -sum(i.stolen for i in c.inversions)):
        if not ch.inversions:
            continue
        client, server = mdl.threads[ch.client], mdl.threads[ch.server]
        culprit_tot = defaultdict(int)
        for inv in ch.inversions:
            for k, v in inv.culprits.items():
                culprit_tot[k] += v
        worst = max(ch.inversions, key=lambda i: i.wait)
        findings.append({
            "kind": "priority_inversion",
            "client": client.label, "client_prio": max(ch.client_prios),
            "server": server.label, "server_prios": sorted(ch.server_prios),
            "messages": len(ch.waits), "affected": len(ch.inversions),
            "blocked_ms_total": round(ms(sum(i.wait for i in ch.inversions)), 3),
            "stolen_ms_total": round(ms(sum(i.stolen for i in ch.inversions)), 3),
            "culprits": [{"thread": mdl.threads[k].label,
                          "prio": max((p or 0) for *_, p in mdl.threads[k].runs) if mdl.threads[k].runs else 0,
                          "ms": round(ms(v), 3)}
                         for k, v in sorted(culprit_tot.items(), key=lambda kv: -kv[1])],
            "worst": {"at_ms": round(ms(worst.message.sent - mdl.start), 3),
                      "blocked_ms": round(ms(worst.wait), 3),
                      "stolen_ms": round(ms(worst.stolen), 3)},
        })
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
        v = [ms(x[0]) for x in vals]
        th = mdl.threads[key]
        latency.append({"thread": th.label, "wakeups": len(v), "p50_ms": round(percentile(v, 50), 3),
                        "p99_ms": round(percentile(v, 99), 3), "max_ms": round(max(v), 3),
                        "prio": max((x[2] or 0) for x in vals)})
    latency.sort(key=lambda r: -r["max_ms"])
    cpu = []
    span = mdl.end - mdl.start or 1
    for th in mdl.threads.values():
        busy = sum(e - s for s, e, _, _ in th.runs)
        if busy and not th.idle:
            cpu.append({"thread": th.label, "ms": round(ms(busy), 3), "share": round(busy / span, 4)})
    cpu.sort(key=lambda r: -r["ms"])
    t = mdl.trace
    return {
        "trace": {"duration_ms": round(ms(span), 3), "cpus": t.cpus, "events": len(t.events),
                  "dropped_buffers": t.dropped, "partial_events": t.partial,
                  "system": f"{t.header.get('TRACE_SYSNAME', '')} {t.header.get('TRACE_SYS_RELEASE', '')}".strip(),
                  "machine": t.header.get("TRACE_MACHINE", "")},
        "findings": findings, "channels": channels, "wake_latency": latency, "cpu": cpu,
    }


def render(s, top=8):
    out = []
    tr = s["trace"]
    out.append(f"kevscope report: {tr['system']} {tr['machine']}, {tr['cpus']} CPUs, "
               f"{tr['duration_ms']:.1f} ms, {tr['events']} events")
    if tr["dropped_buffers"] or tr["partial_events"]:
        out.append(f"  warning: {tr['dropped_buffers']} dropped buffers, {tr['partial_events']} cut-off events; "
                   "results may miss some activity")
    out.append("")
    if not s["findings"]:
        out.append("No priority inversion found in message passing.")
    for f in s["findings"]:
        out.append(f"PRIORITY INVERSION  {f['client']} (prio {f['client_prio']}) waited on {f['server']}")
        out.append(f"  {f['affected']} of {f['messages']} messages affected. The server ran at priority "
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
    out.append("Message passing (client -> server, time blocked per MsgSend):")
    for c in s["channels"][:top]:
        flag = f"  <- {c['inversions']} inversions" if c["inversions"] else ""
        out.append(f"  {c['messages']:5d}  p50 {c['p50_ms']:7.3f}  p99 {c['p99_ms']:7.3f}  max {c['max_ms']:7.3f} ms"
                   f"  {c['client']} -> {c['server']}{flag}")
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
