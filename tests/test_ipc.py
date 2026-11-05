"""QNX COMM records use (destination pid, rcvid) at send/receive, (client pid, tid) at reply.

The official traceprinter walkthrough demonstrates the distinction with rcvid 0x4f
and client tid 1. Interleaved processes must not share a global rcvid namespace.
"""
from pathlib import Path

import pytest

from kevscope import comparison, kev, model, report


def running(at, cpu, key, priority=10):
    return kev.Event(at, cpu, kev.PR_TH, kev.ST_RUNNING, [*key, priority, 1])


def comm(at, cpu, code, first, pid):
    return kev.Event(at, cpu, kev.COMM, code, [first, pid])


def build(events):
    return model.build(kev.Kev({}, 1000, 2, events))


@pytest.mark.parametrize('reply_code', [kev.COMM_REPLY, kev.COMM_ERROR])
def test_cross_process_reply_uses_client_identity_not_rcvid(reply_code):
    client, server = (101, 1), (201, 4)
    mdl = build([
        running(0, 0, client, 50),
        comm(1, 0, kev.COMM_SMSG, 0x4f, server[0]),
        running(2, 1, server),
        comm(3, 1, kev.COMM_RMSG, 0x4f, server[0]),
        comm(8, 1, reply_code, client[1], client[0]),
        running(10, 0, client, 50),
    ])
    message, = mdl.messages
    assert (message.client, message.server, message.received, message.replied) == (client, server, 3, 8)
    assert message.destination_pid == server[0] and message.replier == server
    assert message.error is (reply_code == kev.COMM_ERROR)
    assert not message.issues


def test_same_rcvid_on_two_server_processes_does_not_cross_wire_calls():
    a, b, x, y = (101, 1), (102, 1), (201, 1), (202, 1)
    mdl = build([
        running(0, 0, a, 50), running(0, 1, b, 50),
        comm(1, 0, kev.COMM_SMSG, 7, x[0]), comm(2, 1, kev.COMM_SMSG, 7, y[0]),
        running(3, 0, x), comm(4, 0, kev.COMM_RMSG, 7, x[0]),
        running(5, 1, y), comm(6, 1, kev.COMM_RMSG, 7, y[0]),
        comm(8, 1, kev.COMM_REPLY, b[1], b[0]), comm(9, 0, kev.COMM_REPLY, a[1], a[0]),
        running(10, 0, a, 50), running(10, 1, b, 50),
    ])
    assert [(m.client, m.server, m.received, m.replied) for m in mdl.messages] == [(a, x, 4, 9), (b, y, 6, 8)]


def test_reply_from_a_different_process_cannot_complete_an_outstanding_call():
    client, server, stranger = (101, 1), (201, 1), (301, 1)
    mdl = build([
        running(0, 0, client, 50), comm(1, 0, kev.COMM_SMSG, 1, server[0]),
        running(2, 0, server), comm(3, 0, kev.COMM_RMSG, 1, server[0]),
        running(4, 1, stranger), comm(5, 1, kev.COMM_REPLY, client[1], client[0]),
        running(6, 1, stranger),
    ])
    message, = mdl.messages
    assert message.replied is None
    assert 'message_reply_mismatch' in message.issues


def test_missing_receive_is_reported_instead_of_inventing_a_service_interval():
    client, server = (101, 1), (201, 1)
    mdl = build([
        running(0, 0, client, 50), comm(1, 0, kev.COMM_SMSG, 1, server[0]),
        running(2, 1, server), comm(5, 1, kev.COMM_REPLY, client[1], client[0]),
        running(6, 0, client, 50),
    ])
    summary = report.summary(mdl)
    assert summary['findings'] == []
    assert 'message_missing_receive' in {w['code'] for w in summary['analysis']['warnings']}
    assert summary['analysis']['status'] == 'limited'


def test_reply_handoff_preserves_receiver_and_excludes_unproven_ownership():
    client, receiver, worker = (101, 1), (201, 1), (201, 2)
    mdl = build([
        running(0, 0, client, 50), comm(1, 0, kev.COMM_SMSG, 1, receiver[0]),
        running(2, 1, receiver), comm(3, 1, kev.COMM_RMSG, 1, receiver[0]),
        running(4, 1, worker), comm(5, 1, kev.COMM_REPLY, client[1], client[0]),
        running(6, 0, client, 50),
    ])
    message, = mdl.messages
    assert message.server == receiver and message.replier == worker
    assert 'message_handoff' in message.issues
    summary = report.summary(mdl)
    assert 'message_handoff' in {w['code'] for w in summary['analysis']['warnings']}
    assert not summary['findings']


