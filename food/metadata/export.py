#!/usr/bin/env python3
"""
Export predicted ingredients to CSV and activities.json format.

Usage:
    python export.py metadata --variant FR     # Export FR variant
    python export.py metadata --variant ORG    # Export organic variant
    python export.py final_data                # Generate final CSV with impacts
    python export.py metadata --variant FR --add-old-suffix     # Add (2025) suffix to existing
    python export.py remove-old                                # Remove suffixed entries entirely

Variants: FR, ORG, UE, OI, NUE

Outputs:
    - generated/predictions.csv: CSV with all predictions and confidence scores
    - generated/new_activities.json: Activities format for Ecobalyse
"""

import argparse
import csv
import functools
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal
from urllib.request import urlopen

import inflect
import pandas as pd
from rich.progress import track

# bw2data and dotenv are deliberately imported lazily inside _bw_ready() to
# keep `import export` side-effect-free (bw2data prints to stderr on import).
from predict import Predictor

# Catalog read/write/merge primitives live in a side-effect-free module so
# they can be imported by tools that don't want bw2data/pandas/dotenv.
from lci_catalog import (
    ECOBALYSE_NAMESPACE,
    OLD_ALIAS_SUFFIX,
    OLD_DISPLAY_SUFFIX,
    extract_geo,
    load_lci_catalog,
    merge_activities,
    write_lci_catalog,
)

_inflect_engine = inflect.engine()

PROCESSING_QUALIFIERS = {
    "dried", "fresh", "raw", "frozen", "smoked", "canned", "cooked", "uncooked",
    "roasted", "ground", "blanched", "peeled", "shelled", "dehusked", "pickled",
    "fermented", "salted", "dehydrated", "pasteurized", "refined", "whole",
    "concentrated",
}

PLURAL_EXCEPTIONS = {"oats", "french-fries"}


class Variant(Enum):
    FR = "FR"
    BIO = "BIO"
    UE = "UE"
    OI = "OI"
    NUE = "NUE"


Scenario = Literal["reference", "organic", "import"]
Origin = Literal["France", "EuropeAndMaghreb", "OutOfEuropeAndMaghreb"]


@dataclass(frozen=True)
class VariantConfig:
    display_suffix: str
    alias_suffix: str
    scenario: Scenario
    origin: Origin
    is_organic: bool = False


VARIANTS: dict[Variant, VariantConfig] = {
    Variant.FR:  VariantConfig(" FR",              "-fr",       "reference", "France",                False),
    Variant.BIO: VariantConfig(" Bio",             "-organic",  "organic",   "France",                True),
    Variant.UE:  VariantConfig(" UE",              "-eu",       "import",    "EuropeAndMaghreb",      False),
    Variant.OI:  VariantConfig(" Origine Inconnue", "-default", "import",    "OutOfEuropeAndMaghreb", False),
    Variant.NUE: VariantConfig(" HORS UE",         "-non-eu",   "import",    "OutOfEuropeAndMaghreb", False),
}

@dataclass(frozen=True)
class RowGeo:
    production_fr: str = ""
    antilles: bool = False


@dataclass(frozen=True)
class PredictionRow:
    name: str
    french_name: str
    activity_name: str
    source: str
    unit: str
    predictions: dict
    variant: Variant
    geo: RowGeo
    location: str = ""
    visible: bool = True


def resolve_variant(variant: Variant, geo: RowGeo) -> VariantConfig:
    if variant is Variant.FR and geo.production_fr == "DOM":
        return VariantConfig(" FR Outre-Mer", "-fr-overseas",
                             "reference", "France", False)
    if variant is Variant.UE and geo.antilles:
        return VariantConfig(" UE Antilles", "-eu-antilles",
                             "import", "EuropeAndMaghreb", False)
    return VARIANTS[variant]


KNOWN_ALIAS_SUFFIXES: frozenset[str] = frozenset(
    {c.alias_suffix for c in VARIANTS.values()}
    | {"-fr-overseas", "-eu-antilles"}
)


