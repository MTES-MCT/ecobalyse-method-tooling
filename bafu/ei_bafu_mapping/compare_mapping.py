# /// script
# requires-python = ">=3.12"
# dependencies = ["pyvolca>=0.12", "openpyxl", "matplotlib"]
# ///
"""Hold ecoinvent processes against the BAFU processes a mapping sheet points them to.

One scatter per EF 3.1 impact category, plus the ECS and PEF single scores: a point is one
pair the sheet proposes, x what the ecoinvent 3.11 process scores, y what the BAFU process
it is mapped to scores, both for one unit of the ecoinvent product, coloured by the technical
grade the sheet gives the pair. The command stands on its own: it installs the engine,
starts it on a free port, loads both databases from their files, scores every mapped
process, draws, and writes one CSV line per pair saying what became of it.

    uv run compare_mapping.py mapping.xlsx --ecoinvent ei.zip --bafu bafu.zip --method ef31.zip

VOLCA_BINARY with VOLCA_DATA_DIR runs a local engine build and its data instead of the
release, which is then not downloaded.
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from math import copysign, log
from pathlib import Path
from typing import Literal, NamedTuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import openpyxl  # noqa: E402
import volca  # noqa: E402
from volca import Activity, Client, Server  # noqa: E402

# the sheet's columns this reads, by position: its header names two of them "Geography"
COLUMNS = {
    "EI 3.11 dataset": 0,
    "Unit": 3,
    "BAFU Name": 4,
    "Geography": 5,
    "unit": 6,
    "conversion": 7,
    "Source": 8,
    "Tech\nGrade": 9,
    "Geo Grade": 10,
}
# the technical grade, as the sheet's evaluation rules word it, drawn from the least trusted up
GRADES = {
    "0": ("0, no match", "lightgrey"),
    "1": ("1, bad proxy or to be checked", "tab:orange"),
    "2": ("2, partial match or good proxy", "tab:blue"),
    "3": ("3, perfect match", "tab:green"),
}
# how close a pair counts as: the widest factor between the two sides, whichever way round
BANDS = ((1.1, "10%"), (1.5, "×1.5"), (2, "×2"), (10, "×10"))
SINGLE_SCORES = ("ECS", "PEF")
TABLES = ("ECS", "Climate change")  # the two indicators the sheet itself tracks a gap on
LABELS_PER_GRAPH = 3
COLLECTION = "EF 3.1"  # the method as this tool loads it; the engine also carries its own built-in ones
THREADS = 8  # parallel scoring requests: the engine gains little past this
CHUNK = 250  # processes per request

# Ecobalyse's environmental cost and the EF 3.1 single score, on the adapted names.
# Ecobalyse's own corrections (uranium in fossil resources) are left out: this is EF 3.1.
# Both read freshwater ecotoxicity as its two parts: from adapted 1.05 on, the whole
# category carries almost no factor of its own and scores near zero.
SCORING = """
[[methods.scoring]]
name = "ECS"
unit = "Pts"
variables = { cch = "Climate change", acd = "Acidification", fwe = "Eutrophication, freshwater", swe = "Eutrophication, marine", tre = "Eutrophication, terrestrial", etfo = "Ecotoxicity, freshwater - organics", etfi = "Ecotoxicity, freshwater - inorganics", fru = "Resource use, fossils", mru = "Resource use, minerals and metals", ior = "Ionising radiation", ldu = "Land use", ozd = "Ozone depletion", pco = "Photochemical ozone formation", pma = "Particulate matter", wtu = "Water use" }
computed = { etf = "2 * etfo + etfi" }
normalization = { cch = 7553.08, acd = 55.5695, etf = 98120.0, fwe = 1.60685, swe = 19.5452, tre = 176.755, fru = 65004.3, mru = 0.0636226, ior = 4220.16, ldu = 819498.0, ozd = 0.053648, pco = 40.8592, pma = 0.000595367, wtu = 11468.7 }
weighting = { cch = 0.2106, acd = 0.0491, etf = 0.2106, fwe = 0.0222, swe = 0.0235, tre = 0.0294, fru = 0.0659, mru = 0.0598, ior = 0.0397, ldu = 0.0629, ozd = 0.05, pco = 0.0379, pma = 0.071, wtu = 0.0674 }
scores = { total = "cch + acd + etf + fwe + swe + tre + fru + mru + ior + ldu + ozd + pco + pma + wtu" }

