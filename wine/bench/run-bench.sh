#!/usr/bin/env bash
# Builds er-bench.exe (er-bench.c, plain clang, no Wine source involved) and runs it
# under the er-wine Proton in the install smoke test's scratch prefix. Each round runs
# it three times: stock (spins and pool off), spins on, spins and event pool on. Then
# once each with the er-wine log on (stock, and everything on).
#
# Usage: [CPUS=list] [LOG=0] run-bench.sh [rounds] [spin-us] [pool]   (defaults: 3, 20 us, 64)
#   CPUS  taskset CPU list for the benchmark (not wineserver), e.g. 2,4
#   LOG=0 skips the two runs with the er-wine log on
# Output also goes to ~/.cache/elden-research/wine/bench/bench.log; er-wine logs to
# ~/.cache/elden-research/wine/bench/er-log-spin*.txt. Needs clang, lld and llvm (llvm-dlltool).
set -Eeuo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
W=${ER_WINE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/elden-research/wine}
O=$W/bench
TOOL=$HOME/.local/share/Steam/compatibilitytools.d/proton-cachyos-slr-er
rounds=${1:-3} spin=${2:-20} pool=${3:-64}
mkdir -p "$O"
exec > >(tee "$O/bench.log") 2>&1

if [[ ! -f $O/er-bench.exe || $HERE/er-bench.c -nt $O/er-bench.exe ]]; then
  defs() { local dll=$1; shift; printf 'LIBRARY %s\nEXPORTS\n' "$dll"; printf '%s\n' "$@"; }
  defs kernel32.dll CreateEventW SetEvent ResetEvent WaitForSingleObject CloseHandle CreateThread \
    QueryPerformanceCounter QueryPerformanceFrequency Sleep > "$O/kernel32.def"
  defs ntdll.dll RtlWaitOnAddress RtlWakeAddressSingle > "$O/ntdll.def"
  defs msvcrt.dll printf exit > "$O/msvcrt.def"
  for d in kernel32 ntdll msvcrt; do llvm-dlltool -m i386:x86-64 -d "$O/$d.def" -l "$O/lib$d.a"; done
  clang --target=x86_64-w64-mingw32 -O2 -Wall -ffreestanding -fno-builtin -nostdlib -fuse-ld=lld \
    -Wl,--entry=start -Wl,--subsystem=console -o "$O/er-bench.exe" "$HERE/er-bench.c" \
    -L"$O" -lkernel32 -lntdll -lmsvcrt
  echo "built $O/er-bench.exe"
fi

export WINEPREFIX=$W/smoke-pfx WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
unset WINE_ER_EXE WINE_ER_LOG WINESERVER WINELOADER
[[ -d $WINEPREFIX ]] || { echo "no $WINEPREFIX; run install-er-wine.sh first"; exit 1; }
# One persistent wineserver for all runs, so no run pays for starting it.
"$TOOL/files/bin/wineserver" -p 60
run() {  # run <spin-us> <pool> [er-wine log]
  echo "--- spin $1 us, pool $2${3:+, log on}"
  WINE_SERVER_SPIN_US=$1 WINE_SYNC_SPIN_US=$1 WINE_ER_POOL=$2 WINE_ER_LOG=${3:-} \
    ${CPUS:+taskset -c "$CPUS"} "$TOOL/files/bin/wine" "$O/er-bench.exe"
}
echo "bench CPUs: ${CPUS:-all}"
for ((r = 1; r <= rounds; r++)); do run 0 0; run "$spin" 0; run "$spin" "$pool"; done
if [[ ${LOG:-1} != 0 ]]; then
  rm -f "$O"/er-log-spin*.txt
  run 0 0 "$O/er-log-spin0.txt"
  run "$spin" "$pool" "$O/er-log-spin$spin-pool$pool.txt"
fi
"$TOOL/files/bin/wineserver" -k
if [[ ${LOG:-1} != 0 ]]; then for f in "$O"/er-log-spin*.txt; do echo "=== $f"; cat "$f"; done; fi
