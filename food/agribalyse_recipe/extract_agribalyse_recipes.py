# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.8.0", "openpyxl"]
# ///
"""Extract Agribalyse recipes (ingredient bill of materials) to Excel.

Self-contained: downloads the VoLCA engine binary, starts it locally, uploads
an Agribalyse database (once — later runs reuse the uploaded copy), and writes
one row per ingredient for every selected transformed product::

    1 kg "Pizza, ..."  ->  X kg tomato + Y kg cheese + Z kg flour + ...

What `volca.download()` gives you and what it does NOT
-----------------------------------------------------
`download()` fetches the engine binary and the reference-data bundle. It does
NOT ship any LCA database. You supply Agribalyse yourself — the official
SimaPro CSV export is a free public download from ADEME (data.gouv.fr /
doc.agribalyse.fr). Pass its path with --agribalyse. A Brightway/Excel export
of Agribalyse works too — the engine detects the format from the file. The
upload persists in the engine's shared install dir, so a re-run skips the
upload; pass --replace to re-upload after the source file changed.

Run (uv resolves the two deps automatically)::

    # a few products by name
    uv run extract_agribalyse_recipes.py \
        --agribalyse "/path/to/AGB32_final.CSV.zip" \
        --select Pizza --select Bread --out recipes.xlsx

    # the whole Agribalyse food catalogue, edible ingredients only
    uv run extract_agribalyse_recipes.py \
        --agribalyse "/path/to/AGB32_final.CSV.zip" \
        --all --limit 0 --ingredients-only --out all_recipes.xlsx

Selection: --select NAME (substring), --classification "System=Value", or --all
(shortcut for the food catalogue). --limit caps each selector (0 = no cap).
--ingredients-only keeps only edible inputs (Category type = material, recipe
water included), dropping cooking, [Dummy] operations, waste treatment,
electricity, heat and transport.

The "recipe" is just the process's technosphere inputs: get_activity(pid)
.technosphere_inputs gives (ingredient name, amount, unit, upstream process).
Each row is classified (raw_material / water / electricity / heat / transport /
other) so you can keep only the food ingredients if you want.

--packaging: the packaging bill in the same sheet
--------------------------------------------------
A recipe carries no packaging — Agribalyse models it one stage downstream::

    Pizza, ... | Chilled | Cardboard | at packaging {FR}
    |-- 1 kg  Pizza, ..., at plant {FR}                        <- the recipe
    +-- 1 kg  Pizzas, chilled, 450g | Packaging System, N0, ...
              |-- N1 elements (bag, cardboard support, labels)
              +-- N2/N3 grouping box and pallet

--packaging finds that stage (the recipe's direct consumer under Category
"Agricultural\\Food\\Packaging"), reads its bill of materials and appends the
rows to the same sheet as the ingredients, with role `packaging_material` or
`packaging_eol` and one extra column, `packaging_system`. Amounts are per
functional unit of the product, like the ingredient rows: a 450 g packaging
system counts 2.22 times per kg, and the engine does that scaling.

Kept: packaging materials and their end of life. Dropped: the conversion and
upstream-transport processes that also live in a packaging element's inventory
— a choice, not an oversight.

Docs: https://www.volca.run/docs/python/
"""

from __future__ import annotations

import argparse
import tempfile
from itertools import islice
from pathlib import Path

from openpyxl import Workbook

from volca import ActivityDetail, ClassificationFilter, Client, Server, download
from volca.agribalyse import classify_exchange

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

# Appended by --packaging: which packaging system a packaging row comes from,
# empty on ingredient rows. The material family needs no column of its own —
# it opens the material name ("Plastic, LDPE, ...", "Cardboard, Flat, ...").
_PACKAGING_HEADER = "packaging_system"


# Roles (from classify_exchange) an edible ingredient can carry: kg inputs are
# `raw_material`; gram-based dairy/sugar/chocolate fall to `other`; `water` is
# recipe water, kept because its amount matters. Every other role — wastewater,
# biowaste, electricity, heat, transport, cleaning, infrastructure — is never an
# ingredient, so material-tagged waste treatment is dropped even though
# Agribalyse also tags it `material`.
_INGREDIENT_ROLES = {"raw_material", "other", "water"}

# Food is weighed or counted, never a gas/bulk volume. This drops the last
# material-tagged utilities that share a food role — natural gas, compressed
# air — the only inputs left in m3 once waste and tap water are gone.
_NON_FOOD_UNITS = {"m3"}


