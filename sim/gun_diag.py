"""Where do our gun bullets go? mech / drone-crate / wall / expired, by target distance at fire time.
usage: python -m sim.gun_diag [spec] [n] [squad]"""
import math
import sys
from collections import Counter

from sim import housebot
from sim import run as R
from sim.engine import Match

spec = sys.argv[1] if len(sys.argv) > 1 else "bot.py"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
squad = sys.argv[3] if len(sys.argv) > 3 else "real"
cur, out = {}, Counter()
_orig_sq, _orig_proj = housebot.Squad.__init__, Match._projectiles


def sq_init(self, hero_id, world, seed=0):
    cur["hero"] = hero_id
    _orig_sq(self, hero_id, world, seed)


def proj(self, dt):
    hero = cur.get("hero")
    for p in self.projectiles:
        if p["kind"] != "gun" or p["owner"] != hero:
            continue
        a, v = p["pos"], p["vel"]
        b = [a[0] + v[0] * dt, a[1] + v[1] * dt, a[2] + v[2] * dt]
        wf = self.seg_world_hit(a, b)
        lim = wf if wf is not None else 1.0
        best = None
        for tg in self._targets(hero):
            s = self.seg_cyl(a, b, tg[2], tg[3], tg[4], tg[5], tg[6] + p["radius"])
            if s is not None and s <= lim and (best is None or s < best[0]):
                best = (s, tg)
        if best:
            out["mech" if best[1][0] == "mech" else "drone/crate"] += 1
        elif wf is not None:
            od = math.hypot(a[0] - p["origin"][0], a[1] - p["origin"][1])
            out["wall<20m" if od < 20 else "wall"] += 1
        elif p["ttl"] - dt <= 0:
            out["expired"] += 1
    return _orig_proj(self, dt)


housebot.Squad.__init__ = sq_init
Match._projectiles = proj
R._init()
for i in range(n):
    R.run_one(("practice", [spec], 71000 + i, {"_squad": squad}))
tot = sum(out.values())
print("%s: %d bullets  " % (spec, tot) + "  ".join("%s %.0f%%" % (k, 100 * v / tot) for k, v in out.most_common()))
