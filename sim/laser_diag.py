"""Where do our laser shots go? Runs practice matches serially and logs every hero laser shot.
usage: python -m sim.laser_diag [spec] [n] [squad]"""
import math
import sys

from sim import housebot
from sim import run as R
from sim.engine import Match

spec = sys.argv[1] if len(sys.argv) > 1 else "bot.py"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 16
squad = sys.argv[3] if len(sys.argv) > 3 else "real"
log, cur = [], {}
_orig_fire = Match._fire_laser
_orig_sq = housebot.Squad.__init__


def sq_init(self, hero_id, world, seed=0):
    cur["hero"] = hero_id
    _orig_sq(self, hero_id, world, seed)


def fire(self, m, ws, charge):
    if m.id != cur.get("hero"):
        return _orig_fire(self, m, ws, charge)
    d = self._dir(m.aim[0], m.aim[1])
    mz = self._muzzle(m)
    end = (mz[0] + d[0] * ws["range"], mz[1] + d[1] * ws["range"], mz[2] + d[2] * ws["range"])
    wf = self.seg_world_hit(mz, end)
    best = None
    for o in self.mechs:
        if o.id == m.id or not o.alive:
            continue
        dist = math.hypot(o.pos[0] - m.pos[0], o.pos[1] - m.pos[1])
        err = abs((math.atan2(o.pos[1] - m.pos[1], o.pos[0] - m.pos[0]) - m.aim[0] + math.pi) % (2 * math.pi) - math.pi)
        if best is None or err < best[0]:
            best = (err, dist)
    before = sum(o.hp for o in self.mechs if o.id != m.id)
    r = _orig_fire(self, m, ws, charge)
    after = sum(o.hp for o in self.mechs if o.id != m.id)
    log.append({"hit": after < before - 1, "dist": best[1] if best else 999, "wall": wf is not None and best and wf * ws["range"] < best[1],
                "auto": charge >= ws["charge"] + ws.get("hold_s", 2) - 0.051})
    return r


housebot.Squad.__init__ = sq_init
Match._fire_laser = fire
R._init()
for i in range(n):
    R.run_one(("practice", [spec], 70000 + i, {"_squad": squad}))
N = len(log)
H = sum(r["hit"] for r in log)
print("%s: laser shots %d (%.1f per match), hit %.0f%%, into walls %d, auto-fired %d" % (
    spec, N, N / n, 100 * H / max(1, N), sum(1 for r in log if r["wall"] and not r["hit"]), sum(r["auto"] for r in log)))
for a, b in [(0, 60), (60, 120), (120, 180), (180, 300)]:
    L = [r for r in log if a <= r["dist"] < b]
    if L:
        print("   %3d-%3d m: %3d shots, hit %.0f%%" % (a, b, len(L), 100 * sum(r["hit"] for r in L) / len(L)))
