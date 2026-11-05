"""kevscope command line.

  kevscope convert trace.kev [-o trace.pftrace]   Perfetto trace for ui.perfetto.dev
  kevscope report  trace.kev [--json]             priority inversions (messages, mutexes), IPC, latency
  kevscope compare before.kev after.kev          matched before/after evidence
  kevscope dump    trace.kev [-n N]               decoded events, traceprinter style
"""
import argparse
import json
import sys
from pathlib import Path

from . import __version__, comparison, html_report, kev, model, perfetto, report


def _load(path):
    try:
        return model.build(kev.read(path))
    except (OSError, kev.KevError) as e:
        sys.exit(f"kevscope: {path}: {e}")


def cmd_convert(a):
    mdl = _load(a.trace)
    out = a.output or str(Path(a.trace).with_suffix(".pftrace"))
    _check_output(out, a.trace)
    inv = perfetto.all_inversions(mdl)
    size = perfetto.write(mdl, out, inv)
    n_mutex = sum(isinstance(i, report.MutexInversion) for i in inv)
    print(f"wrote {out} ({size / 1024:.0f} KiB): {len(mdl.threads)} threads, {len(mdl.switches)} context switches, "
          f"{len(mdl.messages)} messages, {len(mdl.mutex_waits)} mutex waits, "
          f"{len(inv)} priority inversions ({len(inv) - n_mutex} message passing, {n_mutex} mutex)")
    print("open it at https://ui.perfetto.dev (Open trace file)")


def cmd_report(a):
    s = report.summary(_load(a.trace), a.process)
    if a.html:
        _check_output(a.html, a.trace)
        Path(a.html).write_text(html_report.render(s, Path(a.trace).name), encoding="utf-8")
        print(f"wrote {a.html}; open it in a browser (works offline)")
    elif a.json:
        json.dump(s, sys.stdout, indent=2)
        print()
    else:
        print(report.render(s, top=a.top))
    if a.strict and s["analysis"]["status"] != "usable":
        sys.exit(4)
    if a.fail_on_inversion and s["findings"]:
        sys.exit(3)


def cmd_compare(a):
    result = comparison.compare(_load(a.baseline), _load(a.candidate), a.process)
    names = Path(a.baseline).name, Path(a.candidate).name
    if a.html:
        _check_output(a.html, a.baseline, a.candidate)
        Path(a.html).write_text(html_report.render_comparison(result, *names), encoding="utf-8")
        print(f"wrote {a.html}; open it in a browser (works offline)")
    elif a.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print(comparison.render(result, *names))
    if a.strict and result["status"] != "comparable":
        sys.exit(4)
    if a.fail_on_regression and result["regressions"]:
        sys.exit(3)


def _check_output(output, *inputs):
    target = Path(output).resolve()
    if any(target == Path(source).resolve() or (target.exists() and target.samefile(source)) for source in inputs):
        raise kev.KevError("output would overwrite an input trace; choose another path")


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _formats(parser):
    parser.add_argument("--process", metavar="NAME_OR_PID", help="analyze waits of this process; keep other threads as scheduling evidence")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--html", metavar="FILE", help="write a self-contained offline HTML report")
    parser.add_argument("--strict", action="store_true", help="exit 4 if evidence or comparison coverage is limited (takes precedence over exit 3)")


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
    _formats(r)
    r.add_argument("--top", type=_positive_int, default=8)
    r.add_argument("--fail-on-inversion", action="store_true", help="exit 3 if an inversion is found (for CI)")
    r.set_defaults(fn=cmd_report)
    diff = sub.add_parser("compare", help="compare completed dependencies across two captures")
    diff.add_argument("baseline")
    diff.add_argument("candidate")
    _formats(diff)
    diff.add_argument("--fail-on-regression", action="store_true", help="exit 3 for a higher affected-wait fraction or a new dependency with an inversion")
    diff.set_defaults(fn=cmd_compare)
    d = sub.add_parser("dump", help="print decoded events")
    d.add_argument("trace")
    d.add_argument("-n", type=_positive_int, default=50)
    d.set_defaults(fn=cmd_dump)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (OSError, kev.KevError) as exc:
        p.exit(1, f"kevscope: {exc}\n")


if __name__ == "__main__":
    main()
