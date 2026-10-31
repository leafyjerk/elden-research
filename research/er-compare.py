#!/usr/bin/env python3
"""Compares MangoHud runs: average, median, 1% and 0.1% lows (mean of the worst
frames), long frames, worst frame, and the average FPS of each 20 s window.

  ./er-compare.py logs/eldenring_A.csv logs/eldenring_B.csv ...
"""
import csv, sys, statistics as st


def load(p):
    rows = list(csv.reader(open(p)))
    h = rows[2]; i_ft = h.index('frametime'); i_el = h.index('elapsed')
    ft = []; el = []
    for r in rows[3:]:
        try: ft.append(float(r[i_ft])); el.append(int(r[i_el]))
        except: pass
    return ft, el
def lows(ft, q):
    s = sorted(ft, reverse=True); n = max(1, int(len(s) * q))
    return 1000 / st.mean(s[:n])
for p in sys.argv[1:]:
    ft, el = load(p)
    t0 = el[0]; dur = (el[-1] - t0) / 1e9
    avg = len(ft) / (sum(ft) / 1000)
    win = []
    for w in range(0, int(dur), 20):
        f = [x for x, e in zip(ft, el) if w <= (e - t0) / 1e9 < w + 20]
        if len(f) > 100: win.append(len(f) / (sum(f) / 1000))
    print(f"{p.split('/')[-1][10:29]}  {dur:5.0f}s  avg {avg:5.2f}  med {1000/st.median(ft):5.2f}  1%low {lows(ft,.01):5.2f}  0.1%low {lows(ft,.001):5.2f}  "
          f">20ms {100*sum(x>20 for x in ft)/len(ft):4.1f}%  >25ms {100*sum(x>25 for x in ft)/len(ft):4.2f}%  worst {max(ft):5.1f}ms")
    print("      20s windows:", " ".join(f"{x:.1f}" for x in win))
