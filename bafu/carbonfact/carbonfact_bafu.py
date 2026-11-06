# /// script
# requires-python = ">=3.12"
# dependencies = ["pyvolca[plot]>=0.14", "openpyxl"]
# ///
"""Put a Carbonfact database on BAFU, compare its scores on both, check it, and merge several into one.

Carbonfact ships one Brightway workbook per process step, each naming its
suppliers in ecoinvent 3.12 cut-off and none naming another step's. The first
three commands work on one such database, so a new one is handled the same way:

    relink   writes the database on BAFU: each ecoinvent supplier replaced by
             its BAFU equivalent, through an ecoinvent 3.11 to BAFU mapping;
    compare  scores two versions of a database, on their own background each,
             and draws one against the other;
    quality  writes the engine's structural quality report of a database;
    merge    writes several databases as one, each activity they share once.

    uv run carbonfact_bafu.py relink Dyeing_Inventory_Brightway.xlsx --mapping mapping.xlsx \\
        --ecoinvent "ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "BAFU-2026 v1_ecoSpold v1.zip"
    uv run carbonfact_bafu.py compare Dyeing_Inventory_Brightway.xlsx Dyeing_Inventory_Brightway.bafu.brightway.xlsx \\
        --ecoinvent "ecoinvent 3.12_cutoff_ecoSpold02.7z" --bafu "BAFU-2026 v1_ecoSpold v1.zip" \\
        --method EF-v3.1.zip
    uv run carbonfact_bafu.py quality Dyeing_Inventory_Brightway.bafu.brightway.xlsx
    uv run carbonfact_bafu.py merge Dyeing_Inventory_Brightway.bafu.brightway.xlsx Printing_Inventory_Brightway.bafu.ecospold2.zip \\
        --bafu "BAFU-2026 v1_ecoSpold v1.zip" --format ecospold2

The engine is downloaded on first run. VOLCA_BINARY with VOLCA_DATA_DIR runs a
local engine build and its data instead.
"""

import argparse
import csv
import io
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import pairwise, takewhile
from pathlib import Path
from typing import IO

import openpyxl
import volca
from volca import Client, DatabaseStatus, Server, UnitDefinition, VoLCAError
from volca.compare_plot import BANDS, Point, band_shares, fold, scatter_grid, widest

NEEDS = (0, 16, 0)  # keeps a mapping's links through finalize, exports under the linked supplier
ECOINVENT, COLLECTION = "ecoinvent-3.12", "method"  # the collection --method loads under, whatever method it is
EXTENSION = {"brightway": "xlsx", "simapro": "csv", "ecospold2": "zip", "openlca": "zip", "ilcd": "zip"}
CLIMATE = "Climate change"  # its name in EF 3.1, adapted or the JRC's: what --reported checks
# "polypropylene, granulate {RER}| polypropylene production, granulate | Cut-off, U"
SIMAPRO_NAME = re.compile(r"^(?P<product>.*?) \{(?P<geo>.+?)\}\s*\|\s*(?P<activity>.*?)(\s*\|.*)?$")

# BAFU suppliers chosen by hand, in mapping.csv's columns, for what the mapping
# leaves without a BAFU process: the README says how strong each one is.
COMPLEMENT = """source_product,source_location,target_product,target_location,conversion,source_unit,target_unit
market for alkyl sulfate (C12-14),GLO,"Fatty alcohol sulfate, mix, at plant",RER,,,
"market for chemical, organic, unspecified",GLO,"Chemicals organic, at plant",GLO,,,
market for polytetrafluoroethylene,GLO,"Tetrafluoroethylene, at plant",RER,,,
market for dryer,GLO,"Washing machine, at retailer",CH,,,
market for electric kettle,GLO,Convector radiator,CH,1.5,unit,kg
market for tissue paper,GLO,"Paper, woodfree, uncoated, at regional storage",RER,,,
market for waste mineral oil,RoW,"Disposal, used mineral oil, 10% water, to hazardous waste incineration",CH,,,
market for wastewater from textile production,RoW,"Treatment, sewage, to wastewater treatment, class 3",CH,,,
market for cationic resin,RoW,"Cationic resin, at plant",CH,,,
"""

# ecoinvent 3.12 suppliers the mapping knows under another 3.11 dataset, as
# ecoinvent's correspondence file gives them: --correspondence replaces these.
# 3.12 splits these markets by region where 3.11 had one for the world.
CORRESPONDENCE = """activity,product,location,activity_311,product_311,location_311
"market for polyethylene, high density, granulate","polyethylene, high density, granulate",RER,"market for polyethylene, high density, granulate","polyethylene, high density, granulate",GLO
"market for polypropylene, granulate","polypropylene, granulate",RER,"market for polypropylene, granulate","polypropylene, granulate",GLO
"market for polypropylene, granulate","polypropylene, granulate",RoW,"market for polypropylene, granulate","polypropylene, granulate",GLO
"""


def norm(text) -> str:
    return " ".join(str(text or "").split()).lower()


