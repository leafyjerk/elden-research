"""Syntax-check patched Wine unix files with gcc and report only errors on lines
the patch added. usage: python3 -I syntax_check.py <patched root> <patch> <wine include dir> <out dir>"""
import os, re, subprocess, sys
root, patch, inc, out = sys.argv[1:5]
unix = os.path.join(root, "dlls/ntdll/unix")
files = ["sync.c", "server.c", "signal_x86_64.c", "virtual.c", "loader.c"]
# config.h: HAVE_FOO_BAR_H for every header these files test that exists on this system, plus common functions
macros = set()
for f in files + ["unix_private.h"]:
    macros |= set(re.findall(r"\bHAVE_[A-Z0-9_]+", open(os.path.join(unix, f)).read()))
lines = ["#define _GNU_SOURCE 1"]
for m in sorted(macros):
    if m.endswith("_H"):
        parts = m[5:-2].lower().split("_")
        cands = ["/".join(parts) + ".h", "_".join(parts) + ".h"] + \
                ["/".join(parts[:i]) + "/" + "_".join(parts[i:]) + ".h" for i in range(1, len(parts))]
        if any(os.path.exists("/usr/include/" + c) for c in cands):
            lines.append(f"#define {m} 1")
for m in ("HAVE_CLOCK_GETTIME", "HAVE_PTHREAD_GETATTR_NP", "HAVE_SCHED_SETAFFINITY", "HAVE_SCHED_YIELD",
          "HAVE_GETAUXVAL", "HAVE_PRCTL", "HAVE_DLADDR1", "HAVE_DL_ITERATE_PHDR", "HAVE_STRUCT_STAT_ST_MTIM",
          "HAVE_LINUX_NTSYNC_H"):
    lines.append(f"#define {m} 1")
lines.append('#define SYSTEMDLLPATH ""')
lines.append('#define BINDIR "/usr/bin"')
lines.append('#define LIBDIR "/usr/lib"')
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "config.h"), "w").write("\n".join(lines) + "\n")
# lines added by the patch, per file (new-file line numbers)
added, cur, ln = {}, None, 0
for l in open(patch):
    if l.startswith("+++ b/"):
        cur = os.path.basename(l[6:].strip()); added.setdefault(cur, set())
    elif l.startswith("@@"):
        ln = int(re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", l)[1])
    elif cur and l.startswith("+"):
        added[cur].add(ln); ln += 1
    elif cur and l.startswith(" "):
        ln += 1
total = 0
for f in files:
    r = subprocess.run(["gcc", "-fsyntax-only", "-std=gnu11", "-DWINE_UNIX_LIB", "-D__WINESRC__", "-D_NTSYSTEM_",
                        "-Wall", "-Wno-unused-function", "-I", out, "-I", os.path.join(root, "dlls/ntdll"), "-I", unix,
                        "-I", inc, os.path.join(unix, f)], capture_output=True, text=True)
    errs = [m for m in re.finditer(r"^.*?/" + re.escape(f) + r":(\d+):\d+: (error|warning): (.*)$", r.stderr, re.M)]
    mine = [e for e in errs if int(e[1]) in added.get(f, set())]
    other = sum(1 for e in errs if e[2] == "error")
    total += len(mine)
    print(f"{f}: {len(mine)} diagnostics on patched lines; {other} errors elsewhere (config artifacts)")
    for e in mine:
        print("   ", e[0][e[0].index(f):])
sys.exit(1 if total else 0)
