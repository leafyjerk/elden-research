#!/usr/bin/env bash
# elden-research's system part (root). elden-research install / oc-test / uninstall run
# it with sudo; safe to re-run.
#
#   sudo lib/system-setup.sh install USER   tweaks, helpers, services, polkit rule
#   sudo lib/system-setup.sh oc [MHZ]       er-oc.service at MHZ (default: the OC test's pick;
#                                           only a step that passed the test on this machine)
#   sudo lib/system-setup.sh oc off         remove er-oc.service (no OC)
#   sudo lib/system-setup.sh uninstall      undo everything above
#
# Permanent changes (until uninstall):
#   - vm.swappiness 10 (CachyOS's zram rule sets 150) and vm.watermark_scale_factor 200
#   - at boot (er-tune.service): GPU/USB interrupts, kswapd, kcompactd and khugepaged on
#     CPUs 0-1, and khugepaged collapses memory into 2 MB pages faster
# Helpers that do nothing until er-launch.sh starts them while the game runs:
#   er-boost (memory compaction), er-offline@N (SMT sibling offline), er-oc (the OC),
#   er-trace@TID (research profile only), plus a polkit rule that lets USER start them
#   (and scx_loader's schedulers) without a password.
# Every file written is listed in /etc/elden-research/manifest. A file that was there
# before is first copied to /etc/elden-research/backup/, and uninstall puts it back.
set -Eeuo pipefail
HERE=$(dirname "$(realpath "$0")")
ETC=/etc/elden-research
MANIFEST=$ETC/manifest
OCSTATE=/var/lib/elden-research/oc-test.json
UNITS=/etc/systemd/system

[[ $EUID == 0 ]] || { echo "run with sudo"; exit 1; }
mkdir -p "$ETC"
touch "$MANIFEST"

ours() { grep -qxF "$1" "$MANIFEST"; }

# put PATH MODE < content: writes PATH (root-owned) and lists it in the manifest.
put() {
  local path=$1 mode=$2 tmp
  tmp=$(mktemp)
  cat > "$tmp"
  if [[ -e $path ]] && ! ours "$path"; then
    mkdir -p "$ETC/backup$(dirname "$path")"
    cp -a "$path" "$ETC/backup$path"
    echo "  backed up the existing $path"
  fi
  install -D -m "$mode" "$tmp" "$path"
  rm -f "$tmp"
  ours "$path" || echo "$path" >> "$MANIFEST"
}

# CachyOS's zram udev rule sets vm.swappiness 150 when the zram device appears, after
# sysctl.d has run; a same-named rule in /etc replaces it. This is that rule with 10.
zram_rule() { sed 's/swappiness}="150"/swappiness}="10"/' /usr/lib/udev/rules.d/30-zram.rules; }

# The research version of these scripts (er-launch.sh --setup) wrote some of the same
# files without a manifest. Recognized by their content, they're moved to
# /etc/elden-research/legacy/ (uninstall doesn't restore them), so they aren't backed up
# as someone else's files.
legacy() {
  local f
  for f in $UNITS/er-oc.service $UNITS/er-tune.service $UNITS/er-irq-affinity.service $UNITS/er-boost.service \
    $UNITS/er-offline@.service $UNITS/er-trace@.service /etc/sysctl.d/99-gaming.conf /etc/udev/rules.d/30-zram.rules \
    /etc/polkit-1/rules.d/49-er-boost.rules /etc/polkit-1/rules.d/49-scx-loader.rules \
    /usr/local/bin/er-smu /usr/local/bin/er-tune /usr/local/bin/er-trace /usr/local/bin/er-irq-affinity; do
    [[ -e $f ]] && ! ours "$f" || continue
    case $f in
      */99-gaming.conf) [[ $(grep -v '^#' "$f") == $'vm.swappiness = 10\nvm.watermark_scale_factor = 200' ]] || continue ;;
      */30-zram.rules) [[ -f /usr/lib/udev/rules.d/30-zram.rules ]] && cmp -s "$f" <(zram_rule) || continue ;;
      */49-scx-loader.rules) grep -q 'isInGroup("wheel")' "$f" || continue ;;
      *) grep -qaE 'Elden Ring|er-boost|er-trace@|khugepaged' "$f" || continue ;;
    esac
    case $f in
      */er-oc.service) systemctl stop er-oc.service 2> /dev/null || true ;;  # stock clocks first
      *.service) systemctl disable "${f##*/}" 2> /dev/null || true ;;
    esac
    mkdir -p "$ETC/legacy$(dirname "$f")"
    mv "$f" "$ETC/legacy$f"
    echo "  moved the research version's $f to $ETC/legacy/"
  done
}

