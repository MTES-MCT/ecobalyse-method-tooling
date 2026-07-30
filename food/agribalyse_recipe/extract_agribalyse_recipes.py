# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.8.2", "openpyxl"]
# ///
"""Extract the Agribalyse recipes — ingredients and packaging — to Excel.

Self-contained: downloads the VoLCA engine binary, starts it locally, uploads
an Agribalyse database (once — later runs reuse the uploaded copy), and writes,
for every recipe of the catalogue, one row per edible ingredient and one row
per packaging system::

    1 kg "Aioli sauce, ..."  ->  0.728 kg olive oil + 0.108 kg garlic + ...
                             ->  2.35 x "Mayonnaise, 425g | Packaging System, ..."

Run (uv resolves the two deps automatically)::

    uv run extract_agribalyse_recipes.py \
        --agribalyse "/path/to/AGB32_final.CSV.zip" --out recipes.xlsx

`download()` fetches the engine binary and the reference-data bundle, not any
LCA database: you supply Agribalyse yourself, the official SimaPro CSV export
being a free public download from ADEME (doc.agribalyse.fr). The upload
persists in the engine's install dir, so later runs skip it; pass --replace
after the source file changed.

What is written
---------------
Every process classified `Category=Agricultural\\Food\\Recipes` (the composite
foods: pizza, ratatouille, aioli, ...) — or every process of the database with
--all — and for each of them:

- its **edible ingredients**, recipe water included. An input is kept when its
  producing activity is tagged `Category type = material`, its role is a food
  role, and its unit is a food unit — which drops cooking, [Dummy] operations,
  waste treatment, electricity, heat, transport, and the m3-billed utilities.
- its **packaging**, found one stage downstream: a recipe carries none, so it
  is read off the "at packaging" process that consumes it::

    Aioli sauce, ... | Chilled | Pack proxy | at packaging {FR}
    |-- 1 kg  Aioli sauce, ..., recipe, at plant {FR}          <- the recipe
    +-- 1 kg  Mayonnaise, 425g | Packaging System, N0, ...     <- the system

  The system is written as one row, as a single process — the black box an
  impact engine resolves on its own — with how many of it one functional unit
  of the product carries: a 425 g system counts 2.35 times per kg.

Two sheets: `ingredients` holds the ingredient rows, `packaging` the system
rows; they join on `product_process_id`.

Docs: https://www.volca.run/docs/python/
"""

from __future__ import annotations

import argparse
import tempfile
from itertools import islice
from pathlib import Path

from openpyxl import Workbook

from volca import ActivityDetail, Client, Server, download
from volca.agribalyse import classify_exchange

# Pinned, not "latest": engine and pyvolca version independently, and the wire
# revision they must share is not implied by either number — pyvolca's PyPI
# page carries the compatibility table. pyvolca ≥ 0.8.2 decodes wire 2 and 3,
# which 0.9.3 speaks; this pair is what the extraction was validated against.
_ENGINE_VERSION = "0.9.3"

# Fixed because nothing here is a real choice: the display name keys the
# uploaded copy, the port only has to be free, and the timeout is generous
# because the very first engine run indexes its reference methods before it
# serves.
_DB_NAME = "agribalyse-3.2"
_PORT = 8123
_STARTUP_TIMEOUT = 600

# The composite/prepared foods (pizza, ratatouille, aioli, ...). Their
# lifecycle stages — at packaging, at distribution, at supermarket, at
# consumer — live under sibling branches and are not recipes: they carry
# logistics, cold and retail losses, not a bill of ingredients.
_RECIPES = ("Category", "Agricultural\\Food\\Recipes")

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

# The packaging sheet: one row per packaging system a product is packed in.
# Its five product columns are those of the ingredient sheet, so the two join
# on `product_process_id`.
_SYSTEM_HEADER = _HEADER[:5] + [
    "packaging_system",
    "system_process_id",
    # How many of that system one functional unit of the product carries: a
    # 425 g system counts 2.35 times per kg of food.
    "systems_per_functional_unit",
    # What the system is authored for: 0.425 kg of packed food, 1 kg, ...
    "system_reference_amount",
    "system_reference_unit",
]

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


