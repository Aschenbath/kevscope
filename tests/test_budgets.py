"""Wait budgets must catch slow dependencies independently of inversion findings."""
import json
from pathlib import Path

import pytest

from kevscope import cli, kev, model, report


DATA = Path(__file__).parent / "data"


@pytest.mark.parametrize("command", ["report", "compare"])
@pytest.mark.parametrize("flag,limit,expected", [
    ("--max-wait-ms", "2", 3),
    ("--max-wait-ms", "3", 0),
    ("--p99-wait-ms", "2", 3),
    ("--p99-wait-ms", "3", 0),
])
def test_budget_checks_fixed_ipc_without_an_inversion(command, flag, limit, expected, capsys):
    args = [command]
    if command == "compare":
        args.append(str(DATA / "ipc-inversion.kev"))
    args += [str(DATA / "ipc-inherit.kev"), "--process", "rtdemo", flag, limit, "--json"]
    if expected:
        with pytest.raises(SystemExit) as stopped:
            cli.main(args)
        assert stopped.value.code == expected
    else:
        cli.main(args)
    result = json.loads(capsys.readouterr().out)
    sample = result["candidate"] if command == "compare" else result
    assert sample["findings"] == []
    assert sample["wait_budget"]["status"] == ("exceeded" if expected else "within_sample")
    assert sample["wait_budget"]["waits_checked"] == 100


def synthetic_waits(durations):
    """Nanosecond-resolution completed calls, with no scheduling interference."""
    threads = {
        (1, 1): model.Thread(1, 1, name="control", process="app",
                             changes=[(0, kev.ST_RUNNING, 50, 0)], runs=[(0, 10_000_000, 0, 50)]),
        (2, 1): model.Thread(2, 1, name="sensor", process="app",
                             changes=[(0, kev.ST_RUNNING, 50, 0)], runs=[(0, 10_000_000, 1, 50)]),
    }
    messages = [model.Message((1, 1), (2, 1), sent=0, received=0, replied=duration)
                for duration in durations]
    return model.Model(kev.Kev({}, 1_000_000_000, 2, []), threads, {}, messages,
                       [], [], 0, 10_000_000)


def test_budget_uses_unrounded_duration_and_allows_exact_equality():
    mdl = synthetic_waits([1_000_400])
    exceeded = report.summary(mdl, max_wait_ms=1.0003)
    assert exceeded["channels"][0]["max_ms"] == 1.0
    assert exceeded["wait_budget"]["status"] == "exceeded"
    assert exceeded["wait_budget"]["measurements"][0]["max_ms"] == 1.0004
    assert report.summary(mdl, max_wait_ms=1.0004)["wait_budget"]["status"] == "within_sample"


def test_p99_budget_is_per_dependency_instead_of_pooled():
    mdl = synthetic_waits([1_000_000] * 1000)
    mdl.threads[(2, 2)] = model.Thread(2, 2, name="rare", process="app",
                                      changes=[(0, kev.ST_RUNNING, 50, 0)])
    mdl.messages.append(model.Message((1, 1), (2, 2), sent=0, received=0, replied=8_000_000))
    budget = report.summary(mdl, p99_wait_ms=5)["wait_budget"]
    assert budget["status"] == "exceeded" and budget["dependencies_checked"] == 2
    failed = [row for row in budget["measurements"] if row["exceeded"]]
    assert len(failed) == 1 and failed[0]["waits"] == 1
    assert "rare" in failed[0]["dependency"]


@pytest.mark.parametrize("flag", ["--max-wait-ms", "--p99-wait-ms"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1e999", "oops"])
def test_invalid_budget_is_rejected_before_reading_input(flag, value, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["report", "nonexistent.kev", flag + "=" + value])
    assert stopped.value.code == 2
    assert "finite positive" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["report", "compare"])
def test_no_completed_waits_cannot_pass_a_budget(command, tmp_path, capsys):
    source = tmp_path / "empty.kev"
    source.write_bytes(b"TRACE_HEADER_BEGIN::TRACE_CYCLES_PER_SEC::1000000TRACE_CPU_NUM::1TRACE_HEADER_END::")
    args = [command, str(source)]
    if command == "compare":
        args.append(str(source))
    with pytest.raises(SystemExit) as stopped:
        cli.main(args + ["--max-wait-ms", "5", "--json"])
    assert stopped.value.code == 4
    result = json.loads(capsys.readouterr().out)
    sample = result["candidate"] if command == "compare" else result
    assert sample["wait_budget"]["status"] == "no_samples"


def test_strict_coverage_has_precedence_over_exceeded_budget(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["report", str(DATA / "ipc-inversion.kev"), "--process", "rtdemo",
                  "--max-wait-ms", "5", "--strict", "--json"])
    assert stopped.value.code == 4
    result = json.loads(capsys.readouterr().out)
    assert result["analysis"]["status"] == "limited"
    assert result["wait_budget"]["status"] == "exceeded"


def test_mutex_budget_is_independent_of_message_samples(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["report", str(DATA / "mutex-inherit.kev"), "--process", "rtdemo",
                  "--max-wait-ms", "1", "--json"])
    assert stopped.value.code == 3
    result = json.loads(capsys.readouterr().out)
    assert result["findings"] == []
    assert result["wait_budget"]["measurements"][0]["kind"] == "mutex"


def test_budget_output_remains_visible_in_text_and_offline_html(tmp_path, capsys):
    source = str(DATA / "ipc-inherit.kev")
    with pytest.raises(SystemExit):
        cli.main(["report", source, "--process", "rtdemo", "--max-wait-ms", "2"])
    assert "Wait budget: exceeded" in capsys.readouterr().out
    target = tmp_path / "budget.html"
    with pytest.raises(SystemExit) as stopped:
        cli.main(["compare", source, source, "--process", "rtdemo", "--p99-wait-ms", "2",
                  "--html", str(target)])
    assert stopped.value.code == 3
    html = target.read_text(encoding="utf-8")
    assert "Candidate wait budget" in html and "Budget exceeded" in html
    assert "p99 ≤ 2 ms" in html and "100" in html


def test_unspecified_budget_preserves_default_report_contract():
    mdl = synthetic_waits([1_000_400])
    original = report.summary(mdl)
    checked = report.summary(mdl, max_wait_ms=5, p99_wait_ms=2)
    checked.pop("wait_budget")
    assert checked == original


def test_budget_html_escapes_dependency_names():
    from kevscope import html_report
    mdl = synthetic_waits([2_000_000])
    mdl.threads[(1, 1)].name = '<img src=x onerror="alert(1)">'
    html = html_report.render(report.summary(mdl, max_wait_ms=1))
    assert '&lt;img' in html and '<img' not in html
