# Handoff: rooftop anchor experiment (not merged)

This is an unfinished experiment from Sat Nov 21. **The code is not in the bot.** The complete patch is at the end of this file. Base commit: `792fd14` (v6, real practice score 4.27).

## How to use the patch

- Copy the `diff` block at the end of this file to `anchor.patch`, then run `git apply anchor.patch` from the repo root.
  - It modifies `bot.py` (+148/−6) and `world.py` (+81), and adds `sim/anchor_diag.py`.
- **Everything is gated behind `PARAMS["anchor"] = 0.0`** (off by default).
  - With anchor off, `check/check.py` passes: 300/300 answered, max 4.2 ms locally. Behaviour should equal v6.
  - A paired run gave 4.60 against 4.64 before the edit, on the same seeds. That is within run-to-run noise, which comes from A* being time-sliced on wall-clock time.

## Idea (Siege-style verticality)

In `sim/housebot.py`, house bots plan in 2D, never jump, and only boost when they are outside the zone. They engage only with line of sight from their eye (z+8) to our centre. The engine allows standing on a roof, there is no fall damage, and a jet climb from full energy covers about 60 m. Laser range is 280 m.

The hypothesis: hold the middle of a roof with a laser. Bots that come close lose line of sight under the roof edge, and bots crossing open ground get lasered.

## What the patch implements

- **`world.py`**
  - `World.__init__` keeps the building prisms in `self.prisms`.
  - New `World.roof_spots(min_h, max_h, min_inset, min_cells)` finds flat building roofs.
    - `spot`: the roof cell farthest from every edge, found by BFS.
    - `inset`: the spot's distance to the edge in metres.
    - `apps`: up to about 9 ground approach points 5 m outside wall cells where the wall runs from the ground to the roof (no overhang). Each comes with a unit vector pointing into the building.
  - On the v2 map with `max_h=40`: **60 roofs**, 9–35 m high, median inset 9 m. It takes 0.12 s, against 0.26 s for World itself.
- **`bot.py`**
  - New PARAMS: `anchor`, `anchor_laser` (default 1: only once we hold a laser), `anchor_t`, `anchor_max_d` (300), `anchor_max_h` (40), `anchor_farm` (default 0: skip farming while anchoring).
  - New methods:
    - `_anchor_ready()`: checks the conditions and picks a roof.
    - `_pick_roof()`: the roof must be inside the next safe circle. Score = distance to its nearest approach + 0.4 × distance to the next centre − 4 × inset. The choice is kept for the whole zone phase.
    - `_climb_legs()`: walk to the approach, wait for energy (1.7 × height + 15), jump, then jet into the wall. **Once the feet are above `top + 0.3`, stop the jet and steer to the spot.** Without this, the mech sails over the whole building. If the roof isn't reached within 7 s, it is added to `roof_bad`.
  - Modes:
    - `"climb"`: once a climb starts, it continues before the threat logic, so it isn't abandoned halfway up a wall.
    - `"anchor"`: when on our roof, retreat and evade map to `anchor`.
    - On the roof, `_legs` keeps every combat or idle mode at the spot. Only zone and travel modes take us off the roof.
  - Per-tick changes:
    - `self.on_roof` = grounded and z − ground > 4.
    - During a climb, jump and boost come from `_climb_legs`.
    - `_dodge_mortar`, the unstuck logic and `_stuck_check` are skipped during a climb.
- **`sim/anchor_diag.py`** (new)
  - Runs practice matches in parallel, like `sim/trace.py`.
  - Reports time per mode, climbs started/finished/failed, time on roof, damage taken per minute and kills on and off the roof, and death place and time.
  - `--probe` prints every decision during the climbs of one match.

```sh
python3 -m sim.anchor_diag --bot bot.py:anchor=1 --n 48 --seed 5000 --workers 15
python3 -m sim.anchor_diag --bot bot.py:anchor=1 --seed 5003 --probe
```

## Results (sim, practice mode)

| Run | Score | Notes |
|---|---|---|
| v6 baseline, 96 matches, seeds 5000+ | 4.64 ± 0.27 | death median 136 s, kills 4.0, laser hit rate 66% |
| anchor=1, first version (jet never cut) | 4.02 (paired **−0.58 ± 0.25**, WORSE) | Of 62 climbs, 4 finished: the mech flew over the roofs. |
| anchor=1, anchor_laser=0, first version | 3.92 (paired −0.69 ± 0.32, WORSE) | |
| anchor=1 **with the landing fix**, 48 matches, seeds 5000–5047 | **3.75**, not yet paired against v6 on these 48 seeds | 35/35 climbs finished, roof reached in 29/48 matches, 49 s on a roof per match |

