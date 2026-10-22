# /// script
# requires-python = ">=3.12"
# dependencies = ["pyvolca>=0.14", "openpyxl", "matplotlib"]
# ///
"""Hold ecoinvent processes against the BAFU processes a mapping sheet points them to.

One scatter per EF 3.1 impact category, plus the ECS and PEF single scores: a point is one
pair the sheet proposes, x what the ecoinvent 3.11 process scores, y what the BAFU process
it is mapped to scores, both for one unit of the ecoinvent product, coloured by the technical
grade the sheet gives the pair. The command stands on its own: it installs the engine,
starts it on a free port, loads both databases from their files, scores every mapped
process, draws, and writes one CSV line per pair saying what became of it.

A score is per one unit of what its process does: a waste market written with -1 kg and
a treatment written with +1 kg both score the treatment of one kilogram, so only the
units are put on one basis, never the signs.

    uv run compare_mapping.py mapping.xlsx --ecoinvent ei.zip --bafu bafu.zip --method ef31.zip

VOLCA_BINARY with VOLCA_DATA_DIR runs a local engine build and its data instead of the
release, which is then not downloaded.
"""

import argparse
import csv
import os
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import accumulate, pairwise
from pathlib import Path
from typing import Literal, NamedTuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import openpyxl  # noqa: E402
import volca  # noqa: E402
from volca import Activity, Client, ScoringRow, Server, UnitDefinition, VoLCAError  # noqa: E402
from volca.compare_plot import BANDS, DifferentMeasures, Point, band_shares, fold, per_left_unit, widest  # noqa: E402

# the sheet's columns this reads, by header, in the order the sheet puts them: each is the first
# of its name after the previous one, so columns added in between are skipped, and of the two the
# header names "Geography" the one read is BAFU's, after "BAFU Name"
COLUMNS = ("EI 3.11 dataset", "Unit", "BAFU Name", "Geography", "unit", "conversion", "Source", "Tech\nGrade", "Geo Grade")
# the technical grade, as the sheet's evaluation rules word it, drawn from the least trusted up
GRADES = {
    "0": ("0, no match", "lightgrey"),
    "1": ("1, bad proxy or to be checked", "tab:orange"),
    "2": ("2, partial match or good proxy", "tab:blue"),
    "3": ("3, perfect match", "tab:green"),
}
TABLES = ("ECS", "Climate change")  # the two indicators the sheet itself tracks a gap on
LABELS_PER_GRAPH = 3
COLLECTION = "EF 3.1"  # the method as this tool loads it; the engine also carries its own built-in ones
# the method with both single scores added, as the engine names a copy: a collection read
# from a file is not changed in place
SCORED = "ef-3-1-ecs-pef"
THREADS = 8  # parallel scoring requests: the engine gains little past this
CHUNK = 250  # processes per request

# Ecobalyse's environmental cost and the EF 3.1 single score, on the adapted names: each
# row a category's terms, its normalization and its weight. Ecobalyse's own corrections
# (uranium in fossil resources) are left out: this is EF 3.1. Both read freshwater
# ecotoxicity as its two parts: from adapted 1.05 on, the whole category carries almost
# no factor of its own and scores near zero.
SINGLE_SCORES = {
    "ECS": (
        "Pts",
        [
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
        ],
    ),
    "PEF": (
        "Pt",
        [
            ({"Acidification": 1}, 55.5695, 0.062),
            ({"Climate change": 1}, 7553.08, 0.2106),
            ({"Ecotoxicity, freshwater - organics": 1, "Ecotoxicity, freshwater - inorganics": 1}, 56716.6, 0.0192),
            ({"Particulate matter": 1}, 0.000595367, 0.0896),
            ({"Eutrophication, marine": 1}, 19.5452, 0.0296),
            ({"Eutrophication, freshwater": 1}, 1.60685, 0.028),
            ({"Eutrophication, terrestrial": 1}, 176.755, 0.0371),
            ({"Human toxicity, cancer": 1}, 1.72529e-05, 0.0213),
            ({"Human toxicity, non-cancer": 1}, 0.000128736, 0.0184),
            ({"Ionising radiation": 1}, 4220.16, 0.0501),
            ({"Land use": 1}, 819498.0, 0.0794),
            ({"Ozone depletion": 1}, 0.0523484, 0.0631),
            ({"Photochemical ozone formation": 1}, 40.8592, 0.0478),
            ({"Resource use, fossils": 1}, 65004.3, 0.0832),
            ({"Resource use, minerals and metals": 1}, 0.0636226, 0.0755),
            ({"Water use": 1}, 11468.7, 0.0851),
        ],
    ),
}


