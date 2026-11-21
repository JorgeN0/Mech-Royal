"""Does the sim rank our versions like the real practice board? Runs every version with a known real score.

usage: python -m sim.calibrate_real [--n 48] [--squad real] [--set sched.per_kill=0.25 ...]
Prints sim vs real per version, then RMSE, mean offset and correlation.
"""
import argparse
import re
import subprocess
import sys

V8 = "bot.py:strafe_min=0.9,strafe_max=2.2,weave=0"
V7ISH = "bot.py:scoot=0,hidden_reload=0,anti_flank=0,strafe_min=0.9,strafe_max=2.2,weave=0"
REAL = [  # (spec, real practice score, label)
    ("sim/bots/example_bot.py", 1.15, "kit example"),
    ("sim/bots/v1_bot.py", 3.38, "v1"),
    ("sim/bots/v7_bot.py", 4.13, "v7"),
    ("sim/bots/v7_bot.py:pick_fights=1,engage_r=200", 3.88, "exp/aggro"),
    ("sim/bots/v7_bot.py:anchor=1,anchor_laser=0", 4.04, "exp/roof"),
    (V7ISH + ",lightning_first=1", 3.85, "exp/lightning"),
    (V8, 4.52, "v8 (cover)"),
    (V8 + ",sentinel=1,pref_gun=80", 4.10, "exp/sentinel"),
    (V8 + ",heal_hp=0.6", 4.54, "exp/heal"),
    (V8 + ",cover_tether=14", 4.25, "exp/tether"),
    ("bot.py", 4.65, "exp/jink (v9)"),
    ("bot.py:pref_gun=120", 4.35, "exp/jink-range"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--seed", type=int, default=60000)
    ap.add_argument("--squad", default="real")
    ap.add_argument("--set", nargs="*", default=[])
    a = ap.parse_args()
    cmd = [sys.executable, "-m", "sim.run", "--mode", "practice", "--n", str(a.n), "--seed", str(a.seed),
           "--squad", a.squad, "--bots"] + [r[0] for r in REAL]
    if a.set:
        cmd += ["--set"] + a.set
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    sim = {}
    for line in out.splitlines():
        m = re.match(r"^practice (\S+)\s+score ([\d.]+)", line)
        if m:
            sim[m.group(1)] = float(m.group(2))
    xs, ys = [], []
    print("%-16s %6s %6s" % ("version", "real", "sim"))
    for spec, real, label in REAL:
        s = sim.get(spec)
        if s is None:
            continue
        xs.append(real)
        ys.append(s)
        print("%-16s %6.2f %6.2f" % (label, real, s))
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    rmse = (sum((x - y) ** 2 for x, y in zip(xs, ys)) / n) ** 0.5
    # the part that matters for choosing tactics: versions scoring 3.8+ in reality
    top = [(x, y) for x, y in zip(xs, ys) if x >= 3.8]
    tx, ty = [p[0] for p in top], [p[1] for p in top]
    tmx, tmy = sum(tx) / len(tx), sum(ty) / len(ty)
    tc = sum((x - tmx) * (y - tmy) for x, y in top)
    tv = (sum((x - tmx) ** 2 for x in tx) * sum((y - tmy) ** 2 for y in ty)) ** 0.5
    print("squad %s set %s: RMSE %.2f, offset %+.2f, corr all %.2f, corr among 3.8+ versions %.2f" % (
        a.squad, a.set, rmse, my - mx, cov / (vx * vy) ** 0.5, tc / tv if tv else 0))


if __name__ == "__main__":
    main()
