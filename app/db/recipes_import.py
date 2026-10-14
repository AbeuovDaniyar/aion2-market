"""Import every crafting recipe from a crafting-graph.json file Danny saved himself.

The file is gamers4.life's crafting graph (https://gamers4.life/aion-2/database/
crafting-graph.json). Their terms forbid automated access, so the app never
downloads it: Danny saves it in his browser and imports the file here.

Format (one entry per recipe id):
    {"p": profession, "qr": "light" | "dark" | None, "ml": mastery level, "gold": fee,
     "in": [[item_key, qty, alt], ...], "out": item_key, "outq": qty,
     "cp": COMBO chance in 1/10000, "combo": item_key or None}
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.db.repo import Database, utcnow
from app.external import models as m
from app.external.aion2exchange import read_cached

SOURCE = "gamers4.life crafting graph"
KINA_ITEM = 930100030  # "Kina (All)" as an ingredient: a fee, not a market item
# The graph's grade codes, checked against NCSOFT's dictionary grades.
GRADE_CODES = {11: "Common", 21: "Rare", 31: "Legend", 41: "Unique", 51: "Epic"}
# Faction -> the race key the graph uses.
RACES = {"Elyos": "light", "Asmodian": "dark"}


@dataclass(frozen=True)
class ImportResult:
    recipes: int
    skipped_other_race: int
    kept_own_rates: int
    skipped_empty: int
    professions: dict[str, int]


def estimated_rate(mastery_level: int | None, known: list[tuple[int, float]]) -> float | None:
    """A success rate for a recipe level, from the API model's (level, rate) pairs: linear
    between known levels, clamped at the ends. A rough default; Danny overrides it with
    the in-game Expected Probability."""
    if not known:
        return None
    known = sorted(known)
    if mastery_level is None:
        return sorted(r for _, r in known)[len(known) // 2]
    if mastery_level <= known[0][0]:
        return known[0][1]
    if mastery_level >= known[-1][0]:
        return known[-1][1]
    for (l0, r0), (l1, r1) in zip(known, known[1:]):
        if l0 <= mastery_level <= l1:
            return r0 if l1 == l0 else r0 + (r1 - r0) * (mastery_level - l0) / (l1 - l0)
    return known[-1][1]


def model_rates(db: Database, server_id: int) -> list[tuple[int, float]]:
    """(mastery level, success rate) pairs from the cached /api/crafts/model."""
    cached = read_cached(db, m.CraftModelInfo, "/api/crafts/model", server=server_id)
    if cached is None:
        return []
    pairs = {(s.mastery_level, s.success_rate) for p in cached.data.profiles for s in p.steps}
    return sorted(pairs)


def import_graph(db: Database, source: str | Path | dict, server_id: int) -> ImportResult:
    """Replace previously imported recipes with the graph's, for this server's race.
    source is a file path, the file's text (pasted), or the parsed dict.
    Rates Danny set himself on an imported recipe are kept."""
    if isinstance(source, dict):
        graph = source
    elif isinstance(source, str) and source.lstrip().startswith("{"):
        graph = json.loads(source)
    else:
        graph = json.loads(Path(source).read_text(encoding="utf-8"))
    if not isinstance(graph, dict):
        raise ValueError("this is not crafting-graph.json")
    recipes, items = graph.get("recipes"), graph.get("items", {})
    if not isinstance(recipes, dict) or not recipes:
        raise ValueError("this file has no 'recipes'; is it crafting-graph.json?")

    faction = db.conn.execute("SELECT faction FROM servers WHERE id = ?", (server_id,)).fetchone()
    race = RACES.get(faction["faction"]) if faction else None
    known = model_rates(db, server_id)
    now = utcnow().isoformat()

    # Rates Danny entered on imported recipes, by the graph's recipe id.
    own = {
        r["api_recipe_id"]: (r["success_rate"], r["rate_updated_at"])
        for r in db.conn.execute(
            "SELECT api_recipe_id, success_rate, rate_updated_at FROM recipes "
            "WHERE api_recipe_id IS NOT NULL AND rate_source = 'manual'"
        )
    }
    db.conn.execute(
        "DELETE FROM recipe_inputs WHERE recipe_id IN (SELECT id FROM recipes WHERE api_recipe_id IS NOT NULL)"
    )
    db.conn.execute("DELETE FROM recipes WHERE api_recipe_id IS NOT NULL")

    # Names for items that are not on this server's market (yet).
    db.conn.executemany(
        "INSERT INTO items (item_key, name) VALUES (?, ?) "
        "ON CONFLICT(item_key) DO UPDATE SET name = COALESCE(items.name, excluded.name)",
        [(int(k), v.get("n")) for k, v in items.items() if str(k).isdigit() and isinstance(v, dict)],
    )

    # The graph codes grades as numbers; learn the names from items whose grade the
    # official dictionary already gave us (item_icons), then fill in the rest.
    named = {r["item_key"]: r["grade"] for r in db.conn.execute(
        "SELECT item_key, grade FROM item_icons WHERE grade IS NOT NULL")}
    votes: dict[object, Counter[str]] = {}
    for k, v in items.items():
        if str(k).isdigit() and isinstance(v, dict) and int(k) in named and v.get("g") is not None:
            votes.setdefault(v["g"], Counter())[named[int(k)]] += 1
    grade_names = dict(GRADE_CODES)
    grade_names.update({code: c.most_common(1)[0][0] for code, c in votes.items()})
    db.conn.executemany(
        "UPDATE items SET grade = COALESCE(grade, ?) WHERE item_key = ?",
        [(grade_names[v["g"]], int(k)) for k, v in items.items()
         if str(k).isdigit() and isinstance(v, dict) and v.get("g") in grade_names],
    )

    imported = other_race = kept = skipped_empty = 0
    professions: Counter[str] = Counter()
    for recipe_id, r in recipes.items():
        if not str(recipe_id).isdigit() or not isinstance(r, dict) or not r.get("out"):
            continue
        if race and r.get("qr") and r["qr"] != race:
            other_race += 1
            continue
        api_id = int(recipe_id)
        level = r.get("ml")
        if api_id in own and own[api_id][0] is not None:
            rate, source, updated = own[api_id][0], "manual", own[api_id][1]
            kept += 1
        else:
            rate, source, updated = estimated_rate(level, known), "estimated", now
        inputs: dict[int, int] = {}
        kina_fee = int(r.get("gold") or 0)
        for line in r.get("in") or []:
            if not line or not str(line[0]).isdigit() or not line[1]:
                continue
            if int(line[0]) == KINA_ITEM:
                kina_fee += int(line[1])
                continue
            inputs[int(line[0])] = inputs.get(int(line[0]), 0) + int(line[1])
        if not inputs and kina_fee == int(r.get("gold") or 0) and not r.get("in"):
            skipped_empty += 1  # a COMBO or by-product entry, not a recipe you can craft
            continue
        for key in [int(r["out"])] + ([int(r["combo"])] if r.get("combo") else []) + list(inputs):
            db.conn.execute("INSERT OR IGNORE INTO items (item_key) VALUES (?)", (key,))
        cursor = db.conn.execute(
            "INSERT INTO recipes (output_key, output_qty, success_rate, combo_probability, "
            "combo_output_key, combo_output_qty, kina_cost, profession, mastery_level, "
            "rate_source, rate_updated_at, api_recipe_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(r["out"]), int(r.get("outq") or 1), rate, (r.get("cp") or 0) / 10000,
                int(r["combo"]) if r.get("combo") else None, None, kina_fee,
                r.get("p"), level, source, updated, api_id, SOURCE,
            ),
        )
        db.conn.executemany(
            "INSERT INTO recipe_inputs (recipe_id, item_key, qty, npc_price) VALUES (?, ?, ?, NULL)",
            [(cursor.lastrowid, key, qty) for key, qty in inputs.items()],
        )
        imported += 1
        professions[r.get("p") or "unknown"] += 1
    db.conn.commit()
    db.set_setting("recipes_imported_at", now)
    return ImportResult(imported, other_race, kept, skipped_empty, dict(professions))
