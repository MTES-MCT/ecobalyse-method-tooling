# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.7.1", "openpyxl"]
# ///
"""Extract Agribalyse recipes (ingredient bill of materials) to Excel.

Self-contained: downloads the VoLCA engine binary + reference-data bundle,
starts it locally, loads an Agribalyse database, and writes one row per
ingredient for every selected transformed product::

    1 kg "Pizza, ..."  ->  X kg tomato + Y kg cheese + Z kg flour + ...

What `volca.download()` gives you and what it does NOT
-----------------------------------------------------
`download()` fetches the engine binary and the *reference-data* bundle
(flow synonyms, units, compartments, geographies). It does NOT ship any LCA
database. You supply Agribalyse yourself — the official SimaPro CSV export is
a free public download from ADEME (data.gouv.fr / doc.agribalyse.fr). Pass its
path with --agribalyse. A Brightway/Excel export of Agribalyse works too — the
engine detects the format from the file.

Run (uv resolves the two deps automatically)::

    # a few products by name
    uv run extract_agribalyse_recipes.py \
        --agribalyse "/path/to/AGB32_final.CSV.zip" \
        --select Pizza --select Bread --out recipes.xlsx

    # the whole Agribalyse food catalogue
    uv run extract_agribalyse_recipes.py \
        --agribalyse "/path/to/AGB32_final.CSV.zip" \
        --all --limit 0 --out all_recipes.xlsx

Selection: --select NAME (substring), --classification "System=Value", or --all
(shortcut for the food catalogue). --limit caps each selector (0 = no cap).

The "recipe" is just the process's technosphere inputs: get_activity(pid)
.technosphere_inputs gives (ingredient name, amount, unit, upstream process).
Each row is classified (raw_material / water / electricity / heat / transport /
other) so you can keep only the food ingredients if you want.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from itertools import islice
from pathlib import Path

from openpyxl import Workbook

from volca import Client, Server, download
from volca.agribalyse import classify_exchange

# Reference-data blocks resolve against the downloaded bundle: the engine
# rewrites any "data/..." path to "$VOLCA_DATA_DIR/...", which Server points at
# the bundle installed by download(). Only these four files ship in the bundle.
_CONFIG_TEMPLATE = """\
geographies = "data/geographies.csv"

[server]
port = {port}
host = "127.0.0.1"

[[databases]]
name = "{db_name}"
path = {db_path}
load = false

[[flow-synonyms]]
name = "flows"
path = "data/flows.csv"
active = true

[[compartment-mappings]]
name = "compartments"
path = "data/compartments.csv"
active = true

