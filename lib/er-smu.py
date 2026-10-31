#!/usr/bin/env python3
"""Minimal BC-250 SMU access (mailbox queue 3 only) for elden-research.

Same protocol as bc250-collective's bc250_smu_oc and cyan-skillfish-governor:
SMN index/data registers at 0xB8/0xBC in the root complex's PCI config space.
The governor uses the same registers, so it's paused while this runs, and the
lock is held for the whole session (theirs only covers single accesses).
Refuses to run on anything but an AMD BC-250.

  sudo er-smu status          per-core clocks and CPU voltage
  sudo er-smu qualify [--max MHZ] [--pace quick|normal|thorough] [--burn PATH] [--json]
                              the OC safety test (see qualify); result in /var/lib/elden-research/oc-test.json
  sudo er-smu apply MHZ SCALE max boost clock + VID curve scale, checked on one loaded core
                              (er-oc.service runs this while the game runs)
  sudo er-smu stock           undo all OC settings (same as bc250_smu_oc's revert_defaults)
  sudo er-smu profile N       SMU perf profile 0-3 (memory controller / fabric)
  sudo er-smu profile-test    membench at the boot profile, then 3, 2, 1; leaves the best
  sudo er-smu probe           clocks/voltage/temperature under 16- and 8-thread load,
                              with and without the OC tool's undervolt; ends at stock
  sudo er-smu oc [MHZ] [MV]   the original research's OC search (see oc_detect); qualify replaces it

Installed as /usr/local/bin/er-smu by lib/system-setup.sh; lib/er-smu.py in the repo is the same file.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from contextlib import contextmanager

CONFIG = "/sys/bus/pci/devices/0000:00:00.0/config"
CMD, RSP, ARG = 0x03B10A20, 0x03B10A80, 0x03B10A88  # queue 3 mailbox (SMN addresses)
GOVERNOR = "cyan-skillfish-governor-smu.service"
HERE = os.path.dirname(os.path.realpath(__file__))
MEMBENCH = os.path.join(HERE, "membench")
STATE = "/var/lib/elden-research/oc-test.json"
VMAX = 1275  # mV: hard cap for apply and qualify (community advice: never above 1300)
# qualify's ladder: (MHz, VID curve scale). 3800/-20 (~1231 mV by the model) ran in every
# game session of the original research. The others follow bc250_smu_oc's V/F model:
# 3600/0 ~1220 mV, 3700/-8 ~1239 mV, 3900/-25 ~1267 mV (3900 only with --max 3900).
LADDER = ((3600, 0), (3700, -8), (3800, -20), (3900, -25))
# Temperatures (Tctl, C) under qualify's game-like load, which runs ~5 C cooler than the
# game itself (no GPU heat on the shared die): stock at or above T_BASE means the cooling
# is too weak for any OC; an OC step at or above T_FAIL fails, at or above T_WARN it passes
# with a thin margin (the recommendation then goes one step lower). T_ABORT is the
# throttle point the OC settings give the SMU: any sample there stops the phase.
T_BASE, T_FAIL, T_WARN, T_ABORT = 80, 85, 80, 90
# Seconds: each core alone, the game-like mix per step, the final soak, the stock baseline.
PACES = {"quick": dict(core=4, game=45, soak=0, base=30),
         "normal": dict(core=8, game=120, soak=300, base=60),
         "thorough": dict(core=20, game=300, soak=900, base=120)}


class Smu:
    def __init__(self):
        self.fd = os.open(CONFIG, os.O_RDWR | os.O_CLOEXEC)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        if self.send(0x01, 123) != 124:  # test message: SMU answers arg + 1
            raise RuntimeError("SMU test message failed")

    def _write(self, reg, value):
        os.pwrite(self.fd, struct.pack("<I", reg), 0xB8)
        os.pwrite(self.fd, struct.pack("<I", value), 0xBC)

    def _read(self, reg):
        os.pwrite(self.fd, struct.pack("<I", reg), 0xB8)
        return struct.unpack("<I", os.pread(self.fd, 4, 0xBC))[0]

    def send(self, msg, arg=0):
        self._write(RSP, 0)
        self._write(ARG, arg)
        self._write(ARG + 4, 0)
        self._write(CMD, msg)
        for _ in range(100000):
            status = self._read(RSP)
            if status:
                break
        if status != 0x01:
            raise RuntimeError(f"SMU message 0x{msg:02X} returned status 0x{status:02X}")
        return self._read(ARG)

    def core_mhz(self, core):
        return self.send(0x43, core)

    def cpu_mv(self):
        return self.send(0x36)

    def set_profile(self, n):
        self.send(0x1E, n)

    def stock(self):
        """bc250_smu_oc's revert_defaults: 3.5 GHz max boost, stock VID curve and
        voltage margin, 100 C limits."""
        self.send(0x8F, 3500)  # max CPU boost clock
        self.send(0x50, 0)     # VID curve scale
        self.send(0x9A, 0)     # extra CPU/GPU voltage margin back on
        self.send(0x8B, 100)   # CPU max temperature
        self.send(0x8C, 100)   # GPU max temperature


def set_oc(smu, f, scale):
    """Max boost `f` MHz with the VID curve scaled by `scale`, extra voltage margin off,
    90 C limits (the SMU throttles there)."""
    smu.send(0x8B, 90)
    smu.send(0x8C, 90)
    smu.send(0x9A, 1)
    smu.send(0x50, scale & 0xFFFF)  # signed 16-bit
    smu.send(0x8F, f)


def tctl():
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if open(hw + "/name").read().strip() == "k10temp":
            return int(open(hw + "/temp1_input").read()) / 1000
    return float("nan")


def effective_mhz(cpus):
    """The kernel's measured clock of each CPU (by CPU number: offline CPUs are left out
    of /proc/cpuinfo, so its order isn't the CPU number)."""
    mhz = dict((int(p), float(m)) for p, m in
               re.findall(r"^processor\s*: (\d+).*?^cpu MHz\s*: ([\d.]+)", open("/proc/cpuinfo").read(), re.M | re.S))
    return [mhz.get(c, 0.0) for c in cpus]


