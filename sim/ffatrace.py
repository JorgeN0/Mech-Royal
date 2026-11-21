"""Trace one of our bots through a free-for-all match.  python -m sim.ffatrace --seed 9101"""
import argparse, math, os, random, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sim import run as R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=9101)
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--field", nargs="*", default=["bot.py"] * 8 + ["solo:expert"] * 4 + ["sim/bots/v1_bot.py"] * 4)
    a = ap.parse_args()
    R._init()
    from sim import housebot
    from sim.engine import Match
    from sim.loader import BotCtrl, load_bot_class
    cfg, w = R._G["cfg"], R._G["world"]
    rng = random.Random(a.seed)
    ctrls = []
    for i, spec in enumerate(a.field):
        if spec.startswith("solo:"):
            c = housebot.SoloBot(i, spec.split(":")[1], cfg, i, w)
            c.name = spec
        else:
            c = BotCtrl(load_bot_class(spec, w), {"type": "hello", "slot": i, "seed": i, "config": cfg,
                                                  "map": {"dir": os.path.join(ROOT, "arena")}}, spec)
        ctrls.append(c)
    m = Match(cfg, w, ctrls, seed=a.seed, mode="ffa")
    hero = 0
    hc = ctrls[hero]
    orig = hc.act
    acc = {}

    def act(obs):
        out = orig(obs)
        for e in obs["events"]:
            if e["type"] == "damaged":
                k = "%s:%s%s" % (e.get("by"), e.get("weapon"), "R" if e.get("rear") else "")
                acc[k] = acc.get(k, 0) + e["amount"]
        if obs["tick"] % int(a.every * 20) == 0:
            me = obs["self"]
            x, y = me["pos"][0], me["pos"][1]
            vis = ["%d@%d%s%s%s" % (e["id"], math.hypot(e["pos"][0] - x, e["pos"][1] - y),
                                    "".join(k[0] for k in e.get("weapons", [])[1:]), "C" if e.get("charging") else "",
                                    "!" if hc.bot._is_threat(dict(e, dh=math.hypot(e["pos"][0] - x, e["pos"][1] - y),
                                                                    aimed=False, approach=0)) else "")
                   for e in obs["mechs"] if e["kind"] == "mech"]
            sec = me["weapons"][1]["kind"] if me["weapons"][1] else "-"
            print("t=%5.1f hp=%4d/%4d %-7s %-8s alive=%2d cubes=%d seen=%s dmg=%s" % (
                obs["t"], me["hp"], me["max_hp"], hc.bot.mode, sec, obs["alive"], me["cubes"], vis[:5],
                {k: round(v) for k, v in acc.items()}))
            acc.clear()
        return out
    hc.act = act
    res = m.run()
    print("RESULT", {k: res[hero][k] for k in ("place", "kills", "cause", "t_death", "cubes", "weap")})


if __name__ == "__main__":
    main()
