"""Importing every recipe from a crafting-graph.json file the user saved."""

from __future__ import annotations

import json

import pytest

from app.analysis import service
from app.db.recipes_import import estimated_rate, import_graph
from app.external.scheduler import refresh_once
from tests.conftest import ODYLE, SERVER

REFINING_STONE = 610530001


def graph() -> dict:
    """The file's shape, with a few made-up recipes."""
    return {
        "items": {
            str(ODYLE): {"n": "Odyle", "g": 11},
            "999000001": {"n": "Green Thing", "g": 21},
            "999000002": {"n": "Yellow Thing", "g": 41},
            "999000003": {"n": "Dark Only", "g": 21},
        },
        "recipes": {
            "1": {"p": "alchemy", "qr": "light", "ml": 5, "gold": 100,
                  "in": [[str(ODYLE), 2, None], [str(REFINING_STONE), 1, None], [str(ODYLE), 1, "777"]],
                  "out": "999000001", "outq": 2, "cp": 2500, "combo": "999000002"},
            "2": {"p": "cooking", "qr": None, "ml": 35, "gold": 0,
                  "in": [[str(ODYLE), 1, None]], "out": "999000002", "outq": 1, "cp": 3000, "combo": None},
            "3": {"p": "alchemy", "qr": "dark", "ml": 5, "gold": 0,
                  "in": [[str(ODYLE), 1, None]], "out": "999000003", "outq": 1, "cp": 0, "combo": None},
        },
    }


@pytest.fixture
def filled(db, source, clock, tmp_path):
    refresh_once(db, source, clock)  # 11304 is an Elyos server: race "light"
    source.crafts_model(SERVER)
    db.conn.executemany(
        "INSERT INTO item_icons (item_key, file, grade, checked_at) VALUES (?, NULL, ?, '')",
        [(ODYLE, "Common"), (REFINING_STONE, "Common")],
    )
    db.conn.execute("UPDATE items SET grade = 'Common' WHERE item_key IN (?, ?)", (ODYLE, REFINING_STONE))
    db.conn.commit()
    path = tmp_path / "crafting-graph.json"
    data = graph()
    data["items"]["999000004"] = {"n": "Learned Green", "g": 21}
    path.write_text(json.dumps(data), encoding="utf-8")
    return db, clock, path


def test_estimated_rate_interpolates_and_clamps():
    known = [(5, 0.93), (20, 0.95), (35, 0.97)]
    assert estimated_rate(5, known) == 0.93
    assert estimated_rate(12.5, known) == pytest.approx(0.94)
    assert estimated_rate(1, known) == 0.93 and estimated_rate(115, known) == 0.97
    assert estimated_rate(None, known) == 0.95 and estimated_rate(10, []) is None


def test_import_keeps_own_faction_and_merges_inputs(filled):
    db, _, path = filled
    result = import_graph(db, path, SERVER)
    assert (result.recipes, result.skipped_other_race) == (2, 1)
    assert result.professions == {"alchemy": 1, "cooking": 1}
    recipes = {r["api_recipe_id"]: (r, i) for r, i in db.recipes()}
    first, inputs = recipes[1]
    assert first["output_qty"] == 2 and first["combo_probability"] == 0.25 and first["kina_cost"] == 100
    assert first["rate_source"] == "estimated" and first["success_rate"] == pytest.approx(0.93121326)
    assert {(i["item_key"], i["qty"]) for i in inputs} == {(ODYLE, 3), (REFINING_STONE, 1)}
    assert db.item_names([999000001]) == {999000001: "Green Thing"}


def test_import_again_replaces_but_keeps_your_rates(filled):
    db, _, path = filled
    import_graph(db, path, SERVER)
    recipe_id = next(r["id"] for r, _ in db.recipes() if r["api_recipe_id"] == 2)
    db.conn.execute("UPDATE recipes SET success_rate = 0.5, rate_source = 'manual' WHERE id = ?", (recipe_id,))
    db.save_recipe({"output_key": ODYLE, "success_rate": 0.9}, [])  # one of your own
    result = import_graph(db, path, SERVER)
    assert result.kept_own_rates == 1
    rows = db.recipes()
    assert len(rows) == 3  # two imported, one of yours untouched
    assert next(r for r, _ in rows if r["api_recipe_id"] == 2)["success_rate"] == 0.5


