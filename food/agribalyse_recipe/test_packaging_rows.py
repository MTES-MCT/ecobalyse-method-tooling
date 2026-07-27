# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.8.0", "openpyxl"]
# ///
"""Self-check for the packaging row builder — no engine, no network.

    uv run test_packaging_rows.py

Fixtures are the real exchanges read off Agribalyse 3.2 for the chilled pizza in
a cardboard support with a plastic bag: the bag holds 9,8145 g of LDPE and
9,1 g of its end of life, per bag. A 450 g format means 1/0,45 = 2,2222 bags per
kg of pizza, so the sheet must show 21,81 g of LDPE.

The walk down a packaging stage is covered too: `fetch_activity` reads its cache
before it reads the client, so a pre-filled cache and no client at all is enough
to check the one division the whole bill hangs on.
"""

from dataclasses import dataclass, field

from extract_agribalyse_recipes import (
    packaging_amount, packaging_role, packaging_rows, stage_system)


@dataclass
class Exchange:
    """The four fields the row builder reads off a technosphere exchange."""
    flow_name: str
    amount: float
    unit: str
    target_process_id: str = ""
    is_reference: bool = False


@dataclass
class Detail:
    """The fields the stage walk reads off a fetched activity."""
    process_id: str
    activity_name: str
    classifications: dict
    product_amount: float
    technosphere_inputs: list = field(default_factory=list)


PREFIX = ["pizza_pid", "Pizza, vegetables or pizza 4 seasons, at plant {FR}", "FR", 1.0, "kg"]
SYSTEM = "Pizzas, chilled, 450g | Packaging System, N0, All, Cardboard support with plastic bag {FR} U"
BAGS_PER_KG = 1 / 0.45

BAG = [
    Exchange("Plastic, LDPE, Production {FR}  | CFF : production with R1=0%", 9.8145, "g"),
    Exchange("Transport, for all type of packaging, packaging production in EU, freight", 3.2388, "g"),
    Exchange("Plastic processing, Cast film extrusion, Gate to Gate {Mix (FR;RER)}", 9.8145, "g"),
    Exchange("Plastic finishing, Plastic pouch shaping, Gate to Gate {Mix (FR;RER)}", 263.16, "cm2"),
    Exchange("Plastic, LDPE waste (films), End of Life {FR} | CFF : EoL with R2 = 6,20 %", 9.1, "g"),
]


def test_role_keeps_materials_and_end_of_life_only():
    assert packaging_role("Plastic, LDPE, Production {FR}  | CFF : production") == "packaging_material"
    assert packaging_role("Plastic, LDPE waste (films), End of Life {FR}") == "packaging_eol"
    assert packaging_role("Transport, for all type of packaging, freight") is None
    assert packaging_role("Plastic processing, Cast film extrusion") is None
    assert packaging_role("Cardboard finishing, cutting and folding") is None
    # A material whose name happens to mention shaping stays a material.
    assert packaging_role("Cardboard, Corrugated, Production and shaping {FR}") == "packaging_material"


def test_role_ignores_the_library_prefix():
    """Names as SimaPro exports them, prefixed by the library they come from.

    Transport read as a material would count grams of freight as packaging mass.
    """
    assert packaging_role(
        "00_Pack_AGB_Lib |Transport, for all type of packaging, freight, lorry {RER}") is None
    assert packaging_role(
        "00_Pack_AGB_Lib |Plastic processing, Cast film extrusion, Gate to Gate") is None
    assert packaging_role(
        "00_Pack_AGB_Lib |Plastic, LDPE waste (films), End of Life {FR} |CFF : EoL with R2 = 6,20 %"
    ) == "packaging_eol"
    assert packaging_role(
        "00_Pack_AGB_Lib |Plastic, LDPE, Production {FR}  |CFF : production with R1=0%"
    ) == "packaging_material"


def test_grams_become_kilos_and_pieces_stay_pieces():
    assert packaging_amount(9.8145, "g") == (0.0098145, "kg")
    assert packaging_amount(0.86, "kg") == (0.86, "kg")
    assert packaging_amount(0.04, "p") == (0.04, "p")


def test_rows_scale_the_bill_and_drop_conversion_and_transport():
    rows = packaging_rows(PREFIX, SYSTEM, BAGS_PER_KG, BAG)
    assert [r[8] for r in rows] == ["packaging_material", "packaging_eol"]
    assert [r[:5] for r in rows] == [PREFIX, PREFIX]
    assert abs(rows[0][6] - 0.0098145 * BAGS_PER_KG) < 1e-15  # 21,81 g of LDPE per kg
    assert rows[0][7] == "kg"
    assert rows[0][10] == SYSTEM
    assert rows[0][9] == ""  # the material link is unresolved in the database
    assert len(rows[0]) == 11  # the ten recipe columns plus packaging_system


def test_no_material_gives_no_row():
    assert packaging_rows(PREFIX, SYSTEM, 1.0, []) == []
    assert packaging_rows(PREFIX, SYSTEM, 1.0, [BAG[1], BAG[2]]) == []


PACKAGING = {"Category": "Agricultural\\Food\\Packaging"}
RECIPE = {"Category": "Agricultural\\Food\\Recipes"}


def packaging_stage(*inputs) -> dict:
    """A stage packing 1 kg of pizza in a 450 g system, plus whatever is asked."""
    cache = {
        "system_pid": Detail("system_pid", SYSTEM, PACKAGING, 0.45),
        "pizza_pid": Detail("pizza_pid", "Pizza, at plant {FR}", RECIPE, 1.0),
        "stage_pid": Detail("stage_pid", "Pizza | at packaging {FR}", PACKAGING, 1.0,
                            list(inputs)),
    }
    return cache


def test_stage_divides_by_the_food_it_packs():
    cache = packaging_stage(
        Exchange(SYSTEM, 1.0, "kg", "system_pid"),
        Exchange("Pizza, at plant {FR}", 1.0, "kg", "pizza_pid"),
    )
    system, per_food = stage_system(None, cache, "stage_pid", "pizza_pid")
    assert system is cache["system_pid"]
    assert abs(per_food - BAGS_PER_KG) < 1e-12  # 2,2222 systems per kg of pizza


def test_an_extra_input_does_not_become_the_divisor():
    """The stage names the recipe it packs; anything else is not the food."""
    cache = packaging_stage(
        Exchange(SYSTEM, 1.0, "kg", "system_pid"),
        Exchange("Pizza, at plant {FR}", 1.0, "kg", "pizza_pid"),
        Exchange("Electricity, low voltage {FR}", 0.5, "kWh", "electricity_pid"),
    )
    cache["electricity_pid"] = Detail(
        "electricity_pid", "Electricity", {"Category": "Energy"}, 1.0)
    _, per_food = stage_system(None, cache, "stage_pid", "pizza_pid")
    assert abs(per_food - BAGS_PER_KG) < 1e-12  # not 1/0,5/0,45 = 4,44


def test_a_stage_packing_nothing_gives_nothing():
    cache = packaging_stage(Exchange("Fruit, at plant {FR}", 1.0, "kg", "pizza_pid"))
    assert stage_system(None, cache, "stage_pid", "pizza_pid") is None


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
            print(f"ok  {name}")
    print("all good")
