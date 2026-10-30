# /// script
# requires-python = ">=3.12"
# dependencies = ["pyvolca[plot]>=0.14", "openpyxl"]
# ///
"""Put the Carbonfact processes on BAFU, and see what it changes.

Carbonfact ships one Brightway workbook per process step, each naming its
suppliers in ecoinvent 3.12 cut-off. This gathers every step into one database,
Carbonfact, and loads it twice: as published, on ecoinvent, and relinked onto
BAFU through an ecoinvent 3.11 to BAFU mapping. Every process is scored on both
and the two are drawn against each other.

    uv run carbonfact_bafu.py "Carbonfact database" MAPPING \\
        --ecoinvent "ecoinvent 3.12_cutoff_ecoSpold02.7z" \\
        --bafu "BAFU-2026 v1_ecoSpold v1.zip" \\
        --method "Environmental Footprint 3.1 (adapted).1.03.CSV.zip"

MAPPING is either the expert workbook (.xlsx, sheet "Matching Full") or the
mapping CSV exported by VoLCA's mapping page (.csv); both must have ecoinvent
3.11 as source. A 3.12 supplier is looked up under the same name in 3.11, or
under its 3.11 name with --correspondence.

Written next to the Carbonfact folder, in "<folder> - BAFU": mapping.csv (the
relink recipe, and why a supplier has no BAFU equivalent), comparison.csv and
comparison.svg, quality.csv (the engine's quality report of the BAFU version),
and with --export the BAFU version as a file another tool can open.

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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path

import openpyxl
import volca
from volca import Client, DatabaseStatus, ScoringRow, Server, VoLCAError
from volca.compare_plot import BANDS, Point, band_shares, fold, scatter_grid, widest

NEEDS = (0, 16, 0)  # keeps a mapping's links through finalize, exports under the linked supplier
ECOINVENT, COLLECTION, WITH_ECS = "ecoinvent-3.12", "EF 3.1", "ef-3-1-with-ecs"  # a copy's name, as the engine keeps it
ON_ECOINVENT, ON_BAFU = "carbonfact-ecoinvent", "carbonfact"
EXTENSION = {"brightway": "xlsx", "simapro": "csv", "ecospold2": "zip", "openlca": "zip"}
TABLES = ("ECS", "Climate change")
ECS_COLUMN = "ECS Single score"  # score_table names a set's column after the set and its score
# "polypropylene, granulate {RER}| polypropylene production, granulate | Cut-off, U"
SIMAPRO_NAME = re.compile(r"^(?P<product>.*?) \{(?P<geo>.+?)\}\s*\|\s*(?P<activity>.*?)(\s*\|.*)?$")

# Ecobalyse's environmental cost on the EF 3.1 adapted names, as ei_bafu_mapping
# scores it: a category's terms (freshwater ecotoxicity counts organics twice),
# its normalization and its weight.
ECS = [
    ({"Climate change": 1}, 7553.08, 0.2106),
    ({"Acidification": 1}, 55.5695, 0.0491),
    ({"Ecotoxicity, freshwater - organics": 2, "Ecotoxicity, freshwater - inorganics": 1}, 98120.0, 0.2106),
    ({"Eutrophication, freshwater": 1}, 1.60685, 0.0222),
    ({"Eutrophication, marine": 1}, 19.5452, 0.0235),
    ({"Eutrophication, terrestrial": 1}, 176.755, 0.0294),
    ({"Resource use, fossils": 1}, 65004.3, 0.0659),
    ({"Resource use, minerals and metals": 1}, 0.0636226, 0.0598),
    ({"Ionising radiation": 1}, 4220.16, 0.0397),
    ({"Land use": 1}, 819498.0, 0.0629),
    ({"Ozone depletion": 1}, 0.053648, 0.05),
    ({"Photochemical ozone formation": 1}, 40.8592, 0.0379),
    ({"Particulate matter": 1}, 0.000595367, 0.071),
    ({"Water use": 1}, 11468.7, 0.0674),
]

# BAFU suppliers chosen by hand, in mapping.csv's columns. They win over the
# workbook, whose automatic proposals can be far off: the README says how strong each one is.
COMPLEMENT = """source_product,source_location,target_product,target_location,conversion,source_unit,target_unit
market for alkyl sulfate (C12-14),GLO,"Fatty alcohol sulfate, mix, at plant",RER,,,
"market for chemical, organic, unspecified",GLO,"Chemicals organic, at plant",GLO,,,
market for polytetrafluoroethylene,GLO,"Tetrafluoroethylene, at plant",RER,,,
market for dryer,GLO,"Washing machine, at retailer",CH,,,
market for electric kettle,GLO,Convector radiator,CH,1.2,unit,kg
market for tissue paper,GLO,"Paper, woodfree, uncoated, at regional storage",RER,,,
market for waste mineral oil,RoW,"Disposal, used mineral oil, 10% water, to hazardous waste incineration",CH,,,
market for wastewater from textile production,RoW,"Treatment, sewage, to wastewater treatment, class 3",CH,,,
market for cationic resin,RoW,"Cationic resin, at plant",CH,,,
"market for polyethylene, high density, granulate",RER,"Polyethylene, HDPE, granulate, at plant",RER,,,
"market for polypropylene, granulate",RER,"Polypropylene, granulate, at plant",RER,,,
"market for polypropylene, granulate",RoW,"Polypropylene, granulate, at plant",RER,,,
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


