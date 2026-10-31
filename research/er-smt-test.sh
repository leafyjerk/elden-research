#!/bin/sh
# Does the main thread's core (CPU 2) get faster with its idle SMT sibling (CPU 3)
# offline? Zen 2 splits the reorder buffer and store queue between a core's two
# threads; "16 chains + filler" is limited by reorder-buffer capacity.
# Run with the game closed: sudo ./er-smt-test.sh
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$(realpath "$0")")" || exit 1
trap 'echo 1 > /sys/devices/system/cpu/cpu3/online' EXIT
echo "CPU 3 online:  $(./membench mlp)"
echo 0 > /sys/devices/system/cpu/cpu3/online
echo "CPU 3 offline: $(./membench mlp)"