@functools.cache
def _bw_ready():
    from dotenv import load_dotenv
    load_dotenv()
    import bw2data
    bw2data.projects.set_current("ecobalyse")
    return bw2data


REFERENCE_DIR = Path(__file__).parent / "reference"


@functools.cache
def _animal_entries() -> tuple[dict, ...]:
    with open(REFERENCE_DIR / "animal.csv", encoding="utf-8") as f:
        return tuple({
            "pattern": re.compile(r"\b" + re.escape(row["name"]) + r"\b", re.IGNORECASE),
            "animalGroup1": row["animalGroup1"],
            "animalGroup2": row["animalGroup2"],
            "animalProduct": row["animalProduct"],
        } for row in csv.DictReader(f))


def detect_animal_fields(name: str, activity_name: str) -> dict:
    text = f"{name} {activity_name}"
    for entry in _animal_entries():
        if entry["pattern"].search(text):
            return {k: entry[k] for k in ("animalGroup1", "animalGroup2", "animalProduct")}
    return {}


def generate_alias(name: str) -> str:
    """Slugify, singularize base words, move processing qualifiers to the end."""
    alias = re.sub(r"-+", "-",
                   re.sub(r"[^a-z0-9-]", "",
                          re.sub(r"[\s_]+", "-", name.lower()))).strip("-")
    if alias in PLURAL_EXCEPTIONS:
        return alias
    words = alias.split("-")
    base = [w for w in words if w not in PROCESSING_QUALIFIERS]
    qualifiers = [w for w in words if w in PROCESSING_QUALIFIERS]
    singularized = [_inflect_engine.singular_noun(w) or w for w in base]
    return "-".join(singularized + qualifiers)


def simplify_for_alias(name: str) -> str:
    """Drop clarifying alternative-lists from an ingredient name before alias generation.

    Two patterns are dropped:
    - parenthesized clarifications: ``Bell pepper (green, yellow, or red)`` → ``Bell pepper``
    - trailing alternative tails containing `` or ``:
      ``Sea salt, celery salt, or fleur de sel`` → ``Sea salt``
    - if a parenthetical was already stripped, the leading comma-tail is also
      dropped (it's redundant with the dropped paren):
      ``Raw rice, all varieties (white, wild, ...)`` → ``Raw rice``

    Real qualifiers (no parens, no `` or `` in the tail) are kept:
    ``Almonds, in shell``, ``Squash, raw, with skin`` are returned as-is.
    """
    stripped, n_strip = re.subn(r"\s*\([^)]*\)", "", name)
    stripped = stripped.strip()
    if "," in stripped:
        head, tail = stripped.split(",", 1)
        if " or " in tail or n_strip > 0:
            return head.strip()
    return stripped


# LCA-process boilerplate phrases that don't help identify the activity.
# Order matters in alternation: longer phrases first.
_LCA_NOISE_RE = re.compile(
    r",\s*(?:at\s+farm\s+gate|at\s+farm|national\s+average|conventional)",
    re.IGNORECASE,
)

# Year-version markers (e.g. `organic 2025`) used in BIO LCI names. Strip the
# year to avoid colliding with the legacy `-2025` alias suffix that already
# tags pre-existing ingredients in activities.json.
_YEAR_MARKER_RE = re.compile(r"\s+20\d{2}\b")


def generate_activity_alias(activity_name: str) -> str:
    """Generate alias from a Brightway/Agribalyse activityName.

    Keeps discriminating qualifiers (`, in shell`, `, non-basmati`, `, dried`,
    …) but drops process-LCA boilerplate (`, at farm`, `{geo}`, `| production
    …`, ` U`, `- Adapted from …`, ` 20XX` year markers).
    """
    head = re.split(r"[{(|]", activity_name, maxsplit=1)[0]
    head = _LCA_NOISE_RE.sub("", head)
    head = _YEAR_MARKER_RE.sub("", head)
    head = re.sub(r"\s+U\s*$", "", head).strip().strip(",").strip()
    return generate_alias(head)


