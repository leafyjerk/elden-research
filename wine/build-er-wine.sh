#!/usr/bin/env bash
# er-wine build: the installed proton-cachyos-slr's ntdll.so with the Elden Ring patches
# (0001-er-main-thread-spin.patch, then 0002-er-event-pool.patch; see RESEARCH.md).
#
#   1. gets the Wine source the installed Proton was built from: CachyOS/wine-cachyos at
#      the commit its tag pins (~400 MB download, once)
#   2. prepares it the way Proton's build does (once) and checks it against the shipped
#      binaries (server protocol version, syscall numbers)
#   3. configures an out-of-tree build (once)
#   4. builds a stock ntdll.so (for comparison) and 5. a patched one
#   6-7. install-er-wine.sh, run at the end: checks both against the shipped ntdll.so,
#      installs the patched one into the proton-cachyos-slr-er compat tool, smoke-tests it
#
# Safe to re-run. Takes ~10-20 minutes the first time. Everything is logged to
# ~/.cache/elden-research/wine/build.log. Nothing outside that folder and the
# proton-cachyos-slr-er tool is changed; the stock Proton is never touched.
# Env overrides: ER_WINE_DIR (work folder), ER_STOCK_TOOL, ER_TOOL, ER_WINE_COMMIT.
set -Eeuo pipefail