def product_columns(activity: ActivityDetail) -> list:
    """The five product columns every row of a product repeats.

    Built once per product and handed to both the ingredient and the packaging
    rows — so the two can never disagree on the functional unit they are
    expressed against, nor on the amount the packaging bill is scaled by.
    """
    amount = activity.product_amount if activity.product_amount is not None else 1.0
    unit = activity.product_unit or activity.unit or ""
    # The product flow name, not the activity name: a multi-output activity
    # (cheese production -> cheese + cream + whey) yields one process per
    # co-product, all sharing the activity name — only the flow tells them apart.
    name = activity.product_name or activity.activity_name
    return [activity.process_id, name, activity.location, amount, unit]


def ingredient_targets(payload: dict) -> dict[str, str]:
    """Process-id of the product each input flow actually comes from, by name.

    The typed `target_process_id` names the target *activity*, and an activity
    with several co-products reports one of them arbitrarily: the biscuit's
    "Palm oil, crude, consumption mix" comes back as the process of "Palm
    kernel oil, crude" — a different oil. Over the whole catalogue that was one
    resolvable target in eight, and 628 rows even pointed at their own product,
    which would send a recursion in circles.

    The raw exchange carries the pair that does name the product: `flowId` for
    the product flow, `activityLinkId` for the activity producing it, and a
    process-id is exactly their join. The engine's own solve already follows
    this pair — only the reported id was ambiguous.

    Inputs only, because a self-consuming recipe (the nuoc mam sauce takes
    0.183 kg of itself) carries its own flow twice: once as the reference
    product, whose activity link is the all-zero uuid, and once as the input.
    Keyed by name, the two collide and the last one seen would win.
    """
    targets = {}
    for ed in (payload.get("activity") or {}).get("exchanges") or []:
        ex = ed.get("exchange") or {}
        activity_id, flow_id = ex.get("activityLinkId"), ex.get("flowId")
        if (ex.get("tag") == "TechnosphereExchange" and ex.get("role") == "Input"
                and activity_id and flow_id):
            targets[ed.get("flowName") or ""] = f"{activity_id}_{flow_id}"
    return targets


def recipe_rows(act: ActivityDetail, targets: dict[str, str],
                ingredient_pids: set[str]) -> list[list]:
    """One row per edible ingredient of `act` — pure, tested.

    Kept when the producing activity is tagged `Category type = material`, the
    role is a food role (recipe water included, biowaste treatment excluded
    though it is `material` too), and the unit is a food unit.
    """
    prefix = product_columns(act)
    rows = []
    for e in act.technosphere_inputs:
        if e.is_reference:  # the product itself, not an ingredient
            continue
        pid = targets.get(e.flow_name, "")
        role = classify_exchange(e)
        if (pid not in ingredient_pids
                or role not in _INGREDIENT_ROLES
                or (e.unit or "").lower() in _NON_FOOD_UNITS):
            continue
        rows.append(prefix + [e.flow_name, e.amount, e.unit, role, pid])
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


def fetch_recipe(client: Client, pid: str) -> tuple[ActivityDetail, dict]:
    """Typed activity plus the corrected target ids of its inputs.

    One call serves both views: `ActivityDetail.from_json` is exactly what the
    typed `get_activity` does with this very payload, and the ids that view
    drops are read off the same raw exchanges. Not cached: each recipe is
    asked for once.
    """
    payload = client.call("get_activity", process_id=pid)
    return ActivityDetail.from_json(payload), ingredient_targets(payload)


def fetch_activity(client: Client, pid: str, cache: dict) -> ActivityDetail:
    """`get_activity`, remembered. Packaging systems are shared by every product
    of a subcategory, so the same handful of them is asked for again and
    again — once each is enough."""
    if pid not in cache:
        cache[pid] = client.get_activity(pid)
    return cache[pid]


