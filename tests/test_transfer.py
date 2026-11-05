"""Exercise the real localhost transfer protocol, including partial-file preservation."""
import json
import socket
import threading
import time

import pytest

from demo import recv


def start_receiver(tmp_path, count=1, timeout=2, **options):
    ready = tmp_path / 'ready.json'
    result = []

    def run():
        try:
            result.append(recv.receive(tmp_path / 'files', count, port=0, timeout=timeout, ready_file=ready, **options))
        except Exception as exc:
            result.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline and worker.is_alive():
        time.sleep(0.005)
    assert ready.exists(), result
    while True:
        try:
            port = json.loads(ready.read_text())['port']
            break
        except json.JSONDecodeError:
            assert time.monotonic() < deadline
            time.sleep(0.005)
    return worker, result, port


def send(port, data):
    with socket.create_connection(('127.0.0.1', port), timeout=2) as conn:
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        try:
            return conn.recv(3)
        except ConnectionResetError:
            return b''


def test_complete_files_are_acknowledged_and_published_atomically(tmp_path):
    worker, result, port = start_receiver(tmp_path, count=2)
    assert send(port, b'KEVSCOPE/1 5 sample.kev\nhello') == b'OK\n'
    assert send(port, b'KEVSCOPE/1 0 empty.txt\n') == b'OK\n'
    worker.join(3)
    assert result == [{'sample.kev', 'empty.txt'}]
    assert (tmp_path / 'files/sample.kev').read_bytes() == b'hello'
    assert (tmp_path / 'files/empty.txt').read_bytes() == b''
    assert not list((tmp_path / 'files').glob('*.part'))


@pytest.mark.parametrize('data', [
    b'KEVSCOPE/1 5 sample.kev\nhi',
    b'KEVSCOPE/1 1 sample.kev\ntoo much',
    b'KEVSCOPE/1 99999 sample.kev\n',
    b'KEVSCOPE/1 1 ../sample.kev\nx',
    b'KEVSCOPE/2 1 sample.kev\nx',
])
def test_failed_transfer_preserves_existing_file_and_removes_partial(data, tmp_path):
    worker, result, port = start_receiver(tmp_path, max_bytes=100)
    target = tmp_path / 'files/sample.kev'
    target.write_bytes(b'original')
    assert send(port, data) != b'OK\n'
    worker.join(3)
    assert len(result) == 1 and isinstance(result[0], ValueError)
    assert target.read_bytes() == b'original'
    assert not list((tmp_path / 'files').glob('*.part'))


def test_duplicate_transfer_is_rejected(tmp_path):
    worker, result, port = start_receiver(tmp_path, count=2)
    assert send(port, b'KEVSCOPE/1 1 same.txt\na') == b'OK\n'
    assert send(port, b'KEVSCOPE/1 1 same.txt\nb') != b'OK\n'
    worker.join(3)
    assert isinstance(result[0], ValueError) and 'duplicate' in str(result[0])
    assert (tmp_path / 'files/same.txt').read_bytes() == b'a'


def test_receiver_cannot_wait_forever_for_missing_guest(tmp_path):
    worker, result, _ = start_receiver(tmp_path, timeout=0.2)
    worker.join(3)
    assert not worker.is_alive() and len(result) == 1
    assert isinstance(result[0], (TimeoutError, socket.timeout))