HERE=$(dirname "$(realpath "$0")")
W=${ER_WINE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/elden-research/wine}
SRC=$W/src
B=$W/build64
PATCHES=("$HERE/0001-er-main-thread-spin.patch" "$HERE/0002-er-event-pool.patch")
# reverts the patches, last first (each one was made on top of the previous ones)
unpatch() { local i; for ((i = ${#PATCHES[@]} - 1; i >= 0; i--)); do git -C "$SRC" apply -R "${PATCHES[i]}"; done; }
STOCK=${ER_STOCK_TOOL:-/usr/share/steam/compatibilitytools.d/proton-cachyos-slr}
SHIPPED=$STOCK/files/lib/wine/x86_64-unix/ntdll.so
REPO=https://github.com/CachyOS/wine-cachyos.git
# Proton tag -> the wine-cachyos commit its wine submodule points at, for the versions
# this was tested with. Others are looked up on GitHub (and must pass every check below).
declare -A KNOWN=([cachyos-11.0-20261015-slr]=cd14106e4fc0f96006005b07985e49cc1517e9c0)
# Proton's x86_64 CFLAGS (Makefile.in at tag cachyos-11.0-20261015-slr, GCC branch)
PROTON_CFLAGS="-O2 -march=nocona -mtune=core-avx2 -pipe -mcmodel=small -mfpmath=sse -fwrapv -fno-strict-aliasing
  -ffunction-sections -fdata-sections -fno-omit-frame-pointer -mno-avx -mno-avx2 -mno-avx512f -fvect-cost-model=cheap
  -Wno-stringop-overflow -Wno-incompatible-pointer-types -Wno-discarded-qualifiers"
PROTON_CFLAGS=$(echo $PROTON_CFLAGS)

die() { printf '\nBUILD FAILED: %s\nFull log: %s\n' "$*" "$W/build.log"; exit 1; }
[[ $W != *[[:space:]]* ]] || { echo "The work folder $W has a space in its path, which autotools can't build in. Set ER_WINE_DIR."; exit 1; }
[[ -f $SHIPPED && -f $STOCK/version ]] || { echo "No proton-cachyos-slr at $STOCK (sudo pacman -S proton-cachyos-slr)"; exit 1; }
mkdir -p "$W"
exec > >(tee "$W/build.log") 2>&1
step() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
trap 'die "command failed at line $LINENO: $BASH_COMMAND"' ERR
JOBS=$(nproc)

step "1/7 get the Wine source"
tag=$(cut -d' ' -f2 "$STOCK/version")
wine_commit=${ER_WINE_COMMIT:-${KNOWN[$tag]:-}}
if [[ -z $wine_commit ]]; then
  echo "$tag isn't a tested version: looking up its Wine commit on GitHub"
  wine_commit=$(curl -fsSL "https://api.github.com/repos/CachyOS/proton-cachyos/contents/wine?ref=$tag" |
    python3 -I -c 'import json, sys; print(json.load(sys.stdin)["sha"])') || die "couldn't look up the Wine commit for $tag"
fi
echo "Proton $tag -> CachyOS/wine-cachyos @ $wine_commit"
if [[ -d $SRC/.git ]] && git -C "$SRC" merge-base --is-ancestor "$wine_commit" HEAD 2> /dev/null; then
  echo "already downloaded"
else
  rm -rf "$SRC" "$B"
  git init -q "$SRC"
  git -C "$SRC" fetch -q --depth 1 "$REPO" "$wine_commit"
  git -C "$SRC" checkout -q FETCH_HEAD
  echo "downloaded ($(du -sh "$SRC" | cut -f1))"
fi

step "2/7 prepare the source like Proton's build (make_vulkan, make_specfiles, make_requests, autoreconf)"
cd "$SRC"
# make_vulkan generates include/wine/vulkan.h (needed by configure's makedep) and the
# winevulkan thunks, from the vk.xml/video.xml in dlls/winevulkan (no download).
make_vulkan() { XDG_CACHE_HOME=$W/cache dlls/winevulkan/make_vulkan -x vk.xml -X video.xml; }
commit() { git add -A; git -c user.name=er-wine -c user.email=er-wine@localhost commit -q -m "$1"; }
if git log -1 --format=%s | grep -q '^er-wine: Proton-style prep'; then
  echo "already prepared"
else
  [[ -z $(git status --porcelain) ]] || die "the source tree has local changes; expected a clean checkout of $wine_commit"
  make_vulkan
  tools/make_specfiles
  tools/make_requests
  autoreconf -fiv > "$W/autoreconf.log" 2>&1 || { tail -20 "$W/autoreconf.log"; die "autoreconf"; }
  commit "er-wine: Proton-style prep (make_vulkan, make_specfiles, make_requests, autoreconf)"
fi
[[ -f include/wine/vulkan.h ]] || die "make_vulkan didn't create include/wine/vulkan.h"
# Only ntdll.so gets replaced, so the rest of Proton (ntdll.dll, wineserver) must speak
# exactly the protocol and syscall numbering this source does.
ver=$(sed -n 's/^#define SERVER_PROTOCOL_VERSION //p' include/wine/server_protocol.h)
echo "server protocol version: $ver"
grep -qE "cmp +\\\$0x$(printf %x "$ver")," <(objdump -d --no-show-raw-insn "$SHIPPED") ||
  die "this source has server protocol $ver, but the shipped ntdll.so checks for another version"
echo "$ver" > "$W/protocol-version"
python3 -I "$HERE/tools/check_syscalls.py" "$STOCK/files/lib/wine/x86_64-windows/ntdll.dll" dlls/ntdll/ntdll.spec |
  tee "$W/syscalls.txt"
grep -q '^id mismatches: 0 ' "$W/syscalls.txt" && grep -qE 'compared [1-9][0-9]+' "$W/syscalls.txt" ||
  die "the shipped ntdll.dll's syscall numbers don't match this source"
# A run that failed midway may have left patches applied.
for ((i = ${#PATCHES[@]} - 1; i >= 0; i--)); do
  if git apply -R --check "${PATCHES[i]}" 2> /dev/null; then git apply -R "${PATCHES[i]}"; fi
done

step "3/7 configure (once)"
mkdir -p "$B"
cd "$B"
conf() {
  "$SRC/configure" -C --enable-win64 --with-mingw=clang --enable-build-id --disable-tests \
    --without-unwind --without-hwloc --without-oss CFLAGS="$PROTON_CFLAGS" "$@" > "$W/configure.log" 2>&1
}
if [[ -f Makefile ]]; then
  echo "already configured"
elif ! { rm -f config.cache; conf --enable-archs=x86_64,i386; }; then
  echo "configure for x86_64,i386 failed:"; tail -5 "$W/configure.log"; echo "retrying for x86_64 only"
  rm -f config.cache
  conf --enable-archs=x86_64 || { tail -30 "$W/configure.log"; die "configure"; }
fi
grep -E "^(HOST_ARCH|PE_ARCHS|UNWIND_LIBS|HWLOC_LIBS) *=" Makefile || true

step "4/7 build the stock ntdll.so (no patch), for comparison"
make -j"$JOBS" dlls/ntdll/ntdll.so > "$W/make-stock.log" 2>&1 || { tail -40 "$W/make-stock.log"; die "make (stock)"; }
cp dlls/ntdll/ntdll.so "$W/ntdll.so.stock-build"

step "5/7 build the patched ntdll.so"
for p in "${PATCHES[@]}"; do
  git -C "$SRC" apply --check "$p" 2> /dev/null || { unpatch 2> /dev/null || true
    die "$(basename "$p") doesn't apply to this Wine version (it was made for ${KNOWN[cachyos-11.0-20261015-slr]:0:8})"; }
  git -C "$SRC" apply "$p"
done
if ! make -j"$JOBS" dlls/ntdll/ntdll.so > "$W/make-er.log" 2>&1; then
  grep -E "error|implicit declaration" "$W/make-er.log" | head -40
  unpatch
  die "make (patched)"
fi
cp dlls/ntdll/ntdll.so "$W/ntdll.so.er"
unpatch   # keep the tree stock: the .patch files are the source of truth
echo "compiler warnings in the patched files:"
grep -E "unix/(sync|server|loader|virtual|signal_x86_64)\.c.*warning" "$W/make-er.log" | head -20 || echo "  none"

# 6-7: check, install and smoke-test (a separate script: it compiles nothing, so it
# can be re-run on its own).
exec "$HERE/install-er-wine.sh"
