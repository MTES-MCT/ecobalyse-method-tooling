"""CSV summary report for generated transformed-ingredient variants.

One row per generated variant (pre-merge granularity: a variant = one
from_existing block or one base activities entry), combining:
  - identity: base ingredient, existing activity, variant suffix, French
    display name, alias
  - the metadata block written to lci_catalog (predicted or hardcoded)
  - upstream-replacement specifics: replacement depth, upstream path,
    replaced/replacement activities

The `ecs` column is left empty by the generator: the environmental cost is
produced by the ecobalyse pipeline (`just import-all && just export-all`)
once the activities are actually created; recomputing it here would not
match (no complements).

Pure row-building; the only side effect is write_csv.
"""

import csv
from pathlib import Path

COLUMNS = [
    "Ingrédient de base",
    "Activité existante",
    "Variante",
    "Nom",
    "Alias",
    "ecs",
    "scenario",
    "defaultOrigin",
    "cropGroup",
    "ingredientCategories",
    "inediblePart",
    "ingredientDensity",
    "rawToCookedRatio",
    "transportCooling",
    "Type de création",
    "Nouveau nom",
    "Profondeur",
    "Chemin amont",
    "Remplacé (from)",
    "Remplaçant (to)",
    "Base cible (source)",
]


def _format_value(v) -> str:
    return "" if v is None else str(v)


def _format_to(to: dict) -> str:
    # database is omitted from the block when it is the Agribalyse default
    qualifiers = ", ".join(
        q for q in (to.get("database", "Agribalyse 3.2"), to.get("location")) if q
    )
    return f"{to['name']} [{qualifiers}]"


def build_row(
    *,
    kind: str,
    base_ingredient: str,
    variant_short: str,
    display_name: str,
    alias: str,
    meta: dict,
    fe: dict | None,
    existing_name: str,
    target_source: str,
) -> dict[str, str]:
    """Build one report row.

    kind: "from_existing" (substitution block) or "existing" (the consumer
    already uses this variant natively — no replacement, fe is None).
    meta is the single metadata block of the generated activities entry.
    """
    plan = fe["replacementPlan"] if fe else None
    upstream = [s["name"] for s in plan["upstreamPath"]] if plan else []
    replace = plan["replace"][0] if plan else None

    return {
        "Ingrédient de base": base_ingredient,
        "Activité existante": existing_name,
        "Variante": variant_short,
        "Nom": display_name,
        "Alias": alias,
        "ecs": "",
        "scenario": _format_value(meta.get("scenario")),
        "defaultOrigin": _format_value(meta.get("defaultOrigin")),
        "cropGroup": _format_value(meta.get("cropGroup")),
        "ingredientCategories": ";".join(meta.get("ingredientCategories") or []),
        "inediblePart": _format_value(meta.get("inediblePart")),
        "ingredientDensity": _format_value(meta.get("ingredientDensity")),
        "rawToCookedRatio": _format_value(meta.get("rawToCookedRatio")),
        "transportCooling": _format_value(meta.get("transportCooling")),
        "Type de création": kind,
        "Nouveau nom": fe["newName"] if fe else "",
        "Profondeur": str(len(upstream)) if plan else "",
        "Chemin amont": " → ".join(upstream),
        "Remplacé (from)": replace["from"]["name"] if replace else "",
        "Remplaçant (to)": _format_to(replace["to"]) if replace else "",
        "Base cible (source)": target_source,
    }


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    ordered = sorted(
        rows,
        key=lambda r: (r["Ingrédient de base"], r["Activité existante"], r["Variante"]),
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(ordered)


def ecs_join_key(row: dict[str, str]) -> str:
    """activityName under which the ecobalyse pipeline exports this row's process.

    from_existing rows are created under their newName (variant bracket and
    {{alias}} marker included, kept verbatim by the export); existing rows
    reference the existing activity directly.
    """
    return row["Nouveau nom"] or row["Activité existante"]


def fill_ecs(
    rows: list[dict[str, str]], ecs_by_activity_name: dict[str, float]
) -> tuple[list[dict[str, str]], list[str]]:
    """Fill the ecs column from pipeline impacts; also return unmatched keys."""
    filled, missing = [], []
    for row in rows:
        ecs = ecs_by_activity_name.get(ecs_join_key(row))
        if ecs is None:
            missing.append(ecs_join_key(row))
        filled.append(row | {"ecs": "" if ecs is None else f"{ecs:.6g}"})
    return filled, missing


def _main() -> None:
    """Backfill the ecs column of transformed_ingredients.csv from the
    ecobalyse pipeline output (run after `just import-all && just export-all`)."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=_main.__doc__)
    parser.add_argument(
        "ecobalyse",
        help="Path to the ecobalyse repository "
        "(reads data/public/data/processes_impacts.json)",
    )
    parser.add_argument("--csv", default="transformed_ingredients.csv")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    impacts_path = (
        Path(args.ecobalyse) / "data" / "public" / "data" / "processes_impacts.json"
    )
    with impacts_path.open(encoding="utf-8") as f:
        ecs_by_name = {
            p["activityName"]: p["impacts"]["ecs"]
            for p in json.load(f)
            if "ecs" in (p.get("impacts") or {})
        }

    filled, missing = fill_ecs(rows, ecs_by_name)
    write_csv(filled, csv_path)
    print(f"{len(filled) - len(missing)}/{len(filled)} ecs filled → {csv_path}")
    for key in missing:
        print(f"  [WARN] no process found for: {key}", file=sys.stderr)


if __name__ == "__main__":
    _main()