_MARKER_RE = re.compile(r"\s*\{\{[^}]*\}\}\s*")


def clean_activity_name(activity_name: str) -> str:
    """Strip `{{archive-alias-…}}` annotation markers used in the source CSV.

    These are human-only hints (no code consumes them) but they leak into the
    predictor's text matching and trigger false positives (e.g. `{{bacon-…}}`
    matching the `\\bbacon\\b` cured-meat rule on a `Live pig` activity).
    """
    return _MARKER_RE.sub(" ", activity_name).strip()


def _format_match(m: dict | None) -> str:
    return m.get("rule", "") if m else ""


def _format_conf(m: dict | None) -> str:
    conf = (m or {}).get("confidence")
    return f"{conf:.3f}" if conf else ""


def get_db_unit(activity_name, location=""):
    """Return (unit, db_name, location_or_empty); location returned only when used to disambiguate."""
    bw2data = _bw_ready()
    dbs = ("Agribalyse 3.2", "Ecoinvent 3.9.1", "Ecoinvent 3.11", "WFLDB", "Ecobalyse", "Ginko 2025")
    for db in dbs:
        activities = [a for a in bw2data.Database(db) if a["name"] == activity_name]
        if len(activities) == 1:
            return activities[0]["unit"], db, ""
        if len(activities) > 1 and location:
            filtered = [a for a in activities if a.get("location") == location]
            if len(filtered) >= 1:
                return filtered[0]["unit"], db, location
    raise Exception(f"Not found in {str(dbs)}: {activity_name}")


def fix_unit(unit):
    return {"kilogram": "kg", "unit": "item", "litre": "L"}[unit]


# =============================================================================
# PREDICTION
# =============================================================================


def predict_all(predictor: Predictor, input_df: pd.DataFrame, variant: Variant) -> list[PredictionRow]:
    """Predict metadata for all ingredients in the DataFrame."""
    results: list[PredictionRow] = []

    for _, row in track(
        input_df.iterrows(), total=len(input_df), description="Predicting..."
    ):
        name = str(row["item"]).strip()
        french_name = (
            str(row["item trad"]).strip()
            if pd.notna(row.get("item trad"))
            else ""
        )
        activity_name = (
            clean_activity_name(str(row["icv final"])) if pd.notna(row["icv final"]) else ""
        )
        if not name or not activity_name:
            continue

        csv_location = str(row.get("location", "")).strip() if pd.notna(row.get("location")) else ""
        unit, source, location = get_db_unit(activity_name, csv_location)

        geo = RowGeo(
            production_fr=str(row.get("production fr", "")).strip(),
            antilles=str(row.get("antilles", "")).strip().upper() == "TRUE",
        )

        predictions = predictor.predict({"name": name, "activityName": activity_name})
        visible = str(row.get("visible", "TRUE")).strip().upper() == "TRUE"

        results.append(PredictionRow(
            name=name,
            french_name=french_name,
            activity_name=activity_name,
            source=source,
            unit=fix_unit(unit),
            predictions=predictions,
            variant=variant,
            geo=geo,
            location=location,
            visible=visible,
        ))

    return results


@dataclass(frozen=True)
class PredField:
    key: str
    has_conf: bool = False
    fmt: str | None = None
    nullable: bool = False  # pred.get(k) or "" vs pred.get(k, "")


PRED_FIELDS: list[PredField] = [
    PredField("foodType",         has_conf=True),
    PredField("novaGroup",        has_conf=True),
    PredField("processingState",  has_conf=True),
    PredField("packaging",                       nullable=True),
    PredField("transportCooling"),
    PredField("cropGroup",        has_conf=True, nullable=True),
    PredField("density",          has_conf=True, fmt=".3f"),
    PredField("inediblePart",     has_conf=True, fmt=".2f"),
    PredField("rawToCookedRatio", has_conf=True, fmt=".3f"),
]


