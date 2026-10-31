"""Compare the request order in a Wine source's server/protocol.def with the
request-name table compiled into a shipped wineserver binary.
usage: python3 -I check_requests.py <protocol.def> <wineserver>"""
import re, subprocess, sys
reqs = re.findall(r"^@REQ\((\w+)\)", open(sys.argv[1]).read(), re.M)
strs = subprocess.run(["strings", "-n", "3", sys.argv[2]], capture_output=True, text=True).stdout.split("\n")
# the binary's req_names table: find the longest run of consecutive strings equal to the request names in order
pos = {s: i for i, s in enumerate(strs) if s in set(reqs)}
first = strs.index(reqs[0]) if reqs[0] in strs else -1
run = strs[first:first + len(reqs)] if first >= 0 else []
same = sum(a == b for a, b in zip(run, reqs))
print(f"protocol.def: {len(reqs)} requests; shipped table run from index {first}: {same}/{len(reqs)} match in order")
if same != len(reqs):
    for i, (a, b) in enumerate(zip(run, reqs)):
        if a != b:
            print("first difference at", i, "binary:", a, "source:", b); break
# tail merging: a name that is the suffix of another name may not exist as its own string
present = [r for r in reqs if r in pos]
merged = [r for r in reqs if r not in pos and any(o.endswith(r) and o != r for o in reqs)]
order = [pos[r] for r in present]
inorder = all(a < b for a, b in zip(order, order[1:]))
print(f"standalone in binary: {len(present)}, missing but tail-mergeable: {len(merged)}, "
      f"missing otherwise: {[r for r in reqs if r not in pos and r not in merged]}, in order: {inorder}")
# positions: first occurrence at/after the table start (names can also appear elsewhere)
pos2 = {}
for i in range(first, min(len(strs), first + 2 * len(reqs))):
    pos2.setdefault(strs[i], i)
order2 = [(pos2[r], r) for r in reqs if r in pos2]
bad = [(a[1], b[1]) for a, b in zip(order2, order2[1:]) if a[0] > b[0]]
print(f"in table region: {len(order2)} found, out-of-order pairs: {len(bad)} {bad[:5]}")
