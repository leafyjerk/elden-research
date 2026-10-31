"""Compare syscall ids in a shipped PE ntdll.dll against the ids Wine's
tools/make_specfiles would assign from a source tree's ntdll.spec (64-bit).
usage: python3 -I check_syscalls.py <ntdll.dll> <ntdll.spec>"""
import re, subprocess, sys, struct
dll, spec = sys.argv[1], sys.argv[2]
data = open(dll, "rb").read()
pe = subprocess.run(["objdump", "-p", dll], capture_output=True, text=True).stdout
hdrs = subprocess.run(["objdump", "-h", dll], capture_output=True, text=True).stdout
base = int(re.search(r"ImageBase\s+([0-9a-f]+)", pe)[1], 16)
secs = [(int(v, 16) - base, int(s, 16), int(o, 16)) for s, v, o in
        re.findall(r"^\s*\d+\s+\S+\s+([0-9a-f]+)\s+([0-9a-f]+)\s+[0-9a-f]+\s+([0-9a-f]+)", hdrs, re.M)]
def off(rva):
    for v, s, o in secs:
        if v <= rva < v + s:
            return rva - v + o
shipped = {}
rvas = {int(i): int(a, 16) for i, a in re.findall(r"^\s*\[\s*(\d+)\]\s+\+base\[\s*\d+\]\s+([0-9a-f]+)\s+Export RVA", pe, re.M)}
names = re.findall(r"^\s*\[\s*(\d+)\]\s+\+base\[\s*\d+\]\s+[0-9a-f]{4}\s+(\w+)$", pe[pe.index("[Ordinal/Name Pointer]"):], re.M)
for idx, name in names:
    rva = rvas.get(int(idx))
    o = off(rva) if rva is not None else None
    if o is not None and data[o:o + 4] == b"\x4c\x8b\xd1\xb8":
        shipped[name] = struct.unpack("<I", data[o + 4:o + 8])[0]
    elif o is not None and name in ("NtCreateEvent", "NtClose"):
        print("first bytes of", name, data[o:o + 16].hex())
# make_specfiles: functions with -syscall; explicit -syscall=N ids; rest alphabetical into free slots
funcs = {}
LINE = re.compile(r"^\s*(@|\d+)\s+(stdcall|cdecl|varargs|thiscall|stub|extern)\s+((?:-\S+\s+)*)([A-Za-z0-9_@$?]+)")
for line in open(spec):
    m = LINE.match(line)
    if not m or "-syscall" not in m[3] or m[4] in funcs:
        continue
    flags, name = m[3].split(), m[4]
    if any(f.startswith("-arch=win32") for f in flags):  # make_specfiles: skip 64-bit if /-arch=win32/
        continue
    sid = next((f[9:] for f in flags if f.startswith("-syscall=")), None)
    funcs[name] = int(sid, 0) if sid else None
used = {v for v in funcs.values() if v is not None}
nxt = 0
for name in sorted(n for n, v in funcs.items() if v is None):
    while nxt in used:
        nxt += 1
    funcs[name] = nxt; used.add(nxt); nxt += 1
common = [n for n in shipped if n in funcs]
bad = [(n, hex(shipped[n]), hex(funcs[n])) for n in sorted(common) if shipped[n] != funcs[n]]
print(f"shipped ntdll.dll: {len(shipped)} syscall thunks; spec: {len(funcs)} syscalls; compared {len(common)}")
print("only in shipped:", sorted(set(shipped) - set(funcs))[:10], " only in spec:", sorted(set(funcs) - set(shipped))[:10])
print("id mismatches:", len(bad), bad[:15])
