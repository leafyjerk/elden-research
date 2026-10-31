#!/usr/bin/env python3
"""Samples what a thread is doing for N seconds: running, or which kernel
function it's sleeping in (wchan). Shows what the game's main thread waits on
in the part of each frame it isn't running.

  er-waits.py PID TID SECONDS
"""
import collections
import sys
import time

pid, tid, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
task = f"/proc/{pid}/task/{tid}"
counts = collections.Counter()
end = time.monotonic() + secs
while time.monotonic() < end:
    try:
        with open(task + "/stat") as f:
            stat = f.read()
        if stat[stat.rindex(")") + 2] == "R":
            what = "running"
        else:
            with open(task + "/wchan") as f:
                what = f.read().strip() or "?"
    except OSError:
        break
    counts[what] += 1
    time.sleep(0.002)
n = sum(counts.values())
for what, c in counts.most_common(10):
    print(f"  {100 * c / n:5.1f}%  {what}")
