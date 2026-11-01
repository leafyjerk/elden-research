#!/usr/bin/env bash
# elden-research's Steam launch wrapper: all the Elden Ring tweaks that apply only
# while the game runs.
#   - CPU overclock (the step your machine passed in the OC safety test) only while
#     the game runs, stock again at exit, plus a thermal guard that turns it off if
#     the CPU stays at ER_OC_TMAX (88 C) for 15 s. Other games never run overclocked.
#   - scx_lavd scheduler while the game runs (via scx_loader)
#   - the main thread's SMT sibling (CPU 3) offline, so the main thread has its core alone
#   - once loaded: main thread alone on CPU 2, render threads on the second L3 cache
#     (CCX), the other game threads and wineserver on the rest of the first one;
#     CPUs 0-1 are left for interrupts and the desktop
#   - other apps pushed to swap and memory compacted at launch, so the game gets 2 MB
#     pages and doesn't have to reclaim memory mid-game
#   - display at 60 Hz while the game runs (KDE; the game caps at 60), restored afterwards
#   - the patched Proton's switches (ER_SPIN, ER_POOL; stock Proton ignores them)
#   - MangoHud with a lean config (FPS log of each session in ./logs)
#
# Steam launch options (elden-research install prints the exact line):
#   "$HOME/.local/share/elden-research/er-launch.sh" %command%
# Settings come from ~/.config/elden-research/config; an ER_* variable in the launch
# options overrides it for that launch, e.g. ER_OC=0 "..../er-launch.sh" %command%.
#
# Wrapper output goes to logs/launch.log. The system part (services, polkit rules,
# sysctls) is lib/system-setup.sh, run by elden-research install.

DIR="$(dirname "$(realpath "$0")")"
LOG="$DIR/logs/launch.log"
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/elden-research/config
# Settings file: KEY=value lines. Launch options win, so only unset variables are taken.
if [[ -r $CONFIG ]]; then
  while IFS='=' read -r k v; do
    [[ $k =~ ^ER_[A-Z_]+$ && -z ${!k+x} ]] && export "$k=$v"
  done < "$CONFIG"
fi
LOAD_WAIT=${ER_LOAD_WAIT:-60}  # the game must start with core 0; pin only after it has loaded
# Layout overrides for A/B tests, e.g. ER_MAIN=8 ER_REST=0-1,10-15 in Steam launch options.
# ER_GX pins the render threads (GXWorker*, vkd3d_queue) separately from the rest.
# ER_LAYOUT=split: main thread alone on the first L3 with the light job threads,
# render threads get the whole second L3. The config file holds the CPU numbers for
# this machine; the fallbacks below are the 8-core unlock's. (ER_LAYOUT=ccx, main +
# render on 8-15, was tested 2026-10-29: 45.9 FPS vs 49.6, much more stutter.)
if [[ $ER_LAYOUT == split ]]; then
  : "${ER_MAIN:=2}" "${ER_GX:=8-15}" "${ER_REST:=0-1,4-7}"
  : "${ER_WS=0-1,4-7}"  # won round 7 (A-B-A-B); ER_WS= turns it off
fi
ER_MAIN=${ER_MAIN:-2}
ER_REST=${ER_REST:-}
ER_GX=${ER_GX:-}
# ER_FPS=50: cap at 50 FPS (MangoHud) with the display at 50 Hz, so every frame
# is shown exactly once; smoother than 50-60 FPS on a fixed 60 Hz display.
ER_FPS=${ER_FPS:-}
ER_REFRESH=${ER_REFRESH-${ER_FPS:-60}}  # display refresh while playing; ER_REFRESH= leaves it alone
ER_RECLAIM=${ER_RECLAIM:-1}  # 0 = don't push other apps to swap at launch
ER_SCX=${ER_SCX:-1}          # 0 = keep the kernel's own scheduler instead of scx_lavd
ER_SMT_OFF=${ER_SMT_OFF:-1}  # 1 = main thread's SMT sibling offline while playing
ER_OC=${ER_OC:-0}            # 1 = er-oc.service while the game runs (set by the OC safety test)
ER_OC_TMAX=${ER_OC_TMAX:-88} # thermal guard: OC off after 15 s at or above this Tctl (C)
ER_HUD=${ER_HUD:-1}          # 0 = MangoHud overlay hidden (Shift_R+F12 shows it; the FPS log still runs)
ER_PROFILE=${ER_PROFILE:-0}  # 1 = research profile 220 s in (perf, wait trace; costs ~1 FPS while it runs)
# ER_WS=0-1,4-7: wineserver on these CPUs (the main thread's L3), so the main
# thread's ~7k requests/s don't cross L3s; empty = left to the scheduler.
# Default with ER_LAYOUT=split (above).
ER_WS=${ER_WS:-}