[[methods.scoring]]
name = "PEF"
unit = "Pt"
variables = { acd = "Acidification", cch = "Climate change", etfo = "Ecotoxicity, freshwater - organics", etfi = "Ecotoxicity, freshwater - inorganics", pma = "Particulate matter", swe = "Eutrophication, marine", fwe = "Eutrophication, freshwater", tre = "Eutrophication, terrestrial", htc = "Human toxicity, cancer", htn = "Human toxicity, non-cancer", ior = "Ionising radiation", ldu = "Land use", ozd = "Ozone depletion", pco = "Photochemical ozone formation", fru = "Resource use, fossils", mru = "Resource use, minerals and metals", wtu = "Water use" }
computed = { etf = "etfo + etfi" }
normalization = { acd = 55.5695, cch = 7553.08, etf = 56716.6, pma = 0.000595367, swe = 19.5452, fwe = 1.60685, tre = 176.755, htc = 1.72529e-05, htn = 0.000128736, ior = 4220.16, ldu = 819498.0, ozd = 0.0523484, pco = 40.8592, fru = 65004.3, mru = 0.0636226, wtu = 11468.7 }
weighting = { acd = 0.062, cch = 0.2106, etf = 0.0192, pma = 0.0896, swe = 0.0296, fwe = 0.028, tre = 0.0371, htc = 0.0213, htn = 0.0184, ior = 0.0501, ldu = 0.0794, ozd = 0.0631, pco = 0.0478, fru = 0.0832, mru = 0.0755, wtu = 0.0851 }
scores = { total = "acd + cch + etf + pma + swe + fwe + tre + htc + htn + ior + ldu + ozd + pco + fru + mru + wtu" }
"""


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


class Unit(NamedTuple):
    """What the engine's units.csv says of one unit."""

    dimension: str
    factor: float  # to the dimension's reference unit


# process id -> category or single score -> value
Scores = dict[str, dict[str, float]]
# category -> (ecoinvent, BAFU), both for one unit of the ecoinvent product
Compared = dict[str, tuple[float, float]]
Result = tuple[Match, Compared]
# unit name as units.csv spells it -> the unit
UnitTable = dict[str, Unit]


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
    first = next(iter(COLUMNS))
    header = next((r for r in rows if r and r[0] == first), None)
    if header is None:
        sys.exit(f"{sheet}: no header row starting with {first!r}")
    for name, col in COLUMNS.items():
        if header[col] != name:
            sys.exit(f"{sheet}: column {col + 1} is {header[col]!r}, expected {name!r}")
    pairs: dict[Pair, None] = {}
    unmapped: dict[Pair, str] = {}
    for row in rows:
        ecoinvent, e_unit, bafu, geo, b_unit, conversion, source, tech, geo_grade = (row[c] for c in COLUMNS.values())
        if ecoinvent is None:  # a blank line under the table
            continue
        if bafu in (None, 0, "#N/A") or str(bafu).startswith("#"):
            # a spreadsheet error other than #N/A (#REF!, ...) is a broken sheet, not a missing process
            why = "no BAFU process in the sheet" if bafu in (None, 0, "#N/A") else f"sheet cell in error: {bafu}"
            unmapped.setdefault(Pair(*map(cell, (ecoinvent, e_unit)), "", "", "", "", *map(cell, (source, tech, geo_grade))), why)
        else:
            # the conversion kept whole: "%g" would round 1/3.6 to six digits
            texts = (*map(cell, (ecoinvent, e_unit, bafu, geo, b_unit)), str(conversion))
            pairs.setdefault(Pair(*texts, *map(cell, (source, tech, geo_grade))))
    return list(pairs), list(unmapped.items())


def key(name: str) -> str:
    """What two spellings of one name share: case and whitespace, non-breaking included, aside."""
    return " ".join(name.split()).lower()


def unit_table(text: str) -> UnitTable:
    """Read the engine's units.csv, every name spelled as the file spells it."""
    return {
        row["name"]: Unit(row["dimension"], float(row["factor"]))
        for row in csv.DictReader(text.splitlines())
        if row["name"] and not row["name"].startswith("#")
    }


def unit_of(units: UnitTable, name: str) -> Unit | str:
    """Read a unit name as the engine does, or say why it cannot be read.

    The exact spelling first; failing that, the one row equal to it with case folded. Several
    such rows (MJ and mJ for "mj") are different units, so the name is refused, not guessed.
    """
    if name in units:
        return units[name]
    folded = [unit for spelling, unit in units.items() if spelling.lower() == name.lower()]
    if len(folded) == 1:
        return folded[0]
    return f"unit {name!r}: {len(folded)} rows of units.csv read it"


def db_name(path: Path) -> str:
    """The engine's name for a database file, derived from it so its cache is reused next run."""
    return "map-" + re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")


def config_toml(ecoinvent: Path, bafu: Path, method: Path) -> str:
    """The engine configuration: both databases, and the method with its single scores."""

    def toml_path(path: Path) -> str:
        # absolute, since the engine reads relative paths from the config's own temporary
        # folder; JSON quoting is a valid TOML string, Windows backslashes included
        return json.dumps(str(path.resolve()))

    return f"""[server]
port = 0

[[databases]]
name = "{db_name(ecoinvent)}"
path = {toml_path(ecoinvent)}

[[databases]]
name = "{db_name(bafu)}"
path = {toml_path(bafu)}

[[methods]]
name = "{COLLECTION}"
path = {toml_path(method)}
{SCORING}"""


