"""How well does lead aiming predict real house-bot movement? Offline, from mechreplay/1 lobby recordings.

For every house-bot frame we predict where it will be after latency + bullet flight (d / 252 m/s) with several
predictors and compare with where it actually was. A shot 'hits' if the horizontal miss is under the mech radius
minus the spread allowance. Prints hit chance by distance and predictor, per persona.

usage: python tools/lead_test.py FILE.jsonl [...]
"""
import math
import sys
from collections import defaultdict

sys.path.insert(0, __import__("os").path.dirname(__file__))
from replay_stats import load  # noqa: E402

DISTS = [40, 60, 80, 100, 120, 150, 200]
SPEED = 252.0
LAT = 0.1          # decision + hold latency before the bullet leaves
RADIUS = 2.5
SPREAD = math.radians(0.6)


def interp(track, t):
    """track: sorted list of (t, x, y); linear interpolation, None outside."""
    lo, hi = 0, len(track) - 1
    if t < track[0][0] or t > track[-1][0]:
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if track[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    t0, x0, y0 = track[lo]
    t1, x1, y1 = track[hi]
    if t1 - t0 > 0.5:
        return None
    a = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    return x0 + a * (x1 - x0), y0 + a * (y1 - y0)


def main(paths):
    res = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # (persona, predictor) -> dist -> [hit, n]
    turn = defaultdict(list)
    for path in paths:
        hdr, frames = load(path)
        srcs = hdr.get("bots") or []
        persona = {}
        for k, m in enumerate(hdr["mechs"]):
            s = srcs[k] if k < len(srcs) else ""
            if s.startswith("house"):
                persona[m["id"]] = m["name"].rstrip("0123456789 ").strip()
        tracks = defaultdict(list)
        for fr in frames:
            t = fr["t"] / 20.0
            for m in fr["m"]:
                if m[0] in persona:
                    tracks[m[0]].append((t, m[1], m[2]))
        for i, tr in tracks.items():
            p = persona[i]
            # how long do they keep a heading? (seconds between >45 deg velocity turns)
            last_h, last_t = None, None
            for k in range(3, len(tr) - 1, 2):
                vx, vy = tr[k][1] - tr[k - 1][1], tr[k][2] - tr[k - 1][2]
                if math.hypot(vx, vy) < 0.3:
                    continue
                h = math.atan2(vy, vx)
                if last_h is not None and abs((h - last_h + math.pi) % (2 * math.pi) - math.pi) > math.radians(45):
                    turn[p].append(tr[k][0] - last_t)
                    last_h, last_t = h, tr[k][0]
                elif last_h is None:
                    last_h, last_t = h, tr[k][0]
            for k in range(4, len(tr) - 2, 3):
                t, x, y = tr[k]
                tp, xp, yp = tr[k - 1]
                tq, xq, yq = tr[k - 3]
                if t - tq > 0.45 or t - tp > 0.15:
                    continue
                v1 = ((x - xp) / (t - tp), (y - yp) / (t - tp))
                v3 = ((x - xq) / (t - tq), (y - yq) / (t - tq))
                for d in DISTS:
                    h = LAT + d / SPEED
                    act = interp(tr, t + h)
                    if act is None:
                        continue
                    tol = RADIUS - 0.5 * d * math.tan(SPREAD)
                    preds = {"no lead": (x, y), "lead v(0.1s)": (x + v1[0] * h, y + v1[1] * h),
                             "lead v(0.3s)": (x + v3[0] * h, y + v3[1] * h),
                             "half lead": (x + 0.5 * v3[0] * h, y + 0.5 * v3[1] * h)}
                    for name, (px, py) in preds.items():
                        e = math.hypot(px - act[0], py - act[1])
                        r = res[(p, name)][d]
                        r[0] += e < tol
                        r[1] += 1
                        r = res[("ALL", name)][d]
                        r[0] += e < tol
                        r[1] += 1
    print("hit chance of a perfectly aimed shot vs real house-bot movement (horizontal miss < radius - spread)")
    print("persona   predictor      " + "".join("%6dm" % d for d in DISTS))
    for (p, name) in sorted(res):
        row = res[(p, name)]
        print("%-9s %-14s " % (p, name) + "".join("%6.0f%%" % (100 * row[d][0] / max(1, row[d][1])) for d in DISTS))
    print("\nmedian seconds between >45 deg heading changes:")
    for p, L in sorted(turn.items()):
        L = sorted(L)
        print("  %-9s %.2f s (n=%d)" % (p, L[len(L) // 2] if L else 0, len(L)))


if __name__ == "__main__":
    main(sys.argv[1:])