def csv_header() -> list[str]:
    cols = ["name", "categories"]
    for f in PRED_FIELDS:
        cols.append(f.key)
        cols.append(f.key + "Match")
        if f.has_conf:
            cols.append(f.key + "Conf")
    return cols


def _render_value(field: PredField, pred: dict) -> str:
    if field.fmt is not None:
        return format(pred.get(field.key, 0), field.fmt)
    if field.nullable:
        return pred.get(field.key) or ""
    return pred.get(field.key, "")


def csv_row(row: PredictionRow) -> dict[str, str]:
    pred = row.predictions
    categories = pred.get("categories", [])
    out: dict[str, str] = {
        "name": row.name,
        "categories": ",".join(categories) if categories else "",
    }
    for f in PRED_FIELDS:
        match_info = pred.get(f.key + "Match")
        out[f.key] = _render_value(f, pred)
        out[f.key + "Match"] = _format_match(match_info)
        if f.has_conf:
            out[f.key + "Conf"] = _format_conf(match_info)
    return out


def write_csv(results: list[PredictionRow], output_path: str):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=csv_header())
        writer.writeheader()
        for r in results:
            writer.writerow(csv_row(r))
    print(f"CSV written to {output_path}")


def build_activity_entry(row: PredictionRow, *, alias_override: str | None = None) -> dict:
    """Activity alias from LCI activityName (shared across variants); ingredient alias
    from row.name + variant suffix. They differ when ingredients proxy onto a shared activity."""
    cfg = resolve_variant(row.variant, row.geo)
    predictions = row.predictions

    activity_alias = alias_override or generate_activity_alias(row.activity_name)
    ingredient_alias = generate_alias(simplify_for_alias(row.name)) + cfg.alias_suffix
    ingredient_display_name = (row.french_name or row.name) + cfg.display_suffix

    base = re.split(r"[{(|]", row.activity_name, maxsplit=1)[0].strip().rstrip(",").strip()
    geo_code = extract_geo(row.activity_name)
    activity_display_name = f"{base} ({geo_code.upper()})" if geo_code else base

    # UUIDs keyed by alias/displayName so identity stays stable across variants/exports.
    activity_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"activity:{activity_alias}"))
    ingredient_id = str(uuid.uuid5(ECOBALYSE_NAMESPACE, f"ingredient:{ingredient_display_name}"))

    ingredient = {
        "alias": ingredient_alias,
        "defaultOrigin": cfg.origin,
        "displayName": ingredient_display_name,
        "id": ingredient_id,
        "inediblePart": predictions.get("inediblePart", 0),
        "inediblePartMatch": predictions.get("inediblePartMatch"),
        "ingredientCategories": predictions.get("categories", ["misc"])
            + (["organic"] if cfg.is_organic else []),
        "ingredientDensity": predictions.get("density", 1.0),
        "ingredientDensityMatch": predictions.get("densityMatch"),
        "rawToCookedRatio": predictions.get("rawToCookedRatio", 1.0),
        "rawToCookedRatioMatch": predictions.get("rawToCookedRatioMatch"),
        "scenario": cfg.scenario,
        "transportCooling": predictions.get("transportCooling", "none"),
        "transportCoolingMatch": predictions.get("transportCoolingMatch"),
        "visible": row.visible,
    }
    if predictions.get("cropGroup"):
        ingredient["cropGroup"] = predictions["cropGroup"]
        ingredient["cropGroupMatch"] = predictions.get("cropGroupMatch")
    ingredient |= detect_animal_fields(row.name, row.activity_name)

    entry = {
        "activityName": row.activity_name,
        "alias": activity_alias,
        "categories": ["ingredient"],
        "displayName": activity_display_name,
        "id": activity_id,
        "metadata": [{**ingredient, "scopes": ["food", "food2"]}],
        "scopes": ["food", "food2"],
        "source": row.source,
        "unit": row.unit,
    }
    if row.location:
        entry["location"] = row.location
    return entry


