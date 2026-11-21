# CLAUDE.md: Mech Royale bot (Builders Buildathon, Nov 20–22 2026)

We are a 2-person team building a bot for the **Builders: Mech Royale** track.
Track page: https://www.joinbuilders.org/buildathon/tracks/builders
Full rulebook: `protocol.md` (from the kit). It is the source of truth. If this file disagrees with it, protocol.md wins.

**Hard deadline: Sun Nov 22, 11:00 AM ET.** Winner takes $5,000; second place gets nothing.

---

## 1. What the bot is

A Docker image whose entrypoint speaks `mechfight/1` JSON lines: observations on **stdin**, actions on **stdout**.
It pilots one mech in a 16-mech 3D battle royale on the OSU campus. **Placement is the score, not kills.**

- Score per match = `16 - place` (15 for a win, 0 for dying first). Same-tick deaths share a place. If time runs out, survivors are ranked by HP.
- Our score = the mean over all matches in a run.

### How we are judged (read this before optimizing anything)

- **Practice (the live leaderboard):** 48 matches against 15 *house bots* that act as **one squad**. They share vision, **never fight each other**, and hunt us, sending the weakest first (one early, more as we thin them out, all of them late).
  - *Inference:* house bots only die from us or the zone, so the practice score is roughly how many house bots die before we do. Practice rewards killing.
- **Final:** the top 32 practice teams play 16 qualifying free-for-all matches **against other teams' bots** on new seeds. The top 16 then play a 64-match series. With fewer than 32 teams, everyone qualifies.
  - *Inference:* the final is a real free-for-all, where survival beats aggression. **Practice and final reward different behavior.** Don't overfit to the practice board.
- Ties: the earlier submission wins. Submit a solid version early.
- Practice seeds are private and fixed for the whole event. Our slot and spawn change each match.

---

## 2. Hard constraints (breaking any of these = rejected or a sitting duck)

| Constraint | Value |
|---|---|
| Decision budget | **50 ms wall time** per observation (measured server-side). Aim for under 10 ms in act(). |
| Ready budget | `ready` within 20 s of `hello`, which covers container start. Precompute map grids here. |
| CPU / RAM | **1 CPU, 512 MB**, no swap, at most 128 processes/threads |
| Runtime | no network, uid 65534, read-only filesystem except `/tmp` (64 MB), map read-only at `/arena` |
| stdout | protocol lines ONLY, one JSON object per line, at most 64 KiB, flushed. **Logs go to stderr.** |
| Repo | public, **at most 20 MB** at the submitted commit, `Dockerfile` at the top level, linux/amd64 build in at most 10 min, **pin versions** (finalists are rebuilt) |
| Submissions | 4 per hour per team. Each runs check.py, then 48 practice matches (a few minutes). |
| Repo history | keep every submitted commit until results are out. **Never force-push or rewrite history.** |
| License | everything built this weekend must be released under the **MIT License** (LICENSE file at the root). |

- Missed answer: the mech keeps `move`/`boost`/`aim`, drops `fire`/`jump`/`reload`/`pick`/`take`/`drop`. After 1 s of misses it idles.
- Crash: the mech idles for the rest of the match. The kit wrapper catches exceptions in act() and re-sends the last move.
- If observations queue up, answer only the newest. The wrapper does this. **Always echo `tick`.**

---

## 3. Repo layout

```
Dockerfile          # FROM python:3.13-slim; COPY . .; ENTRYPOINT ["python3","-u","/bot/bot.py"]
bot.py              # our bot (entrypoint): zone, modes, aiming, weapons, loot
world.py            # map model: terrain, 2 m building grid, LOS raycast, time-sliced A*
mechfight.py        # kit wrapper (stdlib only), copied from kit python/
tools/navtest.py    # offline: grid stats, A* timing, PNG of the nav grid
tools/scenarios.py  # offline: hand-made situations, asserts each behaviour branch
check/check.py      # kit conformance check (local testing only)
check/stream.jsonl.gz
arena/v2/world.json # map, for local testing (in a match it is mounted at /arena)
arena/v2/terrain.i16
protocol.md         # kit rulebook
LICENSE             # MIT
.dockerignore       # check/ arena/ *.md .git
```