def product_columns(activity) -> list:
    """The five product columns every row of a product repeats.

    Takes a search result or a fetched activity — both carry the product's
    amount and unit, the fetched one also a bare `unit` as last resort. One
    definition so ingredient and packaging rows can never disagree on the
    functional unit they are expressed against.
    """
    amount = activity.product_amount if activity.product_amount is not None else 1.0
    unit = activity.product_unit or getattr(activity, "unit", None) or ""
    return [activity.process_id, activity.activity_name, activity.location, amount, unit]


def recipe_rows(client: Client, pid: str,
                ingredient_pids: set[str] | None = None) -> list[list]:
    """One row per technosphere input of `pid` — its ingredient bill.

    When `ingredient_pids` is given, keep only edible ingredients: inputs whose
    producing activity is tagged `Category type = material` (excludes cooking,
    [Dummy] operations, waste treatment, electricity, heat, transport), whose
    role is a food role — including recipe water, dropping biowaste treatment
    (also `material`) — and whose unit is a food unit (drops natural gas and
    compressed air, the last `material` utilities, billed in m3).
    """
    act = client.get_activity(pid)
    prefix = product_columns(act)
    rows = []
    for e in act.technosphere_inputs:
        if e.is_reference:  # the product itself, not an ingredient
            continue
        role = classify_exchange(e)
        if ingredient_pids is not None and (
            e.target_process_id not in ingredient_pids
            or role not in _INGREDIENT_ROLES
            or (e.unit or "").lower() in _NON_FOOD_UNITS
        ):
            continue
        rows.append(prefix + [
            e.flow_name,
            e.amount,
            e.unit,
            role,
            e.target_process_id or "",
        ])
    return rows


def material_pids(client: Client) -> set[str]:
    """Process-ids of every `Category type = material` activity.

    Agribalyse tags real edible materials (flour, butter, oils, …) `material`,
    while cooking/mixing are `processing`, energy is `energy`, transport
    `transport`, and end-of-life is `waste treatment`. One classification sweep
    gives the whole set; membership then filters a recipe to its ingredients.
    """
    res = client.search_activities(
        classification="Category type", classification_value="material", limit=200)
    return {a.process_id for a in res}


# Packaging sits one stage downstream of the recipe: an "at packaging" process
# consuming the recipe plus one packaging system (PACK_AGB project, 2024). Its
# Category places it under this branch, which is how the stage is recognised —
# both when walking down from the recipe and when the selection already matched
# a stage process.
_PACKAGING_CATEGORY = "Agricultural\\Food\\Packaging"

# Packaging material exchanges are authored in grams, and the released engine
# (0.9.3) leaves gram-denominated links unresolved: the solved supply chain
# reaches the grouping box, billed in kg, but never the retail elements. So the
# bill is read from each level's own exchanges and scaled here — engine
# independent, at the price of an empty `ingredient_process_id` on the material
# rows, whose target the database itself does not resolve.
_UNIT_TO_KG = {"g": 0.001, "kg": 1.0, "ton": 1000.0}


def packaging_role(flow_name: str) -> str | None:
    """Role of one packaging exchange, or None for what is not kept.

    PACK_AGB names are regular: "…, End of Life {FR} | CFF : EoL …" for the end
    of life, "Transport, for all type of packaging, …" for the upstream
    transport, "… processing, …" and "… finishing, …" for the conversion of a
    material into a component, "<material>, …, Production …" for the material
    itself. Both dropped families are matched on their trailing comma, because
    "Cardboard, Corrugated, Production and shaping" is a material, not a step.
    """
    name = flow_name or ""
    if "End of Life" in name:
        return "packaging_eol"
    if name.startswith("Transport,") or "processing," in name or "finishing," in name:
        return None
    return "packaging_material"


def packaging_amount(amount: float, unit: str) -> tuple[float, str]:
    """Masses in kg like the ingredient rows; other units stay as authored."""
    factor = _UNIT_TO_KG.get((unit or "").lower())
    return (amount * factor, "kg") if factor else (amount, unit)


def packaging_rows(prefix: list, system_name: str, scale: float,
                   exchanges: list) -> list[list]:
    """Sheet rows for the materials of one packaging component — pure, tested.

    `scale` is how many of that component one functional unit of the product
    carries, so `amount * scale` is the material per functional unit.
    """
    rows = []
    for e in exchanges:
        role = packaging_role(e.flow_name)
        if role is None:
            continue
        amount, unit = packaging_amount(e.amount * scale, e.unit)
        rows.append(prefix + [
            e.flow_name,
            amount,
            unit,
            role,
            e.target_process_id or "",
            system_name,
        ])
    return rows