def test_grades_are_learned_and_rows_are_valued(filled):
    db, clock, path = filled
    import_graph(db, path, SERVER)
    views = service.item_views(db, SERVER, clock.at)
    rows = {r.name: r for r in service.recipe_rows(db, views)}
    green = rows["Green Thing"]
    # Code 21 has no dictionary item to learn from, so the built-in code table names it.
    assert green.grade == "Rare" and green.profession == "alchemy" and green.imported
    assert green.materials == pytest.approx(3 * 1185 + 1 * 1700)
    assert "not_listed_here" in green.flags and "estimated_rate" in green.flags


def test_not_a_graph_file_is_refused(db, tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        import_graph(db, bad, SERVER)


# Entries copied from the real crafting-graph.json.
REAL = {
    "items": {
        "110230015": {"n": "True Dragon Lord Longsword", "g": 41},
        "110230096": {"n": "Splendent True Dragon Lord Longsword", "g": 41},
        "110220006": {"n": "Splendent Noble Dragon Lord Longsword", "g": 51},
        "510616001": {"n": "Mela Juice", "g": 11},
        "510616003": {"n": "Artisan's Mela Juice", "g": 21},
        "510616005": {"n": "Raydam Juice", "g": 11},
        "610680045": {"n": "Mela", "g": 11},
        "630554001": {"n": "Sugar (Bound)", "g": 11},
        "930100030": {"n": "Kina (All)", "g": 11},
    },
    "recipes": {
        "131012001": {"p": "blacksmithing", "qr": "light", "mg": "intermediate", "ml": 95, "gold": 0,
                      "in": [], "out": "110220006", "outq": 1, "cp": 0, "combo": None},
        "111013011": {"p": "blacksmithing", "qr": "light", "mg": "intermediate", "ml": 55, "gold": 4000,
                      "in": [["110230015", 1, "111013001"], ["930100030", 1000000, None]],
                      "out": "110230096", "outq": 1, "cp": 0, "combo": None},
        "514075001": {"p": "cooking", "qr": "light", "mg": "beginner", "ml": 25, "gold": 800,
                      "in": [["610680045", 5, None], ["630554001", 3, None]],
                      "out": "510616003", "outq": 1, "cp": 3000, "combo": "510616003"},
        "534075001": {"p": "cooking", "qr": "light", "mg": "beginner", "ml": 25, "gold": 0,
                      "in": [], "out": "510616003", "outq": 2, "cp": 0, "combo": None},
        "524076001": {"p": "cooking", "qr": "dark", "mg": "beginner", "ml": 1, "gold": 200,
                      "in": [["610680051", 1, None], ["630554001", 1, None]],
                      "out": "510616005", "outq": 1, "cp": 3000, "combo": "510616005"},
    },
    "byCombo": {"110220006": "111012001", "510616003": "514075001"},
}


def test_real_entries_import_correctly(db):
    db.conn.execute("INSERT INTO servers (id, api_server_id, name, faction) VALUES (12107, 12107, 'Ereshkigal', 'Asmodian')")
    db.conn.execute("INSERT INTO servers (id, api_server_id, name, faction) VALUES (11304, 11304, 'X', 'Elyos')")
    result = import_graph(db, json.dumps(REAL), 11304)  # pasted text works too
    assert (result.recipes, result.skipped_other_race, result.combo_attached) == (2, 1, 2)
    recipes = {r["api_recipe_id"]: (r, i) for r, i in db.recipes()}
    upgrade, inputs = recipes[111013011]
    assert upgrade["kina_cost"] == 1_004_000  # the Kina ingredient is a fee
    assert [(i["item_key"], i["qty"]) for i in inputs] == [(110230015, 1)]
    assert upgrade["output_grade"] == "Unique"  # code 41: yellow
    juice, _ = recipes[514075001]
    assert juice["output_grade"] == "Rare" and juice["combo_probability"] == 0.3  # green
    assert juice["combo_output_qty"] == 2  # a cooking COMBO yields two
    rows = {r.recipe_id: r for r in service.recipe_rows(db, [])}
    row = rows[juice["id"]]
    assert (row.combo_name, row.combo_qty) == ("Artisan's Mela Juice", 2)

    result = import_graph(db, REAL, 12107)  # Asmodian: the dark recipe instead
    assert {r["api_recipe_id"] for r, _ in db.recipes()} == {524076001}
