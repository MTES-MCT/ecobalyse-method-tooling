"""CSV summary report for generated transformed-ingredient variants.

One row per generated variant (pre-merge granularity: a variant = one
from_existing block or one base activities entry), combining:
  - identity: base ingredient, French display name, alias, variant suffix
  - the metadata block written to lci_catalog (predicted or hardcoded)
  - prediction provenance (rule + confidence) in the value/Match/Conf
    style of ../metadata/export.py predictions.csv
  - upstream-replacement specifics: existing activity, replacement depth,
    upstream path, replaced/replacement activities

The `ecs` column is left empty on purpose: the environmental cost is
produced by the ecobalyse pipeline (`just export-all`) once the activities
are actually created; recomputing it here would not match (no complements).

Pure row-building; the only side effect is write_csv.
"""

import csv
from pathlib import Path

# Fields whose prediction provenance is reported. cropGroup, density and
# transportCooling values already appear as metadata columns; foodType and
# novaGroup only exist in the predictor output so they get a value column
# too. inediblePart and rawToCookedRatio are hardcoded by the generator
# (0 / 1.0), hence no provenance.
PROVENANCE_FIELDS = ["cropGroup", "density", "transportCooling", "foodType", "novaGroup"]

COLUMNS = [
    "Ingrédient de base",
    "Nom",
    "Alias",
    "Variante",
    "ecs",
    "scenario",
    "defaultOrigin",
    "cropGroup",
    "ingredientCategories",
    "inediblePart",
    "ingredientDensity",
    "rawToCookedRatio",
    "transportCooling",
    "cropGroupMatch",
    "cropGroupConf",
    "densityMatch",
    "densityConf",
    "transportCoolingMatch",
    "transportCoolingConf",
    "foodType",
    "foodTypeMatch",
    "foodTypeConf",
    "novaGroup",
    "novaGroupMatch",
    "novaGroupConf",
    "Type de création",
    "Activité existante",
    "Nouveau nom",
    "Profondeur",
    "Chemin amont",
    "Remplacé (from)",
    "Remplaçant (to)",
    "Base cible (source)",
]


def _format_match(m: dict | None) -> str:
    return m.get("rule", "") if m else ""


def _format_conf(m: dict | None) -> str:
    conf = (m or {}).get("confidence")
    return f"{conf:.3f}" if conf else ""


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
    pred: dict,
    fe: dict | None,
    existing_name: str,
    target_source: str,
) -> dict[str, str]:
    """Build one report row.

    kind: "from_existing" (substitution block) or "existing" (the consumer
    already uses this variant natively — no replacement, fe is None).
    meta is the single metadata block of the generated activities entry;
    pred is the raw predictor output (carries the *Match provenance dicts).
    """
    plan = fe["replacementPlan"] if fe else None
    upstream = [s["name"] for s in plan["upstreamPath"]] if plan else []
    replace = plan["replace"][0] if plan else None

    row = {
        "Ingrédient de base": base_ingredient,
        "Nom": display_name,
        "Alias": alias,
        "Variante": variant_short,
        "ecs": "",
        "scenario": _format_value(meta.get("scenario")),
        "defaultOrigin": _format_value(meta.get("defaultOrigin")),
        "cropGroup": _format_value(meta.get("cropGroup")),
        "ingredientCategories": ";".join(meta.get("ingredientCategories") or []),
        "inediblePart": _format_value(meta.get("inediblePart")),
        "ingredientDensity": _format_value(meta.get("ingredientDensity")),
        "rawToCookedRatio": _format_value(meta.get("rawToCookedRatio")),
        "transportCooling": _format_value(meta.get("transportCooling")),
        "foodType": _format_value(pred.get("foodType")),
        "novaGroup": _format_value(pred.get("novaGroup")),
        "Type de création": kind,
        "Activité existante": existing_name,
        "Nouveau nom": fe["newName"] if fe else "",
        "Profondeur": str(len(upstream)) if plan else "",
        "Chemin amont": " → ".join(upstream),
        "Remplacé (from)": replace["from"]["name"] if replace else "",
        "Remplaçant (to)": _format_to(replace["to"]) if replace else "",
        "Base cible (source)": target_source,
    }
    for key in PROVENANCE_FIELDS:
        m = pred.get(key + "Match")
        row[key + "Match"] = _format_match(m)
        row[key + "Conf"] = _format_conf(m)
    return row


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    ordered = sorted(
        rows,
        key=lambda r: (r["Ingrédient de base"], r["Activité existante"], r["Variante"]),
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(ordered)
