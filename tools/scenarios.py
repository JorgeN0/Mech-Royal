"""Hand-made situations fed to bot.Bot to check each branch does something sensible.

usage: python tools/scenarios.py      (exit code 1 on any failure)
"""
import copy
import gzip
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import mechfight as mf  # noqa: E402

LOGS = []
mf.log = lambda *a: LOGS.append(" ".join(str(x) for x in a))
import bot as botmod  # noqa: E402

botmod.mf.log = mf.log

msgs = [json.loads(l) for l in gzip.open(os.path.join(ROOT, "check", "stream.jsonl.gz"), "rt", encoding="utf-8") if l.strip()]
HELLO = msgs[0]
HELLO["map"]["dir"] = os.path.join(ROOT, "arena")
BASE = [m for m in msgs if m.get("type") == "obs"][150]
CFG = HELLO["config"]

FAILS = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  " + str(info)))
    if not cond:
        FAILS.append(name)


def make_bot(world=True):
    b = botmod.Bot(HELLO)
    if not world:
        b.world = None
    return b


def obs(t=30.0, pos=(0.0, 0.0, 0.0), aim=(0.0, 0.0), hp=1400, weapons=None, **kw):
    o = copy.deepcopy(BASE)
    o["t"] = t
    o["tick"] = int(t * 20)
    s = o["self"]
    s["pos"] = list(pos)
    s["vel"] = [0, 0, 0]
    s["aim"] = list(aim)
    s["hp"] = hp
    s["max_hp"] = 1400
    s["energy"] = 100
    s["grounded"] = True
    gun = {"kind": "gun", "cooldown": 0, "ammo": 12, "reload": 0, "stats": dict(CFG["weapons"]["gun"])}
    s["weapons"] = weapons if weapons is not None else [gun, None]
    for k in ("mechs", "projectiles", "crates", "cubes", "pickups", "sounds", "beams", "events", "drops"):
        o[k] = []
    o["offer"] = None
    o["zone"] = {"center": [0, 0], "radius": 560, "next_center": [0, 0], "next_radius": 380,
                 "shrink_start": 60, "shrink_end": 110, "dps": 20}
    o["alive"] = 16
    o.update(kw)
    return o


def mech(id_, pos, vel=(0, 0, 0), hp=1400, weapons=("gun",), aim=(0, 0), charging=False):
    return {"id": id_, "kind": "mech", "pos": list(pos), "vel": list(vel), "aim": list(aim), "hp": hp,
            "max_hp": 1400, "cubes": 0, "weapons": list(weapons), "charging": charging}


def wep(kind, **extra):
    d = {"kind": kind, "cooldown": 0, "stats": dict(CFG["weapons"][kind])}
    d.update(extra)
    return d


