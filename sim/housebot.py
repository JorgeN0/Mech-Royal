"""Stand-in for the practice house bots: one squad that shares vision, never fights itself, and hunts
the hero weakest first (one early, more as the hero thins them out, all of them late).
Skill levels differ in aim noise, lead, reaction time and strafing. Tunable via LEVELS.
"""
import math
import random

from world import AStar

TAU = 2.0 * math.pi
LEVELS = {
    "easy": dict(sigma=0.045, lead=0.0, react=0.8, strafe=False, pref=45.0),
    "medium": dict(sigma=0.028, lead=0.6, react=0.5, strafe=True, pref=55.0),
    "hard": dict(sigma=0.016, lead=1.0, react=0.3, strafe=True, pref=60.0),
    "expert": dict(sigma=0.008, lead=1.0, react=0.15, strafe=True, pref=60.0),
}
ORDER = ["easy"] * 4 + ["medium"] * 4 + ["hard"] * 4 + ["expert"] * 3
SCHEDULE = dict(first_s=12.0, per_kill=1, all_s=300.0)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


class Squad:
    def __init__(self, hero_id, world, seed=0):
        self.hero = hero_id
        self.w = world
        self.members = []  # HouseBot in weakest-first order
        self.hero_seen = None  # (t, pos, vel)
        self.rng = random.Random(seed)
        self.last_alive_t = {}

    def add(self, bot):
        self.members.append(bot)

    def active(self, t):
        alive = [b for b in self.members if t - self.last_alive_t.get(b.id, -1) < 0.5]
        dead = len(self.members) - len(alive)
        if t < SCHEDULE["first_s"]:
            n = 0
        elif t >= SCHEDULE["all_s"]:
            n = len(alive)
        else:
            n = 1 + SCHEDULE["per_kill"] * dead
        return set(b.id for b in alive[:n])


