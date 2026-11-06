"""Self-contained, offline HTML reports. All trace-derived text is escaped."""
from html import escape

from . import __version__, report


CSS = """
:root{--paper:#f4f3ed;--ink:#182c2a;--muted:#526862;--line:#d4dcd5;--green:#086e59;--red:#bc432d;--amber:#8d5e18;--mono:Consolas,'Liberation Mono',monospace}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 'Trebuchet MS',Verdana,sans-serif}
a{color:var(--green)}button,summary{cursor:pointer}button:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #bb6633;outline-offset:4px}
.wrap{max-width:1220px;margin:auto;padding:0 42px}nav{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding:23px 0;gap:20px}
.brand{font:bold 29px Georgia,serif;letter-spacing:-1.5px}.brand i{color:var(--green);font-style:normal}.mono,.eyebrow,th,.pill,.metric strong{font-family:var(--mono)}
.eyebrow{text-transform:uppercase;letter-spacing:.13em;font-size:11px;color:var(--muted)}.nav-note{font-size:12px;text-align:right;color:var(--muted)}
.hero{display:grid;grid-template-columns:1.45fr 1fr;gap:60px;padding:48px 0 36px;align-items:end}h1{font-size:clamp(36px,4.6vw,61px);letter-spacing:-2.6px;line-height:1.07;margin:16px 0 20px;font-weight:500}h1 em{font-family:Georgia,serif;color:var(--green);font-weight:normal}
.hero p{color:var(--muted);max-width:590px;margin:0}.hero-side{border-left:2px solid var(--ink);padding-left:24px}.source{font-family:var(--mono);font-size:13px;overflow-wrap:anywhere;margin:8px 0 15px}.pill{display:inline-block;border:1px solid currentColor;border-radius:3px;padding:4px 9px;font-size:10px;text-transform:uppercase;letter-spacing:.06em}
.good{color:var(--green)}.bad{color:var(--red)}.caution{color:var(--amber)}.muted{color:var(--muted)}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);border-top:2px solid var(--ink);border-bottom:1px solid var(--line);margin-bottom:40px}.metric{padding:20px 24px 22px 0;min-width:0}.metric+.metric{padding-left:24px;border-left:1px solid var(--line)}
.metric strong{display:block;font-size:clamp(25px,3.3vw,44px);font-weight:400;line-height:1.3;letter-spacing:-2px;margin:8px 0}.metric small{display:block;color:var(--muted);font-size:12px}.arrow{font-size:.7em;color:#81918a;margin:0 8px}
section{margin:38px 0}h2{font-size:24px;font-weight:500;letter-spacing:-.6px;margin:0 0 6px}h3{font-size:17px;margin:0}p{overflow-wrap:anywhere}.section-head{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:20px}.section-head p{margin:0;color:var(--muted);font-size:13px}
.filters{display:flex;gap:6px;flex-wrap:wrap}.filters button{background:transparent;border:1px solid var(--line);border-radius:3px;padding:8px 12px;font:12px var(--mono);color:var(--ink)}.filters button[aria-pressed=true]{background:var(--ink);border-color:var(--ink);color:white}
.notice{border-left:3px solid var(--amber);background:#ece8d9;padding:18px 22px;margin:22px 0;font-size:13px}.notice ul{margin:9px 0 0;padding-left:20px}.notice strong{font-size:14px}.coverage{font:12px/1.7 var(--mono);color:var(--muted);margin-top:12px}.scope{font-size:12px;color:var(--muted);margin-top:12px}
.finding{border:1px solid var(--line);background:#fcfcf8;margin:12px 0}.finding>summary{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:20px 24px;list-style:none}.finding>summary::-webkit-details-marker{display:none}.finding>summary:after{content:'+';font:25px var(--mono)}.finding[open]>summary:after{content:'−'}.finding>summary>div{flex:1;min-width:0}.finding .kind{font:10px var(--mono);letter-spacing:.13em;text-transform:uppercase;color:var(--red);margin-bottom:5px}.finding .detail{border-top:1px solid var(--line);padding:24px}.finding .detail>p{font-size:14px;margin-top:0}
.chain{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:20px 0 24px}.role{border-left:3px solid var(--green);padding:5px 14px;background:#edf2eb;font-size:13px;overflow-wrap:anywhere;max-width:100%}.role.ran{border-color:var(--red);background:#f7eae3}.role small{display:block;font:10px var(--mono);color:var(--muted);margin-bottom:3px}.chain>span{font-size:11px;color:var(--muted)}
.timeline-scroll{overflow-x:auto}.timeline{width:100%;height:auto;display:block;margin:20px 0 4px;background:#f4f5ef}.timeline text{font:11px var(--mono);fill:var(--muted)}.timeline .wait{fill:#bdccc3}.timeline .ready{fill:#d9b575}.timeline .run{fill:var(--red)}
.evidence-table{overflow-x:auto}.evidence-table summary{font-size:12px;color:var(--green);padding:8px 0}table{border-collapse:collapse;width:100%;font-size:13px}th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:normal}td,th{padding:12px 14px 12px 0;border-bottom:1px solid var(--line);vertical-align:top}td:last-child,th:last-child{padding-right:0}.number{font-family:var(--mono);white-space:nowrap}td small{display:block;color:var(--muted);font-size:11px}.comparison{overflow-x:auto}.comparison table{min-width:700px}.dependency{font-weight:bold;min-width:180px;max-width:330px;overflow-wrap:anywhere}.state{font-size:10px;font-family:var(--mono);text-transform:uppercase;white-space:nowrap}.bar{display:block;height:4px;background:#e0e4dd;margin-top:7px;min-width:80px}.bar i{display:block;height:100%;background:var(--green)}.bar.before i{background:#a68359}.empty{padding:24px;border:1px dashed #aebeb2;color:var(--muted);font-size:14px}.hint{padding:14px 18px;background:#edf2eb;color:#2e5146;font-size:13px}.two{display:grid;grid-template-columns:1fr 1fr;gap:36px}.compact td{font-size:12px}.compact td:first-child{overflow-wrap:anywhere}footer{border-top:1px solid var(--line);padding:24px 0 35px;display:flex;justify-content:space-between;gap:24px;font-size:11px;color:var(--muted)}[hidden]{display:none!important}
.two>div{min-width:0}
@media(max-width:760px){.wrap{padding:0 20px}.hero{grid-template-columns:1fr;gap:24px;padding-top:30px}.hero-side{border-left:0;border-top:1px solid var(--line);padding:18px 0 0}h1{letter-spacing:-1.5px}.metrics{grid-template-columns:1fr}.metric{padding:14px 0!important;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:0 14px}.metric+.metric{border-left:0;border-top:1px solid var(--line)}.metric strong{grid-column:2;grid-row:1/3;letter-spacing:-1px;font-size:30px}.metric small{grid-column:1;font-size:10px}.metrics .eyebrow{font-size:10px;letter-spacing:.03em}.section-head{align-items:start;flex-direction:column}.two{grid-template-columns:1fr;gap:12px}.finding>summary{padding:18px 16px}.finding .detail{padding:16px}.chain{gap:9px}.nav-note{max-width:140px}.timeline{min-width:650px}footer{flex-direction:column;gap:8px}}
@media print{body{background:white}.wrap{padding:0}.filters{display:none}.hero{padding-top:20px}h1{font-size:38px}.finding{break-inside:avoid}.comparison{overflow:visible}.comparison table{min-width:0}.notice{background:white}.two{display:block}footer{padding-bottom:0}}
"""

