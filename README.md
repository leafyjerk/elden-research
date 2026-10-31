```
  ███████╗██╗     ██████╗ ███████╗███╗   ██╗
  ██╔════╝██║     ██╔══██╗██╔════╝████╗  ██║
  █████╗  ██║     ██║  ██║█████╗  ██╔██╗ ██║
  ██╔══╝  ██║     ██║  ██║██╔══╝  ██║╚██╗██║
  ███████╗███████╗██████╔╝███████╗██║ ╚████║
  ╚══════╝╚══════╝╚═════╝ ╚══════╝╚═╝  ╚═══╝
  ██████╗ ███████╗███████╗███████╗ █████╗ ██████╗  ██████╗██╗  ██╗
  ██╔══██╗██╔════╝██╔════╝██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║
  ██████╔╝█████╗  ███████╗█████╗  ███████║██████╔╝██║     ███████║
  ██╔══██╗██╔══╝  ╚════██║██╔══╝  ██╔══██║██╔══██╗██║     ██╔══██║
  ██║  ██║███████╗███████║███████╗██║  ██║██║  ██║╚██████╗██║  ██║
  ╚═╝  ╚═╝╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝
```

# elden-research

**Elden Ring at a locked 60 FPS on the AMD BC-250, on CachyOS.**

One terminal tool does it all. It checks your machine, tests how far *your* CPU can overclock safely, builds a patched Proton, and tells you exactly what to set in Steam. Everything we tried along the way, including what didn't work, is in [RESEARCH.md](RESEARCH.md).

## Results

Same spot (the Church of Elleh Site of Grace, a demanding view), same 8-core board, MangoHud logs:

| | Avg FPS | Median | 1% low | Frames over 20 ms | Frames over 17.5 ms |
|---|---|---|---|---|---|
| Before (only the main thread pinned) | 49.6 | – | 40.3 | 50.6% | – |
| **After (everything here)** | **59.96** | **60.00** | **50.5** | **0.2%** | **2.8%** |

At the 60 FPS cap a frame is due every 16.7 ms. In the final run 97% of frames arrived on time, and most of the rest were less than 1 ms late. The very start of the project, with 6 cores and nothing tuned, was ~50 FPS with 1% lows of 32–39 in an easier open-world spot.

## What this tool changes, and what each part contributes

Each number was measured against the setup of its time, in the order we found them, so the gains don't simply add up. [RESEARCH.md](RESEARCH.md) has every run.

| # | Change | When it's active | What it does | Measured effect |
|---|---|---|---|---|
| 1 | **Patched Proton, part 1: spin before sleeping** (`ER_SPIN=20`) | Elden Ring only | The game's main thread waits up to 20 µs for a Wine server reply or a short lock wait instead of going to sleep at once. That saves a ~4 µs wake-up and two context switches thousands of times a second. | Late frames (>17.5 ms) 5.1% / 7.7% → 4.1% / 5.1% (A-B-A-B) |
| 2 | **Patched Proton, part 2: event pool** (`ER_POOL=128`) | Elden Ring only | The main thread creates and closes ~3,600 Windows events a second, and under Wine each one is a round trip to a separate process. A hidden helper thread now creates them ahead of time and closes them afterwards. | Main thread's Wine server round trips 7,450/s → 1,200/s; late frames 4.8% / 5.2% → 4.6% / 2.6%; 2.8% in the final check |
| 3 | **proton-cachyos-slr** as the base, instead of Proton Experimental | Elden Ring only | CachyOS's Proton build | Late frames 20–32% → 12% |
| 4 | **wineserver on the main thread's L3 cache** | while the game runs | The main thread's partner process runs next to it instead of on the other core cluster (CCX) | One round trip 12.0 → 8.2 µs; late frames 20% / 12% → 4.6% / 8.1% |
| 5 | **CPU overclock**, only while the game runs (3.8 GHz on the research box; yours is whatever passes the safety test) | while the game runs | More clock for the main thread, at ~1.23 V | Avg 52.2–53.4 → 54.6 FPS; frames >20 ms 19–24% → 7% |
| 6 | **scx_lavd scheduler** | while the game runs | Keeps other tasks off the main thread's CPU | 1% low 42.9 → 48.2; frames >20 ms 7.9% → 2.3% |
| 7 | **Main thread alone on its core**: its SMT sibling goes offline | while the game runs | The main thread gets the whole core's out-of-order resources | Main-thread IPC +7%; avg 55.6 → 56.6, 1% low 48.2 → 50.4 |
| 8 | **Thread layout + interrupts on CPUs 0–1** | while the game runs (interrupts: from boot) | Light game threads share the main thread's CCX, render threads get the other CCX, and the desktop and interrupts get core 0 | Avg 49.6 → 52.6; frames >20 ms 50.6% → 21.1% |
| 9 | **Memory tuning**: swappiness 10, earlier kswapd, compaction at launch, faster huge pages, other apps pushed to swap at launch | permanent (sysctls, boot) and at launch | The game's memory stays in RAM, ~2/3 of it on 2 MB pages | Worst frame 786 → 49 ms; 1% low 38.1 → 43.2 |
| 10 | **60 Hz while playing** (KDE Plasma) | while the game runs | The game caps at 60; on a 75 Hz desktop every 4th frame was shown twice | Smoother (judder gone; FPS unchanged) |
| 11 | **Lean MangoHud config + session logs** | while the game runs | Small overlay, and an FPS log of each session for `./elden-research status` | – |

