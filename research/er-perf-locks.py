#!/usr/bin/env python3
"""Reads a perf-*.data recording (all game threads, call graphs) and shows, per
thread, how much time goes to Wine's critical sections and who calls them.
ntdll.dll addresses are named from Proton's ntdll.dll symbol table.

  er-perf-locks.py logs/perf-....data [ntdll.dll]
"""
import bisect
import collections
import os
import re
import subprocess
import sys

NTDLL = sys.argv[2] if len(sys.argv) > 2 else (
    os.path.expanduser("~/.local/share/Steam/compatibilitytools.d/proton-cachyos-slr-er/files/lib/wine/x86_64-windows/ntdll.dll"))

syms = sorted((int(a, 16), n) for a, t, n in
              (l.split()[:3] for l in subprocess.run(["nm", NTDLL], capture_output=True, text=True).stdout.splitlines()
               if len(l.split()) >= 3) if t in "tT")
addrs = [a for a, _ in syms]


IMAGE_BASE = 0x170000000  # ntdll.dll's preferred base, which nm's addresses use
# Wine may load it elsewhere: the real base is mapping start - file offset (RVA == file offset).
mmaps = subprocess.run(["perf", "script", "-i", sys.argv[1], "--show-mmap-events", "-F", "ip"],
                       capture_output=True, text=True, errors="replace").stdout
m = re.search(r"\[(0x[0-9a-f]+)\((0x[0-9a-f]+)\) @ (0x[0-9a-f]+|0) .*ntdll\.dll", mmaps)
load_base = int(m.group(1), 16) - int(m.group(3), 16) if m else IMAGE_BASE


def name(ip, dso):
    if "ntdll.dll" in dso:
        ip = ip - load_base + IMAGE_BASE
        i = bisect.bisect_right(addrs, ip) - 1
        return syms[i][1] if i >= 0 else hex(ip)
    if "[JIT]" in dso or "eldenring" in dso:
        return "game code"
    return dso.rsplit("/", 1)[-1]


out = subprocess.run(["perf", "script", "-i", sys.argv[1], "-F", "comm,pid,tid,ip,dso"],
                     capture_output=True, text=True, errors="replace").stdout
head_re = re.compile(r"^\s*(.*?)\s+(\d+)/(\d+)")
per_thread = collections.Counter()
locks = collections.defaultdict(collections.Counter)  # thread -> lock function -> samples
callers = collections.defaultdict(collections.Counter)  # lock function -> caller -> samples
frame = re.compile(r"^\s+([0-9a-f]+) \((.*)\)$")
for block in out.split("\n\n"):
    lines = block.strip("\n").splitlines()
    if not lines:
        continue
    head = head_re.match(lines[0])
    if not head:
        continue
    comm, pid, tid = head.groups()
    if comm == "eldenring.exe" and pid == tid:
        thread = "MAIN THREAD"
    elif comm.startswith(("GXWorker", "FD4JobWorker")):
        thread = re.sub(r"\d+$", "N", comm)  # pool workers summed together
    else:
        thread = comm
    frames = [(int(m.group(1), 16), m.group(2)) for m in map(frame.match, lines[1:]) if m]
    if not frames:
        continue
    per_thread[thread] += 1
    leaf = name(*frames[0])
    if "CriticalSection" in leaf:
        locks[thread][leaf] += 1
        if len(frames) > 1:
            callers[leaf][name(*frames[1])] += 1

print(f"{'thread':<22} {'samples':>8}  in critical sections")
for thread, n in per_thread.most_common(12):
    cs = sum(locks[thread].values())
    detail = ", ".join(f"{k.replace('CriticalSection', 'CS')} {100 * v / n:.1f}%" for k, v in locks[thread].most_common(3))
    print(f"{thread:<22} {n:8d}  {100 * cs / n:5.1f}%  {detail}")
print("\nwho calls them (all threads):")
for fn, c in callers.items():
    total = sum(c.values())
    print(f"  {fn}: " + ", ".join(f"{k} {100 * v / total:.0f}%" for k, v in c.most_common(4)))
