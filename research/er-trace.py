#!/usr/bin/env python3
"""Where a thread sleeps, who wakes it, and how long it then waits to run.

  sudo ./er-trace.py TID [SECONDS]

Records SECONDS (default 5) of the thread's context switches (with kernel call
stacks) and wake-ups, system-wide with perf, then sums up:
  - time asleep by kernel wait site (ntsync, futex, wineserver pipe, ...) and waker
  - wake-up delay: from the waker's wake-up call until the thread runs again
    (IPI, idle exit, scheduler), the part faster idle states would cut
  - same L3: how many wake-ups came from a CPU sharing the thread's L3 cache
    (a handoff across L3s costs ~244 ns per cache line vs ~34 ns within one)
  - wineserver requests by the Wine function making them, from the thread's
    user stack at each wineserver wait (the frame-pointer chain ends at Wine's
    syscall dispatcher, so the game code above it can't be seen from here)
Read-only: no uprobes, which would patch the game's code (Easy Anti-Cheat).
Needs root (sched tracepoints, kernel symbols). The launcher's profile runs the
installed copy, /usr/local/bin/er-trace, as er-trace@TID.service (from --setup).
"""
import bisect
import collections
import glob
import os
import re
import statistics
import subprocess
import sys
import tempfile

KPTR = "/proc/sys/kernel/kptr_restrict"
# Kernel frames that name the wait, searched leaf to root; the first match wins.
SITES = (
    ("ntsync", "ntsync (Win32 event/semaphore)"),
    ("futex", "futex (Wine lock, pthread)"),
    ("pipe_read", "pipe read (wineserver)"),
    ("dma_fence", "GPU fence"),
    ("amdgpu", "amdgpu driver"),
    ("drm_", "DRM driver"),
    ("rwsem_down", "kernel rwsem"),
    ("folio_wait", "page lock/IO"),
    ("io_schedule", "page lock/IO"),
    ("do_sys_poll", "poll"),
    ("do_select", "select"),
    ("ep_poll", "epoll"),
    ("nanosleep", "nanosleep"),
    ("mutex_lock", "kernel mutex"),
)
SCHED = ("__schedule", "schedule", "preempt_schedule", "__cond_resched", "__traceiter_", "[unknown]")
# Wine's request plumbing between the calling function and the pipe read.
PLUMBING = re.compile(r"^(read|__read|__libc_read|wait_reply|read_reply_data|server_call_unlocked|"
                      r"wine_server_call|send_request|pthread_sigmask|0x)")
USER_TOP = 1 << 47  # user-space addresses are below this
EVENT = re.compile(r"^\s*(.*?)\s+\d+\s+\[(\d+)\]\s+(\d+)\.(\d+):\s+sched:(sched_\w+):\s*(.*)$")
SWITCH = re.compile(r"prev_pid=(\d+) .*prev_state=(\S+) ==> .*next_pid=(\d+)")
WAKING = re.compile(r" pid=(\d+) ")


def write(path, value):
    with open(path, "w") as f:
        f.write(value)