@contextmanager
def engine(toml: str, binary: str) -> Iterator[str]:
    """Start an engine of this tool's own on a free port. Yields its URL, stops it on the way out."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "volca.toml"
        path.write_text(toml)
        server = Server(str(path), port="auto", binary=binary)
        # a released engine counts idle time from a request's arrival, not while it runs:
        # the default 300 s would stop it under a long scoring
        server.start(idle_timeout=3600)
        try:
            yield server.base_url
        finally:
            server.stop()


def installation() -> tuple[str, Path]:
    """The engine binary and its data directory: a local build when named, else the release."""
    if "VOLCA_BINARY" not in os.environ:
        installed = volca.download()
        return str(installed.binary), installed.data_dir
    if "VOLCA_DATA_DIR" not in os.environ:
        sys.exit("VOLCA_BINARY needs VOLCA_DATA_DIR, the data directory of the same build")
    return os.environ["VOLCA_BINARY"], Path(os.environ["VOLCA_DATA_DIR"])


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
    if e_unit.dimension == b_unit.dimension:
        per_unit, conversion = e_unit.factor / b_unit.factor, "unit table"
    else:
        per_unit, conversion = sheet_factor(pair, (e.product_unit, e_unit), (b.product_unit, b_unit), units), "sheet"
        if isinstance(per_unit, str):
            return per_unit
    # a score is for one reference unit, the sign of the product amount included, as
    # Ecobalyse's comparison against Brightway established
    scale = copysign(per_unit, b.product_amount) * copysign(1, e.product_amount)
    return Match(pair, e, b, scale, conversion)


def sheet_factor(pair: Pair, ecoinvent: tuple[str, Unit], bafu: tuple[str, Unit], units: UnitTable) -> float | str:
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
        if sheet_unit.dimension != unit.dimension:
            return f"the sheet writes {spelling} on the {side} side, the engine reads {read}"
        written[side] = sheet_unit
    # one engine ecoinvent unit, in the sheet's ecoinvent units, in its BAFU units, in the engine's
    return ecoinvent[1].factor / written["ecoinvent"].factor / conversion * written["BAFU"].factor / bafu[1].factor


def score(url: str, db: str, pids: list[str], long_term: bool) -> Scores:
    """Score processes in parallel batches, every category and both single scores.

    Stops the run if the engine leaves any of them unanswered.
    """

    def batch(chunk: list[str]) -> volca.BatchScores:
        # EF stops at a hundred years, and both databases mark what comes later the same way,
        # so the switch moves the two sides together
        return Client(url, db=db).score_activities(chunk, collection=COLLECTION, top_flows=0, exclude_long_term=not long_term)

    chunks = [pids[i : i + CHUNK] for i in range(0, len(pids), CHUNK)]
    with ThreadPoolExecutor(THREADS) as pool:
        batches = list(pool.map(batch, chunks))
    unscored = [pid for b in batches for pid in b.not_found + b.invalid + b.unscorable]
    if unscored:
        sys.exit(f"{db}: the engine did not score {unscored}")
    scored = [s for b in batches for s in b.results]
    # a scoring set naming one category the method does not have is dropped whole, and
    # only the engine's log says so: stop here rather than on a missing key later
    missing = [name for name in SINGLE_SCORES if scored and name not in scored[0].impacts.scoring_results]
    if missing:
        sys.exit(f"{db}: the engine returned no {', '.join(missing)}: this method names other categories")
    return {
        s.process_id: {r.category: r.score for r in s.impacts.results}
        | {name: s.impacts.scoring_results[name]["total"] for name in SINGLE_SCORES}
        for s in scored
    }


def compare(m: Match, ecoinvent: Scores, bafu: Scores) -> Compared:
    """Put both processes of a pair side by side, single scores first."""
    e, b = ecoinvent[m.ecoinvent.process_id], bafu[m.bafu.process_id]
    order = [*SINGLE_SCORES, *(c for c in e if c not in SINGLE_SCORES)]
    return {c: (e[c], b[c] * m.scale) for c in order}


def positive(results: list[Result], category: str) -> list[tuple[float, float, Match]]:
    """The pairs one category can be judged on: a ratio against zero says nothing, a log scale draws neither."""
    return [(x, y, m) for m, c in results for x, y in [c[category]] if x > 0 and y > 0]


def widest(points: list[tuple[float, float, Match]], count: int) -> list[tuple[float, float, Match]]:
    """The pairs furthest from agreement, a factor 10 up scoring like a factor 10 down."""
    return sorted(points, key=lambda p: abs(log(p[1] / p[0])), reverse=True)[:count]


def fold(ratio: float) -> str:
    """A ratio as the factor it is: ×3.2 or ÷3.2."""
    return f"×{ratio:.3g}" if ratio >= 1 else f"÷{1 / ratio:.3g}"


def proximity(ratios: list[float]) -> list[float]:
    """The share of pairs inside each band, a factor up counting like the same factor down."""
    folds = [max(ratio, 1 / ratio) for ratio in ratios]
    return [sum(fold <= edge for fold in folds) / len(folds) for edge, _ in BANDS]


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
        points = sorted(positive(results, category), key=lambda p: rank_of[p[2].pair.tech_grade])
        left_out = len(results) - len(points)
        ax.set_title(category + (f"  ({left_out} ≤ 0 not drawn)" if left_out else ""), fontsize=9)
        if not points:
            continue
        xs, ys, drawn = zip(*points)
        ax.scatter(xs, ys, c=[style[m.pair.tech_grade][1] for m in drawn], s=4, alpha=0.5, rasterized=True)
        middle = statistics.median(xs)
        for rank, (x, y, m) in enumerate(widest(positive(labelled, category), LABELS_PER_GRAPH)):
            right = x > middle  # names near the right edge grow leftwards
            ax.scatter([x], [y], s=30, facecolor="none", edgecolor="black", zorder=3)
            ax.annotate(
                f"{m.pair.ecoinvent[:40]} {fold(y / x)}",
                (x, y),
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
    columns = " ".join(f"{'within ' + label:>11}" for _, label in BANDS)
    print(f"  {'category':45} {'pairs':>6} {'median':>7} {columns} {'BAFU lower':>11}")
    for category in results[0][1]:
        ratios = [y / x for x, y, _ in positive(graded, category)]
        if not ratios:
            print(f"  {category:45} nothing to compare")
            continue
        shares = " ".join(f"{share:11.0%}" for share in proximity(ratios))
        lower = sum(ratio < 1 for ratio in ratios) / len(ratios)
        print(f"  {category:45} {len(ratios):6} {statistics.median(ratios):7.3f} {shares} {lower:11.0%}")
    for category in TABLES:
        print(f"\nThe {top} widest gaps on {category}, pairs graded {min_grade} or more:")
        for x, y, m in widest(positive(graded, category), top):
            p = m.pair
            print(f"  {fold(y / x):>9}  {p.ecoinvent}  ->  {p.bafu} {{{p.geo}}}  ({p.source}, grade {p.tech_grade})")


def write_csv(results: list[Result], failed: list[tuple[Pair, str]], path: Path) -> None:
    """Keep every pair, compared or not, with both scores of every category."""
    categories = list(results[0][1]) if results else []
    head = ["source", "tech grade", "geo grade", "ecoinvent", "BAFU", "BAFU location", "status", "unit", "conversion"]
    with path.open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(head + [f"{c} {side}" for c in categories for side in ("ecoinvent", "BAFU")])
        for m, compared in results:
            p = m.pair
            fixed = [p.source, p.tech_grade, p.geo_grade, p.ecoinvent, p.bafu, p.geo, "compared"]
            out.writerow(
                fixed + [m.ecoinvent.product_unit, m.conversion] + [v for c in categories for v in compared[c]]
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
    binary, data_dir = installation()
    units = unit_table((data_dir / "units.csv").read_text())
    toml = config_toml(args.ecoinvent, args.bafu, args.method)
    ei_db, bafu_db = db_name(args.ecoinvent), db_name(args.bafu)

    with engine(toml, binary) as url:
        version = Client(url).get_version()
        print(f"VoLCA {version.version} ({version.git_hash}) on {url}")
        counted = "counted" if args.long_term else "excluded"
        print(f"{args.method.name}, long-term emissions {counted}")
        listed = {}
        for db in (ei_db, bafu_db):
            with phase(f"loading {db}"):
                client = Client(url, db=db)
                client.load_database(db)
                listed[db] = catalogue(client)
        ecoinvent = index(listed[ei_db], lambda a: key(a.product_name.rsplit(" | ", 1)[0]))
        bafu = index(listed[bafu_db], lambda a: (key(a.activity_name), a.location))
        matched = [(p, match(p, ecoinvent, bafu, units)) for p in pairs]
        found = [m for _, m in matched if isinstance(m, Match)]
        failed = unmapped + [(p, m) for p, m in matched if isinstance(m, str)]
        ei_pids, bafu_pids = list({m.ecoinvent.process_id for m in found}), list({m.bafu.process_id for m in found})
        with phase(f"scoring {len(ei_pids)} ecoinvent processes"):
            ei_scores = score(url, ei_db, ei_pids, args.long_term)
        with phase(f"scoring {len(bafu_pids)} BAFU processes"):
            bafu_scores = score(url, bafu_db, bafu_pids, args.long_term)

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
