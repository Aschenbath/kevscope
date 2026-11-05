#!/usr/bin/env python3
"""Receive length-checked, acknowledged transfers from demo/kevpush.c."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time

HEADER = re.compile(rb"KEVSCOPE/1 ([0-9]+) ([A-Za-z0-9][A-Za-z0-9._-]{0,127})\n")


def receive(out, want=8, *, port=15610, timeout=120.0, max_bytes=64 * 1024 * 1024, ready_file=None):
    """Publish a file atomically only after its advertised size and EOF have arrived."""
    if want <= 0 or timeout <= 0 or max_bytes <= 0:
        raise ValueError("file count, timeout and maximum size must be positive")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    deadline, seen = time.monotonic() + timeout, set()

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("capture transfer deadline exceeded")
        return value

    def read(conn, size):
        conn.settimeout(remaining())
        return conn.recv(size)

    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(4)
        if ready_file:
            Path(ready_file).write_text(json.dumps({"port": server.getsockname()[1]}), encoding="utf-8")
        for _ in range(want):
            server.settimeout(remaining())
            conn, _ = server.accept()
            with conn:
                line = bytearray()
                while len(line) < 256 and not line.endswith(b'\n'):
                    byte = read(conn, 1)
                    if not byte:
                        raise ValueError("connection ended before the transfer header")
                    line += byte
                match = HEADER.fullmatch(line)
                if match is None:
                    raise ValueError("invalid transfer header or filename (expected KEVSCOPE/1)")
                size, name = int(match[1]), match[2].decode('ascii')
                if size > max_bytes:
                    raise ValueError(f"{name}: advertised size exceeds the transfer limit")
                if name in seen:
                    raise ValueError(f"duplicate file: {name}")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=out, prefix='.kevscope-', suffix='.part', delete=False) as output:
                        temporary = Path(output.name)
                        received = 0
                        while received < size:
                            chunk = read(conn, min(65536, size - received))
                            if not chunk:
                                raise ValueError(f"{name}: truncated transfer ({received}/{size} bytes)")
                            output.write(chunk)
                            received += len(chunk)
                        if read(conn, 1):
                            raise ValueError(f"{name}: data exceeds advertised size")
                    os.replace(temporary, out / name)
                    temporary = None
                    conn.settimeout(remaining())
                    conn.sendall(b'OK\n')
                    seen.add(name)
                    print(f"received {name} {size} bytes (verified)", flush=True)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
    return seen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    parser.add_argument('count', nargs='?', type=int, default=8)
    parser.add_argument('--port', type=int, default=15610)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--max-bytes', type=int, default=64 * 1024 * 1024)
    parser.add_argument('--ready-file')
    args = parser.parse_args()
    try:
        receive(args.output, args.count, port=args.port, timeout=args.timeout,
                max_bytes=args.max_bytes, ready_file=args.ready_file)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"kevscope receiver: {exc}\n")


if __name__ == '__main__':
    main()