@dataclass(frozen=True)
class Resolution:
    supplier: Supplier
    ecoinvent_311: str
    target: Target | None
    status: str
    opposite: bool = False  # the BAFU process writes its reference with the other sign


# -- the Carbonfact workbooks -------------------------------------------------

Block = list[list]  # one activity: its "Activity" row down to its last exchange


def blocks(path: Path) -> list[Block]:
    """Every activity of a workbook, its database renamed Carbonfact, blank cells and rows trimmed."""
    found: list[Block] = []
    for row in openpyxl.load_workbook(path, read_only=True).worksheets[0].iter_rows(values_only=True):
        row = ["Carbonfact" if re.fullmatch(r"Carbonfact_.*_DB", str(c)) else c for c in row]
        while row and row[-1] is None:
            row.pop()
        if row and row[0] == "Activity":
            found.append([])
        if found and row:
            found[-1].append(row)
        elif row and row[0] != "Database":
            sys.exit(f"{path}: row {row} comes before the first activity, and this script does not read it")
    return found


def merge(folder: Path, units: dict) -> tuple[list[Block], dict[str, list[str]]]:
    """One database of every step, each activity once, and the steps each activity comes from.

    An activity met in several steps must say the same thing in each: case,
    spacing and unit spelling (MJ, megajoule) aside.
    """
    workbooks = sorted(folder.glob("*/*_Inventory_Brightway.xlsx"))
    if not workbooks:
        sys.exit(f"no <step>/<step>_Inventory_Brightway.xlsx in {folder}: is it the Carbonfact folder?")

    def meaning(block: Block) -> list:
        # exact spelling first: mJ and MJ are two units
        return [[units.get(c) or units.get(norm(c)) or norm(c) if isinstance(c, str) else c for c in row] for row in block]

    kept: dict[str, Block] = {}
    steps: dict[str, list[str]] = defaultdict(list)
    for path in workbooks:
        for block in blocks(path):
            name = block[0][1]
            if name in kept and meaning(kept[name]) != meaning(block):
                sys.exit(f"{name!r} differs between the steps {', '.join(steps[name])} and {path.parent.name}")
            kept.setdefault(name, block)
            steps[name].append(path.parent.name)
    return list(kept.values()), steps


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


