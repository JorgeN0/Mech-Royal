"""Fit each house persona's aim noise (sigma, radians) to its real gun accuracy in lobby recordings.

Model (what sim/housebot.py does): perfect velocity lead + Gaussian angular noise sigma (pitch 0.6 sigma).
For every real gun shot we know the shooter, the target it aimed at and the target's real future path, so the
lead error is replayed exactly and only sigma is fitted. Also prints fire/hit distance medians and fight distance.

usage: python tools/fit_house.py LOBBY.jsonl [...]
"""
import math
import random
import sys
from collections import defaultdict

sys.path.insert(0, __import__("os").path.dirname(__file__))
from lead_test import interp  # noqa: E402
from replay_stats import load  # noqa: E402

BINS = [(0, 40), (40, 80), (80, 130), (130, 230)]
SPEED, LAT, RADIUS, SPREAD = 252.0, 0.1, 2.5, math.radians(0.6)
SIGMAS = [0.0, 0.004, 0.008, 0.012, 0.016, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05, 0.06, 0.08]


def binof(d):
    for i, (a, b) in enumerate(BINS):
        if a <= d < b:
            return i
    return None


def main(paths):
    rng = random.Random(1)
    shots = defaultdict(list)          # persona -> [(bin, lead_err_x, lead_err_y, d)]
    hits = defaultdict(lambda: [0] * len(BINS))
    fires = defaultdict(lambda: [0] * len(BINS))
    fdist = defaultdict(list)
    for path in paths:
        hdr, frames = load(path)
        srcs = hdr.get("bots") or []
        persona = {}
        for k, m in enumerate(hdr["mechs"]):
            s = srcs[k] if k < len(srcs) else ""
            persona[m["id"]] = m["name"].rstrip("0123456789 ").strip() if s.startswith("house") else "human"
        tracks = defaultdict(list)
        for fr in frames:
            tt = fr["t"] / 20.0
            for m in fr["m"]:
                tracks[m[0]].append((tt, m[1], m[2]))
        pos, aim, vel, lastp = {}, {}, {}, {}
        for fr in frames:
            t = fr["t"] / 20.0
            for m in fr["m"]:
                i = m[0]
                if i in lastp and t - lastp[i][0] > 0:
                    dt = t - lastp[i][0]
                    vel[i] = ((m[1] - lastp[i][1]) / dt, (m[2] - lastp[i][2]) / dt)
                lastp[i] = (t, m[1], m[2])
                pos[i] = (m[1], m[2])
                aim[i] = m[4]
            for e in fr.get("ev", ()):
                k = e.get("e")
                if k == "fire" and e.get("w") == "gun" and persona.get(e.get("id"), "human") != "human":
                    s = e["id"]
                    if s not in pos:
                        continue
                    sx, sy = pos[s]
                    best, ba = None, math.radians(12)
                    for j, p in pos.items():
                        if j == s:
                            continue
                        b = math.atan2(p[1] - sy, p[0] - sx)
                        da = abs((b - aim.get(s, 0) + math.pi) % (2 * math.pi) - math.pi)
                        if da < ba:
                            best, ba = j, da
                    if best is None:
                        continue
                    d = math.hypot(pos[best][0] - sx, pos[best][1] - sy)
                    bi = binof(d)
                    if bi is None:
                        continue
                    p = persona[s]
                    fires[p][bi] += 1
                    fdist[p].append(d)
                    v = vel.get(best, (0.0, 0.0))
                    h = LAT + d / SPEED
                    act = interp(tracks[best], t + h)
                    if act is None:
                        continue
                    ex = pos[best][0] + v[0] * h - act[0]
                    ey = pos[best][1] + v[1] * h - act[1]
                    shots[p].append((bi, ex, ey, d))
                elif k == "hit" and e.get("w") == "gun" and persona.get(e.get("src"), "human") != "human":
                    s, dd = e["src"], e.get("dst")
                    if s in pos and dd in pos:
                        bi = binof(math.hypot(pos[dd][0] - pos[s][0], pos[dd][1] - pos[s][1]))
                        if bi is not None:
                            hits[persona[s]][bi] += 1
    print("persona   measured acc by bin " + " ".join("%7s" % ("%d-%d" % b) for b in BINS) + "   best sigma  model acc at best")
    out = {}
    for p in sorted(shots):
        meas = [hits[p][i] / fires[p][i] if fires[p][i] >= 15 else None for i in range(len(BINS))]
        best, be, bmod = None, 1e9, None
        for sg in SIGMAS:
            mod = [0.0] * len(BINS)
            n = [0] * len(BINS)
            for (bi, ex, ey, d) in shots[p]:
                tol = RADIUS - 0.5 * d * math.tan(SPREAD)
                hit = 0
                for _ in range(8):
                    nx, ny = rng.gauss(0, sg * d), rng.gauss(0, sg * d)
                    hit += math.hypot(ex + nx, ey + ny) < tol
                mod[bi] += hit / 8.0
                n[bi] += 1
            mod = [mod[i] / n[i] if n[i] else 0 for i in range(len(BINS))]
            err = sum((mod[i] - meas[i]) ** 2 * fires[p][i] for i in range(len(BINS)) if meas[i] is not None and i in (1, 2))
            if err < be:
                best, be, bmod = sg, err, mod
        out[p] = best
        fd = sorted(fdist[p])
        print("%-9s %s   %.3f      %s   fire dist median %.0f m" % (
            p, " ".join("%6.0f%%" % (100 * m) if m is not None else "     - " for m in meas), best,
            " ".join("%3.0f%%" % (100 * m) for m in bmod), fd[len(fd) // 2] if fd else 0))
    print("\nLEVELS sigma:", {k: round(v, 3) for k, v in out.items()})


if __name__ == "__main__":
    main(sys.argv[1:])
