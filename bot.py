"""Mech Royale v1: survival-first bot.

Priorities: stay inside the zone, survive over kill, face attackers, lead gun shots,
grab cheap cubes/loot when nobody is around, retreat to cover when hurt.
Every balance number is read from hello.config. Logs go to stderr via mf.log.
"""
import gc
import math
import os
import random
import time

import mechfight as mf

try:
    from world import World, AStar
except Exception as _e:  # never let a map-module bug kill the bot
    World = AStar = None
    mf.log("world import failed", repr(_e))

TAU = 2.0 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


# higher = better; survival first, then gun output
UPGRADE_PRIORITY = {
    "armor": 100, "plating": 95, "nanites": 85, "gun_rate": 70, "gun_damage": 68,
    "servos": 60, "laser_damage": 58, "laser_charge": 56, "lightning_efficiency": 55,
    "gyros": 50, "sensors": 48, "gun_mag": 46, "capacitor": 42, "jets": 40,
    "lightning_range": 38, "reactor": 36, "gun_velocity": 34, "mortar_reload": 30,
    "lightning_chain": 28, "mortar_velocity": 22, "mortar_splash": 20, "laser_pierce": 18,
}
LOOT_RANK = {"laser": 3, "lightning": 2, "mortar": 2}


def loot_rank(kind, P=None):
    P = PARAMS if P is None else P
    if P.get("lightning_first", 0.0):
        return {"lightning": 3, "laser": 2, "mortar": 1}.get(kind, 0)
    if kind == "mortar" and not P.get("use_mortar", 1.0):
        return 0
    return LOOT_RANK.get(kind, 0)

# Tactics knobs (sim/run.py --botset key=value overrides these for experiments)
PARAMS = {
    "engage_r": 150.0,      # treat mechs closer than this as a fight
    "retreat_hp": 0.28,     # always retreat below this HP fraction
    "retreat_mid": 0.5,     # retreat below this if outnumbered or the enemy is healthier
    "loot_close": 110.0,    # loot only when no enemy is closer than this
    "pref_gun": 100.0,      # preferred fight distance with the gun (sim: 100 > 60)
    "pref_lightning": 32.0,  # ... when we hold lightning
    "pref_vs_lightning": 75.0,  # ... when they hold lightning and we don't
    "pref_hurt_add": 25.0,  # extra distance when below 50% HP
    "finish_hp": 0.3,       # close in on a lone target below this HP fraction
    "ambush": 1.0,          # 1 = stand still pre-aimed when the target is behind cover
    "close_r": 50.0,        # a mech this close is always a threat
    "approach_v": 4.0,      # closing speed (m/s) that marks a mech as coming for us
    "opportunist_hp": 0.35,  # attack a visible mech below this HP fraction within 120 m
    "pick_fights": 0.0,     # 1 = old behaviour: fight anything within engage_r
    "weapon_r_early": 220.0,  # how far to walk for a first weapon (remembered pickups)
    "weapon_early_s": 150.0,  # ... during the first this-many seconds
    "scan_every": 9.0,      # 360-degree torso sweep this often while calm and unarmed
    "laser_swap_r": 150.0,  # walk this far to swap a lightning/mortar for a laser
    "use_mortar": 1.0,      # 0 = never pick up or aim the mortar (its lob aim pauses the gun)
    "laser_gun_aim": 0.0,   # 1 = keep the gun's lead aim while the laser charges; switch only to release
    "strafe_min": 0.9,      # strafe direction flips after a random interval in [min, max] seconds
    "strafe_max": 2.2,
    "fight_boost_e": 999.0,  # boost while strafing in a fight when energy is above this (999 = never)
    "upg_offense": 0.0,     # 1 = take gun/laser damage upgrades before armour
    "drop_r": 110.0,        # go for a supply drop this far away
    "drop_quiet_s": 10.0,   # ... only if no mech was seen for this long
    "tgt_hp_w": 200.0,      # target choice: metres of distance one full HP bar is worth (sim: 200 > 60)
    "heal_hp": 0.0,         # >0: with no threat in sight and HP below this, hide at cover and regenerate
    "kite_strafe": 1.0,     # sideways weight while backing off to preferred distance (lower = back off faster)
    "kite_boost_e": 999.0,  # boost while backing off from a closer-than-preferred target if energy is above this
    "farm_r": 250.0,        # walk this far to remembered drones/crates for cubes (0 = only ones in view; sim +0.8)
    "farm_npc_hp": 0.8,     # only fight drones above this HP fraction
    "farm_crate_hp": 0.7,   # only shoot crates above this HP fraction
    "farm_cap": 99.0,       # stop farming at this many cubes (stats cap at 10; more cubes only make us bigger)
    "laser_bonus": 150.0,   # weapon choice: metres of extra walking a rank point is worth (laser = rank 3; sim +0.35 vs 35)
    "scan_until_laser": 0.0,  # 1 = keep frequent 360-degree scans until we hold a laser (not just any weapon)
    "scoot": 1.0,           # 1 = shoot-and-scoot: duck behind cover while the gun reloads and the laser cools down
    "scoot_r": 30.0,        # ... only to cover this close
    "hidden_reload": 1.0,   # 1 = top up the magazine whenever the target is out of sight
    "scoot_hit_hp": 0.0,    # >0: also duck into nearby cover when hit while below this HP fraction
    "cover_tether": 0.0,    # >0: in a visible fight, drift to stay within this many m of the nearest hiding spot
    "anti_flank": 1.2,      # >0: weight of stepping away from two attackers that are on opposite sides of us
    "hunt": 1.0,            # 1 = laser hunt: with no laser, explore unseen ground (scan on arrival) early on (sim +0.2/+0.46)
    "hunt_s": 150.0,        # ... until this time (s)
    "hunt_reach": 300.0,    # ... and walk this far for a remembered laser while hunting
    "laser_tol": 0.8,       # release the laser when aim error < atan(tol * target radius / distance)
    "laser_lead": 0.08,     # seconds of target motion to lead the (hitscan) laser by
    "upg_laser": 0.0,       # 1 = with a laser in hand, take laser damage/charge upgrades first
    "ambush_move": 0.0,     # >0: strafe sideways at this fraction of speed while waiting pre-aimed (do not stand still)
    "loot_strafe": 0.0,     # 1 = strafe while shooting crates (do not stand still)
    "hit_backoff_s": 0.0,   # >0: after taking mech damage in a fight, back away for this many seconds (still shooting)
    "jet_evade_s": 0.0,     # >0: for this long after taking mech damage, jump + boost (jet up / sprint): bots track movers badly
    "jet_evade_e": 30.0,    # ... only with at least this much energy
    "tp_seek": 0.0,         # 1 = third-party: with no threat, move toward fights we hear (others shooting, not at us)
    "lightning_first": 0.0, # 1 = rank lightning above the laser (close-range play); the hunt then seeks lightning
    "anchor": 0.0,          # 1 = Siege-style rooftop anchor: climb a roof inside the next circle and hold its centre
    "anchor_laser": 1.0,    # ... only once we hold a laser (0 = with any loadout)
    "anchor_t": 0.0,        # ... not before this time (s)
    "anchor_max_d": 300.0,  # ... roofs whose wall approach is at most this far
    "anchor_max_h": 40.0,   # ... roofs at most this high above the ground (jet climb ~60 m from full energy)
    "anchor_farm": 0.0,     # 1 = keep farming remembered drones/crates before anchoring
    "ffa_detect": 1.0,      # 1 = switch to FFA_PARAMS once other mechs are clearly killing each other
    "ffa_early": 2.0,       # ... deaths not caused by us before t=120 s that mean free-for-all
    "ffa_any": 4.0,         # ... or this many at any time
}

# Overrides once we decide this is a free-for-all (the final) rather than practice vs the house squad.
# Sim: farming and focusing the wounded help vs the squad (+0.5) but hurt in FFA (-0.6).
FFA_PARAMS = {"farm_r": 0.0, "tgt_hp_w": 60.0}
ALERT_SOUNDS = {"gun", "mortar", "explosion", "laser", "laser_charge", "lightning", "step", "boost", "jet", "reload"}