[[units]]
name = "units"
path = "data/units.csv"
active = true
"""

# Recipe columns: functional unit of the product, then one ingredient per row.
_HEADER = [
    "product_process_id",
    "product_name",
    "location",
    "functional_unit_amount",
    "functional_unit",
    "ingredient_name",
    "ingredient_amount",
    "ingredient_unit",
    "role",
    "ingredient_process_id",
]


def recipe_rows(client: Client, pid: str) -> list[list]:
    """One row per technosphere input of `pid` — its ingredient bill."""
    act = client.get_activity(pid)
    fu_amount = act.product_amount if act.product_amount is not None else 1.0
    fu_unit = act.product_unit or act.unit
    rows = []
    for e in act.technosphere_inputs:
        if e.is_reference:  # the product itself, not an ingredient
            continue
        rows.append([
            act.process_id,
            act.activity_name,
            act.location,
            fu_amount,
            fu_unit,
            e.flow_name,
            e.amount,
            e.unit,
            classify_exchange(e),
            e.target_process_id or "",
        ])
    return rows


# The Agribalyse composite/prepared foods (pizza, ratatouille, yogurt cake, ...)
# live under this classification. --all is a shortcut for it; override with
# --classification "System=Value" for any other slice (contains-match).
_FOOD_CATALOGUE = ("Category", "Agricultural\\Food")


def _take(results, limit: int, label: str) -> list:
    """First `limit` results (all if limit == 0), fetching pages lazily.

    islice stops after `limit`, so a capped run never pulls the whole
    catalogue; `len(results)` reports the server-side total for the warning.
    """
    kept = list(islice(results, limit)) if limit else list(results)
    total = len(results)
    if limit and total > len(kept):
        print(f"  {label}: {total} matches, keeping first {len(kept)} (raise --limit / --limit 0 for all)")
    else:
        print(f"  {label}: {len(kept)} products")
    return kept


def selected_products(client: Client, selects: list[str],
                      classifications: list[tuple[str, str]], limit: int):
    """Yield every selected product, from name terms and classification slices.

    `limit` caps each selector (0 = no cap). Truncation is always reported —
    never a silent cut.
    """
    page = limit or 200  # wire page size; islice bounds the total taken
    for query in selects:
        res = client.search_activities(name=query, limit=page)
        yield from _take(res, limit, repr(query))
    for system, value in classifications:
        res = client.search_activities(
            classification=system, classification_value=value, limit=page)
        yield from _take(res, limit, f"{system}={value}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agribalyse", required=True,
                    help="Path to the Agribalyse source. The engine auto-detects the "
                         "format: SimaPro CSV (.csv/.csv.zip), EcoSpold, ILCD, or a "
                         "Brightway/Excel export (.xlsx).")
    ap.add_argument("--db-name", default="agribalyse-3.2")
    ap.add_argument("--select", action="append", default=[],
                    help="Product-name substring to extract (repeatable).")
    ap.add_argument("--classification", action="append", default=[], metavar="SYSTEM=VALUE",
                    help='Select by classification, e.g. "Category=Agricultural\\Food" (repeatable).')
    ap.add_argument("--all", action="store_true",
                    help="Extract the whole Agribalyse food catalogue "
                         f"({_FOOD_CATALOGUE[0]}={_FOOD_CATALOGUE[1]}). Pair with --limit 0 for everything.")
    ap.add_argument("--limit", type=int, default=3,
                    help="Max products per selector (0 = no cap; extracts all matches).")
    ap.add_argument("--out", default="agribalyse_recipes.xlsx")
    ap.add_argument("--engine-version", default=None,
                    help="VoLCA release tag (default: latest)")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--startup-timeout", type=int, default=600,
                    help="Seconds to wait for the engine to become ready")
    args = ap.parse_args()

    classifications: list[tuple[str, str]] = []
    for spec in args.classification:
        if "=" not in spec:
            ap.error(f"--classification must be SYSTEM=VALUE, got {spec!r}")
        system, value = spec.split("=", 1)
        classifications.append((system, value))
    if args.all:
        classifications.append(_FOOD_CATALOGUE)
    selects = args.select
    if not selects and not classifications:
        selects = ["Pizza"]  # demo default when nothing is asked for

    db_path = str(Path(args.agribalyse).expanduser().resolve())

    print("1. Downloading VoLCA engine + reference data ...")
    inst = download(version=args.engine_version)
    print(f"   binary  {inst.binary}")
    print(f"   data    {inst.data_dir}  (engine {inst.version}, data {inst.data_version})")

    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "volca.toml"
        config.write_text(_CONFIG_TEMPLATE.format(
            # json.dumps produces a valid TOML basic string: quotes the path and
            # escapes backslashes (Windows paths) and any embedded quote.
            port=args.port, db_name=args.db_name, db_path=json.dumps(db_path),
        ))

        print("2. Starting engine ...")
        # Pass the binary from download() explicitly so the exact downloaded
        # version is used, not whatever the default lookup finds first on PATH
        # or in the shared install root.
        # startup_timeout is generous: the very first engine run after a fresh
        # download indexes any bundled reference methods before it serves.
        srv = Server(config=str(config), port=args.port, binary=str(inst.binary))
        srv.start(idle_timeout=1800, wait_timeout=args.startup_timeout)
        try:
            client = Client(base_url=srv.base_url, db=args.db_name)

            print(f"3. Loading {args.db_name} (first load parses the CSV; later loads hit the cache) ...")
            client.load_database(args.db_name)

            print("4. Extracting recipes ...")
            wb = Workbook()
            ws = wb.active
            ws.title = "recipes"
            ws.append(_HEADER)
            n_products = n_rows = 0
            for product in selected_products(client, selects, classifications, args.limit):
                rows = recipe_rows(client, product.process_id)
                for row in rows:
                    ws.append(row)
                n_products += 1
                n_rows += len(rows)
                print(f"   {product.activity_name[:64]:64}  {len(rows)} ingredients")

            wb.save(args.out)
            print(f"\nDone: {n_products} products, {n_rows} ingredient rows -> {args.out}")
        finally:
            srv.stop()


if __name__ == "__main__":
    main()