@dataclass(frozen=True)
class Supplier:
    """An ecoinvent supplier as a workbook row names it."""

    activity: str
    product: str  # empty when the row gives only the activity, as Brightway allows
    location: str
    unit: str


@dataclass(frozen=True)
class Target:
    name: str
    location: str
    unit: str
    conversion: object


# what the mapping writes for an ecoinvent inventory that is empty in practice: the line is dropped, it scores nothing
DUMMY = Target("Dummy", "", "", None)


@dataclass(frozen=True)
class Resolution:
    supplier: Supplier
    ecoinvent_311: str
    target: Target | None
    status: str
    opposite: bool = False  # the BAFU process writes its reference with the other sign




# -- the Carbonfact workbook --------------------------------------------------

Block = list[list]  # one activity: its "Activity" row down to its last exchange


def blocks(path: Path | IO[bytes], label: str | None = None) -> tuple[str, list[Block]]:
    """A workbook's database name and every activity, blank cells and rows trimmed."""
    label = label or str(path)
    database, found = None, []
    for row in openpyxl.load_workbook(path, read_only=True).worksheets[0].iter_rows(values_only=True):
        row = list(row)
        while row and row[-1] is None:
            row.pop()
        if row and row[0] == "Database":
            database = row[1] if len(row) > 1 else None
        elif row and row[0] == "Activity":
            found.append([])
        if found and row:
            found[-1].append(row)
        elif row and row[0] != "Database":
            sys.exit(f"{label}: row {row} comes before the first activity, and this script does not read it")
    if not database or not found:
        sys.exit(f"{label}: no Database row or no activity, is it a Brightway workbook?")
    return str(database), found


def exchanges(block: Block):
    """Each exchange row of an activity, as a dict by header, with its row."""
    header = None
    for row in block:
        if row[:2] == ["name", "amount"]:
            header = row
        elif header:
            yield dict(zip(header, row)), row, header


def is_background(f: dict) -> bool:
    return f.get("type") == "technosphere" and str(f.get("database") or "").startswith("ecoinvent")




def by_activity(activities: list[Block], scales: dict[tuple, float], dropped: frozenset = frozenset()) -> list[Block]:
    """The activities to relink, every ecoinvent input named after its activity, its amount scaled where scales says,
    the inputs dropped says removed.

    Two ecoinvent activities can make one product in one place (hard coal and
    light fuel oil furnaces both make "heat, district or industrial, other than
    natural gas" in RoW) and map to different BAFU processes, while a relink
    recipe keys an input on its name and place. Without a product, a Brightway
    input is named after its activity.
    """
    named = []
    for block in activities:
        gone = {id(row) for f, row, _ in exchanges(block) if is_background(f) and (norm(f["name"]), f["location"]) in dropped}
        rows = [list(row) for row in block if id(row) not in gone]
        for f, row, header in exchanges(rows):
            if is_background(f):
                row += [None] * (len(header) - len(row))  # its blank end cells were trimmed
                row[header.index("reference product")] = None
                scale = scales.get((norm(f["name"]), f["location"]))
                if scale is not None:
                    if not isinstance(f["amount"], (int, float)):
                        sys.exit(f"{block[0][1]}: the amount of {f['name']} is {f['amount']!r}, which the script cannot scale")
                    row[header.index("amount")] = f["amount"] * scale
        named.append(rows)
    return named


