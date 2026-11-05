"""Indexed scheduling evidence shared by message and mutex analysis.

A READY interval is attributed only when the surrounding executions agree on
one CPU. A migration or a missing boundary is reported as uncertain, not spread
across every CPU on which a thread happened to run during the entire wait.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass

from . import kev


@dataclass
class Interference:
    start: int
    end: int
    cpu: int
    thread: tuple
    owner: tuple
    owner_prio: int
    waiter_prio: int
    running_prio: int


class Schedule:
    def __init__(self, mdl):
        self.mdl = mdl
        self.state_times = {k: [c[0] for c in th.changes] for k, th in mdl.threads.items()}
        self.run_starts = {k: [r[0] for r in th.runs] for k, th in mdl.threads.items()}
        self.per_cpu = defaultdict(list)
        self.uncertain = defaultdict(set)
        for th in mdl.threads.values():
            for start, end, cpu, prio in th.runs:
                if end > start:
                    self.per_cpu[cpu].append((start, end, th.key, prio))
        for runs in self.per_cpu.values():
            runs.sort()
        self.ends = {cpu: [r[1] for r in runs] for cpu, runs in self.per_cpu.items()}

    def spans(self, th, start, end, states=None):
        """State/priority intervals, clipped to the query; never scan its entire history."""
        times = self.state_times[th.key]
        first = max(0, bisect_right(times, start) - 1)
        for i in range(first, len(th.changes)):
            at, state, prio, _ = th.changes[i]
            if at >= end:
                break
            stop = th.changes[i + 1][0] if i + 1 < len(th.changes) else end
            if stop > start and stop > at and (states is None or state in states):
                yield max(at, start), min(stop, end), prio

    def runs(self, th, start, end):
        first = max(0, bisect_right(self.run_starts[th.key], start) - 1)
        for i in range(first, len(th.runs)):
            a, b, cpu, prio = th.runs[i]
            if a >= end:
                break
            if b > start:
                yield max(a, start), min(b, end), cpu, prio

    def ready_cpu(self, owner, start, end):
        starts = self.run_starts[owner.key]
        before = bisect_right(starts, start) - 1
        after = bisect_left(starts, end)
        if before < 0 or after >= len(starts):
            self.uncertain["unbounded_ready"].add((owner.key, start, end))
            return None
        left, right = owner.runs[before], owner.runs[after]
        if left[2] != right[2]:
            self.uncertain["migrating_ready"].add((owner.key, start, end))
            return None
        return left[2]

    def interference(self, owner, waiter, start, end, exclude=()):
        """Return disjoint evidence intervals, with all priorities compared at that instant."""
        evidence = []
        skip = {owner.key, waiter.key, *exclude}
        for a, b, owner_prio in self.spans(owner, start, end, {kev.ST_READY}):
            if owner_prio is None:
                self.uncertain["missing_priority"].add((owner.key, a, b))
                continue
            for wa, wb, waiter_prio in self.spans(waiter, a, b):
                if waiter_prio is None:
                    self.uncertain["missing_priority"].add((waiter.key, wa, wb))
                    continue
                if owner_prio >= waiter_prio:
                    continue
                cpu = self.ready_cpu(owner, a, b)
                if cpu is None:
                    continue
                runs = self.per_cpu[cpu]
                for i in range(bisect_right(self.ends[cpu], wa), len(runs)):
                    rs, re, key, prio = runs[i]
                    if rs >= wb:
                        break
                    if key in skip or self.mdl.threads[key].idle:
                        continue
                    if prio is None:
                        self.uncertain["missing_priority"].add((key, max(rs, wa), min(re, wb)))
                        continue
                    if owner_prio <= prio < waiter_prio:
                        evidence.append(Interference(max(rs, wa), min(re, wb), cpu, key,
                                                     owner.key, owner_prio, waiter_prio, prio))
        return evidence


def culprit_times(evidence):
    totals = defaultdict(int)
    for item in evidence:
        totals[item.thread] += item.end - item.start
    return dict(totals)