def assemble_activities(rows: list[PredictionRow]) -> list[dict]:
    """Group rows by activity alias, geo-disambiguating where two activities collapse onto one alias."""
    base_aliases = [generate_activity_alias(r.activity_name) for r in rows]

    by_base: dict[str, set[str]] = {}
    for r, a in zip(rows, base_aliases):
        by_base.setdefault(a, set()).add(r.activity_name)
    needs_geo = {a for a, names in by_base.items() if len(names) > 1}
    if needs_geo:
        print(f"Geo-disambiguating {len(needs_geo)} colliding alias(es): {sorted(needs_geo)}")

    by_alias: dict[str, dict] = {}
    for r, base in zip(rows, base_aliases):
        geo = extract_geo(r.activity_name) if base in needs_geo else ""
        final_alias = f"{base}-{geo}" if geo else base
        entry = build_activity_entry(r, alias_override=final_alias)
        if final_alias in by_alias:
            by_alias[final_alias]["metadata"].extend(entry["metadata"])
        else:
            by_alias[final_alias] = entry
    return list(by_alias.values())


def write_json(results: list[PredictionRow], output_path: str):
    """Write activities to JSON, grouped by activity alias."""
    activities = assemble_activities(results)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, indent=2, ensure_ascii=False)
    print(f"JSON written to {output_path} ({len(activities)} activities, {sum(len(a['metadata']) for a in activities)} ingredients)")


# =============================================================================
# CLI
# =============================================================================

INPUT_CSV_DIR = Path(__file__).parent / "source"
GENERATED_DIR = Path(__file__).parent / "generated"


def get_input_csv(variant: Variant) -> Path:
    return INPUT_CSV_DIR / f"new_ingredient_{variant.value}.csv"


