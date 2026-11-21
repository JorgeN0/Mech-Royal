"""Per-mech stats from mechreplay/1 files (recorded from the public watch stream).

usage: python tools/replay_stats.py FILE.jsonl [FILE2.jsonl ...] [--json out.json]
Prints one table per match and an aggregate by name. Frames repeated after a reconnect are de-duplicated by tick.
"""
import json
import math
import sys
from collections import defaultdict

W_KINDS = ["gun", "mortar", "laser", "lightning"]


def load(path):
    hdr, frames, seen = None, [], set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("type") == "header":
                if hdr is None:
                    hdr = o
                continue
            if "t" in o and "m" in o:
                if o["t"] in seen:
                    continue
                seen.add(o["t"])
                frames.append(o)
    frames.sort(key=lambda o: o["t"])
    return hdr, frames


def analyse(path):
    hdr, frames = load(path)
    if hdr is None or not frames:
        return None
    hz = float(hdr.get("tick_hz", 20))
    names = {m["id"]: m["name"] for m in hdr["mechs"]}
    S = {i: defaultdict(float) for i in names}
    for i in names:
        S[i]["name"] = names[i]
        S[i]["place"] = 0
        S[i]["death_t"] = None
    pos = {}       # id -> (x, y, z) at latest frame
    last = {}      # id -> previous (t, x, y)
    hp_at = {}
    dist_hits = defaultdict(list)  # id -> distances of its hits on mechs
    alive_n = len(names)
    held = {i: defaultdict(float) for i in names}  # weapon kind -> seconds held
    first_secondary = {}
    for fr in frames:
        t = fr["t"] / hz
        for m in fr["m"]:
            i, x, y, z = m[0], m[1], m[2], m[3]
            if i not in S:
                continue
            pos[i] = (x, y, z)
            hp_at[i] = m[6]
            flags = m[8]
            if i in last:
                lt, lx, ly = last[i]
                dt = t - lt
                if 0 < dt < 1.0:
                    sp = math.hypot(x - lx, y - ly) / dt
                    S[i]["move_s"] += dt
                    S[i]["dist_m"] += sp * dt
                    if sp < 5.0 and flags & 1:
                        S[i]["silent_s"] += dt
                    if flags & 2:
                        S[i]["boost_s"] += dt
                    if not flags & 1:
                        S[i]["air_s"] += dt
                    for sl in (m[11], m[12]):
                        if sl is not None and sl >= 0:
                            held[i][W_KINDS[sl]] += dt
                    for sl in (m[11], m[12]):
                        if sl is not None and sl > 0 and i not in first_secondary:
                            first_secondary[i] = (t, W_KINDS[sl])
            last[i] = (t, x, y)
            S[i]["cubes"] = max(S[i]["cubes"], m[9])
            S[i]["max_hp"] = max(S[i]["max_hp"], m[10])
        for e in fr.get("ev", ()):
            k = e.get("e")
            et = e.get("t", fr["t"]) / hz
            if k == "fire" and e.get("id") in S:
                S[e["id"]]["fire_" + e.get("w", "?")] += 1
            elif k == "laser" and e.get("id") in S:
                S[e["id"]]["fire_laser"] += 1
                if any(h in S for h in e.get("hits", ())):
                    S[e["id"]]["lhit"] += 1
            elif k == "zap" and e.get("id") in S:
                S[e["id"]]["fire_lightning"] += 1
            elif k == "hit":
                src, dst, amt, w = e.get("src"), e.get("dst"), float(e.get("amt", 0)), e.get("w", "?")
                if src in S and dst in S:
                    S[src]["dd"] += amt
                    S[src]["dd_" + w] += amt
                    S[src]["hits_" + w] += 1
                    if src in pos and dst in pos:
                        dist_hits[src].append(math.dist(pos[src][:2], pos[dst][:2]))
                if dst in S:
                    S[dst]["dt"] += amt
                    S[dst]["dt_" + w] += amt
                    if e.get("rear"):
                        S[dst]["dt_rear"] += amt
                    if src not in S:
                        S[dst]["dt_npc"] += amt
                if src in S and dst not in S:
                    S[src]["dd_pve"] += amt
            elif k == "death" and e.get("id") in S:
                i = e["id"]
                S[i]["place"] = e.get("place", alive_n)
                S[i]["death_t"] = et
                by = e.get("by")
                S[i]["killer"] = names.get(by, str(by))
                if by in S and by != i:
                    S[by]["kills"] += 1
                alive_n -= 1
            elif k == "upgrade" and e.get("id") in S:
                S[e["id"]].setdefault("ups", [])
                S[e["id"]]["ups"] = S[e["id"]].get("ups", []) + [e.get("up")]
            elif k == "weapon" and e.get("op") == "take" and e.get("by") in S:
                S[e["by"]]["takes"] = S[e["by"]].get("takes", []) + ["%s@%d" % (e.get("kind"), et)]
            elif k == "crate" and e.get("by") in S:
                S[e["by"]]["crates"] += 1
            elif k == "drop" and e.get("op") in ("got", "win", "claim", "captured") and e.get("by") in S:
                S[e["by"]]["drops"] += 1
    end_t = frames[-1]["t"] / hz
    for i in S:
        if not S[i]["place"]:
            S[i]["place"] = 1  # survivor (ranked by hp at the end; refine if needed)
            S[i]["death_t"] = end_t
        d = sorted(dist_hits[i])
        S[i]["hit_d_med"] = d[len(d) // 2] if d else None
        S[i]["held"] = dict(held[i])
        S[i]["first_sec"] = first_secondary.get(i)
    title = (hdr.get("showdown") or {}).get("title", "")
    return {"file": path, "title": title, "seed": hdr.get("seed"), "end_t": end_t, "mechs": S}


def acc(s, w):
    f = s.get("fire_" + w, 0)
    h = s.get("lhit", 0) if w == "laser" else s.get("hits_" + w, 0)
    return "%3d/%-3d" % (h, f) if f else "   -   "


def show(r):
    print("\n== %s  seed %s  %.0fs" % (r["title"], r["seed"], r["end_t"]))
    print("pl name                 die_t kills  dd    dt  rear%% npc  gun_acc  las_acc  lit_n mor_n hitd cubes  boost%% air%% silent%% secondary held (s)        upgrades")
    for i, s in sorted(r["mechs"].items(), key=lambda kv: kv[1]["place"]):
        life = max(1.0, s["death_t"] or 1)
        held = " ".join("%s%.0f" % (k[:3], v) for k, v in sorted(s["held"].items()) if k != "gun")
        print("%2d %-20s %5.0f %5d %5.0f %5.0f %4.0f %4.0f  %s  %s  %4d  %4d  %4s %4d  %5.0f %5.0f %6.0f  %-22s %s" % (
            s["place"], s["name"][:20], s["death_t"] or 0, s["kills"], s["dd"], s["dt"],
            100 * s["dt_rear"] / max(1, s["dt"]), s["dt_npc"], acc(s, "gun"), acc(s, "laser"),
            s.get("fire_lightning", 0), s.get("fire_mortar", 0),
            "%.0f" % s["hit_d_med"] if s["hit_d_med"] else "-", s["cubes"],
            100 * s["boost_s"] / life, 100 * s["air_s"] / life, 100 * s["silent_s"] / life,
            held[:22], ",".join(s.get("ups", []))[:40]))
    if "--takes" in sys.argv:
        for i, s in sorted(r["mechs"].items(), key=lambda kv: kv[1]["place"]):
            print("   %-20s takes %s  crates %d" % (s["name"][:20], " ".join(s.get("takes", [])), s["crates"]))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = None
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        args = [a for a in args if a != out]
    res = []
    for p in args:
        r = analyse(p)
        if r:
            show(r)
            res.append(r)
    if out:
        with open(out, "w") as f:
            json.dump(res, f, default=str)
    if len(res) > 1 or "--agg" in sys.argv:
        # top 6 finishers vs the rest, pooled over matches (showdowns are FFA among top teams)
        groups = {"top6": [], "7-16": []}
        for r in res:
            for s in r["mechs"].values():
                groups["top6" if s["place"] <= 6 else "7-16"].append(s)
        print("\n== pooled over %d matches" % len(res))
        for g, L in groups.items():
            if not L:
                continue
            n = len(L)
            life = [max(1.0, s["death_t"] or 1) for s in L]
            las = sum(1 for s in L if s["held"].get("laser", 0) > 0.5 * (s["death_t"] or 1)) / n
            lig = sum(1 for s in L if s["held"].get("lightning", 0) > 0.5 * (s["death_t"] or 1)) / n
            none = sum(1 for s in L if not any(k != "gun" for k in s["held"])) / n
            sil = sum(s["silent_s"] / l for s, l in zip(L, life)) / n
            dist = sum(s["dist_m"] / l for s, l in zip(L, life)) / n
            kills = sum(s["kills"] for s in L) / n
            hd = sorted(s["hit_d_med"] for s in L if s["hit_d_med"])
            ga = sum(s.get("hits_gun", 0) for s in L) / max(1, sum(s.get("fire_gun", 0) for s in L))
            la = sum(s.get("lhit", 0) for s in L) / max(1, sum(s.get("fire_laser", 0) for s in L))
            print("%-5s n=%3d kills %.2f  laser>half %.0f%%  lightning>half %.0f%%  no-secondary %.0f%%  silent %.0f%%  "
                  "speed %.1f m/s  hit dist med %s  gun acc %.0f%%  laser acc %.0f%%" % (
                      g, n, kills, 100 * las, 100 * lig, 100 * none, 100 * sil, dist,
                      "%.0f" % hd[len(hd) // 2] if hd else "-", 100 * ga, 100 * la))
