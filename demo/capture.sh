#!/bin/sh
# Runs on the QNX guest at boot: trace the demo in each mode and send the traces to the host.
# Usage: capture.sh [modes...]   (default: bad good mutex-bad mutex-good)
D=/data/var/kevscope
sleep 4
MODES="${*:-bad good mutex-bad mutex-good}"
for mode in $MODES; do
    echo "--- capture $mode"
    $D/rtdemo $mode 3 > /dev/shmem/$mode.txt &
    sleep 1
    tracelogger -s 1 -w -b 2048 -k 64 -f /dev/shmem/$mode.kev
    wait
    cat /dev/shmem/$mode.txt
    $D/kevpush 10.0.2.2 15610 /dev/shmem/$mode.txt
    $D/kevpush 10.0.2.2 15610 /dev/shmem/$mode.kev
done
echo "=== CAPTURE DONE ==="