def workbook(database: str, activities: list[Block]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(["Database", database])
    for block in activities:
        ws.append([])
        for row in block:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def reported_climate(path: Path) -> dict[str, float]:
    """Carbonfact's own climate change per activity, from its <step>_DB_LCIA.csv."""
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not {"Activity", "GHG"} <= rows[0].keys():
        sys.exit(f"{path}: no Activity or GHG column, is it Carbonfact's LCIA results?")
    return {row["Activity"]: float(row["GHG"]) for row in rows}


def beside(base: Path, out: Path | None, suffix: str) -> Path:
    """Where a command writes what it made of a database: next to it, its name kept, unless --out says another folder."""
    folder = out or base.resolve().parent
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{base.stem}.{suffix}"

# -- the mapping --------------------------------------------------------------


def read_correspondence(path: Path | None) -> dict[tuple, set[tuple]]:
    """ecoinvent 3.12 (activity, product, place) and (activity, place) -> 3.11 keys, from the file, else the built-in ones.

    A dataset new in 3.12 maps to the empty set: it has no 3.11 predecessor.
    """
    if path is None:
        pairs = [((r["activity"], r["product"], r["location"]), (r["activity_311"], r["product_311"], r["location_311"])) for r in csv.DictReader(io.StringIO(CORRESPONDENCE))]
    else:
        rows = openpyxl.load_workbook(path, read_only=True)["Cut-off"].iter_rows(min_row=3, values_only=True)
        pairs = [((row[16], row[22], row[17]), (row[3], row[9], row[4])) for row in rows]
    back = defaultdict(set)
    for (na, np, nl), (oa, op, ol) in pairs:
        old, new = (norm(oa), norm(op), ol), (norm(na), norm(np), nl)
        if new[0]:
            for key in (new, (new[0], "", new[2])):
                back[key].update([old] if old[0] else [])
    return dict(back)


# the sheet's columns this reads, by header, in the order the sheet puts them: each is the first
# of its name after the previous one, so columns added in between are skipped, and of the two the
# header names "Geography" the one read is BAFU's, after "BAFU Name"
COLUMNS = ("EI 3.11 dataset", "BAFU Name", "Geography", "unit", "conversion")


def read_workbook_mapping(path: Path) -> list[tuple[tuple, Target | None]]:
    """The expert workbook: a None target is a row naming no BAFU process; stops if the header is not the one this reads."""
    wb, sheet = openpyxl.load_workbook(path, read_only=True, data_only=True), "Matching Full"
    if sheet not in wb.sheetnames:
        sys.exit(f"{path}: no sheet {sheet!r}, the one this reads")
    rows = wb[sheet].iter_rows(values_only=True)
    # the header is the first row opening on the first column's name: editions put it on row 1 or 2
    header = next((r for r in rows if r and norm(r[0]) == norm(COLUMNS[0])), None)
    if header is None:
        sys.exit(f"{path}: no header row starting with {COLUMNS[0]!r} in sheet {sheet!r}")
    # compared through norm: an edition writing "Unit" or "Conversion" is the same column
    titles = [norm(t) for t in header]
    positions = [0]
    for previous, name in pairwise(COLUMNS):
        try:
            positions.append(titles.index(norm(name), positions[-1] + 1))
        except ValueError:
            sys.exit(f"{path}: no column {name!r} after {previous!r}")
    # a name met again before the next column read is two candidates: say so rather than read whichever comes first
    for name, here, after in zip(COLUMNS, positions, positions[1:]):
        if norm(name) in titles[here + 1 : after]:
            sys.exit(f"{path}: two columns {name!r} before {header[after]!r}, rename the one not to read")
    found = []
    for row in rows:
        ecoinvent, name, location, unit, conversion = (row[c] for c in positions)
        parsed = SIMAPRO_NAME.match(str(ecoinvent or ""))
        if not parsed:
            continue
        # "#N/A" in the place column: the sheet's lookup did not find the name in BAFU
        if norm(name) == "dummy":
            target = DUMMY
        elif not name or str(location or "#").startswith("#"):
            target = None
        else:
            target = Target(str(name), str(location), str(unit), conversion)
        found.append(((norm(parsed["activity"]), norm(parsed["product"]), parsed["geo"]), target))
    return found


def read_csv_mapping(path: Path) -> list[tuple[tuple, Target | None]]:
    """The export of VoLCA's mapping page: a filled target is a decided equivalent.

    A suggestion is a proposal nobody accepted, so it links nothing; a row
    decided as having no equivalent says so, the others are left out.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not {"source_activity", "target_product", "state"} <= rows[0].keys():
        sys.exit(f"{path}: not a mapping page export (no source_activity, target_product or state column)")
    found = []
    for r in rows:
        key = (norm(r["source_activity"]), norm(r["source_product"]), r["source_location"])
        if r["target_product"]:
            conversion = float(r["conversion"]) if r.get("conversion") else 1
            found.append((key, Target(r["target_product"], r["target_location"], r["target_unit"], conversion)))
        elif r["state"] == "noEquivalent":
            found.append((key, None))
    return found


def read_mapping(path: Path) -> dict[tuple, set[Target | None]]:
    """ecoinvent 3.11 (activity, product, place) and (activity, place) -> BAFU targets."""
    readers = {".xlsx": read_workbook_mapping, ".csv": read_csv_mapping}
    if path.suffix.lower() not in readers:
        sys.exit(f"{path}: the mapping is either the expert workbook (.xlsx) or the mapping page's export (.csv)")
    targets = defaultdict(set)
    for (activity, product, location), target in readers[path.suffix.lower()](path):
        targets[(activity, product, location)].add(target)
        targets[(activity, "", location)].add(target)
    return targets


def read_complement(text: str, source: str) -> dict[tuple, Target]:
    rows = [row for row in csv.DictReader(io.StringIO(text)) if row.get("target_product")]
    # The engine refuses a whole recipe for one conversion stated without its two units.
    for row in rows:
        if row.get("conversion") and not (row.get("source_unit") and row.get("target_unit")):
            sys.exit(f"{source}: {row['source_product']} has a conversion but not both units")
    return {
        (norm(row["source_product"]), row["source_location"]): Target(
            row["target_product"], row["target_location"], row.get("target_unit") or "", float(row["conversion"]) if row.get("conversion") else 1
        )
        for row in rows
    }


def resolve(s: Supplier, back: dict, mapping: dict, complement: dict) -> Resolution:
    """The mapping's answer for a supplier, else the hand choice."""
    found = from_mapping(s, back, mapping)
    by_hand = complement.get((norm(s.activity), s.location))
    return Resolution(s, found.ecoinvent_311, by_hand, "chosen by hand") if by_hand and found.target is None else found


def from_mapping(s: Supplier, back: dict, mapping: dict) -> Resolution:
    key = (norm(s.activity), norm(s.product), s.location)
    older = back.get(key, {key})
    if not older:
        return Resolution(s, "", None, "new in ecoinvent 3.12, no 3.11 dataset to map from")
    found = {t for k in older for t in mapping.get(k, ())}
    label = "; ".join(sorted(f"{a} | {p} | {g}" for a, p, g in older))
    if not found:
        return Resolution(s, label, None, "not in the mapping")
    # A "#N/A" row of one 3.11 predecessor leaves the others' targets standing.
    real = found - {None}
    if not real:
        return Resolution(s, label, None, "the mapping gives no BAFU equivalent")
    if len(real) > 1:
        return Resolution(s, label, None, "the mapping gives several BAFU processes: " + " / ".join(sorted({t.name for t in real})))
    target = next(iter(real))
    return Resolution(s, label, target, "the mapping says Dummy, an empty inventory: the line is dropped" if target is DUMMY else "mapped")


def scale_by_hand(r: Resolution, units: dict[str, UnitDefinition]) -> float:
    """What the script multiplies a line by itself, for a conversion the engine would not apply.

    The engine applies a mapping's conversion only between units of different
    measures (1 p of 1200 kg), and converts kWh to MJ by its unit table. Urea
    "as N" is a kilo of nitrogen where ecoinvent's is a kilo of urea: both are
    kg, the engine would link 1 kg to 1 kg, and the mapping's 2.17 kg of urea
    per kg of nitrogen is lost. The script divides the line by it instead, net
    of what the unit table already converts (0.2778 kWh per MJ is the table's).
    """
    t = r.target
    if t is None or not isinstance(t.conversion, (int, float)) or t.conversion in (0, 1):
        return 1
    folded = {norm(k): v for k, v in units.items()}
    source, target = (units.get(u) or folded.get(norm(u)) for u in (r.supplier.unit, t.unit))
    if norm(r.supplier.unit) == norm(t.unit):
        return 1 / t.conversion
    if source and target and source.dimension == target.dimension:
        return target.factor / (source.factor * t.conversion)
    return 1  # different measures: the engine applies it


def recipe(resolutions: list[Resolution]) -> str:
    """The relink CSV: engine columns first, then the columns for a person."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["source_product", "source_location", "target_product", "target_location", "conversion", "source_unit", "target_unit", "direction", "ecoinvent_3_11", "status"])
    for r in sorted(resolutions, key=lambda r: (r.target is None, r.supplier.activity, r.supplier.location)):
        t = None if r.target is DUMMY else r.target  # its status says why it has no BAFU process
        # The engine reads a conversion only between units of different measures
        # (MJ of fuel for 1 kg); kWh to MJ it converts by itself.
        convert = t is not None and isinstance(t.conversion, (int, float)) and t.conversion > 0 and t.conversion != 1
        w.writerow([
            r.supplier.activity, r.supplier.location, t.name if t else "", t.location if t else "",
            t.conversion if convert else "", r.supplier.unit if convert else "", t.unit if convert else "",
            "opposite" if r.opposite else "", r.ecoinvent_311, r.status,
        ])
    return out.getvalue()


# -- the engine ---------------------------------------------------------------


def db_name(path: Path) -> str:
    """BAFU's name in the engine, which an export writes beside each BAFU supplier: after its file."""
    return re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")


def reference_amount(c: Client, name: str, location: str) -> float | None:
    """The reference amount of the one process of that name and place, None when there is not exactly one."""
    found = [a for a in c.search_activities(name=name, geo=location, exact=True)]
    return found[0].product_amount if len(found) == 1 else None


def directed(c: Client, bafu: str, resolutions: list[Resolution]) -> list[Resolution]:
    """Each mapped supplier marked opposite where ecoinvent and BAFU write the reference with opposite signs.

    An ecoinvent waste market is written with -1 m3 and the BAFU treatment of it
    with +1 m3: a line sending -0.018 m3 to the first must send +0.018 m3 to the
    second, or the treatment runs backwards and scores a credit.
    """

    def amounts(r: Resolution) -> tuple[float | None, float | None]:
        return reference_amount(c.use(ECOINVENT), r.supplier.activity, r.supplier.location), reference_amount(c.use(bafu), r.target.name, r.target.location)

    mapped = [r for r in resolutions if r.target]
    with ThreadPoolExecutor(8) as pool:
        found = dict(zip(mapped, pool.map(amounts, mapped)))
    unknown = sorted(f"{r.supplier.activity} {{{r.supplier.location}}}" for r, (e, b) in found.items() if e is None or b is None)
    if unknown:
        print(f"  {len(unknown)} suppliers whose reference amount was not found on one side, relinked as written: {'; '.join(unknown)}")
    return [replace(r, opposite=(e or 0) * (b or 0) < 0) for r in resolutions for e, b in [found.get(r, (None, None))]]




def fresh_upload(c: Client, source: bytes, name: str) -> str:
    for db in c.list_databases():
        if name in (db.display_name, db.name):
            if db.status != DatabaseStatus.UNLOADED:
                c.unload_database(db.name)
            c.delete_database(db.name)
    return c.upload_database(source, name=name)["slug"]




@contextmanager
def running(databases: dict[str, Path], methods: dict[str, Path] | None = None) -> Iterator[Client]:
    """An engine started on these files, recent enough for this script, stopped on the way out."""
    if "VOLCA_BINARY" not in os.environ:
        volca.download()  # the latest release, once per release
    with Server(databases=databases, methods=methods or {}) as server:
        c = Client(server.base_url)
        version = c.get_version().version
        found = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        if not found or tuple(map(int, found.groups())) < NEEDS:
            sys.exit(f"VoLCA {version} is too old, this needs {'.'.join(map(str, NEEDS))} or later. Each run downloads the latest "
                     "release; until one is recent enough, set VOLCA_BINARY and VOLCA_DATA_DIR to a local build.")
        print(f"VoLCA {version}")
        yield c


def load(c: Client, source: bytes | Path, name: str, dependency: str | None = None, mapping: str | None = None) -> str:
    """Upload a database and load it, or stop saying what is missing.

    With a dependency, it is linked to that one only; without, to those the
    engine finds its suppliers in, which are printed.
    """
    slug = fresh_upload(c, source, name)
    selected = [d["databaseName"] for d in c.get_setup(slug).get("dependencies") or [] if d.get("status") == "selected"]
    if dependency:
        for other in selected:
            if other != dependency:
                c.remove_dependency(other, db_name=slug)
    if mapping:
        c.relink(dependency, mapping, db_name=slug)
    setup = c.get_setup(slug)
    if not setup.get("isReady"):
        missing = "".join(
            f"\n  {m['productName']} {m.get('locations')}: {m.get('reason')} {m.get('detail') or ''}" for m in setup.get("missingSuppliers") or []
        )
        sys.exit(f"{name}: {setup.get('unresolvedLinks')} inputs find no supplier in {dependency or ', '.join(selected) or 'the databases given'}:{missing}")
    if not dependency:
        print(f"  {name} takes its suppliers from {', '.join(selected) or 'itself alone'}")
    c.finalize_database(slug)
    return slug


def drop(c: Client, slug: str) -> None:
    c.unload_database(slug)
    c.delete_database(slug)


Key = tuple[str, str, str]  # activity, product, place: what tells two activities apart in both versions


def score(c: Client, slug: str) -> dict[Key, dict]:
    """Every activity's reference unit and score on every category."""
    db = c.use(slug)
    activities = db.search_activities()
    twice = [k for k, n in Counter((a.activity_name, a.product_name, a.location) for a in activities).items() if n > 1]
    if twice:
        sys.exit(f"{slug}: several activities answer to {twice[:5]}, which version's would pair with which is a guess")
    key = {a.process_id: (a.activity_name, a.product_name, a.location) for a in activities}
    rows = db.score_table(list(key), collection=COLLECTION)
    # The method file brings its own weighting set too, which is not a category.
    categories = {m.name for m in c.list_methods() if m.collection == COLLECTION}
    return {
        key[row["process_id"]]: {"unit": row["product_unit"]} | {k: v for k, v in row.items() if k in categories}
        for row in rows
    }


# -- the merge ----------------------------------------------------------------


def identity(block: Block) -> Key:
    fields = {row[0]: row[1] for row in takewhile(lambda row: row[0] != "Exchanges", block) if len(row) > 1}
    return block[0][1], fields.get("reference product"), fields.get("location")


def merged(own: str, databases: list[tuple[str, list[Block]]]) -> list[Block]:
    """Every activity of these databases once, or a stop naming one they write differently.

    Carbonfact copies a process several steps use (the heat mix, a dye) into
    each step: the same in all, it is kept once; written two ways, which one a
    merge should keep is not its call.
    """

    def line(f: dict) -> tuple:
        return f["type"], f.get("reference product") or f["name"], f["unit"], f.get("categories"), f["amount"]

    def alike(f: dict, g: dict) -> bool:
        # ecoSpold 2 keeps neither the place nor the activity of a line within the database
        # itself (own): read back, such a line names its product alone, and only its product can be compared.
        if f.get("database") == own == g.get("database") and not (f.get("location") and g.get("location")):
            return line(f) == line(g)
        return f == g

    def same(a: Block, b: Block) -> bool:
        lines = [[f for f, _, _ in exchanges(x)] for x in (a, b)]
        return (
            a[: a.index(["Exchanges"])] == b[: b.index(["Exchanges"])]
            and len(lines[0]) == len(lines[1])
            and all(alike(f, g) for f, g in zip(*lines))
        )

    kept: dict[Key, Block] = {}
    origin: dict[Key, str] = {}
    for name, activities in databases:
        for block in activities:
            k = identity(block)
            if k in kept and not same(kept[k], block):
                sys.exit(f"{label(k)} is written differently in {origin[k]} and {name}")
            kept.setdefault(k, block)
            origin.setdefault(k, name)
    return list(kept.values())


# -- the comparison -----------------------------------------------------------


def paired(left: dict[Key, dict], right: dict[Key, dict]) -> tuple[dict[Key, dict], dict[Key, dict], list[Key], list[Key]]:
    """The activities found in both, each side's scores for them, and what only one side has.

    A score is per one reference unit: two versions writing one activity's
    reference in different units cannot be read against each other.
    """
    both = sorted(left.keys() & right.keys())
    units = [k for k in both if left[k]["unit"] != right[k]["unit"]]
    if units:
        sys.exit("references in different units: " + "; ".join(f"{k[0]} {left[k]['unit']} / {right[k]['unit']}" for k in units))
    strip = lambda side: {k: {c: v for c, v in side[k].items() if c != "unit"} for k in both}  # noqa: E731
    return strip(left), strip(right), sorted(left.keys() - right.keys()), sorted(right.keys() - left.keys())


def label(k: Key) -> str:
    return f"{k[0]} {{{k[2]}}}"


def positive(left: dict, right: dict, category: str) -> list[Point]:
    """The activities one category can be judged on: a ratio against zero says nothing, a log scale draws neither."""
    return [Point(label(k), s[category], right[k][category]) for k, s in left.items() if s[category] > 0 and right[k][category] > 0]


def widest_gaps(left: dict, right: dict, top: int) -> list[Point]:
    """The widest gaps, either way, every category together: a ratio has no unit, so categories compare."""
    return widest([Point(f"{category:45} {p.label}", p.left, p.right) for category in next(iter(left.values())) for p in positive(left, right, category)], top)


def unranked(left: dict, right: dict, category: str) -> int:
    """The activities a ratio cannot judge on one category: a zero or a negative on one side, not both at zero."""
    return sum(not (s[category] > 0 and right[k][category] > 0) and (s[category], right[k][category]) != (0, 0) for k, s in left.items())


def report(left: dict, right: dict, names: tuple[str, str], top: int) -> None:
    print(f"\n{names[1]} / {names[0]}, {len(left)} activities:")
    print(f"  {'category':45} {'median':>7} " + " ".join(f"{'within ' + band:>11}" for _, band in BANDS) + f" {'unranked':>9}")
    for category in next(iter(left.values())):
        ratios = [p.right / p.left for p in positive(left, right, category)]
        median, shares = (f"{statistics.median(ratios):7.3f}", "") if ratios else (f"{'-':>7}", " " * 12 * len(BANDS))
        if ratios:
            within, total = band_shares(ratios), 0.0
            for _, band in BANDS:
                total += within[band]
                shares += f" {total:11.0%}"
        print(f"  {category:45} {median}{shares} {unranked(left, right, category):9}")
    print(f"\nThe {top} widest gaps, every category together, of those a ratio can judge (unranked: a zero or a negative on one side):")
    for p in widest_gaps(left, right, top):
        print(f"  {fold(p.right / p.left):>9}  {p.label}")


def check_reported(left: dict, reported: dict[str, float]) -> None:
    """How close the left version comes to the climate change Carbonfact reports for each activity."""
    climate = [(s[CLIMATE], reported[k[0]]) for k, s in left.items() if reported.get(k[0])]
    if not climate:
        sys.exit("no activity of the left database is in the reported results")
    worst = max(abs(v / ghg - 1) for v, ghg in climate)
    print(f"The left database is within {worst:.2%} of the climate change Carbonfact reports, on {len(climate)} activities.")


def write_comparison(left: dict, right: dict, names: tuple[str, str], path: Path) -> None:
    categories = list(next(iter(left.values())))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["activity", "product", "location"] + [f"{c} {side}" for c in categories for side in names])
        for k in left:
            w.writerow([*k] + [s[k][c] for c in categories for s in (left, right)])