# Packaging sits one stage downstream of the food: an "at packaging" process
# consuming the food plus one packaging system (PACK_AGB project, 2024). The
# bill is read from those stages rather than by asking each product "who packs
# you?": one sweep of this branch sees every stage of the database, which is
# what makes the sheet complete.
_PACKAGING_CATEGORY = "Agricultural\\Food\\Packaging"

# An unlinked input: Agribalyse writes an all-zero activity link for an exchange
# it does not connect to a producer, and the join with the flow names no process.
_UNLINKED = "00000000-0000-0000-0000-000000000000"


def is_packaging_system(act: ActivityDetail) -> bool:
    """A packaging system or one of its elements, as opposed to a packed food.

    Both sit under `Agricultural\\Food\\Packaging`, so the branch alone cannot
    tell them apart: PACK_AGB files the systems and their elements under a
    dotted segment (".Packaging systems", ".Packaging II and III") and the
    stages under the food families. The distinction is load-bearing because a
    stage's food may itself be a packaging stage — Agribalyse packs the
    wholemeal sandwich by consuming the French-bread sandwich as a proxy — and
    reading "under Packaging" as "is a packaging" then leaves the stage with no
    food at all, hence with no row.
    """
    return any(p.startswith(".")
               for p in act.classifications.get("Category", "").split("\\"))


def stage_bill(stage: ActivityDetail, targets: dict[str, str], resolve) -> tuple | None:
    """The food a packaging stage packs, and what it is packed in — pure, tested.

    Returns `(food process-id, [(system, systems per unit of food)])`, or None
    when the process is not a stage at all: a packaging system or element, which
    consumes no food. The list is empty for a stage that packs in nothing ("No
    pack", raw fruit sold loose) — a list rather than an optional system so the
    caller can still tell "no stage" from "a stage that packs nothing".

    The food is an input classified under `Agricultural` and not a packaging;
    that positive test is what keeps a third input — electricity, waste — from
    quietly becoming the divisor. Dividing by the food amount is what turns "per
    unit packaged" into "per unit of food", rather than assuming the 1 kg for
    1 kg Agribalyse happens to use ("No losses assumed at packaging"). A stage
    carrying several candidates says so and keeps the first: silence there would
    be a wrong factor on every row of the product.

    `resolve` maps a process-id to its `ActivityDetail`; `targets` holds the
    corrected ids, because the food is often a co-product and the id the typed
    view reports would name an arbitrary sibling.
    """
    foods, systems = [], []
    for e in stage.technosphere_inputs:
        if e.is_reference:
            continue
        pid = targets.get(e.flow_name)
        if not pid or pid.startswith(_UNLINKED):
            continue
        target = resolve(pid)
        if target is None:
            continue
        if is_packaging_system(target):
            systems.append((target, e.amount))
        elif target.classifications.get("Category", "").startswith("Agricultural"):
            foods.append((target, e.amount))
    if not foods or not foods[0][1]:
        return None
    if len(systems) > 1 or len(foods) > 1:
        print(f"   ! {stage.activity_name[:56]:56} {len(systems)} packaging and "
              f"{len(foods)} food input(s), keeping the first of each")
    food, food_amount = foods[0]
    return food.process_id, [
        (system, system_amount / food_amount / (system.product_amount or 1.0))
        for system, system_amount in systems[:1]]


def packaging_by_food(client: Client, cache: dict) -> dict[str, list]:
    """Every packaged food of the database and the system(s) it is packed in.

    One sweep of the packaging branch, so a product only has to look itself up
    afterwards. Systems are deduplicated because one recipe stands in for a
    whole family of Ciqual products — 28 breakfast cereals share one — and each
    of those products has its own stage naming the very same system; a product
    genuinely offered in a glass jar and in a plastic pot keeps both.

    A food packed in nothing gets an empty list, which is how the run can still
    report "no packaging (No pack)" apart from "no packaging stage".
    """
    bill: dict[str, list] = {}
    for a in client.search_activities(classification="Category",
                                      classification_value=_PACKAGING_CATEGORY,
                                      limit=200):
        stage, targets = fetch_recipe(client, a.process_id)
        found = stage_bill(stage, targets,
                           lambda pid: fetch_activity(client, pid, cache))
        if found is None:
            continue
        food_pid, entries = found
        known = bill.setdefault(food_pid, [])
        for system, per_food in entries:
            if not any(s.process_id == system.process_id and q == per_food
                       for s, q in known):
                known.append((system, per_food))
    return bill