def test_duplicate_live_destination_id_is_marked_ambiguous():
    a, b, server = (101, 1), (102, 1), (201, 1)
    mdl = build([
        running(0, 0, a, 50), running(0, 1, b, 50),
        comm(1, 0, kev.COMM_SMSG, 7, server[0]), comm(2, 1, kev.COMM_SMSG, 7, server[0]),
        running(3, 0, server), comm(4, 0, kev.COMM_RMSG, 7, server[0]),
        comm(6, 0, kev.COMM_REPLY, a[1], a[0]), comm(7, 0, kev.COMM_REPLY, b[1], b[0]),
        running(8, 0, a, 50),
    ])
    assert all('message_id_collision' in message.issues for message in mdl.messages)
    assert all(message.received is None for message in mdl.messages)
    assert [message.replied for message in mdl.messages] == [6, 7]


def test_sequential_reuse_does_not_retain_the_previous_transaction():
    client, server = (101, 1), (201, 1)
    events = []
    for at in (0, 10):
        events.extend([
            running(at, 0, client, 50), comm(at + 1, 0, kev.COMM_SMSG, 9, server[0]),
            running(at + 2, 1, server), comm(at + 3, 1, kev.COMM_RMSG, 9, server[0]),
            comm(at + 5, 1, kev.COMM_REPLY, client[1], client[0]),
        ])
    mdl = build(events)
    assert [message.replied for message in mdl.messages] == [5, 15]
    assert all(not message.issues for message in mdl.messages)


def test_existing_native_capture_contains_a_completed_cross_process_call():
    path = Path(__file__).parent / 'data/mutex-inversion.kev'
    mdl = model.build(kev.read(path))
    # tracelogger calls procnto with rcvid 20; the reply identifies its client tid 1.
    calls = [m for m in mdl.messages if m.rcvid == 20 and m.client[0] != 1]
    assert calls and all(m.received is not None and m.replied is not None for m in calls)
    assert all(m.server[0] == 1 for m in calls)


def test_background_replies_do_not_steal_native_application_calls():
    mdl = model.build(kev.read(Path(__file__).parent / 'data/inversion.kev'))
    client = next(t for t in mdl.threads.values() if t.name == 'control')
    server = next(t for t in mdl.threads.values() if t.name == 'sensor')
    replies = [e for e in mdl.trace.events if e.cls == kev.COMM and e.code == kev.COMM_REPLY
               and e.words == [client.tid, client.pid]]
    calls = [m for m in mdl.messages if m.client == client.key]
    assert len(calls) == len(replies) == 100
    assert all(m.server == server.key and m.service_complete for m in calls)
    assert {m.replied for m in calls} == {e.t for e in replies}


def service_model(received):
    client = model.Thread(101, 1, 'control', 'client',
                          changes=[(0, kev.ST_RUNNING, 50, 1), (5, kev.STATES.index('SEND'), 50, 1),
                                   (received, kev.STATES.index('REPLY'), 50, 1)],
                          runs=[(0, 5, 1, 50), (40, 50, 1, 50)])
    server = model.Thread(201, 1, 'sensor', 'server',
                          changes=[(0, kev.ST_RUNNING, 10, 1), (10, kev.ST_READY, 10, 1), (30, kev.ST_RUNNING, 10, 1)],
                          runs=[(0, 10, 0, 10), (30, 40, 0, 10)])
    logger = model.Thread(301, 1, 'logger', 'worker', changes=[(10, kev.ST_RUNNING, 30, 1)], runs=[(10, 30, 0, 30)])
    message = model.Message(client.key, server.key, 7, 5, received, 40)
    message.reply_started = received
    return model.Model(kev.Kev({}, 1000, 2, []), {t.key: t for t in (client, server, logger)}, {}, [message], [], [], 0, 50)


def test_pre_receive_queue_time_is_not_attributed_to_the_later_receiver():
    mdl = service_model(30)
    assert report.summary(mdl)['findings'] == []


def test_analysis_and_phase_metrics_use_the_received_boundary():
    summary = report.summary(service_model(20))
    finding, = summary['findings']
    assert finding['stolen_ms_total'] == 10
    assert all(e['start_ms'] >= 20 for e in finding['worst']['evidence'])
    channel, = summary['channels']
    assert channel['queue_ms']['max'] == 15
    assert channel['service_ms']['max'] == 20
    assert channel['max_ms'] == 35


def test_reply_state_proves_acceptance_before_delayed_receive_event():
    mdl = service_model(20)
    mdl.messages[0].reply_started = 6
    client = mdl.threads[mdl.messages[0].client]
    client.changes[-1] = (6, kev.STATES.index('REPLY'), 50, 1)
    summary = report.summary(mdl)
    assert summary['findings'][0]['stolen_ms_total'] == 20
    assert summary['channels'][0]['queue_ms']['max'] == 1
    assert summary['channels'][0]['service_ms']['max'] == 34


