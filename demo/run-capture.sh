#!/usr/bin/env bash
# Build, capture selected modes, verify transfers, then restore the image snippets.
# Usage: demo/run-capture.sh [output dir] [mode ...]
# Optional: KEVSCOPE_VM, QNX_SDP_ENV, KEVSCOPE_BUILD_TIMEOUT, KEVSCOPE_CAPTURE_TIMEOUT.
set -eo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/traces}"
MODES=("${@:2}")
if [[ ${#MODES[@]} == 0 ]]; then MODES=(bad good mutex-bad mutex-good ipc-bad ipc-good); fi
declare -A SEEN_MODES=()
for mode in "${MODES[@]}"; do
    case "$mode" in bad|good|mutex-bad|mutex-good|ipc-bad|ipc-good) ;;
        *) echo "unknown capture mode: $mode" >&2; exit 2 ;;
    esac
    [[ -z "${SEEN_MODES[$mode]:-}" ]] || { echo "duplicate capture mode: $mode" >&2; exit 2; }
    SEEN_MODES[$mode]=1
done
EXPECTED_FILES=$((${#MODES[@]} * 2))
VM="${KEVSCOPE_VM:-$HOME/qnxvm}"
BUILD_TIMEOUT="${KEVSCOPE_BUILD_TIMEOUT:-180}"
CAPTURE_TIMEOUT="${KEVSCOPE_CAPTURE_TIMEOUT:-180}"
for value in "$BUILD_TIMEOUT" "$CAPTURE_TIMEOUT"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo "timeouts must be positive whole seconds" >&2; exit 2; }
done
source "${QNX_SDP_ENV:-$HOME/qnx800/qnxsdp-env.sh}" >/dev/null
set -u
for tool in qcc mkqnximage qemu-system-x86_64 python3 timeout; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done
[[ -d "$VM/local" ]] || { echo "initialize a dedicated QEMU image with mkqnximage first: $VM" >&2; exit 1; }
VM="$(cd "$VM" && pwd)"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
LOCK="$VM/.kevscope-capture.lock"
mkdir "$LOCK" 2>/dev/null || { echo "capture already owns this VM, or a stale lock needs inspection: $LOCK" >&2; exit 1; }
RUN_DIR=""
QEMU_PID=""
RECV_PID=""
SNIPPETS="$VM/local/snippets"

cleanup() {
    local result=$? keep=0 file
    trap - EXIT
    set +e
    for child in "$RECV_PID" "$QEMU_PID"; do
        if [[ -n "$child" ]]; then kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi
    done
    if [[ -n "$RUN_DIR" ]]; then
        for file in data_files.custom post_start.custom; do
            if [[ -f "$RUN_DIR/$file.installed" ]]; then
                if cmp -s "$SNIPPETS/$file" "$RUN_DIR/$file.installed"; then
                    rm -f -- "$SNIPPETS/$file"
                    if [[ -e "$RUN_DIR/$file.original" || -L "$RUN_DIR/$file.original" ]]; then
                        mv -- "$RUN_DIR/$file.original" "$SNIPPETS/$file"
                    fi
                elif [[ -e "$RUN_DIR/$file.original" || -L "$RUN_DIR/$file.original" ]]; then
                    echo "snippet changed during capture; original preserved at $RUN_DIR/$file.original" >&2
                    keep=1
                fi
            fi
        done
        rm -f -- "$RUN_DIR/receiver.ready" "$RUN_DIR/qemu.pid" "$RUN_DIR/data_files.custom.installed" "$RUN_DIR/post_start.custom.installed"
        if [[ "$keep" == 0 ]]; then rmdir -- "$RUN_DIR" 2>/dev/null; fi
    fi
    rmdir -- "$LOCK" 2>/dev/null
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
RUN_DIR="$(mktemp -d "$OUT/.kevscope-run.XXXXXX")"
mkdir -p "$VM/payload" "$SNIPPETS"
qcc -Vgcc_ntox86_64 -O2 -Wall "$REPO/demo/rtdemo.c" -o "$VM/payload/rtdemo"
qcc -Vgcc_ntox86_64 -O2 -Wall "$REPO/demo/kevpush.c" -o "$VM/payload/kevpush" -lsocket
tr -d '\r' < "$REPO/demo/capture.sh" > "$VM/payload/capture.sh"

cat > "$RUN_DIR/data_files.custom.installed" <<EOF
[uid=0 gid=0 perms=755] var/kevscope/rtdemo=$VM/payload/rtdemo
[uid=0 gid=0 perms=755] var/kevscope/kevpush=$VM/payload/kevpush
[uid=0 gid=0 perms=755] var/kevscope/capture.sh=$VM/payload/capture.sh
EOF
cat > "$RUN_DIR/post_start.custom.installed" <<EOF
/data/var/kevscope/capture.sh ${MODES[*]}
EOF
for file in data_files.custom post_start.custom; do
    if [[ -e "$SNIPPETS/$file" || -L "$SNIPPETS/$file" ]]; then mv -- "$SNIPPETS/$file" "$RUN_DIR/$file.original"; fi
    cp -- "$RUN_DIR/$file.installed" "$SNIPPETS/$file"
done

cd "$VM"
if ! timeout "$BUILD_TIMEOUT" mkqnximage --noprompt --build > "$OUT/build.log" 2>&1; then
    tail -40 "$OUT/build.log" >&2
    echo "image build failed or timed out; QEMU was not started" >&2
    exit 1
fi
python3 "$REPO/demo/recv.py" "$OUT" "$EXPECTED_FILES" --timeout "$CAPTURE_TIMEOUT" --ready-file "$RUN_DIR/receiver.ready" > "$OUT/receiver.log" 2>&1 &
RECV_PID=$!
for ((attempt=0; attempt<100; attempt++)); do
    [[ -s "$RUN_DIR/receiver.ready" ]] && break
    kill -0 "$RECV_PID" 2>/dev/null || break
    sleep 0.05
done
if [[ ! -s "$RUN_DIR/receiver.ready" ]]; then
    cat "$OUT/receiver.log" >&2
    echo "receiver could not bind localhost:15610; QEMU was not started" >&2
    exit 1
fi
: > "$OUT/console.log"
qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 1G -nographic -serial mon:stdio \
  -pidfile "$RUN_DIR/qemu.pid" -kernel output/ifs.bin -drive file=output/disk-qemu.vmdk,if=ide,id=drv0 \
  -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
  -object rng-random,filename=/dev/urandom,id=rng0 -device virtio-rng-pci,rng=rng0 \
  > "$OUT/console.log" 2>&1 < /dev/null &
QEMU_PID=$!
done_capture=0
for ((second=0; second<CAPTURE_TIMEOUT; second++)); do
    if grep -q "=== CAPTURE DONE ===" "$OUT/console.log"; then done_capture=1; break; fi
    kill -0 "$QEMU_PID" 2>/dev/null || break
    if [[ -n "$RECV_PID" ]] && ! kill -0 "$RECV_PID" 2>/dev/null; then
        if ! wait "$RECV_PID"; then RECV_PID=""; cat "$OUT/receiver.log" >&2; exit 1; fi
        RECV_PID=""
    fi
    sleep 1
done
if [[ "$done_capture" != 1 ]]; then
    tail -30 "$OUT/console.log" >&2
    cat "$OUT/receiver.log" >&2
    echo "guest exited, transfer failed, or capture timed out" >&2
    exit 1
fi
if [[ -n "$RECV_PID" ]] && ! wait "$RECV_PID"; then cat "$OUT/receiver.log" >&2; RECV_PID=""; exit 1; fi
RECV_PID=""
grep -E "rtdemo (mutex-|ipc-)?(good|bad)|--- capture|kevpush|CAPTURE DONE" "$OUT/console.log"
echo "All $EXPECTED_FILES files verified in $OUT."
