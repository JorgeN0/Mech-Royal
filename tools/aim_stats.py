"""Gun accuracy by shooter group and distance, from mechreplay/1 files.

usage: python tools/aim_stats.py FILE.jsonl [...]
Each gun 'fire' is assigned to the enemy closest to the shooter's aim ray (within 12 deg); each gun 'hit' on a mech
is binned by the shooter-target distance. Accuracy per bin = hits / fires. Groups: house persona (name without the
trailing number), human, team bot. Also prints movement (speed, share still) and what killed whom.
"""
import json
import math
import sys
from collections import defaultdict

sys.path.insert(0, __import__("os").path.dirname(__file__))
from replay_stats import load  # noqa: E402

BINS = [(0, 40), (40, 80), (80, 130), (130, 400)]


def group_of(name, src):
    if src.startswith("human"):
        return "human"
    if src.startswith("house"):
        return name.rstrip("0123456789 ").strip()
    return "teambot"


def binof(d):
    for i, (a, b) in enumerate(BINS):
        if a <= d < b:
            return i
    return len(BINS) - 1


def main(paths):
    fires = defaultdict(lambda: [0] * len(BINS))
    hits = defaultdict(lambda: [0] * len(BINS))
    lhit = defaultdict(lambda: [0, 0])
    move = defaultdict(lambda: [0.0, 0.0, 0.0])  # time, still time, distance
    tgt_speed = defaultdict(list)  # speed of targets when hit by group
    kills = defaultdict(lambda: defaultdict(int))
    dmg_to = defaultdict(lambda: defaultdict(float))
    for path in paths:
        hdr, frames = load(path)
        if not hdr:
            continue
        srcs = hdr.get("bots") or []
        grp = {}
        for k, m in enumerate(hdr["mechs"]):
            grp[m["id"]] = group_of(m["name"], srcs[k] if k < len(srcs) else "")
        pos, aim, vel, last = {}, {}, {}, {}
        for fr in frames:
            t = fr["t"] / 20.0
            for m in fr["m"]:
                i = m[0]
                if i in pos and i in last:
                    dt = t - last[i]
                    if 0 < dt < 1:
                        sp = math.hypot(m[1] - pos[i][0], m[2] - pos[i][1]) / dt
                        vel[i] = sp
                        g = grp.get(i, "?")
                        move[g][0] += dt
                        move[g][1] += dt if sp < 1.0 else 0
                        move[g][2] += sp * dt
                pos[i] = (m[1], m[2], m[3])
                aim[i] = m[4]
                last[i] = t
            for e in fr.get("ev", ()):
                k = e.get("e")
                if k == "fire" and e.get("w") == "gun" and e.get("id") in pos:
                    s = e["id"]
                    sx, sy, _ = pos[s]
                    best, ba = None, math.radians(12)
                    for j, p in pos.items():
                        if j == s or j not in grp:
                            continue
                        b = math.atan2(p[1] - sy, p[0] - sx)
                        da = abs((b - aim.get(s, 0) + math.pi) % (2 * math.pi) - math.pi)
                        if da < ba:
                            best, ba = j, da
                    if best is not None:
                        d = math.hypot(pos[best][0] - sx, pos[best][1] - sy)
                        fires[grp[s]][binof(d)] += 1
                elif k == "hit" and e.get("w") == "gun" and e.get("src") in grp and e.get("dst") in grp:
                    s, d_ = e["src"], e["dst"]
                    if s in pos and d_ in pos:
                        d = math.hypot(pos[d_][0] - pos[s][0], pos[d_][1] - pos[s][1])
                        hits[grp[s]][binof(d)] += 1
                        tgt_speed[grp[s]].append(vel.get(d_, 0.0))
                if k == "hit" and e.get("src") in grp and e.get("dst") in grp:
                    dmg_to[grp[e["src"]]][grp[e["dst"]]] += float(e.get("amt", 0))
                elif k == "laser" and e.get("id") in grp:
                    lhit[grp[e["id"]]][0] += 1
                    if any(h in grp for h in e.get("hits", ())):
                        lhit[grp[e["id"]]][1] += 1
                elif k == "death" and e.get("id") in grp:
                    kills[grp.get(e.get("by"), "env")][grp[e["id"]]] += 1
    print("group        gun acc by distance:  " + "  ".join("%3d-%-3d" % b for b in BINS) +
          "   laser hit/shot   speed  still%  tgt speed when hit")
    for g in sorted(fires):
        f, h = fires[g], hits[g]
        cells = "  ".join(("%3.0f%%/%-3d" % (100 * h[i] / f[i], f[i])) if f[i] >= 5 else "   -    " for i in range(len(BINS)))
        mv = move[g]
        ts = sorted(tgt_speed[g])
        print("%-12s %s   %3d/%-3d   %5.1f  %4.0f%%   %.1f" % (
            g, cells, lhit[g][1], lhit[g][0], mv[2] / max(1, mv[0]), 100 * mv[1] / max(1, mv[0]),
            ts[len(ts) // 2] if ts else 0))
    print("\nkills (killer group -> victim group):")
    for g, d in sorted(kills.items()):
        print("  %-10s -> %s" % (g, dict(d)))
    print("\ndamage dealt (src group -> dst group):")
    for g, d in sorted(dmg_to.items()):
        print("  %-10s -> %s" % (g, {k: int(v) for k, v in d.items()}))


if __name__ == "__main__":
    main(sys.argv[1:])