class Pair(NamedTuple):
    """One mapping the sheet proposes."""

    ecoinvent: str  # "product {GEO}| activity", the SimaPro name without its system suffix
    ecoinvent_unit: str  # as the sheet writes it
    bafu: str  # the BAFU activity name
    geo: str  # the BAFU activity's location
    bafu_unit: str  # as the sheet writes it
    conversion: str  # one BAFU unit in ecoinvent units, as the sheet writes it; None, #N/A or 0 when it has none
    source: str  # who proposed the mapping
    tech_grade: str  # a key of GRADES, or whatever else the sheet wrote
    geo_grade: str


class Match(NamedTuple):
    """A pair whose two processes were found, and can be put on the same unit."""

    pair: Pair
    ecoinvent: Activity
    bafu: Activity
    scale: float  # multiplies the BAFU score to give it for one unit of the ecoinvent product
    conversion: Literal["unit table", "sheet"]  # where the scale's unit factor comes from


@dataclass(frozen=True)
class Gap(Point):
    """One pair on one category, both sides positive, labelled with its ecoinvent name."""

    match: Match


# process id -> category or single score -> value
Scores = dict[str, dict[str, float]]
# category -> (ecoinvent, BAFU), both for one unit of the ecoinvent product
Compared = dict[str, tuple[float, float]]
Result = tuple[Match, Compared]
# unit name as the engine's unit table spells it -> the unit
UnitTable = dict[str, UnitDefinition]


@contextmanager
def phase(label: str) -> Iterator[None]:
    """Say what is being done, then what it cost."""
    start = time.monotonic()
    print(f"{label}...", flush=True)
    yield
    print(f"{label}: {time.monotonic() - start:.1f}s", flush=True)


def cell(value: object) -> str:
    """A cell as text, a whole number without its decimal point: 3.0 reads 3."""
    return f"{value:g}" if isinstance(value, float | int) else str(value)


def read_sheet(path: Path, sheet: str) -> tuple[list[Pair], list[tuple[Pair, str]]]:
    """Read the pairs a mapping sheet proposes, once each, and the rows that name no BAFU process, with why.

    Stops the run if the sheet's header is not the one this reads.
    """
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True)[sheet]
    rows = ws.iter_rows(values_only=True)
    # the header is the first row opening on the first column's name: editions put it on row 1 or 2
    header = next((r for r in rows if r and r[0] == COLUMNS[0]), None)
    if header is None:
        sys.exit(f"{sheet}: no header row starting with {COLUMNS[0]!r}")
    positions = [0]
    for previous, name in pairwise(COLUMNS):
        try:
            positions.append(header.index(name, positions[-1] + 1))
        except ValueError:
            sys.exit(f"{sheet}: no column {name!r} after {previous!r}")
    # a name met again before the next column read is two candidates, as when a second proposal
    # copies a header: say so rather than read whichever comes first
    for name, here, after in zip(COLUMNS, positions, positions[1:]):
        if name in header[here + 1 : after]:
            sys.exit(f"{sheet}: two columns {name!r} before {header[after]!r}, rename the one not to read")
    pairs: dict[Pair, None] = {}
    # keyed with its reason too: two rows alike but for what their BAFU cell holds stay two lines
    unmapped: dict[tuple[Pair, str], None] = {}
    for row in rows:
        ecoinvent, e_unit, bafu, geo, b_unit, conversion, source, tech, geo_grade = (row[c] for c in positions)
        if ecoinvent is None:  # a blank line under the table
            continue
        if bafu in (None, 0, "#N/A") or str(bafu).startswith("#"):
            # a spreadsheet error other than #N/A (#REF!, ...) is a broken sheet, not a missing process;
            # what the cell holds is kept, an empty cell and a #N/A can mean different things to the sheet's author
            written = "empty" if bafu is None else cell(bafu)
            why = f"no BAFU process in the sheet: {written}" if bafu in (None, 0, "#N/A") else f"sheet cell in error: {bafu}"
            unmapped.setdefault((Pair(*map(cell, (ecoinvent, e_unit)), "", "", "", "", *map(cell, (source, tech, geo_grade))), why))
        else:
            # the conversion kept whole: "%g" would round 1/3.6 to six digits
            texts = (*map(cell, (ecoinvent, e_unit, bafu, geo, b_unit)), str(conversion))
            pairs.setdefault(Pair(*texts, *map(cell, (source, tech, geo_grade))))
    return list(pairs), list(unmapped)


