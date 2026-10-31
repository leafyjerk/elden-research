#!/usr/bin/env bash
# er-wine install: steps 6-7 of build-er-wine.sh, which runs this at the end.
#
#   6. checks the two ntdll.so builds against the shipped one (libraries, exports,
#      protocol, patch markers)
#   7. installs the patched one into the proton-cachyos-slr-er compat tool (made as a
#      copy of the stock tool if needed) and smoke-tests it in a scratch prefix
#
# Compiles nothing and runs nothing from the Wine source tree, so it can also be
# run on its own after a build. Logs to ~/.cache/elden-research/wine/install.log.
# To undo: switch Elden Ring back to the stock proton-cachyos-slr in Steam (the
# stock tool is never touched), or delete the proton-cachyos-slr-er folder.
set -Eeuo pipefail

W=${ER_WINE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/elden-research/wine}
STOCK=${ER_STOCK_TOOL:-/usr/share/steam/compatibilitytools.d/proton-cachyos-slr}
TOOL=${ER_TOOL:-$HOME/.local/share/Steam/compatibilitytools.d/proton-cachyos-slr-er}
SHIPPED=$STOCK/files/lib/wine/x86_64-unix/ntdll.so
MARK=WINE_SYNC_SPIN_US  # only the patched ntdll.so has this string (er-launch.sh checks it too)

exec > >(tee "$W/install.log") 2>&1
step() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
die() { printf '\nBUILD FAILED: %s\nFull log: %s\n' "$*" "$W/install.log"; exit 1; }
trap 'die "command failed at line $LINENO: $BASH_COMMAND"' ERR

step "6/7 check both builds against the shipped ntdll.so"
ver=$(cat "$W/protocol-version" 2> /dev/null) || die "no $W/protocol-version; run build-er-wine.sh"
for f in "$W/ntdll.so.stock-build" "$W/ntdll.so.er"; do
  [[ -f $f ]] || die "$f is missing; run build-er-wine.sh"
  need=$(readelf -d "$f" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p' | sort | tr '\n' ' ')
  glibc=$(objdump -T "$f" | grep -oE 'GLIBC_[0-9.]+' | sort -V | tail -1)
  echo "$(basename "$f"): needs $need; newest glibc symbol $glibc (Steam Linux Runtime 4 has 2.41)"
  [[ $need == "libc.so.6 libgcc_s.so.1 " ]] || die "$f links other libraries than the shipped one (libc, libgcc_s)"
done
for f in shipped stock-build er; do
  src=$W/ntdll.so.$f; [[ $f == shipped ]] && src=$SHIPPED
  nm -D --defined-only "$src" | awk '{print $NF}' | sort > "$W/exports.$f"
done
diff "$W/exports.shipped" "$W/exports.stock-build" > "$W/exports.diff" ||
  { head -20 "$W/exports.diff"; die "the stock build exports different symbols than the shipped ntdll.so"; }
echo "exports: stock build = shipped ($(wc -l < "$W/exports.shipped") symbols)"
diff -q "$W/exports.stock-build" "$W/exports.er" > /dev/null || die "the patch changed the exported symbols"
# Not "objdump | grep -q": grep stops at the first match, objdump then dies of
# SIGPIPE, and pipefail counts that as a failure (build attempt 2).
grep -qE "cmp +\\\$0x$(printf %x "$ver")," <(objdump -d --no-show-raw-insn "$W/ntdll.so.er") ||
  die "the protocol version check ($ver) wasn't found in the patched ntdll.so"
grep -qa "$MARK" "$W/ntdll.so.er" || die "the patched ntdll.so lacks the patch's $MARK string"
if grep -qa "$MARK" "$W/ntdll.so.stock-build"; then die "the stock build has $MARK; the source tree wasn't stock"; fi
grep -qa WINE_ER_POOL "$W/ntdll.so.er" || die "the patched ntdll.so lacks patch 2 (no WINE_ER_POOL string)"
echo "patched ntdll.so: OK"

step "7/7 install into the proton-cachyos-slr-er tool and smoke-test"
# The tool is a copy of the stock one (a reflink on btrfs, so no extra space) with its
# own name: pacman updates never touch it, and the stock tool stays as it is.
if [[ -d $TOOL ]] && ! cmp -s "$TOOL/version" "$STOCK/version"; then
  grep -q 'er-wine' "$TOOL/compatibilitytool.vdf" || die "$TOOL exists but isn't an er-wine tool; not touching it"
  echo "the stock Proton changed ($(cut -d' ' -f2 "$TOOL/version") -> $(cut -d' ' -f2 "$STOCK/version")): recreating the tool"
  rm -rf "$TOOL"
fi
if [[ ! -d $TOOL ]]; then
  mkdir -p "$(dirname "$TOOL")"
  cp -a --reflink=auto "$STOCK" "$TOOL"
  sed -i -E -e 's/^([[:space:]]*)"proton-cachyos-slr"[[:space:]]*$/\1"proton-cachyos-slr-er"/' \
    -e 's/ \(steam linux runtime\)"/"/' -e 's/("display_name"[[:space:]]+"[^"]*)"/\1 er-wine (Elden Ring patch)"/' \
    "$TOOL/compatibilitytool.vdf"
  grep -q '"proton-cachyos-slr-er"' "$TOOL/compatibilitytool.vdf" || die "couldn't rename the tool in its compatibilitytool.vdf"
  echo "made $TOOL"
fi
echo "Steam shows it as: $(sed -n 's/.*"display_name"[[:space:]]*"\(.*\)".*/\1/p' "$TOOL/compatibilitytool.vdf")"
dst=$TOOL/files/lib/wine/x86_64-unix/ntdll.so
[[ -f $TOOL/compatibilitytool.vdf && -f $dst ]] || die "compat tool copy incomplete: $TOOL"
# Only replace the stock ntdll.so or an er-wine build.
cmp -s "$dst" "$SHIPPED" || grep -qa "$MARK" "$dst" ||
  die "the tool's ntdll.so is neither the stock one nor an er-wine build; not overwriting it"
install -m 755 "$W/ntdll.so.er" "$dst"
echo "installed: $dst"

# Scratch prefix, all spins on in every process (no WINE_ER_EXE), log on. No
# Mono/Gecko: installing them adds a minute and has nothing to do with the patch.
export WINEPREFIX=$W/smoke-pfx WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" \
  WINE_SERVER_SPIN_US=20 WINE_SYNC_SPIN_US=20 WINE_ER_POOL=64 WINE_ER_LOG=$W/smoke-er.log
unset WINE_ER_EXE WINESERVER WINELOADER
rm -rf "$WINEPREFIX" "$W/smoke-er.log"
t0=$SECONDS
out=$(timeout 300 "$TOOL/files/bin/wine" cmd /c echo er-wine-smoke-ok 2>&1 | tail -3) || true
echo "first run (creates the prefix, $((SECONDS - t0)) s): $out"
out2=$(timeout 60 "$TOOL/files/bin/wine" cmd /c echo er-wine-smoke-ok 2>&1 | tail -3) || true
echo "second run: $out2"
timeout 30 "$TOOL/files/bin/wineserver" -k || true
[[ $out2 == *er-wine-smoke-ok* ]] || die "smoke test: the patched Wine didn't run cmd"
[[ -s $W/smoke-er.log ]] && { echo "smoke er-wine log:"; head -8 "$W/smoke-er.log"; }

step "done"
echo "The patched ntdll.so is installed in $TOOL"
echo "Next: restart Steam, then pick 'proton-cachyos-... er-wine (Elden Ring patch)'"
echo "in Elden Ring's Properties -> Compatibility."