Python stdlib + `numpy` are fine. If we add numpy, `pip install` it **system-wide** in the Dockerfile with a pinned version, and check that import time fits the 20 s ready budget.

---

## 4. Commands (run inside WSL2 Ubuntu, repo cloned under ~/ and NOT /mnt/c)

```sh
# fast protocol check, no Docker
python3 check/check.py -- python3 -u bot.py

# build + check exactly the way the scorer does (must pass before every push)
docker build -t mech-bot .
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m \
  --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech-bot

# ship
git add -A && git commit -m "..." && git push
# then paste the repo URL + commit on the track page and submit
```

check.py replays 300 recorded observations and does NOT react to our actions. Passing it proves protocol compliance, not play quality.

---

## 5. Protocol essentials

- Units: meters, seconds, radians. **x = east, y = north, z = up.**
- Yaw is measured from +x, counter-clockwise (0 = east, π/2 = north). Pitch is up-positive. Direction = `[cos p·cos y, cos p·sin y, sin p]`.
- Mech = vertical cylinder, radius 2.5, height 9. `pos` is the feet. Center of mass is `z+4.5`, eyes `z+8`, gun muzzle `z+7`. Shots spawn 3 m out along the aim.
- `self.aim` is the **actual** torso aim. It slews toward the commanded aim at `turn_rate` 4 rad/s (yaw) and `pitch_rate` 3 rad/s, and shots leave along the actual aim. **Only fire when the actual aim is on target.**
- Legs and torso are independent: you can move one way and aim another.
- Lockstep: 20 ticks/s, one decision every 2 ticks (10 decisions/s). Each action holds for 0.1 s.
- **Read every number from `hello.config`** (balance can change between matches). The values below are the current defaults.

### Key numbers (current config)
- **Mech:** 1400 HP; walk 11.7 m/s; boost 19.5 m/s (drains 17 energy/s); 100 energy, regen 18/s after 0.6 s; jump 11 m/s; jet 34 m/s² up to 10 m/s rise (drains 15/s, about 60 m of climb from full); gravity 22.
- **HP regen:** 45 HP/s after 5 s with no damage dealt or taken. None outside the zone.
- **Rear hits deal ×1.5:** anything from outside the 180° arc centered on our aim yaw. **Face the attacker.**
- **Gun:** 45 dmg, 0.24 s cooldown, 252 m/s, 230 m range, 0.6° spread, mag 12, reload 2.4 s. Flies straight, so **lead moving targets**.
- **Mortar:** 72 m/s shell, gravity 22 (max range about 235 m at 45°); direct hit 200, splash 160 within 7 m (blocked by walls); knockback; drains 45 energy; our own splash hits us at 50%. Cooldown 2.6 s.
- **Laser:** hold to charge (up to 0.75 s), release to fire a hitscan beam (280 m), up to 315 dmg; cooldown 1.65 s; half move speed while charging; visible `charging: true`; +60% vs a mech holding a mortar.
- **Lightning:** instant, nearest target within 45 m and a 30° cone, with line of sight; 60 dmg, chains to 2 more (×0.7); 3 energy per shot; 0.25 s cooldown; cancels a laser charge and blocks lasers for 2 s.
- **Triangle:** laser beats mortar, mortar beats lightning, lightning beats laser. The gun is neutral.
- **Vision:** 200 m within a 120° wedge on aim yaw, plus 45 m all around. Buildings block; trunks block sight; crowns fade it. Trees do **not** stop shots. Seen mechs stay tracked for 5 s.
- **Sound:** a `bearing` with σ = 7° of noise, plus near (<60) / mid (<160) / far. **Moving slower than 5 m/s on the ground is silent.** Ranges: gun 320, mortar/explosion 420, laser 380, laser_charge 140, lightning 160, step 40, boost/jet 90, death 600.
- **Zone:** starts at radius 560 around (41.3, −72.8). Phases (hold s / shrink s → radius @ dps): 60/50→380@20, 45/40→240@35, 35/35→140@60, 30/30→70@100, 25/25→25@160, 15/25→0@260. Closes by about 6:55; the match ends at 8:00. `obs.zone` always shows the current and next circle.
- **Cubes:** each gives +4% max HP and +4% damage (cap 10) and +5% size (no cap, so bigger targets). Upgrade offers come at 1, 3, 6, 10, 15, 21… cubes; you have 5 s to `pick`, otherwise choice 0 is applied.
- **NPC drones:** 12 per match, 350 HP, 2 cubes each, 25 dmg bullets (1 s cooldown). They stay leashed 30 m from home and chase up to 60 m.
- **Crates:** 36 per match, 300 HP, 1 cube each, 50% chance to drop a weapon. About a quarter are on rooftops.
- **Weapon loot:** 40 floor spawns (mortar:laser:lightning = 1:1:1). `take` works within 4 m. A newly taken weapon is locked for max(1 s, its cooldown). A dead mech drops everything except its gun.
- **Supply drops:** called at 70/140/210 s and land 25 s later inside the next circle. Stand in the 8 m ring **alone** for 4 s to get +3 cubes, full HP and full energy.

