"""Compare captured behavior across runs, without treating new PIDs or lock addresses as regressions."""
from collections import defaultdict

from . import report


def _dependencies(mdl, summary):
    roles = {th.label: (th.process.rsplit("/", 1)[-1] or f"pid {th.pid}", th.name or f"tid {th.tid}")
             for th in mdl.threads.values()}
    grouped = defaultdict(list)
    for kind, entries in (("message", summary["channels"]), ("mutex", summary["mutexes"])):
        for entry in entries:
            waiter = entry["client"] if kind == "message" else entry["waiter"]
            owners = [entry["server"]] if kind == "message" else entry["owners"]
            if not owners:
                continue
            owner_roles = tuple(sorted({roles[owner] for owner in owners}))
            key = (kind, roles[waiter], owner_roles)
            count = entry["messages"] if kind == "message" else entry["waits"]
            grouped[key].append({
                "waits": count, "affected": entry["inversions"],
                "affected_pct": 100 * entry["inversions"] / count if count else 0,
                "p50_ms": entry["p50_ms"], "p99_ms": entry["p99_ms"], "max_ms": entry["max_ms"],
                "protocol": entry.get("protocol"),
                "queue_ms": entry.get("queue_ms"), "service_ms": entry.get("service_ms"),
                "cross_process": entry.get("cross_process", False),
                "client_cpus": entry.get("client_cpus"), "server_cpus": entry.get("server_cpus"),
            })
    return grouped


def compare(before, after, process=None):
    """Matched dependencies retain their measured sample sizes; absent work is never called fixed."""
    baseline, candidate = report.summary(before, process), report.summary(after, process)
    old, new = _dependencies(before, baseline), _dependencies(after, candidate)
    rows, warnings = [], []
    for key in sorted(set(old) | set(new)):
        kind, waiter, owners = key
        label = f"{waiter[1]} ({waiter[0]}) -> " + ", ".join(f"{name} ({proc})" for proc, name in owners)
        a, b = old.get(key, []), new.get(key, [])
        row = {"kind": kind, "dependency": label, "baseline": a[0] if len(a) == 1 else None,
               "candidate": b[0] if len(b) == 1 else None, "change": "unchanged"}
        if len(a) > 1 or len(b) > 1:
            row["state"] = "ambiguous"
            warnings.append(f"Multiple {kind} dependencies share the role names {label}; they were not paired.")
        elif not a:
            row["state"] = "new"
            row["change"] = "new_finding" if b[0]["affected"] else "unchanged"
            warnings.append(f"No baseline sample for {label}; no before/after conclusion is available.")
        elif not b:
            row["state"] = "not_observed"
            warnings.append(f"{label} was not observed in the candidate. Missing work does not establish a fix.")
        else:
            row["state"] = "matched"
            # Compare fractions exactly; unequal capture lengths alone must not trigger a regression.
            difference = b[0]["affected"] * a[0]["waits"] - a[0]["affected"] * b[0]["waits"]
            row["change"] = "regressed" if difference > 0 else "improved" if difference < 0 else "unchanged"
            row["max_wait_delta_ms"] = round(b[0]["max_ms"] - a[0]["max_ms"], 3)
            row["p99_delta_ms"] = round(b[0]["p99_ms"] - a[0]["p99_ms"], 3)
        rows.append(row)
    regressions = sum(row["change"] in ("regressed", "new_finding") for row in rows)
    improvements = sum(row["change"] == "improved" for row in rows)
    matched = sum(row["state"] == "matched" for row in rows)
    if not matched:
        warnings.append("No unambiguous dependency was captured in both traces; comparison is inconclusive.")
    limited = warnings or baseline["analysis"]["status"] != "usable" or candidate["analysis"]["status"] != "usable"
    return {
        "baseline": baseline, "candidate": candidate, "groups": rows, "warnings": warnings,
        "regressions": regressions, "improvements": improvements, "matched": matched,
        "status": "limited" if limited else "comparable",
        "direction": "regressed" if regressions else "improved" if improvements else "unchanged" if matched else "inconclusive",
        "method": "Match by process basename and thread name (tid fallback). PIDs, mutex addresses and protocols may change; ambiguous roles are excluded.",
        "scope": "Measured samples of the same workload, not a statistical guarantee. Regression checks use the affected-wait fraction; latency changes are reported separately.",
    }


def render(result, baseline_name="baseline", candidate_name="candidate"):
    out = [f"kevscope compare: {baseline_name} -> {candidate_name}",
           f"  {result['matched']} matched dependencies, {result['improvements']} improvements, "
           f"{result['regressions']} regressions/new findings", ""]
    for row in result["groups"]:
        out.append(f"{row['kind'].upper()}  {row['dependency']} [{row['state']}; {row['change']}]")
        a, b = row["baseline"], row["candidate"]
        if row["state"] == "matched":
            out.append(f"  affected waits: {a['affected']}/{a['waits']} ({a['affected_pct']:.1f}%)"
                       f" -> {b['affected']}/{b['waits']} ({b['affected_pct']:.1f}%)")
            out.append(f"  p99 wait: {a['p99_ms']:.3f} -> {b['p99_ms']:.3f} ms; "
                       f"max: {a['max_ms']:.3f} -> {b['max_ms']:.3f} ms")
            if a["protocol"] or b["protocol"]:
                out.append(f"  protocol: {a['protocol']} -> {b['protocol']}")
        else:
            out.append("  No paired before/after conclusion.")
        out.append("")
    for name, summary in ((baseline_name, result["baseline"]), (candidate_name, result["candidate"])):
        out.extend(f"  warning ({name}): {w['message']}" for w in summary["analysis"]["warnings"])
    out.extend(f"  warning: {warning}" for warning in result["warnings"])
    out.append(result["scope"])
    return "\n".join(out)