# -- the commands -------------------------------------------------------------


def relink(args) -> None:
    database, activities = blocks(args.base)
    print(f"{len(activities)} activities in {database}")
    # One per name and place, as the recipe keys them: a workbook can spell one unit two ways (kWh, kilowatt hour).
    suppliers = {
        (norm(f["name"]), f["location"]): Supplier(" ".join(str(f["name"]).split()), " ".join(str(f.get("reference product") or "").split()), f["location"], f["unit"])
        for block in reversed(activities) for f, _, _ in exchanges(block) if is_background(f)
    }.values()
    complement = read_complement(args.complement.read_text(encoding="utf-8"), str(args.complement)) if args.complement else read_complement(COMPLEMENT, "built-in")
    back, mapping = read_correspondence(args.correspondence), read_mapping(args.mapping)
    resolutions = [resolve(s, back, mapping, complement) for s in suppliers]
    gaps = sorted((r for r in resolutions if r.target is None), key=lambda r: r.supplier.activity)
    print(f"{len(resolutions) - len(gaps)} of {len(resolutions)} ecoinvent suppliers have a BAFU equivalent")
    recipe_path = beside(args.base, args.out, "bafu.mapping.csv")
    for r in gaps:
        hint = "; --correspondence may find it under its 3.11 name" if r.status == "not in the mapping" and not args.correspondence else ""
        print(f"  none for {r.supplier.activity} {{{r.supplier.location}}}: {r.status}{hint}")
    if gaps:
        recipe_path.write_text(recipe(resolutions), encoding="utf-8")
        sys.exit(f"Name a BAFU supplier for each in a --complement file, in the columns of {recipe_path}.")

    bafu = db_name(args.bafu)
    with running({ECOINVENT: args.ecoinvent, bafu: args.bafu}) as c:
        print("loading ecoinvent and BAFU (the first time, several minutes)...")
        c.load_database(ECOINVENT)
        c.load_database(bafu)
        dropped = frozenset((norm(r.supplier.activity), r.supplier.location) for r in resolutions if r.target is DUMMY)
        for r in resolutions:
            if r.target is DUMMY:
                print(f"  {r.supplier.activity} {{{r.supplier.location}}}: {r.status}")
        resolutions = directed(c, bafu, [r for r in resolutions if r.target is not DUMMY])
        print(f"  {sum(r.opposite for r in resolutions)} suppliers that BAFU counts the other way, turned")
        units = c.unit_table()
        scales = {r: k for r in resolutions if abs((k := scale_by_hand(r, units)) - 1) > 1e-9}
        for r, k in scales.items():
            print(f"  {r.supplier.activity} {{{r.supplier.location}}}: {r.target.conversion} {r.supplier.unit} per {r.target.unit} of {r.target.name}, the line multiplied by {k:.4g}")
        # Written without its conversion, which the line already carries.
        resolutions = [replace(r, target=replace(r.target, conversion=1), status=f"{r.status}, amount multiplied by {scales[r]:.6g}") if r in scales else r for r in resolutions]
        relinked = recipe(resolutions)
        recipe_path.write_text(relinked, encoding="utf-8")
        lines = {(norm(r.supplier.activity), r.supplier.location): k for r, k in scales.items()}
        slug = load(c, workbook(database, by_activity(activities, lines, dropped)), f"{database} on BAFU", bafu, relinked)
        written = beside(args.base, args.out, f"bafu.{args.format}.{EXTENSION[args.format]}")
        c.export_to_file(args.format, str(written), db_name=slug)
        drop(c, slug)
    print(f"written: {written}\n         {recipe_path}")


