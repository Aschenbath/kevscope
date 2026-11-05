"""Trace integrity and analysis coverage, kept separate from positive findings."""


def assess(mdl, schedule, waiters=None):
    trace = mdl.trace
    warnings = []

    def warn(code, message):
        warnings.append({"code": code, "message": message})

    if trace.dropped:
        warn("dropped_buffers", f"{trace.dropped} dropped-buffer markers; recapture with a larger trace buffer.")
    if trace.partial:
        warn("partial_events", f"{trace.partial} incomplete combined events; some arguments could not be decoded.")
    if trace.trailing_bytes:
        warn("trailing_bytes", f"The final record is truncated ({trace.trailing_bytes} trailing bytes).")

    active = [th for th in mdl.threads.values() if not th.idle and (waiters is None or th.key in waiters)]
    has_runs = any(th.runs for th in active)
    has_priorities = any(p is not None for th in active for _, _, _, p in th.runs)
    if not has_runs:
        warn("no_scheduling", "No application scheduling intervals were captured. Record thread-state events.")
    elif not has_priorities:
        warn("no_priorities", "Scheduling priorities are missing. Recapture with tracelogger -w (wide mode).")

    messages = [m for m in mdl.messages if waiters is None or m.client in waiters]
    completed_messages = sum(m.replied is not None and m.server is not None for m in messages)
    waits = [w for w in mdl.mutex_waits if waiters is None or w.waiter in waiters]
    completed_waits = [w for w in waits if w.end is not None and not w.started_before_trace]
    no_address = sum(w.sync is None for w in completed_waits)
    no_owner = sum(not w.holders for w in completed_waits)
    condvar_missing = sum(w.sync is None and w.via_condvar for w in completed_waits)
    if condvar_missing:
        warn("missing_condvar_context", f"{condvar_missing} mutex re-acquisitions lack their earlier condition-wait arguments. Start capture before the condition wait, or scope analysis with --process.")
    if no_address > condvar_missing:
        warn("missing_mutex_arguments", f"{no_address - condvar_missing} completed mutex waits lack lock arguments. Capture kernel calls with tracelogger -w.")
    unresolved = sum(w.sync is not None and not w.holders for w in completed_waits)
    if unresolved:
        warn("unresolved_mutex_owner", f"The owner of {unresolved} completed mutex waits could not be resolved.")

    descriptions = {
        "unbounded_ready": "READY intervals lack an execution boundary needed to establish the CPU",
        "migrating_ready": "READY intervals are bracketed by different CPUs; migration time is not known",
        "missing_priority": "scheduling intervals lack a required priority",
        "ownership_changed": "mutex waits have different observed owners; the handoff time is not known",
    }
    for code, intervals in sorted(schedule.uncertain.items()):
        if intervals:
            warn(code, f"{len(intervals)} {descriptions[code]}. These intervals are excluded from attribution.")
    status = "usable" if not warnings else "limited"
    if not has_runs or not has_priorities:
        status = "insufficient"
    return {
        "status": status,
        "warnings": warnings,
        "coverage": {
            "messages_observed": len(messages), "messages_completed": completed_messages,
            "messages_unfinished": len(messages) - completed_messages,
            "mutex_waits_observed": len(waits), "mutex_waits_completed": len(completed_waits),
            "mutex_waits_unfinished": len(waits) - len(completed_waits),
            "mutex_waits_left_censored": sum(w.started_before_trace for w in waits),
            "mutex_waits_without_address": no_address, "mutex_waits_without_owner": no_owner,
            "uncertain_intervals": {code: len(intervals) for code, intervals in sorted(schedule.uncertain.items()) if intervals},
        },
        "method": "Instantaneous priorities; READY intervals bracketed by executions on the same CPU.",
        "scope": "Completed dependencies with known evidence. Unfinished boundary waits are counted, not analyzed; a usable trace does not prove absence of all inversions.",
    }