With the landing fix:
- **Damage taken per minute: 174 on a roof against 1,337 off it.** The roof is very protective.
- **Kills: 14 made on a roof against 133 off it**, so the score went down.
- Median death time rose to 151 s, from about 130–136 s.

**Why the score drops:** in practice mode, house bots only die to us (and rarely to the zone or drones), so our place is roughly the number of kills we make before dying. Hiding on a roof keeps us alive but kills nothing. Skipping drone and crate farming also costs cubes (4.1 against 5.7 at death).

## Suggested next steps (not done)

1. **Kill from the roof (edge peek).** House bots that lose sight of us walk to our ground position and get stuck at the wall below. Move from the roof centre to the edge above a heard or remembered bot, laser it when line of sight appears, then return to the centre to regenerate (45 HP/s after 5 s with no damage). This is what turns the 7× lower damage rate into kills.
2. **Test in FFA** (`python3 -m sim.run --mode ffa ...`). In the final, survival matters more and other bots kill each other, so the anchor as it stands may already help there. Note the sim's FFA opponents don't climb either, so this test is optimistic.
3. **Use the anchor late only** (`anchor_t` around 200–300 s, `anchor_farm=1`), so early farming and kills are kept.
4. **Act-time spike.** The worst `act()` locally was 34–37 ms, in `hold`, `farm` and `retreat` modes. The match container has only 1 CPU. If the submission page's "wait 122 ms" is the worst answer time, cap `_hold_point()` and the cover search with the existing `self.deadline`.
5. **Validate any anchor variant on the real board before keeping it.** The real house bots may jet onto roofs even though the sim's stand-ins never do.

Fast parallel sims: 96 matches took about 130 s on a 16-vCPU machine (`--workers 15`). Ask Jorge about machine access.

## The patch (against `792fd14`)