def workbook(activities: list[Block], by_activity: bool) -> bytes:
    """The Brightway workbook of the merged database.

    by_activity clears the reference product of every ecoinvent input: two
    ecoinvent activities can make one product in one place (hard coal and light
    fuel oil furnaces both make "heat, district or industrial, other than natural
    gas" in RoW) and map to different BAFU processes, while a relink recipe keys
    an input on its name and place. Without a product, a Brightway input is
    named after its activity.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(["Database", "Carbonfact"])
    for block in activities:
        rows = [list(row) for row in block]
        for f, row, header in exchanges(rows):
            if by_activity and is_background(f):
                row += [None] * (len(header) - len(row))  # its blank end cells were trimmed
                row[header.index("reference product")] = None
        ws.append([])
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def reported_climate(folder: Path) -> dict[str, float]:
    reported = {}
    for path in folder.glob("*/Carbonfact_*_DB_LCIA.csv"):
        with path.open(newline="", encoding="utf-8") as fh:
            reported |= {row["Activity"]: float(row["GHG"]) for row in csv.DictReader(fh)}
    return reported


# -- the mapping --------------------------------------------------------------


def read_correspondence(path: Path | None) -> dict[tuple, set[tuple]]:
    """ecoinvent 3.12 (activity, product, place) and (activity, place) -> 3.11 keys.

    A dataset new in 3.12 maps to the empty set: it has no 3.11 predecessor.
    """
    if path is None:
        return {}
    ws = openpyxl.load_workbook(path, read_only=True)["Cut-off"]
    back = defaultdict(set)
    for row in ws.iter_rows(min_row=3, values_only=True):
        old = (norm(row[3]), norm(row[9]), row[4])
        new = (norm(row[16]), norm(row[22]), row[17])
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
        target = None if not name or str(location or "#").startswith("#") else Target(str(name), str(location), str(unit), conversion)
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
    """The hand choice for a supplier, else the mapping's answer."""
    found = from_mapping(s, back, mapping)
    by_hand = complement.get((norm(s.activity), s.location))
    return Resolution(s, found.ecoinvent_311, by_hand, "chosen by hand") if by_hand else found


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
    return Resolution(s, label, next(iter(real)), "mapped")


def recipe(resolutions: list[Resolution]) -> str:
    """The relink CSV: engine columns first, then the columns for a person."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["source_product", "source_location", "target_product", "target_location", "conversion", "source_unit", "target_unit", "direction", "ecoinvent_3_11", "status"])
    for r in sorted(resolutions, key=lambda r: (r.target is None, r.supplier.activity, r.supplier.location)):
        t = r.target
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


def with_ecs(c: Client) -> str:
    """A copy of the method with ECS added, since a collection read from a file is not changed in place."""
    for old in c.list_method_collections():  # the engine keeps a previous run's copy
        if old["name"] == WITH_ECS:
            c.delete_method_collection(WITH_ECS)
    copy = c.copy_method_collection(COLLECTION, WITH_ECS)["name"]
    ids = {m.name: m.id for m in c.list_methods() if m.collection == copy}
    missing = sorted({name for terms, _, _ in ECS for name in terms} - ids.keys())
    if missing:
        sys.exit(f"the method {COLLECTION} lacks what ECS needs ({', '.join(missing)}): is it EF 3.1 adapted?")
    rows = [ScoringRow(" + ".join(terms), {ids[n]: k for n, k in terms.items()}, normalization, weight) for terms, normalization, weight in ECS]
    c.create_scoring_set(copy, "ECS", rows, unit="Pts")
    return copy


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


def load(c: Client, source: bytes, name: str, dependency: str, mapping: str | None = None) -> str:
    """Upload the merged database, link it to one dependency only, and load it, or stop saying what is missing."""
    slug = fresh_upload(c, source, name)
    for dep in c.get_setup(slug).get("dependencies") or []:
        if dep.get("status") == "selected" and dep["databaseName"] != dependency:
            c.remove_dependency(dep["databaseName"], db_name=slug)
    if mapping:
        c.relink(dependency, mapping, db_name=slug)
    setup = c.get_setup(slug)
    if not setup.get("isReady"):
        missing = "".join(
            f"\n  {m['productName']} {m.get('locations')}: {m.get('reason')} {m.get('detail') or ''}" for m in setup.get("missingSuppliers") or []
        )
        sys.exit(f"{name}: {setup.get('unresolvedLinks')} inputs find no supplier in {dependency}:{missing}")
    c.finalize_database(slug)
    return slug


def score(c: Client, slug: str, collection: str) -> dict[str, dict[str, float]]:
    """Every activity's score on ECS and every category, by activity name."""
    db = c.use(slug)
    rows = db.score_table([a.process_id for a in db.search_activities()], collection=collection)
    # The method file brings its own weighting set too, which is not a category.
    categories = {m.name for m in c.list_methods() if m.collection == collection}
    return {row["activity_name"]: {"ECS": row[ECS_COLUMN]} | {k: v for k, v in row.items() if k in categories} for row in rows}


