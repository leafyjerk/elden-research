# The research: Elden Ring at 60 FPS on a BC-250

This is everything we tried, in order: what we measured, what worked, what didn't, and why. The tool in this repository packages the parts that worked. Dates are 2026-10-25 to 2026-10-31. Every number comes from a MangoHud frame log or from a measurement tool in [research/](research/).

**Contents**

1. [Summary](#1-summary)
2. [The machine](#2-the-machine)
3. [How we measured](#3-how-we-measured)
4. [What worked, in order](#4-what-worked-in-order)
5. [What didn't work or didn't matter](#5-what-didnt-work-or-didnt-matter)
6. [Deep dive: where the main thread's time goes](#6-deep-dive-where-the-main-threads-time-goes)
7. [Deep dive: the two Proton patches](#7-deep-dive-the-two-proton-patches)
8. [The overclock](#8-the-overclock)
9. [Open ends](#9-open-ends)
10. [Tools](#10-tools)

## 1. Summary

- **The game is limited by one thread.** Elden Ring's main thread does most of a frame's work serially. On the BC-250 it was ~90% busy, while the GPU sat at 45–60% load. Graphics settings therefore don't change the FPS. Everything that helped either gave that thread more CPU, or removed things that made it wait.
- **The order of the big wins:**
  1. 8 cores instead of 6 (BIOS)
  2. a thread layout that respects the chip's two core clusters
  3. memory tuning that ended swap hitches
  4. a 3.8 GHz overclock while the game runs
  5. the scx_lavd scheduler
  6. the SMT sibling offline
  7. CachyOS's Proton
  8. wineserver pinned next to the main thread
  9. two patches to Proton's `ntdll.so` that take ~84% of the main thread's Wine server round trips away
- **Result:** at the hard benchmark spot, from 49.6 FPS average with 50.6% of frames over 20 ms, to 59.96 average (median 60.00, the cap) with 0.2% over 20 ms, and 97% of frames within the 16.7 ms budget plus 0.8 ms.
- **Measuring was half the work.** Near the cap, the average stops moving, the scene drifts between runs, and single hitches dominate the 1% lows. Section 3 describes the method that made 1–2% effects visible.

## 2. The machine

| | |
|---|---|
| Board | AMD BC-250: a PS5-derived APU ("Ariel" / Cyan Skillfish) on a mining blade |
| CPU | Zen 2, 8 cores / 16 threads after the BIOS unlock (6 cores stock), in two clusters (CCX) of 4 cores, each with its own 4 MB L3 |
| Memory | 16 GB GDDR6 shared by CPU and GPU (~14 GiB usable, 512 MB VRAM carve-out); RAM latency 145 ns |
| BIOS | Modded P5.00 with the 8-core unlock |
| Cooling | Stock heatsink with two Arctic P12 Pro fans in push-pull, a VRM heatsink, in a case |
| OS | CachyOS, kernel 7.0 (CachyOS), KDE Plasma; the box normally runs on a TV in gaming mode |
| Game | Elden Ring (Steam), launched through Easy Anti-Cheat; it caps at 60 FPS |

Things about this chip that mattered:

| Fact | Value | Why it matters |
|---|---|---|
| L1 / L2 / L3 latency | 1.2 / 3.5 / 11–14 ns | Normal Zen 2 |
| RAM latency, 2 MB pages | ~145 ns | Same as the AMD 4700S (same chip). High for a desktop, so cache misses hurt. |
| RAM latency, 4 KB pages, 1 GB working set | 201 ns | **+38% from page-table walks**, so huge pages matter |
| Thread-to-thread hand-off, same CCX / other CCX | 34 / **244 ns** | Threads that talk to the main thread belong on its CCX |
| Read bandwidth, 1 thread / 1 CCX / both | 19 / 33 / 64 GB/s | Fine |
| CPU frequency driver | none | Cores run at ~3.49 GHz under load (TSC 3.19 GHz); only the SMU sets clocks |
| CPU idle driver | none | Idle is plain HLT: there are no deep C-states adding wake-up latency |
| Default interrupts | USB on CPU 8, GPU (~1,700/s) on CPU 12 | Both landed on the render cluster |
| Max clock (stock) | 3.5 GHz single core; 3.33–3.36 GHz with all 16 threads loaded | Heat-limited on this cooler: 85–90 °C within 4 s at full load |

CPU numbering: CPUs 0–7 are CCX 0 and 8–15 are CCX 1, and SMT siblings are pairs (0–1, 2–3, ...). The main thread runs on CPU 2 (core 1).

## 3. How we measured

**The tools.** MangoHud logs every frame time to a CSV, and [research/er-compare.py](research/er-compare.py) turns the CSV into the average, median, 1%/0.1% lows, late-frame shares and 20-second windows. The launcher's research profile (`ER_PROFILE=1`) adds perf counters, a module/function profile and a wait trace of the main thread (section 10).

**The method, as it ended up:**

1. **One spot:** load the save at the Church of Elleh Site of Grace and **don't touch the controller**. Hands-off runs cut the scene variance.
2. **One save:** the game rewrites its save on every quit, so each run starts with a different time of day and weather. With identical settings, the main thread's work per frame swung by 7–9% (33.8M vs 31.1M instructions per frame), which is as big as the effects being tested. The fix: snapshot the save once, and restore it before every run (`./elden-research bench snapshot` / `restore`).
3. **A-B-A-B:** alternate the two settings, two pairs. A change counts only if it wins both pairs.
4. **The right metric.** Near the 60 cap the average compresses (59.7 vs 59.9 says little), and the 1% low comes from single hitches. Instead we use **frames over 17.5 ms** (a frame that missed its 16.7 ms slot) and the time spent past 16.7 ms.
5. **Logged settings.** Every launch logs its `ER_*` settings and the Proton build, so each run can be checked from the log alone.

Run-to-run noise without these steps was about ±1 FPS, and ±2–3 FPS per 20 s window within a run.

## 4. What worked, in order

### 4.1 6 cores: first tweaks (2026-10-25)

The board started with 6 cores, at **~50 FPS average, 1% lows of 32–39 and 12.5% of frames over 20 ms** in an open-world spot.

- A lean MangoHud config. The Goverlay default polled ~60 statistics, shell commands, network, IO and battery every frame.
- `research/early/er-affinity.sh`: the Linux version of the Windows "disable CPU 0" trick. The game has to *start* with core 0 available, so after it loads, every game thread moves off core 0, which is left for interrupts and the desktop.
- `research/early/er-pin.sh`: the main thread (the thread whose ID equals the process ID) alone on CPU 2, with its SMT sibling idle.
- `research/early/er-monitor.sh`: GPU clock and the busiest threads once a second. It showed the game is main-thread-bound, not GPU-bound.

Result: **54.8 FPS, 4.3% of frames over 20 ms.**

### 4.2 8 cores (BIOS)

A modded P5.00 BIOS with the core unlock: all 8 cores / 16 threads, permanently. Open-world spot: **58.1 FPS, 1% low 46.2, 2.3% over 20 ms**. That's the biggest single step, and it's outside this tool (README → 8 cores).

The benchmark then moved to the harder Church of Elleh spot. With only the main thread pinned (main on 2, everything else on 4–15), the baseline there was **49.6 FPS, 1% low 40.3, 50.6% of frames over 20 ms**.

### 4.3 Thread layout: respect the two clusters (2026-10-29)

| Layout | Avg | 1% low | >20 ms |
|---|---|---|---|
| Baseline: main 2, everything else 4–15 | 49.6 | 40.3 | 50.6% |
| `ccx`: main 8, render 10–15, the rest 2–7 | 45.9 | 32.6 | 82.4% ❌ |
| **`split`**: main 2, render threads 8–15, the rest 4–7, interrupts on 0–1 | **52.6** | 38.1 | **21.1%** |

- **Why `ccx` lost:**
  - the main thread shared its L3 with the busy render threads
  - 8 render threads were crammed onto 6 CPUs
  - CPU 8 takes the USB interrupts
- **Why `split` won:**
  - The render workers (7× GXWorker at ~28% each, plus vkd3d_queue) get a CCX of their own.
  - The light job threads that hand work to and from the main thread (FD4JobWorker, Havok, EzWorkPool...) share its CCX, so their hand-offs cost 34 ns instead of 244 ns.
  - The GPU and USB interrupts and the kernel's memory threads (kswapd, kcompactd, khugepaged) move to CPUs 0–1 at boot (`er-tune.service`).
- The launcher re-checks the layout every 5 s, because threads that the main thread creates inherit CPU 2.

The `split` run still had a burst of hitches up to 786 ms. After the first 50 s it beat the baseline on everything (51.6 vs 49.5 avg, worst frame 45 vs 65 ms). That burst led to the next step.

### 4.4 Memory: end the swap hitches (2026-10-29)

**The cause:** while the game loaded, RAM use rose from 9.8 to 11.3 GB and swap from 1.0 to 2.5 GB. The kernel reclaimed memory mid-game, and game threads stalled in direct reclaim. VS Code (~1 GB) and Steam's web helper (~670 MB) were part of the pressure.

**The fixes:**

- `vm.swappiness 10`. CachyOS's zram udev rule sets 150 after sysctl.d has run, so a same-named rule in /etc overrides it.
- `vm.watermark_scale_factor 200`: kswapd starts ~280 MB earlier, so game threads rarely reclaim themselves.
- At launch, other apps' memory cgroups are pushed to zram (`memory.reclaim`), skipping Steam/the game and the desktop shell. Then memory is compacted (`er-boost.service`), so the game's allocations get 2 MB pages.
- khugepaged went from the slow default (4,096 pages per 10 s) to 32,768 pages per 1 s, collapsing only mostly-filled ranges (`max_ptes_none 128`) so memory use doesn't balloon.

| Run (Elleh, `split`) | Avg | 1% low | 0.1% low | Worst frame |
|---|---|---|---|---|
| Before | 52.6 | 38.1 | – | 786 ms |
| **Memory tuning** | 52.2 | **43.2** | **34.6** | **49 ms** |

Huge pages then covered 61–68% of the game's memory (3.2–3.6 of ~5.1 GB). The coverage falls to ~51% after many game runs without a reboot, because memory fragments.

### 4.5 Display: 60 Hz while playing

The desktop ran at 74.91 Hz. With a 60 FPS cap, that shows every 4th frame twice: visible judder that no FPS counter shows. The launcher switches the display to 60 Hz while the game runs and back afterwards (KDE's `kscreen-doctor`). `ER_FPS=50` is an alternative for scenes that can't hold 60: a 50 FPS cap with the display at 50 Hz shows every frame exactly once.

### 4.6 The overclock: 3.8 GHz while the game runs (2026-10-29)

The BC-250's CPU clocks are set by the SMU (the chip's management firmware), through a mailbox in PCI config space. The bc250-collective's `bc250_smu_oc` documented the messages: max boost clock (`0x8F`), voltage curve scale (`0x50`), and turning off an extra voltage margin (`0x9A`). [lib/er-smu.py](lib/er-smu.py) is a minimal re-implementation. It pauses the GPU governor, which uses the same registers, and holds a lock for the whole session.

| Run (Elleh) | Avg | Median | 1% low | >20 ms | Peak CPU temp |
|---|---|---|---|---|---|
| Stock (two runs) | 52.2 / 53.4 | 52.6 / 54.4 | 43.2 / 42.1 | 23.8% / 18.6% | 68 °C |
| **3.8 GHz, curve scale −20** | **54.6** | **55.1** | **45.9** | **7.2%** | 72–73 °C |

CPU 2 held 3,792 MHz the whole time. The voltage model puts 3.8 GHz at scale −20 at ~1,231 mV: an undervolted curve. At scale 0 it would ask for ~1,360 mV. Section 8 covers how we got there, and why the obvious stress-test approach would have rejected this setting.

The user wanted no other game overclocked (heat), so the OC became a start/stop service. The launcher starts it ~7 s before the game, and the watcher stops it at exit. The watcher first ran inside Steam's process tree, and Steam's Stop button killed it, which left the OC on. It now runs as its own user service (`systemd-run --user`), outside that tree.

### 4.7 Scheduler: scx_lavd (2026-10-29)

| Run (hands-off, 3.8 GHz) | Avg | 1% low | 0.1% low | >20 ms | Main-thread context switches/s |
|---|---|---|---|---|---|
| **scx_lavd** | 55.6 | **48.2** | **38.2** | **2.3%** | 12.7K |
| Kernel scheduler (EEVDF) | 55.6 | 42.9 | 35.4 | 7.9% | 19.5K |

Same average, much better tail. Under the kernel's scheduler the main thread was runnable but not running ~8% of the time: other tasks landed on CPU 2. (The busy time from `/proc` was 81.5%, but sampling saw it in state R 90% of the time.) The launcher starts scx_lavd through scx_loader while the game runs.

### 4.8 The main thread alone on its core (2026-10-30)

Zen 2 statically splits some of a core's resources between its two SMT threads, even when one of them is idle. `membench mlp` measures how many memory loads a core can keep in flight:

| CPU 3 (the main thread's sibling) | 1 chain | 16 chains | 16 chains + filler |
|---|---|---|---|
| online | 148.0 ns | 9.7 ns | 26.2 ns |
| **offline** | 147.9 ns | 9.7 ns | **21.9 ns** |

The filler case (limited by the reorder buffer) is 16% faster with the sibling offline. In-game:

| Run | Avg | 1% low | 0.1% low | >20 ms | Main-thread IPC |
|---|---|---|---|---|---|
| Sibling online | 55.6 | 48.2 | 38.2 | 2.3% | 0.630 |
| **Sibling offline** | **56.6** | **50.4** | **42.5** | **0.8%** | **0.673 (+7%)** |

Every 20 s window was higher. This was a single pair, measured before we knew about save drift (section 3), but the mechanism is confirmed by membench, and it stayed in every later run. The launcher takes the sibling offline before scx_lavd starts, because sched_ext schedulers don't take CPU hotplug well, and brings it back after the game exits.

### 4.9 Proton: CachyOS beats Experimental (2026-10-30)

A-B-A-B with the restored save:

| Run | Proton | Avg | 1% low | Frames >17.5 ms | Time past 16.7 ms |
|---|---|---|---|---|---|
| 1 | Experimental 11.0-20261027b | 58.07 | 46.9 | 31.5% | 3.67% |
| 2 | **CachyOS 11.0-20261015-slr** | 59.32 | 47.9 | **11.8%** | 1.96% |
| 3 | Experimental | 58.98 | 50.7 | 19.8% | 2.53% |
| 4 | **CachyOS** | 59.51 | 49.1 | **11.7%** | 1.89% |

Both CachyOS runs beat both Experimental runs on late frames, including the Experimental run that came after a CachyOS run. It became the base for the patches.

### 4.10 wineserver next to the main thread (2026-10-30)

The first wait trace (section 6) showed the main thread making 7,000–7,600 round trips a second to `wineserver`, the separate process that implements much of the Windows kernel under Wine. Unpinned, wineserver ran on the other CCX 77% of the time, so every request and reply crossed clusters.

| Run | wineserver | Avg | 1% low | Frames >17.5 ms | One round trip (asleep + wake delay) | Same L3 |
|---|---|---|---|---|---|---|
| 1 | anywhere | 59.04 | 47.5 | 19.9% | 5.4 + 6.6 = 12.0 µs | 23% |
| 2 | **CPUs 0–1, 4–7** | **59.86** | **51.3** | **4.6%** | 4.2 + 4.0 = **8.2 µs** | 100% |
| 3 | anywhere | 59.47 | 48.2 | 11.8% | 4.7 + 5.9 = 10.6 µs | 53% |
| 4 | **CPUs 0–1, 4–7** | **59.53** | **49.9** | **8.1%** | 3.8 + 4.4 = 8.2 µs | 100% |

It won every metric in both pairs. The pair-2 gap is smaller because in run 3 the unpinned wineserver happened to sit on the right CCX half the time. The median wake-up delay across all the main thread's wake-ups fell from 5.6–6.0 µs to 3.1 µs.

### 4.11 Patched Proton, part 1: spin before sleeping (2026-10-30)

The main thread's waits are short: a wineserver reply after a few µs, a lock or event released within µs. Each sleep costs a context switch and a wake-up (~4 µs measured), longer than many of the waits. Patch 1 lets the main thread, and only it, poll for up to `ER_SPIN` µs before it blocks. It's safe because nothing else is allowed on CPU 2. Section 7 has the details.

| Run (restored save, hands-off) | Spin | Avg | Median | Frames >17.5 ms |
|---|---|---|---|---|
| 1 | 0 | 59.77 | 59.81 | 5.1% |
| 2 | **20 µs** | 59.89 | 59.98 | **4.1%** |
| 3 | 0 | 59.69 | 59.72 | 7.7% |
| 4 | **20 µs** | 59.97 | 60.02 | **5.1%** |

It won both pairs. The spin-20 runs' 20 s windows all sat at 59.9–60.0, against 59.6–59.8 for spin 0. In run 4, 98.9% of the main thread's wineserver replies arrived within the spin.

The late frames were now almost all *just* late: 17.5–18.5 ms was 3.7–7.3% of frames, and 18.5 ms or more ≤0.5%. About 0.5–1 ms more main-thread headroom per frame would fix most of them, and that's what patch 2 targeted.

### 4.12 Patched Proton, part 2: the event pool (2026-10-31)

The patched build's own counters showed what the round trips were. The main thread creates ~3,700 Windows events a second (about 60 per frame) and closes ~3,100 of them, 99% within one frame. They're manual-reset, 77% created signaled, none named. Each create and each close is a wineserver round trip, ~85% of all of them. The main thread almost never waits on these events itself; other threads use them, or nothing does.

Patch 2 moves the round trips off the main thread. A helper thread keeps fresh events ready and does the main thread's closes for it. Section 7 has the design.

| Run (spin 20 in all) | Pool | Avg | Median | Frames >17.5 ms | 1% low | Main-thread round trips/s |
|---|---|---|---|---|---|---|
| 1 | off | 59.95 | 59.96 | 4.8% | 52.40 | 7,324 |
| 2 | **128** | 59.96 | 59.97 | **4.6%** | 52.33 | 3,351 |
| 3 | off | 59.96 | 60.01 | 5.2% | 53.36 | 7,586 |
| 4 | **128** | 59.98 | 60.01 | **2.6%** | 53.94 | 3,563 |

It won both pairs. The first version deferred only 31% of the closes, because its table of pooled handles had 4,096 slots shared by handle index, and long-lived events kept slots busy. With 2^18 slots, the final check run deferred **every** close the main thread made of its own events (1,583,205 deferred against 1,578,868 counted; 0 untracked), and the main thread's round trips fell to **1,216/s**.

**Final check run:** 59.96 avg, median 60.00, 1% low 50.5, **2.8% of frames over 17.5 ms, 0.34% over 18.5 ms, 0.2% over 20 ms**. One 74 ms hitch set the 1% low.

## 5. What didn't work or didn't matter

| Idea | Result | Why |
|---|---|---|
| Lower graphics settings | No FPS change (Low: 51.8 vs High: 52.2; GPU load 42% vs 59%) | The CPU's main thread is the limit, not the GPU |
| `ccx` layout (main + render on one CCX) | 45.9 vs 49.6 FPS, 82% frames >20 ms | L3 sharing with the render threads, too few CPUs for them, the USB interrupt (4.3) |
| Wayland (`PROTON_ENABLE_WAYLAND=1`) on Proton Experimental | Didn't apply | Valve's Experimental ignores it (winex11 still loaded). The X11 path costs ~0.4% anyway (`XGetInputFocus` round trips, ~150/s at ~25 µs). |
| The kernel's own scheduler | Same average, much worse tail (4.7) | Other tasks landed on the main thread's CPU |
| `ER_REST=0-1,4-7` (2 more CPUs for the light job threads) | No clear effect | The main thread's waits barely moved (12.6% → 12.0%). It stayed in the defaults: harmless, and used in every later run. |
| SMU performance profile | Already the fastest at boot | Profile 3 (boot): 145.8 ns / 63.5 GB/s; 2: 252 ns / 36.1 GB/s; 1: 515 ns / 14.2 GB/s. Never use the low profiles while gaming. |
| Stress-test OC search (`er-smu.py oc`, like `bc250-detect`) | Stopped at stock 3.5 GHz | 4 full `stress` cores reach 87–90 °C, where the SMU throttles. The game is far lighter (IPC 0.66) and ran 3.8 GHz at 68–73 °C. Judging an OC by an all-out stress test tests the cooler, not the setting (section 8). |
| `bc250-detect` (16-thread stress) | Aborted at stock from throttling | Same reason |
| A full custom Proton build "for speed" | Rejected before patching | The main thread is ~84% game code, which Proton never touches, and ~7.5% critical sections that cost the same on Windows. A rebuild alone was worth ≤1%. What paid off was targeting the *waiting* (sections 6–7). |
| uprobes on `wine_server_call` to count requests | Removed | A uprobe writes a breakpoint into the game process, which runs under Easy Anti-Cheat. Nothing may write into the game process. |
| Finding which game code creates the 60 events per frame | Not possible from outside | The game has no frame pointers, and Wine's syscall dispatcher switches to a separate stack. An in-process caller log found the creating API (kernelbase `CreateEventExW`), but not reliably the game code above it. The pool didn't need to know. |
| Deep C-states as the cause of slow wake-ups | Ruled out | There's no cpuidle driver: idle is HLT. Even timer wake-ups, with the CPU already awake, showed ~4.6 µs of delay, so it's the scheduler path. |
| Thread placement around the shared locks | Not worth it | The game's critical sections are contended by its own threads (EzWorkPool, FD4, Havok, GX). `split` already keeps most of them on the main thread's CCX, and moving the render threads there costs more L3 than it saves. |
| VRR / FreeSync on the test monitor | Blocked | The monitor (HDMI, FreeSync 48–75 Hz in its EDID) reports `vrr_capable = 0`. On HDMI, amdgpu reads FreeSync through display firmware that this GPU (DCN 2.0.1) doesn't load. It would need a kernel patch, and the target is a TV anyway. |

## 6. Deep dive: where the main thread's time goes

### 6.1 CPU time (perf, 10 s, round 4)

The main thread was 90% busy at IPC 0.66.

| Module | Share of its CPU time |
|---|---|
| Game code (`[JIT]`, eldenring.exe) | 82% |
| `ntdll.dll` (Wine, Windows side) | **9.8%** |
| Mesa RADV (`libvulkan_radeon`) | 2.7% |
| vkd3d-proton `d3d12core` | 1.0% |
| `ntdll.so` (Wine, Unix side) | 1.0% |
| winex11 / win32u | 0.4% |

The `ntdll.dll` time is critical sections: `RtlLeaveCriticalSection` (3.0%), `RtlEnterCriticalSection` (2.5%) and `RtlTryEnterCriticalSection` (0.75%). The hot instructions sit right after the `lock inc/dec` on the lock count, and the spin count is 0. So this isn't spinning; it's the cost of moving the lock's cache line between threads (244 ns across CCXs). [research/er-perf-locks.py](research/er-perf-locks.py) splits it by thread: main 6.2%, EzWorkPool_LOW 10.1%, EzWorkPool_HIGH 6.5%, FD4 2.6%, Havok 2.4%. The callers are game code. Windows pays the same.

Page-table walks: 863K/s, of which 705K/s are 4 KB-page L2 TLB misses, ≈4% of the time at ~55 ns each. That's the case for huge pages.

### 6.2 Waiting (the wait tracer)

[research/er-trace.py](research/er-trace.py) records every sleep of the main thread for 5 s: the kernel stack where it slept, who woke it, and how long it then waited to run. It uses perf sched tracepoints, read-only (no uprobes, see EAC above). The first version lost two thirds of the events because it parsed while recording; it now records to tmpfs first and is exact: 12,001 sleeps/s against 12.9K context switches/s from `/proc`.

Run 5, stock Proton, wineserver pinned:

- The main thread sleeps ~12,000 times a second: 8.1% of the time asleep, plus 7.5% woken but not yet running.
- Wake-up delay: median 3.2 µs, p99 39 µs.
- **Wineserver round trips: 7,579/s.** Each is 3.3 µs asleep (median 2.1) plus 4.0 µs of wake delay. That's ≈5.5% of the main thread's time, ≈0.9 ms per frame.
  - **NtCreateEvent 3,591/s + NtClose 3,018/s = 87% of them.** The rest: message-queue internals (~60/s each, once per frame) and `NtSetEvent`'s sync fd lookups (21–58/s).
- In NtClose, Wine holds a global `fd_cache_mutex` across the round trip, so threads that close handles at the same time queue behind each other.

### 6.3 The verdict that shaped the patches

| Part of the main thread's time | Share | Can Proton change it? |
|---|---|---|
| Game code | ~84% | No: native x86, never touched by Wine |
| Critical sections | ~7.5% | Almost not: the game's own lock traffic, the same atomic instructions on Windows |
| Mesa + vkd3d-proton | ~4.4% | A little; Windows pays its own driver cost |
| Wine plumbing (ntdll.so, win32u, libc) | ~2% | A little |
| **Plus: waiting** (~12% of wall time) | | **Yes, where it's Wine's own round trips and wake-ups** |

So the patches don't try to make Wine "faster" in general. They remove two specific kinds of waiting on one thread.

## 7. Deep dive: the two Proton patches

Both patches change only `ntdll.so`, Wine's Unix side, in a copy of proton-cachyos-slr called `proton-cachyos-slr-er`. The Windows side (`ntdll.dll`), `wineserver` and the rest stay as shipped, so the build has to match the shipped binaries exactly. [wine/build-er-wine.sh](wine/build-er-wine.sh) builds from the exact wine-cachyos commit the installed Proton was built from, the way Proton's own build prepares the tree. It checks:

- the server protocol version (933)
- all 258 syscall numbers
- the exported symbols (291, identical to the shipped file)
- the linked libraries (libc and libgcc_s only)

and smoke-tests the result in a scratch prefix. Everything applies only to the game process (`WINE_ER_EXE=eldenring.exe`) and only to its main thread, the thread whose ID equals the process ID. Every switch is an environment variable, 0 = stock behavior, so A/B tests need no rebuild.

### 7.1 Patch 1: spin before blocking (`0001-er-main-thread-spin.patch`)

- **`WINE_SERVER_SPIN_US`:** in `wait_reply()`, before the blocking `read()` of wineserver's reply pipe, `poll(fd, 0)` in a loop for up to N µs.
- **`WINE_SYNC_SPIN_US`:** before blocking on ntsync (`NTSYNC_IOC_WAIT_ANY/ALL`) or a futex wait, re-check for up to N µs.
- **`WINE_ER_LOG`:** a stats file every 10 s with:
  - spin hit/miss counts and average waits
  - NtCreateEvent counts by type, initial state and named/unnamed
  - event lifetimes (create → close)
  - the first code addresses on the PE stack at each create, a caller histogram that's resolved with the saved `/proc/PID/maps`

In a synthetic benchmark ([wine/bench](wine/bench/)), with the two threads on separate cores of the same CCX like the game's:

| Pair of threads on CPUs 2 and 4 | Spins off | 20 µs spins |
|---|---|---|
| Event create + close | 9.3 µs | 6.5 µs (−30%) |
| Event ping-pong | 3.3 µs | 2.2 µs (−33%) |
| Futex ping-pong | 3.35 µs | 0.26 µs |

Hit rates: 826,326 server-reply hits vs 114 misses (average hit 2.3 µs). The log's own overhead is ~35 ns per create.

Unpinned, the event ping-pong got *worse* (3.3 → 5.6 µs). Without the spin, the two strictly alternating threads get stacked on one CPU; with the main thread spinning, the other one has to run elsewhere, sometimes on the other CCX. The game can't hit this case: nothing else may run on CPU 2.

### 7.2 Patch 2: event pool and deferred close (`0002-er-event-pool.patch`)

**The helper thread.** It's Wine's own `PsCreateSystemThread`, a Unix-side-only thread that Wine's audio code also uses. wineserver marks it as a system thread, so the game never sees it: not in thread lists, not in debug events, not in DLL thread notifications. It never runs game code. It's named `wine_er_pool`, so the launcher pins it with the light threads, and it starts after the main thread's 1,000th eligible create.

**Creating.** The helper keeps two single-producer/single-consumer rings of fresh manual-reset events, one unsignaled and one signaled. It refills each to N (`ER_POOL`). The main thread wakes it with a futex once a ring is half empty, and the helper also runs a pass every 4 ms. The pool serves only requests that match what it holds:

- `NotificationEvent`
- unnamed, with no root directory, security descriptor or inherit flag
- attributes ⊆ `OPENIF | CASE_INSENSITIVE`
- access nonzero and ⊆ `EVENT_ALL_ACCESS`

Every pooled handle has `EVENT_ALL_ACCESS`, a superset of what was asked. Anything else takes the normal path.

**Closing.** A table, direct-mapped by handle index (2^18 slots, 1 MB of bss, only the touched pages are used), holds `h` for each pooled event the main thread owns, and `h|1` once its close is queued.

- A main-thread NtClose of such a handle queues the close. At 32 queued, it wakes the helper.
- Another thread's NtClose of a pooled handle clears the slot and closes it normally.
- Closing an already-queued handle returns `STATUS_INVALID_HANDLE`, like stock Wine.
- `NtDuplicateObject(DUPLICATE_CLOSE_SOURCE)` clears the slot too.
- The helper clears the slot *before* its real close, so a reused handle value never meets a stale entry.

**Why it's safe:** no event object is ever reused. A pooled event is a fresh object that nobody has seen, so even if something else still holds a handle to an event the main thread "closed", that event behaves exactly as in stock Wine until the helper's close.

**What it counts:** events served (by state), pool-empty misses, ineligible requests, deferred closes, a full close queue, helper creates/closes, helper wake-ups, and untracked closes (the table check). In the final run: 2.02M served, empty 0.16% of the time, the queue never full, untracked 0.

## 8. The overclock

**What the SMU offers** (queue 3; see `bc250_smu_oc`):

- max boost clock
- a scale for the voltage/frequency curve (negative = less voltage at a given clock)
- the extra voltage margin on/off
- CPU/GPU temperature limits

`bc250_smu_oc` also fits a model for the voltage the SMU will choose: `mV = 0.0003·f² + (−1.519 + 0.004325·s)·f + 2800 − 10·s`, for clock `f` in MHz and scale `s`. It matched our measurement at 3.6 GHz within 2 mV.

**What we measured** (`er-smu.py probe`, stock clocks):

| Load | Clock | Voltage | Tctl |
|---|---|---|---|
| 16 threads | 3,325–3,362 MHz | ~1.11 V | 85–90 °C within 4 s |
| 8 threads (1 per core) | 3,425–3,443 MHz | ~1.15 V | 89–90 °C |
| 1 thread | 3,500 MHz | 1.175 V | – |

**Why the obvious method failed.** An OC search that judges each step by a full stress load (like `bc250-detect`, or our first `er-smu.py oc` with 4 cores at 100%) stopped at 3.5 GHz on this cooler. At 3.6 GHz it hit 90 °C and the SMU throttled to 3,513 MHz. But the game, at IPC 0.66 with one heavy thread and several light ones, ran 3.8 GHz at 68–73 °C, stable through every session. A stress test at full load measures the cooler, not whether the clock/voltage pair is stable.

**What we used:** 3.8 GHz at scale −20 (~1,231 mV), checked at each launch on one loaded core. The community's notes put the sweet spot around 3.85 GHz at ~1,150 mV, and 4.0 GHz at ~1,270 mV with better cooling.

**What the tool does for other boards** (`er-smu.py qualify`, [lib/er-burn.c](lib/er-burn.c)). It separates the two questions:

- **Stability:** for each step, each core alone at full load must hold the clock under the voltage cap with no wrong results. A game-shaped load (the main core flat out, the render CCX at 25% per CPU, the light-job CPUs at 20%) must also produce no wrong results and no kernel hardware errors.
- **Heat:** the game-shaped load must stay below 85 °C. At stock it must stay below 80 °C, or there's no OC at all.

`er-burn` repeats a fixed block of work: integer hashing over an L2-sized buffer, then AVX2 FMA chains interleaved with integer ALU chains. It compares every block's 64-bit checksum with the first one. The floating-point part is a chaotic map, so one wrong bit anywhere grows instead of fading. Calibration on the research box at stock:

| Load | Tctl |
|---|---|
| 4 cores at 100%, er-burn | 76 °C |
| 4 cores at 100%, `stress --cpu` | 83 °C |
| The game-shaped er-burn load | 65 °C |
| Elden Ring itself | 68–72 °C (its GPU load heats the same chip) |

Crash safety, the voltage cap and the in-game guards are described in the [README](README.md#overclock-safety).

## 9. Open ends

- **Rare one-off hitches:** a single 60–75 ms frame every few minutes. They're events, not load. Next step: catch one with the profile running.
- **The main thread's last ~1,200 wineserver requests/s** (~0.6% of its time): message queue (`PeekMessage`/`GetMessage` internals, once per frame), sync fd lookups, and handles the pool doesn't serve.
- **Polling idle on CPU 2:** it needs a cpuidle driver first (an ACPI C-state table fix). Worth ~1–1.5 µs per remote wake-up, which is low now.
- **`MADV_COLLAPSE`** for the third of the game's memory that isn't on huge pages, without writing into the game process (`process_madvise`).
- **3.9 GHz:** about +1%. There's thermal room at ~1.27 V, but it was never tested in-game (`oc-test --max 3900` allows it).
- **L3 cache partitioning** (resctrl CAT) for the main thread, and `nohz_full` on CPU 2. Not tried.
- **Early self-closing launches:** now and then, the game closed itself ~5 s after starting. It happened with the patches on and off, with nothing in the kernel log and EAC reporting it loaded fine. Waiting a few minutes fixed it every time. Cause unknown.

## 10. Tools

| Tool | What it does |
|---|---|
| [lib/er-launch.sh](lib/er-launch.sh) | The Steam wrapper: OC on/off, SMT sibling, scx_lavd, memory reclaim + compaction, display mode, thread layout every 5 s, thermal guard, logs. With `ER_PROFILE=1`, a research profile 220 s in: busiest threads, perf counters (IPC, TLB walks, ROB stalls), wait breakdown, module/function profile, a saved all-thread perf recording, the wait trace, huge-page coverage. |
| [lib/er-smu.py](lib/er-smu.py) | SMU: `status`, `qualify` (OC safety test), `apply`, `stock`, `profile`, `profile-test`, `probe`, `oc` (the old search) |
| [lib/er-burn.c](lib/er-burn.c) | Self-checking load generator for `qualify` |
| [research/er-trace.py](research/er-trace.py) | The main thread's sleeps by kernel wait site and waker, sleep lengths, wake-up delay, same-L3 share, wineserver requests by Wine function (root; read-only) |
| [research/er-perf-locks.py](research/er-perf-locks.py) | Critical-section time per thread from a saved perf recording |
| [research/er-waits.py](research/er-waits.py) | Samples a thread's state/wchan (running vs. what it sleeps in) |
| [research/membench.c](research/membench.c) | Memory latency (4K vs 2M pages), bandwidth, core-to-core latency, memory-level parallelism (`quick`, `mlp`) |
| [research/er-smt-test.sh](research/er-smt-test.sh) | membench mlp with the main thread's sibling online vs offline |
| [research/er-compare.py](research/er-compare.py) | Stats and 20 s windows for MangoHud CSVs |
| [research/er-save.sh](research/er-save.sh) | Benchmark save snapshot/restore |
| [wine/](wine/) | The patches, the scripts that generate them, build/verify/install, and the synthetic benchmark |
| [research/early/](research/early/) | The first scripts from the 6-core days, kept for the record |