class HouseBot:
    kind = "house"

    def __init__(self, squad, mech_id, level, cfg, seed):
        self.squad = squad
        self.id = mech_id
        self.level = level
        self.name = "house-" + level
        self.p = LEVELS[level]
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.bias = (0.0, 0.0)
        self.bias_t = -1.0
        self.seen_since = None
        self.strafe = 1.0
        self.flip_t = 0.0
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.path_i = 0
        self.spot = None

    def act(self, obs):
        sq = self.squad
        t = obs["t"]
        sq.last_alive_t[self.id] = t
        me = obs["self"]
        x, y, z = me["pos"]
        hero = next((m for m in obs["mechs"] if m["id"] == sq.hero), None)
        visible_now = hero is not None
        if hero is not None:
            sq.hero_seen = (t, hero["pos"], hero["vel"])
        act = {"tick": obs["tick"]}
        assigned = self.id in sq.active(t)
        attacked = any(e.get("type") == "damaged" and e.get("by") == sq.hero for e in obs["events"])
        if attacked:
            self.provoked = t
        provoked = t - getattr(self, "provoked", -99) < 8.0

        # zone first
        zn = obs["zone"]
        nc, nr = zn["next_center"], zn["next_radius"]
        dn = hyp(x - nc[0], y - nc[1])
        safe_r = max(0.0, nr - max(6.0, 0.25 * nr))
        zone_goal = None
        if dn > safe_r:
            if self.spot is None or hyp(self.spot[0] - nc[0], self.spot[1] - nc[1]) > safe_r:
                a = self.rng.random() * TAU
                rr = safe_r * 0.6 * math.sqrt(self.rng.random())
                self.spot = (nc[0] + rr * math.cos(a), nc[1] + rr * math.sin(a))
            zone_goal = self.spot

        engage = hero is not None and (assigned or provoked or hyp(hero["pos"][0] - x, hero["pos"][1] - y) < 70)
        move = (0.0, 0.0)
        aim = None
        fire = False
        if engage:
            hp_ = hero["pos"]
            d = hyp(hp_[0] - x, hp_[1] - y)
            # aim with lead and level noise
            if t - self.bias_t > 0.6:
                s = self.p["sigma"]
                self.bias = (self.rng.gauss(0, s), self.rng.gauss(0, s * 0.6))
                self.bias_t = t
            spd = me["weapons"][0]["stats"]["speed"] if me["weapons"][0] else 252
            tf = d / spd * self.p["lead"]
            tx = hp_[0] + hero["vel"][0] * tf
            ty = hp_[1] + hero["vel"][1] * tf
            tz = hp_[2] + 4.5
            mz = z + 7.0
            yaw = math.atan2(ty - y, tx - x) + self.bias[0]
            pit = math.atan2(tz - mz, hyp(tx - x, ty - y)) + self.bias[1]
            aim = [yaw, pit]
            if visible_now and self.squad.w.los((x, y, z + 8), (hp_[0], hp_[1], hp_[2] + 4.5)):
                if self.seen_since is None:
                    self.seen_since = t
                err = abs(wrap(me["aim"][0] - yaw)) + abs(me["aim"][1] - pit)
                if t - self.seen_since >= self.p["react"] and err < 0.06 and d < 225:
                    fire = True
                # spacing + strafe
                b = math.atan2(hp_[1] - y, hp_[0] - x)
                radial = max(-1.0, min(1.0, (d - self.p["pref"]) / 25.0))
                mx, my = math.cos(b) * radial, math.sin(b) * radial
                if self.p["strafe"]:
                    if t > self.flip_t:
                        self.strafe = -self.strafe
                        self.flip_t = t + self.rng.uniform(1.0, 2.5)
                    mx += -math.sin(b) * self.strafe
                    my += math.cos(b) * self.strafe
                n = hyp(mx, my)
                move = (mx / n, my / n) if n > 1e-6 else (0.0, 0.0)
            else:
                self.seen_since = None
                move = self._nav(x, y, hp_[0], hp_[1], t)
        elif assigned and sq.hero_seen is not None:
            self.seen_since = None
            hp_ = sq.hero_seen[1]
            move = self._nav(x, y, hp_[0], hp_[1], t)
            aim = [math.atan2(hp_[1] - y, hp_[0] - x), 0.0]
        else:
            self.seen_since = None
            if zone_goal is not None:
                move = self._nav(x, y, zone_goal[0], zone_goal[1], t)
        if zone_goal is not None and not engage and not assigned:
            move = self._nav(x, y, zone_goal[0], zone_goal[1], t)
        # never stay outside the live circle
        c = zn["center"]
        if not me["in_zone"]:
            move = self._nav(x, y, nc[0], nc[1], t)
            act["boost"] = me["energy"] > 10
        if aim is None:
            if hyp(*move) > 0.1:
                aim = [math.atan2(move[1], move[0]), 0.0]
            else:
                aim = [wrap(me["aim"][0] + 0.6), 0.0]
        g = me["weapons"][0]
        if g and g.get("ammo", 1) == 0 and g.get("reload", 0) <= 0:
            act["reload"] = True
        act["move"] = [move[0], move[1]]
        act["aim"] = aim
        if fire:
            act["fire"] = [True, False]
        return act

    def _nav(self, x, y, gx, gy, t):
        w = self.squad.w
        dx, dy = gx - x, gy - y
        d = hyp(dx, dy)
        if d < 2.0:
            return (0.0, 0.0)
        if d < 120 and w.walk_line(x, y, gx, gy):
            return (dx / d, dy / d)
        if self.path_goal is None or hyp(self.path_goal[0] - gx, self.path_goal[1] - gy) > 15 or t - self.path_t > 4.0:
            s = AStar(w, x, y, gx, gy, weight=1.6)
            s.step(1e18, lambda: 0.0)
            self.path = s.path if s.state == "found" else None
            self.path_goal = (gx, gy)
            self.path_t = t
            self.path_i = 0
        if not self.path:
            return (dx / d, dy / d)
        p = self.path
        i = self.path_i
        while i < len(p) - 1 and hyp(p[i][0] - x, p[i][1] - y) < 4.0:
            i += 1
        best = i
        for k in range(i + 1, min(len(p), i + 8)):
            if w.walk_line(x, y, p[k][0], p[k][1]):
                best = k
        self.path_i = best
        ex, ey = p[best][0] - x, p[best][1] - y
        e = hyp(ex, ey)
        return (ex / e, ey / e) if e > 0.3 else (dx / d, dy / d)
