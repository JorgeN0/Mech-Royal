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
sim/                # local simulator: engine.py (rules), housebot.py (practice squad), run.py, trace.py
sim/bots/           # opponents/baselines: kit example_bot.py, v1_bot.py (snapshot of 81fef72)
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
- Fri night: v1 survival bot written (stdlib only). check.py PASS without Docker (max 2.4 ms); tools/scenarios.py 26/26. Docker Engine 29.1.3 installed in WSL Ubuntu (docker.io package); Docker check PASS (ready 634 ms, max answer 3.2 ms, image 176 MB).
- v1 `81fef72` practice score **3.38** (23rd of 57; leader 5.17, baseline 1.15).
- v2: commit to winning 1v1s (retreat only <28% HP, or <50% vs 2+ enemies / a healthier enemy), finish low-HP targets, ambush (stand still, pre-aimed) when the target is behind cover, wider early weapon search, loot wrecks when nobody is within 110 m, no NPC/crate fights while hurt, tighter gun fire gate. LOS verified against real hits (tools/losdiag.py: 8/9 agree). Commit `4d6cade`, not yet submitted.
- Simulator built (`python -m sim.run --mode practice --bots bot.py sim/bots/v1_bot.py --n 96`). Calibration: kit example 1.29 (real 1.15), v1 3.10 (real 3.38). Sim says v2 ≈ v1 (3.12). `--bots bot.py:key=val` overrides bot.PARAMS.
- Sim findings: holding a laser ≈ 5.0 vs gun-only ≈ 2.0; we picked fights with idle house bots; NPC fire derailed loot trips.
- v3: fight only threats (attackers, <50 m, aimed at us or closing, or nearly dead), remember pickups + 360° scans, boost to first weapon, grab a weapon mid-fight, kill attacking drones, laser swaps up to 150 m, fight at 100 m. Sim (fresh seeds, n=96): v3 3.66 vs v1 2.75. Pushed `6479a88`.
- FFA sim (`--mode ffa`, opponents incl. `solo:expert` aggressive stand-ins): v3 10.1 vs v1 8.5, example 6.1, solo-expert 5.3. Tactic knobs (engage range, pick_fights, opportunist, mortar on/off) are all within noise in FFA; picking fights is worse. Differences under ~0.3 need n≥700 bot-matches to trust. No practice/final mode switch justified yet. `python -m sim.ffatrace --seed N` traces one of our bots in FFA.
- Tested and rejected (sim, large n): faster strafe jinks, boost-strafing in fights (+0.27 at first, then -0.09 on new seeds: noise), offense-first upgrades, keeping gun lead aim while the laser charges.
- Sim ~3x faster (stand-in bots get light observations, per-tick target cache); practice runs now print paired-by-seed differences with a verdict.
- v4 = v3 play + telemetry: every 5 s the debug drawing gets a text line `TM t.. a(alive) k(kills) s(fire decisions) h(hits) dd(dmg dealt) c(cubes) <secondary> <dmg taken by weapon>`. Download our debug files after real matches to see how we die. Pushed `bbcbe0d`.
- **Target: real practice score ≥ 5.30** (leader was 5.17).
- Sim fidelity: tree canopy/trunks now block sight (sim/trees.py); house bots shoot back at drones (they were dying to drones and inflating everyone's score). Recalibrated squad (wider aim noise, escalation +1 hunter per 2 kills): kit example 1.43 (real 1.15), v1 3.64 (real 3.38). In this sim v4 ≈ 4.0.
- Tested, no gain: chasing supply drops (aggressive = worse), hiding to heal between waves, kiting at long range.
- v5: **farm remembered drones/crates within 250 m** (+0.8 in practice sim; cubes 4.6 → 6.8 at death), focus the wounded (tgt_hp_w 200, +0.17). Both hurt in FFA, so a **free-for-all detector** (deaths not caused by us: ≥2 before 120 s or ≥4 any time) switches to FFA_PARAMS (no farming, old targeting). Detector: fires for 85% of FFA bots (median 17 s), 4% false positives in practice. Sim: practice 4.31–4.40 vs v4 3.94 (+0.4); FFA 7.61 vs 7.39 (neutral). Pushed `2c7e167`.
- **v5 real practice score: 4.28** (sim predicted 4.3–4.4: the calibrated sim tracks the real board). Need ≈ +1.0 for 5.30.
- Media/TV research → fight-from-cover ideas. Hull-down rejected (only 0.6% of open cells have 4.9–6.6 m cover). Shoot-and-scoot (duck behind cover while reloading), reload-while-hidden, anti-flank (step away from attackers on opposite sides) implemented as PARAMS; first sim pass +0.39 / +0.10 / +0.13, but on fresh seeds (n=192) −0.10 / combos −0.2, FFA neutral → noise, left OFF (code kept).
- Re-sweep of 9 knobs in the calibrated sim: all flat (±0.1); retreating earlier when outnumbered is worse (−0.26). Knobs are at their optimum; gains need new abilities.
- Score by weapon held (sim): **laser 6.1**, mortar ~2.7–4.3, lightning ~2.5–3.0, gun only ~1.3–1.9. A laser alone is above the 5.30 target.
- v6: laser_bonus 150 (prefer a laser over a nearer lightning/mortar by up to ~300 m): paired **+0.35 ± 0.12**; FFA 7.60 vs 7.40. Tested and worse: laser_bonus 300 (−0.06), lightning held at gun range (−0.21), never using the mortar (−0.30). Sim per-weapon hit rate: gun 79%, laser 63%, lightning 85%, mortar 78%. Pushed `792fd14`.
- Apex Legends movement techs (sim/bots/apex_bot.py, a copy of bot.py + knobs): jump-strafe juke, ADAD stutter stops, jet-hop dodge of charging lasers. Practice: juke +0.14 ± 0.21 (noise), stutter −0.21; FFA: juke 7.69 vs 7.61 (noise), laser dodge 7.27 (worse). Not merged. Why: direction changes take ~0.4 s (accel 58.5), a 2.75 m hop doesn't leave a 9 m hitbox, lasers are hitscan, and air control is weak. Noise floor: two identical bots differ by ±0.17 paired at n=192 (A* time slices make runs non-repeatable).
- Rooftop anchor merged in `72be390` (off by default); see HANDOFF-rooftop-anchor.md. v6 real score: 4.27 per the handoff (Jorge recalled ~4.8). Leaderboard Sat midday: 262 teams, top 6.31, top-50 floor 5.15; we are outside the top 32 that qualify for the final.
- v7: (1) search budget 28 → 14 ms: acts > 20 ms in the sim went from 0.8% of decisions to ~0 (the real server drops answers > 50 ms; the 1-CPU box is slower than ours). (2) **laser hunt** (`hunt`): with no laser, early and calm, explore unseen ground in ~120 m hops and scan on arrival; walk up to 300 m to a remembered laser. Paired +0.20 ± 0.17 then **+0.46 ± 0.21** on fresh seeds; FFA 7.53 vs 7.47. Tested, no gain: laser_tol 1.2 (−0.10), laser_lead 0.12 (−0.03), laser-first upgrades (+0.01), late anchor (anchor_t 250 + farming, −0.07: we rarely live to 250 s).
- **Real board (Sat 12:30): v3 4.17, v5 4.25, v6 4.27 and 4.40 (same commit twice: noise ±0.15), v7 4.13.** Rank 105/265; top-32 cutoff 5.38; leader 6.31. Sim gains since v3 (+0.5–1.0 in sim) have NOT transferred. "wait N ms" on the submissions list is queue wait, not answer time. No debug-file download is offered in the UI.
- **New method: real-board experiments** on branches (submit by commit hash), each one bold change from v7, to learn which direction the real game rewards:
  - exp/roof `80574b5` anchor=1, anchor_laser=0 (climb a roof early, hold)
  - exp/aggro `7e271f9` pick_fights=1, engage_r=200
  - exp/noloot `84f6f57` farm_r=0, hunt=0, laser_bonus=35
  - exp/range `77b90fe` pref_gun=180
  - exp/hide `80c2291` passive: engage_r 70, close_r 40, no farming/hunt/opportunism, plating first
  - exp/cover `15c4149` scoot=1, hidden_reload=1, anti_flank=1.2 (fight from cover)
  - exp/lightning `1c03f6b` LOOT_RANK lightning 3 > laser 2 (lightning shorts lasers: counter if real bots carry lasers)
  - **Results: exp/roof 4.04, exp/aggro 3.88** (v7 4.13, v6 4.27/4.40). Both strategy extremes are worse → the strategy axis is near its optimum; the gap is fight quality vs the real house bots. Skipped hide/noloot/range for now.
  - play.joinbuilders.org joins a live server match over WebSocket; house-bot logic is server-side (not in the client bundle). Playing a round there is the way to observe real house bots.
- **Jorge's human play on play.joinbuilders.org** (FFA vs bots named expert/astra/skirmish/red/crimson, 3 each): 2nd place by playing aggressive up close, third-partying fights, backing off when taking damage, boosting/jetting (esp. upward) when under fire because bots mostly miss moving targets; died to a 1v1 from fairly far. Prefers lightning over the laser for close combat; rooftops not worth it.
- **Sim calibration vs 8 real scores** (`sim/calibrate.sh`, snapshots in sim/bots/v3..v7_bot.py): K0 = current house bots (leading aim) fits best: RMSE 0.40, corr 0.85 (overrates aggro 4.45 vs 3.88, underrates roof 3.47 vs 4.04). Lagging/no-lead house bots (K1/K3) predict 6–7 for bots that really score ~4 → against our bot the real house bots aim well. K2 (lag + lasers) RMSE 0.60.
- Human-style knobs added (all default off): hit_backoff_s, jet_evade_s/_e, ambush_move, loot_strafe, tp_seek (third-party: move toward heard fights), lightning_first (rank lightning > laser; hunt seeks lightning).
- **Real board Sat 4:10–4:19 PM (each from v7 4.13):** **exp/cover 4.56 (new best, entered)**, exp/human 4.27, exp/backoff 4.15, exp/lightning 3.85. The sim predicted cover ≈ 0, human −1.20, backoff +0.24, lightning −0.77, so it gets the direction wrong on fight-exposure tactics. It still agrees that lightning_first hurts. Reading: the real house bots punish exposure (reloading in the open, being flanked), so hiding during reloads pays. Human minus lightning may be ≈ +0.4.
- v8 (main) = v7 + cover tactics on by default (scoot 1, hidden_reload 1, anti_flank 1.2), plus new knob scoot_hit_hp (off: when hit while below this HP fraction, duck into cover within scoot_r). Next real tests stack on v8: exp/cover-human (human style minus lightning), exp/cover-hit (scoot_hit_hp 0.6, scoot_r 40), exp/cover-move (jet_evade + ambush_move + loot_strafe only).
- **Real: v8 `9927b0d` 4.48** (cover replicated: 4.56/4.48, mean 4.52 vs v7 4.13), **exp/cover-human 4.27** (worse than v8; the human style scored 4.27 both from v7 and from v8, so it stays out). New knob cover_tether (off): in a visible fight, drift to stay within N m of the nearest hiding spot, so reload ducks are short.
- **Real match data:** the public broadcast `wss://watch.joinbuilders.org/live` streams full `mechreplay/1` replays (header with names, weapons, pickups, zone; 10 Hz frames of every mech; events fire/hit/death/upgrade/weapon take/laser/zap). It airs Top-16 showdowns (top teams' bots, FFA), real-player lobbies, and maybe house matches. Our own practice runs are not listed. Recorder script: scratchpad recorder.py (not in repo); analysis: `python tools/replay_stats.py FILE.jsonl --takes`.
- First Top-16 showdown match: 12/16 had a laser or lightning by 40 s (most at 0–9 s); all 6 survivors at 186 s held lasers; survivors were silent (<5 m/s on the ground) 75–98% of the time; defensive upgrades (nanites/armor/plating); bots that never got a secondary placed 14–16.
- Sim (n=96, v8 base): cover_tether 14 +0.31 ± 0.25, heal_hp 0.6 +0.26 ± 0.22, retreat_hp 0.4 −0.14 (all n.s.).
- sentinel knob (off): once we hold a laser (or after sentinel_t 100 s), no drone/crate farming or hunting: only cubes and nearby weapons, then hold a spot by a building; fights with the laser at >50 m strafe at 35% speed. Modeled on the top-16 showdown bots (laser, mostly stationary, few trips). Sim: practice −0.08 ± 0.29 (with pref_gun 80), FFA 7.95 vs 8.04: neutral. Real test: exp/sentinel.
- **Real (v8 base): exp/heal 4.54, exp/tether 4.25, exp/cover-hit 4.25, exp/sentinel 4.10** (v8 4.48/4.56). Nothing beats v8; standing still (sentinel) is worse in practice even though the showdown top bots do it in FFA.
- **House bots measured** (broadcast "Real players" lobbies run the server's house AI: 3 each of Skirmish = house:probe-astra-skirmish, Astra = probe-astra-finalist, Red = probe-red-best, Expert = expert, Crimson = probe-red-honest; 15 = the practice squad size). `tools/aim_stats.py`, `tools/lead_test.py`:
  - they move 95–99% of the time at ~10.5 m/s; Red/Crimson/Expert change heading every 0.6 s, Skirmish/Astra every 1.2–1.4 s;
  - gun accuracy vs each other at 40–80 / 80–130 / >130 m: Red 83/38/7%, Expert 72/41/12%, Crimson 63/48/7%, Astra 45/22/1%, Skirmish 34/18/3%; lasers carried, 15–27% laser hits; they spam the gun beyond 130 m;
  - their accuracy matches perfect velocity lead → **they lead-aim**. The sim's easy/medium bots do not lead (why the sim rejected faster jinks).
  - perfect lead vs OUR movement physics (accel 58.5): current strafe flips 0.9–2.2 s → 78/68/62% hittable at 60/80/100 m; flips 0.4–0.8 s → 45/29/20%; standing still 100%.
  - our perfect lead vs THEIR movement: 92/83/75/65/48/36% at 40/60/80/100/120/150 m.
- weave knob (off): zigzag runs (retreat/scoot/evade/weapon/travel/...) while an enemy sees us, on the strafe timer. Experiments: exp/jink (strafe 0.4–0.8 s + weave 0.8), exp/jink-range (+ pref_gun 120).
- Sim with all house levels leading (--set easy.lead=1 medium.lead=1): exp/jink +0.55 ± 0.35, jink-range +0.41; old sim +0.18 / +0.07. Follow-ups ready: exp/jink-heal (jink + heal_hp 0.6), exp/jink-fast (flips 0.3–0.6 s, weave 1.0).
