# /// script
# requires-python = ">=3.10"
# dependencies = ["pyvolca>=0.9.0", "openpyxl"]
# ///
"""Extract the Agribalyse products to Excel: ingredients and packaging. See README.md."""

from __future__ import annotations

import argparse
import tempfile
from itertools import islice
from pathlib import Path

from openpyxl import Workbook

from volca import ActivityDetail, ClassificationFilter, Client, Server, download
from volca.agribalyse import classify_exchange

# Pinned: engine and pyvolca must agree on a wire revision neither number announces.
_ENGINE_VERSION = "0.9.3"

_DB_NAME = "agribalyse-3.2"
_PORT = 8123
_STARTUP_TIMEOUT = 600

# What a row is about: a Ciqual product (the "at consumer" process) or a recipe.
_SCOPES = {
    "recipes": ("Agricultural\\Food\\Recipes", "recipes"),
    "ciqual": ("Agricultural\\Food\\Preparation", "Ciqual products"),
}

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

# Same five product columns as _HEADER, so the two sheets join on product_process_id.
_SYSTEM_HEADER = _HEADER[:5] + [
    "packaging_system",
    "system_process_id",
    "systems_per_functional_unit",
    "system_reference_amount",
    "system_reference_unit",
]

# Food roles from classify_exchange; every other role is never an ingredient.
_INGREDIENT_ROLES = {"raw_material", "other", "water"}

# Food is weighed or counted: m3 is the natural gas and compressed air left over.
_NON_FOOD_UNITS = {"m3"}


def product_columns(activity: ActivityDetail) -> list:
    """The five product columns every row repeats — the product flow name, not the activity's."""
    amount = activity.product_amount if activity.product_amount is not None else 1.0
    unit = activity.product_unit or activity.unit or ""
    name = activity.product_name or activity.activity_name
    return [activity.process_id, name, activity.location, amount, unit]


def ingredient_targets(payload: dict) -> dict[str, str]:
    """Process-id of the product each input comes from: activityLinkId + flowId, the typed one naming an arbitrary co-product."""
    targets = {}
    for ed in (payload.get("activity") or {}).get("exchanges") or []:
        ex = ed.get("exchange") or {}
        activity_id, flow_id = ex.get("activityLinkId"), ex.get("flowId")
        if (ex.get("tag") == "TechnosphereExchange" and ex.get("role") == "Input"
                and activity_id and flow_id):
            targets[ed.get("flowName") or ""] = f"{activity_id}_{flow_id}"
    return targets


def recipe_rows(prefix: list, food: ActivityDetail, targets: dict[str, str],
                ingredient_pids: set[str]) -> list[list]:
    """One row per edible ingredient of `food`, rescaled to the functional unit `prefix` declares."""
    scale = prefix[3] / (food.product_amount or 1.0)
    rows = []
    for e in food.technosphere_inputs:
        if e.is_reference:
            continue
        pid = targets.get(e.flow_name, "")
        role = classify_exchange(e)
        if (pid not in ingredient_pids
                or role not in _INGREDIENT_ROLES
                or (e.unit or "").lower() in _NON_FOOD_UNITS):
            continue
        rows.append(prefix + [e.flow_name, e.amount * scale, e.unit, role, pid])
    return rows


def material_pids(client: Client) -> set[str]:
    """Process-ids of every `Category type = material` activity — Agribalyse's tag for edible matter."""
    res = client.search_activities(
        classification="Category type", classification_value="material", limit=200)
    return {a.process_id for a in res}


def fetch_recipe(client: Client, pid: str) -> tuple[ActivityDetail, dict]:
    """Typed activity plus the corrected target ids of its inputs, off one raw payload."""
    payload = client.call("get_activity", process_id=pid)
    return ActivityDetail.from_json(payload), ingredient_targets(payload)


