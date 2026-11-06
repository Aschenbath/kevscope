#!/usr/bin/env python3
"""Rebuild the public, offline examples from the six controlled QNX fixtures."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kevscope import comparison, html_report, kev, model


LANDING_CSS = """
.landing .hero{align-items:center;gap:0 48px;padding:42px 0 0}.landing .hero>div:first-child{padding-bottom:36px}.landing .hero-side{padding-left:28px;margin-bottom:36px}
.landing .hero-side p{margin:12px 0}.landing .actions{display:flex;flex-wrap:wrap;align-items:center;gap:14px 22px;margin-top:25px}
.landing .primary{display:inline-block;background:var(--green);color:white;padding:13px 18px;text-decoration:none;border-radius:3px;font-size:14px}
.landing .primary:hover{background:#075544}.landing .secondary{font-size:13px;text-underline-offset:4px}
.landing .metric strong{font-size:clamp(25px,2.8vw,36px);letter-spacing:-1.4px}.landing .metrics{grid-column:1/-1;margin-bottom:10px}
.landing .sample-note{font-size:12px;color:var(--muted);max-width:850px;margin:12px 0 32px}
.landing .proof{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:16px 0;font:12px/1.8 var(--mono);color:var(--muted)}
.landing .proof strong{color:var(--ink)}.landing .scenario{margin:17px 0;font:13px/1.9 var(--mono)}
.landing .scenario span{display:block;border-bottom:1px solid var(--line);padding:5px 0}.landing pre{white-space:pre-wrap;overflow-wrap:anywhere}
@media(max-width:760px){.landing .hero{display:flex;flex-direction:column;align-items:stretch;gap:0;padding:30px 0 0}.landing .hero>div:first-child{padding-bottom:24px}.landing .hero-side{order:2;padding:15px 0 0;margin:16px 0 0}.landing .metrics{order:1}.landing .metric{display:block;line-height:1.4;padding:10px 0!important}.landing .metric strong{font-size:32px;margin:5px 0}.landing .actions{gap:14px}.landing .two{gap:8px}}
"""


def generate(output):
    output = Path(output)
    examples = output / 'examples'
    examples.mkdir(parents=True, exist_ok=True)
    models = {name: model.build(kev.read(ROOT / 'tests/data' / f'{name}.kev'))
              for name in ('inversion', 'inherit', 'mutex-inversion', 'mutex-inherit', 'ipc-inversion', 'ipc-inherit')}
    cards, results = [], {}
    for kind, before, after, title, topology in (
        ('ipc', 'ipc-inversion', 'ipc-inherit', 'A reply from another process', 'Control on CPU 1; a separate sensor process and logger on CPU 0.'),
        ('mutex', 'mutex-inversion', 'mutex-inherit', 'A mutex owner that cannot finish', 'Three threads in one process on CPU 0.'),
        ('message', 'inversion', 'inherit', 'A server delayed by the logger', 'Three threads in one process on CPU 0.'),
    ):
        result = comparison.compare(models[before], models[after], process='rtdemo')
        results[kind] = result
        page = html_report.render_comparison(result, before + '.kev', after + '.kev')
        (examples / (kind + '.html')).write_bytes(page.encode('utf-8'))
        row = next(row for row in result['groups'] if row['dependency'] == 'control (rtdemo) -> sensor (rtdemo)')
        a, b = row['baseline'], row['candidate']
        if kind == 'ipc':
            lead_before, lead_after = a, b
            continue
        cards.append(f'''<article class="finding"><div class="detail"><span class="eyebrow">{kind} priority inheritance</span>
<h2>{title}</h2><p>{topology}</p><p>{a['affected']}/{a['waits']} affected waits → {b['affected']}/{b['waits']}<br>
Longest wait: <strong>{a['max_ms']:.3f} → {b['max_ms']:.3f} ms</strong></p>
<a href="examples/{kind}.html">Explore the {kind} comparison →</a></div></article>''')
    events = sum(len(m.trace.events) for m in models.values())
    delay_before = sum(f['stolen_ms_total'] for f in results['ipc']['baseline']['findings'])
    delay_after = sum(f['stolen_ms_total'] for f in results['ipc']['candidate']['findings'])
    body = f'''<div class="landing"><header class="hero"><div><span class="eyebrow">QNX kernel trace diagnostics / Interactive examples</span>
<h1>A late reply.<br><em>An explained wait.</em></h1><p>Control waits on CPU 1. Its server is READY on CPU 0 while the logger runs. Follow the priority inversion and compare the fix.</p>
<div class="actions"><a class="primary" href="examples/ipc.html">Inspect the evidence →</a>
<a class="secondary" href="https://youtu.be/I44O-XcHyqg">2-min demo ↗</a></div></div>
<div class="hero-side"><span class="pill good">Start with the captured evidence</span>
<div class="scenario"><span>CPU 1 · control waits at priority 50</span><span>CPU 0 · sensor is READY at priority 10</span><span>CPU 0 · logger runs at priority 30</span></div>
<p>Default priority inheritance lets sensor serve control at priority 50.</p>
<p>No account, SDK or installation needed.</p></div>
<div class="metrics"><div class="metric"><span class="eyebrow">Calls affected by inversion</span><strong>{lead_before['affected']}/{lead_before['waits']}<span class="arrow">→</span>{lead_after['affected']}/{lead_after['waits']}</strong><small>Before → after priority inheritance</small></div>
<div class="metric"><span class="eyebrow">Longest MsgSend</span><strong>{lead_before['max_ms']:.3f}<span class="arrow">→</span>{lead_after['max_ms']:.3f}</strong><small>Milliseconds · completed calls</small></div>
<div class="metric"><span class="eyebrow">Attributed scheduling delay</span><strong>{delay_before:.3f}<span class="arrow">→</span>{delay_after:.3f}</strong><small>Milliseconds · sum across affected calls</small></div></div></header>
<p class="sample-note">Measured in the bundled one-second QNX 8.0 captures. The baseline has one unbounded READY interval, which remains visible as a coverage warning. These measurements describe this workload.</p>
<div class="proof"><strong>6 QNX captures</strong> · <strong>{events:,} events</strong> checked against QNX traceprinter · <strong>0 runtime dependencies</strong> · Python 3.9+</div>
<section><h2>Follow another dependency</h2><p class="muted">The same three roles, with a mutex or a message channel inside one process.</p>
<div class="two">{''.join(cards)}</div></section>
<section><h2>Try it on the captured data</h2><p>Clone the repository and run these commands from its root:</p>
<pre class="hint" style="overflow-x:auto"><code>python -m pip install .
kevscope compare tests/data/ipc-inversion.kev tests/data/ipc-inherit.kev --process rtdemo --html comparison.html</code></pre>
<p>Open <code>comparison.html</code> in a browser. Each report is a single file and works offline.</p>
<p><a href="https://github.com/Aschenbath/kevscope">Source, installation &amp; full tutorial ↗</a></p></section>
<section><h2>Keep a wait budget in CI</h2><p>A capture can have zero inversions and still contain a wait that exceeds your budget. In v0.3.2, check each dependency's maximum or p99 wait:</p>
<pre class="hint"><code>kevscope report tests/data/ipc-inherit.kev --process rtdemo --max-wait-ms 2 --json</code></pre>
<p>This fixed capture has a 2.495 ms maximum wait, so the 2 ms check exits <code>3</code>. Limits are optional, use unrounded durations, and appear in text, JSON and offline HTML. A capture with no completed reportable waits exits <code>4</code>.</p></section>
<aside class="notice"><strong>What the evidence establishes</strong><p>These are controlled examples, not a guarantee for every workload. The detector excludes unresolved CPU migrations, unbounded READY intervals, missing priority evidence, and process-shared locks without object identity. A report with no identified inversion does not prove that every real-time deadline was met. Budget checks apply to completed reportable waits; use <code>--strict</code> to also reject limited evidence.</p></aside></div>'''
    (output / 'index.html').write_bytes(html_report._document('Interactive QNX examples', body, LANDING_CSS).encode('utf-8'))
    (output / '.nojekyll').touch()
    print(f'Generated index and three comparisons in {output}')


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'docs')