def compare(args) -> None:
    if args.left.resolve() == args.right.resolve():
        sys.exit("a database compared with itself says nothing")
    # two versions of one name in two folders would give two columns of one title
    names = (args.left.name, args.right.name) if args.left.name != args.right.name else (str(args.left), str(args.right))
    # read before the long scoring, so a wrong file stops the run at once
    reported = reported_climate(args.reported) if args.reported else None
    bafu = db_name(args.bafu)
    with running({ECOINVENT: args.ecoinvent, bafu: args.bafu}, {COLLECTION: args.method}) as c:
        if reported is not None and CLIMATE not in {m.name for m in c.list_methods() if m.collection == COLLECTION}:
            sys.exit(f"--reported checks climate change, and {args.method} has no {CLIMATE!r} category: is it EF 3.1?")
        print("loading ecoinvent and BAFU (the first time, several minutes)...")
        c.load_database(ECOINVENT)
        c.load_database(bafu)
        slugs = [load(c, path, f"{side}: {path.stem}") for side, path in (("left", args.left), ("right", args.right))]
        print("scoring...")
        left, right = (score(c, slug) for slug in slugs)
        for slug in slugs:
            drop(c, slug)
    left, right, only_left, only_right = paired(left, right)
    for name, only in zip(names, (only_left, only_right)):
        if only:
            print(f"  {len(only)} activities only in {name}, left out: {'; '.join(label(k) for k in only[:10])}{' ...' if len(only) > 10 else ''}")
    if not left:
        sys.exit("no activity is in both databases")
    table, figure = beside(args.right, args.out, "comparison.csv"), beside(args.right, args.out, "comparison.svg")
    write_comparison(left, right, names, table)
    points = {category: positive(left, right, category) for category in next(iter(left.values()))}
    scatter_grid(points, figure, labels=names)
    print(f"\nwritten: {table}\n         {figure}")
    if reported is not None:
        check_reported(left, reported)
    report(left, right, names, args.top)