# Prints the busiest game threads over 10s: % of one CPU, TID, name, last CPU.
# Fields are counted after the "(comm) " part of /proc/.../stat, since names can contain spaces.
# Also: the main thread's page-table walks (what 2 MB pages remove), how much of
# the game's memory is in 2 MB pages, NTSync use, and the main CPU's clock.
profile_threads() {
  local pid=$1 t perf_out waits_out perf_data i mhz= temps= cs0 cs1 ws ws0
  declare -A before
  cs0=$(awk '/ctxt_switches/ {s += $2} END {print s}' /proc/"$pid"/task/"$pid"/status)
  ws=$(pgrep -x wineserver | head -1)
  ws0=$(sed 's/.*) //' /proc/"$ws"/stat 2>/dev/null | awk '{print $12 + $13}')
  for t in /proc/"$pid"/task/*; do
    before[${t##*/}]=$(sed 's/.*) //' "$t/stat" 2>/dev/null | awk '{print $12 + $13}')
  done
  perf_out=$(mktemp) waits_out=$(mktemp) perf_data=$(mktemp)
  # TLB walks (data and instruction side), reorder-buffer-full and instruction-fetch stall cycles.
  perf stat -x, -o "$perf_out" -t "$pid" -e cycles:u,instructions:u,ls_tablewalker.dside:u,ls_l1_d_tlb_miss.tlb_reload_4k_l2_miss:u,ls_l1_d_tlb_miss.tlb_reload_2m_l2_miss:u,bp_l1_tlb_miss_l2_tlb_miss:u,de_dis_dispatch_token_stalls0.retire_token_stall:u,ic_fetch_stall.ic_stall_any:u -- sleep 10 &
  python3 "$DIR/er-waits.py" "$pid" "$pid" 10 > "$waits_out" &
  for ((i = 0; i < 10; i++)); do
    sleep 1
    mhz+=" $(awk -v c="$ER_MAIN" '/^processor/ {p = $3} /^cpu MHz/ && p == c {print int($4)}' /proc/cpuinfo)"
    temps+=" $(($(< "$tctl") / 1000))"
  done
  wait
  cs1=$(awk '/ctxt_switches/ {s += $2} END {print s}' /proc/"$pid"/task/"$pid"/status)
  echo "$(date +%T) busiest threads (% of one CPU over 10s):"
  for t in /proc/"$pid"/task/*; do
    [[ -n ${before[${t##*/}]} ]] || continue
    awk -v b="${before[${t##*/}]}" -v tid="${t##*/}" -v name="$(cat "$t/comm")" \
      '{printf "%5.1f%%  %-7s %-20s cpu %s\n", ($12 + $13 - b) / 10, tid, name, $37}' \
      < <(sed 's/.*) //' "$t/stat" 2>/dev/null)
  done | sort -rn | head -15
  [[ -n $ws0 ]] && sed 's/.*) //' /proc/"$ws"/stat 2>/dev/null |
    awk -v b="$ws0" -v c="$(taskset -pc "$ws" | awk '{print $NF}')" \
      '{printf "wineserver: %.1f%% of one CPU, last on cpu %s, allowed %s\n", ($12 + $13 - b) / 10, $37, c}'
  echo "main thread over the same 10s:"
  awk -F, '$3 {printf "  %-42s %s\n", $3, $1}' "$perf_out"
  echo "main thread running vs. sleeping (kernel function it sleeps in), $(((cs1 - cs0) / 10)) context switches/s:"
  cat "$waits_out"
  # Next 10s: where the main thread's CPU time goes, by module (game, Wine, drivers).
  perf record -q -t "$pid" -e cycles:u -F 999 -o "$perf_data" -- sleep 10 2> /dev/null
  echo "main thread CPU time by module:"
  perf report -i "$perf_data" --sort dso --stdio 2> /dev/null | awk '/^ +[0-9.]+%/ && $1 + 0 >= 0.3'
  echo "top functions outside eldenring.exe:"
  perf report -i "$perf_data" --sort dso,sym --stdio 2> /dev/null | awk '/^ +[0-9.]+%/ && !/eldenring\.exe/' | head -15
  rm -f "$perf_out" "$waits_out" "$perf_data"
  # Next 10s: every game thread with call graphs, kept for offline analysis
  # (which threads take the same critical sections as the main thread).
  perf_data="$DIR/logs/perf-$(date +%F_%H-%M-%S).data"
  perf record -q -g -p "$pid" -e cycles:u -F 499 -o "$perf_data" -- sleep 10 2> /dev/null &&
    echo "all-thread call graphs saved to $perf_data"
  # Next 5s (er-trace@, root): each of the main thread's sleeps, where in the
  # kernel it slept, who woke it, and how long it then waited to run.
  systemctl start --no-ask-password "er-trace@$pid.service" 2>&1 | sed 's/^/er-trace: /'
  cat "/run/er-trace-$pid.txt" 2> /dev/null
  awk '/^(Rss|Anonymous|AnonHugePages|Swap):/ {printf "%s %d MB  ", $1, $2 / 1024} END {print ""}' /proc/"$pid"/smaps_rollup
  echo "ntsync handles: $(find /proc/"$pid"/fd -lname /dev/ntsync 2>/dev/null | wc -l)"
  echo "CPU $ER_MAIN MHz, once a second:$mhz"
  echo "Tctl C, once a second:$temps"
  echo "wine display driver: $(grep -oE 'wine(wayland|x11)\.so' /proc/"$pid"/maps | sort -u | tr '\n' ' ')"
  echo "libc: $(grep -m1 -oE '/[^ ]*/libc\.so\.6' /proc/"$pid"/maps)"
  # The game's memory map, to resolve the er-wine log's caller addresses (DLLs are file-backed).
  cp "/proc/$pid/maps" "$DIR/logs/maps-$(date +%F_%H-%M-%S).txt" 2> /dev/null
  free -m | awk '/^(Mem|Swap)/ {printf "%s used %d MB  ", $1, $3} END {print ""}'
}