def test_native_model_records_reply_state_before_receiver_runs():
    client, server = (101, 1), (201, 1)
    mdl = build([
        running(0, 0, client, 50), comm(1, 0, kev.COMM_SMSG, 7, server[0]),
        kev.Event(2, 0, kev.PR_TH, kev.STATES.index('REPLY'), [*client, 50, 1]),
        running(5, 1, server), comm(6, 1, kev.COMM_RMSG, 7, server[0]),
        comm(9, 1, kev.COMM_REPLY, client[1], client[0]),
    ])
    message, = mdl.messages
    assert message.reply_started == message.accepted == 2
    assert message.received == 6 and message.service_complete


def test_shared_mutex_owner_word_does_not_prefer_a_colliding_local_pid():
    waiter, local_owner, remote_owner = (101, 1), (101, 2), (101 + 32768, 2)
    mdl = build([
        running(0, 0, local_owner), running(0, 1, remote_owner), running(1, 0, waiter, 50),
        kev.Event(2, 0, kev.KER_CALL, model.KER_MUTEX_LOCK,
                  [0x1000, 0, model.SYNC_SHARED | model.SYNC_PRIONONE, 101 << 16 | 2]),
        kev.Event(3, 0, kev.PR_TH, model.ST_MUTEX, [*waiter, 50, 1]),
        running(4, 0, local_owner),
        kev.Event(7, 0, kev.PR_TH, kev.ST_READY, [*waiter, 50, 1]),
        running(8, 0, waiter, 50),
    ])
    wait, = mdl.mutex_waits
    assert wait.owner is None
    assert 'shared_mutex_identity' in {w['code'] for w in report.summary(mdl)['analysis']['warnings']}


@pytest.mark.parametrize('name,affected,priority', [('ipc-inversion', 25, 10), ('ipc-inherit', 0, 50)])
def test_native_cross_process_capture_keeps_identity_cpu_and_phases(name, affected, priority):
    mdl = model.build(kev.read(Path(__file__).parent / 'data' / f'{name}.kev'))
    control, sensor, logger = (next(t for t in mdl.threads.values() if t.name == n)
                               for n in ('control', 'sensor', 'logger'))
    assert control.pid != sensor.pid and logger.pid == control.pid
    assert {cpu for _, _, cpu, _ in control.runs} == {1}
    assert {cpu for _, _, cpu, _ in sensor.runs + logger.runs} == {0}
    calls = [m for m in mdl.messages if m.client == control.key]
    assert len(calls) == 100
    assert all(m.server == sensor.key and m.replier == sensor.key and m.service_complete for m in calls)
    assert all(m.destination_pid == sensor.pid != control.pid for m in calls)
    assert all(m.sent <= m.accepted <= m.received <= m.replied for m in calls)
    assert not (mdl.trace.dropped or mdl.trace.partial or mdl.trace.trailing_bytes)
    result = report.summary(mdl, 'rtdemo')
    channel, = result['channels']
    assert channel['cross_process'] and channel['server_prios'] == [priority]
    assert channel['inversions'] == affected
    assert channel['queue_ms']['count'] == channel['service_ms']['count'] == 100
    if affected:
        finding, = result['findings']
        assert finding['culprits'][0]['thread'] == logger.label
        assert finding['stolen_ms_total'] == 92.446
        assert {item['cpu'] for item in finding['worst']['evidence']} == {0}
        assert result['analysis']['coverage']['uncertain_intervals'] == {'unbounded_ready': 1}
    else:
        assert result['findings'] == [] and result['analysis']['status'] == 'usable'


def test_native_ipc_comparison_keeps_boundary_warning_and_improvement():
    before, after = (model.build(kev.read(Path(__file__).parent / 'data' / f'{name}.kev'))
                     for name in ('ipc-inversion', 'ipc-inherit'))
    result = comparison.compare(before, after, 'rtdemo')
    row, = result['groups']
    assert result['status'] == 'limited' and result['improvements'] == 1
    assert row['state'] == 'matched' and row['max_wait_delta_ms'] == -6.624
    assert row['baseline']['cross_process'] and row['candidate']['cross_process']


def test_restarted_call_releases_its_receive_id_index():
    client, server = (101, 1), (201, 1)
    mdl = build([
        running(0, 0, client, 50), comm(1, 0, kev.COMM_SMSG, 7, server[0]),
        comm(2, 0, kev.COMM_SMSG, 7, server[0]),
        running(3, 1, server), comm(4, 1, kev.COMM_RMSG, 7, server[0]),
        comm(5, 1, kev.COMM_REPLY, client[1], client[0]),
    ])
    first, second = mdl.messages
    assert first.replied is None and first.issues == {'message_restarted'}
    assert second.received == 4 and second.replied == 5 and second.service_complete