SCRIPT = """
document.querySelectorAll('[data-filter]').forEach(button => {
  button.addEventListener('click', () => {
    const filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
    document.querySelectorAll('[data-kind]').forEach(row => { row.hidden = filter !== 'all' && row.dataset.kind !== filter; });
    const visible = [...document.querySelectorAll('[data-kind]')].filter(row => !row.hidden).length;
    const status = document.getElementById('filter-status');
    if (status) status.textContent = visible + ' visible item' + (visible === 1 ? '' : 's');
  });
});
"""


def e(value):
    return escape(str(value), quote=True)


def _document(title, body, extra_css=""):
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='5' fill='%23086e59'/%3E%3Cpath d='M4 18h5l3-9 5 16 3-10h8' fill='none' stroke='white' stroke-width='2'/%3E%3C/svg%3E"><title>{e(title)} · kevscope</title><style>{CSS}{extra_css}</style></head>
<body><div class="wrap"><nav aria-label="Report identity"><div class="brand">kev<i>scope</i></div>
<div class="nav-note">QNX KERNEL TRACE DIAGNOSTICS<br>Offline report · v{e(__version__)}</div></nav>
<main>{body}</main><footer><span>kevscope · Local analysis. No uploads or external resources.</span>
<span>Capture → Diagnose → Compare → Verify</span></footer></div><script>{SCRIPT}</script></body></html>'''


def _totals(s):
    return (sum(f["affected"] for f in s["findings"]),
            sum(f["stolen_ms_total"] for f in s["findings"]),
            max((row["max_ms"] for row in s["channels"] + s["mutexes"]), default=0))


def _metrics(before, after=None):
    old = _totals(before)
    new = _totals(after) if after is not None else None
    labels = ("Affected waits", "Attributed delay", "Longest completed wait")
    notes = ("Individual message and mutex waits", "Sum across affected waits · ms", "Across analyzed dependencies · ms")
    parts = []
    for i, (label, note) in enumerate(zip(labels, notes)):
        a = f"{old[i]:,}" if i == 0 else f"{old[i]:.2f}"
        value = e(a)
        if new is not None:
            b = f"{new[i]:,}" if i == 0 else f"{new[i]:.2f}"
            value += f'<span class="arrow">→</span>{e(b)}'
        parts.append(f'<div class="metric"><span class="eyebrow">{label}</span><strong>{value}</strong><small>{note}</small></div>')
    return '<div class="metrics">' + ''.join(parts) + '</div>'


def _budget(s, candidate=False):
    result = s.get("wait_budget")
    if result is None:
        return ''
    label, color = {
        "exceeded": ("Budget exceeded", "bad"),
        "within_sample": ("Within observed sample", "good"),
        "no_samples": ("No completed waits", "caution"),
    }[result["status"]]
    title = "Candidate wait budget" if candidate else "Wait budget"
    limits = ' · '.join(f'{name} ≤ {value:.9g} ms' for name, value in result['limits_ms'].items())
    rows = []
    for row in result["measurements"]:
        status = ', '.join(row['exceeded']) + ' exceeded' if row['exceeded'] else 'Within sample'
        rows.append(f'<tr data-kind="{row["kind"]}"><td class="dependency">{e(row["dependency"])}</td>'
                    f'<td class="number">{row["waits"]}</td><td class="number">{row["max_ms"]:.9g}</td>'
                    f'<td class="number">{row["p99_ms"]:.9g}</td><td class="{ "bad" if row["exceeded"] else "good" }">{e(status)}</td></tr>')
    table = (f'<div class="comparison"><table><thead><tr><th>Dependency</th><th>Completed waits</th>'
             f'<th>Max ms</th><th>p99 ms</th><th>Budget result</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
             if rows else '<p class="empty">No completed reportable waits; the budget could not be evaluated.</p>')
    return (f'<section aria-label="{title}"><div class="section-head"><div><h2>{title}</h2><p>{e(limits)}</p></div>'
            f'<span class="pill {color}">{label}</span></div>{table}<p class="scope">{e(result["scope"])}</p></section>')


def _quality(s, name=None):
    analysis, tr = s["analysis"], s["trace"]
    c = analysis["coverage"]
    label = f"Evidence · {name}" if name else "Evidence coverage"
    if analysis.get("process"):
        label += f" · process {analysis['process']}"
    coverage = (f"{tr['events']:,} events · {tr['duration_ms']:.3f} ms · {tr['cpus']} CPUs<br>"
                f"{c['messages_completed']}/{c['messages_observed']} messages and "
                f"{c['mutex_waits_completed']}/{c['mutex_waits_observed']} mutex waits have captured boundaries.")
    if c['messages_observed']:
        coverage += f"<br>{c['messages_service_complete']} messages have correlated service evidence."
    if analysis["warnings"]:
        rows = ''.join(f"<li>{e(w['message'])}</li>" for w in analysis["warnings"])
        return f'<aside class="notice"><strong>{e(label)} · {e(analysis["status"])}</strong><ul>{rows}</ul><div class="coverage">{coverage}</div></aside>'
    return f'<div class="coverage"><strong>{e(label)}</strong><br>{coverage}</div>'


def _filters():
    return ('<div><div class="filters" role="group" aria-label="Filter dependency type">'
            '<button data-filter="all" aria-pressed="true">All</button>'
            '<button data-filter="message" aria-pressed="false">Messages</button>'
            '<button data-filter="mutex" aria-pressed="false">Mutexes</button></div>'
            '<span id="filter-status" class="scope" role="status" aria-live="polite"></span></div>')


def _timeline(f):
    worst = f["worst"]
    start, duration = worst["at_ms"], worst["blocked_ms"]
    duration = max(duration, 0.000001)
    parts = ['<svg class="timeline" viewBox="0 0 900 158" role="img" aria-label="Attributed intervals within the worst wait">',
             '<text x="16" y="25">WORST WAIT / RELATIVE TRACE TIME</text>',
             '<text x="16" y="59">Waiter blocked</text><text x="16" y="91">Owner READY</text><text x="16" y="123">Ran instead</text>',
             '<rect class="wait" x="145" y="43" width="735" height="20" rx="2"/>',
             f'<text x="145" y="149">+{start:.3f} ms</text><text x="880" y="149" text-anchor="end">+{start + duration:.3f} ms</text>']
    for x in worst.get("evidence", []):
        left = max(0, min(1, (x["start_ms"] - start) / duration))
        right = max(left, min(1, (x["end_ms"] - start) / duration))
        pos, width = 145 + 735 * left, max(0.3, 735 * (right - left))
        tip = e(f"{x['thread']} · CPU {x['cpu']} · priority {x['running_prio']} · "
                f"{x['start_ms']:.6f}–{x['end_ms']:.6f} ms")
        parts.append(f'<rect class="ready" x="{pos:.3f}" y="75" width="{width:.3f}" height="20"><title>{tip}</title></rect>')
        parts.append(f'<rect class="run" x="{pos:.3f}" y="107" width="{width:.3f}" height="20"><title>{tip}</title></rect>')
    parts.append('</svg><p class="scope">Colored intervals are supported by same-CPU execution boundaries and simultaneous priority evidence.</p>')
    return '<div class="timeline-scroll" tabindex="0" aria-label="Scrollable evidence timeline">' + ''.join(parts) + '</div>'


def _finding(f, opened=False):
    mutex = f["kind"] == "mutex_priority_inversion"
    kind = "mutex" if mutex else "message"
    waiter = f["waiter"] if mutex else f["client"]
    owners = ' / '.join(f["owners"]) if mutex else f["server"]
    waiter_prio = f["waiter_prio"] if mutex else f["client_prio"]
    owner_prios = f["owner_prios"] if mutex else f["server_prios"]
    total = f["waits"] if mutex else f["messages"]
    cause = ' '.join(report.mutex_cause(f["protocol"], waiter_prio)) if mutex else (
        "Check for _NTO_CHF_FIXED_PRIORITY or a server that changes its own priority. "
        "With QNX's default message priority inheritance, the server can run at its client's priority.")
    culprit = f["culprits"][0] if f["culprits"] else {"thread": "unknown", "prio": "?"}
    phases = (f'<p class="scope">Worst-call phases: {f["worst"]["queue_ms"]:.3f} ms before acceptance; '
              f'{f["worst"]["service_ms"]:.3f} ms accepted to reply. '
              'Acceptance is established by client REPLY state or the receive event.</p>' if kind == 'message' else '')
    rows = ''.join(f'<tr><td class="number">{x["start_ms"]:.6f}–{x["end_ms"]:.6f}</td>'
                   f'<td class="number">{x["cpu"]}</td><td>{e(x["thread"])}</td>'
                   f'<td class="number">{x["owner_prio"]} ≤ {x["running_prio"]} &lt; {x["waiter_prio"]}</td></tr>'
                   for x in f["worst"].get("evidence", []))
    return f'''<details class="finding" data-kind="{kind}" {'open' if opened else ''}>
