"""Replay check/stream.jsonl.gz through the bot and report how often visible enemies pass our LOS gates.

Every mech in obs.mechs was seen by the server within memory_s, so a low LOS pass rate for close,
freshly-seen mechs points at a bug in world.los (we would hold fire at targets we can hit).
"""
import gzip
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import mechfight as mf  # noqa: E402

mf.log = lambda *a: None
import bot as botmod  # noqa: E402

botmod.mf.log = mf.log
msgs = [json.loads(l) for l in gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt", encoding="utf-8") if l.strip()]
hello = msgs[0]
hello["map"]["dir"] = os.path.join(ROOT, "arena")
b = botmod.Bot(hello)
w = b.world

n_obs_enemy = n_eye = n_muz = 0
fails = []
modes = {}
fires = 0
for o in msgs[1:]:
    if o.get("type") != "obs":
        continue
    a = b.act(o)
    modes[b.mode] = modes.get(b.mode, 0) + 1
    if a.get("fire") and any(a["fire"]):
        fires += 1
    me = o["self"]
    x, y, z = me["pos"]
    for m in o["mechs"]:
        if m["kind"] != "mech":
            continue
        p = m["pos"]
        d = math.hypot(p[0] - x, p[1] - y)
        if d > 120:
            continue
        n_obs_enemy += 1
        eye = (x, y, z + 8)
        muz = (x, y, z + 7)
        com = (p[0], p[1], p[2] + 4.5)
        e = w.los(eye, com) or w.los(eye, (p[0], p[1], p[2] + 8))
        mz = w.los(muz, com)
        n_eye += e
        n_muz += mz
        if not e and len(fails) < 8:
            fails.append((o["t"], round(d, 1), [round(v, 1) for v in me["pos"]], [round(v, 1) for v in p],
                          round(w.ground_at(x, y), 2), round(w.ground_at(p[0], p[1]), 2)))
def why(a, bpt, step=1.5):
    dx, dy, dz = bpt[0] - a[0], bpt[1] - a[1], bpt[2] - a[2]
    n = int(math.hypot(dx, dy) / step)
    for k in range(1, n):
        f = k / n
        px, py, pz = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
        c = w.cell_of(px, py)
        if pz < w.ground[c] - 0.5:
            return "terrain at f=%.2f z=%.1f cellground=%.1f fine=%.1f" % (f, pz, w.ground[c], w.ground_at(px, py))
        if w.bb[c] <= pz <= w.bt[c]:
            return "building at f=%.2f z=%.1f (%.1f..%.1f)" % (f, pz, w.bb[c], w.bt[c])
        if w.rb[c] <= pz <= w.rt[c]:
            return "bridge at f=%.2f z=%.1f" % (f, pz)
    return "clear"


# ground truth: a gun bullet that hit us had a clear path from the shooter's muzzle
gt = gt_ok = 0
by_pos = {}
for o in msgs[1:]:
    if o.get("type") != "obs":
        continue
    for m in o["mechs"]:
        by_pos[m["id"]] = m["pos"]
    me = o["self"]["pos"]
    for e in o["events"]:
        if e["type"] == "damaged" and e.get("weapon") == "gun" and e.get("by") in by_pos:
            p = by_pos[e["by"]]
            src = (p[0], p[1], p[2] + 7)
            dst = (me[0], me[1], me[2] + 4.5)
            gt += 1
            ok = w.los(src, dst)
            gt_ok += ok
            if not ok:
                print("  GT blocked: t=%s d=%.0f %s" % (o["t"], math.hypot(p[0] - me[0], p[1] - me[1]), why(src, dst)))
print("ground-truth gun hits on us: %d, our LOS agrees on %d" % (gt, gt_ok))
for f in fails[:3]:
    t_, d_, me_, th_, _, _ = f
    print("  sighting blocked by:", why((me_[0], me_[1], me_[2] + 8), (th_[0], th_[1], th_[2] + 4.5)))

print("enemy sightings <120 m:", n_obs_enemy, " eye-LOS pass %.0f%%" % (100.0 * n_eye / max(1, n_obs_enemy)),
      " muzzle-LOS pass %.0f%%" % (100.0 * n_muz / max(1, n_obs_enemy)))
print("modes:", modes, " decisions with fire:", fires)
for f in fails:
    print("  blocked: t=%s d=%s me=%s them=%s ground me/them=%s/%s" % f)