def system_rows(prefix: list, entries: list) -> list[list]:
    """The packaging rows of one product — pure, tested.

    The row is the packaging as a single process, enough to hand to the engine,
    which resolves the rest. Quantities are per functional unit of the product,
    so a grain billed by the ton carries a thousand times the systems a kilo
    does.
    """
    return [prefix + [
        system.activity_name,
        system.process_id,
        prefix[3] * per_food,
        system.product_amount,
        system.product_unit or system.unit or "",
    ] for system, per_food in entries]


def selected_products(client: Client, limit: int, everything: bool) -> list:
    """The products to extract — the recipes by default, every process of the
    database with --all, the first `limit` of either for a dry run.

    islice stops after `limit`, so a capped run never pulls the whole
    catalogue; `len(results)` reports the server-side total for the warning.
    Truncation is always said out loud, never silent.
    """
    system, value = _RECIPES
    kwargs = {} if everything else {
        "classification": system, "classification_value": value}
    res = client.search_activities(limit=limit or 200, **kwargs)
    kept = list(islice(res, limit)) if limit else list(res)
    total = len(res)
    label = "processes" if everything else "recipes"
    if limit and total > len(kept):
        print(f"  {total} {label}, keeping the first {len(kept)} (--limit 0 for all)")
    else:
        print(f"  {len(kept)} {label}")
    return kept


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract the Agribalyse recipes to Excel: one row per edible "
                    "ingredient and one row per packaging system, per functional "
                    "unit of the product. Downloads and runs the VoLCA engine "
                    "locally. Details: README.md.")
    ap.add_argument("--agribalyse", default=None, metavar="FILE",
                    help="The Agribalyse export to upload: the official ADEME SimaPro "
                         "CSV (e.g. AGB32_final.CSV.zip, free from doc.agribalyse.fr); "
                         "EcoSpold, ILCD or a Brightway/Excel .xlsx also work (format "
                         "auto-detected). Only read on the first run, which uploads it "
                         "into the engine — later runs reuse the uploaded copy and "
                         "ignore this flag unless --replace is given.")
    ap.add_argument("--replace", action="store_true",
                    help="Delete the previously uploaded database and re-upload from "
                         "--agribalyse (use after the source file changed).")
    ap.add_argument("--all", action="store_true",
                    help="Extract every process of the database, not only the "
                         "recipes (Category=Agricultural\\Food\\Recipes). The "
                         "ingredient filters still apply to each process's inputs; "
                         "packaging only exists for the food products a packaging "
                         "stage consumes.")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="Extract only the first N products, for a dry run "
                         "(default: 0, no cap).")
    ap.add_argument("--out", default="agribalyse_recipes.xlsx",
                    help="Output Excel file (default: %(default)s).")
    args = ap.parse_args()

    db_path = Path(args.agribalyse).expanduser().resolve() if args.agribalyse else None
    have_file = db_path is not None and db_path.is_file()
    # --replace deletes the upload before re-uploading, so refuse now rather
    # than after: an unreadable file would leave the machine with no database.
    if args.replace and not have_file:
        ap.error(f"--replace re-uploads the database, so --agribalyse must name a "
                 f"readable file{'' if db_path is None else f': {db_path}'}")
    # A missing file is otherwise not fatal: it is only read when nothing is
    # uploaded yet, and a command line kept from the first run must keep working
    # once the drive holding the export is gone.
    if db_path is not None and not have_file:
        print(f"   ! --agribalyse: no such file: {db_path} — ignored, "
              f"only the uploaded {_DB_NAME} can be used")
        db_path = None

    print("1. Downloading VoLCA engine + reference data ...")
    inst = download(version=_ENGINE_VERSION)
    print(f"   binary  {inst.binary}")
    print(f"   data    {inst.data_dir}  (engine {inst.version}, data {inst.data_version})")

    with tempfile.TemporaryDirectory() as tmp:
        # The engine insists on an existing config file; an empty one means
        # "all defaults" — the database arrives via upload, not via TOML.
        config = Path(tmp) / "volca.toml"
        config.write_text("")

        print("2. Starting engine ...")
        # Pass the binary from download() explicitly so the exact downloaded
        # version is used, not whatever the default lookup finds first on PATH.
        srv = Server(config=str(config), port=_PORT, binary=str(inst.binary))
        srv.start(idle_timeout=1800, wait_timeout=_STARTUP_TIMEOUT)
        try:
            client = Client(base_url=srv.base_url)

            # Uploads persist under the engine's install dir and are keyed by a
            # slug derived from the display name, which is how a previous run's
            # upload is recognised.
            existing = {d.display_name: d.name for d in client.list_databases()}
            slug = existing.get(_DB_NAME)
            if slug is not None and args.replace:
                print(f"3. Deleting previously uploaded {_DB_NAME} ...")
                client.delete_database(slug)
                slug = None
            if slug is None:
                if db_path is None:
                    raise SystemExit(
                        f"Database {_DB_NAME!r} is not uploaded yet on this "
                        "machine — pass --agribalyse FILE (see --help).")
                print(f"3. Uploading {_DB_NAME} (once; later runs reuse it) ...")
                slug = client.upload_database(db_path, _DB_NAME)["slug"]
            elif db_path is not None:
                # The file was passed but is NOT re-read — say so, or a changed
                # CSV looks mysteriously without effect.
                print(f"3. Reusing the uploaded {_DB_NAME}; --agribalyse not re-read "
                      "(pass --replace after the source file changed).")
            client = client.use(slug)
            print(f"   Loading {slug} (first load parses the CSV; later loads hit the cache) ...")
            client.load_database(slug)

            keep = material_pids(client)
            print(f"   {len(keep)} 'material' activities eligible as ingredients")

            # Every activity fetched: the packaging stages and the systems whole
            # subcategories share, then the products themselves.
            cache: dict = {}
            print("4. Reading the packaging stages ...")
            packaging = packaging_by_food(client, cache)
            print(f"   {sum(len(v) for v in packaging.values())} (product, packaging "
                  f"system) pairs over {len(packaging)} packed products")

            print("5. Extracting ...")
            wb = Workbook()
            ws = wb.active
            ws.title = "ingredients"
            ws.append(_HEADER)
            ws_sys = wb.create_sheet("packaging")
            ws_sys.append(_SYSTEM_HEADER)
            n_products = n_rows = n_systems = 0
            for product in selected_products(client, args.limit, args.all):
                act, targets = fetch_recipe(client, product.process_id)
                prefix = product_columns(act)  # one source for both kinds of row
                rows = recipe_rows(act, targets, keep)
                systems = system_rows(prefix, packaging.get(act.process_id, []))
                for row in rows:
                    ws.append(row)
                for row in systems:
                    ws_sys.append(row)
                n_products += 1
                n_rows += len(rows)
                n_systems += len(systems)
                # Never silent: a product without packaging says which of the two
                # cases it is — no stage at all, or a stage packing in nothing.
                if systems:
                    note = f", {len(systems)} packaging system(s)"
                elif act.process_id in packaging:
                    note = ", no packaging (No pack)"
                else:
                    note = ", no packaging stage"
                # Full name, no truncation: co-products of one activity differ
                # only at the tail ("..., 1 kg of cream (PGi) {FR} U").
                print(f"   {len(rows):3} ingredients{note}  {prefix[1]}")

            wb.save(args.out)
            print(f"\nDone: {n_products} products, {n_systems} packaging systems, "
                  f"{n_rows} ingredient rows -> {args.out}")
        finally:
            srv.stop()


if __name__ == "__main__":
    main()
