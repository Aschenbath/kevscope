#!/usr/bin/env bash
# Build the demo, bake it into the QEMU image, boot, capture traces, and collect them on the host.
# Usage: demo/run-capture.sh [output dir]
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/traces}"
VM=~/qnxvm
source ~/qnx800/qnxsdp-env.sh >/dev/null
mkdir -p "$VM/payload" "$VM/local/snippets"
qcc -Vgcc_ntox86_64 -O2 -Wall "$REPO/demo/rtdemo.c" -o "$VM/payload/rtdemo" || exit 1
qcc -Vgcc_ntox86_64 -O2 -Wall "$REPO/demo/kevpush.c" -o "$VM/payload/kevpush" -lsocket || exit 1
tr -d '\r' < "$REPO/demo/capture.sh" > "$VM/payload/capture.sh"

cat > "$VM/local/snippets/data_files.custom" <<EOF
[uid=0 gid=0 perms=755] var/kevscope/rtdemo=$HOME/qnxvm/payload/rtdemo
[uid=0 gid=0 perms=755] var/kevscope/kevpush=$HOME/qnxvm/payload/kevpush
[uid=0 gid=0 perms=755] var/kevscope/capture.sh=$HOME/qnxvm/payload/capture.sh
EOF
cat > "$VM/local/snippets/post_start.custom" <<EOF
/data/var/kevscope/capture.sh
EOF

cd "$VM"
mkqnximage --noprompt --build 2>&1 | grep -iE "error|fatal"
python3 "$REPO/demo/recv.py" "$OUT" 4 &
RECV=$!
qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 1G -nographic -serial mon:stdio \
  -pidfile output/qemu.pid -kernel output/ifs.bin -drive file=output/disk-qemu.vmdk,if=ide,id=drv0 \
  -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
  -object rng-random,filename=/dev/urandom,id=rng0 -device virtio-rng-pci,rng=rng0 \
  > console.log 2>&1 < /dev/null &
for _ in $(seq 1 90); do
  grep -q "CAPTURE DONE" console.log 2>/dev/null && break
  sleep 1
done
kill "$(cat output/qemu.pid)" 2>/dev/null
wait "$RECV" 2>/dev/null
grep -E "rtdemo (good|bad)|--- capture|kevpush|CAPTURE DONE|tracelogger" console.log | tail -14
ls -la "$OUT"