def key(name: str) -> str:
    """What two spellings of one name share: case and whitespace, non-breaking included, aside."""
    return " ".join(name.split()).lower()


def unit_of(units: UnitTable, name: str) -> tuple[str, UnitDefinition] | str:
    """Read a unit name as the engine does, as the table spells it, or say why it cannot be read.

    The exact spelling first; failing that, the one row equal to it with case folded. Several
    such rows (MJ and mJ for "mj") are different units, so the name is refused, not guessed.
    """
    if name in units:
        return name, units[name]
    folded = [(spelling, unit) for spelling, unit in units.items() if spelling.lower() == name.lower()]
    if len(folded) == 1:
        return folded[0]
    return f"unit {name!r}: {len(folded)} rows of the unit table read it"


def db_name(path: Path) -> str:
    """The engine's name for a database file, derived from it so its cache is reused next run."""
    return "map-" + re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")


def with_single_scores(client: Client) -> str:
    """A copy of the method with ECS and PEF added; stops the run if it lacks a category they read."""
    for old in client.list_method_collections():  # the engine keeps a previous run's copy
        if old["name"] == SCORED:
            client.delete_method_collection(SCORED)
    copy = client.copy_method_collection(COLLECTION, SCORED)["name"]
    ids = {m.name: m.id for m in client.list_methods() if m.collection == copy}
    for name, (unit, rows) in SINGLE_SCORES.items():
        missing = sorted({category for terms, _, _ in rows for category in terms} - ids.keys())
        if missing:
            sys.exit(f"{name} reads categories the method lacks ({', '.join(missing)}): is it EF 3.1 adapted?")
        scoring = [ScoringRow(" + ".join(terms), {ids[c]: k for c, k in terms.items()}, norm, weight) for terms, norm, weight in rows]
        client.create_scoring_set(copy, name, scoring, unit=unit)
    return copy


def catalogue(client: Client) -> list[Activity]:
    """List a whole database up front, so every lookup afterwards is local."""
    return list(client.search_activities(page=1, page_size=5000))


def index(activities: list[Activity], key_of) -> dict:
    """Group activities under a key: several under one key is an ambiguity the caller reports."""
    grouped: dict = {}
    for a in activities:
        grouped.setdefault(key_of(a), []).append(a)
    return grouped


def match(pair: Pair, ecoinvent: dict, bafu: dict, units: UnitTable) -> Match | str:
    """Find both processes of a pair and the factor between their units, or say why not."""
    es = ecoinvent.get(key(pair.ecoinvent), [])
    bs = bafu.get((key(pair.bafu), pair.geo), [])
    if len(es) != 1:
        return f"{len(es)} ecoinvent processes of that name"
    if len(bs) != 1:
        return f"{len(bs)} BAFU processes of that name and location"
    (e,), (b,) = es, bs
    e_unit, b_unit = unit_of(units, e.product_unit), unit_of(units, b.product_unit)
    if isinstance(e_unit, str):
        return e_unit
    if isinstance(b_unit, str):
        return b_unit
    scale = per_left_unit(e_unit[0], b_unit[0], units)
    if not isinstance(scale, DifferentMeasures):
        return Match(pair, e, b, scale, "unit table")
    scale = sheet_factor(pair, (e.product_unit, e_unit[1]), (b.product_unit, b_unit[1]), units)
    return scale if isinstance(scale, str) else Match(pair, e, b, scale, "sheet")