# Prints "<output> <current mode id> <mode id at $1 Hz, same resolution>" for
# the first enabled output, from kscreen-doctor's "Modes: 1:1920x1080@60.00! 2:...*" line.
display_modes() {
  env -u LD_PRELOAD timeout 5 kscreen-doctor -o 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | awk -v hz="$1" '
    /^Output:/ {name = $3; on = 0}
    /^[ \t]+enabled/ {on = 1}
    on && /Modes:/ {
      for (i = 2; i <= NF; i++) if ($i ~ /\*/) { split($i, a, /[:@]/); cur = a[1]; res = a[2] }
      for (i = 2; i <= NF && !want; i++) {
        split($i, a, /[:@]/); r = a[3]; gsub(/[!*]/, "", r)
        if (a[2] == res && r > hz - 0.01 && r < hz + 0.01) want = a[1]
      }
      print name, cur, want; exit
    }'
}

# Pushes other apps (VS Code, browsers...) to swap before the game needs the
# memory; otherwise the kernel reclaims mid-game and game threads stall (the
# 0.4-0.8 s hitches on 2026-10-29). Skips our own cgroup, which holds Steam
# and the game; the desktop shell is in session.slice and isn't touched.
reclaim_apps() {
  local self cg cur before=0 after=0
  self=${ER_CGROUP:-/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)}
  for cg in "${self%/*}"/*/; do
    cg=${cg%/}
    [[ $cg != "$self" && -w $cg/memory.reclaim ]] || continue
    cur=$(< "$cg/memory.current")
    ((before += cur))
    echo "$cur" 2> /dev/null > "$cg/memory.reclaim"
    ((after += $(< "$cg/memory.current")))
  done
  echo "pushed $(((before - after) >> 20)) MB of other apps to swap ($((after >> 20)) MB left)"
}

# Keeps the newest 30 sessions' logs (each MangoHud CSV is ~1 MB) and 5 profile recordings.
rotate_logs() {
  local pat f
  for pat in 'eldenring_*[0-9].csv:30' 'eldenring_*_summary.csv:30' 'er-wine-*.log:30' 'maps-*.txt:5' 'perf-*.data:5'; do
    ls -1t "$DIR"/logs/${pat%:*} 2> /dev/null | tail -n +$((${pat#*:} + 1)) | while read -r f; do rm -f "$f"; done
  done
  if [[ $(stat -c %s "$LOG" 2> /dev/null || echo 0) -gt 4000000 ]]; then
    tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}

# Runs as its own user service, outside Steam's process tree, so cleanup still
# happens when Steam kills the game: Steam's Stop kills the whole tree, which
# left the OC on and CPU 3 offline on 2026-10-30.
watch_game() {
  local pid main=$ER_MAIN rest=$ER_REST gx=$ER_GX want started_scx=0 n i profiled=0 start sib= oc=0 hot=0
  if [[ -z $rest ]]; then
    if [[ $(nproc --all) -ge 16 ]]; then rest=4-15; else rest=0-1,4-11; fi
  fi
  [[ -z $gx ]] && gx=$rest
  tctl=$(grep -l k10temp /sys/class/hwmon/hwmon*/name | head -1)
  tctl=${tctl%/name}/temp1_input
  taskset -pc 0-1 $$ > /dev/null  # this watcher and its samplers stay off the game's cores

  ((ER_RECLAIM)) && reclaim_apps
  systemctl start --no-ask-password er-boost.service 2> /dev/null && echo "memory compacted"

  for ((i = 0; i < 150; i++)); do   # up to 5 min for the game to appear
    pid=$(pgrep -x eldenring.exe | head -1) && [[ -n $pid ]] && break
    sleep 2
  done
  if [[ -z $pid ]]; then
    echo "eldenring.exe never appeared"
  else
    echo "found PID $pid, pinning in ${LOAD_WAIT}s"
    sleep "$LOAD_WAIT"
    # The sibling goes offline only now, with the game up. When Wine started with it
    # already offline (Game Mode, where nothing delays the launch), Wine saw 15 CPUs
    # and the game died in its sound init (Wwise thread setup, 2026-11-01).
    # Before scx_lavd starts: sched_ext schedulers don't take CPU hotplug well.
    if kill -0 "$pid" 2> /dev/null; then
      if ((ER_SMT_OFF)); then
        sib=$(tr ',-' '\n\n' < /sys/devices/system/cpu/cpu"$main"/topology/thread_siblings_list | grep -vx "$main" | head -1)
        if [[ -z $sib ]]; then
          echo "CPU $main has no online sibling (already offline?)"
        else
          systemctl start --no-ask-password "er-offline@$sib.service" && echo "CPU $sib (main thread's sibling) offline" || sib=
        fi
      fi
      if ((!ER_SCX)); then
        echo "kernel scheduler (ER_SCX=0)"
      elif ! command -v scxctl > /dev/null; then
        echo "scxctl not found (scx-tools package): kernel scheduler"
      elif scxctl get 2>&1 | grep -q 'no scx'; then
        scxctl start -s lavd && started_scx=1 && echo "scx_lavd started"
      else
        echo "scheduler already running: $(scxctl get)"
      fi
    fi
    taskset -p -c "$main" "$pid" >/dev/null && echo "main thread -> CPU $main, render -> $gx, others -> $rest"
    if [[ -n $ER_WS ]]; then
      for i in $(pgrep -x wineserver); do
        taskset -a -p -c "$ER_WS" "$i" > /dev/null && echo "wineserver $i -> CPUs $ER_WS"
      done
    fi
    systemctl is-active --quiet er-oc.service && oc=1
    start=$SECONDS
    # Re-check every 5s: threads created by the main thread inherit CPU 2.
    while kill -0 "$pid" 2>/dev/null; do
      n=0
      for t in /proc/"$pid"/task/*; do
        [[ ${t##*/} == "$pid" ]] && continue
        case $(cat "$t/comm" 2>/dev/null) in
          GXWorker*|vkd3d_queue) want=$gx ;;
          *) want=$rest ;;
        esac
        [[ $(awk '/Cpus_allowed_list/ {print $2}' "$t/status" 2>/dev/null) == "$want" ]] && continue
        taskset -p -c "$want" "${t##*/}" >/dev/null 2>&1 && ((n++))
      done
      ((n)) && echo "$(date +%T) repinned $n threads"
      # Thermal guard: the OC goes off (the game keeps running, at stock clocks) if
      # Tctl stays at or above ER_OC_TMAX for 3 checks in a row (~15 s). The SMU
      # itself throttles at 90 C while the OC is on.
      if ((oc)) && [[ -r $tctl ]]; then
        if (($(< "$tctl") / 1000 >= ER_OC_TMAX)); then ((hot++)); else hot=0; fi
        if ((hot >= 3)); then
          systemctl stop --no-ask-password er-oc.service &&
            echo "$(date +%T) thermal guard: Tctl $(($(< "$tctl") / 1000)) C for 15 s, CPU OC off (stock clocks)"
          oc=0
        fi
      fi
      # 220s after pinning (mid-gameplay), log which threads are actually busy.
      # That's ~13s after MangoHud's 3-minute log ends, so the profile's own
      # overhead (~1 FPS) stays out of the FPS numbers.
      if ((ER_PROFILE && !profiled && SECONDS - start > 220)); then
        profile_threads "$pid"; profiled=1
      fi
      sleep 5
    done
    echo "game exited"
    [[ -s $ER_WINE_LOG ]] && { echo "er-wine stats ($ER_WINE_LOG):"; sed 's/^/  /' "$ER_WINE_LOG"; }
  fi

  ((started_scx)) && scxctl stop && echo "scx_lavd stopped"
  [[ -n $sib ]] && systemctl stop --no-ask-password "er-offline@$sib.service" && echo "CPU $sib back online"
  systemctl is-active --quiet er-oc.service && systemctl stop --no-ask-password er-oc.service &&
    echo "CPU OC off (stock clocks)"
  if [[ -n $ER_RESTORE_MODE ]]; then
    env -u LD_PRELOAD timeout 5 kscreen-doctor "output.${ER_RESTORE_MODE% *}.mode.${ER_RESTORE_MODE#* }" >/dev/null 2>&1 &&
      echo "display restored to mode ${ER_RESTORE_MODE#* }"
  fi
}

