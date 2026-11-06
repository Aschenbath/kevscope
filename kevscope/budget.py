"""Optional limits on observed dependency waits, independent of inversion attribution."""
import math


def evaluate(measurements, max_wait_ms=None, p99_wait_ms=None):
    limits = {name: value for name, value in (("max", max_wait_ms), ("p99", p99_wait_ms))
              if value is not None}
    if any(not math.isfinite(value) or value <= 0 for value in limits.values()):
        raise ValueError("wait budgets must be finite positive milliseconds")
    rows = [{**row, "exceeded": [name for name, limit in limits.items()
                                if row[name + "_ms"] > limit]}
            for row in measurements]
    violations = sum(len(row["exceeded"]) for row in rows)
    return {
        "limits_ms": limits,
        "status": "no_samples" if not rows else "exceeded" if violations else "within_sample",
        "dependencies_checked": len(rows),
        "waits_checked": sum(row["waits"] for row in rows),
        "violations": violations,
        "measurements": rows,
        "scope": "Limits apply separately to each dependency's completed, reportable waits, "
                 "using durations before display rounding. Incomplete waits, messages without a server "
                 "identity, and mutex waits without an address are excluded. Review coverage; "
                 "this sample does not prove application deadlines.",
    }


def render(result, candidate=False):
    limits = ", ".join(f"{name} <= {value:.9g} ms" for name, value in result["limits_ms"].items())
    title = "Candidate wait budget" if candidate else "Wait budget"
    out = [f"{title}: {result['status']} ({limits})",
           f"  {result['waits_checked']} completed waits across {result['dependencies_checked']} dependencies"]
    for row in result["measurements"]:
        for name in row["exceeded"]:
            out.append(f"  exceeded: {row['dependency']} | {name} {row[name + '_ms']:.9g} ms "
                       f"> {result['limits_ms'][name]:.9g} ms ({row['waits']} waits)")
    if result["status"] == "no_samples":
        out.append("  No completed reportable waits; the budget could not be evaluated.")
    out.append("  " + result["scope"])
    return "\n".join(out)
