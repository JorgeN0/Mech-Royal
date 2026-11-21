"""Where does the rooftop anchor spend its time, and does it actually help on the roof?

  python -m sim.anchor_diag --bot bot.py:anchor=1 --n 48 --seed 5000 --workers 15

Practice matches like sim/trace.py, with the hero's decisions instrumented: time per mode,
climbs started / finished / failed, time on a roof, damage taken and kills made on and off
the roof, and where we died.
"""
import argparse
import collections
import multiprocessing as mp
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from sim import run as R  # noqa: E402

PROBE = False


def one(job):
    spec, seed = job
    R._init()
    from sim import housebot
    from sim.engine import Match
    from sim.loader import BotCtrl, load_bot_class
    cfg, w = R._G["cfg"], R._G["world"]
    rng = random.Random(seed)
    hero = rng.randrange(16)
    hello = {"type": "hello", "protocol": "mechfight/1", "slot": hero, "seed": 1, "config": cfg,
             "map": {"dir": os.path.join(ROOT, "arena"), "files": []}}
    squad = housebot.Squad(hero, w, seed=seed)
    ctrls = [None] * 16
    hc = BotCtrl(load_bot_class(spec, w), hello, spec)
    ctrls[hero] = hc
    for lvl, i in zip(housebot.ORDER, [i for i in range(16) if i != hero]):
        hb = housebot.HouseBot(squad, i, lvl, cfg, seed * 31 + i)
        squad.add(hb)
        ctrls[i] = hb
    m = Match(cfg, w, ctrls, seed=seed, mode="practice", hero=hero)
    st = {"modes": collections.Counter(), "roof_s": 0.0, "climb_start": 0, "climb_ok": 0,
          "dmg_roof": 0.0, "dmg_off": 0.0, "k_roof": 0, "k_off": 0, "laser_t": None,
          "first_roof_t": None, "prev_climb": None, "prev_roof": False, "last_on_roof": False}
    orig = hc.act

    def act(obs):
        out = orig(obs)
        b = hc.bot
        on = bool(getattr(b, "on_roof", False))
        if PROBE and (getattr(b, "climb_t", None) is not None or (st["prev_climb"] is not None)):
            me = obs["self"]
            p = me["pos"]
            r = getattr(b, "roof", None)
            app = getattr(b, "roof_app", None)
            print("t=%6.1f mode=%-6s pos=(%.1f,%.1f,%.1f) gnd=%.1f top=%s app=%s grounded=%s e=%.0f vel=%s move=%s jump=%s boost=%s" % (
                obs["t"], b.mode, p[0], p[1], p[2], w.ground_at(p[0], p[1]), r and round(r["top"], 1),
                app and (round(app[0], 1), round(app[1], 1)), me.get("grounded"), me.get("energy", 0),
                [round(v, 1) for v in me.get("vel", [0, 0, 0])], out.get("move"), out.get("jump"), out.get("boost")))
        st["modes"][getattr(b, "mode", "?")] += 1
        if on:
            st["roof_s"] += 0.1
            if st["first_roof_t"] is None:
                st["first_roof_t"] = obs["t"]
        ct = getattr(b, "climb_t", None)
        if ct is not None and st["prev_climb"] is None:
            st["climb_start"] += 1
        if on and not st["prev_roof"] and st["prev_climb"] is not None:
            st["climb_ok"] += 1
        st["prev_climb"], st["prev_roof"] = ct, on
        st["last_on_roof"] = on
        if st["laser_t"] is None and any(x and x.get("kind") == "laser" for x in obs["self"].get("weapons") or ()):
            st["laser_t"] = obs["t"]
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("weapon") != "zone":
                st["dmg_roof" if on else "dmg_off"] += e["amount"]
            elif e["type"] == "kill":
                st["k_roof" if on else "k_off"] += 1
        return out
    hc.act = act
    res = m.run()
    h = res[hero]
    st["score"], st["place"], st["cause"], st["t_death"] = h["score"], h["place"], h["cause"], h["t_death"]
    st["fails"] = len(getattr(hc.bot, "roof_bad", ()))
    st["died_on_roof"] = st["last_on_roof"]
    st["modes"] = dict(st["modes"])
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="bot.py:anchor=1")
    ap.add_argument("--n", type=int, default=48)
    ap.add_argument("--seed", type=int, default=5000)
    ap.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
    ap.add_argument("--probe", action="store_true", help="one match, print every decision during climbs")
    a = ap.parse_args()
    if a.probe:
        global PROBE
        PROBE = True
        r = one((a.bot, a.seed))
        print("RESULT", {k: r[k] for k in ("score", "climb_start", "climb_ok", "fails", "roof_s")})
        return
    jobs = [(a.bot, a.seed + i) for i in range(a.n)]
    with mp.Pool(a.workers) as pool:
        rows = pool.map(one, jobs)
    n = len(rows)
    modes = collections.Counter()
    for r in rows:
        modes.update(r["modes"])
    tot = sum(modes.values())
    print(f"{a.bot}: {n} matches, mean score {sum(r['score'] for r in rows) / n:.2f}")
    print("decision share by mode:", ", ".join(f"{k} {100 * v / tot:.0f}%" for k, v in modes.most_common(12)))
    got_laser = [r for r in rows if r["laser_t"] is not None]
    reached = [r for r in rows if r["first_roof_t"] is not None]
    print(f"laser in {len(got_laser)}/{n} (median t {sorted(r['laser_t'] for r in got_laser)[len(got_laser) // 2] if got_laser else '-'} s); "
          f"reached a roof in {len(reached)}/{n} (median first t {sorted(r['first_roof_t'] for r in reached)[len(reached) // 2] if reached else '-'} s)")
    print(f"climbs started {sum(r['climb_start'] for r in rows)}, finished {sum(r['climb_ok'] for r in rows)}, "
          f"roofs given up {sum(r['fails'] for r in rows)}; mean time on roof {sum(r['roof_s'] for r in rows) / n:.0f} s")
    rs = sum(r["roof_s"] for r in rows)
    ofs = sum((r["t_death"] or 480) for r in rows) - rs
    print(f"damage taken per minute: on roof {60 * sum(r['dmg_roof'] for r in rows) / max(rs, 1):.0f}, "
          f"off roof {60 * sum(r['dmg_off'] for r in rows) / max(ofs, 1):.0f}; kills on roof {sum(r['k_roof'] for r in rows)}, "
          f"off {sum(r['k_off'] for r in rows)}; died on a roof {sum(r['died_on_roof'] for r in rows)}/{n}")
    causes = collections.Counter(r["cause"] for r in rows)
    print("death causes:", dict(causes), " median death t", sorted((r["t_death"] or 480) for r in rows)[n // 2])


if __name__ == "__main__":
    main()