<summary><div><div class="kind">{kind} priority inversion</div><h3>{e(waiter)} → {e(owners)}</h3>
<span class="scope">{f['affected']} of {total} waits affected · {f['stolen_ms_total']:.3f} ms attributed delay</span></div></summary>
<div class="detail"><p>The dependency was READY at lower priority while other work ran on the same observed CPU.</p>{phases}
<div class="chain"><div class="role"><small>WAITER · PRIORITY {waiter_prio}</small>{e(waiter)}</div><span>waits for →</span>
<div class="role"><small>DEPENDENCY · PRIORITY {e(', '.join(map(str, owner_prios)))}</small>{e(owners)}</div>
<span>delayed by →</span><div class="role ran"><small>TOP CONTRIBUTOR · PRIORITY {e(culprit['prio'])}</small>{e(culprit['thread'])}</div></div>
{_timeline(f)}<details class="evidence-table"><summary>Inspect exact intervals and priorities</summary>
<table><thead><tr><th>Trace time (ms)</th><th>CPU</th><th>Ran instead</th><th>Owner ≤ running &lt; waiter</th></tr></thead><tbody>{rows}</tbody></table></details>
<p class="hint">{e(cause)}</p></div></details>'''


def _findings(s, title="Inspect the evidence", filters=False):
    cards = ''.join(_finding(f, i == 0) for i, f in enumerate(s["findings"]))
    if not cards:
        message = ("No inversion found in the analyzed dependencies." if s["analysis"]["status"] == "usable"
                   else "No inversion established. Review the coverage warnings before drawing a conclusion.")
        cards = f'<div class="empty">{message}</div>'
    return (f'<section><div class="section-head"><div><h2>{e(title)}</h2>'
            f'<p>Follow a wait from the blocked thread to the work that delayed it.</p></div>{_filters() if filters else ""}</div>{cards}</section>')


def _latencies(s):
    rows = []
    for kind, entries in (("Message", s["channels"]), ("Mutex", s["mutexes"])):
        for row in entries:
            name = row["client"] if kind == "Message" else row["waiter"]
            count = row["messages"] if kind == "Message" else row["waits"]
            phases = ''
            if kind == 'Message' and row['queue_ms']['count']:
                phases = (f'<small>Phase maxima: {row["queue_ms"]["max"]:.3f} ms before acceptance; '
                          f'{row["service_ms"]["max"]:.3f} ms accepted to reply.</small>')
                cpus = f'Client CPUs {row["client_cpus"]}; server CPUs {row["server_cpus"]}'
                phases += f'<small>{"Cross-process" if row["cross_process"] else "Same-process"} · {e(cpus)}</small>'
            rows.append(f'<tr><td>{e(name)}<small>{kind} · {count} waits</small>{phases}</td><td class="number">{row["p99_ms"]:.3f}</td><td class="number">{row["max_ms"]:.3f}</td></tr>')
    cpu = ''.join(f'<tr><td>{e(x["thread"])}</td><td class="number">{x["ms"]:.2f}</td><td class="number">{x["share"]*100:.1f}%</td></tr>' for x in s["cpu"][:8])
    return ('<section class="two"><div><h2>Wait distribution</h2><div class="comparison"><table class="compact"><thead><tr><th>Waiter</th><th>p99 ms</th><th>Max ms</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div><p class="scope">Phase maxima may describe different calls. CPUs are observed across this capture.</p></div><div><h2>CPU time</h2><div class="comparison"><table class="compact"><thead><tr><th>Thread</th><th>ms</th><th>1 CPU share</th></tr></thead>'
            f'<tbody>{cpu}</tbody></table></div><p class="scope">Share is relative to one CPU for the capture duration.</p></div></section>')


def render(s, name="trace.kev"):
    status = s["analysis"]["status"]
    verdict = "Inversions observed" if s["findings"] else "No inversions observed" if status == "usable" else "Evidence incomplete"
    color = "bad" if s["findings"] else "good" if status == "usable" else "caution"
    body = f'''<header class="hero"><div><span class="eyebrow">01 / Kernel trace analysis</span>
