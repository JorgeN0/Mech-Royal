"""Local approximation of the mechfight/1 server, for offline testing. Not the real server.

Implements the rules in protocol.md with numbers from hello.config: lockstep 20 Hz ticks with a
decision every 2, movement with walls/roofs/steps on the World grid, gun/mortar/laser/lightning,
rear armour, armour/cube scaling, vision (wedge + proximity + LOS, 5 s memory; canopy ignored),
sound with bearing noise, zone phases, regen, cubes/upgrades, crates, NPC drones, floor loot,
supply drops. Bots are driven in-process through act(obs) -> action dicts, exactly as over stdio.
"""
import math
import random

TAU = 2.0 * math.pi


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def hyp(dx, dy):
    return math.sqrt(dx * dx + dy * dy)


class Mech:
    pass


class Match:
    def __init__(self, cfg, world, controllers, seed=0, mode="ffa", hero=None):
        """controllers: list of 16 objects with .act(obs)->action and .kind ('bot' or 'house')."""
        self.cfg = cfg
        self.w = world
        self.rng = random.Random(seed)
        self.mode = mode
        self.hero = hero
        self.dt = 1.0 / cfg["match"]["tick_hz"]
        self.decide_every = cfg["match"]["decide_every"]
        self.tick = 0
        self.t = 0.0
        self.mc = cfg["mech"]
        self.wc = cfg["weapons"]
        self.cc = cfg["cubes"]
        self.vc = cfg["vision"]
        self.sc = cfg["sound"]
        self.ctrl = controllers
        self.mechs = []
        self.projectiles = []
        self.npcs = []
        self.crates = []
        self.cubes = []
        self.pickups = []
        self.drops = []
        self.next_id = {"proj": 1, "npc": 100, "crate": 1000, "cube": 5000, "pickup": 8000, "drop": 20000}
        self.deaths = []  # (tick, id)
        self.log = []
        self._setup_zone()
        self._spawn()

    # ------------------------------------------------------------------ setup
    def _nid(self, k):
        v = self.next_id[k]
        self.next_id[k] = v + 1
        return v

    def _random_free(self, cx, cy, r, tries=400, min_sep=None, others=()):
        w = self.w
        for _ in range(tries):
            a = self.rng.random() * TAU
            d = r * math.sqrt(self.rng.random())
            x, y = cx + d * math.cos(a), cy + d * math.sin(a)
            if not (w.minx + 20 < x < w.maxx - 20 and w.miny + 20 < y < w.maxy - 20):
                continue
            k = w.cell_of(x, y)
            if w.blocked[k]:
                continue
            if min_sep and any(hyp(x - o[0], y - o[1]) < min_sep for o in others):
                continue
            return x, y
        return None

    def _setup_zone(self):
        z = self.cfg["zone"]
        c = list(z["start_center"])
        r = float(z["start_radius"])
        t = 0.0
        self.zone_phases = []
        for ph in z["phases"]:
            nr = float(ph["radius"])
            off = max(0.0, r - nr)
            a = self.rng.random() * TAU
            d = off * math.sqrt(self.rng.random()) * 0.9
            nc = [c[0] + d * math.cos(a), c[1] + d * math.sin(a)]
            ss = t + ph["hold_s"]
            se = ss + ph["shrink_s"]
            self.zone_phases.append({"c": c, "r": r, "nc": nc, "nr": nr, "ss": ss, "se": se, "dps": ph["dps"], "start": t})
            c, r, t = nc, nr, se
        self.zone_final = {"c": c, "r": r}

    def zone_state(self):
        t = self.t
        for ph in self.zone_phases:
            if t < ph["se"]:
                return ph
        last = self.zone_phases[-1]
        return {"c": self.zone_final["c"], "r": self.zone_final["r"], "nc": self.zone_final["c"], "nr": self.zone_final["r"],
                "ss": last["se"], "se": last["se"], "dps": last["dps"], "start": last["se"]}

    def live_circle(self):
        ph = self.zone_state()
        t = self.t
        if t <= ph["ss"] or ph["se"] <= ph["ss"]:
            return ph["c"], ph["r"], ph["dps"]
        f = min(1.0, (t - ph["ss"]) / (ph["se"] - ph["ss"]))
        c = (ph["c"][0] + (ph["nc"][0] - ph["c"][0]) * f, ph["c"][1] + (ph["nc"][1] - ph["c"][1]) * f)
        return c, ph["r"] + (ph["nr"] - ph["r"]) * f, ph["dps"]

    def _spawn(self):
        cfg = self.cfg
        zc = cfg["zone"]["start_center"]
        zr = cfg["zone"]["start_radius"] * cfg["match"]["spawn_zone_fraction"]
        pts = []
        for i in range(len(self.ctrl)):
            p = None
            sep = cfg["match"]["spawn_min_separation"]
            while p is None:
                p = self._random_free(zc[0], zc[1], zr, min_sep=sep, others=pts)
                sep *= 0.8
            pts.append(p)
        for i, p in enumerate(pts):
            m = Mech()
            m.id = i
            m.ctrl = self.ctrl[i]
            m.pos = [p[0], p[1], self.w.ground_at(p[0], p[1])]
            m.vel = [0.0, 0.0, 0.0]
            yaw = self.rng.random() * TAU - math.pi
            m.aim = [yaw, 0.0]
            m.cmd_aim = [yaw, 0.0]
            m.stats = dict(self.mc)
            m.wstats = {k: dict(v) for k, v in self.wc.items()}
            m.upgrades = []
            m.cubes = 0
            m.max_hp = float(m.stats["hp"])
            m.hp = m.max_hp
            m.energy = float(m.stats["energy"])
            m.last_spend = -99.0
            m.grounded = True
            m.weapons = [self._new_weapon(m, "gun"), None]
            m.alive = True
            m.place = None
            m.kills = 0
            m.last_combat = -99.0
            m.memory = {}
            m.events = []
            m.sounds = []
            m.beams = []
            m.offers = []
            m.offer_id = 0
            m.offer_expires = None
            m.action = {}
            m.shorted_until = -1.0
            m.step_acc = 0.0
            m.death_cause = None
            m.dmg_taken = 0.0
            m.dmg_dealt = 0.0
            m.shots = 0
            m.hits = 0
            self.mechs.append(m)
        # loot
        lc = cfg["loot"]
        kinds = [k for k, v in lc["weights"].items() if v > 0]
        wts = [lc["weights"][k] for k in kinds]
        for _ in range(lc["floor_count"]):
            p = self._random_free(zc[0], zc[1], 520)
            if p:
                self.pickups.append({"id": self._nid("pickup"), "kind": self.rng.choices(kinds, wts)[0],
                                     "pos": [p[0], p[1], self.w.ground_at(*p)], "ammo": None})
        for _ in range(cfg["crates"]["count"]):
            p = self._random_free(zc[0], zc[1], 520)
            if p:
                self.crates.append({"id": self._nid("crate"), "pos": [p[0], p[1], self.w.ground_at(*p)], "hp": float(cfg["crates"]["hp"])})
        nc = cfg["npc"]
        for _ in range(nc["count"]):
            p = self._random_free(zc[0], zc[1], 500)
            if p:
                self.npcs.append({"id": self._nid("npc"), "pos": [p[0], p[1], self.w.ground_at(*p)], "home": (p[0], p[1]),
                                  "hp": float(nc["hp"]), "target": None, "cd": 0.0, "vel": [0.0, 0.0, 0.0], "aim": 0.0,
                                  "wander": (p[0], p[1])})

    def _new_weapon(self, m, kind, ammo=None):
        ws = m.wstats[kind]
        wp = {"kind": kind, "cooldown": 0.0, "lock": 0.0}
        if kind == "gun":
            wp["ammo"] = int(ws["mag"]) if ammo is None else ammo
            wp["reload"] = 0.0
        if kind == "laser":
            wp["charge"] = 0.0
            wp["releasing"] = False
        return wp

    # ------------------------------------------------------------------ geometry
    def size(self, m):
        s = 1.0 + self.cc["size_per_cube"] * m.cubes
        return m.stats["radius"] * s, m.stats["height"] * s

    def body_blocked(self, x, y, z, r, h):
        w = self.w
        su = float(self.mc["step_up"])
        if x - r < w.minx or y - r < w.miny or x + r >= w.maxx or y + r >= w.maxy:
            return True
        bt, bb, rt, rb = w.bt, w.bb, w.rt, w.rb
        for px, py in ((x, y), (x + r, y), (x - r, y), (x, y + r), (x, y - r)):
            c = w.cell_of(px, py)
            if bb[c] < z + h - 0.1 and bt[c] > z + su:
                return True
            if rb[c] < z + h - 0.1 and rt[c] > z + su:
                return True
        if w.ground_at(x, y) > z + su:
            return True
        return False

    def surface(self, x, y, z):
        w = self.w
        su = float(self.mc["step_up"])
        s = w.ground_at(x, y)
        c = w.cell_of(x, y)
        if w.bt[c] > -1e8 and z >= w.bt[c] - su - 0.01:
            s = max(s, w.bt[c])
        if w.rt[c] > -1e8 and z >= w.rt[c] - su - 0.01:
            s = max(s, w.rt[c])
        return s

    def seg_world_hit(self, a, b):
        """Fraction along a->b where the segment first enters terrain/building/bridge, or None."""
        w = self.w
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        L = math.sqrt(dx * dx + dy * dy + dz * dz)
        n = max(1, int(L / 1.0))
        for k in range(1, n + 1):
            f = k / n
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            if x < w.minx or y < w.miny or x >= w.maxx or y >= w.maxy:
                return f
            c = w.cell_of(x, y)
            if z < w.ground[c] - 0.3 or (w.bb[c] <= z <= w.bt[c]) or (w.rb[c] <= z <= w.rt[c]):
                return f
        return None

    @staticmethod
    def seg_cyl(a, b, cx, cy, z0, z1, r):
        """Fraction where segment a->b first hits a vertical cylinder, or None (approximate: closest approach)."""
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        if L2 < 1e-9:
            s = 0.0
        else:
            s = ((cx - a[0]) * dx + (cy - a[1]) * dy) / L2
            s = min(1.0, max(0.0, s))
        px, py = a[0] + dx * s, a[1] + dy * s
        d = hyp(px - cx, py - cy)
        if d > r:
            return None
        # back up to the entry point
        if L2 > 1e-9:
            back = math.sqrt(max(0.0, r * r - d * d)) / math.sqrt(L2)
            s = max(0.0, s - back)
        z = a[2] + (b[2] - a[2]) * s
        if z0 <= z <= z1:
            return s
        return None

    # ------------------------------------------------------------------ main loop
    def alive_mechs(self):
        return [m for m in self.mechs if m.alive]

    def run(self, max_t=None):
        max_t = max_t or self.cfg["match"]["duration_s"]
        while True:
            alive = self.alive_mechs()
            if len(alive) <= 1 or self.t >= max_t:
                break
            if self.mode == "practice" and not self.mechs[self.hero].alive:
                break
            if self.tick % self.decide_every == 0:
                self._decide()
            self._step()
        # survivors ranked by hp
        surv = sorted(self.alive_mechs(), key=lambda m: -m.hp)
        for i, m in enumerate(surv):
            m.place = i + 1
        return self.results()

    def results(self):
        return [{"id": m.id, "name": getattr(m.ctrl, "name", "?"), "kind": m.ctrl.kind, "place": m.place,
                 "score": 16 - (m.place or 16), "kills": m.kills, "cubes": m.cubes, "cause": m.death_cause,
                 "t_death": getattr(m, "t_death", None), "shots": m.shots, "hits": m.hits,
                 "weap": "+".join(wp["kind"] for wp in m.weapons if wp and wp["kind"] != "gun") or "-",
                 "took_t": getattr(m, "took_t", None), "drops": getattr(m, "drops_got", 0),
                 "dmg_dealt": round(m.dmg_dealt), "dmg_taken": round(m.dmg_taken)} for m in self.mechs]

    def _decide(self):
        vis = self._vision()
        for m in self.mechs:
            if not m.alive:
                continue
            obs = self._obs(m, vis)
            try:
                act = m.ctrl.act(obs) or {}
            except Exception as e:  # mirror the wrapper: keep the continuous parts
                if not getattr(m.ctrl, "err_logged", False):
                    m.ctrl.err_logged = True
                    self.log.append("bot %d act error: %r" % (m.id, e))
                act = {k: v for k, v in m.action.items() if k in ("move", "aim", "boost")}
            if act.get("tick") is not None and act["tick"] != obs["tick"]:
                act = {k: v for k, v in m.action.items() if k in ("move", "aim", "boost")}
            m.action = act
            m.first_tick = True
            self._one_shots(m, act)
            m.events = []
            m.sounds = []
            m.beams = []

    # ------------------------------------------------------------------ vision
    def can_see(self, o, tx, ty, tz_c, tz_top):
        x, y, z = o.pos
        dx, dy = tx - x, ty - y
        d = hyp(dx, dy)
        st = o.stats
        near = self.vc["near_range"] + st.get("near_range_bonus", 0)
        rng = self.vc["range"] + st.get("vision_bonus", 0)
        if d > near:
            if d > rng:
                return False
            if abs(wrap(math.atan2(dy, dx) - o.aim[0])) > math.radians(self.vc["fov_deg"]) * 0.5:
                return False
        eye = (x, y, z + st["eye_height"] * (1 + self.cc["size_per_cube"] * o.cubes))
        trees = getattr(self.w, "trees", None) if self.vc.get("canopy_blocks", True) else None
        for tgt in ((tx, ty, tz_c), (tx, ty, tz_top)):
            if self.w.los(eye, tgt) and (trees is None or not trees.blocks(eye, tgt)):
                return True
        return False

    def _vision(self):
        """vis[i] = set of mech/npc ids mech i sees now."""
        vis = {}
        alive = self.alive_mechs()
        for o in alive:
            s = set()
            light = getattr(o.ctrl, "light_obs", False)
            for m in alive:
                if m is o:
                    continue
                if light and self.mode == "practice" and m.id != self.hero:
                    continue  # the house squad only looks for the hero
                if abs(m.pos[0] - o.pos[0]) > 260 or abs(m.pos[1] - o.pos[1]) > 260:
                    continue
                _, h = self.size(m)
                if self.can_see(o, m.pos[0], m.pos[1], m.pos[2] + h * 0.5, m.pos[2] + h):
                    s.add(m.id)
            npc_r = 60 if light else 210  # stand-ins only need drones close enough to be shooting them
            for n in self.npcs:
                if abs(n["pos"][0] - o.pos[0]) > npc_r or abs(n["pos"][1] - o.pos[1]) > npc_r:
                    continue
                h = self.cfg["npc"]["height"]
                if self.can_see(o, n["pos"][0], n["pos"][1], n["pos"][2] + h * 0.5, n["pos"][2] + h):
                    s.add(n["id"])
            vis[o.id] = s
            for i in s:
                o.memory[i] = self.t
        return vis

    def _visible_pt(self, o, p, hgt=1.0):
        if abs(p[0] - o.pos[0]) > 205 or abs(p[1] - o.pos[1]) > 205:
            return False
        return self.can_see(o, p[0], p[1], p[2] + hgt * 0.5, p[2] + hgt)

    # ------------------------------------------------------------------ observations
    def _obs(self, m, vis):
        mem_s = self.vc["memory_s"]
        mechs = []
        for o in self.mechs:
            if o is m or not o.alive:
                continue
            if o.id in vis[m.id] or self.t - m.memory.get(o.id, -99) <= mem_s:
                e = {"id": o.id, "kind": "mech", "pos": [round(v, 2) for v in o.pos], "vel": [round(v, 2) for v in o.vel],
                     "aim": [round(o.aim[0], 3), round(o.aim[1], 3)], "cubes": o.cubes,
                     "weapons": [wp["kind"] for wp in o.weapons if wp],
                     "charging": bool(o.weapons[1] and o.weapons[1]["kind"] == "laser" and o.weapons[1]["charge"] > 0)
                     or bool(o.weapons[0] and o.weapons[0]["kind"] == "laser" and o.weapons[0]["charge"] > 0)}
                if self.vc.get("show_enemy_hp", True):
                    e["hp"] = round(o.hp)
                    e["max_hp"] = round(o.max_hp)
                mechs.append(e)
        for n in self.npcs:
            if n["id"] in vis[m.id] or self.t - m.memory.get(n["id"], -99) <= mem_s:
                mechs.append({"id": n["id"], "kind": "npc", "pos": [round(v, 2) for v in n["pos"]], "vel": list(n["vel"]),
                              "aim": [n["aim"], 0.0], "hp": round(n["hp"]), "max_hp": self.cfg["npc"]["hp"]})
        projs, crates, cubes, picks = [], [], [], []
        if not getattr(m.ctrl, "light_obs", False):  # stand-in bots never look at loot or projectiles
            for p in self.projectiles:
                if p["owner"] == m.id:
                    continue
                if self._visible_pt(m, p["pos"], 0.2):
                    projs.append({"id": p["id"], "kind": p["kind"], "pos": [round(v, 2) for v in p["pos"]],
                                  "vel": [round(v, 2) for v in p["vel"]]})
            crates = [{"id": c["id"], "pos": c["pos"], "hp": round(c["hp"])} for c in self.crates if self._visible_pt(m, c["pos"], 2.4)]
            cubes = [{"id": c["id"], "pos": c["pos"]} for c in self.cubes if self._visible_pt(m, c["pos"], 0.6)]
            picks = [{"id": p["id"], "kind": p["kind"], "pos": p["pos"]} for p in self.pickups if self._visible_pt(m, p["pos"], 0.8)]
        ph = self.zone_state()
        wl = []
        for wp in m.weapons:
            if wp is None:
                wl.append(None)
                continue
            d = {"kind": wp["kind"], "cooldown": round(max(wp["cooldown"], wp["lock"]), 3), "stats": dict(m.wstats[wp["kind"]])}
            if wp["kind"] == "gun":
                d["ammo"] = wp["ammo"]
                d["reload"] = round(wp["reload"], 3)
            if wp["kind"] == "laser":
                d["charge"] = round(wp["charge"], 3)
            wl.append(d)
        sz = 1.0 + self.cc["size_per_cube"] * m.cubes
        st = dict(m.stats)
        for k in ("radius", "height", "eye_height", "muzzle_height"):
            st[k] = m.stats[k] * sz
        c, r, _ = self.live_circle()
        offer = None
        if m.offers:
            offer = {"id": m.offer_id, "expires": m.offer_expires, "choices": m.offers[0]}
        obs = {
            "type": "obs", "tick": self.tick, "t": round(self.t, 2), "alive": len(self.alive_mechs()),
            "self": {"id": m.id, "pos": [round(v, 3) for v in m.pos], "vel": [round(v, 3) for v in m.vel],
                     "aim": [round(m.aim[0], 4), round(m.aim[1], 4)], "hp": round(m.hp, 1), "max_hp": round(m.max_hp, 1),
                     "energy": round(m.energy, 1), "max_energy": m.stats["energy"], "grounded": m.grounded,
                     "cubes": m.cubes, "upgrades": list(m.upgrades), "in_zone": hyp(m.pos[0] - c[0], m.pos[1] - c[1]) <= r,
                     "weapons": wl, "stats": st},
            "mechs": mechs, "projectiles": projs, "crates": crates, "cubes": cubes, "pickups": picks,
            "sounds": m.sounds, "beams": m.beams, "events": m.events,
            "drops": [{"id": d["id"], "pos": d["pos"], "radius": self.cfg["drops"]["radius"],
                       "lands_in": round(max(0.0, d["land_t"] - self.t), 2), "capture_s": self.cfg["drops"]["capture_s"],
                       "progress": round(d["progress"], 2), "holder": d["holder"]} for d in self.drops],
            "zone": {"center": [round(ph["c"][0], 2), round(ph["c"][1], 2)], "radius": ph["r"],
                     "next_center": [round(ph["nc"][0], 2), round(ph["nc"][1], 2)], "next_radius": ph["nr"],
                     "shrink_start": ph["ss"], "shrink_end": ph["se"], "dps": ph["dps"]},
            "offer": offer,
        }
        return obs

    # ------------------------------------------------------------------ actions
    def _one_shots(self, m, act):
        # upgrade pick
        if m.offers and act.get("pick") is not None:
            try:
                i = int(act["pick"])
                if 0 <= i < len(m.offers[0]):
                    self._apply_upgrade(m, m.offers[0][i]["id"])
            except (TypeError, ValueError):
                pass
        # drop / take
        take = act.get("take")
        drop = act.get("drop")
        if take is not None:
            self._take(m, take, drop)
        elif drop in (0, 1):
            wp = m.weapons[drop]
            if wp is not None and not (wp["kind"] == "laser" and wp["charge"] > 0):
                self._drop_weapon(m, drop)
        if act.get("reload"):
            g = self._weapon(m, "gun")
            if g and g["reload"] <= 0 and g["ammo"] < m.wstats["gun"]["mag"]:
                g["reload"] = m.wstats["gun"]["reload"]
                self._sound("reload", m.pos, m.id)
        if act.get("jump") and m.grounded:
            m.vel[2] = float(m.stats["jump_speed"])
            m.grounded = False
        a = act.get("aim")
        if isinstance(a, (list, tuple)) and len(a) == 2:
            m.cmd_aim = [float(a[0]), max(m.stats["min_pitch"], min(m.stats["max_pitch"], float(a[1])))]

    def _weapon(self, m, kind):
        for wp in m.weapons:
            if wp and wp["kind"] == kind:
                return wp
        return None

    def _take(self, m, pid, drop):
        pk = next((p for p in self.pickups if p["id"] == pid), None)
        if pk is None:
            m.events.append({"type": "take_failed", "id": pid, "reason": "gone"})
            return
        if hyp(pk["pos"][0] - m.pos[0], pk["pos"][1] - m.pos[1]) > self.cfg["loot"]["pickup_radius"] or abs(pk["pos"][2] - m.pos[2]) > 4:
            m.events.append({"type": "take_failed", "id": pid, "reason": "range"})
            return
        if self._weapon(m, pk["kind"]):
            m.events.append({"type": "take_failed", "id": pid, "reason": "have"})
            return
        slot = None
        if drop in (0, 1):
            old = m.weapons[drop]
            if old is not None and old["kind"] == "laser" and old["charge"] > 0:
                m.events.append({"type": "take_failed", "id": pid, "reason": "busy"})
                return
            if old is not None:
                self._drop_weapon(m, drop)
            slot = drop
        else:
            for i in (0, 1):
                if m.weapons[i] is None:
                    slot = i
                    break
        if slot is None:
            m.events.append({"type": "take_failed", "id": pid, "reason": "full"})
            return
        self.pickups.remove(pk)
        wp = self._new_weapon(m, pk["kind"], pk.get("ammo"))
        wp["lock"] = max(self.cfg["loot"]["equip_s"], m.wstats[pk["kind"]]["cooldown"])
        m.weapons[slot] = wp
        if getattr(m, "took_t", None) is None:
            m.took_t = round(self.t, 1)
        m.events.append({"type": "took", "id": pid, "kind": pk["kind"], "slot": slot})

    def _drop_weapon(self, m, slot):
        wp = m.weapons[slot]
        pid = self._nid("pickup")
        self.pickups.append({"id": pid, "kind": wp["kind"], "pos": list(m.pos), "ammo": wp.get("ammo")})
        m.weapons[slot] = None
        m.events.append({"type": "dropped", "id": pid, "kind": wp["kind"], "slot": slot})

    def _apply_upgrade(self, m, uid):
        up = next((u for u in self.cfg["upgrades"] if u["id"] == uid), None)
        if up is None:
            return
        for ef in up["effects"]:
            parts = ef["path"].split(".")
            if parts[0] == "mech":
                d, k = m.stats, parts[1]
            else:
                d, k = m.wstats[parts[1]], parts[2]
            if "mul" in ef:
                d[k] = d[k] * ef["mul"]
            if "add" in ef:
                d[k] = d[k] + ef["add"]
        m.upgrades.append(uid)
        m.events.append({"type": "upgrade", "id": uid})
        self._recalc_hp(m)
        m.offers.pop(0)
        m.offer_expires = self.t + self.cfg["bot"]["offer_timeout_s"] if m.offers else None
        m.offer_id += 1

    def _recalc_hp(self, m):
        new = m.stats["hp"] * (1 + self.cc["hp_per_cube"] * min(m.cubes, self.cc["stat_cap"]))
        if new > m.max_hp:
            m.hp += new - m.max_hp
        m.max_hp = new
        m.hp = min(m.hp, m.max_hp)

    def _give_cube(self, m, n=1):
        for _ in range(n):
            m.cubes += 1
            self._recalc_hp(m)
            if m.cubes in self.cc["upgrade_thresholds"]:
                held = {wp["kind"] for wp in m.weapons if wp}
                avail = [u for u in self.cfg["upgrades"]
                         if m.upgrades.count(u["id"]) < u.get("max", 1) and (u.get("requires") is None or u["requires"] in held)]
                if len(avail) >= 1:
                    ch = self.rng.sample(avail, min(3, len(avail)))
                    m.offers.append([{"id": u["id"], "name": u["name"], "desc": u["desc"]} for u in ch])
                    if m.offer_expires is None:
                        m.offer_expires = self.t + self.cfg["bot"]["offer_timeout_s"]

    # ------------------------------------------------------------------ sound
    def _sound(self, kind, pos, src_id):
        rng = self.sc["range"].get(kind)
        if rng is None:
            return
        for m in self.mechs:
            if not m.alive or m.id == src_id:
                continue
            d = hyp(pos[0] - m.pos[0], pos[1] - m.pos[1])
            if d > rng:
                continue
            noise = math.radians(self.sc["bearing_noise_deg"]) * m.stats.get("hearing", 1)
            b = wrap(math.atan2(pos[1] - m.pos[1], pos[0] - m.pos[0]) + self.rng.gauss(0, noise))
            dc = "near" if d < self.sc["near"] else "mid" if d < self.sc["mid"] else "far"
            m.sounds.append({"kind": kind, "bearing": round(b, 3), "dist": dc, "age": 0.0})

    # ------------------------------------------------------------------ simulation step
    def _step(self):
        dt = self.dt
        for m in self.mechs:
            if m.alive:
                self._move(m, dt)
        self._separate()
        for m in self.mechs:
            if m.alive:
                self._weapons_tick(m, dt)
        self._projectiles(dt)
        self._npcs_tick(dt)
        self._world_tick(dt)
        self._resolve_deaths()
        for m in self.mechs:
            m.first_tick = False
        self.tick += 1
        self.t = self.tick * dt

    def _move(self, m, dt):
        st = m.stats
        act = m.action
        # torso slew
        tr = st["turn_rate"] * dt
        pr = st["pitch_rate"] * dt
        dy = wrap(m.cmd_aim[0] - m.aim[0])
        m.aim[0] = wrap(m.aim[0] + max(-tr, min(tr, dy)))
        dp = m.cmd_aim[1] - m.aim[1]
        m.aim[1] += max(-pr, min(pr, dp))
        # legs
        mv = act.get("move") or [0, 0]
        try:
            mx, my = float(mv[0]), float(mv[1])
        except (TypeError, ValueError, IndexError):
            mx, my = 0.0, 0.0
        n = hyp(mx, my)
        if n > 1:
            mx, my = mx / n, my / n
            n = 1.0
        boost = bool(act.get("boost"))
        charging = any(wp and wp["kind"] == "laser" and wp["charge"] > 0 for wp in m.weapons)
        spend = False
        if m.grounded:
            spd = st["walk_speed"]
            if boost and n > 0.05 and m.energy > 0:
                spd = st["boost_speed"]
                m.energy -= st["boost_drain"] * dt
                spend = True
            if charging:
                spd *= st["charge_move_mult"]
            acc = st["ground_accel"]
        else:
            spd = st["walk_speed"] * (st["charge_move_mult"] if charging else 1)
            acc = st["air_accel"]
            if boost and m.energy > 0:
                m.vel[2] = min(st["max_rise_speed"], m.vel[2] + st["jet_accel"] * dt)
                m.energy -= st["jet_drain"] * dt
                spend = True
        tvx, tvy = mx * spd, my * spd
        dvx, dvy = tvx - m.vel[0], tvy - m.vel[1]
        dl = hyp(dvx, dvy)
        mxd = acc * dt
        if dl > mxd:
            dvx, dvy = dvx / dl * mxd, dvy / dl * mxd
        m.vel[0] += dvx
        m.vel[1] += dvy
        if spend:
            m.last_spend = self.t
            m.energy = max(0.0, m.energy)
        elif self.t - m.last_spend >= st["energy_regen_delay"]:
            m.energy = min(st["energy"], m.energy + st["energy_regen"] * dt)
        # horizontal with wall sliding
        r, h = self.size(m)
        x, y, z = m.pos
        nx, ny = x + m.vel[0] * dt, y + m.vel[1] * dt
        if self.body_blocked(nx, ny, z, r, h):
            if not self.body_blocked(nx, y, z, r, h):
                ny = y
                m.vel[1] = 0.0
            elif not self.body_blocked(x, ny, z, r, h):
                nx = x
                m.vel[0] = 0.0
            else:
                nx, ny = x, y
                m.vel[0] = m.vel[1] = 0.0
        # vertical
        surf = self.surface(nx, ny, z)
        if m.grounded:
            if surf >= z - 1.0:
                z = surf
                m.vel[2] = 0.0
            else:
                m.grounded = False
        if not m.grounded:
            m.vel[2] -= st["gravity"] * dt
            z += m.vel[2] * dt
            if z <= surf:
                z = surf
                m.vel[2] = 0.0
                m.grounded = True
        m.pos = [nx, ny, z]
        # footsteps
        hs = hyp(m.vel[0], m.vel[1])
        if m.grounded and hs > self.sc["step_speed"]:
            m.step_acc += dt
            if m.step_acc >= self.sc["step_every"]:
                m.step_acc = 0.0
                self._sound("boost" if (boost and m.energy > 0) else "step", m.pos, m.id)
        elif not m.grounded and boost and m.energy > 0:
            m.step_acc += dt
            if m.step_acc >= self.sc["step_every"]:
                m.step_acc = 0.0
                self._sound("jet", m.pos, m.id)

    def _separate(self):
        al = self.alive_mechs()
        for i in range(len(al)):
            a = al[i]
            ra, _ = self.size(a)
            for j in range(i + 1, len(al)):
                b = al[j]
                rb, _ = self.size(b)
                dx, dy = b.pos[0] - a.pos[0], b.pos[1] - a.pos[1]
                d = hyp(dx, dy)
                lim = ra + rb
                if d < lim and abs(a.pos[2] - b.pos[2]) < 9:
                    if d < 1e-6:
                        dx, dy, d = 1.0, 0.0, 1.0
                    push = (lim - d) * 0.5
                    ux, uy = dx / d, dy / d
                    for mm, s in ((a, -1), (b, 1)):
                        r, h = self.size(mm)
                        nx, ny = mm.pos[0] + s * ux * push, mm.pos[1] + s * uy * push
                        if not self.body_blocked(nx, ny, mm.pos[2], r, h):
                            mm.pos[0], mm.pos[1] = nx, ny

    # ------------------------------------------------------------------ weapons
    def _muzzle(self, m):
        sz = 1 + self.cc["size_per_cube"] * m.cubes
        return (m.pos[0], m.pos[1], m.pos[2] + m.stats["muzzle_height"] * sz)

    def _dir(self, yaw, pitch):
        cp = math.cos(pitch)
        return (cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch))

    def _dmg_mult(self, m):
        return 1 + self.cc["damage_per_cube"] * min(m.cubes, self.cc["stat_cap"])

    def _weapons_tick(self, m, dt):
        fire = m.action.get("fire") or [False, False]
        for i, wp in enumerate(m.weapons):
            if wp is None:
                continue
            wp["cooldown"] = max(0.0, wp["cooldown"] - dt)
            wp["lock"] = max(0.0, wp["lock"] - dt)
            held = bool(fire[i]) if i < len(fire) else False
            kind = wp["kind"]
            ws = m.wstats[kind]
            if kind == "gun":
                if wp["reload"] > 0:
                    wp["reload"] -= dt
                    if wp["reload"] <= 0:
                        wp["reload"] = 0.0
                        wp["ammo"] = int(ws["mag"])
                if held and wp["cooldown"] <= 0 and wp["lock"] <= 0 and wp["ammo"] > 0 and wp["reload"] <= 0:
                    self._fire_gun(m, ws)
                    wp["ammo"] -= 1
                    wp["cooldown"] = ws["cooldown"]
                    if wp["ammo"] <= 0:
                        wp["reload"] = ws["reload"]
            elif kind == "mortar":
                if held and wp["cooldown"] <= 0 and wp["lock"] <= 0:
                    self._fire_mortar(m, ws)
                    wp["cooldown"] = ws["cooldown"]
            elif kind == "lightning":
                if held and wp["cooldown"] <= 0 and wp["lock"] <= 0 and m.energy >= ws["energy"]:
                    self._fire_lightning(m, ws)
                    m.energy -= ws["energy"]
                    m.last_spend = self.t
                    wp["cooldown"] = ws["cooldown"]
            elif kind == "laser":
                can = wp["cooldown"] <= 0 and wp["lock"] <= 0 and self.t >= m.shorted_until
                if wp["charge"] > 0:
                    wp["charge"] += dt
                    full = ws["charge"]
                    if wp["charge"] >= full + ws["hold_s"] or ((not held or wp["releasing"]) and wp["charge"] >= ws["min_charge"]):
                        self._fire_laser(m, ws, wp["charge"])
                        wp["charge"] = 0.0
                        wp["releasing"] = False
                        wp["cooldown"] = ws["cooldown"]
                    elif not held:
                        wp["releasing"] = True
                elif held and can:
                    wp["charge"] = dt
                    self._sound("laser_charge", m.pos, m.id)

    def _fire_gun(self, m, ws):
        sp = math.radians(ws["spread_deg"])
        yaw = m.aim[0] + self.rng.gauss(0, sp)
        pit = m.aim[1] + self.rng.gauss(0, sp)
        d = self._dir(yaw, pit)
        mz = self._muzzle(m)
        r, _ = self.size(m)
        off = r + 0.5
        start = (mz[0] + d[0] * off, mz[1] + d[1] * off, mz[2] + d[2] * off)
        if self.seg_world_hit(mz, start) is not None:
            return
        self.projectiles.append({"id": self._nid("proj"), "kind": "gun", "owner": m.id, "pos": list(start),
                                 "vel": [d[0] * ws["speed"], d[1] * ws["speed"], d[2] * ws["speed"]],
                                 "ttl": ws["range"] / ws["speed"], "dmg": ws["damage"] * self._dmg_mult(m), "g": 0.0,
                                 "radius": self.cfg["projectile"]["gun_radius"], "origin": list(mz)})
        m.shots += 1
        self._sound("gun", m.pos, m.id)

    def _fire_mortar(self, m, ws):
        d = self._dir(m.aim[0], m.aim[1])
        _, h = self.size(m)
        r, _ = self.size(m)
        tube = (m.pos[0], m.pos[1], m.pos[2] + h + 0.5)
        start = (tube[0] + d[0] * (r + 0.5), tube[1] + d[1] * (r + 0.5), tube[2] + d[2] * (r + 0.5))
        self.projectiles.append({"id": self._nid("proj"), "kind": "mortar", "owner": m.id, "pos": list(start),
                                 "vel": [d[0] * ws["speed"], d[1] * ws["speed"], d[2] * ws["speed"]],
                                 "ttl": ws.get("ttl", 7), "dmg": ws["damage"] * self._dmg_mult(m), "g": ws["gravity"],
                                 "radius": 0.4, "origin": list(tube), "ws": ws})
        m.shots += 1
        self._sound("mortar", m.pos, m.id)

    def _targets(self, exclude):
        """All damageable things: (kind, obj, cx, cy, z0, z1, r). Cached per tick (positions change only in _move)."""
        c = getattr(self, "_tg_cache", None)
        if c is None or c[0] != self.tick:
            c = (self.tick, self._targets_all())
            self._tg_cache = c
        if exclude is None or exclude < 0:
            return [tg for tg in c[1] if tg[0] != "mech" or tg[1].alive]
        return [tg for tg in c[1] if tg[0] != "mech" or (tg[1].alive and tg[1].id != exclude)]

    def _targets_all(self):
        out = []
        for o in self.mechs:
            if o.alive:
                r, h = self.size(o)
                out.append(("mech", o, o.pos[0], o.pos[1], o.pos[2], o.pos[2] + h, r))
        nc = self.cfg["npc"]
        for n in self.npcs:
            out.append(("npc", n, n["pos"][0], n["pos"][1], n["pos"][2], n["pos"][2] + nc["height"], nc["radius"]))
        cc = self.cfg["crates"]
        for c in self.crates:
            out.append(("crate", c, c["pos"][0], c["pos"][1], c["pos"][2], c["pos"][2] + cc["height"], cc["radius"]))
        return out

    def _fire_laser(self, m, ws, charge):
        full = ws["charge"]
        frac = ws["min_frac"] + (1 - ws["min_frac"]) * min(1.0, charge / full)
        dmg = ws["damage"] * frac * self._dmg_mult(m)
        d = self._dir(m.aim[0], m.aim[1])
        mz = self._muzzle(m)
        end = (mz[0] + d[0] * ws["range"], mz[1] + d[1] * ws["range"], mz[2] + d[2] * ws["range"])
        wf = self.seg_world_hit(mz, end)
        lim = wf if wf is not None else 1.0
        best = None
        for tg in self._targets(m.id):
            s = self.seg_cyl(mz, end, tg[2], tg[3], tg[4], tg[5], tg[6])
            if s is not None and s <= lim and (best is None or s < best[0]):
                best = (s, tg)
        f = best[0] if best else lim
        path = [list(mz), [mz[0] + (end[0] - mz[0]) * f, mz[1] + (end[1] - mz[1]) * f, mz[2] + (end[2] - mz[2]) * f]]
        self._beam(m, "laser", path)
        self._sound("laser", m.pos, m.id)
        m.shots += 1
        if best:
            kind, obj = best[1][0], best[1][1]
            if kind == "mech" and self._weapon(obj, "mortar"):
                dmg *= 1 + ws.get("cookoff", 0)
                obj.events.append({"type": "effect", "effect": "cookoff", "by": m.id})
            self._damage(obj, kind, dmg, "laser", m, mz)

    def _fire_lightning(self, m, ws):
        mz = self._muzzle(m)
        cone = math.radians(ws["cone_deg"]) * 0.5
        best = None
        for tg in self._targets(m.id):
            dx, dy = tg[2] - m.pos[0], tg[3] - m.pos[1]
            d = hyp(dx, dy)
            if d > ws["range"] or abs(wrap(math.atan2(dy, dx) - m.aim[0])) > cone:
                continue
            if not self.w.los(mz, (tg[2], tg[3], (tg[4] + tg[5]) * 0.5)):
                continue
            if best is None or d < best[0]:
                best = (d, tg)
        m.shots += 1
        self._sound("lightning", m.pos, m.id)
        if best is None:
            d = self._dir(m.aim[0], 0)
            self._beam(m, "lightning", [list(mz), [mz[0] + d[0] * ws["range"], mz[1] + d[1] * ws["range"], mz[2]]])
            return
        hit = [best[1]]
        path = [list(mz), [best[1][2], best[1][3], (best[1][4] + best[1][5]) * 0.5]]
        dmg = ws["damage"] * self._dmg_mult(m)
        src = mz
        for k in range(int(ws["chain"]) + 1):
            tg = hit[-1]
            self._damage(tg[1], tg[0], dmg * (ws["chain_falloff"] ** k), "lightning", m, src)
            if tg[0] == "mech":
                for wp in tg[1].weapons:
                    if wp and wp["kind"] == "laser":
                        wp["charge"] = 0.0
                        tg[1].shorted_until = self.t + ws["short_s"]
                        tg[1].events.append({"type": "effect", "effect": "short", "by": m.id})
            if k == int(ws["chain"]):
                break
            src = (tg[2], tg[3], (tg[4] + tg[5]) * 0.5)
            nxt = None
            for t2 in self._targets(m.id):
                if any(t2[1] is h[1] for h in hit):
                    continue
                d = hyp(t2[2] - tg[2], t2[3] - tg[3])
                if d <= ws["chain_range"] and (nxt is None or d < nxt[0]):
                    nxt = (d, t2)
            if nxt is None:
                break
            hit.append(nxt[1])
            path.append([nxt[1][2], nxt[1][3], (nxt[1][4] + nxt[1][5]) * 0.5])
        self._beam(m, "lightning", path)

    def _beam(self, m, kind, path):
        for o in self.mechs:
            if o.alive and o is not m and (hyp(o.pos[0] - path[0][0], o.pos[1] - path[0][1]) < 200
                                           or hyp(o.pos[0] - path[-1][0], o.pos[1] - path[-1][1]) < 200):
                o.beams.append({"by": m.id, "kind": kind, "path": [[round(v, 2) for v in p] for p in path], "age": 0.0})

    def _projectiles(self, dt):
        keep = []
        for p in self.projectiles:
            a = p["pos"]
            v = p["vel"]
            v[2] -= p["g"] * dt
            b = [a[0] + v[0] * dt, a[1] + v[1] * dt, a[2] + v[2] * dt]
            wf = self.seg_world_hit(a, b)
            lim = wf if wf is not None else 1.0
            best = None
            for tg in self._targets(p["owner"] if p["kind"] != "npc" else -1):
                if p["kind"] == "npc" and tg[0] != "mech":
                    continue
                s = self.seg_cyl(a, b, tg[2], tg[3], tg[4], tg[5], tg[6] + p["radius"])
                if s is not None and s <= lim and (best is None or s < best[0]):
                    best = (s, tg)
            p["ttl"] -= dt
            hitpt = None
            if best:
                f = best[0]
                hitpt = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]
            elif wf is not None:
                hitpt = [a[0] + (b[0] - a[0]) * wf, a[1] + (b[1] - a[1]) * wf, a[2] + (b[2] - a[2]) * wf]
            if hitpt is None and p["ttl"] > 0:
                p["pos"] = b
                keep.append(p)
                continue
            owner = self.mechs[p["owner"]] if 0 <= p["owner"] < len(self.mechs) else None
            if p["kind"] == "mortar":
                self._explode(p, hitpt or b, best[1] if best else None, owner)
            elif best:
                src = (a[0] - v[0] * 0.2, a[1] - v[1] * 0.2, a[2])
                if p["kind"] == "npc":
                    self._damage(best[1][1], best[1][0], p["dmg"], "npc", None, src, npc_id=p["owner_npc"])
                else:
                    self._damage(best[1][1], best[1][0], p["dmg"], "gun", owner, src)
        self.projectiles = keep

    def _explode(self, p, pt, direct, owner):
        ws = p["ws"]
        self._sound("explosion", pt, -1)
        R = ws["splash_radius"]
        mult = p["dmg"] / ws["damage"]
        if direct is not None:
            self._damage(direct[1], direct[0], p["dmg"], "mortar", owner, pt)
        for tg in self._targets(-1):
            if direct is not None and tg[1] is direct[1]:
                continue
            cz = (tg[4] + tg[5]) * 0.5
            d = math.sqrt((tg[2] - pt[0]) ** 2 + (tg[3] - pt[1]) ** 2 + (cz - pt[2]) ** 2) - tg[6]
            if d > R:
                continue
            d = max(0.0, d)
            fall = 1 - (1 - ws["splash_edge"]) * d / R
            if not self.w.los((pt[0], pt[1], pt[2] + 0.5), (tg[2], tg[3], cz)):
                continue
            dmg = ws["splash"] * fall * mult
            if tg[0] == "mech":
                o = tg[1]
                if owner is not None and o is owner:
                    dmg *= self.mc["self_damage_mult"]
                dx, dy = tg[2] - pt[0], tg[3] - pt[1]
                dl = max(1e-6, hyp(dx, dy))
                o.vel[0] += dx / dl * ws["knock"] * fall
                o.vel[1] += dy / dl * ws["knock"] * fall
                o.vel[2] += ws["knock_up"] * fall
                o.grounded = False
                o.energy = max(0.0, o.energy - ws["drain"] * fall)
                o.events.append({"type": "effect", "effect": "concuss", "by": owner.id if owner else None})
            self._damage(tg[1], tg[0], dmg, "mortar", owner, pt)

    def _damage(self, obj, kind, dmg, weapon, attacker, src, npc_id=None):
        if kind == "crate":
            obj["hp"] -= dmg
            if attacker is not None:
                attacker.events.append({"type": "hit", "target": obj["id"], "amount": round(dmg), "weapon": weapon})
            return
        if kind == "npc":
            obj["hp"] -= dmg
            if attacker is not None:
                obj["target"] = attacker.id
                attacker.events.append({"type": "hit", "target": obj["id"], "amount": round(dmg), "weapon": weapon})
                attacker.last_combat = self.t
                attacker.hits += 1
            return
        m = obj
        if not m.alive:
            return
        rear = False
        bearing = math.atan2(src[1] - m.pos[1], src[0] - m.pos[0])
        if weapon != "zone":
            if abs(wrap(bearing - m.aim[0])) > math.radians(self.mc["front_arc_deg"]) * 0.5:
                dmg *= self.mc["rear_mult"]
                rear = True
            dmg *= max(0.0, 1 - m.stats.get("armor", 0))
        m.hp -= dmg
        m.dmg_taken += dmg
        m.last_combat = self.t
        ev = {"type": "damaged", "amount": round(dmg, 1), "weapon": weapon,
              "bearing": round(bearing, 3) if weapon != "zone" else None,
              "by": attacker.id if attacker is not None else (npc_id if weapon == "npc" else None)}
        if rear:
            ev["rear"] = True
        m.events.append(ev)
        if attacker is not None and attacker is not m:
            attacker.last_combat = self.t
            attacker.hits += 1
            attacker.dmg_dealt += dmg
            hv = {"type": "hit", "target": m.id, "amount": round(dmg, 1), "weapon": weapon}
            if rear:
                hv["rear"] = True
            attacker.events.append(hv)
        if m.hp <= 0 and m.death_cause is None:
            m.death_cause = weapon
            m.killer = attacker.id if attacker is not None else None

    # ------------------------------------------------------------------ npcs, world
    def _npcs_tick(self, dt):
        nc = self.cfg["npc"]
        keep = []
        for n in self.npcs:
            if n["hp"] <= 0:
                for _ in range(nc["cubes"]):
                    self._spawn_cube(n["pos"])
                continue
            keep.append(n)
            home = n["home"]
            tgt = None
            if n["target"] is not None:
                o = self.mechs[n["target"]]
                if o.alive and hyp(o.pos[0] - n["pos"][0], o.pos[1] - n["pos"][1]) < nc["deaggro_range"] \
                        and hyp(n["pos"][0] - home[0], n["pos"][1] - home[1]) < nc["chase_leash"]:
                    tgt = o
                else:
                    n["target"] = None
            if tgt is None and hyp(n["pos"][0] - home[0], n["pos"][1] - home[1]) < nc["leash"] and self.tick % 4 == 0:
                for o in self.mechs:
                    if o.alive and hyp(o.pos[0] - n["pos"][0], o.pos[1] - n["pos"][1]) < nc["aggro_range"]:
                        if self.w.los((n["pos"][0], n["pos"][1], n["pos"][2] + 2), (o.pos[0], o.pos[1], o.pos[2] + 4.5)):
                            n["target"] = o.id
                            tgt = o
                            break
            # movement
            if tgt is not None:
                gx, gy = tgt.pos[0], tgt.pos[1]
                if hyp(gx - n["pos"][0], gy - n["pos"][1]) < 25:
                    gx, gy = n["pos"][0], n["pos"][1]
            else:
                if hyp(n["wander"][0] - n["pos"][0], n["wander"][1] - n["pos"][1]) < 2 or self.rng.random() < 0.002:
                    a = self.rng.random() * TAU
                    rr = self.rng.random() * nc["leash"]
                    n["wander"] = (home[0] + rr * math.cos(a), home[1] + rr * math.sin(a))
                gx, gy = n["wander"]
            dx, dy = gx - n["pos"][0], gy - n["pos"][1]
            d = hyp(dx, dy)
            if d > 0.5:
                vx, vy = dx / d * nc["speed"], dy / d * nc["speed"]
                nx, ny = n["pos"][0] + vx * dt, n["pos"][1] + vy * dt
                if self.w.free_xy(nx, ny):
                    n["pos"] = [nx, ny, self.w.ground_at(nx, ny)]
                    n["vel"] = [round(vx, 2), round(vy, 2), 0.0]
                else:
                    n["vel"] = [0.0, 0.0, 0.0]
            else:
                n["vel"] = [0.0, 0.0, 0.0]
            n["cd"] = max(0.0, n["cd"] - dt)
            if tgt is not None and n["cd"] <= 0:
                src = (n["pos"][0], n["pos"][1], n["pos"][2] + 2.5)
                dst = (tgt.pos[0], tgt.pos[1], tgt.pos[2] + 4.5)
                if self.w.los(src, dst):
                    yaw = math.atan2(dst[1] - src[1], dst[0] - src[0])
                    pit = math.atan2(dst[2] - src[2], hyp(dst[0] - src[0], dst[1] - src[1]))
                    sp = math.radians(nc["spread_deg"])
                    yaw += self.rng.gauss(0, sp)
                    pit += self.rng.gauss(0, sp)
                    n["aim"] = round(yaw, 3)
                    d3 = self._dir(yaw, pit)
                    v = nc["projectile_speed"]
                    self.projectiles.append({"id": self._nid("proj"), "kind": "npc", "owner": -1, "owner_npc": n["id"],
                                             "pos": [src[0] + d3[0] * 2.3, src[1] + d3[1] * 2.3, src[2] + d3[2] * 2.3],
                                             "vel": [d3[0] * v, d3[1] * v, d3[2] * v], "ttl": 2.0, "dmg": nc["damage"],
                                             "g": 0.0, "radius": self.cfg["projectile"]["npc_radius"], "origin": list(src)})
                    n["cd"] = nc["cooldown"]
                    self._sound("npc_gun", src, -1)
        self.npcs = keep

    def _spawn_cube(self, pos):
        a = self.rng.random() * TAU
        r = self.rng.random() * 2.5
        x, y = pos[0] + r * math.cos(a), pos[1] + r * math.sin(a)
        self.cubes.append({"id": self._nid("cube"), "pos": [round(x, 2), round(y, 2), round(pos[2], 2)]})

    def _world_tick(self, dt):
        cfg = self.cfg
        # crates
        keep = []
        for c in self.crates:
            if c["hp"] <= 0:
                self._sound("crate", c["pos"], -1)
                for _ in range(cfg["crates"]["cubes"]):
                    self._spawn_cube(c["pos"])
                if self.rng.random() < cfg["loot"]["crate_weapon_chance"]:
                    lw = cfg["loot"]["weights"]
                    kinds = [k for k, v in lw.items() if v > 0]
                    self.pickups.append({"id": self._nid("pickup"), "kind": self.rng.choices(kinds, [lw[k] for k in kinds])[0],
                                         "pos": list(c["pos"]), "ammo": None})
                continue
            keep.append(c)
        self.crates = keep
        # cubes: closest mech within radius
        if self.cubes:
            pr = self.cc["pickup_radius"]
            rest = []
            for cb in self.cubes:
                best = None
                for m in self.mechs:
                    if not m.alive:
                        continue
                    d = hyp(m.pos[0] - cb["pos"][0], m.pos[1] - cb["pos"][1])
                    if d <= pr and abs(m.pos[2] - cb["pos"][2]) <= 4 and (best is None or d < best[0]):
                        best = (d, m)
                if best:
                    self._give_cube(best[1])
                    best[1].events.append({"type": "pickup", "cubes": best[1].cubes})
                else:
                    rest.append(cb)
            self.cubes = rest
        # zone, regen, offers
        c, r, dps = self.live_circle()
        for m in self.mechs:
            if not m.alive:
                continue
            outside = hyp(m.pos[0] - c[0], m.pos[1] - c[1]) > r
            if outside:
                m.hp -= dps * dt
                m.dmg_taken += dps * dt
                m.last_combat = self.t
                if self.tick % self.decide_every == 0:
                    m.events.append({"type": "damaged", "amount": round(dps * dt * 2, 1), "weapon": "zone", "bearing": None, "by": None})
                if m.hp <= 0 and m.death_cause is None:
                    m.death_cause = "zone"
                    m.killer = None
            elif self.t - m.last_combat >= m.stats["regen_delay"] and m.hp < m.max_hp:
                m.hp = min(m.max_hp, m.hp + m.stats["regen_per_s"] * dt)
            if m.offers and m.offer_expires is not None and self.t >= m.offer_expires:
                self._apply_upgrade(m, m.offers[0][0]["id"])
        # supply drops
        dc = cfg["drops"]
        for tt in dc["times"]:
            if abs(self.t - tt) < dt * 0.5:
                ph = self.zone_state()
                p = self._random_free(ph["nc"][0], ph["nc"][1], max(5.0, ph["nr"] * dc["spread"]))
                if p:
                    self.drops.append({"id": self._nid("drop"), "pos": [p[0], p[1], self.w.ground_at(*p)],
                                       "land_t": self.t + dc["fall_s"], "progress": 0.0, "holder": None, "landed": False})
        keep = []
        for d in self.drops:
            if self.t >= d["land_t"]:
                if not d["landed"]:
                    d["landed"] = True
                    self._sound("drop", d["pos"], -1)
                inside = [m for m in self.mechs if m.alive and hyp(m.pos[0] - d["pos"][0], m.pos[1] - d["pos"][1]) <= dc["radius"]]
                if len(inside) == 1:
                    if d["holder"] != inside[0].id:
                        d["holder"] = inside[0].id
                        d["progress"] = 0.0
                    d["progress"] += dt
                    if d["progress"] >= dc["capture_s"]:
                        m = inside[0]
                        self._give_cube(m, dc["cubes"])
                        m.hp = m.max_hp
                        m.energy = m.stats["energy"]
                        m.events.append({"type": "drop", "id": d["id"], "cubes": m.cubes})
                        m.drops_got = getattr(m, "drops_got", 0) + 1
                        continue
                elif len(inside) == 0:
                    d["holder"] = None
                    d["progress"] = 0.0
            keep.append(d)
        self.drops = keep

    def _resolve_deaths(self):
        dying = [m for m in self.mechs if m.alive and m.hp <= 0]
        if not dying:
            return
        n_alive = len(self.alive_mechs())
        for m in dying:
            m.alive = False
            m.place = n_alive
            m.t_death = round(self.t, 1)
            self.deaths.append((self.tick, m.id))
            self._sound("death", m.pos, m.id)
            k = getattr(m, "killer", None)
            if k is not None and k != m.id:
                km = self.mechs[k]
                km.kills += 1
                km.events.append({"type": "kill", "target": m.id})
            for _ in range(m.cubes + self.cc["death_drop_extra"]):
                self._spawn_cube(m.pos)
            for wp in m.weapons:
                if wp and wp["kind"] != "gun":
                    self.pickups.append({"id": self._nid("pickup"), "kind": wp["kind"], "pos": list(m.pos), "ammo": None})
