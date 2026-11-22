"""How does the hero die? Attackers in the last 5 s, their range, hero mode, time, weapons; score histogram.
usage: python -m sim.death_diag [spec] [n] [squad] [per_kill]"""
import math
import sys
from collections import Counter, defaultdict

from sim import housebot
from sim import run as R
from sim.engine import Match

spec = sys.argv[1] if len(sys.argv) > 1 else "bot.py"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 48
squad = sys.argv[3] if len(sys.argv) > 3 else "real"
pk = float(sys.argv[4]) if len(sys.argv) > 4 else 0.25
cur = {}
_osq, _odmg = housebot.Squad.__init__, Match._damage
deaths = []


def sq_init(self, hero_id, world, seed=0):
    cur.update(hero=hero_id, hits=[], match=None)
    _osq(self, hero_id, world, seed)


def dmg(self, obj, kind, amount, weapon, src, *a, **k):
    hero = cur.get("hero")
    if kind == "mech" and getattr(obj, "id", None) == hero:
        sid = src.id if src is not None and hasattr(src, "id") else "npc/zone"
        d = math.hypot(src.pos[0] - obj.pos[0], src.pos[1] - obj.pos[1]) if src is not None and hasattr(src, "pos") else None
        lvl = getattr(src.ctrl, "level", "?") if src is not None and hasattr(src, "ctrl") else "-"
        cur["hits"].append((self.t, sid, amount, d, weapon, lvl, getattr(getattr(obj.ctrl, "bot", None), "mode", "?"), obj.hp))
    return _odmg(self, obj, kind, amount, weapon, src, *a, **k)


housebot.Squad.__init__ = sq_init
Match._damage = dmg
R._init()
scores, kills = [], []
for i in range(n):
    o = R.run_one(("practice", [spec], 90000 + i, {"_squad": squad, "sched.per_kill": pk}))
    h = next(x for x in o["res"] if x["id"] == o["hero"])
    scores.append(h["score"]); kills.append(h["kills"])
    td = h.get("t_death")
    if td is not None:
        recent = [x for x in cur["hits"] if td - x[0] < 5.0]
        atk = {x[1] for x in recent if isinstance(x[1], int)}
        ds = [x[3] for x in recent if x[3] is not None]
        first = next((x for x in cur["hits"] if isinstance(x[1], int)), None)
        deaths.append({"t": td, "n_atk": len(atk), "dist": sorted(ds)[len(ds) // 2] if ds else None,
                       "mode": recent[-1][6] if recent else "?", "lvls": sorted({x[5] for x in recent if isinstance(x[1], int)}),
                       "weap": h["weap"], "score": h["score"], "kills": h["kills"],
                       "first_hit_t": first[0] if first else None})
print("%s squad=%s per_kill=%s: score %.2f, kills %.2f" % (spec, squad, pk, sum(scores) / n, sum(kills) / n))
print("score histogram:", dict(sorted(Counter(scores).items())))
print("attackers in the last 5 s at death:", dict(sorted(Counter(min(d["n_atk"], 4) for d in deaths).items())))
print("death time:", dict(sorted(Counter(int(d["t"] // 60) * 60 for d in deaths).items())), "(bucket start, s)")
print("hero mode at death:", dict(Counter(d["mode"] for d in deaths).most_common(8)))
print("secondary held at death:", dict(Counter(d["weap"] for d in deaths).most_common()))
print("killer levels:", dict(Counter(l for d in deaths for l in d["lvls"]).most_common()))
ds = sorted(d["dist"] for d in deaths if d["dist"] is not None)
early = [d for d in deaths if d["score"] <= 1]
print("EARLY deaths (score<=1): n=%d, death t median %.0f s, attackers %s, modes %s, weapons %s, killer levels %s, kills %s" % (
    len(early), sorted(d["t"] for d in early)[len(early) // 2] if early else 0,
    dict(Counter(min(d["n_atk"], 4) for d in early)), dict(Counter(d["mode"] for d in early).most_common(5)),
    dict(Counter(d["weap"] for d in early)), dict(Counter(l for d in early for l in d["lvls"])), dict(Counter(d["kills"] for d in early))))
print("first mech hit on us at t (early deaths):", sorted(int(d["first_hit_t"]) for d in early if d["first_hit_t"] is not None))
print("median attacker range at death: %.0f m (quartiles %.0f / %.0f)" % (ds[len(ds) // 2], ds[len(ds) // 4], ds[3 * len(ds) // 4]))