### Map (`/arena/v2/`, the same every match)
- `world.json`: `bounds` (x −512…555, y −333…389); `prisms[]` (259 solids: building/stadium/bridge, each with `base`, `top`, `ring` as a flat xy polygon, and `holes`); `trees` (flat `data` array, columns listed in `fields`). Everything else in the file is scenery the server ignores.
- `terrain.i16`: int16 little-endian centimeters, 1070 × 724 cells, row-major from (minX −513, minY −334), 1 m step. Sample bilinearly.
- Movement: steps up to 1.2 m are walkable; anything taller is a wall unless you jump and jet over it. Slopes over 50° can't be walked. Walls slide you along them.

---

## 6. Strategy (hypotheses to test, not facts)

v1 priorities, in order:
1. **Never die to the zone.** Path toward a point inside `next_center/next_radius` with margin, and be inside before `shrink_start`. Late-phase zone damage (100–260 HP/s) is lethal within seconds.
2. **Survive over kill**, especially in the final. Disengage below about 50% HP and break line of sight behind prisms; regen kicks in after 5 s.
3. **Face whoever is hitting us** (the `damaged` event gives `bearing`). Rear hits cost ×1.5.
4. **Gun aim:** solve the intercept for a 252 m/s bullet from the muzzle to the target's center of mass using its `vel`. Fire only when the actual aim error is small relative to target size and range. Track reloads.
5. **Early cubes when nobody is near:** NPCs (2 cubes each), then crates (1 cube). Cubes stack HP and damage.
6. **Loot:** take a laser or lightning on the way when the detour is small. Carry a counter to what nearby enemies hold (the `weapons` list).
7. **Upgrades:** start with armor / plating / nanites (survival); gun_rate and gun_damage when fighting a lot.
8. **Late game:** sit near cover inside the circle, move under 5 m/s to stay silent, and let others fight. Shoot at sounds through trees.
9. **Supply drops:** only contest one if no enemy has been seen or heard nearby recently.

Open questions:
- How much should we fight in practice (good for the board) versus hide (good for the final)? Consider a mode switch on `alive` count and time.
- Is it worth building a local free-for-all simulator for self-play? The kit has none. The "Play" site is play.joinbuilders.org, and the "Watch" link shows replays.

---

## 7. Working rules for Claude Code in this repo

- Before every push: run both check.py commands in §4. Never push a bot that fails the Docker check.
- Keep act() fast: no per-tick full-map scans. Precompute grids (height map, building mask, coarse nav graph) in `__init__` from `/arena`. Profile the worst-case act() time.
- Never print to stdout except through the wrapper. Use `mf.log()` (stderr) for debugging.
- Don't hard-code config numbers. Read them from `hello["config"]`.
- Keep commits small and in history (no force-push). Write the score of each submitted commit in the commit message or a log.
- The repo is **public**: don't commit private notes, tokens, or anything we don't want other teams to read.

## 8. Status log
- Fri 7 PM: kit unpacked, protocol read, repo being created. v1 survival bot next.
- Fri night: v1 survival bot written (stdlib only). check.py PASS without Docker (max 2.4 ms); tools/scenarios.py 26/26. Docker Engine 29.1.3 installed in WSL Ubuntu (docker.io package); Docker check PASS (ready 634 ms, max answer 3.2 ms, image 176 MB). Not yet committed or submitted.
