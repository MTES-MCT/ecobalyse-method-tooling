# /// script
# requires-python = ">=3.12"
# dependencies = ["pyvolca[plot]>=0.14", "openpyxl"]
# ///
"""Self-check of the mapping reader, of who decides a supplier and of how two versions pair: no engine, no network.

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
        [KETTLE, "", "GLO", "unit", 0, "Convector radiator", "CH", "kg", 1.2],
        ["dryer {GLO}| market for dryer", "", "GLO", "unit", 0, "Dryer", "#N/A", "", ""],
    )
    found = dict(cf.read_workbook_mapping(path))
    assert found[("market for electric kettle", "electric kettle", "GLO")] == cf.Target("Convector radiator", "CH", "kg", 1.2)
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


def test_hand_choice_wins() -> None:
    """The workbook's automatic proposal for the kettle was a whole factory per appliance:
    a choice written in the script must win over it, and the workbook decide the rest."""
    kettle = cf.Supplier("market for electric kettle", "electric kettle", "GLO", "unit")
    iron = cf.Supplier("market for iron", "iron", "GLO", "kg")
    factory = cf.Target("Household devices production plant", "CH", "unit", 1)
    mapping = {
        ("market for electric kettle", "electric kettle", "GLO"): {factory},
        ("market for iron", "iron", "GLO"): {cf.Target("Iron, at plant", "RER", "kg", 1)},
    }
    by_hand = cf.read_complement(cf.COMPLEMENT, "built-in")
    assert cf.resolve(kettle, {}, mapping, by_hand).target.name == "Convector radiator"
    assert cf.resolve(iron, {}, mapping, by_hand).target.name == "Iron, at plant"


def test_new_in_312() -> None:
    """A 3.12 dataset with no 3.11 predecessor is said so, not looked up under its own name."""
    s = cf.Supplier("market for something new", "something new", "GLO", "kg")
    back = {("market for something new", "something new", "GLO"): set()}
    assert cf.from_mapping(s, back, {}).status == "new in ecoinvent 3.12, no 3.11 dataset to map from"


def test_pairing_by_name_product_place() -> None:
    """Two versions pair activity by activity; what one side lacks is set apart, and a
    reference written in another unit stops the run, since a score is per one reference unit."""
    dye = ("Fabric dyeing", "dyed fabric", "GLO")
    left = {dye: {"unit": "kg", "ECS": 2.0}, ("Only here", "x", "GLO"): {"unit": "kg", "ECS": 1.0}}
    right = {dye: {"unit": "kg", "ECS": 1.0}}
    l, r, only_left, only_right = cf.paired(left, right)
    assert (l, r) == ({dye: {"ECS": 2.0}}, {dye: {"ECS": 1.0}})
    assert (only_left, only_right) == ([("Only here", "x", "GLO")], [])
    try:
        cf.paired(left, {dye: {"unit": "m2", "ECS": 1.0}})
    except SystemExit as stop:
        assert "different units" in str(stop)
    else:
        raise AssertionError("two units were compared")


if __name__ == "__main__":
    for name, check in list(globals().items()):
        if name.startswith("test_"):
            check()
            print(f"ok  {name}")