Not done by this tool: **unlocking all 8 CPU cores** (a modded BIOS). It was the biggest single step, 54.8 → 58.1 FPS in the open world. See [8 cores](#8-cores).

## Requirements and assumptions

- **An AMD BC-250** (the PS5-chip mining board) **with a fan on its heatsink**. The stock heatsink is passive, made for server airflow. The research box has two Arctic P12 Pros in push-pull through the stock heatsink, plus a VRM heatsink, in a case. The tool refuses to run on anything else.
- **8 cores unlocked** is what everything was tuned and tested on (modded P5.00 BIOS). A 6-core board works, since the thread layout is computed from the CPU topology, but that path is untested.
- **CachyOS, fully updated** (`sudo pacman -Syu`) with its own kernel, which brings sched_ext for scx_lavd. Other Arch-based systems may work but are untested.
- **Native Steam** (the `steam` package, not Flatpak), with a normal desktop or gaming-mode login (a systemd user session).
- **Elden Ring installed in Steam.**
- **`proton-cachyos-slr`** from the CachyOS repos. The patches were made for version 11.0-20261015. The build also tries newer versions, but installs nothing unless every check passes.
- **sudo rights** (the wheel group), internet access (packages, plus ~400 MB of Wine source from GitHub), and ~1.5 GB free disk (~3.5 GB on non-btrfs filesystems).
- **Assumptions:** you play at the game's 60 FPS cap. On this hardware the game is limited by its main thread on the CPU, so graphics settings don't change the FPS (Low and High measured the same), and you can keep High. The automatic 60 Hz switch needs KDE Plasma; elsewhere, set 60 Hz yourself.

## Quick start

```sh
git clone https://github.com/leafyjerk/elden-research.git
cd elden-research
./elden-research install
```

The installer walks through seven steps with a progress bar. It's safe to stop and re-run at any point; finished steps are skipped or quickly re-checked.

| Step | What happens | Time |
|---|---|---|
| 1. Preflight checks | Hardware, OS, Steam, game, Proton, disk, packages. Changes nothing. | ~10 s |
| 2. Packages | Installs what's missing with pacman (asks first) | ~1 min |
| 3. System tweaks | sudo: sysctls, boot-time affinity, the game-time helpers, a polkit rule | ~10 s |
| 4. Launcher + settings | `~/.local/share/elden-research/` and `~/.config/elden-research/config` | ~1 s |
| 5. Overclock safety test | sudo: finds the highest step your CPU passes (see below); optional | ~15 min |
| 6. Patched Proton | Downloads, builds, verifies and installs the patched `ntdll.so`; optional | ~10–20 min |
| 7. Steam setup | The clicks below, then a check that Steam saved them | you |

Then in Steam (the tool prints these with your exact paths):

1. **Quit Steam completely** (Steam menu → Exit) and start it again. It only looks for new compatibility tools at startup.
2. Right-click ELDEN RING → Properties → **Compatibility** → tick *Force the use of a specific Steam Play compatibility tool* → pick **proton-cachyos-11.0-20261015 er-wine (Elden Ring patch)**.
3. Properties → **General** → Launch Options:
   ```
   "/home/<you>/.local/share/elden-research/er-launch.sh" %command%
   ```
4. Play. Afterwards, `./elden-research status` shows how the session went: FPS, OC, patches, layout.

In gaming mode (Big Picture) the same settings are under the game's ⚙ → Properties.

## Overclock safety

Every BC-250 is different: some CPUs won't hold a higher clock at a given voltage, and some boxes have weaker cooling. So the tool never just applies a number. It tests your machine first, and it keeps checking while you play.

**The safety test** (`./elden-research oc-test`, also step 5 of the install):

- It runs only on a BC-250, after the SMU (the chip's management controller) answers a test message. The GPU is never touched.
- **Voltage hard cap 1.275 V.** The community advice is never above 1.3 V. The steps are 3600, 3700 and 3800 MHz, with the voltage curve lowered so they need about 1.22, 1.24 and 1.23 V. 3900 MHz (~1.27 V) only runs if you ask for it (`--max 3900`), and it was never run in-game.
- **First, a cooling check at stock clocks.** If a game-like load already pushes the CPU to 80 °C at stock, there's no OC.
- **Each step, each core alone at full load:** it must hold the clock without going over the voltage cap.
- **Then a game-like load:** below 85 °C, and no machine-check or hardware errors in the kernel log.
- **Every result is checked.** The load generator (`lib/er-burn.c`) repeats a fixed calculation and compares each result bit for bit; a wrong result means the step is unstable. If the only problem was wrong results, the step is retried once with ~30 mV more, still under the cap.
- **It picks the highest step that passed,** or one lower if that step ran hot (80 °C or more). Then it runs a 5-minute soak at the pick.
- **Crash-safe.** Each step is written to disk before it starts. If the machine freezes or reboots, power-cycle it: the OC is never applied at boot, so it comes back at stock. The next run counts that step as crashed and tests only the steps below it.
- It ends at stock clocks either way, including after Ctrl+C.

**While you play:**

- The OC is on **only while Elden Ring runs**. It's turned off when the game exits, even if Steam kills the game, because the launcher's watcher runs outside Steam's process tree. Other games always run at stock clocks.
- At each launch, `er-smu apply` checks one loaded core's clock and voltage before the game starts, and goes back to stock if either is off.
- While the OC is on, the SMU throttles at 90 °C, which is stricter than the stock firmware's 100 °C. Separately, a **thermal guard** turns the OC off if the CPU stays at 88 °C for 15 seconds (`ER_OC_TMAX`).
- If the game crashes or the box hangs while playing: `./elden-research oc off`, then run `./elden-research oc-test --pace thorough` (~35 min).

The test is a screening, not a guarantee. Its load heats the CPU about as much as the game does, but the game's GPU load also heats the same chip, and the in-game guard is there for that.

## Settings

`~/.config/elden-research/config` holds the defaults. Any of them can be overridden for one launch in Steam's launch options, for example `ER_OC=0 ER_POOL=0 "…/er-launch.sh" %command%`.

| Setting | Default | Meaning |
|---|---|---|
| `ER_OC` | set by the OC test | 1 = the CPU OC while the game runs |
| `ER_OC_TMAX` | 88 | Thermal guard (°C, 15 s) |
| `ER_SPIN` | 20 | Patched Proton: main-thread spin before sleeping, µs (0 = off) |
| `ER_POOL` | 128 | Patched Proton: event pool size (0 = off) |
| `ER_SMT_OFF` | 1 | Main thread's SMT sibling offline while playing |
| `ER_SCX` | 1 | scx_lavd while playing (0 = the kernel's scheduler) |
| `ER_LAYOUT`, `ER_MAIN`, `ER_GX`, `ER_REST`, `ER_WS` | from your CPU topology | Thread layout: main thread, render threads, other game threads, wineserver |
| `ER_RECLAIM` | 1 | Push other apps' memory to swap at launch |
| `ER_REFRESH` | 60 | Display refresh while playing (KDE only; empty = leave it) |
| `ER_FPS` | – | e.g. 50: cap at 50 FPS with the display at 50 Hz |
| `ER_HUD` | 1 | MangoHud overlay visible (Shift_R+F12 toggles it; the log runs either way) |
| `ER_PROFILE` | 0 | 1 = research profile 220 s into the session (needs `perf`; costs ~1 FPS while it runs) |

## Commands

```
./elden-research              menu
./elden-research install      everything, step by step (re-run to continue)
./elden-research check        preflight checks only (changes nothing)
./elden-research status       what's installed, Steam's settings, the last game session
./elden-research oc-test      the overclock safety test (--pace quick|normal|thorough, --max 3900)
./elden-research oc on|off    use the test's pick while the game runs, or stock clocks
./elden-research proton       build/rebuild the patched Proton (e.g. after a Proton update)
./elden-research steam        the Steam steps, and a check that Steam saved them
./elden-research bench        benchmark helpers: save snapshot/restore, compare MangoHud logs
./elden-research uninstall    remove everything this tool installed
```

## Uninstall

`./elden-research uninstall` removes the system part (every file it installed is listed in `/etc/elden-research/manifest`, and files that existed before are restored from backup), the patched Proton copy, the launcher and the settings. It asks before deleting logs and the build cache. In Steam, clear the launch options and set the compatibility tool back. Reboot to get the boot-time defaults back. Your game, saves and the stock Proton are never touched.

## Troubleshooting

- **The game closes by itself a few seconds after starting.** This happened now and then during the research, with and without the patches. Waiting a few minutes before launching again fixed it every time.
- **`status` says the OC failed.** Run `journalctl -u er-oc` for the reason (the 5-second check at launch didn't pass). The game then runs at stock clocks.
- **Steam doesn't list the patched Proton.** Exit Steam completely (Steam menu → Exit), not just its window.
- **pacman updated proton-cachyos-slr.** The patched tool is a separate copy and keeps working. `./elden-research proton` rebuilds it against the new version.
- **A/B testing a single change.** Launch options like `ER_POOL=0`, `ER_SPIN=0`, `ER_OC=0`, `ER_SCX=0` or `ER_SMT_OFF=0` turn one part off for that launch. `./elden-research bench` explains the method that makes 1–2% differences visible.
- **Online play and Easy Anti-Cheat.** This tool never touches the game's files or memory: no injection, no hooks, nothing written into the game process. The patched Proton is a custom Proton build, like GE-Proton or proton-cachyos itself, and EAC loaded normally in every test session. That's no guarantee: anti-cheat policies can change.
- **Gaming mode / gamescope.** Everything works the same except the 60 Hz switch (KDE only). Set 60 Hz in Steam's display settings.

## 8 cores

Out of the box the BC-250 runs 6 of its 8 cores. The research box runs all 8 after a modded BIOS (P5.00) and a core unlock; see the BC-250 community resources ([bc250-collective](https://github.com/bc250-collective), [bc250-core-cu-unlock](https://github.com/GabriWar/bc250-core-cu-unlock)). Flashing a BIOS can brick a board, so keep a backup of the original ROM somewhere other than the flash stick. That's outside this tool. After the flash we saw a slower POST (10.9 → 18.6 s), and the first cold power-on sometimes landed in BIOS setup.

## Repository

```
elden-research          the tool (Python 3, no extra modules)
lib/er-launch.sh        Steam launch wrapper: everything that runs only while the game runs
lib/er-smu.py           SMU access: OC apply/stock, the OC safety test (qualify), status
lib/er-burn.c           self-checking CPU load for the OC test
lib/system-setup.sh     the root part: install / oc / uninstall, with manifest and backups
lib/MangoHud.conf       lean overlay + session log
wine/                   the two Proton patches, their generators, build + verify + install scripts, benchmark
research/               the analysis tools used in RESEARCH.md (tracer, lock profiler, membench, ...)
RESEARCH.md             the full write-up
```

## Disclaimer

This project overclocks your CPU, changes system settings and replaces part of Proton. It runs safety checks, but every board is different, and no test can guarantee stability or rule out damage to hardware or data. **You use it at your own risk, and you're responsible for your hardware, your data and your Steam account.** It comes with no warranty (see [LICENSE](LICENSE)). It isn't affiliated with or endorsed by FromSoftware, Bandai Namco, Valve, AMD, CachyOS or the Wine project.

## Credits and licenses

- [bc250-collective/bc250_smu_oc](https://github.com/bc250-collective/bc250_smu_oc): the SMU mailbox protocol and the voltage/frequency model that `er-smu.py` uses. Also the cyan-skillfish-governor (the SMU access pattern, paused while the OC tool runs).
- [CachyOS](https://cachyos.org): the kernel, proton-cachyos and wine-cachyos. [sched-ext/scx](https://github.com/sched-ext/scx): scx_lavd. [MangoHud](https://github.com/flightlessmango/MangoHud). Wine and Proton (Valve, CodeWeavers and the Wine developers).
- License: the tool and scripts are MIT ([LICENSE](LICENSE)). The Wine patches in `wine/` are LGPL-2.1-or-later, like Wine itself ([wine/README.md](wine/README.md)).