def cpu_list(s):
    """'0-1,4-7' -> [0, 1, 4, 5, 6, 7]"""
    out = []
    for part in s.strip().split(","):
        if part:
            a, _, b = part.partition("-")
            out += range(int(a), int(b or a) + 1)
    return out


def fmt_cpus(cpus):
    """[0, 1, 4, 5, 6, 7] -> '0-1,4-7'"""
    runs = []
    for c in sorted(cpus):
        if runs and c == runs[-1][1] + 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def siblings(cpu):
    return cpu_list(open(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list").read())


def cores():
    """The first online CPU of each physical core."""
    online = cpu_list(open("/sys/devices/system/cpu/online").read())
    return sorted({min(siblings(c)) for c in online})


def layout():
    """The tuned thread layout, from the CPU topology. Core 0 stays free for the desktop
    and interrupts. The main thread gets the next core of the first L3 cache (CCX), the
    rest of that CCX takes the light job threads, and the second CCX the render threads.
    8 cores: main 2, rest 4-7, render 8-15. 6 cores: main 2, rest 4-5, render 6-11."""
    groups = sorted({open(p).read().strip() for p in
                     glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cache/index3/shared_cpu_list")},
                    key=lambda g: cpu_list(g)[0])
    first = cpu_list(groups[0])
    core0 = siblings(first[0])
    main = next(c for c in first if c not in core0)
    rest = [c for c in first if c not in core0 and c not in siblings(main)]
    return {"main": main, "sibling": next((c for c in siblings(main) if c != main), None),
            "core0": core0, "rest": rest, "gx": [c for g in groups[1:] for c in cpu_list(g)]}


def load(cpus):
    """Full load on `cpus` (no result checking): stress if installed, else Python spinners."""
    if shutil.which("stress"):
        return [subprocess.Popen(["taskset", "-c", fmt_cpus(cpus), "stress", "--cpu", str(len(cpus))],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    return [subprocess.Popen(["taskset", "-c", str(c), sys.executable, "-c", "while True: pass"])
            for c in cpus]


def unload(procs):
    for p in procs:
        p.terminate()
        p.wait()


def probe(smu):
    n = os.cpu_count()
    loads = ((f"{n} threads", list(range(n))), (f"{len(cores())} threads, 1 per core", cores()))
    print(f"SMU core MHz (cores 0-{len(cores()) - 1}) | effective MHz min-max | CPU mV | Tctl")
    for state, margin_off in (("undervolted (OC tool state)", 1), ("stock voltage", 0)):
        smu.send(0x9A, margin_off)
        for label, cpus in loads:
            procs = load(cpus)
            try:
                time.sleep(4)
                mhz = [smu.core_mhz(c) for c in range(len(cores()))]
                eff = effective_mhz(cpus)
                print(f"{state}, {label}:\n  {' '.join(map(str, mhz))} | {min(eff):.0f}-{max(eff):.0f}"
                      f" | {smu.cpu_mv()} mV | {tctl():.0f} C")
            finally:
                unload(procs)
            time.sleep(2)
    print("single thread on CPU 2:")
    procs = load([2])
    try:
        time.sleep(3)
        print(f"  core 1: {smu.core_mhz(1)} MHz (SMU), {effective_mhz([2])[0]:.0f} MHz effective, {smu.cpu_mv()} mV")
    finally:
        unload(procs)
    smu.stock()
    print("Back to stock: 3.5 GHz max, stock voltage, 100 C limits.")


@contextmanager
def governor_paused():
    active = subprocess.run(["systemctl", "is-active", "--quiet", GOVERNOR]).returncode == 0
    if active:
        subprocess.run(["systemctl", "stop", GOVERNOR], check=True)
    try:
        yield
    finally:
        if active:
            subprocess.run(["systemctl", "start", GOVERNOR], check=True)


def membench_quick():
    out = subprocess.run([MEMBENCH, "quick"], capture_output=True, text=True, check=True).stdout
    return {k: float(v) for k, v in re.findall(r"(\w+)=([\d.]+)", out)}


def profile_test(smu):
    rows = []
    for profile in (None, 3, 2, 1):
        if profile is not None:
            smu.set_profile(profile)
        rows.append(("boot value" if profile is None else str(profile), membench_quick()))
    # Profile 3 (the documented performance default) unless another is clearly faster.
    best = min(rows[1:], key=lambda r: r[1]["lat_2m_1g"])
    if best[1]["lat_2m_1g"] > 0.98 * rows[1][1]["lat_2m_1g"]:
        best = rows[1]
    smu.set_profile(int(best[0]))
    print(f"{'profile':<11} {'RAM latency':>12} {'4K pages':>9} {'256 MB':>8} {'bandwidth':>10} {'cross-CCX':>10}")
    for name, m in rows:
        print(f"{name:<11} {m['lat_2m_1g']:9.1f} ns {m['lat_4k_1g']:6.1f} ns {m['lat_2m_256m']:5.1f} ns"
              f" {m['bw_all']:5.1f} GB/s {m['xccx']:7.1f} ns")
    print(f"Left at profile {best[0]} (lowest latency) until reboot.")


def vid_predict(clock, scale):
    """bc250_smu_oc's fitted V/F model: CPU mV at `clock` MHz (>= 3 GHz) for a VID curve scale."""
    p = -1.519 + scale * 0.004325
    q = 2800.0 - scale * 10.0
    return 0.0003 * clock * clock + p * clock + q


def oc_detect(smu, target, vmax, cpus=(2, 6, 10, 14), secs=30):
    """Like bc250-detect: 100 MHz steps from 3.5 GHz, VID curve scaled down so the
    measured voltage stays <= vmax, extra voltage margin off, 90 C limits. But the
    load is game-like (4 cores across both CCXs, not all 16 threads, which hits
    90 C in seconds on this cooler), each step runs `secs` seconds, and throttling
    is judged by the loaded CPUs' effective clocks. Leaves the best step applied
    and writes overclock.conf in bc250_smu_oc's format.
    (Research history: on the original box this stopped at 3500 MHz because 4 full
    `stress` cores reach 90 C, while the game ran at 3.8 GHz at 68-73 C. qualify replaces it.)"""
    smu.send(0x8B, 90)  # CPU max temperature
    smu.send(0x8C, 90)  # GPU max temperature
    smu.send(0x9A, 1)   # extra voltage margin off
    f, scale, safe = 3500, 0, None
    while f <= target:
        smu.send(0x50, scale & 0xFFFF)  # signed 16-bit
        smu.send(0x8F, f)               # max CPU boost clock
        procs = load(list(cpus))
        try:
            time.sleep(0.5)
            v = smu.cpu_mv()
            if v > vmax:
                scale -= max(int((v - vmax) / 6), 1)
                if scale < -50:
                    print(f"{f} MHz needs more than {vmax} mV even at the lowest curve: stopping")
                    break
                continue
            print(f"{f} MHz (scale {scale}): ", end="", flush=True)
            low, peak_v, peak_t = float("inf"), v, tctl()
            for _ in range(secs):
                time.sleep(1)
                low = min(low, min(effective_mhz(cpus)))
                peak_v, peak_t = max(peak_v, smu.cpu_mv()), max(peak_t, tctl())
                if peak_t >= 92:
                    break
            print(f"lowest {low:.0f} MHz, peak {peak_v} mV, peak {peak_t:.0f} C")
        finally:
            unload(procs)
        if low < f - 50 or peak_v > vmax or peak_t >= 92:
            print("  throttled, over the voltage cap or too hot: stopping")
            break
        safe = (f, scale, peak_v, peak_t)
        f += 100
        # Lower the curve ahead of the next step (bc250_smu_oc's 0.75-weighted prediction).
        nxt = scale
        while nxt > -50 and peak_v + 0.75 * (vid_predict(f, nxt) - vid_predict(f - 100, scale)) > vmax:
            nxt -= 1
        scale = nxt
        time.sleep(3)
    if safe is None:
        smu.stock()
        print("No step passed; back to stock.")
        return
    f, scale, v, t = safe
    set_oc(smu, f, scale)
    conf = os.path.join(os.getcwd(), "overclock.conf")
    with open(conf, "w") as fh:
        fh.write(f"[overclock]\nfrequency = {f}\nscale = {scale}\nmax_temperature = 90\n")
    if "SUDO_UID" in os.environ:
        os.chown(conf, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    print(f"Applied {f} MHz (scale {scale}, {v} mV, {t:.0f} C under 4-core load) until reboot;"
          f" written to {conf}.")


def apply_oc(smu, f, scale, vmax=VMAX):
    """Max boost `f` MHz with the VID curve scaled by `scale`, extra margin off, 90 C
    limits; the SMU still throttles on temperature. Checks one loaded core (CPU 2,
    the game's main thread) reaches the clock under `vmax`, else reverts to stock."""
    set_oc(smu, f, scale)
    procs = load([2])
    try:
        peak_v, low = 0, float("inf")
        for _ in range(5):
            time.sleep(1)
            peak_v, low = max(peak_v, smu.cpu_mv()), min(low, effective_mhz([2])[0])
    finally:
        unload(procs)
    print(f"one core loaded: lowest {low:.0f} MHz, peak {peak_v} mV, {tctl():.0f} C")
    if peak_v > vmax or low < f - 50:
        smu.stock()
        sys.exit(f"over {vmax} mV or clock not reached: back to stock")
    print(f"Applied {f} MHz max boost, curve scale {scale}, until reboot (stock: sudo er-smu stock).")


# --- qualify: the OC safety test ------------------------------------------------------

def read_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def write_state(state):
    """Written and synced before each risky step, so the record of what was being tested
    survives a hard freeze (qualify finds it on the next run and counts it as a crash)."""
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE)
    dfd = os.open(os.path.dirname(STATE), os.O_RDONLY)
    os.fsync(dfd)
    os.close(dfd)


def kernel_errors(since):
    """Machine-check / hardware-error lines the kernel logged since `since` (epoch s)."""
    out = subprocess.run(["journalctl", "-k", "-q", "-o", "cat", "--since", f"@{int(since)}"],
                         capture_output=True, text=True).stdout
    # Not the boot banners ("MCE: In-kernel MCE decoding enabled.", "EDAC MC: Ver: 3.0.0").
    return [l for l in out.splitlines()
            if re.search(r"hardware error|machine check (event|exception)|events logged|\bedac\b.*\b(ce|ue|error)", l, re.I)]


def burn(exe, cpus, secs, pct=100):
    return subprocess.Popen([exe, fmt_cpus(cpus), str(secs), str(pct)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def game_burns(exe, lay, secs):
    """Shaped like Elden Ring's load in the tuned layout: the main thread's core flat out,
    the render CCX at 25% per CPU, the light job threads at 20%."""
    return [burn(exe, cpus, secs, pct) for cpus, pct in
            (([lay["main"]], 100), (lay["gx"], 25), (lay["rest"], 20)) if cpus]


def run_phase(smu, burns, secs, probe_cpu, emit, name):
    """Waits for er-burn processes while sampling once a second: the probe CPU's clock,
    the CPU voltage, Tctl. Stops them early at the voltage cap or T_ABORT. Returns
    (reason it stopped early or None, stats)."""
    emit("phase", name=name, secs=secs)
    t0, reason, samples = time.monotonic(), None, []
    try:
        while any(b.poll() is None for b in burns) and time.monotonic() - t0 < secs + 15:
            time.sleep(1)
            t = round(time.monotonic() - t0)
            mhz, mv, temp = effective_mhz([probe_cpu])[0], smu.cpu_mv(), tctl()
            samples.append((t, mhz, mv, temp))
            emit("sample", phase=name, t=min(t, secs), secs=secs, mhz=round(mhz), mv=mv, tctl=round(temp))
            if mv > VMAX:
                reason = f"CPU voltage {mv} mV, over the {VMAX} mV cap"
            elif temp >= T_ABORT:
                reason = f"{temp:.0f} C, the SMU's throttle point"
            if reason:
                break
    finally:
        for b in burns:
            if b.poll() is None:
                b.terminate()
        outs = [b.communicate()[0] for b in burns]
    text = "".join(outs)
    errors = max(sum(int(n) for n in re.findall(r"blocks, (\d+) errors", text)), text.count("wrong result"))
    bad = sorted({int(c) for c in re.findall(r"CPU (\d+): (?:wrong result|\d+ errors)", text)})
    if not reason and any(b.returncode == 1 for b in burns):
        reason = "er-burn couldn't run: " + " ".join(l for l in text.splitlines() if "can't" in l)
    steady = [s for s in samples if s[0] > 2] or samples
    return reason, {"errors": errors, "bad_cpus": bad,
                    "low": round(min((s[1] for s in steady), default=0)),
                    "peak_mv": max((s[2] for s in samples), default=0),
                    "peak_t": round(max((s[3] for s in samples), default=0))}


def oc_step(smu, exe, lay, f, scale, pace, emit):
    """One ladder step: each core alone at full load (it must hold the clock, with no
    wrong results), then the game-like mix. Returns (status, reason, stats); status is
    "pass", "errors" (wrong results: worth a retry with more voltage) or "fail"."""
    set_oc(smu, f, scale)
    since = time.time()
    stats = {"mhz": f, "scale": scale, "model_mv": round(vid_predict(f, scale)), "peak_mv": 0, "peak_t": 0}

    def merge(r):
        stats["peak_mv"], stats["peak_t"] = max(stats["peak_mv"], r["peak_mv"]), max(stats["peak_t"], r["peak_t"])

    for cpu in cores():
        reason, r = run_phase(smu, [burn(exe, [cpu], pace["core"])], pace["core"], cpu, emit, f"{f} MHz: CPU {cpu} alone")
        merge(r)
        if r["errors"]:
            return "errors", f"wrong results on CPU {cpu}", stats
        if reason:
            return "fail", reason, stats
        if r["low"] < f - 50:
            return "fail", f"CPU {cpu} held only {r['low']} MHz", stats
    reason, r = run_phase(smu, game_burns(exe, lay, pace["game"]), pace["game"], lay["main"], emit,
                          f"{f} MHz: game-like load")
    merge(r)
    stats["game_t"], stats["main_low"] = r["peak_t"], r["low"]
    if r["errors"]:
        return "errors", f"wrong results on CPU {fmt_cpus(r['bad_cpus'])}", stats
    if reason:
        return "fail", reason, stats
    if r["peak_t"] >= T_FAIL:
        return "fail", f"{r['peak_t']} C under the game-like load (limit {T_FAIL} C)", stats
    errs = kernel_errors(since)
    if errs:
        return "fail", "the kernel logged hardware errors: " + errs[0], stats
    return "pass", None, stats


def qualify(smu, exe, max_mhz, pace_name, emit):
    """The OC safety test. Every BC-250 is different, so each step of LADDER (up to
    max_mhz) must pass on this machine:
      - each core alone at full load holds the clock, with the voltage under VMAX and no
        wrong results (er-burn checks every result)
      - a game-like load: no wrong results, no machine-check errors in the kernel log,
        Tctl below T_FAIL
    It stops at the first failure. If a step fails only with wrong results, it's retried
    once with ~30 mV more (still under VMAX). Before that comes a baseline at stock clocks:
    if the cooling can't hold the game-like load below T_BASE at stock, there's no OC. The
    pick is the highest step that passed (one lower if its margin to T_FAIL was thin),
    then a soak (a longer game-like run) at that step. Ends at stock clocks either way.
    Each step is recorded on disk before it starts: if the machine freezes or reboots,
    the next run counts that step as crashed and tests only the steps below it."""
    pace = PACES[pace_name]
    lay = layout()
    old = read_state()
    crashed = set(old.get("crashed", []))
    if old.get("testing"):
        crashed.add(old["testing"]["mhz"])
        emit("crash_found", **old["testing"])
    ladder = [(f, s) for f, s in LADDER if f <= max_mhz and f < min(crashed, default=99999)]
    state = {"version": 1, "time": int(time.time()), "pace": pace_name, "crashed": sorted(crashed),
             "layout": {k: v for k, v in lay.items() if k != "core0"},
             "baseline": None, "steps": [], "testing": None, "recommended": None, "result": None}
    write_state(state)
    emit("plan", steps=[f for f, _ in ladder], crashed=sorted(crashed), pace=pace_name,
         cores=len(cores()), main=lay["main"], gx=fmt_cpus(lay["gx"]), rest=fmt_cpus(lay["rest"]))

    def mark(f=None, scale=None):
        state["testing"] = None if f is None else {"mhz": f, "scale": scale}
        write_state(state)

    def finish(result, rec=None):
        state["result"], state["recommended"] = result, rec
        write_state(state)
        emit("done", result=result, recommended=rec)

    try:
        smu.stock()
        mark(3500, 0)
        since = time.time()
        reason, base = run_phase(smu, game_burns(exe, lay, pace["base"]), pace["base"], lay["main"], emit,
                                 "stock clocks: baseline")
        state["baseline"] = base
        if base["errors"]:
            return finish(f"wrong results at stock clocks (CPU {fmt_cpus(base['bad_cpus'])}): this machine "
                          "isn't stable even without an OC")
        if reason or base["peak_t"] >= T_BASE:
            return finish(f"{reason or str(base['peak_t']) + ' C'} at stock clocks under the game-like load: "
                          f"the cooling is too weak for an OC (limit {T_BASE} C)")
        errs = kernel_errors(since)
        if errs:
            return finish("the kernel logged hardware errors at stock clocks: " + errs[0])
        if not ladder:
            return finish("no OC step left to test" + (f" (crashed at {min(crashed)} MHz before)" if crashed else ""))
        passed = []
        for f, scale in ladder:
            for s in (scale, scale + 5):
                if s != scale and (s > 0 or vid_predict(f, s) > VMAX - 10):
                    break
                if s != scale:
                    emit("info", msg=f"{f} MHz: retrying once with ~30 mV more (scale {s})")
                mark(f, s)
                status, reason, stats = oc_step(smu, exe, lay, f, s, pace, emit)
                state["steps"].append({**stats, "ok": status == "pass", "reason": reason})
                emit("step", ok=status == "pass", reason=reason, **stats)
                if status != "errors":
                    break
            if status != "pass":
                break
            passed.append(stats)
        smu.stock()
        mark()
        if not passed:
            return finish(f"no OC step passed ({state['steps'][-1]['reason']})")
        picks = passed[::-1]
        if picks[0]["game_t"] >= T_WARN and len(picks) > 1:
            emit("info", msg=f"{picks[0]['mhz']} MHz passed, but at {picks[0]['game_t']} C its thermal margin is "
                             f"thin: picking {picks[1]['mhz']} MHz")
            picks = picks[1:]
        for pick in picks:
            if pace["soak"]:
                mark(pick["mhz"], pick["scale"])
                set_oc(smu, pick["mhz"], pick["scale"])
                since = time.time()
                reason, r = run_phase(smu, game_burns(exe, lay, pace["soak"]), pace["soak"], lay["main"], emit,
                                      f"{pick['mhz']} MHz: soak")
                smu.stock()
                mark()
                errs = kernel_errors(since)
                ok = not (reason or r["errors"] or r["peak_t"] >= T_FAIL or errs)
                emit("soak", mhz=pick["mhz"], ok=ok, peak_t=r["peak_t"], errors=r["errors"],
                     reason=reason or (errs[0] if errs else None))
                if not ok:
                    continue
            rec = {k: pick[k] for k in ("mhz", "scale", "model_mv", "peak_mv", "peak_t", "game_t")}
            return finish(f"{pick['mhz']} MHz passed", rec)
        finish("the soak failed at every step that passed")
    finally:
        smu.stock()
        if state["testing"]:
            mark()


def emitter(as_json):
    def emit(ev, **kw):
        if as_json:
            print(json.dumps({"ev": ev, **kw}), flush=True)
        elif ev == "sample":
            if kw["t"] % 10 == 0:
                print(f"    {kw['t']:4}/{kw['secs']} s  {kw['mhz']} MHz  {kw['mv']} mV  {kw['tctl']} C", flush=True)
        elif ev == "phase":
            print(f"== {kw['name']} ({kw['secs']} s)", flush=True)
        elif ev == "step":
            print(f"-> {kw['mhz']} MHz (scale {kw['scale']}): " + ("PASS" if kw["ok"] else f"FAIL: {kw['reason']}")
                  + f"  peak {kw['peak_mv']} mV, {kw['peak_t']} C", flush=True)
        elif ev == "done":
            rec = kw["recommended"]
            print(f"Result: {kw['result']}" + (f"\nRecommended: {rec['mhz']} MHz, scale {rec['scale']}" if rec else ""))
        else:
            print(f"{ev}: " + ", ".join(f"{k}={v}" for k, v in kw.items()), flush=True)
    return emit


def main():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    model = re.search(r"^model name\s*: (.*)$", open("/proc/cpuinfo").read(), re.M)
    if not model or "BC-250" not in model[1]:
        sys.exit(f"This is for the AMD BC-250 only (this CPU: {model[1] if model else 'unknown'}). Nothing was changed.")
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "qualify":
        opts = {"--max": "3800", "--pace": "normal", "--burn": os.path.join(HERE, "er-burn")}
        rest = args[1:]
        while rest:
            if rest[0] == "--json":
                opts["--json"] = True
                rest = rest[1:]
            elif rest[0] in opts and len(rest) > 1:
                opts[rest[0]], rest = rest[1], rest[2:]
            else:
                sys.exit(__doc__)
        if not (opts["--max"].isdigit() and 3600 <= int(opts["--max"]) <= 3900) or opts["--pace"] not in PACES:
            sys.exit("--max must be 3600-3900 and --pace quick, normal or thorough")
        if not os.access(opts["--burn"], os.X_OK):
            sys.exit(f"{opts['--burn']} not found: build it with gcc -O3 -march=native -pthread -o er-burn er-burn.c")
        if subprocess.run(["pgrep", "-x", "eldenring.exe"], capture_output=True).returncode == 0:
            sys.exit("Quit Elden Ring first.")
        if subprocess.run(["systemctl", "is-active", "--quiet", "er-oc.service"]).returncode == 0:
            sys.exit("er-oc.service is active (the game-time OC): quit the game first.")
    with governor_paused():
        smu = Smu()
        if cmd == "status":
            print("core MHz:", " ".join(str(smu.core_mhz(c)) for c in range(len(cores()))), f" CPU {smu.cpu_mv()} mV")
        elif cmd == "qualify":
            try:
                qualify(smu, opts["--burn"], int(opts["--max"]), opts["--pace"], emitter(opts.get("--json")))
            except KeyboardInterrupt:
                emitter(opts.get("--json"))("aborted", msg="stopped by the user; back to stock clocks")
                sys.exit(130)
        elif cmd == "profile" and len(args) == 2 and args[1] in ("0", "1", "2", "3"):
            smu.set_profile(int(args[1]))
            print(f"perf profile set to {args[1]}")
        elif cmd == "profile-test":
            profile_test(smu)
        elif cmd == "probe":
            probe(smu)
        elif cmd == "stock":
            smu.stock()
            print("Back to stock: 3.5 GHz max, stock voltage, 100 C limits.")
        elif cmd == "oc":
            target = int(args[1]) if len(args) > 1 else 3900
            vmax = int(args[2]) if len(args) > 2 else 1250
            if not (3500 <= target <= 4200 and 1000 <= vmax <= 1300):
                sys.exit("target must be 3500-4200 MHz and the voltage cap 1000-1300 mV")
            oc_detect(smu, target, vmax)
        elif cmd == "apply" and len(args) == 3:
            f, scale = int(args[1]), int(args[2])
            if not (3500 <= f <= 4000 and -50 <= scale <= 0) or vid_predict(f, scale) > VMAX:
                sys.exit(f"need 3500-4000 MHz, scale -50..0, and a predicted voltage <= {VMAX} mV")
            apply_oc(smu, f, scale)
        else:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
