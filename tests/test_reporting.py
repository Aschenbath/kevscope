"""Usable reports, honest comparisons, and the public CLI's exit contracts."""
import copy
import json
import struct
from html.parser import HTMLParser
from pathlib import Path

import pytest

from kevscope import cli, comparison, html_report, kev, model, report

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def traces():
    return {name: model.build(kev.read(DATA / f"{name}.kev"))
            for name in ("inversion", "inherit", "mutex-inversion", "mutex-inherit")}


def header(**fields):
    values = {"TRACE_CYCLES_PER_SEC": "1000000", "TRACE_CPU_NUM": "2", **fields}
    return ("TRACE_HEADER_BEGIN::" + ''.join(f"{key}::{value}" for key, value in values.items())
            + "TRACE_HEADER_END::").encode()


@pytest.mark.parametrize("fields", [
    {"TRACE_CYCLES_PER_SEC": "oops"}, {"TRACE_CYCLES_PER_SEC": "0"},
    {"TRACE_CPU_NUM": "0"}, {"TRACE_CPU_NUM": "65"},
    {"TRACE_SYSPAGE_LEN": "-1"}, {"TRACE_SYSPAGE_LEN": "9999"},
])
def test_bad_header_is_a_friendly_trace_error(fields):
    with pytest.raises(kev.KevError):
        kev.read(header(**fields))


def test_trailing_bytes_are_not_silently_discarded():
    trace = kev.read(header() + struct.pack('<4I', kev.CONTROL << 10 | kev.CTL_TIME, 0, 0, 0) + b'cut')
    assert trace.trailing_bytes == 3
    summary = report.summary(model.build(trace))
    assert summary["analysis"]["status"] == "insufficient"
    assert "trailing_bytes" in {warning["code"] for warning in summary["analysis"]["warnings"]}
    assert "No priority inversion established" in report.render(summary)


def test_empty_trace_is_insufficient_and_has_zero_duration():
    summary = report.summary(model.build(kev.read(header())))
    assert summary["trace"]["duration_ms"] == 0
    assert summary["analysis"]["status"] == "insufficient"


def test_initial_mutex_snapshot_is_counted_as_clipped():
    events = [kev.Event(0, 0, kev.PR_TH, kev.ST_CREATE, [100, 1]),
              kev.Event(1, 0, kev.PR_TH, model.ST_MUTEX, [100, 1, 50, 1]),
              kev.Event(10, 0, kev.PR_TH, kev.ST_RUNNING, [100, 1, 50, 1]),
              kev.Event(20, 0, kev.PR_TH, kev.ST_RUNNING, [100, 2, 10, 1])]
    summary = report.summary(model.build(kev.Kev({}, 1000, 1, events)))
    coverage = summary["analysis"]["coverage"]
    assert coverage["mutex_waits_left_censored"] == 1
    assert coverage["mutex_waits_completed"] == coverage["mutex_waits_observed"] - 1
    assert summary["analysis"]["status"] == "usable"


def test_condvar_context_is_reported_and_process_scope_is_explicit(traces):
    full = report.summary(traces["mutex-inherit"])
    assert 'missing_condvar_context' in {w['code'] for w in full['analysis']['warnings']}
    focused = report.summary(traces["mutex-inherit"], process='rtdemo')
    assert focused['analysis']['status'] == 'usable' and focused['analysis']['process'] == 'rtdemo'
    assert focused['analysis']['coverage']['mutex_waits_observed'] == 38


@pytest.mark.parametrize("before,after,kind", [
    ("inversion", "inherit", "message"), ("mutex-inversion", "mutex-inherit", "mutex"),
])
def test_comparison_matches_changed_pids_and_mutex_addresses(traces, before, after, kind):
    result = comparison.compare(traces[before], traces[after])
    row = next(row for row in result["groups"] if row["kind"] == kind and row["dependency"] == 'control (rtdemo) -> sensor (rtdemo)')
    assert row["state"] == "matched" and row["change"] == "improved"
    assert row["baseline"]["affected"] > 0 and row["candidate"]["affected"] == 0
    assert row["max_wait_delta_ms"] < 0
    assert result["regressions"] == 0 and result["improvements"] >= 1


def test_missing_candidate_work_is_not_a_fix(traces):
    candidate = copy.deepcopy(traces["mutex-inherit"])
    candidate.mutex_waits = []
    result = comparison.compare(traces["mutex-inversion"], candidate)
    row = next(row for row in result["groups"] if row["dependency"].startswith('control'))
    assert row["state"] == "not_observed" and row["candidate"] is None
    assert result["improvements"] == 0 and result["status"] == "limited"