def quality(args) -> None:
    written = beside(args.base, args.out, "quality.csv")
    with running({}) as c:
        # The report reads the database as staged: no background needs to be loaded.
        slug = fresh_upload(c, args.base, args.base.stem)
        c.get_setup(slug)  # what parses an upload: the report reads the parsed database
        written.write_bytes(c.quality_report_csv(slug))
        c.delete_database(slug)
    print(f"written: {written}")


def merge(args) -> None:
    folder = args.out or Path.cwd()
    folder.mkdir(parents=True, exist_ok=True)
    written = folder / f"{args.name}.{args.format}.{EXTENSION[args.format]}"
    backgrounds = {name: path for name, path in ((ECOINVENT, args.ecoinvent), (args.bafu and db_name(args.bafu), args.bafu)) if path}
    with running(backgrounds) as c:
        # The engine loads a database only once it finds every supplier.
        print(f"loading {' and '.join(backgrounds) or 'no background'} (the first time, several minutes)...")
        for name in backgrounds:
            c.load_database(name)
        databases = []
        names = [path.name for path in args.bases]
        for path in args.bases:
            label = path.name if names.count(path.name) == 1 else str(path)  # one name in two folders
            slug = load(c, path, path.stem)
            # Read through the engine's own Brightway writing, which spells one thing one way in every database.
            databases.append((label, blocks(io.BytesIO(c.export_database("brightway", db_name=slug)), label)))
            drop(c, slug)
        names = {database for _, (database, _) in databases}
        if len(names) != 1:
            sys.exit(f"the engine named its exports {sorted(names)}: which name their links point to is unclear")
        own = names.pop()
        activities = merged(own, [(name, found) for name, (_, found) in databases])
        print(f"{len(activities)} activities in {len(databases)} databases")
        slug = load(c, workbook(own, activities), args.name)
        c.export_to_file(args.format, str(written), db_name=slug)
        drop(c, slug)
    print(f"written: {written}")


