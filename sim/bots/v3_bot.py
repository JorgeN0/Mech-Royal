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
}
ALERT_SOUNDS = {"gun", "mortar", "explosion", "laser", "laser_charge", "lightning", "step", "boost", "jet", "reload"}


class Bot:
    name = "mech-royal-survivor"

    def __init__(self, hello):
        t0 = time.perf_counter()
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
        self.last_move = (0.0, 0.0)
        self.cmd_speed = 0.0
        self.worst_ms = 0.0
        self.n_act = 0
        self.known_pk = {}  # pickup id -> (kind, pos); weapons on the ground never despawn
        self.scan_start = -1e9
        self.scan_base = 0.0
        self.next_scan_t = 0.0
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
        self.deadline = t0 + 0.028
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

        self._events(obs)
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

        # ----- legs
        mvx, mvy = self._legs(mode, goal, speed, target, obs)
        jump = False
        dodge = self._dodge_mortar(obs)
        if dodge is not None:
            mvx, mvy = dodge
            boost = self.energy > 10
        if t < self.unstuck_until:
            mvx, mvy = self.unstuck_dir
            jump = bool(me.get("grounded"))
            boost = self.energy > 5
        self._stuck_check(mvx, mvy, mode)
        act["move"] = [round(mvx, 3), round(mvy, 3)]
        act["boost"] = bool(boost)
        if jump:
            act["jump"] = True
        self.last_move = (mvx, mvy)
        self.cmd_speed = hyp(mvx, mvy)

        # ----- torso and weapons
        # only shoot mechs we are actually fighting (gunfire carries 320 m); otherwise loot targets
        aim_target = self.loot_target
        if target is not None and (mode in ("fight", "retreat") or target["dh"] < 120.0 or target["id"] in self.attackers):
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
        except Exception:
            pass

    # ------------------------------------------------------------------ events
    def _events(self, obs):
        t = self.t
        for e in obs.get("events") or ():
            ty = e.get("type")
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
            if s.get("kind") in ALERT_SOUNDS and s.get("dist") in ("near", "mid"):
                if self.alert is None or s.get("dist") == "near" or t - self.alert[0] > 1.0:
                    self.alert = (t - float(s.get("age", 0)), float(s["bearing"]))

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
        P = PARAMS
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
                s += 60.0 * hp / max(1.0, mhp)
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

        P = PARAMS
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

        if threat:
            weak_one = t_hp < hp * 0.8 and n_close <= 1
            if self.retreating and not weak_one:
                return "retreat", self._cover_point(target), 1.0, boost_ok and target["los"]
            grab = self._near_weapon(35.0)
            if grab is not None:
                return "weapon", grab, 1.0, False  # unarmed and a weapon is right here: take it, still shooting
            return "fight", None, 1.0, False

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
            # hit by someone we cannot see: turn to face it (handled by aim) and keep moving to cover
            return "evade", self._cover_from_bearing(self.dmg_bearing), 1.0, False

        if self.retreating:
            return "retreat", self.cover_pt or self.zone_goal, 0.38, False

        # supply drop when nobody is around
        drop = self._drop_goal(obs)
        if drop is not None:
            return "drop", drop, 1.0, False

        # loot when nobody is close (also right after a kill: the wreck drops cubes and weapons)
        no_close = all(e["dh"] > P["loot_close"] and not (e["dh"] < 220 and e.get("approach", 0.0) > 3.0)
                       for e in self.enemies)
        if (t - self.last_mech_seen_t > 6.0 or no_close) and self.zone_need < 0.45 * max(1.0, self.zone_avail):
            lg = self._loot_goal(obs, npcs)
            if lg is not None:
                return lg

        if not self.in_safe:
            return "travel", self.zone_goal, 1.0, False

        return "hold", self._hold_point(), 1.0, False

    def _loot_goal(self, obs, npcs):
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
        cur = LOOT_RANK.get(s1["kind"], 0) if s1 else 0
        held = {w["kind"] for w in self.weap if w}
        P = PARAMS
        reach = P["weapon_r_early"] if (cur == 0 and self.t < P["weapon_early_s"]) else 60.0
        best, bs = None, 1e18
        for pid, (k, p) in self.known_pk.items():
            if k in held or LOOT_RANK.get(k, 0) <= cur:
                continue
            if abs(p[2] - z) > 3.5:
                continue
            d = hyp(p[0] - x, p[1] - y)
            r_ok = max(reach, P["laser_swap_r"]) if k == "laser" else reach  # the laser is worth a detour
            if d > r_ok or not self._inside_live(p[0], p[1], 10):
                continue
            s = d - 35.0 * LOOT_RANK.get(k, 0)
            if s < bs:
                best, bs = p, s
        if best is not None:
            # first weapon: run for it (energy is otherwise unused early)
            first = cur == 0 and self.t < P["weapon_early_s"] and self.energy > 20
            return "weapon", (best[0], best[1]), 1.0, first
        # 3) NPC drones (2 cubes each) only when healthy: hurt, we sit still and regenerate instead
        if hp > 0.8 and self.t < 330:
            best, bd = None, 95.0
            for n in npcs:
                if n["dh"] < bd and n["los"] and self._inside_live(n["pos"][0], n["pos"][1], 0):
                    best, bd = n, n["dh"]
            if best is not None:
                self.loot_target = best
                return "npc", None, 1.0, False
        # 4) crates (1 cube, maybe a weapon); hitting them may reset regen, so not while hurt
        if self.t < 360 and hp > 0.7:
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
        return None

    def _near_weapon(self, reach):
        """Goal (x, y) of a remembered weapon within reach if we hold no secondary, else None."""
        if len(self.weap) > 1 and self.weap[1] is not None:
            return None
        x, y, z = self.pos
        held = {w["kind"] for w in self.weap if w}
        best, bd = None, reach
        for k, p in self.known_pk.values():
            if k in held or LOOT_RANK.get(k, 0) == 0 or abs(p[2] - z) > 3.5:
                continue
            d = hyp(p[0] - x, p[1] - y)
            if d < bd:
                best, bd = (p[0], p[1]), d
        return best

    def _inside_live(self, px, py, margin):
        return hyp(px - self.live_c[0], py - self.live_c[1]) < self.live_r - margin

    def _drop_goal(self, obs):
        x, y = self.pos[0], self.pos[1]
        if self.t - self.last_mech_seen_t < 10.0:
            return None
        for d in obs.get("drops") or ():
            p = d["pos"]
            dist = hyp(p[0] - x, p[1] - y)
            holder = d.get("holder")
            if holder is not None and holder != self.me_id:
                continue
            eta = dist / max(1.0, self.walk)
            if dist < 110 and float(d.get("lands_in", 99)) < eta + 6 and self._inside_live(p[0], p[1], 15):
                return (p[0], p[1])
        return None

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
            if time.perf_counter() > self.deadline - 0.010:
                break
        if best is None:
            best = (x + 25 * math.cos(away), y + 25 * math.sin(away))
            if not self._inside_live(best[0], best[1], 10):
                best = self.zone_goal
        self.cover_pt = best
        return best

    def _cover_from_bearing(self, bearing):
        fake = {"pos": [self.pos[0] + 80 * math.cos(bearing), self.pos[1] + 80 * math.sin(bearing), self.pos[2]], "h": 9}
        return self._cover_point(fake)

    # ------------------------------------------------------------------ legs
    def _legs(self, mode, goal, speed, target, obs):
        x, y = self.pos[0], self.pos[1]
        t = self.t
        if mode in ("fight", "npc") or (mode == "crate" and goal is None):
            tg = target if mode == "fight" else self.loot_target
            if tg is None:
                return 0.0, 0.0
            if mode == "crate":
                return 0.0, 0.0
            P = PARAMS
            if mode == "fight" and P["ambush"] and not tg.get("los") and self.d_live_edge > 25.0:
                return 0.0, 0.0  # behind cover: wait silently, pre-aimed where it will reappear
            if t > self.strafe_flip_t:
                self.strafe = -self.strafe
                self.strafe_flip_t = t + self.rng.uniform(0.9, 2.2)
            b = tg["bearing"]
            d = tg["dh"]
            if mode == "npc":
                pref = 55.0
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
            px, py = -math.sin(b) * self.strafe, math.cos(b) * self.strafe
            mx = math.cos(b) * radial + px
            my = math.sin(b) * radial + py
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
        if mode == "hold":
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
                    yaw, pit = self._direct_aim(tp, tv, zoff, 0.08)
                    aim = (yaw, pit)
                    aimed_weapon = "laser"
                    err_y = abs(wrap(ay - yaw))
                    err_p = abs(ap - pit)
                    tol = math.atan2(rad * 0.8, max(d3, 1.0))
                    hold_max = float(ls.get("hold_s", 2)) + full - 0.25
                    if (charge >= full * 0.95 and err_y < tol and err_p < tol * 1.6) or charge >= hold_max:
                        fire[li] = False  # release
                    else:
                        fire[li] = True
                elif float(lw.get("cooldown", 0)) <= 0 and d3 < float(ls.get("range", 280)) * 0.95 and self.mode not in ("zone", "retreat"):
                    fire[li] = True  # start charging

            # mortar: lob when ready and the target is mid range
            if aimed_weapon is None and "mortar" in slots and target.get("kind") != "crate":
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
            if gun is not None and int(gun.get("ammo", 12) or 0) < int(gun.get("stats", {}).get("mag", 12)) * 0.5 \
                    and float(gun.get("reload", 0) or 0) <= 0 and t - self.last_mech_seen_t > 2.0:
                reload = True

        if gun is not None and int(gun.get("ammo", 1) or 0) == 0 and float(gun.get("reload", 0) or 0) <= 0:
            reload = True

        if aim is None:
            aim = self._idle_aim(mvx, mvy)
        yaw, pit = aim
        act["aim"] = [round(wrap(yaw), 4), round(clamp(pit, self.min_pitch, self.max_pitch), 4)]
        if fire[0] or fire[1]:
            act["fire"] = fire
        if reload:
            act["reload"] = True

    def _idle_aim(self, mvx, mvy):
        t = self.t
        P = PARAMS
        calm = self.mode not in ("fight", "retreat", "zone", "evade") and t - self.last_dmg_t > 3.0
        armed = len(self.weap) > 1 and self.weap[1] is not None
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
        cur = LOOT_RANK.get(s1["kind"], 0) if s1 else 0
        best, br = None, cur
        for pk in obs.get("pickups") or ():
            k = pk.get("kind")
            if k in held or k == "gun":
                continue
            p = pk["pos"]
            if hyp(p[0] - x, p[1] - y) > self.pick_r - 0.3 or abs(p[2] - z) > 3.8:
                continue
            r = LOOT_RANK.get(k, 0)
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
            if s > bs:
                best, bs = i, s
        return best


if __name__ == "__main__":
    mf.run(Bot)