def fetch_cached(client: Client, pid: str, cache: dict) -> tuple[ActivityDetail, dict]:
    """`fetch_recipe`, remembered — for the packaging side, whose processes are asked for again and again."""
    if pid not in cache:
        cache[pid] = fetch_recipe(client, pid)
    return cache[pid]


# An "at packaging" process consumes the food plus one packaging system (PACK_AGB, 2024).
_PACKAGING_CATEGORY = "Agricultural\\Food\\Packaging"

# Agribalyse writes an all-zero activity link for an input it connects to no producer.
_UNLINKED = "00000000-0000-0000-0000-000000000000"


def is_packaging_system(act) -> bool:
    """A packaging, not a packed food: PACK_AGB files systems under a dotted segment, stages under food families."""
    return any(p.startswith(".")
               for p in act.classifications.get("Category", "").split("\\"))


def stage_bill(stage: ActivityDetail, targets: dict[str, str], resolve) -> tuple | None:
    """`(food, [(system, systems per unit of food)])` of a packaging stage; None if it is not one, empty list if it packs nothing."""
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


# consumer <- retail <- distribution <- packaging: the bound the old lifecycle walk enforced.
_STAGE_DEPTH = 4


def stage_of(entries) -> str | None:
    """The product's own packaging stage: the shallowest non-system entry of its filtered supply chain."""
    stages = sorted((e for e in entries if not is_packaging_system(e)),
                    key=lambda e: e.depth)
    if len(stages) > 1 and stages[0].depth == stages[1].depth:
        print(f"   ! two packaging stages at depth {stages[0].depth}, "
              f"keeping {stages[0].activity_name[:52]}")
    return stages[0].process_id if stages else None


def family_bill(consumers, fetch, pid) -> list | None:
    """Union of the formats of every stage packing this food; None if no stage packs it, empty if packed in nothing."""
    packed, entries = False, []
    for c in consumers:
        if (not c.classifications.get("Category", "").startswith(_PACKAGING_CATEGORY)
                or is_packaging_system(c)):
            continue
        found = stage_bill(*fetch(c.process_id), lambda p: fetch(p)[0])
        if found is None or found[0] != pid:
            continue
        packed = True
        for system, per_food in found[1]:
            if not any(s.process_id == system.process_id and q == per_food
                       for s, q in entries):
                entries.append((system, per_food))
    return entries if packed else None


def system_rows(prefix: list, entries: list) -> list[list]:
    """The packaging rows of one product, the system as a single process, per functional unit."""
    return [prefix + [
        system.activity_name,
        system.process_id,
        prefix[3] * per_food,
        system.product_amount,
        system.product_unit or system.unit or "",
    ] for system, per_food in entries]