def test_ambiguous_role_names_are_not_silently_paired(traces):
    candidate = copy.deepcopy(traces["mutex-inherit"])
    wait = copy.deepcopy(next(w for w in candidate.mutex_waits if candidate.threads[w.waiter].name == 'control'))
    wait.sync += 4
    candidate.mutex_waits.append(wait)
    result = comparison.compare(traces["mutex-inversion"], candidate)
    row = next(row for row in result["groups"] if row["dependency"].startswith('control'))
    assert row["state"] == "ambiguous" and row["candidate"] is None
    assert row["change"] != "improved"


def test_doubled_sample_count_with_same_affected_fraction_is_not_a_regression(traces):
    candidate = copy.deepcopy(traces["inversion"])
    candidate.messages *= 2
    result = comparison.compare(traces["inversion"], candidate)
    row = next(row for row in result["groups"] if row["kind"] == "message" and row["dependency"].startswith('control'))
    assert row["candidate"]["waits"] == row["baseline"]["waits"] * 2
    assert row["change"] == "unchanged" and result["regressions"] == 0


@pytest.mark.parametrize("args,expected", [
    (["report", "inversion.kev", "--fail-on-inversion"], 3),
    (["report", "inherit.kev", "--process", "rtdemo", "--strict"], 0),
    (["compare", "mutex-inherit.kev", "mutex-inversion.kev", "--fail-on-regression"], 3),
    (["compare", "mutex-inversion.kev", "mutex-inherit.kev", "--process", "rtdemo", "--fail-on-regression", "--strict"], 0),
])
def test_cli_exit_codes_and_json_remain_machine_readable(args, expected, capsys):
    args = [str(DATA / value) if value.endswith('.kev') else value for value in args] + ['--json']
    if expected:
        with pytest.raises(SystemExit) as stopped:
            cli.main(args)
        assert stopped.value.code == expected
    else:
        cli.main(args)
    assert isinstance(json.loads(capsys.readouterr().out), dict)


def test_strict_truncated_trace_exits_four_without_claiming_a_clean_report(tmp_path, capsys):
    path = tmp_path / 'truncated.kev'
    path.write_bytes((DATA / 'inherit.kev').read_bytes()[:-3])
    with pytest.raises(SystemExit) as stopped:
        cli.main(['report', str(path), '--strict', '--fail-on-inversion', '--json'])
    assert stopped.value.code == 4
    assert json.loads(capsys.readouterr().out)['analysis']['status'] == 'limited'


@pytest.mark.parametrize('command', ['convert', 'report', 'compare'])
def test_report_output_cannot_overwrite_an_input_trace(command, tmp_path, capsys):
    source = tmp_path / 'trace.kev'
    original = (DATA / 'inherit.kev').read_bytes()
    source.write_bytes(original)
    args = [command, str(source)]
    if command == 'compare':
        args += [str(DATA / 'inversion.kev')]
    args += ['-o' if command == 'convert' else '--html', str(source)]
    with pytest.raises(SystemExit) as stopped:
        cli.main(args)
    assert stopped.value.code == 1 and source.read_bytes() == original
    assert 'overwrite an input trace' in capsys.readouterr().err


def test_dump_io_error_has_no_traceback(tmp_path, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(['dump', str(tmp_path / 'missing.kev')])
    assert stopped.value.code == 1
    assert 'Traceback' not in capsys.readouterr().err


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_offline_html_escapes_trace_text_and_has_no_external_resources(traces):
    summary = report.summary(traces['inversion'])
    payload = '<img src=x onerror="alert(1)"></script><script>alert(2)</script>'
    summary['findings'][0]['client'] = payload
    html = html_report.render(summary, payload)
    document = Document(html)
    assert html == html_report.render(summary, payload)
    assert sum(tag == 'script' for tag, _ in document.elements) == 1
    assert not any(tag in ('img', 'iframe') or 'src' in attrs or (tag == 'link' and not attrs.get('href', '').startswith('data:')) or any(name.startswith('on') for name in attrs)
                   for tag, attrs in document.elements)
    assert '&lt;img' in html and '@import' not in html and 'url(' not in html


def test_html_contains_the_actual_comparison_and_witness_intervals(traces, tmp_path, capsys):
    target = tmp_path / 'comparison.html'
    cli.main(['compare', str(DATA / 'mutex-inversion.kev'), str(DATA / 'mutex-inherit.kev'), '--html', str(target)])
    html = target.read_text(encoding='utf-8')
    assert 'PTHREAD_PRIO_NONE' in html and 'PTHREAD_PRIO_INHERIT' in html
    assert '5.277' in html and '1.439' in html
    document = Document(html)
    assert any(tag == 'svg' and attrs.get('role') == 'img' for tag, attrs in document.elements)
    assert 'works offline' in capsys.readouterr().out
