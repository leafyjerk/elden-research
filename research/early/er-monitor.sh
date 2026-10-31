#!/usr/bin/env bash
# Start this BEFORE launching Elden Ring. It waits for the game, gives it time
# to load, then logs GPU clock + the busiest game threads once a second so we
# can tell whether we're GPU-bound or main-thread-bound.
# Usage: ./er-monitor.sh [seconds]   (default 180). Ctrl+C to stop early.

DUR="${1:-180}"
OUT="$(dirname "$(realpath "$0")")/logs/monitor-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$OUT")"
SCLK=/sys/class/drm/card1/device/pp_dpm_sclk

echo "Waiting for eldenring.exe... (launch the game now)"
until PID=$(pgrep -f 'eldenring.exe' | head -1) && [[ -n $PID ]]; do sleep 2; done
echo "Found PID $PID. Waiting 90s for you to load into the world..."
sleep 90
echo "Logging for ${DUR}s -> $OUT  (play normally, open world is best)"

for ((i = 0; i < DUR; i++)); do
  kill -0 "$PID" 2>/dev/null || { echo "Game exited."; break; }
  {
    echo "=== $(date +%T)  gpu: $(grep '\*' "$SCLK" | awk '{print $2}')  temp: $(( $(cat /sys/class/drm/card1/device/hwmon/hwmon*/temp1_input 2>/dev/null || echo 0) / 1000 ))C"
    # top 6 threads by CPU% (100% = one full core)
    top -H -b -n1 -d1 -p "$PID" | awk 'NR>7 && NF {printf "  %6s%%  %s\n", $9, $12}' | head -6
  } >> "$OUT"
  sleep 1
done
echo "Done: $OUT"
