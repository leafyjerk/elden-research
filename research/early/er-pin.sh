#!/usr/bin/env bash
# Give Elden Ring's main thread a physical core to itself.
# The main thread (TID == PID) goes on CPU 2; its SMT sibling (CPU 3) is left
# idle so no worker shares the core with it. Every other game thread is kept
# off CPUs 2-3. Run while in-game; re-run after a restart.
# Usage: ./er-pin.sh [on|off]   (default on)
# Topology (BC-250): siblings 0-1 2-3 4-5 | 6-7 8-9 10-11, L3 split at 5/6.

MAIN_CPU=2
REST_CPUS=0,1,4-11

PID=$(pgrep -f 'eldenring.exe' | head -1)
[[ -z $PID ]] && { echo "eldenring.exe not running"; exit 1; }

if [[ ${1:-on} == off ]]; then
  taskset -a -p -c 0-11 "$PID" >/dev/null && echo "All threads of $PID back on CPUs 0-11"
  exit
fi

n=0
for t in /proc/"$PID"/task/*; do
  tid=${t##*/}
  if [[ $tid == "$PID" ]]; then
    taskset -p -c "$MAIN_CPU" "$tid" >/dev/null
  else
    taskset -p -c "$REST_CPUS" "$tid" >/dev/null 2>&1 && ((n++))
  fi
done
echo "Main thread $PID -> CPU $MAIN_CPU alone (CPU 3 idle); $n other threads -> CPUs $REST_CPUS"
