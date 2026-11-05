#!/usr/bin/env python3
"""Rebuild the public, offline examples from the six controlled QNX fixtures."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kevscope import comparison, html_report, kev, model


def generate(output):
    output = Path(output)
    examples = output / 'examples'
    examples.mkdir(parents=True, exist_ok=True)
    models = {name: model.build(kev.read(ROOT / 'tests/data' / f'{name}.kev'))
              for name in ('inversion', 'inherit', 'mutex-inversion', 'mutex-inherit', 'ipc-inversion', 'ipc-inherit')}
    cards = []
    for kind, before, after, title, topology in (
        ('ipc', 'ipc-inversion', 'ipc-inherit', 'A reply from another process', 'Control on CPU 1; a separate sensor process and logger on CPU 0.'),
        ('mutex', 'mutex-inversion', 'mutex-inherit', 'A mutex owner that cannot finish', 'Three threads in one process on CPU 0.'),
        ('message', 'inversion', 'inherit', 'A server delayed by the logger', 'Three threads in one process on CPU 0.'),
    ):
        result = comparison.compare(models[before], models[after], process='rtdemo')
        page = html_report.render_comparison(result, before + '.kev', after + '.kev')
        (examples / (kind + '.html')).write_text(page, encoding='utf-8')
        row = next(row for row in result['groups'] if row['dependency'] == 'control (rtdemo) -> sensor (rtdemo)')
        a, b = row['baseline'], row['candidate']
        cards.append(f'''<article class="finding"><div class="detail"><span class="eyebrow">{kind} priority inheritance</span>
<h2>{title}</h2><p>{topology}</p><p>{a['affected']}/{a['waits']} affected waits → {b['affected']}/{b['waits']}<br>
Longest wait: <strong>{a['max_ms']:.3f} → {b['max_ms']:.3f} ms</strong></p>
<a href="examples/{kind}.html">Explore the {kind} comparison →</a></div></article>''')
    events = sum(len(m.trace.events) for m in models.values())
    body = f'''<header class="hero"><div><span class="eyebrow">QNX Global Hackathon / Developer Tooling &amp; Host Tools</span>
<h1>Replay a real<br><em>QNX diagnosis.</em></h1><p>Explore kernel traces from a controlled QNX 8.0 workload. See what delayed the control thread, inspect the scheduling evidence, and compare the fix.</p></div>
<div class="hero-side"><span class="pill good">Interactive examples</span><p>No account, SDK or installation needed to explore these reports.</p>
<p><a href="https://github.com/Aschenbath/kevscope">Source, installation &amp; full tutorial ↗</a></p></div></header>
<div class="metrics"><div class="metric"><span class="eyebrow">Captured on QNX</span><strong>6 traces</strong><small>QNX 8.0 · QEMU x86_64</small></div>
<div class="metric"><span class="eyebrow">Decoder validation</span><strong>{events:,}</strong><small>Events matched against QNX traceprinter</small></div>
<div class="metric"><span class="eyebrow">Runtime dependencies</span><strong>0</strong><small>Python 3.9+ · Standard library only</small></div></div>
<section><h2>Choose a dependency</h2><p class="muted">Each workload uses control (priority 50), sensor (10), and logger (30). Compare channel or mutex priority inheritance, including IPC across processes and CPUs.</p>
<div class="two" style="grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:20px">{''.join(cards)}</div></section>
<section><h2>Try it on the captured data</h2><p>Clone the repository and run these commands from its root:</p>
<pre class="hint" style="overflow-x:auto"><code>python -m pip install .
kevscope compare tests/data/ipc-inversion.kev tests/data/ipc-inherit.kev --process rtdemo --html comparison.html</code></pre>
<p>Open <code>comparison.html</code> in a browser. Each report is a single file and works offline. For your own application, capture with <code>tracelogger -w</code>, then use <code>kevscope report</code> or <code>kevscope convert</code> for Perfetto.</p></section>
<aside class="notice"><strong>What the evidence establishes</strong><p>These are controlled examples, not a guarantee for every workload. The detector excludes unresolved CPU migrations, unbounded READY intervals, missing priority evidence, and process-shared locks without object identity. A report with no identified inversion does not prove that every real-time deadline was met.</p></aside>'''
    (output / 'index.html').write_text(html_report._document('Interactive QNX examples', body), encoding='utf-8')
    (output / '.nojekyll').touch()
    print(f'Generated index and three comparisons in {output}')


if __name__ == '__main__':
    generate(sys.argv[1] if len(sys.argv) > 1 else ROOT / 'docs')