# -- main ---------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Put a Carbonfact database on BAFU, compare its scores on both, check it, and merge several into one.")
    commands = ap.add_subparsers(dest="command", required=True)

    def backgrounds(p: argparse.ArgumentParser, required: bool = True) -> None:
        p.add_argument("--ecoinvent", type=Path, required=required, help="ecoinvent 3.12 cut-off, its ecoSpold02 archive")
        p.add_argument("--bafu", type=Path, required=required, help="BAFU, its ecoSpold v1 archive")

    def out(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", type=Path, help="the folder to write in (default: the database's own)")

    p = commands.add_parser("relink", help="write a database on BAFU, as <name>.bafu.<format>.<ext>, and the mapping it applied")
    p.add_argument("base", type=Path, help="a Brightway workbook whose suppliers are in ecoinvent 3.12 cut-off")
    p.add_argument("--mapping", type=Path, required=True, help="ecoinvent 3.11 to BAFU: the expert workbook (.xlsx) or the mapping page's export (.csv)")
    backgrounds(p)
    p.add_argument("--correspondence", type=Path, help="ecoinvent's 'Correspondence File v3.11 - v.3.12.xlsx', for suppliers renamed in 3.12; it replaces the built-in renames, and is needed only when the script asks for it")
    p.add_argument("--complement", type=Path, help="BAFU suppliers chosen by hand, in the mapping CSV's columns; used where the mapping has no BAFU process, in place of the built-in ones")
    p.add_argument("--format", choices=sorted(EXTENSION), default="brightway", help="the format to write (default: %(default)s)")
    out(p)
    p.set_defaults(run=relink)

    p = commands.add_parser("compare", help="score two versions of a database, each on its own background, one against the other")
    p.add_argument("left", type=Path, help="the reference version, the one on ecoinvent say, in any format the engine reads")
    p.add_argument("right", type=Path, help="the version to judge against it, the one on BAFU say")
    backgrounds(p)
    p.add_argument("--method", type=Path, required=True, help="the impact method, any the engine reads: EF 3.1 as the JRC's ILCD package or SimaPro's CSV export, ecological scarcity 2021...")
    p.add_argument("--reported", type=Path, help="Carbonfact's <step>_DB_LCIA.csv, to check the left version against its climate change")
    p.add_argument("--top", type=int, default=20, help="how many of the widest gaps to list, every category together (default: %(default)s)")
    out(p)
    p.set_defaults(run=compare)

    p = commands.add_parser("quality", help="write the engine's structural quality report of a database, as <name>.quality.csv")
    p.add_argument("base", type=Path, help="a database in any format the engine reads")
    out(p)
    p.set_defaults(run=quality)

    p = commands.add_parser("merge", help="write several databases as one, as <name>.<format>.<ext>, each activity they share once")
    p.add_argument("bases", type=Path, nargs="+", help="the databases, in any formats the engine reads, on ecoinvent, BAFU or both")
    backgrounds(p, required=False)  # those the databases take their suppliers from
    p.add_argument("--format", choices=sorted(EXTENSION), default="brightway", help="the format to write (default: %(default)s)")
    p.add_argument("--name", default="merged", help="the merged database's name, and its file's (default: %(default)s)")
    p.add_argument("--out", type=Path, help="the folder to write in (default: the current one)")
    p.set_defaults(run=merge)

    args = ap.parse_args()
    for name, given in vars(args).items():
        for path in given if isinstance(given, list) else [given]:
            if isinstance(path, Path) and name != "out" and not path.exists():
                sys.exit(f"not found: {path}")
    args.run(args)


if __name__ == "__main__":
    try:
        main()
    except VoLCAError as e:
        sys.exit(f"the engine refused: {e}")
