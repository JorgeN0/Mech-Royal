"""Second-by-second trace of the hero in one practice match.

  python -m sim.trace --bot bot.py --seed 1003
"""
import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from sim import run as R  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="bot.py")
    ap.add_argument("--seed", type=int, default=1000)
    a = ap.parse_args()
    R._init()
    from sim import housebot
    from sim.engine import Match
    from sim.loader import BotCtrl, load_bot_class
    import random
    cfg, w = R._G["cfg"], R._G["world"]
    rng = random.Random(a.seed)
    hero = rng.randrange(16)
    hello = {"type": "hello", "protocol": "mechfight/1", "slot": hero, "seed": 1, "config": cfg,
             "map": {"dir": os.path.join(ROOT, "arena"), "files": []}}
    squad = housebot.Squad(hero, w, seed=a.seed)
    ctrls = [None] * 16
    hc = BotCtrl(load_bot_class(a.bot, w), hello, a.bot)
    ctrls[hero] = hc
    for lvl, i in zip(housebot.ORDER, [i for i in range(16) if i != hero]):
        hb = housebot.HouseBot(squad, i, lvl, cfg, a.seed * 31 + i)
        squad.add(hb)
        ctrls[i] = hb
    m = Match(cfg, w, ctrls, seed=a.seed, mode="practice", hero=hero)
    acc = {"dmg": {}, "fired": 0}
    orig = hc.act

    def act(obs):
        out = orig(obs)
        for e in obs["events"]:
            if e["type"] == "damaged":
                k = "%s:%s" % (e.get("by"), e.get("weapon"))
                acc["dmg"][k] = acc["dmg"].get(k, 0) + e["amount"]
        if out.get("fire") and any(out["fire"]):
            acc["fired"] += 1
        if obs["tick"] % 20 == 0:
            me = obs["self"]
            x, y = me["pos"][0], me["pos"][1]
            hunters = sorted((round(math.hypot(o.pos[0] - x, o.pos[1] - y)), o.ctrl.level[0], o.id)
                             for o in m.mechs if o.alive and o.id != hero)
            act_ids = squad.active(obs["t"])
            near = ["%d%s%s" % (d, lv, "*" if i in act_ids else "") for d, lv, i in hunters[:4]]
            vis = [e["id"] for e in obs["mechs"] if e["kind"] == "mech"]
            print("t=%5.1f hp=%4d mode=%-8s alive=%2d seen=%s near=%s fired=%d dmg=%s" % (
                obs["t"], me["hp"], getattr(hc.bot, "mode", "?"), obs["alive"], vis, near, acc["fired"],
                {k: round(v) for k, v in acc["dmg"].items()}))
            acc["dmg"] = {}
            acc["fired"] = 0
        return out
    hc.act = act
    res = m.run()
    h = res[hero]
    print("RESULT place %s score %s kills %s cause %s t_death %s" % (h["place"], h["score"], h["kills"], h["cause"], h["t_death"]))


if __name__ == "__main__":
    main()