<h1>Find the delay.<br><em>Follow the evidence.</em></h1><p>Message passing and mutex dependencies, explained through QNX scheduling evidence.</p></div>
<div class="hero-side"><span class="pill {color}">{verdict}</span><div class="source">{e(name)}</div>
<span class="eyebrow">{e(s['trace']['system'])} · {e(s['trace']['machine'])}</span></div></header>'''
    body += _metrics(s) + _budget(s) + _quality(s) + _findings(s, filters=True) + _latencies(s)
    body += f'<p class="scope">{e(s["analysis"]["method"])} {e(s["analysis"]["scope"])}</p>'
    return _document(name, body)


def render_comparison(result, baseline_name="baseline.kev", candidate_name="candidate.kev"):
    a, b = result["baseline"], result["candidate"]
    rows = []
    for row in result["groups"]:
        old, new = row["baseline"], row["candidate"]
        color = "bad" if row["change"] in ("regressed", "new_finding") else "good" if row["change"] == "improved" else "muted"
        def cell(x):
            return (f'<span class="number">{x["affected"]}/{x["waits"]}</span><small>{x["affected_pct"]:.1f}% affected</small>' if x else '—')
        scale = max((x["max_ms"] for x in (old, new) if x), default=1) or 1
        def wait_cell(x, before=False):
            if not x:
                return '—'
            phases = (f'<small>Before acceptance: {x["queue_ms"]["max"]:.3f}<br>'
                      f'Accepted → reply: {x["service_ms"]["max"]:.3f}</small>'
                      if x['queue_ms'] and x['queue_ms']['count'] else '')
            return (f'<span class="number">{x["max_ms"]:.3f}</span><small>p99 {x["p99_ms"]:.3f}</small>'
                    f'{phases}<span class="bar {"before" if before else ""}"><i style="width:{100*x["max_ms"]/scale:.2f}%"></i></span>')
        state = row["change"].replace('_', ' ') if row["state"] == "matched" else row["state"].replace('_', ' ')
        protocol = f'<small>{e(old["protocol"])} → {e(new["protocol"])}</small>' if old and new and old["protocol"] else ''
        topology = ''
        if row['kind'] == 'message':
            descriptions = []
            for x in (old, new):
                if x:
                    cpus = ', '.join(map(str, x['client_cpus'])) or '?'
                    server_cpus = ', '.join(map(str, x['server_cpus'])) or '?'
                    descriptions.append(f'{"Cross-process" if x["cross_process"] else "Same-process"} · client CPU {cpus} → server CPU {server_cpus}')
            topology = '<small>' + '<br>Then: '.join(e(t) for t in dict.fromkeys(descriptions)) + '</small>'
        rows.append(f'<tr data-kind="{row["kind"]}"><td class="dependency">{e(row["dependency"])}<small>{row["kind"]}</small>{protocol}{topology}</td>'
                    f'<td>{cell(old)}</td><td>{cell(new)}</td><td>{wait_cell(old, True)}</td><td>{wait_cell(new)}</td>'
                    f'<td><span class="state {color}">{e(state)}</span></td></tr>')
    direction = result["direction"]
    color = "bad" if result["regressions"] else "good" if result["improvements"] else "caution"
    body = f'''<header class="hero"><div><span class="eyebrow">02 / Before &amp; after</span><h1>Verify the fix.<br><em>Keep the evidence.</em></h1>