def packaging_stages(client: Client, product) -> list[str]:
    """Process-ids of the packaging stage(s) packing this product.

    Direct consumers only. A stage two hops away belongs to another product
    that uses this one as an ingredient — counting it would hang someone
    else's packaging on this recipe. A product that already is a stage is
    returned as is, so `--select Pizza --packaging` also works on the stage
    processes the same search matches. Several stages mean several packaging
    variants (chilled and frozen, glass and PET): all are kept, told apart by
    the `packaging_system` column.
    """
    if "at packaging" in (product.product_name or ""):
        return [product.process_id]
    consumers = client.get_consumers(
        product.process_id,
        classification_filters=[ClassificationFilter("Category", _PACKAGING_CATEGORY)],
        max_depth=1,
    ).consumers
    return [c.process_id for c in consumers]


def fetch_activity(client: Client, pid: str, cache: dict) -> ActivityDetail:
    """`get_activity`, remembered. Packaging systems are shared by every product
    of a subcategory, so the same handful of components is asked for again and
    again — once each is enough."""
    if pid not in cache:
        cache[pid] = client.get_activity(pid)
    return cache[pid]


def _is_packaging(act: ActivityDetail) -> bool:
    return _PACKAGING_CATEGORY in act.classifications.get("Category", "")


def component_rows(client: Client, cache: dict, prefix: list, system_name: str,
                   pid: str, scale: float) -> list[list]:
    """Rows for one packaging component and, recursively, its sub-components.

    An input whose target is itself in the packaging branch is a component to
    walk into — a system holds elements, an element could hold another. Every
    other input is a material of this level. Each step divides by the target's
    reference amount: a grouping box authored per 120 boxes contributes one
    hundred-and-twentieth of its materials per box used.
    """
    act = fetch_activity(client, pid, cache)
    rows, materials = [], []
    for e in act.technosphere_inputs:
        if e.is_reference:
            continue
        target = fetch_activity(client, e.target_process_id, cache) if e.target_process_id else None
        if target is not None and _is_packaging(target):
            rows += component_rows(client, cache, prefix, system_name, e.target_process_id,
                                   scale * e.amount / (target.product_amount or 1.0))
        else:
            materials.append(e)
    return rows + packaging_rows(prefix, system_name, scale, materials)


def stage_system(client: Client, cache: dict, stage_id: str) -> tuple | None:
    """The packaging system of a stage and how many of it one unit of the packed
    food carries — None for a stage that packs in nothing.

    The stage holds two inputs: the packaging system, and the food it packs.
    Dividing by the food amount is what turns "per unit packaged" into "per unit
    of food", rather than assuming the 1 kg for 1 kg Agribalyse happens to use
    ("No losses assumed at packaging").
    """
    stage = fetch_activity(client, stage_id, cache)
    system = system_amount = food_amount = None
    for e in stage.technosphere_inputs:
        if e.is_reference or not e.target_process_id:
            continue
        target = fetch_activity(client, e.target_process_id, cache)
        if _is_packaging(target):
            system, system_amount = target, e.amount
        else:
            food_amount = e.amount
    # A "No pack" stage (raw fruit sold loose) holds the food and nothing else.
    if system is None or not food_amount:
        return None
    return system, system_amount / food_amount / (system.product_amount or 1.0)


