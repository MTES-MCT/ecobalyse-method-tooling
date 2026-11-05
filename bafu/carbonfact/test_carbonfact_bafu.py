# /// script
# requires-python = ">=3.12"
# dependencies = ["pyvolca[plot]>=0.14", "openpyxl"]
# ///
"""Self-check of the mapping reader, of who decides a supplier, of how two versions pair and of what a merge keeps: no engine, no network.

    uv run test_carbonfact_bafu.py
"""

import tempfile
from pathlib import Path

import openpyxl

import carbonfact_bafu as cf

KETTLE = "electric kettle {GLO}| market for electric kettle"


def workbook(header: list, *rows: list) -> Path:
    """A one-sheet expert workbook, its header on row 2 as some editions put it."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Matching Full"
    ws.append(["a title above the table"])
    for row in (header, *rows):
        ws.append(row)
    path = Path(tempfile.mkdtemp()) / "mapping.xlsx"
    wb.save(path)
    return path


def test_columns_found_by_header() -> None:
    """A column added before BAFU's (the « bis » of 28 October) must not shift what is read,
    the ecoinvent "Geography" on the left must not be taken for BAFU's, and capitals in a title do not matter."""
    path = workbook(
        ["EI 3.11 dataset", "Identifiant", "Geography", "EI Unit", "bis", "BAFU Name", "Geography", "Unit", "Conversion"],
        [KETTLE, "", "GLO", "unit", 0, "Convector radiator", "CH", "kg", 1.5],
        ["dryer {GLO}| market for dryer", "", "GLO", "unit", 0, "Dryer", "#N/A", "", ""],
    )
    found = dict(cf.read_workbook_mapping(path))
    assert found[("market for electric kettle", "electric kettle", "GLO")] == cf.Target("Convector radiator", "CH", "kg", 1.5)
    assert found[("market for dryer", "dryer", "GLO")] is None, "a #N/A place names no BAFU process"


def test_two_candidate_columns_stop() -> None:
    """Two « BAFU Name » columns: reading whichever comes first would be a silent guess."""
    path = workbook(["EI 3.11 dataset", "BAFU Name", "BAFU Name", "Geography", "unit", "conversion"])
    try:
        cf.read_workbook_mapping(path)
    except SystemExit as stop:
        assert "two columns 'BAFU Name'" in str(stop)
    else:
        raise AssertionError("a doubled column was read")


def test_mapping_wins() -> None:
    """The mapping decides where it names a BAFU process; a choice written in the
    script fills only what it leaves without one (#N/A)."""
    kettle = cf.Supplier("market for electric kettle", "electric kettle", "GLO", "unit")
    dryer = cf.Supplier("market for dryer", "dryer", "GLO", "unit")
    drying = cf.Target("Technical wood drying, infrastructure", "RER", "Item(s)", 1)
    mapping = {
        ("market for electric kettle", "electric kettle", "GLO"): {None},
        ("market for dryer", "dryer", "GLO"): {drying},
    }
    by_hand = cf.read_complement(cf.COMPLEMENT, "built-in")
    assert cf.resolve(kettle, {}, mapping, by_hand).target.name == "Convector radiator"
    assert cf.resolve(dryer, {}, mapping, by_hand).target == drying


def test_dummy_drops_the_line() -> None:
    """« Dummy » is the mapping's word for an ecoinvent inventory empty in practice: the line
    is dropped, so it scores nothing, and no choice written in the script stands in for it."""
    chloride = "magnesium chloride, from titanium sponge production"
    path = workbook(
        ["EI 3.11 dataset", "EI Unit", "BAFU Name", "Geography", "unit", "conversion"],
        [f"{chloride} {{GLO}}| market for {chloride}", "kg", "Dummy", "#N/A", "#N/A", "#N/A"],
    )
    found = dict(cf.read_workbook_mapping(path))
    key = (f"market for {chloride}", chloride, "GLO")
    assert found[key] is cf.DUMMY
    supplier = cf.Supplier(f"market for {chloride}", chloride, "GLO", "kg")
    by_hand = {(f"market for {chloride}", "GLO"): cf.Target("Magnesium sulphate, at plant", "RER", "", 1)}
    assert cf.resolve(supplier, {}, {key: {cf.DUMMY}}, by_hand).target is cf.DUMMY
    header = ["name", "amount", "reference product", "location", "unit", "categories", "type", "comment", "database"]
    block = [
        ["Activity", "Finishing"], header,
        ["finishing", 1, "finishing", "GLO", "kg", None, "production", None, "own"],
        [f"market for {chloride}", 0.2, chloride, "GLO", "kg", None, "technosphere", None, "ecoinvent-3.12"],
        ["market for tap water", 3, "tap water", "GLO", "kg", None, "technosphere", None, "ecoinvent-3.12"],
    ]
    (kept,) = cf.by_activity([block], {}, frozenset({(f"market for {chloride}", "GLO")}))
    assert [row[0] for row in kept] == ["Activity", "name", "finishing", "market for tap water"]