<p>Compare the same workload across two captures. Changed PIDs and mutex addresses are matched by thread roles.</p></div>
<div class="hero-side"><span class="pill {color}">Measured direction: {e(direction)}</span>
<div class="source">{e(baseline_name)}<br>↓<br>{e(candidate_name)}</div>
<span class="eyebrow">{result['matched']} matched {'dependency' if result['matched'] == 1 else 'dependencies'} · {e(result['status'])} coverage</span></div></header>'''
    body += _metrics(a, b) + _budget(b, candidate=True)
    if result["warnings"]:
        body += '<aside class="notice"><strong>Comparison limits</strong><ul>' + ''.join(f'<li>{e(w)}</li>' for w in result["warnings"]) + '</ul></aside>'
    body += ('<section><div class="section-head"><div><h2>What changed</h2><p>Sample counts stay visible. Disappearing work is never labeled fixed.</p></div>'
             f'{_filters()}</div><div class="comparison"><table><thead><tr><th>Dependency</th><th>Baseline waits</th><th>Candidate waits</th><th>Baseline max ms</th><th>Candidate max ms</th><th>Inversion rate</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div><p class="scope">{e(result["scope"])}</p>'
             '<p class="scope">Phase maxima may come from different calls. CPUs are observed across each capture.</p></section>')
    body += f'<section class="two"><div>{_quality(a, baseline_name)}</div><div>{_quality(b, candidate_name)}</div></section>'
    body += _findings(a, "Baseline: why it was flagged") + _findings(b, "Candidate: remaining findings")
    body += f'<p class="scope">{e(result["method"])}</p>'
    return _document(f"{baseline_name} → {candidate_name}", body)