def product_packaging_rows(client: Client, cache: dict, prefix: list,
                           stage_ids: list[str]) -> list[list]:
    """Rows for every distinct packaging system this product is packed in.

    One recipe often stands in for a whole family of Ciqual products — 28
    breakfast cereals share one — and each of those products has its own
    packaging stage naming the very same system. Writing the bill once per stage
    would repeat it 28 times, so systems are deduplicated: a product genuinely
    offered in a glass jar and in a plastic pot still keeps both bills.
    """
    systems = {}
    for stage_id in stage_ids:
        found = stage_system(client, cache, stage_id)
        if found is not None:
            systems.setdefault((found[0].process_id, found[1]), found)
    rows = []
    for system, per_food in systems.values():
        rows += component_rows(client, cache, prefix, system.activity_name,
                               system.process_id, prefix[3] * per_food)
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
    ap.add_argument("--replace", action="store_true",
                    help="Delete the previously uploaded database and re-upload "
                         "from --agribalyse (use after the source file changed).")
    ap.add_argument("--select", action="append", default=[],
                    help="Product-name substring to extract (repeatable).")
    ap.add_argument("--classification", action="append", default=[], metavar="SYSTEM=VALUE",
                    help='Select by classification, e.g. "Category=Agricultural\\Food" (repeatable).')
    ap.add_argument("--all", action="store_true",
                    help="Extract the whole Agribalyse food catalogue "
                         f"({_FOOD_CATALOGUE[0]}={_FOOD_CATALOGUE[1]}). Pair with --limit 0 for everything.")
    ap.add_argument("--limit", type=int, default=3,
                    help="Max products per selector (0 = no cap; extracts all matches).")
    ap.add_argument("--ingredients-only", action="store_true",
                    help="Keep only edible ingredients (Category type = material, "
                         "recipe water included); drop cooking, [Dummy] operations, "
                         "waste treatment, electricity, heat and transport.")
    ap.add_argument("--packaging", action="store_true",
                    help="Also write the packaging bill of each product in the same "
                         "sheet: materials and their end of life, per functional unit, "
                         "with role packaging_material / packaging_eol and a "
                         f"{_PACKAGING_HEADER} column. Independent of --ingredients-only.")
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
        # The engine insists on an existing config file; an empty one means
        # "all defaults" — the database arrives via upload, not via TOML.
        config = Path(tmp) / "volca.toml"
        config.write_text("")

        print("2. Starting engine ...")
        # Pass the binary from download() explicitly so the exact downloaded
        # version is used, not whatever the default lookup finds first on PATH
        # or in the shared install root.
        # startup_timeout is generous: the very first engine run after a fresh
        # download indexes any bundled reference methods before it serves.
        srv = Server(config=str(config), port=args.port, binary=str(inst.binary))
        srv.start(idle_timeout=1800, wait_timeout=args.startup_timeout)
        try:
            client = Client(base_url=srv.base_url)

            # Uploads persist under the engine's install dir and are keyed by a
            # slug derived from --db-name; the human name is kept as
            # display_name, which is how a previous run's upload is recognised.
            existing = {d.display_name: d.name for d in client.list_databases()}
            slug = existing.get(args.db_name)
            if slug is not None and args.replace:
                print(f"3. Deleting previously uploaded {args.db_name} ...")
                client.delete_database(slug)
                slug = None
            if slug is None:
                print(f"3. Uploading {args.db_name} (once; later runs reuse it) ...")
                slug = client.upload_database(db_path, args.db_name)["slug"]
            client = client.use(slug)
            print(f"   Loading {slug} (first load parses the CSV; later loads hit the cache) ...")
            client.load_database(slug)

            keep = material_pids(client) if args.ingredients_only else None
            if keep is not None:
                print(f"   ingredients-only: {len(keep)} 'material' activities eligible")

            print("4. Extracting recipes ...")
            wb = Workbook()
            ws = wb.active
            ws.title = "recipes"
            # Without --packaging the sheet is exactly what it always was.
            pad = [""] if args.packaging else []
            ws.append(_HEADER + ([_PACKAGING_HEADER] if args.packaging else []))
            n_products = n_rows = n_packaging = 0
            cache: dict = {}  # packaging components, shared by whole subcategories
            for product in selected_products(client, selects, classifications, args.limit):
                rows = recipe_rows(client, product.process_id, keep)
                stages = packaging_stages(client, product) if args.packaging else []
                pack = product_packaging_rows(
                    client, cache, product_columns(product), stages)
                for row in rows:
                    ws.append(row + pad)
                for row in pack:
                    ws.append(row)
                n_products += 1
                n_rows += len(rows)
                n_packaging += len(pack)
                # Never silent: a product without packaging says which of the two
                # cases it is — no stage at all, or a stage packing in nothing.
                if not args.packaging:
                    note = ""
                elif pack:
                    note = f", {len(pack)} packaging in {len({r[10] for r in pack})} system(s)"
                elif stages:
                    note = ", no packaging (No pack)"
                else:
                    note = ", no packaging stage"
                print(f"   {product.activity_name[:64]:64}  {len(rows)} ingredients{note}")

            wb.save(args.out)
            packaging_note = f", {n_packaging} packaging rows" if args.packaging else ""
            print(f"\nDone: {n_products} products, {n_rows} ingredient rows"
                  f"{packaging_note} -> {args.out}")
        finally:
            srv.stop()


if __name__ == "__main__":
    main()