def test_new_in_312() -> None:
    """A 3.12 dataset with no 3.11 predecessor is said so, not looked up under its own name."""
    s = cf.Supplier("market for something new", "something new", "GLO", "kg")
    back = {("market for something new", "something new", "GLO"): set()}
    assert cf.from_mapping(s, back, {}).status == "new in ecoinvent 3.12, no 3.11 dataset to map from"


def test_pairing_by_name_product_place() -> None:
    """Two versions pair activity by activity; what one side lacks is set apart, and a
    reference written in another unit stops the run, since a score is per one reference unit."""
    dye = ("Fabric dyeing", "dyed fabric", "GLO")
    left = {dye: {"unit": "kg", "Climate change": 2.0}, ("Only here", "x", "GLO"): {"unit": "kg", "Climate change": 1.0}}
    right = {dye: {"unit": "kg", "Climate change": 1.0}}
    l, r, only_left, only_right = cf.paired(left, right)
    assert (l, r) == ({dye: {"Climate change": 2.0}}, {dye: {"Climate change": 1.0}})
    assert (only_left, only_right) == ([("Only here", "x", "GLO")], [])
    try:
        cf.paired(left, {dye: {"unit": "m2", "Climate change": 1.0}})
    except SystemExit as stop:
        assert "different units" in str(stop)
    else:
        raise AssertionError("two units were compared")


def test_built_in_renames() -> None:
    """Without the correspondence file, the polypropylene market 3.12 splits by region is
    still read as the 3.11 one for the world, the only one the mapping knows."""
    rer = cf.Supplier("market for polypropylene, granulate", "", "RER", "kg")
    granulate = cf.Target("Polypropylene, granulate, at plant", "RER", "kg", 1)
    mapping = {("market for polypropylene, granulate", "polypropylene, granulate", "GLO"): {granulate}}
    assert cf.from_mapping(rer, cf.read_correspondence(None), mapping).target == granulate
    assert cf.from_mapping(rer, {}, mapping).target is None, "the check proves nothing"


def test_conversion_within_one_measure() -> None:
    """Urea « as N » is kg for kg: the engine would drop the mapping's 2.17, so the script divides the line.
    kWh to MJ the engine converts itself, and a piece to kg it converts by the mapping: both left to it."""
    units = {"kg": cf.UnitDefinition("mass", 1.0), "kWh": cf.UnitDefinition("energy", 3.6), "MJ": cf.UnitDefinition("energy", 1.0), "p": cf.UnitDefinition("count", 1.0)}

    def line(unit: str, target_unit: str, conversion: float) -> cf.Resolution:
        return cf.Resolution(cf.Supplier("x", "", "GLO", unit), "", cf.Target("y", "RER", target_unit, conversion), "mapped")

    assert abs(cf.scale_by_hand(line("kg", "kg", 2.17), units) - 1 / 2.17) < 1e-12
    assert abs(cf.scale_by_hand(line("kWh", "MJ", 1 / 3.6), units) - 1) < 1e-12
    assert cf.scale_by_hand(line("p", "kg", 1.2), units) == 1
    assert abs(cf.scale_by_hand(line("megajoule", "MJ", 0.599), units | {"megajoule": cf.UnitDefinition("energy", 1.0)}) - 1 / 0.599) < 1e-12


def dye(heat: list, amount: float) -> list:
    """A dye as the engine writes it in Brightway, its heat line given."""
    header = ["name", "amount", "reference product", "location", "unit", "categories", "type", "comment", "database"]
    return [
        ["Activity", "Azo dyes production"], ["reference product", "azo dyes"], ["location", "GLO"], ["Exchanges"], header,
        ["azo dyes", 1, "azo dyes", "GLO", "kg", None, "production", None, "own"],
        ["Tap water, at user", amount, "Tap water, at user", "RER", "kg", None, "technosphere", None, "bafu"],
        heat,
    ]


def test_merge_keeps_a_shared_activity_once() -> None:
    """Two steps copy one dye: kept once, even when one step was read back from ecoSpold 2, which
    names a line within the database by its product, with no place. Written two ways, it stops."""
    heat = ["Heat, for textile processes, production mix", 12.6, "heat, for textile processes", "GLO", "MJ", None, "technosphere", None, "own"]
    from_ecospold2 = ["heat, for textile processes", 12.6, "heat, for textile processes", None, "MJ", None, "technosphere", None, "own"]
    from_ecospold2_dye = dye(from_ecospold2, 0.5)
    from_ecospold2_dye[5][3] = None
    assert cf.merged("own", [("dyeing", [dye(heat, 0.5)]), ("printing", [from_ecospold2_dye])]) == [dye(heat, 0.5)]
    coal = ["Heat, production mix, coal", 12.6, "heat, for textile processes", "GLO", "MJ", None, "technosphere", None, "own"]
    for other in (dye(heat, 0.6), dye(coal, 0.5)):  # another amount, another maker of the same heat
        try:
            cf.merged("own", [("dyeing", [dye(heat, 0.5)]), ("printing", [other])])
        except SystemExit as stop:
            assert "written differently in dyeing and printing" in str(stop)
        else:
            raise AssertionError("two versions of one dye were merged")


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
            print(f"ok  {name}")
