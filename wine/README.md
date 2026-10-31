# wine/: the Proton patches

Two patches to Wine's `dlls/ntdll/unix/` (Proton's `ntdll.so`) for Elden Ring's main thread. [RESEARCH.md](../RESEARCH.md#7-deep-dive-the-two-proton-patches) explains why and how they work.

| File | What it is |
|---|---|
| `0001-er-main-thread-spin.patch` | Patch 1: spin before blocking on wineserver replies and short waits (`WINE_SERVER_SPIN_US`, `WINE_SYNC_SPIN_US`), plus the stats log (`WINE_ER_LOG`, `WINE_ER_EXE`) |
| `0002-er-event-pool.patch` | Patch 2: event pool and deferred close (`WINE_ER_POOL`), on top of patch 1 |
| `apply-er-patch.py`, `apply-er-pool.py` | The generators. They make exact-anchor text edits on a clean tree (every anchor must match exactly once, or nothing is written). The `.patch` files are their `git diff` output. |
| `build-er-wine.sh` | Fetches the matching wine-cachyos source, prepares it like Proton's build, checks protocol and syscall numbers against the shipped Proton, then builds a stock and a patched `ntdll.so` |
| `install-er-wine.sh` | Checks both builds (libraries, exports, protocol, markers), makes the `proton-cachyos-slr-er` tool copy, installs, and smoke-tests |
| `bench/` | `er-bench.exe` (no CRT, built with clang) and `run-bench.sh`: create/close and ping-pong timings with the spins and the pool off/on |
| `tools/` | `check_syscalls.py` and `check_requests.py` (source vs shipped binaries), `syntax_check.py` (gcc syntax check of patched files without a full build) |

All switches are off when unset or 0, and apply only to the process named by `WINE_ER_EXE`, and in it only to the main thread (TID == PID). `er-launch.sh` sets them from `ER_SPIN` and `ER_POOL`.

**Base:** proton-cachyos `cachyos-11.0-20261015-slr` → `CachyOS/wine-cachyos` @ `cd14106e4fc0f96006005b07985e49cc1517e9c0` (server protocol 933, 258 x86-64 syscalls).

**Build by hand** (`./elden-research proton` does the same with a progress display):

```sh
wine/build-er-wine.sh        # ~10-20 min the first time; logs in ~/.cache/elden-research/wine/
```

**License:** these patches modify Wine and are licensed like Wine, under the GNU Lesser General Public License, version 2.1 or later. The rest of this repository is MIT ([LICENSE](../LICENSE)). The build compiles the patched file from the public wine-cachyos source on your own machine; this repository ships no Wine binaries.