case $1 in
  --setup) echo "The system setup moved: run elden-research install (it uses lib/system-setup.sh)"; exit 1 ;;
  --watch) watch_game; exit ;;
esac

mkdir -p "$DIR/logs"
rotate_logs
echo "=== $(date '+%F %T') launch" >> "$LOG"
# What this run used, so A/B runs can be checked from the log alone.
echo "settings: $(env | grep '^ER_' | sort | tr '\n' ' ')" >> "$LOG"
for a in "$@"; do
  [[ $a == */proton ]] || continue
  echo "proton: $(cut -d' ' -f2 "${a%/proton}/version" 2>/dev/null || echo "${a%/proton}") ($(basename "${a%/proton}"))" >> "$LOG"
  grep -qa WINE_SYNC_SPIN_US "${a%/proton}/files/lib/wine/x86_64-unix/ntdll.so" 2> /dev/null &&
    echo "ntdll.so: er-wine patched" >> "$LOG"
  grep -qa WINE_ER_POOL "${a%/proton}/files/lib/wine/x86_64-unix/ntdll.so" 2> /dev/null &&
    echo "ntdll.so: has the event pool (patch 2)" >> "$LOG"
done
# er-wine (the patched ntdll.so in the proton-cachyos-slr-er tool, see RESEARCH.md):
# ER_SPIN=N lets the game's main thread spin up to N us before it blocks, on
# wineserver replies and on short waits; ER_SPIN_SERVER / ER_SPIN_SYNC set the
# two separately. Default 20 (won both A-B-A-B pairs, 2026-10-30); ER_SPIN=0 turns
# the spins off. Stock Proton ignores all of these variables.
export ER_WINE_LOG="$DIR/logs/er-wine-$(date +%F_%H-%M-%S).log"
export WINE_ER_EXE=eldenring.exe WINE_ER_LOG="$ER_WINE_LOG"
export WINE_SERVER_SPIN_US=${ER_SPIN_SERVER:-${ER_SPIN:-20}} WINE_SYNC_SPIN_US=${ER_SPIN_SYNC:-${ER_SPIN:-20}}
# ER_POOL=N (patch 2): a helper thread keeps N fresh events of each kind ready for the
# main thread and does its event closes. Default 128 (won both A-B-A-B pairs, 2026-10-31); 0 = off.
export WINE_ER_POOL=${ER_POOL:-128}
echo "er-wine: server reply spin ${WINE_SERVER_SPIN_US} us, sync spin ${WINE_SYNC_SPIN_US} us, event pool ${WINE_ER_POOL}" >> "$LOG"
# CPU OC (er-oc.service, ~7s) before the game starts: apply checks one loaded
# core's clock, which the game's loading threads would drag down.
if ((ER_OC)); then
  if [[ ! -f /etc/systemd/system/er-oc.service ]]; then
    echo "CPU OC: no er-oc.service (run elden-research oc-test first), running stock" >> "$LOG"
  elif env -u LD_PRELOAD -u LD_LIBRARY_PATH systemctl start --no-ask-password er-oc.service 2>> "$LOG"; then
    echo "CPU OC on ($(sed -n 's|^ExecStart=.*apply \([0-9]*\) .*|\1 MHz|p' /etc/systemd/system/er-oc.service)) until the game exits" >> "$LOG"
  else
    echo "CPU OC failed, running stock (journalctl -u er-oc)" >> "$LOG"
  fi
