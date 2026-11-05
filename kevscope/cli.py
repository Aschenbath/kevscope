"""kevscope command line.

  kevscope convert trace.kev [-o trace.pftrace]   Perfetto trace for ui.perfetto.dev
  kevscope report  trace.kev [--json]             priority inversions (messages, mutexes), IPC, latency
  kevscope dump    trace.kev [-n N]               decoded events, traceprinter style
"""
import argparse
import json
import sys
from pathlib import Path

from . import __version__, kev, model, perfetto, report


def _load(path):
    try:
        return model.build(kev.read(path))
    except (OSError, kev.KevError) as e:
        sys.exit(f"kevscope: {path}: {e}")


def cmd_convert(a):
    mdl = _load(a.trace)
    out = a.output or str(Path(a.trace).with_suffix(".pftrace"))
    inv = perfetto.all_inversions(mdl)
    size = perfetto.write(mdl, out, inv)
    n_mutex = sum(isinstance(i, report.MutexInversion) for i in inv)
    print(f"wrote {out} ({size / 1024:.0f} KiB): {len(mdl.threads)} threads, {len(mdl.switches)} context switches, "
          f"{len(mdl.messages)} messages, {len(mdl.mutex_waits)} mutex waits, "
          f"{len(inv)} priority inversions ({len(inv) - n_mutex} message passing, {n_mutex} mutex)")
    print("open it at https://ui.perfetto.dev (Open trace file)")


def cmd_report(a):
    s = report.summary(_load(a.trace))
    if a.json:
        json.dump(s, sys.stdout, indent=2)
        print()
    else:
        print(report.render(s, top=a.top))
    if a.fail_on_inversion and s["findings"]:
        sys.exit(3)


def cmd_dump(a):
    tr = kev.read(a.trace)
    for ev in tr.events[: a.n or None]:
        cls, name = kev.label(ev)
        words = " ".join(f"0x{w:08x}" for w in ev.words[:6])
        print(f"t:0x{ev.t:x} CPU:{ev.cpu:02d} {cls:<15} :{name:<20} {words}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="kevscope", description="QNX kernel trace (.kev) analysis and Perfetto export")
    p.add_argument("--version", action="version", version=f"kevscope {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="write a Perfetto trace")
    c.add_argument("trace")
    c.add_argument("-o", "--output")
    c.set_defaults(fn=cmd_convert)
    r = sub.add_parser("report", help="find priority inversions (message passing, mutexes) and latency problems")
    r.add_argument("trace")
    r.add_argument("--json", action="store_true")
    r.add_argument("--top", type=int, default=8)
    r.add_argument("--fail-on-inversion", action="store_true", help="exit 3 if an inversion is found (for CI)")
    r.set_defaults(fn=cmd_report)
    d = sub.add_parser("dump", help="print decoded events")
    d.add_argument("trace")
    d.add_argument("-n", type=int, default=50)
    d.set_defaults(fn=cmd_dump)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
