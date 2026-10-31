#!/usr/bin/env bash
# Linux version of the "disable CPU 0" trick. The game must START with core 0,
# so this waits until it's in-game, then moves every game thread off CPU 0/1
# (core 0 and its SMT sibling), leaving IRQs/desktop on core 0.
# Usage: run it, then launch the game.   ./er-affinity.sh [cpu-list]  (default 2-11)

CPUS="${1:-2-11}"
echo "Waiting for eldenring.exe..."
until PID=$(pgrep -f 'eldenring.exe' | head -1) && [[ -n $PID ]]; do sleep 2; done
echo "Found PID $PID, waiting 60s for the game to finish loading..."
sleep 60
taskset -a -p -c "$CPUS" "$PID" >/dev/null && echo "All threads of $PID pinned to CPUs $CPUS"
