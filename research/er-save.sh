#!/usr/bin/env bash
# Benchmark save. The game rewrites ER0000.sl2 on every quit, so each run would
# start in a different world state (time of day, weather), and the main thread's
# work per frame drifts by ~7-9% between runs. Restore one snapshot before each
# benchmark run instead (with the game closed):
#   er-save.sh snapshot   copy the current save to saves/bench/
#   er-save.sh restore    put saves/bench/ back (the current save goes to saves/last/ first)
# Never restore when you play for real: it rolls your progress back to the snapshot.
# Saves are kept in ~/.local/share/elden-research/saves (ER_SAVES overrides).
SAVES=${ER_SAVES:-${XDG_DATA_HOME:-$HOME/.local/share}/elden-research/saves}
ROOT=$HOME/.local/share/Steam/steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing
# The save folder is named after the Steam account's ID; with several, the most recently used one.
SAVE=$(ls -1td "$ROOT"/[0-9]*/ 2> /dev/null | head -1)
SAVE=${SAVE%/}

[[ -n $SAVE && -f $SAVE/ER0000.sl2 ]] || { echo "No Elden Ring save found under $ROOT"; exit 1; }
pgrep -x eldenring.exe > /dev/null && { echo "Quit the game first"; exit 1; }
case $1 in
  snapshot)
    mkdir -p "$SAVES/bench" && cp -p "$SAVE"/ER0000.sl2* "$SAVES/bench/" &&
      echo "snapshot of the $(date -r "$SAVE/ER0000.sl2" '+%F %T') save in $SAVES/bench/" ;;
  restore)
    [[ -f $SAVES/bench/ER0000.sl2 ]] || { echo "No snapshot yet: er-save.sh snapshot"; exit 1; }
    # Plain cp (new mtime), so Steam Cloud sees a normal local change and uploads it.
    mkdir -p "$SAVES/last" && cp -p "$SAVE"/ER0000.sl2* "$SAVES/last/" &&
      cp "$SAVES/bench"/ER0000.sl2* "$SAVE/" &&
      echo "bench save restored (the save it replaced is in $SAVES/last/)" ;;
  *) sed -n 2,9p "$0"; exit 1 ;;
esac