# -- the comparison -----------------------------------------------------------


def positive(ecoinvent: dict, bafu: dict, category: str) -> list[Point]:
    """The activities one category can be judged on: a ratio against zero says nothing, a log scale draws neither."""
    return [Point(name, e[category], bafu[name][category]) for name, e in ecoinvent.items() if e[category] > 0 and bafu[name][category] > 0]


def report(ecoinvent: dict, bafu: dict, reported: dict, steps: dict, top: int) -> None:
    climate = [(ecoinvent[n]["Climate change"], ghg) for n, ghg in reported.items() if n in ecoinvent and ghg]
    if climate:
        worst = max(abs(v / ghg - 1) for v, ghg in climate)
        print(f"On ecoinvent, climate change is within {worst:.2%} of what Carbonfact reports, on {len(climate)} activities.")
    print(f"\nBAFU background / ecoinvent background, {len(bafu)} activities:")
    print(f"  {'category':45} {'median':>7} " + " ".join(f"{'within ' + label:>11}" for _, label in BANDS))
    for category in next(iter(ecoinvent.values())):
        ratios = [p.right / p.left for p in positive(ecoinvent, bafu, category)]
        if ratios:
            within, total = band_shares(ratios), 0.0
            shares = ""
            for _, label in BANDS:
                total += within[label]
                shares += f" {total:11.0%}"
            print(f"  {category:45} {statistics.median(ratios):7.3f}{shares}")
    for category in TABLES:
        print(f"\nThe {top} widest gaps on {category}:")
        for p in widest(positive(ecoinvent, bafu, category), top):
            print(f"  {fold(p.right / p.left):>9}  {p.label}  ({', '.join(steps[p.label])})")




def draw(ecoinvent: dict, bafu: dict, steps: dict, path: Path) -> None:
    """One scatter per category, coloured by step, the three widest gaps named."""
    points = {category: positive(ecoinvent, bafu, category) for category in next(iter(ecoinvent.values()))}
    scatter_grid(points, path, colour={name: s[0] for name, s in steps.items()}, labels=("ecoinvent 3.12 background", "BAFU background"))


def write_comparison(ecoinvent: dict, bafu: dict, reported: dict, steps: dict, path: Path) -> None:
    categories = list(next(iter(ecoinvent.values())))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["step", "activity", "Carbonfact reported GHG"] + [f"{c} {side}" for c in categories for side in ("ecoinvent", "BAFU")])
        for name in sorted(ecoinvent, key=lambda n: (steps[n][0], n)):
            w.writerow(["; ".join(steps[name]), name, reported.get(name, "")] + [s[name][c] for c in categories for s in (ecoinvent, bafu)])