def fetch_source_csv(variant: Variant) -> Path:
    """Refresh the local source CSV from the Google Sheet tab for `variant`."""
    sheet_id = os.environ["GSHEET_ID"]
    gid = os.environ[f"GSHEET_GID_{variant.value}"]
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    target = get_input_csv(variant)
    print(f"Fetching {variant.value} from Google Sheet (gid={gid})...")
    with urlopen(url, timeout=30) as resp:
        data = resp.read().replace(b"\r\n", b"\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    print(f"  wrote {len(data)} bytes to {target}")
    return target


def load_source_csv(variant: Variant) -> pd.DataFrame:
    df = pd.read_csv(get_input_csv(variant))
    df.columns = df.columns.str.lower().str.replace("_", " ")
    if "under review" in df.columns:
        mask = df["under review"].astype(str).str.strip().str.upper() == "TRUE"
        if mask.any():
            print(f"Forcing visible=FALSE on {int(mask.sum())} rows marked 'under review'")
            df.loc[mask, "visible"] = "FALSE"
    return df


def get_output_paths(variant: Variant) -> tuple[Path, Path, Path]:
    s = f"_{variant.value}"
    return (GENERATED_DIR / f"predictions{s}.csv",
            GENERATED_DIR / f"new_activities{s}.json",
            GENERATED_DIR / f"fichier_final{s}.csv")


IMPACT_COLUMNS = ["acd", "cch", "etf-c", "fru", "fwe", "htc-c", "htn-c", "ior",
                  "ldu", "mru", "ozd", "pco", "pma", "swe", "tre", "wtu", "ecs"]

ECOSYSTEMIC_SERVICES_MULTIPLIERS = {
    "cropDiversity": -1.5, "hedges": -3, "livestockDensity": 3000,
    "permanentPasture": -7, "plotSize": -4,
}

# (result_column, food_meta_key) — `categories` is special-cased (joined with ";").
FOOD_META_COLS = [
    ("transportCooling", "transportCooling"),
    ("cropGroup",        "cropGroup"),
    ("defaultOrigin",    "defaultOrigin"),
    ("density",          "ingredientDensity"),
    ("inediblePart",     "inediblePart"),
    ("rawToCookedRatio", "rawToCookedRatio"),
]


def _row_food_meta(activity: dict | None) -> dict[str, str]:
    m = next((x for x in (activity or {}).get("metadata", []) if "food" in x.get("scopes", [])), {})
    out: dict[str, str] = {"categories": ";".join(m.get("ingredientCategories", []))}
    out.update((col, m.get(key, "")) for col, key in FOOD_META_COLS)
    return out


def _row_impacts(process: dict | None) -> dict[str, str]:
    impacts = (process or {}).get("impacts", {})
    return {col: impacts.get(col, "") for col in IMPACT_COLUMNS}


def _row_ecosystemic(es: dict) -> dict[str, float | str]:
    out: dict[str, float | str] = {}
    for field, multiplier in ECOSYSTEMIC_SERVICES_MULTIPLIERS.items():
        raw = es.get(field) or 0
        out[field] = raw * multiplier if raw else ""
    return out


def enrich_row(
    row,
    variant: Variant,
    activities_by_alias: dict,
    processes_by_name: dict,
    es_by_base_alias: dict,
) -> dict:
    """Combine source-CSV row + predicted metadata + impacts + ES into one record.

    Pure data transformation: all I/O happens upstream (the three lookup dicts).
    """
    result = dict(row)
    activity_name = clean_activity_name(str(row["icv final"]))
    geo = RowGeo(
        production_fr=str(row.get("production fr", "")).strip(),
        antilles=str(row.get("antilles", "")).strip().upper() == "TRUE",
    )
    cfg = resolve_variant(variant, geo)
    base_alias = generate_alias(row["item"])
    row_alias = base_alias + cfg.alias_suffix

    result.update(_row_food_meta(activities_by_alias.get(row_alias)))
    result.update(_row_impacts(processes_by_name.get(activity_name)))
    result.update(_row_ecosystemic(es_by_base_alias.get(base_alias) or {}))
    return result


def _strip_variant_suffix(alias: str) -> str:
    """Strip legacy `-2025` then any variant suffix (longest match wins)."""
    if alias.endswith(OLD_ALIAS_SUFFIX):
        alias = alias[: -len(OLD_ALIAS_SUFFIX)]
    for sfx in sorted(KNOWN_ALIAS_SUFFIXES, key=len, reverse=True):
        if alias.endswith(sfx):
            return alias[: -len(sfx)]
    return alias


def build_es_by_base_alias(ingredients: list[dict]) -> dict[str, dict]:
    """Map base alias → ecosystemicServices, picking the first non-empty entry per base.
    ES is variant-agnostic; only `scenario` differs across variants of the same ingredient."""
    es_by_base: dict[str, dict] = {}
    for ing in ingredients:
        al = ing.get("alias")
        es = ing.get("ecosystemicServices") or {}
        if not al or not any(es.values()):
            continue
        es_by_base.setdefault(_strip_variant_suffix(al), es)
    return es_by_base


def _load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def generate_final_data(variant: Variant, fetch: bool = True):
    """Combine source CSV + predicted metadata (new_activities.json) + impacts
    (processes_impacts.json) + ES (ingredients.json, by base alias) into one CSV."""
    output_csv, output_json, final_output_csv = get_output_paths(variant)

    print(f"Loading {get_input_csv(variant)}...")
    if fetch:
        fetch_source_csv(variant)
    source_df = load_source_csv(variant)

    ecobalyse_data = Path(os.environ["ECOBALYSE_DATA"])
    ecobalyse = Path(os.environ["ECOBALYSE"])

    processes_path = ecobalyse_data / "public/data/processes_impacts.json"
    ingredients_path = ecobalyse / "public/data/food/ingredients.json"
    print(f"Loading {processes_path}...")
    processes_by_name = {p["activityName"]: p for p in _load_json(processes_path)}
    print(f"Loading {output_json}...")
    activities_by_alias = {a["alias"]: a for a in _load_json(output_json)}
    print(f"Loading {ingredients_path} (ES lookup only)...")
    es_by_base_alias = build_es_by_base_alias(_load_json(ingredients_path))

    print(
        f"\nLoaded: {len(activities_by_alias)} activities, {len(processes_by_name)} processes,"
        f" {len(es_by_base_alias)} ecosystemicServices entries (by base alias)"
    )

    print(f"Processing {len(source_df)} ingredients...")
    results = [
        enrich_row(row, variant, activities_by_alias, processes_by_name, es_by_base_alias)
        for _, row in source_df.iterrows()
    ]
    matched_processes = sum(
        1 for _, row in source_df.iterrows()
        if processes_by_name.get(clean_activity_name(str(row["icv final"])))
    )

    pd.DataFrame(results).to_csv(final_output_csv, index=False)
    print(f"\nMatched: {matched_processes}/{len(results)} processes with impacts")
    print(f"Final data written to {final_output_csv}")


def remove_old(target_catalog_dir: Path):
    """Remove activities/ingredients whose alias ends with '-2025' or displayName ends with ' (2025)'.

    Also removes feed.json entries whose top-level key ends with '-2025'.
    """
    old_alias_suffix = OLD_ALIAS_SUFFIX
    old_display_suffix = OLD_DISPLAY_SUFFIX

    activities_list = load_lci_catalog(target_catalog_dir)

    filtered = []
    removed_activities = 0
    removed_ingredients = 0
    for activity in activities_list:
        alias = activity.get("alias", "")
        display = activity.get("displayName", "")
        if alias.endswith(old_alias_suffix) or display.endswith(old_display_suffix):
            removed_activities += 1
            continue

        # Filter food ingredients within the activity
        meta_list = activity.get("metadata", [])
        food_ings = [m for m in meta_list if "food" in m.get("scopes", [])]
        if food_ings:
            new_food = []
            for ing in food_ings:
                ing_alias = ing.get("alias", "")
                ing_display = ing.get("displayName", "")
                if ing_alias.endswith(old_alias_suffix) or ing_display.endswith(old_display_suffix):
                    removed_ingredients += 1
                else:
                    new_food.append(ing)
            non_food = [m for m in meta_list if "food" not in m.get("scopes", [])]
            if new_food:
                activity = {**activity, "metadata": non_food + new_food}
            else:
                if non_food:
                    activity = {**activity, "metadata": non_food}
                else:
                    activity = {k: v for k, v in activity.items() if k != "metadata"}

        filtered.append(activity)

    write_lci_catalog(filtered, target_catalog_dir)
    print(f"lci_catalog: removed {removed_activities} activities, {removed_ingredients} ingredients")

    # Load and filter feed.json
    feed_path = target_catalog_dir.parent / "food/ecosystemic_services/feed.json"
    if feed_path.exists():
        with open(feed_path, encoding="utf-8") as f:
            feed_data = json.load(f)

        removed_feed = 0
        filtered_feed = {}
        for key, value in feed_data.items():
            if key.endswith(old_alias_suffix):
                removed_feed += 1
            else:
                filtered_feed[key] = value
        feed_data = filtered_feed

        with open(feed_path, "w", encoding="utf-8") as f:
            json.dump(feed_data, f, indent=2, ensure_ascii=False)
        print(f"feed.json: removed {removed_feed} entries")

    print("Done!")


# Predictions are deterministic from (item, activity_name) — variant should not
# affect them. Derived from PRED_FIELDS so adding/removing predictor outputs
# stays in lockstep.
VARIANT_INDEPENDENT_FIELDS = [f.key for f in PRED_FIELDS] + ["categories"]


def compare_variants(generated_dir: Path):
    """Compare predictions across variant CSVs and warn on mismatches."""
    csv_files = sorted(generated_dir.glob("predictions_*.csv"))
    if len(csv_files) < 2:
        return

    variant_data = {}
    for csv_file in csv_files:
        variant = csv_file.stem.replace("predictions_", "")
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            variant_data[variant] = {row["name"]: row for row in reader}

    variants = list(variant_data.keys())
    all_names = set()
    for data in variant_data.values():
        all_names.update(data.keys())

    mismatches = 0
    for name in sorted(all_names):
        present = {v: variant_data[v][name] for v in variants if name in variant_data[v]}
        if len(present) < 2:
            continue
        variant_list = list(present.keys())
        for field in VARIANT_INDEPENDENT_FIELDS:
            values = {v: present[v].get(field, "") for v in variant_list}
            unique = set(values.values())
            if len(unique) > 1:
                parts = ", ".join(f"{v}={values[v]}" for v in variant_list)
                print(f"WARNING: '{name}' has different '{field}' across variants: {parts}")
                mismatches += 1

    if mismatches:
        print(f"\n{mismatches} cross-variant mismatches found.")
    else:
        print("\nNo cross-variant mismatches found.")


def main():
    parser = argparse.ArgumentParser(description="Export predicted ingredients to CSV and JSON")
    parser.add_argument("command", choices=["metadata", "final_data", "remove-old"],
        help="metadata: export predictions + merge activities. final_data: generate final CSV with impacts. remove-old: remove suffixed entries")
    parser.add_argument("--variant", type=lambda v: Variant[v.upper()], choices=list(Variant),
        metavar="{FR,ORG,UE,OI,NUE}", help="Variant (required for metadata/final_data)")
    parser.add_argument("--clear-cache", action="store_true", help="Clear translation cache before running")
    parser.add_argument("--add-old-suffix", action="store_true",
        help="Add '(2025)' suffix to pre-existing ingredient displayNames and '-2025' to their aliases")
    parser.add_argument("--no-fetch", action="store_true",
        help="Skip Google Sheet fetch, use existing local source/*.csv as-is")
    args = parser.parse_args()

    if args.command == "remove-old":
        remove_old(Path(os.environ["ECOBALYSE_DATA"]) / "lci_catalog")
        return

    if args.variant is None:
        parser.error("--variant is required for metadata and final_data commands")

    if args.command == "final_data":
        generate_final_data(args.variant, fetch=not args.no_fetch)
        return

    if args.clear_cache:
        Predictor.clear_translation_cache()
        print("Translation cache cleared")

    ecobalyse_data = Path(os.environ["ECOBALYSE_DATA"])
    output_csv, output_json, _ = get_output_paths(args.variant)

    predictor = Predictor()
    predictor.fit()

    print(f"\nLoading {get_input_csv(args.variant)}...")
    if not args.no_fetch:
        fetch_source_csv(args.variant)
    df = load_source_csv(args.variant)
    if "item" not in df.columns or "icv final" not in df.columns:
        raise ValueError("CSV must have 'item' and 'icv final' columns")

    print(f"\nProcessing {len(df)} ingredients...")
    results = predict_all(predictor, df, args.variant)

    print(f"\nWriting {len(results)} results...")
    write_csv(results, output_csv)
    write_json(results, output_json)
    compare_variants(GENERATED_DIR)

    catalog_dir = ecobalyse_data / "lci_catalog"
    if not catalog_dir.exists():
        print(f"\nWarning: lci_catalog directory does not exist: {catalog_dir}")
        print("\nDone!")
        return

    merge_activities(output_json, catalog_dir, args.add_old_suffix)
    ref_dst = ecobalyse_data / "food/metadata"
    ref_dst.mkdir(parents=True, exist_ok=True)
    for csv_file in sorted((Path(__file__).parent / "reference").glob("*.csv")):
        shutil.copy2(csv_file, ref_dst / csv_file.name)
    print(f"Copied reference files to {ref_dst}")
    print("\nNext step: run 'just export-all' in ecobalyse-data to regenerate ingredients.json")
    print("\nDone!")


if __name__ == "__main__":
    main()