def sheet_factor(
    pair: Pair, ecoinvent: tuple[str, UnitDefinition], bafu: tuple[str, UnitDefinition], units: UnitTable
) -> float | str:
    """How many of the engine's BAFU units one of its ecoinvent units is, by the sheet's conversion, or why not.

    Only for units that measure different things, where the unit table cannot answer. The
    conversion is written between the sheet's two units, so each must measure what the
    engine reads on its side; the unit table then bridges the sheet's spelling and the engine's.
    """
    # an empty cell reads None, #N/A or 0, as the sheet's BAFU name does
    if pair.conversion in ("None", "#N/A", "0"):
        return f"units measure different things: {ecoinvent[0]} and {bafu[0]}, and the sheet gives no conversion"
    try:
        conversion = float(pair.conversion)
    except ValueError:
        return f"the sheet's conversion {pair.conversion!r} is not a number"
    if conversion <= 0:
        return f"the sheet's conversion {pair.conversion!r} is not a positive number"
    written = {}
    for side, spelling, (read, unit) in (("ecoinvent", pair.ecoinvent_unit, ecoinvent), ("BAFU", pair.bafu_unit, bafu)):
        sheet_unit = unit_of(units, spelling)
        if isinstance(sheet_unit, str):
            return f"the sheet's {side} {sheet_unit}"
        if sheet_unit[1].dimension != unit.dimension:
            return f"the sheet writes {spelling} on the {side} side, the engine reads {read}"
        written[side] = sheet_unit[1]
    # one engine ecoinvent unit, in the sheet's ecoinvent units, in its BAFU units, in the engine's
    return ecoinvent[1].factor / written["ecoinvent"].factor / conversion * written["BAFU"].factor / bafu[1].factor


def score(client: Client, collection: str, pids: list[str], long_term: bool) -> Scores:
    """Score processes, every category of the method and both single scores.

    Stops the run if the engine leaves any of them unanswered.
    """
    categories = {m.name for m in client.list_methods() if m.collection == collection}
    try:
        # EF stops at a hundred years, and both databases mark what comes later the same way,
        # so the switch moves the two sides together
        rows = client.score_table(pids, collection=collection, chunk=CHUNK, workers=THREADS, exclude_long_term=not long_term)
    except VoLCAError as e:
        sys.exit(f"{client.db}: {e}")
    return {
        row["process_id"]: {c: v for c, v in row.items() if c in categories}
        | {name: row[f"{name} Single score"] for name in SINGLE_SCORES}
        for row in rows
    }


def compare(m: Match, ecoinvent: Scores, bafu: Scores) -> Compared:
    """Put both processes of a pair side by side, single scores first."""
    e, b = ecoinvent[m.ecoinvent.process_id], bafu[m.bafu.process_id]
    order = [*SINGLE_SCORES, *(c for c in e if c not in SINGLE_SCORES)]
    return {c: (e[c], b[c] * m.scale) for c in order}


def positive(results: list[Result], category: str) -> list[Gap]:
    """The pairs one category can be judged on: a ratio against zero says nothing, a log scale draws neither."""
    return [Gap(m.pair.ecoinvent, x, y, m) for m, c in results for x, y in [c[category]] if x > 0 and y > 0]


def within(ratios: list[float]) -> list[float]:
    """The share of pairs inside each band, a narrower band counted in the wider ones."""
    return list(accumulate(band_shares(ratios)[label] for _, label in BANDS))


def trusted(results: list[Result], min_grade: int) -> list[Result]:
    """The pairs the sheet grades at least min_grade: a gap on a pair graded "no match" says nothing."""
    return [(m, c) for m, c in results if m.pair.tech_grade in GRADES and int(m.pair.tech_grade) >= min_grade]