def l3_groups():
    """CPU -> the list of CPUs sharing its L3 cache (online CPUs only)."""
    return {int(re.search(r"cpu(\d+)/", p)[1]): open(p).read().strip()
            for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cache/index3/shared_cpu_list")}


def events(lines):
    """perf script lines -> (comm, cpu, time ns, event, trace text, frame names leaf
    first, user-space frame addresses leaf first)."""
    ev = None
    for line in lines:
        m = EVENT.match(line)
        if m:
            if ev:
                yield ev
            comm, cpu, sec, frac, name, trace = m.groups()
            ev = (comm, int(cpu), int(sec) * 10**9 + int(frac.ljust(9, "0")[:9]), name, trace, [], [])
        elif ev and line[:1].isspace() and line.strip():
            addr, _, sym = line.strip().partition(" ")
            ev[5].append(sym.strip() or "[unknown]")
            if re.fullmatch(r"[0-9a-f]+", addr) and int(addr, 16) < USER_TOP:
                ev[6].append(int(addr, 16))
    if ev:
        yield ev


def site(state, chain):
    if state.startswith("R"):
        return "preempted (runnable)"
    fault = " in page fault" if any("exc_page_fault" in f for f in chain) else ""
    for f in chain:
        for pattern, label in SITES:
            if pattern in f:
                return label + fault
    return next((f for f in chain if not f.startswith(SCHED)), "?") + fault


def waker(comm, chain):
    """The waking thread's name without its number (FD4JobWorker:3 -> FD4JobWorker);
    interrupt-context wake-ups by what fired."""
    if any("hrtimer_wakeup" in f for f in chain):
        return "timer"
    if any("dma_fence" in f for f in chain):
        return "GPU fence signal"
    return re.sub(r"[:/]\d.*$", "", comm) or comm


def analyze(evs, tid, l3=None):
    """-> {(site, waker): [(asleep ns, wake-up delay ns, waker on the same L3 or None,
    the thread's user frame addresses when it went to sleep)]}, ns preempted, ns spanned."""
    l3 = l3 or {}
    rows = collections.defaultdict(list)
    preempted = 0
    cur = None    # current off-CPU stretch: [switched out at, site, woken at, waker, waker's CPU, user frames]
    early = None  # wake-up that arrived while the thread was still switching out
    t0 = t1 = None
    for comm, cpu, t, name, trace, chain, ustack in evs:
        t0, t1 = t0 or t, t
        if name == "sched_waking":
            m = WAKING.search(trace)
            if m and int(m[1]) == tid:
                if cur and cur[2] is None:
                    cur[2:5] = [t, waker(comm, chain), cpu]
                elif not cur:
                    early = (t, waker(comm, chain), cpu)
            continue
        m = SWITCH.search(trace)
        if not m:
            continue
        prev, state, nxt = int(m[1]), m[2], int(m[3])
        if prev == tid:
            cur = [t, site(state, chain), None, None, None, tuple(ustack[:16])]
            if early and t - early[0] < 20_000 and not state.startswith("R"):
                cur[2:5] = [t, early[1], early[2]]
            early = None
        elif nxt == tid and cur:
            if cur[1].startswith("preempted"):
                preempted += t - cur[0]
            else:
                woken = t if cur[2] is None else cur[2]
                near = None if cur[4] not in l3 or cpu not in l3 else l3[cur[4]] == l3[cpu]
                rows[(cur[1], cur[3] or "?")].append((max(0, woken - cur[0]), max(0, t - woken), near, cur[5]))
            cur = None
    return rows, preempted, (t1 - t0) if t0 else 0


def requester(ustack, name):
    """The Wine function behind a wineserver wait, from the sleeping thread's user
    frames: the innermost non-plumbing Wine function, plus the Nt* entry point the
    game called if that's a different function (get_inproc_sync < NtWaitForSingleObject)."""
    wine = [f.rsplit(" (", 1)[0] for f in map(name, ustack) if f.endswith(("(ntdll.so)", "(win32u.so)"))]
    wine = [f for f in wine if not PLUMBING.match(f)]
    if not wine:
        return "?"
    nt = [f for f in wine if f.startswith("Nt")]
    return wine[0] + (f" < {nt[-1]}" if nt and nt[-1] != wine[0] else "")


def report(rows, preempted, span, name=None):
    n = sum(map(len, rows.values()))
    if not n or not span:
        print("er-trace: no sleeps recorded")
        return
    asleep = lambda v: sum(x[0] for x in v)
    pct = lambda ns: 100 * ns / span
    delays = sorted(x[1] for v in rows.values() for x in v)
    print(f"main thread sleeps (er-trace, {span / 1e9:.1f}s): {n * 1e9 / span:.0f}/s, "
          f"{pct(sum(map(asleep, rows.values()))):.1f}% of the time asleep, "
          f"{pct(sum(delays)):.1f}% woken but not yet running, {pct(preempted):.1f}% preempted")
    sites = collections.defaultdict(list)
    for (s, _), v in rows.items():
        sites[s] += v
    print("  asleep by wait site: " + ", ".join(
        f"{s} {pct(asleep(v)):.1f}%" for s, v in sorted(sites.items(), key=lambda kv: -asleep(kv[1]))[:6]))
    print(f"  {'slept in':<32} {'woken by':<18} {'per s':>6} {'% time':>7} {'avg us':>7} "
          f"{'median':>7} {'<20us':>6} {'wake delay us':>14} {'same L3':>8}")
    for (s, w), v in sorted(rows.items(), key=lambda kv: -asleep(kv[1]))[:12]:
        sl = [x[0] for x in v]
        near = [x[2] for x in v if x[2] is not None]
        print(f"  {s:<32} {w:<18} {len(v) * 1e9 / span:6.0f} {pct(sum(sl)):6.1f}% "
              f"{statistics.mean(sl) / 1e3:7.1f} {statistics.median(sl) / 1e3:7.1f} "
              f"{100 * sum(x < 20_000 for x in sl) / len(sl):5.0f}% {statistics.mean(x[1] for x in v) / 1e3:14.1f} "
              + (f"{100 * sum(near) / len(near):7.0f}%" if near else f"{'-':>8}"))
    q = lambda p: delays[min(len(delays) - 1, int(p * len(delays)))] / 1e3
    print(f"  wake-up delay (woken -> running): avg {statistics.mean(delays) / 1e3:.1f} us, "
          f"median {q(.5):.1f}, p90 {q(.9):.1f}, p99 {q(.99):.1f}")
    ws = sites.get("pipe read (wineserver)")
    if name and ws:
        by = collections.Counter(requester(x[3], name) for x in ws)
        print(f"  wineserver requests by caller ({len(ws) * 1e9 / span:.0f}/s): " + ", ".join(
            f"{f} {c * 1e9 / span:.0f}/s" for f, c in by.most_common(10)))


def resolver(tid):
    """Code address in the thread's process -> "function (library)", from the
    libraries' symbol tables (code segments load at their file offsets). Reads the
    memory map now; symbols are looked up later, so it still works after the
    process has exited (the game runs in Steam's container, which sees the
    host's /usr as /run/host/usr)."""
    spans = []
    for line in open(f"/proc/{tid}/maps"):
        f = line.split()
        if len(f) > 5 and "x" in f[1] and f[5].startswith("/"):
            start, end = (int(x, 16) for x in f[0].split("-"))
            spans.append((start, end, start - int(f[2], 16), f[5]))
    tables = {}

    def table(path):
        if path not in tables:
            syms = []
            file = next((p for p in (f"/proc/{tid}/root{path}", path, path.removeprefix("/run/host"))
                         if os.path.exists(p)), path)
            for flags in ([], ["-D"]):  # full symbol table, else the dynamic one
                out = subprocess.run(["nm", "--defined-only", *flags, file],
                                     capture_output=True, text=True).stdout
                syms = sorted((int(a, 16), n) for a, t, n in
                              (l.split()[:3] for l in out.splitlines() if len(l.split()) >= 3) if t in "tTwW")
                if syms:
                    break
            tables[path] = syms
        return tables[path]

    def name(addr):
        for start, end, base, path in spans:
            if start <= addr < end:
                syms = table(path)
                i = bisect.bisect_right(syms, (addr - base, "\U0010ffff")) - 1
                fn = re.sub(r"\.(constprop|part|isra|cold)[.\d]*|@.*", "", syms[i][1]) if i >= 0 else hex(addr - base)
                return f"{fn} ({path.rsplit('/', 1)[-1]})"
        return hex(addr)
    return name


def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        sys.exit(__doc__)
    tid, secs = int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "5"
    name = resolver(tid)  # the memory map now, while the process surely exists
    old = open(KPTR).read().strip()
    err = tempfile.TemporaryFile(mode="w+")
    # Record to RAM first, then parse: piped straight into perf script, the parsing
    # couldn't keep up, perf's buffers overflowed and ~40% of the events were lost.
    data = tempfile.NamedTemporaryFile(dir="/run", prefix="er-trace-", suffix=".data")
    try:
        if old == "2":  # 2 hides kernel addresses even from root, and perf needs them for the stacks
            write(KPTR, "1")
        subprocess.run(
            ["perf", "record", "-q", "-N", "-a", "-T", "--sample-cpu", "-m", "8M", "-o", data.name, "-g",
             "-e", "sched:sched_switch", "--filter", f"prev_pid == {tid} || next_pid == {tid}",
             "-e", "sched:sched_waking", "--filter", f"pid == {tid}",
             "--", "sleep", secs], stderr=err)
        script = subprocess.Popen(
            ["perf", "script", "-i", data.name, "--ns", "--max-stack", "48", "-F", "comm,tid,cpu,time,event,trace,ip,sym"],
            stdout=subprocess.PIPE, stderr=err, text=True, errors="replace")
        rows, preempted, span = analyze(events(script.stdout), tid, l3_groups())
        script.wait()
    finally:
        data.close()
        if old == "2":
            write(KPTR, old)
    report(rows, preempted, span, name)
    err.seek(0)
    for line in err:  # perf's complaints: all of them if nothing came through, else lost events
        if not rows or re.search(r"lost", line, re.I):
            print("  perf:", line.rstrip())


if __name__ == "__main__":
    main()
