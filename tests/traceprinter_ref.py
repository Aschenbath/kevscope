"""Compare kevscope's decoder with QNX's own traceprinter output for the same .kev file.

    python tests/traceprinter_ref.py trace.kev traceprinter-output.txt

Every event must appear in both, with the same timestamp, CPU, class and event name. For thread,
process, communication and control events the printed numbers must also equal the decoded words.
"""
import gzip
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kevscope import kev  # noqa: E402

HEAD = re.compile(r"^t:0x([0-9a-f]+) CPU:(\d+) (\S+)\s*:(\S+)\s*(.*)$")
FIELD = re.compile(r"([A-Za-z_][A-Za-z_0-9\[\]]*)\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)")
CHECK_FIELDS = {"THREAD", "COMM", "PROCESS"}


def read_reference(path):
    events, cur = [], None
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = HEAD.match(line)
            if m:
                cur = {"t": int(m.group(1), 16), "cpu": int(m.group(2)), "cls": m.group(3),
                       "name": m.group(4), "text": m.group(5)}
                events.append(cur)
            elif cur is not None and line.startswith(" "):
                cur["text"] += " " + line.strip()
    return events


def numbers(text):
    """Printed field values in order, e.g. "pid:12 tid:1" -> [12, 1]."""
    out = []
    for m in FIELD.finditer(text):
        v = m.group(2)
        out.append(int(v, 16) if v.startswith("0x") else int(v))
    return out


def expected_words(r):
    """What the decoded payload words should be, according to the printed text."""
    if r["cls"] == "CONTROL" and r["name"] == "TIME":
        return numbers(r["text"])[:2]                       # msb, lsb
    if r["cls"] == "CONTROL" and r["name"] == "BUFFER":
        return numbers(r["text"])[:2]                       # sequence, num_events
    if r["cls"] == "PROCESS":
        return numbers(r["text"].split("name:", 1)[0])[:2]  # (ppid, pid) or (pid, tid)
    if r["cls"] in CHECK_FIELDS:
        return numbers(r["text"])
    return None


def compare(kev_path, ref_path, verbose=True):
    trace = kev.read(kev_path)
    ref = read_reference(ref_path)
    mine = defaultdict(list)
    for ev in trace.events:
        cls, name = kev.label(ev)
        if cls in ("KER_CALL", "KER_EXIT"):
            name = name.rsplit("/", 1)[1]                    # traceprinter shortens some names
        mine[(ev.t, ev.cpu, cls, name)].append(ev)

    problems, examples, matched, checked = Counter(), [], 0, 0
    for r in ref:
        name = r["name"]
        if r["cls"] in ("KER_CALL", "KER_EXIT"):
            name = name.rsplit("/", 1)[1]
        bucket = mine.get((r["t"], r["cpu"], r["cls"], name))
        if not bucket:
            problems["missing from kevscope"] += 1
            if len(examples) < 10:
                examples.append(("missing", r))
            continue
        ev = bucket.pop(0)
        matched += 1
        want = expected_words(r)
        if want is None or (r["cls"] == "CONTROL" and r["name"] not in ("TIME", "BUFFER")):
            continue
        checked += 1
        got = ev.words[:len(want)]
        if got != want:
            problems[f"fields differ: {r['cls']} {r['name']}"] += 1
            if len(examples) < 10:
                examples.append(("fields", r, [hex(w) for w in ev.words]))
        if r["cls"] == "PROCESS" and "name:" in r["text"]:
            printed = r["text"].split("name:", 1)[1].strip()
            if ev.string_at(2) != printed:
                problems["name differs"] += 1
                if len(examples) < 10:
                    examples.append(("name", printed, ev.string_at(2)))
    extra = sum(len(v) for v in mine.values())
    if extra:
        problems["extra in kevscope"] += extra
    if verbose:
        print(f"{Path(kev_path).name}: traceprinter {len(ref)} events, kevscope {len(trace.events)}, "
              f"matched {matched}, field-checked {checked}, problems {sum(problems.values())}")
        for k, v in problems.items():
            print(f"  {v} x {k}")
        for e in examples:
            print("  e.g.", e)
    return {"reference": len(ref), "decoded": len(trace.events), "matched": matched,
            "checked": checked, "problems": problems}


if __name__ == "__main__":
    result = compare(sys.argv[1], sys.argv[2])
    sys.exit(1 if result["problems"] else 0)