def selected_products(client: Client, limit: int, scope: str) -> list:
    """The products to extract; a `limit` truncates for a dry run and says so."""
    value, label = _SCOPES[scope]
    res = client.search_activities(classification="Category",
                                   classification_value=value, limit=limit or 200)
    kept = list(islice(res, limit)) if limit else list(res)
    total = len(res)
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
    ap.add_argument("--scope", choices=sorted(_SCOPES), default="ciqual",
                    help="What a row is about. 'ciqual' (default): one row per "
                         "product of the Ciqual table, 2 451 of them, each with its "
                         "own code and packaging format. 'recipes': one row per "
                         "composite food, 763 of them, a recipe standing in for a "
                         "whole family — the only way to see the 17 recipes no "
                         "Ciqual product reaches.")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="Extract only the first N products, for a dry run "
                         "(default: 0, no cap).")
    ap.add_argument("--out", default="agribalyse_recipes.xlsx",
                    help="Output Excel file (default: %(default)s).")
    args = ap.parse_args()

    db_path = Path(args.agribalyse).expanduser().resolve() if args.agribalyse else None
    have_file = db_path is not None and db_path.is_file()
    # Refuse now: --replace deletes the upload first, leaving the machine with none.
    if args.replace and not have_file:
        ap.error(f"--replace re-uploads the database, so --agribalyse must name a "
                 f"readable file{'' if db_path is None else f': {db_path}'}")
    # Otherwise not fatal: the file is only read when nothing is uploaded yet.
    if db_path is not None and not have_file:
        print(f"   ! --agribalyse: no such file: {db_path} — ignored, "
              f"only the uploaded {_DB_NAME} can be used")
        db_path = None

    print("1. Downloading VoLCA engine + reference data ...")
    inst = download(version=_ENGINE_VERSION)
    print(f"   binary  {inst.binary}")
    print(f"   data    {inst.data_dir}  (engine {inst.version}, data {inst.data_version})")

    with tempfile.TemporaryDirectory() as tmp:
        # The engine insists on a config file; an empty one means all defaults.
        config = Path(tmp) / "volca.toml"
        config.write_text("")

        print("2. Starting engine ...")
        # The binary from download(), not whatever the lookup finds first on PATH.
        srv = Server(config=str(config), port=_PORT, binary=str(inst.binary))
        srv.start(idle_timeout=1800, wait_timeout=_STARTUP_TIMEOUT)
        try:
            client = Client(base_url=srv.base_url)

            # Uploads persist under the engine's install dir, keyed by a slug.
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
                # Say it, or a changed CSV looks mysteriously without effect.
                print(f"3. Reusing the uploaded {_DB_NAME}; --agribalyse not re-read "
                      "(pass --replace after the source file changed).")
            client = client.use(slug)
            print(f"   Loading {slug} (first load parses the CSV; later loads hit the cache) ...")
            client.load_database(slug)

            keep = material_pids(client)
            print(f"   {len(keep)} 'material' activities eligible as ingredients")

            cache: dict = {}
            def fetch(pid):
                return fetch_cached(client, pid, cache)

            print("4. Extracting ...")
            wb = Workbook()
            ws = wb.active
            ws.title = "ingredients"
            ws.append(_HEADER)
            ws_sys = wb.create_sheet("packaging")
            ws_sys.append(_SYSTEM_HEADER)
            n_products = n_rows = n_systems = 0
            for product in selected_products(client, args.limit, args.scope):
                act, targets = fetch_recipe(client, product.process_id)
                prefix = product_columns(act)
                # Both sheets describe the food at the bottom of the product's chain.
                if args.scope == "ciqual":
                    chain = client.get_supply_chain(
                        act.process_id, max_depth=_STAGE_DEPTH, limit=200,
                        classification_filters=[
                            ClassificationFilter("Category", _PACKAGING_CATEGORY)])
                    spid = stage_of(chain.entries)
                    found = (stage_bill(*fetch(spid), lambda p: fetch(p)[0])
                             if spid else None)
                else:
                    got = client.get_consumers(act.process_id, max_depth=1, limit=200)
                    entries = family_bill(got.consumers, fetch, act.process_id)
                    found = (act.process_id, entries) if entries is not None else None
                food_pid, entries = found if found is not None else (act.process_id, None)
                food = (act, targets) if food_pid == act.process_id else fetch(food_pid)
                rows = recipe_rows(prefix, food[0], food[1], keep)
                systems = system_rows(prefix, entries or [])
                for row in rows:
                    ws.append(row)
                for row in systems:
                    ws_sys.append(row)
                n_products += 1
                n_rows += len(rows)
                n_systems += len(systems)
                # Never silent: no stage at all, or a stage packing in nothing.
                if systems:
                    note = f", {len(systems)} packaging system(s)"
                elif entries is not None:
                    note = ", no packaging (No pack)"
                else:
                    note = ", no packaging stage"
                # Full name: co-products differ only at the tail.
                print(f"   {len(rows):3} ingredients{note}  {prefix[1]}")

            wb.save(args.out)
            print(f"\nDone: {n_products} products, {n_systems} packaging systems, "
                  f"{n_rows} ingredient rows -> {args.out}")
        finally:
            srv.stop()


if __name__ == "__main__":
    main()