class Bot:
    name = "mech-royal-survivor"

    def __init__(self, hello):
        t0 = time.perf_counter()
        self.P = dict(PARAMS)  # per-instance: the FFA switch edits this copy only
        cfg = hello["config"]
        self.cfg = cfg
        self.me_id = hello.get("slot", -1)
        self.rng = random.Random(hello.get("seed", 0))
        m = cfg["mech"]
        self.base_h = float(m.get("height", 9))
        self.base_r = float(m.get("radius", 2.5))
        self.g = float(m.get("gravity", 22))
        cub = cfg.get("cubes", {})
        self.size_per_cube = float(cub.get("size_per_cube", 0.05))
        self.cube_radius = float(cub.get("pickup_radius", 3.5))
        self.pick_r = float(cfg.get("loot", {}).get("pickup_radius", 4))
        self.npc_h = float(cfg.get("npc", {}).get("height", 3.5))
        self.npc_r = float(cfg.get("npc", {}).get("radius", 1.8))
        cr = cfg.get("crates", {})
        self.crate_h = float(cr.get("height", 2.4))
        self.crate_r = float(cr.get("radius", 1.4))
        self.regen_delay = float(m.get("regen_delay", 5))
        self.wcfg = cfg.get("weapons", {})
        self.decision_dt = cfg["match"].get("decide_every", 2) / float(cfg["match"].get("tick_hz", 20))

        self.world = None
        d = self._find_map(hello)
        if d and World is not None:
            try:
                self.world = World(d, step_up=float(m.get("step_up", 1.2)), mech_height=self.base_h)
            except Exception as e:
                mf.log("map load failed:", repr(e))
        mf.log("init %.2fs map=%s world=%s" % (time.perf_counter() - t0, d, self.world is not None))

        # navigation state
        self.path = None
        self.path_i = 0
        self.path_goal = None
        self.path_t = -1e9
        self.astar = None
        self.astar_fail_t = -1e9
        self.zone_key = None
        self.zone_goal = None
        self.hold_key = None
        self.hold_pt = None
        # memory
        self.hist = []
        self.unstuck_until = -1e9
        self.unstuck_dir = (1.0, 0.0)
        self.stuck_count = 0
        self.last_dmg_t = -1e9
        self.last_mech_dmg_t = -1e9
        self.npc_attacker = None  # (npc id, time)
        self.last_dealt_t = -1e9
        self.dmg_bearing = None
        self.attackers = {}  # id -> last time it hit us
        self.last_mech_seen_t = -1e9
        self.alert = None  # (t, bearing)
        self.strafe = 1.0
        self.strafe_flip_t = 0.0
        self.mode = "start"
        self.retreating = False
        self.cover_pt = None
        self.cover_t = -1e9
        self.cover_ok = False
        self.last_move = (0.0, 0.0)
        self.cmd_speed = 0.0
        self.worst_ms = 0.0
        self.n_act = 0
        self.known_pk = {}  # pickup id -> (kind, pos); weapons on the ground never despawn
        self.ffa = False  # set once we believe this is a free-for-all
        self.ffa_t = None
        self.visited = []  # (x, y) of places we scanned from, for the laser hunt
        self.hunt_goal = None
        self.fight_sounds = []  # (t, bearing, dist class) of combat we heard that was not aimed at us
        self.known_npc = {}  # drone id -> pos (drones stay near home)
        self.known_crate = {}  # crate id -> pos (crates never move)
        # telemetry, written into the debug drawing (downloadable by our team after real matches)
        self.tm = {"k": 0, "sh": 0, "hit": 0, "dd": 0.0, "dt": {}}
        self.tm_next = 0.0
        self.scan_start = -1e9
        self.scan_base = 0.0
        self.next_scan_t = 0.0
        # rooftop anchor
        self.roofs = []
        if self.world is not None and self.P["anchor"]:
            try:
                self.roofs = self.world.roof_spots(max_h=self.P["anchor_max_h"])
            except Exception as e:
                mf.log("roof_spots failed:", repr(e))
        self.roof = self.roof_app = self.roof_i = self.roof_key = None
        self.roof_bad = set()
        self.climb_t = None
        self.on_roof = False
        self.want_jump = False
        self.jet = False
        mf.log("roofs %d (%.2fs)" % (len(self.roofs), time.perf_counter() - t0))
        gc.collect()
        if hasattr(gc, "freeze"):
            gc.freeze()

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _find_map(hello):
        here = os.path.dirname(os.path.abspath(__file__))
        d = (hello.get("map") or {}).get("dir") or "/arena"
        cands = [d, os.path.join(os.getcwd(), d), os.path.join(here, d), "/arena", os.path.join(here, "arena")]
        for c in cands:
            try:
                if os.path.isfile(os.path.join(c, "v2", "world.json")) and os.path.isfile(os.path.join(c, "v2", "terrain.i16")):
                    return c
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------ main
    def act(self, obs):
        t0 = time.perf_counter()
        self.deadline = t0 + 0.014  # budget for the time-sliced searches; the 1-CPU match box is slower than ours
        act = {"tick": obs["tick"]}
        try:
            self._act(obs, act)
        except Exception as e:
            mf.log("act error", repr(e))
            if "move" not in act:
                act["move"] = list(self.last_move)
        ms = (time.perf_counter() - t0) * 1000.0
        self.n_act += 1
        if ms > self.worst_ms:
            self.worst_ms = ms
            if ms > 15:
                mf.log("slow act %.1f ms at t=%.1f mode=%s" % (ms, obs.get("t", 0), self.mode))
        return act

    def _act(self, obs, act):
        t = self.t = float(obs["t"])
        me = obs["self"]
        self.me = me
        self.me_id = me.get("id", self.me_id)
        pos = self.pos = me["pos"]
        x, y, z = pos
        st = me.get("stats", {})
        self.hp_frac = me["hp"] / max(1.0, me["max_hp"])
        self.energy = me.get("energy", 0)
        self.walk = float(st.get("walk_speed", 11.7))
        self.eye = z + float(st.get("eye_height", 8))
        self.muzzle = (x, y, z + float(st.get("muzzle_height", 7)))
        self.tube_z = z + float(st.get("height", self.base_h)) + 0.5
        self.turn_rate = float(st.get("turn_rate", 4))
        self.pitch_rate = float(st.get("pitch_rate", 3))
        self.min_pitch = float(st.get("min_pitch", -0.7))
        self.max_pitch = float(st.get("max_pitch", 1.3))
        self.aim_now = me.get("aim", [0.0, 0.0])
        self.weap = me.get("weapons") or [None, None]
        w = self.world
        self.on_roof = w is not None and bool(me.get("grounded")) and z - w.ground_at(x, y) > 4.0
        if self.on_roof:
            self.climb_t = None

        self._events(obs)
        self._detect_ffa(obs)
        self._zone(obs)
        enemies, npcs = self._perceive(obs)
        self.enemies = enemies
        self._remember(obs)

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])

        # ----- decide target and mode
        target = self._pick_target(enemies)
        mode, goal, speed, boost = self._decide(obs, target, npcs)
        self.mode = mode
        if mode != "climb":
            self.climb_t = None

        # ----- legs
        self.want_jump = False
        self.jet = False
        mvx, mvy = self._legs(mode, goal, speed, target, obs)
        jump = False
        climbing = mode == "climb" and self.climb_t is not None
        if climbing:
            jump = self.want_jump
            boost = self.jet
        dodge = None if climbing else self._dodge_mortar(obs)
        if dodge is not None:
            mvx, mvy = dodge
            boost = self.energy > 10
        if t < self.unstuck_until and not climbing:
            mvx, mvy = self.unstuck_dir
            jump = bool(me.get("grounded"))
            boost = self.energy > 5
        if not climbing:
            self._stuck_check(mvx, mvy, mode)
        P = self.P
        if P["jet_evade_s"] > 0 and t - self.last_mech_dmg_t < P["jet_evade_s"] and self.energy > P["jet_evade_e"]                 and mode in ("fight", "retreat", "scoot", "evade", "weapon"):
            # taking fire: get moving fast, and upward (house bots aim where we were and miss climbing targets)
            boost = True
            if me.get("grounded"):
                jump = True
        act["move"] = [round(mvx, 3), round(mvy, 3)]
        act["boost"] = bool(boost)
        if jump:
            act["jump"] = True
        self.last_move = (mvx, mvy)
        self.cmd_speed = hyp(mvx, mvy)

        # ----- torso and weapons
        # only shoot mechs we are actually fighting (gunfire carries 320 m); otherwise loot targets
        aim_target = self.loot_target
        if target is not None and (mode in ("fight", "retreat", "scoot") or target["dh"] < 120.0 or target["id"] in self.attackers):
            aim_target = target
        self._weapons(act, aim_target, mvx, mvy)

        # ----- loot pickup
        self._take(obs, act)

        # ----- debug (small)
        try:
            label = "%s hp%d e%d" % (mode, me["hp"], self.energy)
            if aim_target is not None:
                label += " ->%s" % aim_target.get("id")
                tp = aim_target["pos"]
                mf.draw.line(list(self.muzzle), [tp[0], tp[1], tp[2] + 2], "#ff5040")
            mf.draw.label(label)
            if goal is not None:
                mf.draw.point([goal[0], goal[1], z + 1], "#40c0ff", 5)
            if self.t >= self.tm_next:
                # compact cumulative stats line for offline analysis of real matches (<= 80 chars)
                self.tm_next = self.t + 5.0
                tm = self.tm
                sec = (self.weap[1] or {}).get("kind", "-") if len(self.weap) > 1 else "-"
                dts = ",".join("%s%d" % (k, v) for k, v in sorted(tm["dt"].items()))
                mf.draw.text(("TM%s t%d a%d k%d s%d h%d dd%d c%d %s %s" % (
                    "F" if self.ffa else "", self.t, obs.get("alive", 0), tm["k"], tm["sh"], tm["hit"], tm["dd"], me.get("cubes", 0),
                    sec[:2], dts))[:80])
        except Exception:
            pass

    # ------------------------------------------------------------------ events
    def _events(self, obs):
        t = self.t
        tm = self.tm
        for e in obs.get("events") or ():
            ty = e.get("type")
            if ty == "damaged":
                wk = str(e.get("weapon"))[:2]
                tm["dt"][wk] = tm["dt"].get(wk, 0.0) + float(e.get("amount", 0) or 0)
            elif ty == "hit":
                tm["hit"] += 1
                tm["dd"] += float(e.get("amount", 0) or 0)
            elif ty == "kill":
                tm["k"] += 1
            if ty == "damaged":
                if e.get("weapon") == "zone":
                    continue
                self.last_dmg_t = t
                by = e.get("by")
                if e.get("weapon") == "npc" or (by is not None and by >= 100):
                    self.npc_attacker = (by, t)
                    continue
                self.last_mech_dmg_t = t
                if e.get("bearing") is not None:
                    self.dmg_bearing = e["bearing"]
                if by is not None:
                    self.attackers[by] = t
            elif ty == "hit":
                self.last_dealt_t = t
        for s in obs.get("sounds") or ():
            if s.get("kind") in ("gun", "laser", "lightning", "explosion", "mortar") and s.get("dist") in ("near", "mid")                     and t - self.last_mech_dmg_t > 1.0:
                self.fight_sounds.append((t - float(s.get("age", 0)), float(s["bearing"]), s.get("dist")))
        if len(self.fight_sounds) > 40:
            del self.fight_sounds[:20]
        for s in obs.get("sounds") or ():
            if s.get("kind") in ALERT_SOUNDS and s.get("dist") in ("near", "mid"):
                if self.alert is None or s.get("dist") == "near" or t - self.alert[0] > 1.0:
                    self.alert = (t - float(s.get("age", 0)), float(s["bearing"]))

    def _detect_ffa(self, obs):
        """Practice: house bots only die to us (rarely drones/zone). Final: mechs kill each other."""
        if self.ffa or not self.P["ffa_detect"]:
            return
        n0 = int(self.cfg["match"].get("mechs", 16))
        unexplained = (n0 - int(obs.get("alive", n0))) - self.tm["k"]
        if (unexplained >= self.P["ffa_early"] and self.t < 120.0) or unexplained >= self.P["ffa_any"]:
            self.ffa = True
            self.ffa_t = self.t
            self.P.update(FFA_PARAMS)
            mf.log("free-for-all detected at t=%.1f (%d deaths not ours)" % (self.t, unexplained))

    # ------------------------------------------------------------------ zone
    def _zone(self, obs):
        zn = obs["zone"]
        t = self.t
        x, y = self.pos[0], self.pos[1]
        c = zn["center"]
        r = float(zn["radius"])
        nc = zn["next_center"]
        nr = float(zn["next_radius"])
        ss = float(zn["shrink_start"])
        se = float(zn["shrink_end"])
        if t > ss and se > ss:
            f = clamp((t - ss) / (se - ss), 0.0, 1.0)
            lc = (c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f)
            lr = r + (nr - r) * f
        else:
            lc, lr = (c[0], c[1]), r
        self.live_c, self.live_r = lc, lr
        self.next_c, self.next_r = (nc[0], nc[1]), nr
        margin = clamp(0.25 * nr, 6.0, 60.0)
        self.safe_r = max(0.0, nr - margin)
        d_next = hyp(x - nc[0], y - nc[1])
        self.d_next = d_next
        self.in_safe = d_next <= self.safe_r or (nr <= 1.0 and d_next < 3.0)
        d_live_edge = lr - hyp(x - lc[0], y - lc[1])
        self.d_live_edge = d_live_edge

        key = (round(nc[0], 1), round(nc[1], 1), round(nr, 1))
        if key != self.zone_key:
            self.zone_key = key
            self.zone_goal = self._make_zone_goal(nc, nr)
        # travel needed to reach the safe circle and the time we have for it
        need = max(0.0, d_next - self.safe_r) * 1.35 / max(1.0, self.walk) + 2.0
        if t < ss:
            avail = (ss - t) + 0.5 * (se - ss)
        else:
            avail = max(0.0, se - t)
        self.zone_need, self.zone_avail = need, avail
        self.zone_urgent = (not self.in_safe and need > 0.75 * avail) or d_live_edge < 8.0
        self.outside = d_live_edge < 0.0

    def _make_zone_goal(self, nc, nr):
        """A point well inside the next circle on our side of it, snapped to a free cell."""
        x, y = self.pos[0], self.pos[1]
        dx, dy = x - nc[0], y - nc[1]
        d = hyp(dx, dy)
        depth = 0.55 * max(0.0, nr - clamp(0.25 * nr, 6.0, 60.0))
        if d > 1e-3 and depth > 0:
            gx, gy = nc[0] + dx / d * depth, nc[1] + dy / d * depth
        else:
            gx, gy = nc[0], nc[1]
        w = self.world
        if w is not None:
            k = w.nearest_free(w.cell_of(gx, gy), max_r=25)
            if k is not None:
                gx, gy = w.center(k)
        return (gx, gy)

    def _inside_safe(self, px, py, slack=0.0):
        return hyp(px - self.next_c[0], py - self.next_c[1]) <= self.safe_r + slack

    # ------------------------------------------------------------------ perception
    def _perceive(self, obs):
        x, y, z = self.pos
        t = self.t
        w = self.world
        enemies, npcs = [], []
        eye = (x, y, self.eye)
        n_los = 0
        mechs = sorted(obs.get("mechs") or (), key=lambda m: hyp(m["pos"][0] - x, m["pos"][1] - y))
        for m in mechs:
            p = m["pos"]
            dx, dy = p[0] - x, p[1] - y
            dh = hyp(dx, dy)
            e = dict(m)
            e["dh"] = dh
            e["bearing"] = math.atan2(dy, dx)
            if m.get("kind") == "mech":
                scale = 1.0 + self.size_per_cube * float(m.get("cubes", 0) or 0)
                e["h"] = self.base_h * scale
                e["r"] = self.base_r * scale
            else:
                e["h"] = self.npc_h
                e["r"] = self.npc_r
            com = (p[0], p[1], p[2] + e["h"] * 0.5)
            if w is not None and dh < 260 and n_los < 10:
                e["los"] = w.los(eye, com) or w.los(eye, (p[0], p[1], p[2] + e["h"] * 0.9))
                n_los += 1
            else:
                e["los"] = w is None
            if m.get("kind") == "mech":
                # is it aiming at us?
                a = m.get("aim") or [0, 0]
                back = math.atan2(-dy, -dx)
                e["aimed"] = abs(wrap(a[0] - back)) < max(0.12, math.atan2(4.0, max(dh, 1.0)))
                v = m.get("vel") or [0, 0, 0]
                e["approach"] = -(dx * v[0] + dy * v[1]) / max(dh, 1.0)  # >0: closing on us
                enemies.append(e)
                if dh < 220:
                    self.last_mech_seen_t = t
            else:
                npcs.append(e)
        # forget stale attackers
        for k in [k for k, v in self.attackers.items() if t - v > 8.0]:
            del self.attackers[k]
        return enemies, npcs

    def _is_threat(self, e):
        """Fight only what fights us (or is too close, or is easy to finish); leave the rest alone."""
        P = self.P
        if P["pick_fights"]:
            return e["dh"] < P["engage_r"] or e["id"] in self.attackers
        if e["id"] in self.attackers or e["dh"] < P["close_r"]:
            return True
        if e["dh"] < P["engage_r"] and (e.get("aimed") or e.get("approach", 0.0) > P["approach_v"]):
            return True
        hp, mhp = e.get("hp"), e.get("max_hp") or 1400
        if hp is not None and hp / max(1.0, mhp) < P["opportunist_hp"] and e["dh"] < 120 and e.get("los"):
            return True
        return False

    def _remember(self, obs):
        x, y, z = self.pos
        seen = set()
        for pk in obs.get("pickups") or ():
            self.known_pk[pk["id"]] = (pk.get("kind"), pk["pos"])
            seen.add(pk["id"])
        # forget pickups that should be in plain view (proximity sensors) but are not listed
        gone = [k for k, (kind, p) in self.known_pk.items()
                if k not in seen and hyp(p[0] - x, p[1] - y) < 30.0 and abs(p[2] - z) < 4.0]
        for k in gone:
            del self.known_pk[k]
        # drones and crates: remember where they are, forget them when we are close and they are not there
        for mem, items in ((self.known_npc, [m for m in obs.get("mechs") or () if m.get("kind") == "npc"]),
                           (self.known_crate, obs.get("crates") or ())):
            ids = set()
            for it in items:
                mem[it["id"]] = it["pos"]
                ids.add(it["id"])
            for k in [k for k, p in mem.items() if k not in ids and hyp(p[0] - x, p[1] - y) < 30.0]:
                del mem[k]

    def _pick_target(self, enemies):
        best, bs = None, 1e18
        for e in enemies:
            if e["dh"] > 240 or not self._is_threat(e):
                continue
            s = e["dh"]
            if not e["los"]:
                s += 160
            if e["id"] in self.attackers:
                s -= 90
            if e.get("aimed"):
                s -= 30
            hp = e.get("hp")
            mhp = e.get("max_hp") or 1400
            if hp is not None:
                s += self.P["tgt_hp_w"] * hp / max(1.0, mhp)
            if s < bs:
                best, bs = e, s
        return best

    # ------------------------------------------------------------------ decisions
    def _decide(self, obs, target, npcs):
        """Returns (mode, goal(x,y) or None, speed factor 0..1, boost)."""
        t = self.t
        x, y = self.pos[0], self.pos[1]
        hp = self.hp_frac
        alive = obs.get("alive", 16)
        self.loot_target = None
        boost_ok = self.energy > 25

        P = self.P
        engage_r = P["engage_r"] if alive > 5 else min(90.0, P["engage_r"])
        attacked = t - self.last_mech_dmg_t < 3.0
        threat = target is not None and (target["dh"] < engage_r or target["id"] in self.attackers)
        n_close = sum(1 for e in self.enemies if e["dh"] < 150)
        t_hp = 1.0
        if target is not None and target.get("hp") is not None:
            t_hp = target["hp"] / max(1.0, target.get("max_hp") or 1400)

        # retreat hysteresis: commit to fights we are winning; leave ones we are losing
        if self.retreating:
            if hp > 0.75 or (t - self.last_dmg_t > 7.0 and (target is None or not target["los"])):
                self.retreating = False
        elif threat and (hp < P["retreat_hp"] or (hp < P["retreat_mid"] and (n_close >= 2 or t_hp > hp + 0.15))):
            self.retreating = True

        # zone pressure first: still shoot on the way
        if self.zone_urgent or self.outside:
            return "zone", self.zone_goal, 1.0, (self.energy > 8 and (self.outside or self.zone_need > 0.5 * self.zone_avail))

        on_anchor = P["anchor"] and self.on_roof and self.roof is not None
        if P["anchor"] and self.climb_t is not None and not self.on_roof and self.roof is not None:
            return "climb", (self.roof_app[0], self.roof_app[1]), 1.0, False  # finish a climb once started
        if threat:
            weak_one = t_hp < hp * 0.8 and n_close <= 1
            if self.retreating and not weak_one:
                if on_anchor:
                    return "anchor", self.roof["spot"], 1.0, False  # the roof centre is our cover
                return "retreat", self._cover_point(target), 1.0, boost_ok and target["los"]
            grab = self._near_weapon(35.0)
            if grab is not None:
                return "weapon", grab, 1.0, False  # unarmed and a weapon is right here: take it, still shooting
            has_lightning = any(w and w.get("kind") == "lightning" for w in self.weap)
            fboost = target["los"] and not has_lightning and self.energy > P["fight_boost_e"]
            if target["dh"] < P["pref_gun"] - 20.0 and self.energy > P["kite_boost_e"] and not has_lightning:
                fboost = True  # kite: open the distance fast
            if P["scoot"] and target["los"] and (self._reloading_exposed(target) or
                    (hp < P["scoot_hit_hp"] and t - self.last_mech_dmg_t < 1.0)):
                # shoot-and-scoot: nothing to fire for a couple of seconds, so get out of its sight
                cp = self._cover_point(target)
                if cp is not None and hyp(cp[0] - x, cp[1] - y) < P["scoot_r"]:
                    return "scoot", cp, 1.0, self.energy > 30
            return "fight", None, 1.0, fboost

        # a drone is shooting us: it is 2 cubes for ~8 gun hits, so kill it unless we are hurt
        if self.npc_attacker is not None and t - self.npc_attacker[1] < 3.0:
            n = next((n for n in npcs if n["id"] == self.npc_attacker[0]), None)
            if n is not None:
                if hp > 0.45 and n["los"]:
                    self.loot_target = n
                    return "npc", None, 1.0, False
                away = math.atan2(y - n["pos"][1], x - n["pos"][0])
                return "evade", (x + 40 * math.cos(away), y + 40 * math.sin(away)), 1.0, False

        if attacked and self.dmg_bearing is not None:
            if on_anchor:
                return "anchor", self.roof["spot"], 1.0, False
            # hit by someone we cannot see: turn to face it (handled by aim) and keep moving to cover
            return "evade", self._cover_from_bearing(self.dmg_bearing), 1.0, False

        if self.retreating:
            return "retreat", self.cover_pt or self.zone_goal, 0.38, False

        # between waves: get out of sight and let regen top us up before the next hunters arrive
        if P["heal_hp"] > 0 and hp < P["heal_hp"]:
            for c in obs.get("cubes") or ():
                if hyp(c["pos"][0] - x, c["pos"][1] - y) < 20.0:
                    return "cube", (c["pos"][0], c["pos"][1]), 1.0, False
            if self.dmg_bearing is not None and t - self.last_mech_dmg_t < 25.0:
                return "heal", self._cover_from_bearing(self.dmg_bearing), 1.0, False
            return "heal", self._hold_point(), 1.0, False

        if P["tp_seek"] and hp > 0.6:
            recent = [fs for fs in self.fight_sounds if t - fs[0] < 3.0]
            if len(recent) >= 2:
                bx = sum(math.cos(fs[1]) for fs in recent)
                by = sum(math.sin(fs[1]) for fs in recent)
                b_ = math.atan2(by, bx)
                gx, gy = x + 70.0 * math.cos(b_), y + 70.0 * math.sin(b_)
                if self._inside_live(gx, gy, 15):
                    return "seek", (gx, gy), 1.0, False  # a fight nearby: arrive while they are weakened

        # supply drop when nobody is around
        drop = self._drop_goal(obs)
        if drop is not None:
            return "drop", drop, 1.0, False

        # loot when nobody is close (also right after a kill: the wreck drops cubes and weapons)
        no_close = all(e["dh"] > P["loot_close"] and not (e["dh"] < 220 and e.get("approach", 0.0) > 3.0)
                       for e in self.enemies)
        anchoring = self._anchor_ready()
        if (t - self.last_mech_seen_t > 6.0 or no_close) and self.zone_need < 0.45 * max(1.0, self.zone_avail):
            lg = self._loot_goal(obs, npcs, farm=not anchoring or P["anchor_farm"])
            if lg is not None:
                return lg

        if anchoring:
            r = self.roof
            if self.on_roof and abs(self.pos[2] - r["top"]) < 2.5:
                return "anchor", r["spot"], 1.0, False
            return "climb", (self.roof_app[0], self.roof_app[1]), 1.0, False

        if not self.in_safe:
            return "travel", self.zone_goal, 1.0, False

        return "hold", self._hold_point(), 1.0, False

    def _loot_goal(self, obs, npcs, farm=True):
        x, y, z = self.pos
        hp = self.hp_frac
        early = self.t < 90.0
        # 1) cubes on the ground nearby
        best, bd = None, 90.0
        for c in obs.get("cubes") or ():
            p = c["pos"]
            if abs(p[2] - z) > 3.5:
                continue
            d = hyp(p[0] - x, p[1] - y)
            if d < bd and self._inside_live(p[0], p[1], 10):
                best, bd = p, d
        if best is not None:
            return "cube", (best[0], best[1]), 1.0, False
        # 2) better weapon nearby
        s1 = self.weap[1] if len(self.weap) > 1 else None
        cur = loot_rank(s1["kind"], self.P) if s1 else 0
        held = {w["kind"] for w in self.weap if w}
        P = self.P
        reach = P["weapon_r_early"] if (cur == 0 and self.t < P["weapon_early_s"]) else 60.0
        best, bs = None, 1e18
        for pid, (k, p) in self.known_pk.items():
            if k in held or loot_rank(k, self.P) <= cur:
                continue
            if abs(p[2] - z) > 3.5:
                continue
            d = hyp(p[0] - x, p[1] - y)
            pref_k = "lightning" if P["lightning_first"] else "laser"
            r_ok = max(reach, P["laser_swap_r"]) if k == pref_k else reach  # the preferred weapon is worth a detour
            if k == pref_k and P["hunt"] and self.t < P["hunt_s"]:
                r_ok = max(r_ok, P["hunt_reach"])
            if d > r_ok or not self._inside_live(p[0], p[1], 10):
                continue
            s = d - self.P["laser_bonus"] * loot_rank(k, self.P)
            if s < bs:
                best, bs = p, s
        if best is not None:
            # first weapon: run for it (energy is otherwise unused early)
            first = cur == 0 and self.t < P["weapon_early_s"] and self.energy > 20
            return "weapon", (best[0], best[1]), 1.0, first
        # 3) NPC drones (2 cubes each) only when healthy: hurt, we sit still and regenerate instead
        capped = self.me.get("cubes", 0) >= self.P["farm_cap"]
        if hp > self.P["farm_npc_hp"] and self.t < 330 and not capped:
            best, bd = None, 95.0
            for n in npcs:
                if n["dh"] < bd and n["los"] and self._inside_live(n["pos"][0], n["pos"][1], 0):
                    best, bd = n, n["dh"]
            if best is not None:
                self.loot_target = best
                return "npc", None, 1.0, False
        # 4) crates (1 cube, maybe a weapon); hitting them may reset regen, so not while hurt
        if self.t < 360 and hp > self.P["farm_crate_hp"] and not capped:
            best, bd = None, 75.0
            for c in obs.get("crates") or ():
                p = c["pos"]
                if abs(p[2] - z) > 4.0:
                    continue
                d = hyp(p[0] - x, p[1] - y)
                if d < bd and self._inside_live(p[0], p[1], 5):
                    if self.world is None or self.world.los((x, y, self.eye), (p[0], p[1], p[2] + self.crate_h * 0.5)):
                        best, bd = c, d
            if best is not None:
                p = best["pos"]
                self.loot_target = {"id": best["id"], "kind": "crate", "pos": p, "vel": [0, 0, 0],
                                    "h": self.crate_h, "r": self.crate_r, "dh": bd, "los": True,
                                    "bearing": math.atan2(p[1] - y, p[0] - x)}
                goal = (p[0], p[1]) if bd > 12 else None
                return "crate", goal, 1.0, False
        # 5) farm: walk to remembered drones (2 cubes) and crates (1 cube) that are out of view
        fr = self.P["farm_r"]
        if fr > 0 and not capped and farm:
            best, bs = None, 1e18
            if hp > self.P["farm_npc_hp"] and self.t < 330:
                for p in self.known_npc.values():
                    d = hyp(p[0] - x, p[1] - y)
                    if d < fr and self._inside_live(p[0], p[1], 15) and d - 40.0 < bs:
                        best, bs = p, d - 40.0  # drones are worth twice a crate
            if hp > self.P["farm_crate_hp"] and self.t < 360:
                for p in self.known_crate.values():
                    d = hyp(p[0] - x, p[1] - y)
                    if d < fr and abs(p[2] - z) < 4.0 and self._inside_live(p[0], p[1], 10) and d < bs:
                        best, bs = p, d
            if best is not None:
                return "farm", (best[0], best[1]), 1.0, False
        # 6) laser hunt: no laser yet, so go and look where we have not looked
        pref_k = "lightning" if self.P["lightning_first"] else "laser"
        if self.P["hunt"] and self.t < self.P["hunt_s"] and not any(w and w.get("kind") == pref_k for w in self.weap):
            g = self._hunt_goal()
            if g is not None:
                return "hunt", g, 1.0, False
        return None

    def _hunt_goal(self):
        """Next exploration point: free ground ~120 m away, inside the safe circle, far from where we already looked."""
        x, y = self.pos[0], self.pos[1]
        t = self.t
        if not self.visited or hyp(self.visited[-1][0] - x, self.visited[-1][1] - y) > 60.0:
            self.visited.append((x, y))
        g = self.hunt_goal
        if g is not None and hyp(g[0] - x, g[1] - y) > 8.0 and self._inside_safe(g[0], g[1]):
            return g
        if g is not None:
            self.next_scan_t = t  # arrived: look around (the idle aim does a 360-degree sweep)
        w = self.world
        best, bs = None, -1.0
        for k in range(12):
            a = k * TAU / 12
            for r in (120.0, 80.0):
                px, py = x + r * math.cos(a), y + r * math.sin(a)
                if not self._inside_safe(px, py, -10.0):
                    continue
                if w is not None and not w.free_xy(px, py):
                    continue
                s_ = min(hyp(px - vx, py - vy) for vx, vy in self.visited)
                if s_ > bs:
                    best, bs = (px, py), s_
                break
        self.hunt_goal = best
        return best

    def _near_weapon(self, reach):
        """Goal (x, y) of a remembered weapon within reach if we hold no secondary, else None."""
        if len(self.weap) > 1 and self.weap[1] is not None:
            return None
        x, y, z = self.pos
        held = {w["kind"] for w in self.weap if w}
        best, bd = None, reach
        for k, p in self.known_pk.values():
            if k in held or loot_rank(k, self.P) == 0 or abs(p[2] - z) > 3.5:
                continue
            d = hyp(p[0] - x, p[1] - y)
            if d < bd:
                best, bd = (p[0], p[1]), d
        return best

    def _inside_live(self, px, py, margin):
        return hyp(px - self.live_c[0], py - self.live_c[1]) < self.live_r - margin

    def _drop_goal(self, obs):
        x, y = self.pos[0], self.pos[1]
        P = self.P
        if self.t - self.last_mech_seen_t < P["drop_quiet_s"]:
            return None
        best, bd = None, P["drop_r"]
        for d in obs.get("drops") or ():
            p = d["pos"]
            dist = hyp(p[0] - x, p[1] - y)
            holder = d.get("holder")
            if holder is not None and holder != self.me_id:
                continue
            eta = dist / max(1.0, self.walk)
            if dist < bd and float(d.get("lands_in", 99)) < eta + 6 and self._inside_live(p[0], p[1], 15):
                best, bd = (p[0], p[1]), dist
        return best

    def _hold_point(self):
        """A free spot inside the safe circle next to a building, near us. Recomputed per zone phase."""
        x, y = self.pos[0], self.pos[1]
        if self.hold_key == self.zone_key and self.hold_pt is not None and self._inside_safe(*self.hold_pt):
            return self.hold_pt
        self.hold_key = self.zone_key
        w = self.world
        best, bs = None, 1e18
        if w is not None:
            for ring in (8.0, 18.0, 30.0, 45.0, 60.0):
                for k in range(16):
                    a = k * TAU / 16
                    px, py = x + ring * math.cos(a), y + ring * math.sin(a)
                    if not self._inside_safe(px, py, -5.0) or not w.free_xy(px, py):
                        continue
                    # next to a solid footprint?
                    near = 0
                    for da in range(8):
                        b = da * TAU / 8
                        if w.solid[w.cell_of(px + 4.0 * math.cos(b), py + 4.0 * math.sin(b))]:
                            near += 1
                    if near == 0:
                        continue
                    s = ring - 6.0 * near + 0.15 * hyp(px - self.next_c[0], py - self.next_c[1])
                    if s < bs:
                        best, bs = (px, py), s
                if best is not None and ring >= 18.0:
                    break
        if best is None:
            best = (x, y) if self._inside_safe(x, y, -5.0) else self.zone_goal
        self.hold_pt = best
        return best

    def _cover_point(self, threat):
        """Nearby free point with no line of sight from the threat, inside the zone."""
        t = self.t
        if self.cover_pt is not None and t - self.cover_t < 0.6:
            return self.cover_pt
        self.cover_t = t
        x, y, z = self.pos
        w = self.world
        tp = threat["pos"]
        away = math.atan2(y - tp[1], x - tp[0])
        if w is None:
            self.cover_pt = (x + 30 * math.cos(away), y + 30 * math.sin(away))
            return self.cover_pt
        teye = (tp[0], tp[1], tp[2] + threat.get("h", 9) * 0.9)
        best, bs = None, 1e18
        self.cover_ok = False
        for ring in (10.0, 20.0, 32.0, 45.0):
            for k in range(12):
                a = away + (k - 5.5) * (TAU / 12)
                px, py = x + ring * math.cos(a), y + ring * math.sin(a)
                if not w.free_xy(px, py) or not self._inside_live(px, py, 12):
                    continue
                gz = w.ground_at(px, py)
                if w.los(teye, (px, py, gz + 6.0)):
                    continue
                s = ring + 25.0 * abs(wrap(a - away)) - (0 if self._inside_safe(px, py) else -30)
                if s < bs:
                    best, bs = (px, py), s
            if time.perf_counter() > self.deadline:
                break
        self.cover_ok = best is not None
        if best is None:
            best = (x + 25 * math.cos(away), y + 25 * math.sin(away))
            if not self._inside_live(best[0], best[1], 10):
                best = self.zone_goal
        self.cover_pt = best
        return best

    def _reloading_exposed(self, target):
        """True when we have nothing to shoot for a while: gun reloading and no other weapon ready."""
        gun = next((w for w in self.weap if w and w.get("kind") == "gun"), None)
        if gun is None:
            return False
        if not (float(gun.get("reload", 0) or 0) > 0.4 or int(gun.get("ammo", 1) or 0) == 0):
            return False
        for w in self.weap:
            if not w or w.get("kind") == "gun":
                continue
            ready = float(w.get("cooldown", 0) or 0) < 0.3
            k = w.get("kind")
            if k == "laser" and (ready or float(w.get("charge", 0) or 0) > 0):
                return False
            if k == "lightning" and ready and target["dh"] < float(w.get("stats", {}).get("range", 45)):
                return False
            if k == "mortar" and ready and 28.0 < target["dh"] < 215.0:
                return False
        return True

    def _cover_from_bearing(self, bearing):
        fake = {"pos": [self.pos[0] + 80 * math.cos(bearing), self.pos[1] + 80 * math.sin(bearing), self.pos[2]], "h": 9}
        return self._cover_point(fake)

    # ------------------------------------------------------------------ rooftop anchor
    def _anchor_ready(self):
        """True when we should hold (or climb to) a roof now; picks the roof."""
        P = self.P
        if not P["anchor"] or not self.roofs or self.t < P["anchor_t"] or self.zone_urgent or self.outside:
            return False
        if P["anchor_laser"] and not any(w and w.get("kind") == "laser" for w in self.weap):
            return False
        return self._pick_roof() is not None

    def _pick_roof(self):
        """The climbable roof inside the next safe circle that is near us, deep in the circle and
        far from its own edges. Kept for the whole zone phase while it stays inside the circle."""
        if self.roof is not None and self.roof_key == self.zone_key and \
                self._inside_safe(self.roof["spot"][0], self.roof["spot"][1], -self.roof["inset"]):
            return self.roof
        self.roof_key = self.zone_key
        x, y = self.pos[0], self.pos[1]
        P = self.P
        best, bs = None, 1e18
        for i, r in enumerate(self.roofs):
            if i in self.roof_bad:
                continue
            sx, sy = r["spot"]
            if not self._inside_safe(sx, sy, -r["inset"]):
                continue
            app = min(r["apps"], key=lambda a: hyp(a[0] - x, a[1] - y))
            d = hyp(app[0] - x, app[1] - y)
            if d > P["anchor_max_d"]:
                continue
            s = d + 0.4 * hyp(sx - self.next_c[0], sy - self.next_c[1]) - 4.0 * r["inset"]
            if s < bs:
                best, bs = (i, r, app), s
        if best is None:
            self.roof = self.roof_app = self.roof_i = None
            return None
        if self.roof_i != best[0]:
            self.climb_t = None
            mf.log("anchor roof %d h=%.0f inset=%.0f at t=%.0f" % (best[0], best[1]["h"], best[1]["inset"], self.t))
        self.roof_i, self.roof, self.roof_app = best
        return self.roof

    def _climb_legs(self):
        """Walk to the wall approach, wait for energy, then jump and jet straight up onto the roof."""
        x, y, z = self.pos
        r = self.roof
        ax, ay, ux, uy = self.roof_app
        d = hyp(ax - x, ay - y)
        if self.on_roof:
            # on some other roof or ledge: walk off it towards our approach (there is no fall damage)
            return ((ax - x) / d, (ay - y) / d) if d > 1.0 else (0.0, 0.0)
        grounded = bool(self.me.get("grounded"))
        if self.climb_t is None:
            if d > 2.5:
                return self._nav(ax, ay)
            need = 1.7 * max(0.0, r["top"] - z) + 15.0
            if grounded and self.energy < min(need, 98.0):
                return 0.0, 0.0  # at the wall: regenerate energy first (about 6 s from empty)
            self.climb_t = self.t
        if self.t - self.climb_t > 7.0:
            mf.log("climb failed on roof %s at t=%.0f" % (self.roof_i, self.t))
            self.roof_bad.add(self.roof_i)
            self.roof = self.roof_app = self.roof_i = self.roof_key = None
            self.climb_t = None
            return 0.0, 0.0
        self.want_jump = grounded
        if grounded or z < r["top"] + 0.3:
            self.jet = not grounded
            return ux, uy  # push into the wall and rise along it
        # above the roof edge: stop the jet (or we sail over the whole building) and drop onto the roof
        self.jet = False
        sx, sy = r["spot"]
        ds = hyp(sx - x, sy - y)
        if ds < 3.0:
            return 0.0, 0.0
        return (sx - x) / ds, (sy - y) / ds

    # ------------------------------------------------------------------ legs
    def _legs(self, mode, goal, speed, target, obs):
        x, y = self.pos[0], self.pos[1]
        t = self.t
        if self.P["anchor"]:
            if mode == "climb":
                return self._climb_legs()
            if self.on_roof and self.roof is not None and mode in (
                    "anchor", "fight", "npc", "crate", "retreat", "scoot", "evade", "hold", "heal", "cube"):
                # holding the roof: stay on the spot farthest from the edges (silent, out of sight from below)
                sx, sy = self.roof["spot"]
                d = hyp(sx - x, sy - y)
                if d < 2.0:
                    return 0.0, 0.0
                sp = 1.0 if d > 6.0 else 0.4
                return (sx - x) / d * sp, (sy - y) / d * sp
        if mode in ("fight", "npc") or (mode == "crate" and goal is None):
            tg = target if mode == "fight" else self.loot_target
            if tg is None:
                return 0.0, 0.0
            P = self.P
            if mode == "crate" and not P["loot_strafe"]:
                return 0.0, 0.0
            if mode == "fight" and P["ambush"] and not tg.get("los") and self.d_live_edge > 25.0:
                if P["ambush_move"] <= 0:
                    return 0.0, 0.0  # behind cover: wait silently, pre-aimed where it will reappear
                if t > self.strafe_flip_t:
                    self.strafe = -self.strafe
                    self.strafe_flip_t = t + self.rng.uniform(P["strafe_min"], P["strafe_max"])
                bb = tg["bearing"]  # pre-aimed, but keep sliding sideways: still targets are easy targets
                vx, vy = -math.sin(bb) * self.strafe, math.cos(bb) * self.strafe
                vx, vy = self._avoid(vx, vy, flip=True)
                return vx * P["ambush_move"], vy * P["ambush_move"]
            if t > self.strafe_flip_t:
                self.strafe = -self.strafe
                self.strafe_flip_t = t + self.rng.uniform(self.P["strafe_min"], self.P["strafe_max"])
            b = tg["bearing"]
            d = tg["dh"]
            if mode == "npc":
                pref = 55.0
            elif mode == "crate":
                pref = clamp(d, 15.0, 45.0)  # circle the crate at our current distance while shooting it
            else:
                kinds = set(tg.get("weapons") or ())
                mine = {w["kind"] for w in self.weap if w}
                pref = P["pref_gun"]
                if "lightning" in mine and "mortar" not in kinds:
                    pref = P["pref_lightning"]
                elif "lightning" in kinds and "lightning" not in mine:
                    pref = max(pref, P["pref_vs_lightning"])
                if self.hp_frac < 0.5:
                    pref += P["pref_hurt_add"]
                thp = (tg.get("hp") or 1400) / max(1.0, tg.get("max_hp") or 1400)
                if thp < P["finish_hp"] and sum(1 for e in self.enemies if e["dh"] < 150) <= 1:
                    pref = 28.0  # nearly dead and alone: close in and finish it
            radial = clamp((d - pref) / 25.0, -1.0, 1.0)
            if mode == "fight" and P["hit_backoff_s"] > 0 and t - self.last_mech_dmg_t < P["hit_backoff_s"]:
                radial = -1.0  # just got hit: back off (still facing and shooting), like a human trading damage
            sw = 1.0 if radial >= 0 else 1.0 + (P["kite_strafe"] - 1.0) * min(1.0, -radial * 2.0)
            px, py = -math.sin(b) * self.strafe * sw, math.cos(b) * self.strafe * sw
            mx = math.cos(b) * radial + px
            my = math.sin(b) * radial + py
            if mode == "fight" and P["anti_flank"] > 0:
                # a second attacker behind us hits our back for x1.5: step away from both to bring it in front
                ux, uy = math.cos(b), math.sin(b)
                for e in self.enemies:
                    if e is tg or not e.get("los") or e["dh"] > 150 or not self._is_threat(e):
                        continue
                    if abs(wrap(e["bearing"] - b)) > math.radians(100):
                        vx, vy = ux + math.cos(e["bearing"]), uy + math.sin(e["bearing"])
                        sn = hyp(vx, vy)
                        if sn > 0.3:
                            ax, ay = -vx / sn, -vy / sn
                        else:  # almost opposite: sidestep off the line between them
                            ax, ay = -uy * self.strafe, ux * self.strafe
                        mx += P["anti_flank"] * ax
                        my += P["anti_flank"] * ay
                        break
            if mode == "fight" and P["cover_tether"] > 0 and tg.get("los"):
                # fight next to cover: a reload duck is then ~1 s instead of a long run in the open
                cp = self._cover_point(tg)
                if self.cover_ok:
                    cx, cy = cp[0] - x, cp[1] - y
                    dc = hyp(cx, cy)
                    if dc > P["cover_tether"]:
                        pull = min(1.5, (dc - P["cover_tether"]) / 10.0)
                        mx += pull * cx / dc
                        my += pull * cy / dc
            # do not drift out of the zone while fighting
            zx, zy = self.next_c[0] - x, self.next_c[1] - y
            zd = hyp(zx, zy)
            if self.d_live_edge < 25.0 and zd > 1:
                mx += 1.5 * zx / zd
                my += 1.5 * zy / zd
            n = hyp(mx, my)
            if n < 1e-6:
                return 0.0, 0.0
            mx, my = mx / n, my / n
            mx, my = self._avoid(mx, my, flip=True)
            return mx, my
        if goal is None:
            return 0.0, 0.0
        gx, gy = goal
        d = hyp(gx - x, gy - y)
        if mode in ("hold", "heal"):
            if d < 4.0:
                return 0.0, 0.0
            # walk quietly once close to the hold spot
            speed = min(speed, 4.4 / max(1.0, self.walk)) if d < 35.0 else speed
        if mode in ("cube", "weapon", "drop") and d < 1.0:
            return 0.0, 0.0
        mx, my = self._nav(gx, gy)
        return mx * speed, my * speed

    def _nav(self, gx, gy):
        x, y = self.pos[0], self.pos[1]
        dx, dy = gx - x, gy - y
        d = hyp(dx, dy)
        if d < 0.8:
            return 0.0, 0.0
        w = self.world
        ux, uy = dx / d, dy / d
        if w is None:
            return ux, uy
        if d < 140 and w.walk_line(x, y, gx, gy):
            return self._avoid(ux, uy)
        t = self.t
        replan = (self.path_goal is None or hyp(self.path_goal[0] - gx, self.path_goal[1] - gy) > 12.0
                  or t - self.path_t > 12.0)
        if replan and self.astar is None and t - self.astar_fail_t > 2.0:
            self.astar = AStar(w, x, y, gx, gy)
            self.path_goal = (gx, gy)
            if self.astar.state != "searching":
                self.astar = None
                self.astar_fail_t = t
        if self.astar is not None:
            st = self.astar.step(self.deadline, time.perf_counter)
            if st == "found":
                self.path = self.astar.path
                self.path_i = 0
                self.path_t = t
                self.astar = None
            elif st == "failed":
                self.astar = None
                self.path = None
                self.astar_fail_t = t
        path = self.path
        if path and self.path_goal and hyp(self.path_goal[0] - gx, self.path_goal[1] - gy) < 20.0:
            i = self.path_i
            n = len(path)
            while i < n - 1 and hyp(path[i][0] - x, path[i][1] - y) < 4.0:
                i += 1
            best = i
            for k in range(i + 1, min(n, i + 10)):
                if w.walk_line(x, y, path[k][0], path[k][1]):
                    best = k
            self.path_i = best
            px, py = path[best]
            ex, ey = px - x, py - y
            e = hyp(ex, ey)
            if e > 30.0 and not w.walk_line(x, y, px, py):
                self.path_t = -1e9  # far off the path: replan next time
            if e > 0.5:
                return ex / e, ey / e
        return self._avoid(ux, uy)

    def _avoid(self, ux, uy, flip=False):
        w = self.world
        if w is None:
            return ux, uy
        x, y = self.pos[0], self.pos[1]
        if not w.free_xy(x, y):
            return ux, uy
        for da in (0.0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.6, -1.6, 2.2, -2.2):
            c, s = math.cos(da), math.sin(da)
            vx, vy = ux * c - uy * s, ux * s + uy * c
            if w.free_xy(x + vx * 3.0, y + vy * 3.0) and w.free_xy(x + vx * 6.0, y + vy * 6.0):
                if flip and abs(da) > 1.0:
                    self.strafe = -self.strafe
                return vx, vy
        return ux, uy

    def _stuck_check(self, mvx, mvy, mode):
        t = self.t
        x, y = self.pos[0], self.pos[1]
        h = self.hist
        h.append((t, x, y))
        while h and t - h[0][0] > 1.6:
            h.pop(0)
        if t < self.unstuck_until or hyp(mvx, mvy) < 0.6 or len(h) < 4:
            return
        t0, x0, y0 = h[0]
        if t - t0 < 1.2:
            return
        moved = hyp(x - x0, y - y0)
        if moved < 2.0:
            self.hist = []
            if mode == "fight":
                self.strafe = -self.strafe
                return
            self.stuck_count += 1
            w = self.world
            n = hyp(mvx, mvy)
            ux, uy = mvx / n, mvy / n
            if w is not None:
                for dist in (3.5, 5.5):
                    w.extra[w.cell_of(x + ux * dist, y + uy * dist)] = 1
            self.path_t = -1e9
            self.astar = None
            # jump and jet over it, slightly turned to slide along the wall
            a = math.atan2(uy, ux) + self.rng.uniform(-0.5, 0.5)
            self.unstuck_dir = (math.cos(a), math.sin(a))
            self.unstuck_until = t + 1.3
            mf.log("stuck at t=%.1f (%.0f,%.0f) mode=%s" % (t, x, y, mode))

    def _dodge_mortar(self, obs):
        x, y, z = self.pos
        g = float(self.wcfg.get("mortar", {}).get("gravity", 22))
        rad = float(self.wcfg.get("mortar", {}).get("splash_radius", 7)) + 3.0
        for p in obs.get("projectiles") or ():
            if p.get("kind") != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            # time until the shell falls to our height
            a, b, c = -0.5 * g, vz, pz - z
            disc = b * b - 4 * a * c
            if disc < 0:
                continue
            tl = (-b - math.sqrt(disc)) / (2 * a)
            if tl <= 0 or tl > 3.0:
                continue
            lx, ly = px + vx * tl, py + vy * tl
            dx, dy = x - lx, y - ly
            d = hyp(dx, dy)
            if d < rad:
                if d < 0.5:
                    dx, dy, d = -vy, vx, max(1e-6, hyp(vx, vy))
                return self._avoid(dx / d, dy / d)
        return None

    # ------------------------------------------------------------------ torso / weapons
    def _weapons(self, act, target, mvx, mvy):
        t = self.t
        mx, my, mz = self.muzzle
        ay, ap = self.aim_now
        fire0 = fire1 = False
        aim = None
        gun = self.weap[0] if self.weap and self.weap[0] and self.weap[0].get("kind") == "gun" else None
        # find the gun / secondary wherever they are
        slots = {}
        for i, w in enumerate(self.weap):
            if w:
                slots[w["kind"]] = (i, w)
        fire = [False, False]
        reload = False

        if target is not None and (target.get("los") or target.get("kind") == "crate"):
            tp = target["pos"]
            tv = target.get("vel") or [0, 0, 0]
            zoff = target.get("h", 9) * 0.5
            rad = target.get("r", 2.5)
            d3 = math.sqrt((tp[0] - mx) ** 2 + (tp[1] - my) ** 2 + (tp[2] + zoff - mz) ** 2)
            aimed_weapon = None

            # laser: charge, release when full and on target
            if "laser" in slots:
                li, lw = slots["laser"]
                ls = lw.get("stats", {})
                charge = float(lw.get("charge", 0) or 0)
                full = float(ls.get("charge", 0.75))
                if charge > 0:
                    yaw, pit = self._direct_aim(tp, tv, zoff, self.P["laser_lead"])
                    if not self.P["laser_gun_aim"] or charge >= full * 0.8:
                        aim = (yaw, pit)  # (with laser_gun_aim the gun keeps leading until the charge is nearly full)
                        aimed_weapon = "laser"
                    err_y = abs(wrap(ay - yaw))
                    err_p = abs(ap - pit)
                    tol = math.atan2(rad * self.P["laser_tol"], max(d3, 1.0))
                    hold_max = float(ls.get("hold_s", 2)) + full - 0.25
                    if (charge >= full * 0.95 and err_y < tol and err_p < tol * 1.6) or charge >= hold_max:
                        fire[li] = False  # release
                    else:
                        fire[li] = True
                elif float(lw.get("cooldown", 0)) <= 0 and d3 < float(ls.get("range", 280)) * 0.95 and self.mode not in ("zone", "retreat"):
                    fire[li] = True  # start charging

            # mortar: lob when ready and the target is mid range
            if aimed_weapon is None and "mortar" in slots and target.get("kind") != "crate" and self.P["use_mortar"]:
                mi, mw = slots["mortar"]
                ms = mw.get("stats", {})
                if float(mw.get("cooldown", 0)) <= 0 and 28.0 < target["dh"] < 215.0:
                    sol = self._mortar_aim(tp, tv, ms)
                    if sol is not None:
                        yaw, pit = sol
                        aim = (yaw, pit)
                        aimed_weapon = "mortar"
                        tol_y = math.atan2(3.5, max(target["dh"], 1.0))
                        if abs(wrap(ay - yaw)) < tol_y and abs(ap - pit) < 0.025:
                            fire[mi] = True

            # gun: lead the target
            if gun is not None or "gun" in slots:
                gi, gw = slots.get("gun", (0, gun))
                gs = gw.get("stats", {})
                spd = float(gs.get("speed", 252))
                rng_ = float(gs.get("range", 230))
                yaw, pit, dd, ip = self._lead_aim(tp, tv, zoff, spd)
                if aim is None:
                    aim = (yaw, pit)
                    aimed_weapon = "gun"
                if dd < rng_ * 0.98 and int(gw.get("ammo", 1) or 0) > 0 and float(gw.get("reload", 0) or 0) <= 0:
                    slew_y = self.turn_rate * 0.0125
                    slew_p = self.pitch_rate * 0.0125
                    err_y = max(0.0, abs(wrap(ay - yaw)) - (slew_y if aimed_weapon == "gun" else 0))
                    err_p = max(0.0, abs(ap - pit) - (slew_p if aimed_weapon == "gun" else 0))
                    tol_y = math.atan2(rad * 0.85, max(dd, 1.0)) + 0.004
                    tol_p = math.atan2(zoff * 0.85, max(dd, 1.0)) + 0.004
                    if err_y < tol_y and err_p < tol_p:
                        if self.world is None or self.world.los((mx, my, mz), ip):
                            fire[gi] = True

            # lightning: needs only the yaw cone and range
            if "lightning" in slots:
                li, lw = slots["lightning"]
                ls = lw.get("stats", {})
                if (float(lw.get("cooldown", 0)) <= 0 and d3 < float(ls.get("range", 45)) - 1.5
                        and abs(wrap(ay - target["bearing"])) < math.radians(float(ls.get("cone_deg", 30))) * 0.4
                        and self.energy > float(ls.get("energy", 3)) + 6):
                    fire[li] = True
        else:
            if target is not None and target.get("pos") is not None:
                # tracked behind cover: pre-aim (yaw and pitch) so we fire the moment it reappears
                yaw, pit, _, _ = self._lead_aim(target["pos"], target.get("vel") or [0, 0, 0],
                                                target.get("h", 9) * 0.5, float(self.wcfg.get("gun", {}).get("speed", 252)))
                aim = (yaw, pit)
            # keep a charged laser from wasting itself: release only if it would auto-fire anyway
            if "laser" in slots:
                li, lw = slots["laser"]
                if float(lw.get("charge", 0) or 0) > 0:
                    fire[li] = True
            if gun is not None and float(gun.get("reload", 0) or 0) <= 0:
                ammo = int(gun.get("ammo", 12) or 0)
                mag = int(gun.get("stats", {}).get("mag", 12))
                if ammo < mag * 0.5 and t - self.last_mech_seen_t > 2.0:
                    reload = True
                elif self.P["hidden_reload"] and target is not None and ammo < mag * 0.6:
                    reload = True  # target out of sight: start the next exposure with a full magazine

        if gun is not None and int(gun.get("ammo", 1) or 0) == 0 and float(gun.get("reload", 0) or 0) <= 0:
            reload = True

        if aim is None:
            aim = self._idle_aim(mvx, mvy)
        yaw, pit = aim
        act["aim"] = [round(wrap(yaw), 4), round(clamp(pit, self.min_pitch, self.max_pitch), 4)]
        if fire[0] or fire[1]:
            act["fire"] = fire
            self.tm["sh"] += 1
        if reload:
            act["reload"] = True

    def _idle_aim(self, mvx, mvy):
        t = self.t
        P = self.P
        calm = self.mode not in ("fight", "retreat", "zone", "evade") and t - self.last_dmg_t > 3.0
        armed = len(self.weap) > 1 and self.weap[1] is not None
        if P["scan_until_laser"]:
            armed = any(w and w.get("kind") == "laser" for w in self.weap)
        if calm and t >= self.next_scan_t and t - self.last_mech_seen_t > 4.0:
            self.scan_start = t
            self.scan_base = self.aim_now[0]
            self.next_scan_t = t + P["scan_every"] * (2.5 if armed else 1.0)
        if calm and t - self.scan_start < 1.7:
            # 360-degree sweep: reveals loot and mechs out to vision range in every direction
            return (wrap(self.scan_base + (t - self.scan_start) * self.turn_rate * 0.95), 0.0)
        # someone just hit us or made noise nearby: look there
        if t - self.last_dmg_t < 2.5 and self.dmg_bearing is not None:
            return (self.dmg_bearing, 0.0)
        if self.alert is not None and t - self.alert[0] < 1.5:
            return (self.alert[1], 0.0)
        for e in self.enemies:
            if e["dh"] < 260:
                return (e["bearing"], 0.0)  # tracked but not shootable: keep facing it
        if hyp(mvx, mvy) > 0.3:
            base = math.atan2(mvy, mvx)
            return (base + 0.9 * math.sin(t * 1.3), 0.0)
        return (wrap(t * 1.1), 0.0)  # standing still: sweep a full circle

    def _lead_aim(self, tp, tv, zoff, spd):
        mx, my, mz = self.muzzle
        tz0 = tp[2] + zoff
        vz = tv[2] if len(tv) > 2 else 0.0
        airborne = abs(vz) > 0.5
        tt = 0.0
        lat = 0.06
        px, py, pz = tp[0], tp[1], tz0
        for _ in range(3):
            k = tt + lat
            px = tp[0] + tv[0] * k
            py = tp[1] + tv[1] * k
            pz = tz0 + vz * k - (0.5 * self.g * k * k if airborne else 0.0)
            if airborne and self.world is not None:
                gz = self.world.ground_at(px, py) + zoff * 0.6
                if pz < gz:
                    pz = gz
            dd = math.sqrt((px - mx) ** 2 + (py - my) ** 2 + (pz - mz) ** 2)
            tt = max(0.0, dd - 3.0) / spd
        yaw = math.atan2(py - my, px - mx)
        pit = math.atan2(pz - mz, hyp(px - mx, py - my))
        return yaw, pit, dd, (px, py, pz)

    def _direct_aim(self, tp, tv, zoff, lead_t):
        mx, my, mz = self.muzzle
        px = tp[0] + tv[0] * lead_t
        py = tp[1] + tv[1] * lead_t
        pz = tp[2] + zoff
        return math.atan2(py - my, px - mx), math.atan2(pz - mz, hyp(px - mx, py - my))

    def _mortar_aim(self, tp, tv, ms):
        v = float(ms.get("speed", 72))
        g = float(ms.get("gravity", 22))
        x, y = self.pos[0], self.pos[1]
        oz = self.tube_z
        T = hyp(tp[0] - x, tp[1] - y) / (v * 0.75)
        sol = None
        for _ in range(3):
            px = tp[0] + tv[0] * T
            py = tp[1] + tv[1] * T
            dx = hyp(px - x, py - y) - 3.0
            dz = tp[2] + 1.0 - oz
            if dx < 1.0:
                return None
            disc = v ** 4 - g * (g * dx * dx + 2.0 * dz * v * v)
            if disc < 0:
                return None
            th = math.atan((v * v - math.sqrt(disc)) / (g * dx))
            T = dx / max(1e-3, v * math.cos(th))
            sol = (math.atan2(py - y, px - x), th)
        if sol is None or not (self.min_pitch <= sol[1] <= self.max_pitch):
            return None
        return sol

    # ------------------------------------------------------------------ loot / upgrades
    def _take(self, obs, act):
        x, y, z = self.pos
        held = {}
        for i, w in enumerate(self.weap):
            if w:
                held[w["kind"]] = i
        s1 = self.weap[1] if len(self.weap) > 1 else None
        cur = loot_rank(s1["kind"], self.P) if s1 else 0
        best, br = None, cur
        for pk in obs.get("pickups") or ():
            k = pk.get("kind")
            if k in held or k == "gun":
                continue
            p = pk["pos"]
            if hyp(p[0] - x, p[1] - y) > self.pick_r - 0.3 or abs(p[2] - z) > 3.8:
                continue
            r = loot_rank(k, self.P)
            if r > br:
                best, br = pk, r
        if best is None:
            return
        if s1 is not None:
            if s1.get("kind") == "laser" and float(s1.get("charge", 0) or 0) > 0:
                return
            act["drop"] = 1
        act["take"] = best["id"]

    def _pick_upgrade(self, offer):
        ch = offer.get("choices") or []
        best, bs = 0, -1e9
        for i, c in enumerate(ch):
            s = UPGRADE_PRIORITY.get(c.get("id"), 10)
            if self.P["upg_offense"] and c.get("id") in ("gun_rate", "gun_damage", "laser_damage", "laser_charge"):
                s += 60
            if self.P["upg_laser"] and c.get("id") in ("laser_damage", "laser_charge") and                     any(w and w.get("kind") == "laser" for w in self.weap):
                s += 120 if c.get("id") == "laser_damage" else 110
            if s > bs:
                best, bs = i, s
        return best


if __name__ == "__main__":
    mf.run(Bot)
