"""Run local matches in parallel.

  python -m sim.run --mode practice --bot bot.py --n 48
  python -m sim.run --mode ffa --bots bot.py sim/bots/v1_bot.py --n 16

practice: our bot (hero) vs a 15-bot house squad (4 easy, 4 medium, 4 hard, 3 expert). The match ends
when the hero dies; score = 16 - place, as on the practice board.
ffa: 16 bots, assigned round-robin from --bots (empty slots: expert house-like bots are not used;
fill with the listed bots). Reports mean (16 - place) per bot file.
"""
import argparse
import gzip
import json
import multiprocessing as mp
import os
import random
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_G = {}


def _init():
    from world import World
    with gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt", encoding="utf-8") as f:
        hello = json.loads(f.readline())
    _G["cfg"] = hello["config"]
    _G["world"] = World(os.path.join(ROOT, "arena"))
    if os.environ.get("SIM_TREES", "1") != "0":  # SIM_TREES=0 turns canopy sight-blocking off
        from sim.trees import TreeIndex
        _G["world"].trees = TreeIndex(os.path.join(ROOT, "arena"))


def run_one(args):
    mode, bots, seed, overrides = args
    if "world" not in _G:
        _init()
    from sim import housebot
    from sim.engine import Match
    from sim.loader import BotCtrl, load_bot_class
    for k, v in (overrides or {}).items():
        lvl, key = k.split(".")
        if lvl == "sched":  # e.g. sched.per_kill=0.5 sched.all_s=360
            housebot.SCHEDULE[key] = v
        else:
            housebot.LEVELS[lvl][key] = v
    cfg, w = _G["cfg"], _G["world"]
    rng = random.Random(seed)
    arena = os.path.join(ROOT, "arena")

    def hello(slot):
        return {"type": "hello", "protocol": "mechfight/1", "slot": slot, "seed": rng.randrange(1 << 31),
                "map": {"dir": arena, "files": ["v2/world.json", "v2/terrain.i16"]}, "config": cfg}

    ctrls = [None] * 16
    if mode == "practice":
        hero = rng.randrange(16)
        squad = housebot.Squad(hero, w, seed=seed)
        cls = load_bot_class(bots[0], w)
        ctrls[hero] = BotCtrl(cls, hello(hero), bots[0])
        others = [i for i in range(16) if i != hero]
        for lvl, i in zip(housebot.ORDER, others):
            hb = housebot.HouseBot(squad, i, lvl, cfg, seed * 31 + i)
            squad.add(hb)
            ctrls[i] = hb
    else:
        hero = None
        order = list(range(16))
        rng.shuffle(order)
        for k, i in enumerate(order):
            spec = bots[k % len(bots)]
            if spec.startswith("solo:"):  # e.g. solo:expert -> aggressive stand-in opponent
                ctrls[i] = housebot.SoloBot(i, spec.split(":")[1], cfg, seed * 31 + i, w)
                ctrls[i].name = spec
            else:
                ctrls[i] = BotCtrl(load_bot_class(spec, w), hello(i), spec)
    m = Match(cfg, w, ctrls, seed=seed, mode=mode, hero=hero)
    t0 = time.perf_counter()
    res = m.run()
    wall = time.perf_counter() - t0
    timing = {c.name: (c.worst_ms, c.total_ms / max(1, c.n), getattr(c, "worst_at", None)) for c in ctrls if hasattr(c, "worst_ms")}
    ffa = {}
    for c in ctrls:
        if hasattr(c, "bot"):
            ffa.setdefault(c.name, []).append(getattr(c.bot, "ffa_t", None))
    return {"seed": seed, "hero": hero, "spec": bots[0], "res": res, "wall": wall, "t_end": m.t, "timing": timing, "ffa": ffa,
            "log": m.log[:5]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["practice", "ffa"], default="practice")
    ap.add_argument("--bot", default="bot.py")
    ap.add_argument("--bots", nargs="*")
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--set", nargs="*", default=[], help="house overrides, e.g. easy.sigma=0.05")
    ap.add_argument("--json", help="write raw results here")
    a = ap.parse_args()
    bots = a.bots if a.bots else [a.bot]
    overrides = {}
    for s in a.set:
        k, v = s.split("=")
        overrides[k] = float(v) if v.replace(".", "", 1).replace("-", "", 1).isdigit() else (v == "True")
    if a.mode == "practice":  # every spec plays the same seeds
        jobs = [(a.mode, [spec], a.seed + i, overrides) for spec in bots for i in range(a.n)]
    else:
        jobs = [(a.mode, bots, a.seed + i, overrides) for i in range(a.n)]
    t0 = time.perf_counter()
    if a.workers > 1 and a.n > 1:
        with mp.Pool(a.workers, initializer=_init) as pool:
            out = pool.map(run_one, jobs, chunksize=1)
    else:
        out = [run_one(j) for j in jobs]
    wall = time.perf_counter() - t0
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f)
    logs = [l for o in out for l in o["log"]]
    if logs:
        print("bot errors:", logs[:3])
    if a.mode == "practice":
        for spec in bots:
            sub = [o for o in out if o["spec"] == spec]
            hs = [o["res"][o["hero"]] for o in sub]
            sc = [h["score"] for h in hs]
            causes = {}
            for h in hs:
                causes[h["cause"]] = causes.get(h["cause"], 0) + 1
            wk = {}
            for h in hs:
                wk[h["weap"]] = wk.get(h["weap"], 0) + 1
            took = [h["took_t"] for h in hs if h["took_t"] is not None]
            print("   weapons at death %s, first weapon taken in %d/%d matches (median t %.0f s)" % (
                wk, len(took), len(hs), statistics.median(took) if took else -1))
            ws, wh = {}, {}
            for h in hs:
                for k, v in (h.get("wshots") or {}).items():
                    ws[k] = ws.get(k, 0) + v
                for k, v in (h.get("whits") or {}).items():
                    wh[k] = wh.get(k, 0) + v
            print("   hit rate on mechs+drones by weapon (crate shots count as misses): %s" % {k: "%d%% of %d" % (100 * wh.get(k, 0) / max(1, v), v) for k, v in ws.items()})
            print("   supply drops captured per match: %.2f" % statistics.mean(h.get("drops", 0) for h in hs))
            print("   score by weapon held: %s" % {k: "%.2f" % statistics.mean(h["score"] for h in hs if h["weap"] == k)
                                                  for k in wk})
            shots = sum(h["shots"] for h in hs)
            hits = sum(h["hits"] for h in hs)
            sd = statistics.pstdev(sc) if len(sc) > 1 else 0
            print("practice %-40s score %.2f +- %.2f (n=%d) kills %.2f death t %.0f s cubes %.1f hit %.0f%% causes %s" % (
                spec, statistics.mean(sc), sd / max(1, len(sc)) ** 0.5, len(sc), statistics.mean(h["kills"] for h in hs),
                statistics.mean(h["t_death"] or o["t_end"] for h, o in zip(hs, sub)), statistics.mean(h["cubes"] for h in hs),
                100.0 * hits / max(1, shots), causes))
        if len(bots) > 1:
            # paired by seed: same spawns/zone/loot, so most of the luck cancels
            base = {o["seed"]: o["res"][o["hero"]]["score"] for o in out if o["spec"] == bots[0]}
            for spec in bots[1:]:
                d = [o["res"][o["hero"]]["score"] - base[o["seed"]] for o in out if o["spec"] == spec and o["seed"] in base]
                if len(d) > 1:
                    m_, se = statistics.mean(d), statistics.pstdev(d) / len(d) ** 0.5
                    verdict = "BETTER" if m_ > 2 * se else "WORSE" if m_ < -2 * se else "no clear difference"
                    print("   paired vs %s: %-40s %+.2f +- %.2f  -> %s" % (bots[0], spec, m_, se, verdict))
    else:
        by = {}
        for o in out:
            for r in o["res"]:
                by.setdefault(r["name"], []).append(r)
        for name, rs in sorted(by.items(), key=lambda kv: -statistics.mean(r["score"] for r in kv[1])):
            sc = [r["score"] for r in rs]
            causes = {}
            for r in rs:
                causes[r["cause"]] = causes.get(r["cause"], 0) + 1
            print("ffa %-16s score %.2f +- %.2f (n=%d) kills %.2f causes %s" % (
                name, statistics.mean(sc), statistics.pstdev(sc) / max(1, len(sc)) ** 0.5, len(sc),
                statistics.mean(r["kills"] for r in rs), causes))
    det = {}
    for o in out:
        for k, ts in o.get("ffa", {}).items():
            det.setdefault(k, []).extend(ts)
    for k, ts in det.items():
        hit = [t for t in ts if t is not None]
        if hit or a.mode == "ffa":
            print("FFA detector %-40s fired for %d/%d bots%s" % (k, len(hit), len(ts),
                  (" (median t %.0f s)" % statistics.median(hit)) if hit else ""))
    tm = {}
    for o in out:
        for k, (worst, avg, at) in o["timing"].items():
            w0, a0, n0, at0 = tm.get(k, (0, 0, 0, None))
            tm[k] = (max(w0, worst), a0 + avg, n0 + 1, at if worst > w0 else at0)
    for k, (w, a_, n, at) in tm.items():
        print("act ms %-40s avg %.2f worst %.1f (t, mode at worst: %s)" % (k, a_ / n, w, at))
    print("sim wall %.0f s for %d matches (%.1f s/match/worker)" % (wall, len(out), statistics.mean(o["wall"] for o in out)))


if __name__ == "__main__":
    main()
