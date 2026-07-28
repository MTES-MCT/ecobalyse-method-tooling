# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.8.0", "openpyxl"]
# ///
"""Extract the Agribalyse recipes — ingredients and packaging — to Excel.

Self-contained: downloads the VoLCA engine binary, starts it locally, uploads
an Agribalyse database (once — later runs reuse the uploaded copy), and writes,
for every recipe of the catalogue, one row per edible ingredient and one row
per packaging material::

    1 kg "Aioli sauce, ..."  ->  0.728 kg olive oil + 0.108 kg garlic + ...
                             ->  67.4 g PET + 33.7 g cardboard + ...

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
foods: pizza, ratatouille, aioli, ...), and for each of them:

- its **edible ingredients**, recipe water included. An input is kept when its
  producing activity is tagged `Category type = material`, its role is a food
  role, and its unit is a food unit — which drops cooking, [Dummy] operations,
  waste treatment, electricity, heat, transport, and the m3-billed utilities.
- its **packaging**, found one stage downstream: a recipe carries none, so the
  bill is read off the "at packaging" process that consumes it::

    Aioli sauce, ... | Chilled | Pack proxy | at packaging {FR}
    |-- 1 kg  Aioli sauce, ..., recipe, at plant {FR}          <- the recipe
    +-- 1 kg  Mayonnaise, 425g | Packaging System, N0, ...
              |-- N1 elements (squeeze bottle, cap, label)
              +-- N2/N3 grouping box and pallet

  Amounts are per functional unit of the product, like the ingredient rows: a
  425 g packaging system counts 2.35 times per kg. Materials and their end of
  life are kept; the conversion and upstream-transport processes that also live
  in a packaging element's inventory are dropped — a choice, not an oversight.

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

# Pinned, not "latest": engine and pyvolca version independently, and the wire
# revision they must share is not implied by either number. Engine 0.9.3 speaks
# wire 3 while every pyvolca released so far (≤0.8.2) decodes wire 2, so
# "latest" warns and may return rows that fail to decode. 0.9.1 is the engine
# this extraction was run against; move it when a pyvolca decoding wire 3 ships.
_ENGINE_VERSION = "0.9.1"

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
    # Which packaging system a packaging row comes from; empty on ingredient
    # rows. The material family needs no column of its own — it opens the
    # material name ("Plastic, LDPE, ...", "Cardboard, Flat, ...").
    "packaging_system",
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
        rows.append(prefix + [e.flow_name, e.amount, e.unit, role, pid, ""])
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
# Category places it under this branch, which is how the stage is recognised.
_PACKAGING_CATEGORY = "Agricultural\\Food\\Packaging"

# Packaging material exchanges are authored in grams, and the engine leaves
# gram-denominated links unresolved: the solved supply chain reaches the
# grouping box, billed in kg, but never the retail elements. So the bill is read
# from each level's own exchanges and scaled here — engine independent, at the
# price of an empty `ingredient_process_id` on the material rows, whose target
# the database itself does not resolve.
_UNIT_TO_KG = {"g": 0.001, "kg": 1.0, "ton": 1000.0}


def packaging_role(flow_name: str) -> str | None:
    """Role of one packaging exchange, or None for what is not kept.

    PACK_AGB names are regular: "…, End of Life {FR} | CFF : EoL …" for the end
    of life, "Transport, for all type of packaging, …" for the upstream
    transport, "… processing, …" and "… finishing, …" for the conversion of a
    material into a component, "<material>, …, Production …" for the material
    itself. Both dropped families are matched on their trailing comma, because
    "Cardboard, Corrugated, Production and shaping" is a material, not a step.

    A name is matched segment by segment, because an export may or may not
    carry the library prefix ("00_Pack_AGB_Lib |Transport, for all type …") and
    the end-of-life names carry a "| CFF : EoL …" suffix of their own. Matching
    the whole string would let a prefixed transport through, and it would come
    out a material — grams of freight silently counted as packaging mass.
    """
    parts = [p.strip() for p in (flow_name or "").split("|")]
    if any("End of Life" in p for p in parts):
        return "packaging_eol"
    if any(p.startswith("Transport,") or "processing," in p or "finishing," in p
           for p in parts):
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


def packaging_stages(client: Client, product: ActivityDetail) -> list[str]:
    """Process-ids of the packaging stage(s) packing this product.

    Direct consumers only. A stage two hops away belongs to another product
    that uses this one as an ingredient — counting it would hang someone
    else's packaging on this recipe. Several stages mean several packaging
    variants (chilled and frozen, glass and PET): all are kept, told apart by
    the `packaging_system` column.
    """
    consumers = client.get_consumers(
        product.process_id,
        classification_filters=[ClassificationFilter("Category", _PACKAGING_CATEGORY)],
        max_depth=1,
    ).consumers
    return [c.process_id for c in consumers]


def fetch_recipe(client: Client, pid: str) -> tuple[ActivityDetail, dict]:
    """Typed activity plus the corrected target ids of its inputs.

    One call serves both views: `ActivityDetail.from_json` is exactly what the
    typed `get_activity` does with this very payload, and the ids that view
    drops are read off the same raw exchanges. Only a recipe needs the ids —
    packaging materials are unresolved in the database anyway — so this is not
    cached, being asked once per recipe.
    """
    payload = client.call("get_activity", process_id=pid)
    return ActivityDetail.from_json(payload), ingredient_targets(payload)


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


def stage_system(client: Client, cache: dict, stage_id: str,
                 food_pid: str | None = None) -> tuple[ActivityDetail, float] | None:
    """The packaging system of a stage and how many of it one unit of the packed
    food carries — None for a stage that packs in nothing.

    The stage holds two inputs: the packaging system, and the food it packs.
    The food is the input whose target is `food_pid` — the very recipe we walked
    down from — and not merely "whatever is not packaging", so a third input on
    the stage cannot quietly become the divisor. Dividing by the food amount is
    what turns "per unit packaged" into "per unit of food", rather than assuming
    the 1 kg for 1 kg Agribalyse happens to use ("No losses assumed at
    packaging"). A stage carrying several candidates says so and keeps the
    first — silence there would be a wrong factor on every row of the product.
    """
    stage = fetch_activity(client, stage_id, cache)
    systems, foods = [], []
    for e in stage.technosphere_inputs:
        if e.is_reference or not e.target_process_id:
            continue
        target = fetch_activity(client, e.target_process_id, cache)
        bucket = systems if _is_packaging(target) else foods
        bucket.append((target, e.amount))
    named = [f for f in foods if f[0].process_id == food_pid]
    foods = named or foods
    # A "No pack" stage (raw fruit sold loose) holds the food and nothing else.
    if not systems or not foods or not foods[0][1]:
        return None
    if len(systems) > 1 or len(foods) > 1:
        print(f"   ! {stage.activity_name[:56]:56} {len(systems)} packaging and "
              f"{len(foods)} food input(s), keeping the first of each")
    system, system_amount = systems[0]
    return system, system_amount / foods[0][1] / (system.product_amount or 1.0)


def product_packaging_rows(client: Client, cache: dict, prefix: list,
                           food_pid: str, stage_ids: list[str]) -> list[list]:
    """Rows for every distinct packaging system this product is packed in.

    One recipe often stands in for a whole family of Ciqual products — 28
    breakfast cereals share one — and each of those products has its own
    packaging stage naming the very same system. Writing the bill once per stage
    would repeat it 28 times, so systems are deduplicated: a product genuinely
    offered in a glass jar and in a plastic pot still keeps both bills.
    """
    systems = {}
    for stage_id in stage_ids:
        found = stage_system(client, cache, stage_id, food_pid)
        if found is not None:
            systems.setdefault((found[0].process_id, found[1]), found)
    rows = []
    for system, per_food in systems.values():
        rows += component_rows(client, cache, prefix, system.activity_name,
                               system.process_id, prefix[3] * per_food)
    return rows


def selected_recipes(client: Client, limit: int) -> list:
    """The recipes to extract — all of them, or the first `limit` for a dry run.

    islice stops after `limit`, so a capped run never pulls the whole
    catalogue; `len(results)` reports the server-side total for the warning.
    Truncation is always said out loud, never silent.
    """
    system, value = _RECIPES
    res = client.search_activities(
        classification=system, classification_value=value, limit=limit or 200)
    kept = list(islice(res, limit)) if limit else list(res)
    total = len(res)
    if limit and total > len(kept):
        print(f"  {total} recipes, keeping the first {len(kept)} (--limit 0 for all)")
    else:
        print(f"  {len(kept)} recipes")
    return kept


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract the Agribalyse recipes to Excel: one row per edible "
                    "ingredient and one row per packaging material, per functional "
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
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="Extract only the first N recipes, for a dry run "
                         "(default: 0, every recipe).")
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

            print("4. Extracting recipes ...")
            wb = Workbook()
            ws = wb.active
            ws.title = "recipes"
            ws.append(_HEADER)
            n_products = n_rows = n_packaging = 0
            # Every activity fetched: the recipes themselves, their packaging
            # stages, and the components whole subcategories share.
            cache: dict = {}
            for product in selected_recipes(client, args.limit):
                act, targets = fetch_recipe(client, product.process_id)
                cache[act.process_id] = act  # the packaging walk meets it again
                prefix = product_columns(act)  # one source for both kinds of row
                rows = recipe_rows(act, targets, keep)
                stages = packaging_stages(client, act)
                pack = product_packaging_rows(client, cache, prefix, act.process_id, stages)
                for row in rows + pack:
                    ws.append(row)
                n_products += 1
                n_rows += len(rows)
                n_packaging += len(pack)
                # Never silent: a recipe without packaging says which of the two
                # cases it is — no stage at all, or a stage packing in nothing.
                if pack:
                    note = f", {len(pack)} packaging in {len({r[10] for r in pack})} system(s)"
                elif stages:
                    note = ", no packaging (No pack)"
                else:
                    note = ", no packaging stage"
                # Full name, no truncation: co-products of one activity differ
                # only at the tail ("..., 1 kg of cream (PGi) {FR} U").
                print(f"   {len(rows):3} ingredients{note}  {prefix[1]}")

            wb.save(args.out)
            print(f"\nDone: {n_products} recipes, {n_rows} ingredient rows, "
                  f"{n_packaging} packaging rows -> {args.out}")
        finally:
            srv.stop()


if __name__ == "__main__":
    main()