def draw(results: list[Result], labelled: list[Result], path: Path) -> None:
    """One scatter per category, coloured by grade, the widest gaps among labelled pairs named."""
    grades = [*GRADES, *sorted({m.pair.tech_grade for m, _ in results} - set(GRADES))]
    style = {g: GRADES.get(g, (g, "black")) for g in grades}
    rank_of = {g: rank for rank, g in enumerate(grades)}
    categories = list(results[0][1])
    fig, axes = plt.subplots(6, 5, figsize=(25, 30))
    for ax in axes.flat[len(categories) :]:
        ax.set_visible(False)
    for ax, category in zip(axes.flat, categories):
        # the least trusted first, so the better grades are drawn over them
        points = sorted(positive(results, category), key=lambda p: rank_of[p.match.pair.tech_grade])
        left_out = len(results) - len(points)
        ax.set_title(category + (f"  ({left_out} ≤ 0 not drawn)" if left_out else ""), fontsize=9)
        if not points:
            continue
        xs, ys = [p.left for p in points], [p.right for p in points]
        ax.scatter(xs, ys, c=[style[p.match.pair.tech_grade][1] for p in points], s=4, alpha=0.5, rasterized=True)
        middle = statistics.median(xs)
        for rank, p in enumerate(widest(positive(labelled, category), LABELS_PER_GRAPH)):
            right = p.left > middle  # names near the right edge grow leftwards
            ax.scatter([p.left], [p.right], s=30, facecolor="none", edgecolor="black", zorder=3)
            ax.annotate(
                f"{p.label[:40]} {fold(p.right / p.left)}",
                (p.left, p.right),
                xytext=(-6 if right else 6, 3 + 10 * rank),  # stack them, they crowd
                textcoords="offset points",
                ha="right" if right else "left",
                fontsize=6,
            )
        lo, hi = min(*xs, *ys), max(*xs, *ys)
        ax.fill_between([lo, hi], [lo / 2, hi / 2], [lo * 2, hi * 2], color="grey", alpha=0.15, lw=0)
        ax.fill_between([lo, hi], [lo * 0.9, hi * 0.9], [lo * 1.1, hi * 1.1], color="grey", alpha=0.3, lw=0)
        ax.plot([lo, hi], [lo, hi], color="black", lw=0.5)
        ax.set(xscale="log", yscale="log")
    axes[0, 0].set(xlabel="ecoinvent 3.11", ylabel="BAFU 2026, as mapped")
    fig.legend(
        handles=[
            plt.Line2D([], [], marker="o", ls="", color=colour, label=f"technical grade {label}")
            for label, colour in style.values()
        ],
        loc="upper center",
        ncol=len(grades),
    )
    fig.subplots_adjust(top=0.96)  # matplotlib's default margin leaves the legend far above the graphs
    fig.savefig(path, bbox_inches="tight", dpi=150)


def report(results: list[Result], failed: list[tuple[Pair, str]], min_grade: int, top: int) -> None:
    """Say what was compared, what was not, and where the two databases part most."""
    print(f"\n{len(results)} pairs compared, {len(failed)} not:")
    for reason, count in Counter(reason for _, reason in failed).most_common():
        print(f"  {count:6}  {reason}")
    by_sheet = sum(m.conversion == "sheet" for m, _ in results)
    print(f"  of the compared, {by_sheet} put on one unit by the sheet's conversion, units measuring different things")
    print("\nTechnical grade of the compared pairs:")
    for grade, count in sorted(Counter(m.pair.tech_grade for m, _ in results).items()):
        print(f"  {count:6}  {GRADES.get(grade, (grade,))[0]}")
    graded = trusted(results, min_grade)
    print(f"\nBAFU / ecoinvent per category, on the {len(graded)} pairs graded {min_grade} or more:")
    columns = " ".join(f"{'within ' + label:>12}" for _, label in BANDS)
    print(f"  {'category':45} {'pairs':>6} {'median':>7} {columns} {'BAFU lower':>11}")
    for category in results[0][1]:
        ratios = [p.right / p.left for p in positive(graded, category)]
        if not ratios:
            print(f"  {category:45} nothing to compare")
            continue
        shares = " ".join(f"{share:12.0%}" for share in within(ratios))
        lower = sum(ratio < 1 for ratio in ratios) / len(ratios)
        print(f"  {category:45} {len(ratios):6} {statistics.median(ratios):7.3f} {shares} {lower:11.0%}")
    for category in TABLES:
        print(f"\nThe {top} widest gaps on {category}, pairs graded {min_grade} or more:")
        for gap in widest(positive(graded, category), top):
            p = gap.match.pair
            print(f"  {fold(gap.right / gap.left):>9}  {p.ecoinvent}  ->  {p.bafu} {{{p.geo}}}  ({p.source}, grade {p.tech_grade})")


def write_csv(results: list[Result], failed: list[tuple[Pair, str]], path: Path) -> None:
    """Keep every pair, compared or not, with both scores of every category."""
    categories = list(results[0][1]) if results else []
    head = ["source", "tech grade", "geo grade", "ecoinvent", "BAFU", "BAFU location", "status", "unit", "conversion", "factor"]
    with path.open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(head + [f"{c} {side}" for c in categories for side in ("ecoinvent", "BAFU")])
        for m, compared in results:
            p = m.pair
            fixed = [p.source, p.tech_grade, p.geo_grade, p.ecoinvent, p.bafu, p.geo, "compared"]
            out.writerow(
                fixed + [m.ecoinvent.product_unit, m.conversion, m.scale] + [v for c in categories for v in compared[c]]
            )
        for p, reason in failed:
            out.writerow([p.source, p.tech_grade, p.geo_grade, p.ecoinvent, p.bafu, p.geo, reason])


