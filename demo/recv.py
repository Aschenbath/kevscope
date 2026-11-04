#!/usr/bin/env python3
"""Receive files from the QEMU guest (see kevpush.c). First line of each connection is the name."""
import os
import socket
import sys

out = sys.argv[1]
want = int(sys.argv[2]) if len(sys.argv) > 2 else 4
os.makedirs(out, exist_ok=True)
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 15610))
srv.listen(4)
for _ in range(want):
    conn, _addr = srv.accept()
    f = conn.makefile("rb")
    name = os.path.basename(f.readline().decode().strip())
    with open(os.path.join(out, name), "wb") as w:
        size = 0
        while chunk := f.read(65536):
            w.write(chunk)
            size += len(chunk)
    print("received", name, size, flush=True)
