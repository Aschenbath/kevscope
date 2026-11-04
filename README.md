# kevscope

**See why a QNX real-time thread missed its deadline. Open a QNX kernel trace in Perfetto, and let kevscope point at the priority inversion.**

QNX's `tracelogger` records every context switch, message and kernel call into a `.kev` file. Until now the only way to read one was the Momentics System Profiler. kevscope is a small host tool (Python 3.9+, standard library only) that:

- **decodes `.kev` files** without the QNX SDP. The decoder is checked event by event against QNX's own `traceprinter`: 32,302 of 32,302 events match on the two bundled traces, and thread, process and message fields are compared value by value.
- **converts them to Perfetto** (`kevscope convert`), so you get CPU lanes, per-thread scheduling states, `MsgSend` → server → reply arrows and a findings track in [ui.perfetto.dev](https://ui.perfetto.dev). It's the same free UI Android and Linux developers already use, and you can query the trace with SQL.
- **finds real-time problems** (`kevscope report`): message-passing priority inversions, with the threads that ran instead and for how long, plus MsgSend blocking times, wake-up latency and CPU time per thread. `--json` and `--fail-on-inversion` make it usable in CI.

QNX publishes a [Perfetto example for the hypervisor](https://gitlab.com/qnx/hypervisor/tools-for-qnx-hypervisor/perfetto). Its README says it shows a single track and isn't meant for detailed diagnosis. kevscope covers the QNX kernel trace itself and adds the analysis.

## Example: a priority inversion in a small C program

[`demo/rtdemo.c`](demo/rtdemo.c) is a tiny control system pinned to one CPU:

| thread  | priority | work |
|---------|----------|------|
| control | 50 | every 10 ms: `MsgSend` to sensor, must finish within 5 ms |
| sensor  | 10 | server, ~2 ms of CPU per request |
| logger  | 30 | 6 ms bursts every 25 ms |

QNX servers normally inherit their client's priority, so sensor runs at 50 while it serves control. `rtdemo bad` creates the channel with `_NTO_CHF_FIXED_PRIORITY`, which turns that off. Measured on QNX 8.0 in QEMU:

```
rtdemo bad:  300 cycles, 55 over the 5 ms budget, mean 3.59 ms, worst 9.24 ms
rtdemo good: 300 cycles,  0 over the 5 ms budget, mean 2.62 ms, worst 2.96 ms
```

The program only knows it was late. kevscope reads the kernel trace and says why:

```
$ kevscope report tests/data/inversion.kev
PRIORITY INVERSION  control (rtdemo 352275/1) (prio 50) waited on sensor (rtdemo 352275/2)
  27 of 98 messages affected. The server ran at priority 10, below its priority 50 client,
  so lower-priority work ran first: 91.2 ms in total, while the client was blocked 146.9 ms.
    logger (rtdemo 352275/3) (prio 30): 91.1 ms
  Worst: at +273.064 ms the client was blocked 8.22 ms, 6.07 ms of it behind lower-priority threads.
  Likely cause: the server does not inherit its client's priority. Check for a channel
  created with _NTO_CHF_FIXED_PRIORITY, or a server that sets its own priority.
```

On the `good` trace it reports no inversion, and the worst `MsgSend` takes 2.26 ms.

The same moment in Perfetto (`kevscope convert`): control is blocked in `MsgSend -> sensor`. Sensor is serving it at priority 10 when logger (priority 30) preempts it, so sensor turns *Runnable* and logger takes CPU 0. The `kevscope findings` track marks the inversion.

![Priority inversion in Perfetto](docs/perfetto-inversion.png)

## Use it

```sh
pip install .                     # or run in place: python -m kevscope ...

kevscope report  trace.kev        # findings, IPC and latency tables
kevscope convert trace.kev        # writes trace.pftrace; open it in ui.perfetto.dev
kevscope dump    trace.kev -n 50  # decoded events, traceprinter style
```

Capture on the target. Run as root; `-w` (wide mode) is needed for thread names and priorities:

```sh
tracelogger -s 1 -w -f /dev/shmem/trace.kev
```

## Reproduce the demo on QEMU

Requires QNX SDP 8.0 (the free QNX Everywhere license works) and QEMU with KVM on a Linux host or WSL2.

```sh
source ~/qnx800/qnxsdp-env.sh
mkdir -p ~/qnxvm && cd ~/qnxvm && mkqnximage --type=qemu --arch=x86_64 --noprompt --build
demo/run-capture.sh traces/     # builds rtdemo with qcc, bakes it into the image, boots,
                                # traces both modes and copies the .kev files back
kevscope report traces/bad.kev
```

`run-capture.sh` uses QEMU user networking. A small helper (`demo/kevpush.c`) sends the traces back to the host over TCP, because the stock image has no working sshd.

## How it works

- `kevscope/kev.py`: reads the text header and skips the syspage, then reads 16-byte records. It rebuilds 64-bit timestamps from per-CPU `CONTROL/TIME` events and reassembles multi-record ("combine") events. Event layout comes from `<sys/trace.h>`.
- `kevscope/model.py`: thread table, per-CPU run intervals, state history, and `MsgSend`/`MsgReceive`/`MsgReply` transactions matched by receive id.
- `kevscope/report.py`: a message is a priority inversion when the server ran below the client's priority, and while the server was READY, threads with priority below the client's ran on the server's CPU. The report sums that time per culprit thread.
- `kevscope/perfetto.py`: a hand-written protobuf encoder. It writes ftrace `sched_switch`/`sched_waking` records for the CPU view, `TrackEvent` slices with flows for message passing, and a process tree for names. QNX pids/tids are mapped to Linux-style tids, and the real ones stay in the thread names, e.g. `sensor [352275/2]`. Priorities are QNX priorities (higher is more urgent).

## Tests

```sh
pip install pytest && pytest
```

The tests use two real traces captured from `rtdemo` on QNX 8.0 (`tests/data`), and gzipped `traceprinter` output of the same files as the reference.

## Limits

- Supports little-endian traces in the 16-byte event encoding, which is QNX 8.0's default. It has been tested on x86_64 traces from QEMU.
- Inversion detection covers message passing. Mutex and condvar waits show up in Perfetto but aren't analysed yet.
- Some kernel-call arguments are decoded only as raw words (`dump` prints them; Perfetto doesn't show them).

## Built for

The [QNX Global Hackathon](https://qnx.devpost.com/), Developer Tooling & Host Tools category. Target: QNX SDP 8.0 (8.0.0), x86_64 on QEMU 8.2 with KVM; toolchain `qcc -Vgcc_ntox86_64`.

Made with the help of AI coding tools.

## License

MIT