# -- main ---------------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("carbonfact", type=Path, help="the 'Carbonfact database' folder, one sub-folder per step")
    ap.add_argument("mapping", type=Path, help="ecoinvent 3.11 to BAFU: the expert workbook (.xlsx) or the mapping page's export (.csv)")
    ap.add_argument("--ecoinvent", type=Path, required=True, help="ecoinvent 3.12 cut-off, its ecoSpold02 archive")
    ap.add_argument("--bafu", type=Path, required=True, help="BAFU, its ecoSpold v1 archive")
    ap.add_argument("--method", type=Path, required=True, help="EF 3.1 adapted, its SimaPro CSV export")
    ap.add_argument("--correspondence", type=Path, help="ecoinvent's 'Correspondence File v3.11 - v.3.12.xlsx', for suppliers renamed in 3.12")
    ap.add_argument("--complement", type=Path, help="BAFU suppliers chosen by hand, in mapping.csv's columns; they win over the mapping and replace the built-in ones")
    ap.add_argument("--export", choices=sorted(EXTENSION), help="also write the BAFU version in this format")
    ap.add_argument("--no-quality", action="store_true", help="skip the engine's quality report")
    ap.add_argument("--out", type=Path, help="where to write (default: '<carbonfact folder> - BAFU' beside it)")
    ap.add_argument("--top", type=int, default=20, help="how many of the widest gaps to list")
    args = ap.parse_args()
    for given in (args.carbonfact, args.mapping, args.ecoinvent, args.bafu, args.method, args.correspondence, args.complement):
        if given is not None and not given.exists():
            sys.exit(f"not found: {given}")
    out = args.out or args.carbonfact.resolve().with_name(args.carbonfact.resolve().name + " - BAFU")
    out.mkdir(parents=True, exist_ok=True)

    bafu = db_name(args.bafu)
    if "VOLCA_BINARY" not in os.environ:
        volca.download()  # the latest release, once per release
    server = Server(databases={ECOINVENT: args.ecoinvent, bafu: args.bafu}, methods={COLLECTION: args.method})
    server.start()
    try:
        c = Client(server.base_url)
        version = c.get_version().version
        found = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
        if not found or tuple(map(int, found.groups())) < NEEDS:
            sys.exit(f"VoLCA {version} is too old, this needs {'.'.join(map(str, NEEDS))} or later. Each run downloads the latest "
                     "release; until one is recent enough, set VOLCA_BINARY and VOLCA_DATA_DIR to a local build.")
        units = c.unit_table()
        activities, steps = merge(args.carbonfact, units)
        print(f"{len(activities)} Carbonfact activities from {len({s for ss in steps.values() for s in ss})} steps")

        # One per name and place, as the recipe keys them: the steps spell one unit two ways (kWh, kilowatt hour).
        suppliers = {
            (norm(f["name"]), f["location"]): Supplier(" ".join(str(f["name"]).split()), " ".join(str(f.get("reference product") or "").split()), f["location"], f["unit"])
            for block in reversed(activities) for f, _, _ in exchanges(block) if is_background(f)
        }.values()
        complement = read_complement(args.complement.read_text(encoding="utf-8"), str(args.complement)) if args.complement else read_complement(COMPLEMENT, "built-in")
        back, mapping = read_correspondence(args.correspondence), read_mapping(args.mapping)
        resolutions = [resolve(s, back, mapping, complement) for s in suppliers]
        gaps = sorted((r for r in resolutions if r.target is None), key=lambda r: r.supplier.activity)
        print(f"{len(resolutions) - len(gaps)} of {len(resolutions)} ecoinvent suppliers have a BAFU equivalent")
        for r in gaps:
            hint = "; --correspondence may find it under its 3.11 name" if r.status == "not in the mapping" and not args.correspondence else ""
            print(f"  none for {r.supplier.activity} {{{r.supplier.location}}}: {r.status}{hint}")
        if gaps:
            (out / "mapping.csv").write_text(recipe(resolutions), encoding="utf-8")
            sys.exit(f"Name a BAFU supplier for each in a --complement file, in the columns of {out / 'mapping.csv'}.")

        print(f"VoLCA {version}, loading ecoinvent and BAFU (the first time, several minutes)...")
        c.load_database(ECOINVENT)
        c.load_database(bafu)
        resolutions = directed(c, bafu, resolutions)
        print(f"  {sum(r.opposite for r in resolutions)} suppliers that BAFU counts the other way, turned")
        relink = recipe(resolutions)
        (out / "mapping.csv").write_text(relink, encoding="utf-8")
        collection = with_ecs(c)
        on_ecoinvent = load(c, workbook(activities, by_activity=False), ON_ECOINVENT, ECOINVENT)
        on_bafu = load(c, workbook(activities, by_activity=True), ON_BAFU, bafu, relink)
        print("scoring...")
        ecoinvent, bafu_scores = score(c, on_ecoinvent, collection), score(c, on_bafu, collection)
        if not args.no_quality:
            (out / "quality.csv").write_bytes(c.quality_report_csv(on_bafu))
        if args.export:
            c.export_to_file(args.export, str(out / f"carbonfact.{args.export}.{EXTENSION[args.export]}"), db_name=on_bafu)
        for slug in (on_ecoinvent, on_bafu):
            c.unload_database(slug)
            c.delete_database(slug)
    finally:
        server.stop()

    reported = reported_climate(args.carbonfact)
    write_comparison(ecoinvent, bafu_scores, reported, steps, out / "comparison.csv")
    draw(ecoinvent, bafu_scores, steps, out / "comparison.svg")
    report(ecoinvent, bafu_scores, reported, steps, args.top)
    print(f"\nwritten in {out}")


if __name__ == "__main__":
    try:
        main()
    except VoLCAError as e:
        sys.exit(f"the engine refused: {e}")