def main() -> None:
    """Run the whole comparison: read the sheet, start the engine, load, match, score, report, draw."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mapping", type=Path, help="the mapping workbook")
    parser.add_argument("--sheet", default="Matching Full")
    parser.add_argument("--ecoinvent", type=Path, required=True, help="ecoinvent 3.11 as a SimaPro CSV export")
    parser.add_argument("--bafu", type=Path, required=True, help="BAFU 2026 as its EcoSpold 1 archive")
    parser.add_argument("--method", type=Path, required=True, help="a SimaPro method export, zipped or plain CSV")
    parser.add_argument(
        "--long-term",
        action="store_true",
        help="count emissions released beyond a hundred years, on both sides, as published results usually do",
    )
    parser.add_argument("--top", type=int, default=20, help="how many of the widest gaps to list")
    parser.add_argument(
        "--min-grade", type=int, default=2, help="lowest technical grade the statistics and gap lists count"
    )
    args = parser.parse_args()
    # appended, not substituted: a workbook named "EI 3.11 …" has no suffix to replace.
    # The convention is part of the name: a run counting long-term emissions gives other
    # numbers than one leaving them out, and must not overwrite it.
    convention = " - long-term" if args.long_term else ""
    out = args.mapping.with_name(f"{args.mapping.stem} - {args.sheet}{convention}")
    csv_path, svg_path = out.with_name(out.name + ".csv"), out.with_name(out.name + ".svg")
    started = time.monotonic()

    with phase(f"reading {args.sheet}"):
        pairs, unmapped = read_sheet(args.mapping, args.sheet)
    print(f"{len(pairs)} distinct pairs, {len(unmapped)} ecoinvent rows without a BAFU process")
    ei_db, bafu_db = db_name(args.ecoinvent), db_name(args.bafu)
    if "VOLCA_BINARY" not in os.environ:
        volca.download()  # the latest release, once per release

    with Server(databases={ei_db: args.ecoinvent, bafu_db: args.bafu}, methods={COLLECTION: args.method}) as server:
        client = Client(server.base_url)
        version = client.get_version()
        print(f"VoLCA {version.version} ({version.git_hash}) on {server.base_url}")
        counted = "counted" if args.long_term else "excluded"
        print(f"{args.method.name}, long-term emissions {counted}")
        units = client.unit_table()
        scored = with_single_scores(client)
        listed = {}
        for db in (ei_db, bafu_db):
            with phase(f"loading {db}"):
                client.load_database(db)
                listed[db] = catalogue(client.use(db))
        ecoinvent = index(listed[ei_db], lambda a: key(a.product_name.rsplit(" | ", 1)[0]))
        bafu = index(listed[bafu_db], lambda a: (key(a.activity_name), a.location))
        matched = [(p, match(p, ecoinvent, bafu, units)) for p in pairs]
        found = [m for _, m in matched if isinstance(m, Match)]
        failed = unmapped + [(p, m) for p, m in matched if isinstance(m, str)]
        ei_pids, bafu_pids = list({m.ecoinvent.process_id for m in found}), list({m.bafu.process_id for m in found})
        with phase(f"scoring {len(ei_pids)} ecoinvent processes"):
            ei_scores = score(client.use(ei_db), scored, ei_pids, args.long_term)
        with phase(f"scoring {len(bafu_pids)} BAFU processes"):
            bafu_scores = score(client.use(bafu_db), scored, bafu_pids, args.long_term)

    results = [(m, compare(m, ei_scores, bafu_scores)) for m in found]
    write_csv(results, failed, csv_path)
    if not results:
        sys.exit(f"no pair could be compared: {csv_path} gives the reason for each")
    report(results, failed, args.min_grade, args.top)
    with phase("drawing"):
        draw(results, trusted(results, args.min_grade), svg_path)
    print(f"written {csv_path} and {svg_path}, {time.monotonic() - started:.0f}s in total")


if __name__ == "__main__":
    main()