def run():
    # --- open-field aiming (no map so LOS is assumed clear)
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (60, 0, 0))], aim=(0.0, 0.0)))
    check("aim at stationary enemy east", abs(a["aim"][0]) < 0.02, a)
    check("fire gun when on target", a.get("fire", [False])[0] is True, a)

    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (60, 0, 0))], aim=(2.0, 0.0)))
    check("no fire when torso points away", not a.get("fire", [False, False])[0], a)

    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (100, 0, 0), vel=(0, 10, 0))]))
    exp = math.atan2(10 * (100 / 252 + 0.06), 100)
    check("leads a target moving north", abs(a["aim"][0] - exp) < 0.02, (a["aim"], exp))

    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (50, 0, 0))], hp=300))
    check("low hp -> retreat", b.mode == "retreat", b.mode)
    mv = a["move"]
    check("retreat moves away from enemy", mv[0] < 0, mv)
    check("retreat still faces the enemy", abs(a["aim"][0]) < 0.05, a["aim"])

    # v2: commit to a 1v1 we are winning, even when hurt
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (50, 0, 0), hp=350)], hp=600))
    check("hurt but winning 1v1 -> keep fighting", b.mode == "fight", b.mode)
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (50, 0, 0), hp=1400)], hp=600))
    check("hurt and losing -> retreat", b.mode == "retreat", b.mode)

    # v2: target tracked behind cover -> stand still, pre-aimed at it
    b = make_bot(world=False)
    orig = b._perceive

    def hidden(o, _orig=orig):
        en, np_ = _orig(o)
        for e in en:
            e["los"] = False
        return en, np_
    b._perceive = hidden
    a = b.act(obs(mechs=[mech(5, (70, 20, 0))], events=[{"type": "damaged", "amount": 45, "weapon": "gun",
                                                          "bearing": 0.3, "by": 5}]))
    exp = math.atan2(20, 70)
    check("hidden target -> stand still", b.mode == "fight" and math.hypot(*a["move"]) < 0.01, (b.mode, a["move"]))
    check("hidden target -> pre-aimed at it", abs(a["aim"][0] - exp) < 0.03, (a["aim"], exp))
    check("hidden target -> no fire", not any(a.get("fire", [False, False])), a)

    # v2: after a kill, loot the wreck even if another mech is far away
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(7, (180, 0, 0))], cubes=[{"id": 5009, "pos": [-15, 0, 0]}]))
    check("loots wreck cubes while others are far", b.mode == "cube", b.mode)

    # laser: starts charging, releases when full and on target
    gun = wep("gun", ammo=12, reload=0)
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (80, 0, 0))], weapons=[gun, wep("laser", charge=0)]))
    check("laser starts charging", a.get("fire", [0, 0])[1] is True, a)
    a = b.act(obs(t=31, mechs=[mech(5, (80, 0, 0))], weapons=[gun, wep("laser", charge=0.75)]))
    check("laser releases when full and aimed", a.get("fire", [False, False])[1] is False, a)

    # lightning in range and cone
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (30, 0, 0))], weapons=[gun, wep("lightning")]))
    check("lightning fires at 30 m in cone", a.get("fire", [0, 0])[1] is True, a)
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (80, 0, 0))], weapons=[gun, wep("lightning")]))
    check("lightning holds at 80 m", not a.get("fire", [0, 0])[1], a)

    # mortar lob
    b = make_bot(world=False)
    a = b.act(obs(mechs=[mech(5, (120, 0, 0))], weapons=[gun, wep("mortar")]))
    v, g = CFG["weapons"]["mortar"]["speed"], CFG["weapons"]["mortar"]["gravity"]
    check("mortar pitches up for 120 m", 0.05 < a["aim"][1] < 0.6, a["aim"])
    a2 = b.act(obs(t=30.1, mechs=[mech(5, (120, 0, 0))], weapons=[gun, wep("mortar")], aim=a["aim"]))
    check("mortar fires once aimed", a2.get("fire", [0, 0])[1] is True, a2)

    # loot: take a laser at our feet
    b = make_bot(world=False)
    a = b.act(obs(pickups=[{"id": 8001, "kind": "laser", "pos": [1.5, 0, 0]}]))
    check("takes laser at feet", a.get("take") == 8001 and "drop" not in a, a)
    b = make_bot(world=False)
    a = b.act(obs(weapons=[gun, wep("mortar")], pickups=[{"id": 8002, "kind": "laser", "pos": [1, 1, 0]}]))
    check("swaps mortar for laser", a.get("take") == 8002 and a.get("drop") == 1, a)
    b = make_bot(world=False)
    a = b.act(obs(weapons=[gun, wep("laser")], pickups=[{"id": 8003, "kind": "mortar", "pos": [1, 1, 0]}]))
    check("does not swap laser for mortar", "take" not in a, a)

    # upgrades
    b = make_bot(world=False)
    offer = {"id": 1, "expires": 40, "choices": [{"id": "gun_rate"}, {"id": "sensors"}, {"id": "armor"}]}
    a = b.act(obs(offer=offer))
    check("picks armor", a.get("pick") == 2, a)

    # mortar shell about to land on us -> dodge
    b = make_bot(world=False)
    shell = {"id": 1, "kind": "mortar", "pos": [-20, 0, 15], "vel": [20, 0, 0]}
    a = b.act(obs(projectiles=[shell]))
    check("dodges incoming shell", math.hypot(*a["move"]) > 0.5 and a.get("boost"), a)

    # cube nearby, nobody around
    b = make_bot(world=False)
    a = b.act(obs(t=10, cubes=[{"id": 5001, "pos": [20, 5, 0]}]))
    check("walks to cube", b.mode == "cube" and a["move"][0] > 0.5, (b.mode, a["move"]))

    # damaged by unseen attacker -> look there
    b = make_bot(world=False)
    a = b.act(obs(events=[{"type": "damaged", "amount": 45, "weapon": "gun", "bearing": 1.5, "by": 9}]))
    check("faces unseen attacker bearing", abs(a["aim"][0] - 1.5) < 0.01, a["aim"])

    # zone: outside the live circle late -> run for it
    b = make_bot(world=False)
    z = {"center": [0, 0], "radius": 140, "next_center": [0, 0], "next_radius": 70,
         "shrink_start": 230, "shrink_end": 260, "dps": 60}
    a = b.act(obs(t=250, pos=(200, 0, 0), zone=z))
    check("outside zone -> zone mode with boost", b.mode == "zone" and a.get("boost") and a["move"][0] < -0.5, (b.mode, a))

    # stuck: commanding motion but not moving -> jump
    b = make_bot(world=False)
    for k in range(20):
        a = b.act(obs(t=40 + k * 0.1, pos=(0, 0, 0), cubes=[{"id": 5001, "pos": [30, 0, 0]}]))
        if a.get("jump"):
            break
    check("unsticks with a jump", a.get("jump") is True, a)

    # --- with the map: long zone trip needs A*; act time stays in budget
    b = make_bot(world=True)
    w = b.world
    z = {"center": [41.3, -72.8], "radius": 560, "next_center": [300, 200], "next_radius": 140,
         "shrink_start": 60, "shrink_end": 110, "dps": 20}
    start = w.center(w.nearest_free(w.cell_of(-400, -250)))
    worst = 0.0
    found = False
    for k in range(30):
        t0 = time.perf_counter()
        a = b.act(obs(t=20 + k * 0.1, pos=(start[0], start[1], w.ground_at(*start)), zone=z))
        worst = max(worst, (time.perf_counter() - t0) * 1000)
        found = found or b.path is not None
    check("A* path found for cross-map trip", found, (b.astar and b.astar.state, b.path))
    check("worst act with A* under 35 ms", worst < 35, worst)
    print("     worst act ms (this machine): %.1f" % worst)
    errs = [l for l in LOGS if "act error" in l]
    check("no act errors logged", not errs, errs[:3])


if __name__ == "__main__":
    run()
    print("%d failures" % len(FAILS))
    sys.exit(1 if FAILS else 0)