````diff
diff --git a/bot.py b/bot.py
index 1d14a45..5197196 100644
--- a/bot.py
+++ b/bot.py
@@ -92,6 +92,12 @@ PARAMS = {
     "scoot_r": 30.0,        # ... only to cover this close
     "hidden_reload": 0.0,   # 1 = top up the magazine whenever the target is out of sight
     "anti_flank": 0.0,      # >0: weight of stepping away from two attackers that are on opposite sides of us
+    "anchor": 0.0,          # 1 = Siege-style rooftop anchor: climb a roof inside the next circle and hold its centre
+    "anchor_laser": 1.0,    # ... only once we hold a laser (0 = with any loadout)
+    "anchor_t": 0.0,        # ... not before this time (s)
+    "anchor_max_d": 300.0,  # ... roofs whose wall approach is at most this far
+    "anchor_max_h": 40.0,   # ... roofs at most this high above the ground (jet climb ~60 m from full energy)
+    "anchor_farm": 0.0,     # 1 = keep farming remembered drones/crates before anchoring
     "ffa_detect": 1.0,      # 1 = switch to FFA_PARAMS once other mechs are clearly killing each other
     "ffa_early": 2.0,       # ... deaths not caused by us before t=120 s that mean free-for-all
     "ffa_any": 4.0,         # ... or this many at any time
@@ -184,6 +190,20 @@ class Bot:
         self.scan_start = -1e9
         self.scan_base = 0.0
         self.next_scan_t = 0.0
+        # rooftop anchor
+        self.roofs = []
+        if self.world is not None and self.P["anchor"]:
+            try:
+                self.roofs = self.world.roof_spots(max_h=self.P["anchor_max_h"])
+            except Exception as e:
+                mf.log("roof_spots failed:", repr(e))
+        self.roof = self.roof_app = self.roof_i = self.roof_key = None
+        self.roof_bad = set()
+        self.climb_t = None
+        self.on_roof = False
+        self.want_jump = False
+        self.jet = False
+        mf.log("roofs %d (%.2fs)" % (len(self.roofs), time.perf_counter() - t0))
         gc.collect()
         if hasattr(gc, "freeze"):
             gc.freeze()
@@ -241,6 +261,10 @@ class Bot:
         self.max_pitch = float(st.get("max_pitch", 1.3))
         self.aim_now = me.get("aim", [0.0, 0.0])
         self.weap = me.get("weapons") or [None, None]
+        w = self.world
+        self.on_roof = w is not None and bool(me.get("grounded")) and z - w.ground_at(x, y) > 4.0
+        if self.on_roof:
+            self.climb_t = None
 
         self._events(obs)
         self._detect_ffa(obs)
@@ -256,19 +280,28 @@ class Bot:
         target = self._pick_target(enemies)
         mode, goal, speed, boost = self._decide(obs, target, npcs)
         self.mode = mode
+        if mode != "climb":
+            self.climb_t = None
 
         # ----- legs
+        self.want_jump = False
+        self.jet = False
         mvx, mvy = self._legs(mode, goal, speed, target, obs)
         jump = False
-        dodge = self._dodge_mortar(obs)
+        climbing = mode == "climb" and self.climb_t is not None
+        if climbing:
+            jump = self.want_jump
+            boost = self.jet
+        dodge = None if climbing else self._dodge_mortar(obs)
         if dodge is not None:
             mvx, mvy = dodge
             boost = self.energy > 10
-        if t < self.unstuck_until:
+        if t < self.unstuck_until and not climbing:
             mvx, mvy = self.unstuck_dir
             jump = bool(me.get("grounded"))
             boost = self.energy > 5
-        self._stuck_check(mvx, mvy, mode)
+        if not climbing:
+            self._stuck_check(mvx, mvy, mode)
         act["move"] = [round(mvx, 3), round(mvy, 3)]
         act["boost"] = bool(boost)
         if jump:
@@ -546,9 +579,14 @@ class Bot:
         if self.zone_urgent or self.outside:
             return "zone", self.zone_goal, 1.0, (self.energy > 8 and (self.outside or self.zone_need > 0.5 * self.zone_avail))
 
+        on_anchor = P["anchor"] and self.on_roof and self.roof is not None
+        if P["anchor"] and self.climb_t is not None and not self.on_roof and self.roof is not None:
+            return "climb", (self.roof_app[0], self.roof_app[1]), 1.0, False  # finish a climb once started
         if threat:
             weak_one = t_hp < hp * 0.8 and n_close <= 1
             if self.retreating and not weak_one:
+                if on_anchor:
+                    return "anchor", self.roof["spot"], 1.0, False  # the roof centre is our cover
                 return "retreat", self._cover_point(target), 1.0, boost_ok and target["los"]
             grab = self._near_weapon(35.0)
             if grab is not None:
@@ -575,6 +613,8 @@ class Bot:
                 return "evade", (x + 40 * math.cos(away), y + 40 * math.sin(away)), 1.0, False
 
         if attacked and self.dmg_bearing is not None:
+            if on_anchor:
+                return "anchor", self.roof["spot"], 1.0, False
             # hit by someone we cannot see: turn to face it (handled by aim) and keep moving to cover
             return "evade", self._cover_from_bearing(self.dmg_bearing), 1.0, False
 
@@ -598,17 +638,24 @@ class Bot:
         # loot when nobody is close (also right after a kill: the wreck drops cubes and weapons)
         no_close = all(e["dh"] > P["loot_close"] and not (e["dh"] < 220 and e.get("approach", 0.0) > 3.0)
                        for e in self.enemies)
+        anchoring = self._anchor_ready()
         if (t - self.last_mech_seen_t > 6.0 or no_close) and self.zone_need < 0.45 * max(1.0, self.zone_avail):
-            lg = self._loot_goal(obs, npcs)
+            lg = self._loot_goal(obs, npcs, farm=not anchoring or P["anchor_farm"])
             if lg is not None:
                 return lg
 
+        if anchoring:
+            r = self.roof
+            if self.on_roof and abs(self.pos[2] - r["top"]) < 2.5:
+                return "anchor", r["spot"], 1.0, False
+            return "climb", (self.roof_app[0], self.roof_app[1]), 1.0, False
+
         if not self.in_safe:
             return "travel", self.zone_goal, 1.0, False
 
         return "hold", self._hold_point(), 1.0, False
 
-    def _loot_goal(self, obs, npcs):
+    def _loot_goal(self, obs, npcs, farm=True):
         x, y, z = self.pos
         hp = self.hp_frac
         early = self.t < 90.0
@@ -676,7 +723,7 @@ class Bot:
                 return "crate", goal, 1.0, False
         # 5) farm: walk to remembered drones (2 cubes) and crates (1 cube) that are out of view
         fr = self.P["farm_r"]
-        if fr > 0 and not capped:
+        if fr > 0 and not capped and farm:
             best, bs = None, 1e18
             if hp > self.P["farm_npc_hp"] and self.t < 330:
                 for p in self.known_npc.values():
@@ -820,10 +867,99 @@ class Bot:
         fake = {"pos": [self.pos[0] + 80 * math.cos(bearing), self.pos[1] + 80 * math.sin(bearing), self.pos[2]], "h": 9}
         return self._cover_point(fake)
 
+    # ------------------------------------------------------------------ rooftop anchor
+    def _anchor_ready(self):
+        """True when we should hold (or climb to) a roof now; picks the roof."""
+        P = self.P
+        if not P["anchor"] or not self.roofs or self.t < P["anchor_t"] or self.zone_urgent or self.outside:
+            return False
+        if P["anchor_laser"] and not any(w and w.get("kind") == "laser" for w in self.weap):
+            return False
+        return self._pick_roof() is not None
+
+    def _pick_roof(self):
+        """The climbable roof inside the next safe circle that is near us, deep in the circle and
+        far from its own edges. Kept for the whole zone phase while it stays inside the circle."""
+        if self.roof is not None and self.roof_key == self.zone_key and \
+                self._inside_safe(self.roof["spot"][0], self.roof["spot"][1], -self.roof["inset"]):
+            return self.roof
+        self.roof_key = self.zone_key
+        x, y = self.pos[0], self.pos[1]
+        P = self.P
+        best, bs = None, 1e18
+        for i, r in enumerate(self.roofs):
+            if i in self.roof_bad:
+                continue
+            sx, sy = r["spot"]
+            if not self._inside_safe(sx, sy, -r["inset"]):
+                continue
+            app = min(r["apps"], key=lambda a: hyp(a[0] - x, a[1] - y))
+            d = hyp(app[0] - x, app[1] - y)
+            if d > P["anchor_max_d"]:
+                continue
+            s = d + 0.4 * hyp(sx - self.next_c[0], sy - self.next_c[1]) - 4.0 * r["inset"]
+            if s < bs:
+                best, bs = (i, r, app), s
+        if best is None:
+            self.roof = self.roof_app = self.roof_i = None
+            return None
+        if self.roof_i != best[0]:
+            self.climb_t = None
+            mf.log("anchor roof %d h=%.0f inset=%.0f at t=%.0f" % (best[0], best[1]["h"], best[1]["inset"], self.t))
+        self.roof_i, self.roof, self.roof_app = best
+        return self.roof
+
+    def _climb_legs(self):
+        """Walk to the wall approach, wait for energy, then jump and jet straight up onto the roof."""
+        x, y, z = self.pos
+        r = self.roof
+        ax, ay, ux, uy = self.roof_app
+        d = hyp(ax - x, ay - y)
+        if self.on_roof:
+            # on some other roof or ledge: walk off it towards our approach (there is no fall damage)
+            return ((ax - x) / d, (ay - y) / d) if d > 1.0 else (0.0, 0.0)
+        grounded = bool(self.me.get("grounded"))
+        if self.climb_t is None:
+            if d > 2.5:
+                return self._nav(ax, ay)
+            need = 1.7 * max(0.0, r["top"] - z) + 15.0
+            if grounded and self.energy < min(need, 98.0):
+                return 0.0, 0.0  # at the wall: regenerate energy first (about 6 s from empty)
+            self.climb_t = self.t
+        if self.t - self.climb_t > 7.0:
+            mf.log("climb failed on roof %s at t=%.0f" % (self.roof_i, self.t))
+            self.roof_bad.add(self.roof_i)
+            self.roof = self.roof_app = self.roof_i = self.roof_key = None
+            self.climb_t = None
+            return 0.0, 0.0
+        self.want_jump = grounded
+        if grounded or z < r["top"] + 0.3:
+            self.jet = not grounded
+            return ux, uy  # push into the wall and rise along it
+        # above the roof edge: stop the jet (or we sail over the whole building) and drop onto the roof
+        self.jet = False
+        sx, sy = r["spot"]
+        ds = hyp(sx - x, sy - y)
+        if ds < 3.0:
+            return 0.0, 0.0
+        return (sx - x) / ds, (sy - y) / ds
+
     # ------------------------------------------------------------------ legs
     def _legs(self, mode, goal, speed, target, obs):
         x, y = self.pos[0], self.pos[1]
         t = self.t
+        if self.P["anchor"]:
+            if mode == "climb":
+                return self._climb_legs()
+            if self.on_roof and self.roof is not None and mode in (
+                    "anchor", "fight", "npc", "crate", "retreat", "scoot", "evade", "hold", "heal", "cube"):
+                # holding the roof: stay on the spot farthest from the edges (silent, out of sight from below)
+                sx, sy = self.roof["spot"]
+                d = hyp(sx - x, sy - y)
+                if d < 2.0:
+                    return 0.0, 0.0
+                sp = 1.0 if d > 6.0 else 0.4
+                return (sx - x) / d * sp, (sy - y) / d * sp
         if mode in ("fight", "npc") or (mode == "crate" and goal is None):
             tg = target if mode == "fight" else self.loot_target
             if tg is None:
diff --git a/world.py b/world.py
index ea8d2b6..aed2769 100644
--- a/world.py
+++ b/world.py
@@ -16,6 +16,9 @@ class World:
     def __init__(self, map_dir, step_up=1.2, mech_height=9.0, cell=2.0):
         with open(os.path.join(map_dir, "v2", "world.json"), "r", encoding="utf-8") as f:
             wj = json.load(f)
+        # building footprints, kept for roof_spots() (rooftop anchor)
+        self.prisms = [{"top": p["top"], "ring": p["ring"], "holes": p.get("holes") or []}
+                       for p in wj.get("prisms", []) if p.get("kind", "building") == "building"]
         ter = wj["terrain"]
         self.tnx = int(ter["nx"])
         self.tny = int(ter["ny"])
@@ -258,6 +261,84 @@ class World:
                 return False
         return True
 
+    # ---------------------------------------------------------------- rooftops
+    def roof_spots(self, min_h=8.0, max_h=45.0, min_inset=6.0, min_cells=25):
+        """Flat building roofs we can climb (jump + jet up a wall) and hold.
+
+        For each roof: `spot` is the roof cell farthest from every edge (out of sight of mechs
+        close to the building), `inset` its distance to the edge in metres, and `apps` a few
+        approaches (ax, ay, ux, uy): free ground just outside a wall that runs from the ground
+        to the roof (no overhang to bump the jet into), with (ux, uy) pointing into the building.
+        """
+        W, H, C = self.W, self.H, self.C
+        bt, bb, ground = self.bt, self.bb, self.ground
+        out = []
+        for p in self.prisms:
+            top = float(p["top"])
+            flat = {c for c in self._raster([p["ring"]] + list(p["holes"])) if abs(bt[c] - top) < 0.6}
+            if len(flat) < min_cells:
+                continue
+            g = sorted(ground[c] for c in flat)[len(flat) // 2]
+            h = top - g
+            if not (min_h <= h <= max_h):
+                continue
+            # 8-neighbour distance (cells) from the roof edge, by BFS inwards
+            dist = {}
+            frontier = []
+            for c in flat:
+                j, i = divmod(c, W)
+                for dj in (-1, 0, 1):
+                    jj = j + dj
+                    for di in (-1, 0, 1):
+                        ii = i + di
+                        if not (0 <= jj < H and 0 <= ii < W) or (jj * W + ii) not in flat:
+                            dist[c] = 1
+                            break
+                    if c in dist:
+                        break
+                if c in dist:
+                    frontier.append(c)
+            while frontier:
+                nxt = []
+                for c in frontier:
+                    j, i = divmod(c, W)
+                    d = dist[c] + 1
+                    for dj in (-1, 0, 1):
+                        for di in (-1, 0, 1):
+                            k = (j + dj) * W + (i + di)
+                            if k in flat and k not in dist:
+                                dist[k] = d
+                                nxt.append(k)
+                frontier = nxt
+            if not dist:
+                continue
+            best = max(dist, key=dist.get)
+            inset = (dist[best] - 0.5) * C
+            if inset < min_inset:
+                continue
+            sx, sy = self.center(best)
+            apps = []
+            for c, d in dist.items():
+                if d != 1 or bb[c] > ground[c] + 1.0:
+                    continue  # not an edge, or a raised/overhanging part the jet would hit from below
+                cx, cy = self.center(c)
+                ox, oy = cx - sx, cy - sy
+                on = math.hypot(ox, oy)
+                if on < 1e-6:
+                    continue
+                ux, uy = ox / on, oy / on
+                ax, ay = cx + ux * 5.0, cy + uy * 5.0
+                if not self.free_xy(ax, ay):
+                    continue
+                apps.append((math.atan2(uy, ux), ax, ay, -ux, -uy))
+            if not apps:
+                continue
+            apps.sort()
+            stride = max(1, len(apps) // 8)
+            out.append({"spot": (sx, sy), "top": top, "ground": g, "h": h, "inset": inset,
+                        "apps": [a[1:] for a in apps[::stride]]})
+        return out
+
 
 class AStar:
     """Weighted A* on the World grid that can be resumed across decisions."""
diff --git a/sim/anchor_diag.py b/sim/anchor_diag.py
new file mode 100644
index 0000000..1937ade
--- /dev/null
+++ b/sim/anchor_diag.py
@@ -0,0 +1,132 @@
+"""Where does the rooftop anchor spend its time, and does it actually help on the roof?
+
+  python -m sim.anchor_diag --bot bot.py:anchor=1 --n 48 --seed 5000 --workers 15
+
+Practice matches like sim/trace.py, with the hero's decisions instrumented: time per mode,
+climbs started / finished / failed, time on a roof, damage taken and kills made on and off
+the roof, and where we died.
+"""
+import argparse
+import collections
+import multiprocessing as mp
+import os
+import random
+import sys
+
+ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
+if ROOT not in sys.path:
+    sys.path.insert(0, ROOT)
+from sim import run as R  # noqa: E402
+
+PROBE = False
+
+
+def one(job):
+    spec, seed = job
+    R._init()
+    from sim import housebot
+    from sim.engine import Match
+    from sim.loader import BotCtrl, load_bot_class
+    cfg, w = R._G["cfg"], R._G["world"]
+    rng = random.Random(seed)
+    hero = rng.randrange(16)
+    hello = {"type": "hello", "protocol": "mechfight/1", "slot": hero, "seed": 1, "config": cfg,
+             "map": {"dir": os.path.join(ROOT, "arena"), "files": []}}
+    squad = housebot.Squad(hero, w, seed=seed)
+    ctrls = [None] * 16
+    hc = BotCtrl(load_bot_class(spec, w), hello, spec)
+    ctrls[hero] = hc
+    for lvl, i in zip(housebot.ORDER, [i for i in range(16) if i != hero]):
+        hb = housebot.HouseBot(squad, i, lvl, cfg, seed * 31 + i)
+        squad.add(hb)
+        ctrls[i] = hb
+    m = Match(cfg, w, ctrls, seed=seed, mode="practice", hero=hero)
+    st = {"modes": collections.Counter(), "roof_s": 0.0, "climb_start": 0, "climb_ok": 0,
+          "dmg_roof": 0.0, "dmg_off": 0.0, "k_roof": 0, "k_off": 0, "laser_t": None,
+          "first_roof_t": None, "prev_climb": None, "prev_roof": False, "last_on_roof": False}
+    orig = hc.act
+
+    def act(obs):
+        out = orig(obs)
+        b = hc.bot
+        on = bool(getattr(b, "on_roof", False))
+        if PROBE and (getattr(b, "climb_t", None) is not None or (st["prev_climb"] is not None)):
+            me = obs["self"]
+            p = me["pos"]
+            r = getattr(b, "roof", None)
+            app = getattr(b, "roof_app", None)
+            print("t=%6.1f mode=%-6s pos=(%.1f,%.1f,%.1f) gnd=%.1f top=%s app=%s grounded=%s e=%.0f vel=%s move=%s jump=%s boost=%s" % (
+                obs["t"], b.mode, p[0], p[1], p[2], w.ground_at(p[0], p[1]), r and round(r["top"], 1),
+                app and (round(app[0], 1), round(app[1], 1)), me.get("grounded"), me.get("energy", 0),
+                [round(v, 1) for v in me.get("vel", [0, 0, 0])], out.get("move"), out.get("jump"), out.get("boost")))
+        st["modes"][getattr(b, "mode", "?")] += 1
+        if on:
+            st["roof_s"] += 0.1
+            if st["first_roof_t"] is None:
+                st["first_roof_t"] = obs["t"]
+        ct = getattr(b, "climb_t", None)
+        if ct is not None and st["prev_climb"] is None:
+            st["climb_start"] += 1
+        if on and not st["prev_roof"] and st["prev_climb"] is not None:
+            st["climb_ok"] += 1
+        st["prev_climb"], st["prev_roof"] = ct, on
+        st["last_on_roof"] = on
+        if st["laser_t"] is None and any(x and x.get("kind") == "laser" for x in obs["self"].get("weapons") or ()):
+            st["laser_t"] = obs["t"]
+        for e in obs["events"]:
+            if e["type"] == "damaged" and e.get("weapon") != "zone":
+                st["dmg_roof" if on else "dmg_off"] += e["amount"]
+            elif e["type"] == "kill":
+                st["k_roof" if on else "k_off"] += 1
+        return out
+    hc.act = act
+    res = m.run()
+    h = res[hero]
+    st["score"], st["place"], st["cause"], st["t_death"] = h["score"], h["place"], h["cause"], h["t_death"]
+    st["fails"] = len(getattr(hc.bot, "roof_bad", ()))
+    st["died_on_roof"] = st["last_on_roof"]
+    st["modes"] = dict(st["modes"])
+    return st
+
+
+def main():
+    ap = argparse.ArgumentParser()
+    ap.add_argument("--bot", default="bot.py:anchor=1")
+    ap.add_argument("--n", type=int, default=48)
+    ap.add_argument("--seed", type=int, default=5000)
+    ap.add_argument("--workers", type=int, default=max(1, mp.cpu_count() - 1))
+    ap.add_argument("--probe", action="store_true", help="one match, print every decision during climbs")
+    a = ap.parse_args()
+    if a.probe:
+        global PROBE
+        PROBE = True
+        r = one((a.bot, a.seed))
+        print("RESULT", {k: r[k] for k in ("score", "climb_start", "climb_ok", "fails", "roof_s")})
+        return
+    jobs = [(a.bot, a.seed + i) for i in range(a.n)]
+    with mp.Pool(a.workers) as pool:
+        rows = pool.map(one, jobs)
+    n = len(rows)
+    modes = collections.Counter()
+    for r in rows:
+        modes.update(r["modes"])
+    tot = sum(modes.values())
+    print(f"{a.bot}: {n} matches, mean score {sum(r['score'] for r in rows) / n:.2f}")
+    print("decision share by mode:", ", ".join(f"{k} {100 * v / tot:.0f}%" for k, v in modes.most_common(12)))
+    got_laser = [r for r in rows if r["laser_t"] is not None]
+    reached = [r for r in rows if r["first_roof_t"] is not None]
+    print(f"laser in {len(got_laser)}/{n} (median t {sorted(r['laser_t'] for r in got_laser)[len(got_laser) // 2] if got_laser else '-'} s); "
+          f"reached a roof in {len(reached)}/{n} (median first t {sorted(r['first_roof_t'] for r in reached)[len(reached) // 2] if reached else '-'} s)")
+    print(f"climbs started {sum(r['climb_start'] for r in rows)}, finished {sum(r['climb_ok'] for r in rows)}, "
+          f"roofs given up {sum(r['fails'] for r in rows)}; mean time on roof {sum(r['roof_s'] for r in rows) / n:.0f} s")
+    rs = sum(r["roof_s"] for r in rows)
+    ofs = sum((r["t_death"] or 480) for r in rows) - rs
+    print(f"damage taken per minute: on roof {60 * sum(r['dmg_roof'] for r in rows) / max(rs, 1):.0f}, "
+          f"off roof {60 * sum(r['dmg_off'] for r in rows) / max(ofs, 1):.0f}; kills on roof {sum(r['k_roof'] for r in rows)}, "
+          f"off {sum(r['k_off'] for r in rows)}; died on a roof {sum(r['died_on_roof'] for r in rows)}/{n}")
+    causes = collections.Counter(r["cause"] for r in rows)
+    print("death causes:", dict(causes), " median death t", sorted((r["t_death"] or 480) for r in rows)[n // 2])
+
+
+if __name__ == "__main__":
+    main()
````