install_all() {
  local user=$1 i p
  [[ $user =~ ^[a-z_][a-z0-9_-]*$ ]] && id "$user" > /dev/null 2>&1 || { echo "not a user name: $user"; exit 1; }
  legacy
  if [[ -f /usr/lib/udev/rules.d/30-zram.rules ]] && grep -q 'swappiness}="150"' /usr/lib/udev/rules.d/30-zram.rules; then
    zram_rule | put /etc/udev/rules.d/30-zram.rules 644
    udevadm control --reload 2> /dev/null || true
  fi
  put /etc/sysctl.d/99-elden-research.conf 644 <<'EOF'
# elden-research: fewer memory-reclaim stalls in the game's threads.
# swappiness 10: keep the game's memory in RAM; idle apps go to zram instead.
# watermark_scale_factor 200: kswapd starts freeing memory ~280 MB earlier, so the
# game's own threads rarely have to reclaim (direct reclaim = hitches).
vm.swappiness = 10
vm.watermark_scale_factor = 200
EOF
  sysctl -q -p /etc/sysctl.d/99-elden-research.conf

  # er-boost: compact memory at game launch so its allocations get 2 MB pages
  # (4K pages cost +38% memory latency at 1 GB working sets, measured).
  put $UNITS/er-boost.service 644 <<'EOF'
[Unit]
Description=Compact memory before Elden Ring loads (huge pages; elden-research)

[Service]
Type=oneshot
CPUAffinity=0-1
ExecStart=/bin/sh -c 'echo 1 > /proc/sys/vm/compact_memory'
EOF
  # er-offline@N: CPU N offline while the game runs. An idle SMT sibling still
  # holds half its core's reorder buffer (membench mlp: 26.2 -> 21.9 ns/load
  # with CPU 3 offline), so the main thread gets the whole core.
  put $UNITS/er-offline@.service 644 <<'EOF'
[Unit]
Description=CPU %i offline while Elden Ring runs (frees its SMT sibling's core resources; elden-research)

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo 0 > /sys/devices/system/cpu/cpu%i/online'
ExecStop=/bin/sh -c 'echo 1 > /sys/devices/system/cpu/cpu%i/online'
EOF
  # At every boot: GPU/USB interrupts (they land on the second CCX by default) and the
  # memory-management kernel threads on CPUs 0-1, out of the game's way; khugepaged
  # collapses into 2 MB pages ~80x faster than the default, but only mostly-filled
  # ranges (max_ptes_none 128), so memory use doesn't balloon.
  put /usr/local/bin/er-tune 755 <<'EOF'
#!/bin/sh
# elden-research: interrupt and kernel-thread affinity, khugepaged (run at boot by er-tune.service).
for i in $(awk -F: '/amdgpu|xhci_hcd/ {print $1 + 0}' /proc/interrupts); do
  echo 0-1 > /proc/irq/$i/smp_affinity_list
done
for p in $(pgrep -x 'kswapd0|kcompactd0|khugepaged'); do
  taskset -pc 0-1 $p > /dev/null
done
T=/sys/kernel/mm/transparent_hugepage/khugepaged
echo 32768 > $T/pages_to_scan
echo 1000 > $T/scan_sleep_millisecs
echo 128 > $T/max_ptes_none
EOF
  put $UNITS/er-tune.service 644 <<'EOF'
[Unit]
Description=Elden Ring tuning: IRQ/kernel-thread affinity, khugepaged (elden-research)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/er-tune

[Install]
WantedBy=multi-user.target
EOF
  # The OC tool (er-oc.service runs it; written by "oc" below after the OC test).
  put /usr/local/bin/er-smu 755 < "$HERE/er-smu.py"
  # er-trace@TID: 5 s trace of one thread's sleeps for the launcher's research profile
  # (ER_PROFILE=1). Root because the sched tracepoints and kernel symbols are root-only.
  put /usr/local/bin/er-trace 755 < "$HERE/../research/er-trace.py"
  put $UNITS/er-trace@.service 644 <<'EOF'
[Unit]
Description=Trace thread %i's sleeps for the Elden Ring profile (elden-research)

[Service]
Type=oneshot
CPUAffinity=0-1
Nice=10
ExecStart=/usr/local/bin/er-trace %i
StandardOutput=truncate:/run/er-trace-%i.txt
EOF
  put /etc/polkit-1/rules.d/49-elden-research.rules 644 <<EOF
// elden-research: lets $user start the game-time helpers (er-launch.sh) and
// scx_loader's schedulers without a password. Removed by elden-research uninstall.
polkit.addRule(function(action, subject) {
    if (subject.user != "$user")
        return;
    if (action.id == "org.scx.loader.manage-schedulers")
        return polkit.Result.YES;
    if (action.id != "org.freedesktop.systemd1.manage-units")
        return;
    var unit = action.lookup("unit"), verb = action.lookup("verb");
    if (unit == "er-boost.service" && verb == "start")
        return polkit.Result.YES;
    if (/^er-offline@[0-9]+\.service\$/.test(unit) && (verb == "start" || verb == "stop"))
        return polkit.Result.YES;
    if (unit == "er-oc.service" && (verb == "start" || verb == "stop"))
        return polkit.Result.YES;
    if (/^er-trace@[0-9]+\.service\$/.test(unit) && verb == "start")
        return polkit.Result.YES;
});
EOF
  systemctl daemon-reload
  systemctl enable --now er-tune.service 2>&1 | sed 's/^/  /'
  echo "swappiness $(sysctl -n vm.swappiness), watermark_scale_factor $(sysctl -n vm.watermark_scale_factor)"
  for i in $(awk -F: '/amdgpu|xhci_hcd/ {print $1 + 0}' /proc/interrupts); do
    echo "IRQ $i -> CPUs $(cat /proc/irq/$i/effective_affinity_list)"
  done
  for p in $(pgrep -x 'kswapd0|kcompactd0|khugepaged'); do
    echo "$(cat /proc/$p/comm) -> CPUs $(taskset -pc $p | awk '{print $NF}')"
  done
  echo "khugepaged: $(cat /sys/kernel/mm/transparent_hugepage/khugepaged/pages_to_scan) pages per $(cat /sys/kernel/mm/transparent_hugepage/khugepaged/scan_sleep_millisecs) ms"
}

oc_unit() {
  local mhz=${1:-} scale
  if [[ $mhz == off ]]; then
    systemctl stop er-oc.service 2> /dev/null || true
    rm -f $UNITS/er-oc.service
    sed -i "\|^$UNITS/er-oc.service\$|d" "$MANIFEST"
    systemctl daemon-reload
    echo "er-oc.service removed: stock clocks in the game too"
    return
  fi
  # Only the OC test's pick, or a lower step that also passed it on this machine.
  read -r mhz scale < <(python3 -I - "$OCSTATE" "$mhz" <<'EOF'
import json, sys
try:
    st = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit("no OC test result yet: run elden-research oc-test")
rec = st.get("recommended")
if not rec:
    sys.exit("the OC test didn't recommend an OC on this machine: " + str(st.get("result")))
mhz = int(sys.argv[2] or rec["mhz"])
ok = [s for s in st.get("steps", []) if s.get("ok") and s["mhz"] == mhz and mhz <= rec["mhz"]]
if not ok:
    sys.exit(f"{mhz} MHz isn't the OC test's pick ({rec['mhz']} MHz) or a lower step that passed it")
print(mhz, rec["scale"] if mhz == rec["mhz"] else ok[-1]["scale"])
EOF
  ) && [[ -n ${scale:-} ]] || exit 1
  put $UNITS/er-oc.service 644 <<EOF
# elden-research: started by er-launch.sh when Elden Ring starts and stopped when it
# exits, so no other game runs overclocked. No [Install] section on purpose: it must
# never run at boot (a bad OC then still ends with a power cycle).
[Unit]
Description=BC-250 CPU overclock while Elden Ring runs: $mhz MHz, VID curve scale $scale (elden-research)

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/er-smu apply $mhz $scale
ExecStop=/usr/local/bin/er-smu stock
EOF
  systemctl daemon-reload
  echo "er-oc.service: $mhz MHz, VID curve scale $scale, only while the game runs"
}

uninstall_all() {
  local f u
  systemctl stop er-oc.service 2> /dev/null || true  # stock clocks before er-smu goes
  for u in $(systemctl list-units --plain --no-legend 'er-offline@*' | awk '{print $1}'); do
    systemctl stop "$u" || true  # CPUs back online
  done
  systemctl disable er-tune.service 2>&1 | sed 's/^/  /' || true
  tac "$MANIFEST" | while read -r f; do
    [[ -n $f ]] || continue
    rm -f "$f"
    if [[ -e $ETC/backup$f ]]; then
      mv "$ETC/backup$f" "$f"
      echo "  restored $f"
    else
      echo "  removed $f"
    fi
  done
  rm -rf "$ETC/backup" "$MANIFEST" /var/lib/elden-research
  rmdir "$ETC" 2> /dev/null || echo "  kept $ETC/legacy (the research version's files)"
  systemctl daemon-reload
  udevadm control --reload 2> /dev/null || true
  sysctl -q --system || true
  echo "System part removed. Reboot to get the boot-time defaults back (interrupt"
  echo "affinity, khugepaged, and CachyOS's zram swappiness)."
}

case ${1:-} in
  install) [[ -n ${2:-} ]] || { sed -n 5,9p "$0"; exit 1; }; install_all "$2" ;;
  oc) oc_unit "${2:-}" ;;
  uninstall) uninstall_all ;;
  *) sed -n 5,9p "$0"; exit 1 ;;
esac