fi
# Game Mode (gamescope session): gamescope owns the display, and kscreen-doctor,
# with no KDE session to talk to, never exited (the launch sat for 3 min, 2026-11-01).
if [[ -n $ER_REFRESH && -n $GAMESCOPE_WAYLAND_DISPLAY ]]; then
  echo "display: Game Mode (gamescope), refresh left to Steam" >> "$LOG"
elif [[ -n $ER_REFRESH ]] && command -v kscreen-doctor > /dev/null &&
  read -r out cur want < <(display_modes "$ER_REFRESH") &&
  [[ -n $want && $want != "$cur" ]] &&
  env -u LD_PRELOAD timeout 5 kscreen-doctor "output.$out.mode.$want" >/dev/null 2>&1; then
  export ER_RESTORE_MODE="$out $cur"
  echo "display $out -> ${ER_REFRESH} Hz (mode $want, was $cur)" >> "$LOG"
fi
# Without Steam's preload and runtime library path: perf broke on its libcurl.
# The watcher gets the ER_* settings, the display variables (kscreen-doctor) and
# Steam's cgroup (reclaim_apps skips it); setsid is the fallback without systemd-run.
export ER_CGROUP=/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)
envs=()
for v in $(compgen -e | grep '^ER_') WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR; do
  [[ -v $v ]] && envs+=(-E "$v")
done
env -u LD_PRELOAD -u LD_LIBRARY_PATH systemd-run --user --collect --quiet --unit="er-watch-$$" "${envs[@]}" \
  /bin/bash -c 'exec "$0" --watch >> "$1" 2>&1 < /dev/null' "$(realpath "$0")" "$LOG" 2>> "$LOG" ||
  env -u LD_PRELOAD -u LD_LIBRARY_PATH setsid "$0" --watch >> "$LOG" 2>&1 < /dev/null &

CONF="$DIR/MangoHud.conf"
if [[ -n $ER_FPS ]] || ((!ER_HUD)); then
  CONF="$DIR/logs/MangoHud-run.conf"
  {
    cat "$DIR/MangoHud.conf"
    [[ -n $ER_FPS ]] && printf 'fps_limit=%s\nfps_limit_method=early\n' "$ER_FPS"
    ((ER_HUD)) || echo "no_display"
  } > "$CONF"
fi
export MANGOHUD=1 MANGOHUD_CONFIGFILE="$CONF"
exec "$@"
